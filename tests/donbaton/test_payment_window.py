"""Окно оплаты: 48 часов после окончания подписки остаётся один сервер.

Клиенту в РФ без VPN не открыть ни Telegram, ни кабинет — заплатить нечем.
Поэтому на время окна истёкшая подписка Wi-Fi/LTE держит в панели ровно один
сквад (Telegram + наша инфраструктура), статус ACTIVE и дату «конец срока +
окно». Пишет это тот же владелец доступа, что и всё остальное, —
``apply_panel_policy`` (цикл LTE и ``push_subscription``); конец окна снимает
тот же цикл, оплата в окне даёт обычные сквады. Импорт из панели и вебхуки
состояние окна обратно в подписку не несут.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings, normalize_squad_uuid, settings
from app.database.models import LteOrphanQuota, LteQuota, Subscription, User
from app.external.remnawave_api import TrafficLimitStrategy, UserStatus
from app.services import donbaton_payment_window as window_module
from app.services.donbaton_payment_window import payment_window, window_squad
from app.services.lte_quota_service import (
    apply_panel_policy,
    get_quota_status,
    initialize_quota,
    reconcile_subscription,
    run_quota_cycle,
    update_entitlement,
)
from app.services.panel_sync import (
    BULK_SNAPSHOT,
    WEBHOOK,
    PanelIdentity,
    apply_managed_policy,
    build_panel_payload,
    project_onto_subscription,
    push_subscription,
)
from app.services.panel_sync.projection import PanelSnapshot
from app.services.remnawave_webhook_service import RemnaWaveWebhookService
from tests.donbaton.test_lte_quota import LTE, TABLES as QUOTA_TABLES, WIFI, tariff
from tests.fixtures.sqlite_memory import memory_session


WINDOW = '00000000-0000-4000-8000-000000000003'
TABLES = [*QUOTA_TABLES, LteOrphanQuota.__table__]
HOURS = 48


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    monkeypatch.setattr(settings, 'DONBATON_PAYMENT_WINDOW_HOURS', HOURS)
    monkeypatch.setattr(settings, 'DONBATON_PAYMENT_WINDOW_SQUAD_UUID', WINDOW)
    monkeypatch.setattr(settings, 'DONBATON_PAYMENT_WINDOW_TRIAL_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_PAYMENT_WINDOW_SERVER_NAME', '💳 Оплата | Telegram')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


async def seed(db, *, ended_ago=timedelta(minutes=1), status='expired', user_status='active', **overrides):
    """Платная подписка Wi-Fi + LTE, закончившаяся ``ended_ago`` назад."""
    plan = tariff()
    user = User(id=1, telegram_id=123, first_name='Test', status=user_status, remnawave_id=11, language='ru')
    db.add_all([user, plan])
    await db.flush()
    values = dict(
        id=1,
        user_id=1,
        status=status,
        is_trial=False,
        tariff_id=plan.id,
        start_date=now_utc() - timedelta(days=30),
        end_date=now_utc() - ended_ago,
        connected_squads=[WIFI, LTE],
        device_limit=3,
        remnawave_id=11,
        remnawave_short_id='lte001',
    )
    values.update(overrides)
    sub = Subscription(**values)
    db.add(sub)
    await db.flush()
    quota = await initialize_quota(db, sub, plan, now=sub.start_date)
    quota.limit_bytes = 100
    await db.commit()
    sub.tariff = plan
    return sub, quota, plan, user


class FakePanel:
    """Один аккаунт панели: отдаёт снимок и применяет PATCH, как панель."""

    def __init__(self, *, status='EXPIRED', squads=(WIFI, LTE), expire_at=None, lifetime=1000):
        self.user = SimpleNamespace(
            id=11,
            username='client_11',
            status=UserStatus(status),
            traffic_limit_bytes=0,
            traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
            active_internal_squads=[{'uuid': squad} for squad in squads],
            expire_at=expire_at,
            hwid_device_limit=3,
            user_traffic=SimpleNamespace(lifetime_used_traffic_bytes=lifetime, used_traffic_bytes=0),
            short_uuid='short11',
            subscription_url='https://sub.example/short11',
            happ_crypto_link=None,
        )
        self.updates = []
        self.get_user_by_id = AsyncMock(side_effect=self._get)

    async def _get(self, panel_id):
        return self.user if panel_id == self.user.id else None

    async def update_user(self, **kwargs):
        self.updates.append(kwargs)
        for field in ('status', 'traffic_limit_bytes', 'traffic_limit_strategy', 'expire_at', 'hwid_device_limit'):
            if field in kwargs:
                setattr(self.user, field, kwargs[field])
        if 'active_internal_squads' in kwargs:
            self.user.active_internal_squads = [{'uuid': squad} for squad in kwargs['active_internal_squads']]
        return self.user

    async def create_user(self, **kwargs):  # pragma: no cover - писатель берёт атрибут заранее
        raise AssertionError('аккаунт уже есть — создавать нечего')

    def squads(self):
        return [item['uuid'] for item in self.user.active_internal_squads]


def window_end(sub):
    return sub.end_date + timedelta(hours=HOURS)


# ============ Решение политики ============


@pytest.mark.asyncio
@pytest.mark.parametrize('ended_ago', [timedelta(minutes=1), timedelta(hours=47)])
async def test_expired_paid_subscription_keeps_only_the_window_squad(monkeypatch, ended_ago):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, _ = await seed(db, ended_ago=ended_ago)
        projected = await apply_panel_policy(db, sub, {}, panel_user=FakePanel().user, now=now_utc())

        assert projected['status'] == UserStatus.ACTIVE
        assert projected['active_internal_squads'] == [WINDOW]
        assert projected['expire_at'] == window_end(sub)
        # Безлимит панели, как у любой управляемой подписки: маршрутизация окна
        # пропускает только Telegram и наши домены, а LTE-учёт не касается сквада.
        assert projected['traffic_limit_bytes'] == 0
        assert projected['traffic_limit_strategy'] == TrafficLimitStrategy.NO_RESET
        # Лимит устройств не трогается: в запросе его нет — остаётся как был.
        assert 'hwid_device_limit' not in projected


@pytest.mark.asyncio
async def test_window_payload_reaches_the_panel_with_its_own_date(monkeypatch):
    """Путь «подписка целиком» (покупка, админка, update_remnawave_user)."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, user = await seed(db)
        now = now_utc()
        base = build_panel_payload(user, sub, multi_tariff=False, now=now)
        assert base.status == UserStatus.EXPIRED  # без окна — погашенный аккаунт

        projected = await apply_managed_policy(db, sub, base, panel_user=FakePanel().user, now=now)
        kwargs = projected.update_kwargs(user_id=11, now=now)
        assert kwargs['status'] == UserStatus.ACTIVE
        assert kwargs['active_internal_squads'] == [WINDOW]
        assert kwargs['expire_at'] == window_end(sub)
        assert kwargs['hwid_device_limit'] == 3
        # Узкая правка (мониторинг, sync_squads=False) не теряет ни сквад, ни дату окна.
        narrow = projected.update_kwargs(user_id=11, now=now, only_fields={'description'})
        assert narrow['status'] == UserStatus.ACTIVE
        assert narrow['active_internal_squads'] == [WINDOW]
        assert narrow['expire_at'] == window_end(sub)
        created = projected.create_kwargs(now=now)
        assert created['status'] == UserStatus.ACTIVE
        assert created['expire_at'] == window_end(sub)
        # Настоящий срок подписки не подменяется.
        assert projected.end_date == sub.end_date


@pytest.mark.asyncio
@pytest.mark.parametrize('known', ['snapshot', 'id_only'])
async def test_writer_does_not_extinguish_the_window_date(monkeypatch, known):
    """Писатель гасит будущую дату истёкшей подписки — но не дату окна.

    ``id_only`` — как ``subscription_service.update_remnawave_user``: дата панели
    заранее неизвестна, и писатель узнаёт её из ответа на PATCH.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, user = await seed(db, ended_ago=timedelta(hours=1))
        panel = FakePanel(status='ACTIVE', squads=(WINDOW,), expire_at=window_end(sub))
        identity = PanelIdentity(panel_user=panel.user) if known == 'snapshot' else PanelIdentity(known_id=11)

        result = await push_subscription(panel, user, sub, db=db, identity=identity, reset_devices=False)

        assert len(panel.updates) == 1  # второго запроса «погасить дату» нет
        assert result.expiry_extinguished is False
        assert panel.updates[0]['expire_at'] == window_end(sub)
        assert panel.user.expire_at == window_end(sub)
        assert panel.squads() == [WINDOW]


@pytest.mark.asyncio
async def test_after_the_window_access_is_cleared_as_before(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, _ = await seed(db, ended_ago=timedelta(hours=49))
        projected = await apply_panel_policy(db, sub, {}, panel_user=FakePanel().user, now=now_utc())

        assert projected['active_internal_squads'] == []
        assert 'expire_at' not in projected
        assert 'status' not in projected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('key', 'value'),
    [
        ('DONBATON_PAYMENT_WINDOW_HOURS', 0),
        ('DONBATON_PAYMENT_WINDOW_SQUAD_UUID', ''),
        ('DONBATON_PAYMENT_WINDOW_SQUAD_UUID', 'not-a-uuid'),
        # Сквад окна, совпавший с Wi-Fi или LTE, отдал бы истёкшей подписке обычный доступ.
        ('DONBATON_PAYMENT_WINDOW_SQUAD_UUID', WIFI),
        ('DONBATON_PAYMENT_WINDOW_SQUAD_UUID', LTE),
    ],
)
async def test_disabled_or_misconfigured_window_behaves_as_before(monkeypatch, key, value):
    monkeypatch.setattr(settings, key, value)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, user = await seed(db)
        projected = await apply_panel_policy(db, sub, {}, panel_user=FakePanel().user, now=now_utc())

        assert projected['active_internal_squads'] == []
        assert 'expire_at' not in projected
        assert window_squad() is None
        assert window_module.cycle_candidates(now_utc()) is None
        assert window_module.expired_notice_hint(sub, user) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('trial_enabled', [False, True])
async def test_trial_gets_the_window_only_with_its_flag(monkeypatch, trial_enabled):
    monkeypatch.setattr(settings, 'DONBATON_PAYMENT_WINDOW_TRIAL_ENABLED', trial_enabled)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, _ = await seed(db, is_trial=True, status='trial')
        projected = await apply_panel_policy(db, sub, {}, panel_user=FakePanel().user, now=now_utc())

        assert projected['active_internal_squads'] == ([WINDOW] if trial_enabled else [])
        assert ('expire_at' in projected) is trial_enabled


@pytest.mark.asyncio
async def test_blocked_user_gets_no_window_on_either_path(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, user = await seed(db, user_status='blocked')
        now = now_utc()
        # Путь «подписка целиком»: блокировку несёт статус DISABLED набора полей.
        base = build_panel_payload(user, sub, multi_tariff=False, now=now)
        assert base.status == UserStatus.DISABLED
        projected = await apply_managed_policy(db, sub, base, panel_user=FakePanel().user, now=now)
        assert projected.active_internal_squads == ()
        assert projected.payment_window_until is None
        assert 'expire_at' not in projected.update_kwargs(user_id=11, now=now)

        # Цикл LTE: блокировку проверяет сам цикл по пользователю.
        panel = FakePanel(status='DISABLED')
        await reconcile_subscription(db, sub, panel, user=user)
        assert panel.updates[-1]['active_internal_squads'] == []
        assert panel.updates[-1]['status'] == UserStatus.DISABLED
        assert 'expire_at' not in panel.updates[-1]
        assert payment_window(sub, user=user) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['disabled', 'pending'])
async def test_subscription_disabled_in_the_bot_gets_no_window(monkeypatch, status):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, user = await seed(db, status=status)
        now = now_utc()
        projected = await apply_managed_policy(
            db, sub, build_panel_payload(user, sub, multi_tariff=False, now=now), panel_user=FakePanel().user, now=now
        )
        assert projected.active_internal_squads == ()
        assert projected.payment_window_until is None


@pytest.mark.asyncio
async def test_live_and_lte_exhausted_subscriptions_are_untouched(monkeypatch):
    """Окно — только про истечение срока. Исчерпанный LTE по-прежнему оставляет Wi-Fi."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, _ = await seed(db, status='active', end_date=now_utc() + timedelta(days=20))
        now = now_utc()
        ready = await apply_panel_policy(db, sub, {}, panel_user=FakePanel(lifetime=1000).user, now=now)
        assert ready['active_internal_squads'] == [WIFI, LTE]
        exhausted = await apply_panel_policy(
            db, sub, {}, panel_user=FakePanel(lifetime=1200).user, now=now + timedelta(seconds=1)
        )
        assert exhausted['active_internal_squads'] == [WIFI]
        for projected in (ready, exhausted):
            assert 'expire_at' not in projected
            assert WINDOW not in projected['active_internal_squads']


@pytest.mark.asyncio
async def test_plain_subscription_is_not_managed_and_gets_no_window():
    plain = SimpleNamespace(
        network_category=None, status='expired', is_trial=False, end_date=now_utc() - timedelta(minutes=1)
    )
    assert payment_window(plain) is None


# ============ Цикл LTE ============


@pytest.mark.asyncio
async def test_cycle_opens_the_window_once_and_does_not_rewrite_it(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _, user = await seed(db, status='active')
        panel = FakePanel(status='EXPIRED', expire_at=sub.end_date)  # панель уже погасила аккаунт сама

        await reconcile_subscription(db, sub, panel, user=user)
        assert len(panel.updates) == 1
        sent = panel.updates[0]
        assert sent['status'] == UserStatus.ACTIVE
        assert sent['active_internal_squads'] == [WINDOW]
        assert sent['expire_at'] == window_end(sub)
        await db.refresh(quota)
        assert quota.panel_access_cleared_at is None  # окно держит подписку в цикле

        # Панель вернула дату с миллисекундами — это та же дата, PATCH не нужен.
        panel.user.expire_at = window_end(sub) + timedelta(milliseconds=400)
        await reconcile_subscription(db, sub, panel, user=user)
        assert len(panel.updates) == 1

        # Дату окна кто-то сдвинул (погасил вручную) — цикл возвращает её.
        panel.user.expire_at = now_utc() + timedelta(minutes=5)
        await reconcile_subscription(db, sub, panel, user=user)
        assert len(panel.updates) == 2
        assert panel.user.expire_at == window_end(sub)


@pytest.mark.asyncio
async def test_panel_disable_still_closes_the_window(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, user = await seed(db)
        panel = FakePanel(status='DISABLED', squads=(WINDOW,), expire_at=window_end(sub))

        await reconcile_subscription(db, sub, panel, user=user)
        sent = panel.updates[-1]
        assert sent['active_internal_squads'] == []
        assert 'status' not in sent and 'expire_at' not in sent


@pytest.mark.asyncio
async def test_cycle_selects_the_window_and_clears_access_after_it(monkeypatch):
    """Подписка, истёкшая до включения окна, возвращается в цикл на остаток окна."""
    from app.database import database

    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _, _ = await seed(db, ended_ago=timedelta(hours=47))
        quota.panel_access_cleared_at = sub.end_date  # доступ сняли до включения окна
        await db.commit()
        monkeypatch.setattr(database, 'AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False))
        panel = FakePanel(status='EXPIRED', squads=(), expire_at=sub.end_date)

        @asynccontextmanager
        async def client():
            yield panel

        service = SimpleNamespace(is_configured=True, get_api_client=client)

        await run_quota_cycle(service)
        assert panel.get_user_by_id.await_count == 1
        assert panel.squads() == [WINDOW]
        assert panel.user.status == UserStatus.ACTIVE
        assert panel.user.expire_at == window_end(sub)
        cleared = (await db.execute(select(LteQuota.panel_access_cleared_at))).scalar_one()
        assert cleared is None

        # Окно кончилось: панель погасила аккаунт по дате окна, цикл снимает сквад.
        await db.execute(Subscription.__table__.update().values(end_date=now_utc() - timedelta(hours=HOURS, minutes=1)))
        await db.commit()
        panel.user.status = UserStatus.EXPIRED
        await run_quota_cycle(service)
        assert panel.squads() == []
        assert 'expire_at' not in panel.updates[-1] and 'status' not in panel.updates[-1]
        cleared = (await db.execute(select(LteQuota.panel_access_cleared_at))).scalar_one()
        assert cleared is not None

        # Дальше подписка в цикл не попадает.
        reads = panel.get_user_by_id.await_count
        await run_quota_cycle(service)
        assert panel.get_user_by_id.await_count == reads


@pytest.mark.asyncio
async def test_cycle_opens_the_window_for_an_expired_trial_by_default(monkeypatch):
    """Решение владельца: пробным окно даём (флаг по умолчанию включён)."""
    from app.database import database

    monkeypatch.setattr(
        settings,
        'DONBATON_PAYMENT_WINDOW_TRIAL_ENABLED',
        Settings.model_fields['DONBATON_PAYMENT_WINDOW_TRIAL_ENABLED'].default,
    )
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _, _ = await seed(db, is_trial=True, status='trial', ended_ago=timedelta(hours=1))
        quota.panel_access_cleared_at = sub.end_date
        await db.commit()
        monkeypatch.setattr(database, 'AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False))
        panel = FakePanel(squads=())

        @asynccontextmanager
        async def client():
            yield panel

        await run_quota_cycle(SimpleNamespace(is_configured=True, get_api_client=client))
        assert panel.squads() == [WINDOW]
        assert panel.user.status == UserStatus.ACTIVE
        assert panel.user.expire_at == window_end(sub)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['trial', 'blocked'])
async def test_cycle_does_not_reselect_cleared_subscriptions_without_a_window(monkeypatch, case):
    """Снятая пробная при выключенном флаге и заблокированный — в цикл не возвращаются."""
    from app.database import database

    overrides = {'trial': {'is_trial': True, 'status': 'trial'}, 'blocked': {'user_status': 'blocked'}}[case]
    monkeypatch.setattr(settings, 'DONBATON_PAYMENT_WINDOW_TRIAL_ENABLED', False)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _, _ = await seed(db, **overrides)
        quota.panel_access_cleared_at = sub.end_date
        await db.commit()
        monkeypatch.setattr(database, 'AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False))
        panel = FakePanel(squads=())

        @asynccontextmanager
        async def client():
            yield panel

        await run_quota_cycle(SimpleNamespace(is_configured=True, get_api_client=client))
        panel.get_user_by_id.assert_not_awaited()


# ============ Оплата в окне ============


@pytest.mark.asyncio
async def test_payment_inside_the_window_restores_regular_squads_and_new_date(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plan, user = await seed(db, ended_ago=timedelta(hours=10))
        panel = FakePanel(status='EXPIRED', expire_at=sub.end_date, lifetime=1000)
        await reconcile_subscription(db, sub, panel, user=user)
        assert panel.squads() == [WINDOW]
        # «Грязная» отметка снятия доступа от прошлых проходов не должна ни на что влиять.
        quota.panel_access_cleared_at = now_utc() - timedelta(hours=9)
        await db.commit()

        # Оплата: тариф на 30 дней с текущего момента (любой путь покупки идёт через это).
        now = now_utc()
        await update_entitlement(db, sub, plan, now=now)
        sub.status = 'active'
        sub.end_date = now + timedelta(days=30)
        await db.commit()

        await push_subscription(
            panel, user, sub, db=db, identity=PanelIdentity(panel_user=panel.user), reset_devices=False
        )
        sent = panel.updates[-1]
        assert sent['status'] == UserStatus.ACTIVE
        assert sent['expire_at'] == sub.end_date
        assert WINDOW not in sent['active_internal_squads']
        assert WIFI in sent['active_internal_squads']
        assert panel.user.expire_at == sub.end_date

        # Цикл выбирает её как живую и держит обычные сквады, дату больше не пишет.
        written = len(panel.updates)
        await reconcile_subscription(db, sub, panel, user=user)
        assert panel.squads() == [WIFI, LTE]
        assert all('expire_at' not in update for update in panel.updates[written:])
        assert panel.user.expire_at == sub.end_date
        await db.refresh(quota)
        assert quota.panel_access_cleared_at is None


@pytest.mark.asyncio
async def test_window_traffic_on_a_zero_multiplier_node_is_not_charged(monkeypatch):
    """Сквад окна стоит на ноде с множителем 0: счётчик панели не растёт — списывать нечего."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _, user = await seed(db, status='active')
        panel = FakePanel(lifetime=1000)
        await reconcile_subscription(db, sub, panel, user=user)
        for _ in range(3):  # проходы цикла в окне: трафик Telegram не меняет lifetime
            await reconcile_subscription(db, sub, panel, user=user)
        await db.refresh(quota)
        assert quota.used_bytes == 0
        assert quota.counter_bytes == 1000


@pytest.mark.asyncio
async def test_window_bytes_never_eat_the_newly_paid_period(monkeypatch):
    """Даже если окно по ошибке на ноде с множителем: оплаченный в окне период цел.

    Дельту, пришедшую после оплаты, леджер относит к старым корзинам (до начала
    нового периода), поэтому новый пакет LTE начинается нетронутым.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plan, user = await seed(db, ended_ago=timedelta(hours=5))
        now = now_utc()
        await apply_panel_policy(db, sub, {}, panel_user=FakePanel(lifetime=1000).user, now=now)
        await db.commit()
        await update_entitlement(db, sub, plan, now=now + timedelta(seconds=1))
        sub.status = 'active'
        sub.end_date = now + timedelta(days=30)
        await db.commit()

        # Счётчик вырос на 50 за время окна (неверный множитель ноды окна).
        after = await apply_panel_policy(
            db, sub, {}, panel_user=FakePanel(lifetime=1050).user, now=now + timedelta(seconds=2)
        )
        status = await get_quota_status(db, sub, now=now + timedelta(seconds=2))
        assert status['used_bytes'] == 0
        assert status['remaining_bytes'] == plan.lte_traffic_limit_gb * 1024**3
        assert after['active_internal_squads'] == [WIFI, LTE]


# ============ Панель → бот: окно не оживляет подписку ============


def expired_managed(**overrides):
    values = dict(
        id=1,
        network_category='wifi_lte',
        status='expired',
        is_trial=False,
        end_date=now_utc() - timedelta(hours=1),
        traffic_used_gb=0.0,
        traffic_limit_gb=0,
        device_limit=3,
        connected_squads=[WIFI, LTE],
        remnawave_short_uuid='short11',
        subscription_url=None,
        subscription_crypto_link=None,
        updated_at=None,
        last_webhook_update_at=None,
        grace_candidate_reason=None,
        grace_candidate_at=None,
        grace_session_open=False,
        grace_tail_expire_at=None,
        grace_overlay_expire_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize('policy', [BULK_SNAPSHOT, WEBHOOK])
def test_window_state_in_the_panel_is_not_imported(policy):
    sub = expired_managed()
    end_date = sub.end_date
    snapshot = PanelSnapshot(
        status='ACTIVE', expire_at=end_date + timedelta(hours=HOURS), squads=(WINDOW,), traffic_limit_gb=0
    )

    project_onto_subscription(sub, snapshot, policy=policy, now=now_utc())

    assert sub.status == 'expired'
    assert sub.end_date == end_date
    assert sub.connected_squads == [WIFI, LTE]


@pytest.mark.asyncio
async def test_bulk_sync_skips_managed_subscriptions(monkeypatch):
    """Ночная синхронизация «все» управляемые подписки не трогает вовсе."""
    from app.services.remnawave_service import RemnaWaveService

    sub = Subscription(
        id=1,
        network_category='wifi_lte',
        status='expired',
        traffic_limit_gb=0,
        end_date=now_utc() - timedelta(hours=1),
        connected_squads=[WIFI, LTE],
    )
    monkeypatch.setattr('app.database.crud.subscription.get_subscription_by_user_id', AsyncMock(return_value=sub))
    await RemnaWaveService()._update_subscription_from_panel_data(
        SimpleNamespace(),
        SimpleNamespace(id=1),
        {
            'status': 'ACTIVE',
            'expireAt': (sub.end_date + timedelta(hours=HOURS)).isoformat(),
            'activeInternalSquads': [{'uuid': WINDOW}],
        },
    )
    assert sub.status == 'expired'
    assert sub.end_date < now_utc()


def panel_event(sub, *, hours_after_end=HOURS, **extra):
    data = {
        'id': 11,
        'status': 'ACTIVE',
        'expireAt': (sub.end_date + timedelta(hours=hours_after_end)).isoformat(),
        'activeInternalSquads': [{'uuid': WINDOW}],
        'trafficLimitBytes': 0,
    }
    data.update(extra)
    return data


def webhook_service():
    service = RemnaWaveWebhookService(bot=None)
    service._notify_user = AsyncMock()
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize('handler', ['_handle_user_modified', '_handle_user_enabled'])
async def test_webhooks_do_not_revive_a_subscription_in_the_window(monkeypatch, handler):
    from app.services import remnawave_webhook_service as webhook_module

    monkeypatch.setattr(webhook_module, 'get_open_grace_subscription_ids', AsyncMock(return_value=set()))
    monkeypatch.setattr('app.database.crud.transaction.get_last_subscription_payment_at', AsyncMock(return_value=None))
    sub = Subscription(
        id=1,
        network_category='wifi_lte',
        status='expired',
        traffic_limit_gb=0,
        end_date=now_utc() - timedelta(hours=1),
        connected_squads=[WIFI, LTE],
    )
    end_date = sub.end_date
    service = webhook_service()
    db = SimpleNamespace(commit=AsyncMock())

    await getattr(service, handler)(db, SimpleNamespace(id=1), sub, panel_event(sub))

    assert sub.status == 'expired'
    assert sub.end_date == end_date
    assert sub.connected_squads == [WIFI, LTE]
    service._notify_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_webhook_at_the_window_end_is_not_announced_again(monkeypatch):
    from app.services import remnawave_webhook_service as webhook_module

    consider = AsyncMock()
    monkeypatch.setattr(webhook_module.grace_access_runtime, 'consider_candidate', consider)
    sub = Subscription(
        id=1,
        network_category='wifi_lte',
        status='expired',
        traffic_limit_gb=0,
        end_date=now_utc() - timedelta(hours=HOURS, minutes=1),
        connected_squads=[WIFI, LTE],
    )
    service = webhook_service()
    db = SimpleNamespace(commit=AsyncMock())

    await service._handle_user_expired(
        db, SimpleNamespace(id=1, language='ru'), sub, panel_event(sub, status='EXPIRED')
    )

    service._notify_user.assert_not_awaited()
    assert sub.status == 'expired'

    # Настоящее истечение (дата панели = срок подписки) объявляется, как раньше.
    await service._handle_user_expired(
        db, SimpleNamespace(id=1, language='ru'), sub, panel_event(sub, hours_after_end=0)
    )
    service._notify_user.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('handler', 'extra'),
    [
        ('_handle_expires_in_48h', {}),
        ('_handle_expires_in_24h', {}),
        ('_handle_expired_24h_ago', {}),
        ('_handle_user_expiration', {'_meta': {'expiration': -24}}),
    ],
)
async def test_panel_reminders_about_the_window_date_are_not_sent(handler, extra):
    sub = Subscription(
        id=1,
        network_category='wifi_lte',
        status='expired',
        end_date=now_utc() - timedelta(hours=2),
        connected_squads=[WIFI, LTE],
    )
    service = webhook_service()

    await getattr(service, handler)(
        SimpleNamespace(), SimpleNamespace(id=1, language='ru'), sub, panel_event(sub, **extra)
    )

    service._notify_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_panel_reminders_for_a_live_subscription_still_go_out():
    sub = Subscription(
        id=1,
        network_category='wifi_lte',
        status='active',
        end_date=now_utc() + timedelta(hours=24),
        connected_squads=[WIFI, LTE],
    )
    service = webhook_service()

    await service._handle_expires_in_24h(
        SimpleNamespace(), SimpleNamespace(id=1, language='ru'), sub, panel_event(sub, hours_after_end=0)
    )

    service._notify_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_monitoring_never_takes_the_window_for_a_renewal():
    """Мониторинг перед гашением не спрашивает панель у управляемой подписки."""
    from app.services.monitoring_service import MonitoringService

    service = MonitoringService(bot=None)
    service._read_panel_before_expiry = AsyncMock()
    sub = expired_managed(status='active')

    assert await service._panel_keeps_alive(SimpleNamespace(), sub) is False
    service._read_panel_before_expiry.assert_not_awaited()


# ============ Уведомление «⛔ Подписка истекла» ============


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_expired_notice_names_the_window_server(monkeypatch, enabled):
    from app.services.monitoring_service import MonitoringService

    if not enabled:
        monkeypatch.setattr(settings, 'DONBATON_PAYMENT_WINDOW_HOURS', 0)
    service = MonitoringService(bot=SimpleNamespace())
    service._send_message_with_logo = AsyncMock()
    sub = expired_managed(end_date=now_utc() - timedelta(minutes=3), tariff=None)
    user = SimpleNamespace(id=1, telegram_id=123, status='active', language='ru')

    assert await service._send_subscription_expired_notification(user, sub) is True

    text = service._send_message_with_logo.await_args.kwargs['text']
    assert '⛔ <b>Подписка истекла</b>' in text
    if enabled:
        assert '48 часов' in text
        assert '«💳 Оплата | Telegram»' in text
        assert 'выберите его, чтобы открыть бот и оплатить' in text
        assert 'остальные серверы не работают до оплаты' in text
        assert 'Доступ к серверам заблокирован' not in text
    else:
        assert 'Доступ к серверам заблокирован до продления' in text
        assert 'Оплата | Telegram' not in text


@pytest.mark.parametrize(('language', 'phrase'), [('ru', '48 часов'), ('en', '48 hours'), ('ua', '48 годин')])
def test_expired_hint_is_localized_and_escapes_the_server_name(monkeypatch, language, phrase):
    monkeypatch.setattr(settings, 'DONBATON_PAYMENT_WINDOW_SERVER_NAME', 'Оплата <Telegram>')
    sub = expired_managed(end_date=now_utc() - timedelta(minutes=3))
    hint = window_module.expired_notice_hint(sub, SimpleNamespace(status='active', language=language))

    assert phrase in hint
    assert 'Оплата &lt;Telegram&gt;' in hint
    assert window_module._hours_phrase(1, 'ru') == '1 час'
    assert window_module._hours_phrase(24, 'ru') == '24 часа'


# ============ Настройки ============


def test_squad_uuid_is_validated_and_normalized():
    assert normalize_squad_uuid('  ') == ''
    assert normalize_squad_uuid(WINDOW.upper()) == WINDOW
    with pytest.raises(ValueError, match='UUID'):
        normalize_squad_uuid('telegram-squad')
    with pytest.raises(ValueError):
        Settings(BOT_TOKEN='x', DONBATON_PAYMENT_WINDOW_SQUAD_UUID='telegram-squad', _env_file=None)
    with pytest.raises(ValueError):
        Settings(BOT_TOKEN='x', DONBATON_PAYMENT_WINDOW_HOURS=500, _env_file=None)
    defaults = Settings(BOT_TOKEN='x', _env_file=None)
    assert defaults.DONBATON_PAYMENT_WINDOW_HOURS == 0
    assert defaults.DONBATON_PAYMENT_WINDOW_SQUAD_UUID == ''
    assert defaults.DONBATON_PAYMENT_WINDOW_TRIAL_ENABLED is True  # владелец: пробным окно даём
    assert defaults.DONBATON_PAYMENT_WINDOW_SERVER_NAME == '💳 Оплата | Telegram'


def test_admin_edits_are_validated_in_the_bot_and_the_cabinet():
    from fastapi import HTTPException

    from app.cabinet.routes.admin_settings import _coerce_value
    from app.services.system_settings_service import bot_configuration_service

    bot_configuration_service.initialize_definitions()
    key = 'DONBATON_PAYMENT_WINDOW_SQUAD_UUID'
    assert bot_configuration_service.get_definition(key).category_key == 'DONBATON'
    assert bot_configuration_service.parse_user_value(key, WINDOW.upper()) == WINDOW
    with pytest.raises(ValueError, match='UUID'):
        bot_configuration_service.parse_user_value(key, 'telegram-squad')
    with pytest.raises(ValueError, match='168'):
        bot_configuration_service.parse_user_value('DONBATON_PAYMENT_WINDOW_HOURS', '500')
    assert bot_configuration_service.parse_user_value('DONBATON_PAYMENT_WINDOW_HOURS', '48') == 48
    with pytest.raises(HTTPException) as error:
        _coerce_value(key, 'telegram-squad')
    assert error.value.status_code == 400
    assert _coerce_value(key, WINDOW.upper()) == WINDOW
    for name in (
        'DONBATON_PAYMENT_WINDOW_HOURS',
        'DONBATON_PAYMENT_WINDOW_SQUAD_UUID',
        'DONBATON_PAYMENT_WINDOW_TRIAL_ENABLED',
        'DONBATON_PAYMENT_WINDOW_SERVER_NAME',
    ):
        assert bot_configuration_service.get_setting_guidance(name)['description']


def test_env_example_keeps_the_window_keys_commented():
    from pathlib import Path

    lines = (Path(__file__).resolve().parents[2] / '.env.example').read_text(encoding='utf-8').splitlines()
    assert not [line for line in lines if line.startswith('DONBATON_PAYMENT_WINDOW_')]
    documented = [line for line in lines if line.startswith('# DONBATON_PAYMENT_WINDOW_')]
    assert len(documented) == 4


@pytest.mark.asyncio
async def test_policy_writer_takes_a_date_only_with_the_window_shape():
    from app.services.panel_sync import patch_panel_policy

    panel = FakePanel()
    until = now_utc() + timedelta(hours=HOURS)
    with pytest.raises(ValueError, match='окно оплаты'):
        await patch_panel_policy(
            panel, user_id=11, fields={'active_internal_squads': [WIFI, LTE]}, payment_window_until=until
        )
    assert panel.updates == []
    await patch_panel_policy(
        panel,
        user_id=11,
        fields={'status': UserStatus.ACTIVE, 'active_internal_squads': [WINDOW]},
        payment_window_until=until,
    )
    assert panel.updates == [
        {'user_id': 11, 'expire_at': until, 'status': UserStatus.ACTIVE, 'active_internal_squads': [WINDOW]}
    ]
