"""Уведомления об остатке LTE: «почти закончился» (≤ порога) и «закончился».

Решение и отметка — в цикле учёта, на заблокированной строке и в одной транзакции
со списанием; отправка — после коммита, модулем ``lte_quota_notifications``.
Здесь проверено: одно уведомление на пересечение порога и повтор только после
пополнения; ничего без подтверждённого учёта (``telemetry_unavailable``, grace,
не ``ready``); Wi-Fi-only и безлимит молчат всегда; legacy-пул считается
в реальных байтах (/17); права только в панели — лишь в админ-чат и лишь по
флагу; предохранитель от залпа; пробные — по флагу; сбой доставки не роняет цикл.
"""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from structlog.testing import capture_logs

from app.config import Settings, settings
from app.database.models import LteOrphanQuota, LteQuota, Subscription, TrafficPurchase, User
from app.services import lte_quota_notifications as notify, lte_quota_service
from app.services.admin_notification_service import AdminNotificationService
from app.services.lte_quota_service import (
    GIB,
    LteNoticeCandidate,
    TelemetryEvidence,
    activate_orphan_quota,
    credit_topup,
    decide_lte_notice,
    import_orphan_quota,
    initialize_quota,
    initialize_trial_quota,
    reconcile_orphan_quota,
    reconcile_subscription,
    run_quota_cycle,
)
from app.services.notification_delivery_service import notification_delivery_service
from app.services.notification_types import NotificationType
from app.utils.timezone import format_local_datetime
from tests.donbaton.conftest import TEST_LTE_NODES
from tests.donbaton.test_lte_legacy import BASELINE, EPOCH, IMPORT, activate, legacy_seed
from tests.donbaton.test_lte_migration import migration
from tests.donbaton.test_lte_quota import LTE, TABLES as BASIC_TABLES, WIFI, sample, seed, tariff
from tests.fixtures.sqlite_memory import memory_session


TABLES = [*BASIC_TABLES, TrafficPurchase.__table__, LteOrphanQuota.__table__]
ENV_EXAMPLE = Path(__file__).resolve().parents[2] / '.env.example'
NOTICE_KEYS = (
    'DONBATON_LTE_NOTICE_ENABLED',
    'DONBATON_LTE_LOW_NOTICE_GB',
    'DONBATON_LTE_EXHAUSTED_NOTICE_ENABLED',
    'DONBATON_LTE_NOTICE_TRIALS',
    'DONBATON_LTE_NOTICE_ADMIN_ORPHANS',
    'DONBATON_LTE_NOTICE_MAX_PER_CYCLE',
    'DONBATON_LTE_LOW_NOTICE_TEXT',
    'DONBATON_LTE_EXHAUSTED_NOTICE_TEXT',
)
T0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_EPOCH', EPOCH)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 0)
    # Решения владельца (25.09.2026) — это же дефолты кода.
    for key in NOTICE_KEYS:
        monkeypatch.setattr(settings, key, Settings.model_fields[key].default)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(notify, '_overflow_alert', {'sent_at': None})


def proof(at):
    return {
        'version': 1,
        'epoch': settings.DONBATON_LTE_ACCOUNTING_EPOCH,
        'observed_at': at.isoformat(),
        'node_ids': list(TEST_LTE_NODES),
    }


def live():
    """Живая аренда канарейки, как её отдаёт цикл учёта."""
    return TelemetryEvidence(payload=proof(datetime.now(UTC)), last_valid=None)


def grace_only():
    """Аренды нет, но память о ней свежая: LTE открыт grace-окном, учёт не подтверждён."""
    now = datetime.now(UTC)
    return TelemetryEvidence(
        payload=None,
        last_valid={'payload': proof(now - timedelta(minutes=10)), 'seen_at': (now - timedelta(minutes=1)).isoformat()},
    )


def panel(lifetime, *, panel_id=11, username='client_11'):
    current = sample(lifetime, panel_id=panel_id)
    current.username = username
    return current


def api_for(current):
    return SimpleNamespace(get_user_by_id=AsyncMock(return_value=current), update_user=AsyncMock(return_value=current))


async def customer(db, *, limit_bytes=2 * GIB):
    sub, quota, plan = await seed(db)
    quota.limit_bytes = limit_bytes
    await db.commit()
    return sub, quota, plan


async def step(db, sub, quota, lifetime, notices, *, heartbeat=None):
    user = await db.get(User, sub.user_id)
    await reconcile_subscription(
        db, sub, api_for(panel(lifetime)), user=user, heartbeat=heartbeat or live(), notices=notices
    )
    await db.refresh(quota)


def events(notices):
    return [notice.event for notice in notices]


# ============ Чистое решение ============


def status(remaining, *, limit=60 * GIB, topup=0, state='ready', unlimited=False):
    return {
        'remaining_bytes': remaining,
        'limit_bytes': limit,
        'topup_bytes': topup,
        'accounting_state': state,
        'is_unlimited': unlimited,
    }


def decide(current, *, low=None, exhausted=None, eligible=True, telemetry_live=True, trial=False, audience=True):
    return decide_lte_notice(
        current,
        low_sent_at=low,
        exhausted_sent_at=exhausted,
        eligible=eligible,
        telemetry_live=telemetry_live,
        is_trial=trial,
        audience_enabled=audience,
        now=T0,
    )


@pytest.mark.parametrize(
    ('remaining', 'expected'),
    [
        (GIB + 1, (None, None, None)),
        (GIB, (T0, None, 'low')),
        (1, (T0, None, 'low')),
        # Сразу с «много» в ноль: уходит одно «закончился», порог «мало» отмечен по пути.
        (0, (T0, T0, 'exhausted')),
    ],
)
def test_threshold_is_inclusive_and_zero_is_only_exhausted(remaining, expected):
    marks = decide(status(remaining))
    assert (marks.low_sent_at, marks.exhausted_sent_at, marks.event) == expected


def test_marks_hold_until_the_balance_grows_again():
    earlier = T0 - timedelta(days=3)
    held = decide(status(GIB // 2), low=earlier)
    assert (held.low_sent_at, held.event) == (earlier, None)
    exhausted = decide(status(0), low=earlier)
    assert (exhausted.low_sent_at, exhausted.exhausted_sent_at, exhausted.event) == (earlier, T0, 'exhausted')
    assert decide(status(0), low=earlier, exhausted=earlier).event is None
    # Небольшое пополнение ниже порога заново взводит только «закончился».
    small = decide(status(GIB // 4), low=earlier, exhausted=earlier)
    assert (small.low_sent_at, small.exhausted_sent_at, small.event) == (earlier, None, None)
    # Докупка / новый период / ручная добавка выше порога снимает обе отметки.
    refilled = decide(status(20 * GIB), low=earlier, exhausted=earlier)
    assert (refilled.low_sent_at, refilled.exhausted_sent_at, refilled.event) == (None, None, None)


@pytest.mark.parametrize(
    'state',
    ['telemetry_unavailable', 'stale', 'pending', 'prepared', 'missing', 'regressed', 'identity_changed'],
)
def test_unconfirmed_accounting_never_marks_but_still_rearms(state):
    assert decide(status(GIB // 2, state=state)).event is None
    assert decide(status(0, state=state)).event is None
    earlier = T0 - timedelta(days=1)
    rearmed = decide(status(20 * GIB, state=state), low=earlier, exhausted=earlier)
    assert (rearmed.low_sent_at, rearmed.exhausted_sent_at) == (None, None)


def test_grace_or_disabled_accounting_never_marks(monkeypatch):
    assert decide(status(0), telemetry_live=False).event is None
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', False)
    assert decide(status(0)).event is None


def test_wifi_only_and_unlimited_are_never_told_anything():
    wifi_only = decide(status(0, limit=0, topup=0))
    assert (wifi_only.low_sent_at, wifi_only.exhausted_sent_at, wifi_only.event) == (None, None, None)
    earlier = T0 - timedelta(days=1)
    unlimited = decide(status(0, limit=0, unlimited=True), low=earlier, exhausted=earlier)
    assert (unlimited.low_sent_at, unlimited.exhausted_sent_at, unlimited.event) == (earlier, earlier, None)
    # Wi-Fi-тариф с живой докупкой — это настоящий LTE, о нём сообщаем.
    assert decide(status(GIB // 2, limit=0, topup=20 * GIB)).event == 'low'


def test_switches_trials_audience_and_eligibility(monkeypatch):
    assert decide(status(0), eligible=False).event is None
    assert decide(status(0), audience=False).event is None
    assert decide(status(GIB // 2), trial=True).event is None
    monkeypatch.setattr(settings, 'DONBATON_LTE_NOTICE_TRIALS', True)
    assert decide(status(GIB // 2), trial=True).event == 'low'
    monkeypatch.setattr(settings, 'DONBATON_LTE_EXHAUSTED_NOTICE_ENABLED', False)
    silent = decide(status(0))
    assert (silent.low_sent_at, silent.exhausted_sent_at, silent.event) == (None, None, None)
    monkeypatch.setattr(settings, 'DONBATON_LTE_EXHAUSTED_NOTICE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_LOW_NOTICE_GB', 0)
    assert decide(status(1)).event is None
    assert decide(status(0)).event == 'exhausted'
    monkeypatch.setattr(settings, 'DONBATON_LTE_LOW_NOTICE_GB', 2.5)
    assert decide(status(2 * GIB)).event == 'low'
    monkeypatch.setattr(settings, 'DONBATON_LTE_NOTICE_ENABLED', False)
    assert decide(status(0)).event is None


# ============ Настоящий леджер: отметки в транзакции цикла ============


@pytest.mark.asyncio
async def test_one_notice_per_crossing_and_again_only_after_a_refill(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await customer(db, limit_bytes=2 * GIB)
        notices = []
        await step(db, sub, quota, 0, notices)
        assert notices == [] and quota.low_notice_sent_at is None

        await step(db, sub, quota, GIB + GIB // 5, notices)
        assert events(notices) == ['low']
        assert notices[0].subscription_id == sub.id
        assert notices[0].remaining_bytes == 2 * GIB - (GIB + GIB // 5)
        first_mark = quota.low_notice_sent_at
        assert first_mark is not None and quota.exhausted_notice_sent_at is None

        await step(db, sub, quota, GIB + GIB // 4, notices)
        assert events(notices) == ['low'] and quota.low_notice_sent_at == first_mark

        await credit_topup(db, sub, bytes_amount=20 * GIB, operation_id='notice-refill')
        await db.commit()
        await step(db, sub, quota, GIB + GIB // 4, notices)
        assert events(notices) == ['low'] and quota.low_notice_sent_at is None

        await step(db, sub, quota, 21 * GIB + GIB // 4, notices)
        assert events(notices) == ['low', 'low']
        assert notices[-1].remaining_bytes == 22 * GIB - (21 * GIB + GIB // 4)
        assert notices[-1].topup_bytes == 20 * GIB

        await step(db, sub, quota, 23 * GIB, notices)
        assert events(notices) == ['low', 'low', 'exhausted']
        assert notices[-1].remaining_bytes == 0
        assert quota.exhausted_notice_sent_at is not None and quota.low_notice_sent_at is not None
        await step(db, sub, quota, 23 * GIB + 100, notices)
        assert events(notices) == ['low', 'low', 'exhausted']


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence', ['missing', 'grace'])
async def test_no_notice_without_a_live_lease_even_when_lte_stays_open(monkeypatch, evidence):
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await customer(db, limit_bytes=2 * GIB)
        heartbeat = TelemetryEvidence(payload=None, last_valid=None) if evidence == 'missing' else grace_only()
        notices = []
        await step(db, sub, quota, 0, notices, heartbeat=heartbeat)
        await step(db, sub, quota, 2 * GIB, notices, heartbeat=heartbeat)
        expected_state = 'telemetry_unavailable' if evidence == 'missing' else 'ready'
        assert sub._lte_quota_status['accounting_state'] == expected_state
        assert sub._lte_quota_status['exhausted']
        assert notices == []
        assert (quota.low_notice_sent_at, quota.exhausted_notice_sent_at) == (None, None)
        # Та же исчерпанная квота на живой аренде объявляется сразу.
        await step(db, sub, quota, 2 * GIB, notices)
        assert events(notices) == ['exhausted']


@pytest.mark.asyncio
async def test_regressed_counter_is_not_ready_and_sends_nothing(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await customer(db, limit_bytes=2 * GIB)
        notices = []
        await step(db, sub, quota, 0, notices)
        await step(db, sub, quota, GIB + GIB // 2, notices, heartbeat=TelemetryEvidence(payload=None, last_valid=None))
        await step(db, sub, quota, GIB, notices)
        assert quota.accounting_state == 'regressed'
        assert notices == [] and quota.low_notice_sent_at is None
        await step(db, sub, quota, GIB + GIB // 2, notices)
        assert quota.accounting_state == 'ready'
        assert events(notices) == ['low']


@pytest.mark.asyncio
async def test_reconcile_without_a_collector_leaves_marks_alone(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await customer(db, limit_bytes=GIB // 2)
        user = await db.get(User, sub.user_id)
        await reconcile_subscription(db, sub, api_for(panel(0)), user=user, heartbeat=live())
        await db.refresh(quota)
        assert (quota.low_notice_sent_at, quota.exhausted_notice_sent_at) == (None, None)


@pytest.mark.asyncio
async def test_blocked_user_or_expired_subscription_is_not_marked(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await customer(db, limit_bytes=GIB // 2)
        user = await db.get(User, sub.user_id)
        user.status = 'blocked'
        await db.commit()
        notices = []
        await step(db, sub, quota, 0, notices)
        assert notices == [] and quota.low_notice_sent_at is None
        user.status = 'active'
        sub.end_date = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()
        await step(db, sub, quota, 0, notices)
        assert notices == [] and quota.low_notice_sent_at is None


@pytest.mark.asyncio
async def test_wifi_only_subscription_is_always_exhausted_and_never_notified(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        wifi = tariff(
            name='Только Wi-Fi',
            network_category='wifi',
            device_limit=3,
            lte_traffic_limit_gb=None,
            lte_period_days=None,
            allowed_squads=[WIFI],
            traffic_topup_enabled=False,
            traffic_topup_packages={},
        )
        user = User(id=1, telegram_id=123, first_name='Wifi', status='active', remnawave_id=11)
        db.add_all([user, wifi])
        await db.flush()
        sub = Subscription(
            id=1,
            user_id=1,
            status='active',
            is_trial=False,
            tariff_id=wifi.id,
            end_date=datetime.now(UTC) + timedelta(days=30),
            connected_squads=[WIFI],
            remnawave_id=11,
            remnawave_short_id='wifi001',
        )
        db.add(sub)
        await db.flush()
        quota = await initialize_quota(db, sub, wifi)
        await db.commit()
        notices = []
        for lifetime in (0, GIB, 5 * GIB):
            await step(db, sub, quota, lifetime, notices)
            assert sub._lte_quota_status['exhausted'] and sub._lte_quota_status['accounting_state'] == 'ready'
        assert notices == []
        assert (quota.low_notice_sent_at, quota.exhausted_notice_sent_at) == (None, None)


@pytest.mark.asyncio
async def test_legacy_unlimited_is_never_notified(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, base_gb=0, custom=True, strategy='NO_RESET')
        quota = await activate(db, sub, target, snapshot)
        notices = []
        await step(db, sub, quota, BASELINE + 50 * GIB, notices)
        assert sub._lte_quota_status['is_unlimited']
        assert notices == []
        assert (quota.low_notice_sent_at, quota.exhausted_notice_sent_at) == (None, None)


@pytest.mark.asyncio
async def test_legacy_pool_is_counted_in_real_bytes_divided_by_17(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        # 34 «старых» ГиБ = 2 ГиБ настоящих; 170 старых единиц расхода = 10 байт.
        sub, target, snapshot, _ = await legacy_seed(db, base_gb=34, used_bytes=170)
        quota = await activate(db, sub, target, snapshot)
        notices = []
        await step(db, sub, quota, BASELINE, notices)
        assert sub._lte_quota_status['remaining_bytes'] == 2 * GIB - 10
        assert notices == []
        await step(db, sub, quota, BASELINE + GIB + GIB // 2, notices)
        assert events(notices) == ['low']
        assert notices[0].remaining_bytes == 2 * GIB - 10 - (GIB + GIB // 2)
        await step(db, sub, quota, BASELINE + 2 * GIB, notices)
        assert events(notices) == ['low', 'exhausted']


@pytest.mark.asyncio
@pytest.mark.parametrize('trials_enabled', [False, True])
async def test_trial_is_notified_only_when_the_owner_enables_it(monkeypatch, trials_enabled):
    monkeypatch.setattr(settings, 'DONBATON_LTE_NOTICE_TRIALS', trials_enabled)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, _ = await legacy_seed(db, is_trial=True, paid_until=datetime.now(UTC) + timedelta(days=3))
        quota = await initialize_trial_quota(db, sub, legacy_traffic_limit_gb=10)
        sub.status = 'trial'
        await db.commit()
        notices = []
        # Пробный пакет (~0,59 ГиБ) ниже порога уже на первом замере.
        await step(db, sub, quota, BASELINE, notices)
        assert sub._lte_quota_status['remaining_bytes'] < GIB
        assert events(notices) == (['low'] if trials_enabled else [])
        assert (quota.low_notice_sent_at is not None) is trials_enabled


@pytest.mark.asyncio
async def test_orphan_goes_to_the_admin_chat_only_and_only_when_enabled(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, source, _ = await legacy_seed(db)
        source['subscription'] = None
        source['panel']['id'] = 77
        quota = await import_orphan_quota(db, snapshot=source, **IMPORT)
        await activate_orphan_quota(
            db,
            quota,
            migration_id=IMPORT['migration_id'],
            counter_epoch=EPOCH,
            panel_id=77,
            lifetime_bytes=BASELINE,
        )
        await db.commit()
        api = api_for(panel(BASELINE + GIB // 2, panel_id=77, username='panel_only_77'))
        notices = []
        # Решение владельца: по умолчанию сирот не трогаем вовсе — ни отметок, ни сообщений.
        await reconcile_orphan_quota(db, quota, api, heartbeat=live(), notices=notices)
        assert notices == [] and quota.low_notice_sent_at is None

        monkeypatch.setattr(settings, 'DONBATON_LTE_NOTICE_ADMIN_ORPHANS', True)
        await reconcile_orphan_quota(db, quota, api, heartbeat=live(), notices=notices)
        assert [(n.event, n.panel_id, n.panel_username, n.subscription_id) for n in notices] == [
            ('low', 77, 'panel_only_77', None)
        ]
        assert quota.low_notice_sent_at is not None
        await reconcile_orphan_quota(db, quota, api, heartbeat=live(), notices=notices)
        assert len(notices) == 1

        admin = AsyncMock(return_value=True)
        customer_send = AsyncMock(return_value=True)
        monkeypatch.setattr(AdminNotificationService, 'send_admin_notification', admin)
        monkeypatch.setattr(notification_delivery_service, 'send_notification', customer_send)
        assert await notify.dispatch(SimpleNamespace(), notices) == {'sent': 1}
        customer_send.assert_not_awaited()
        text = admin.call_args.args[0]
        assert 'LTE почти закончился' in text
        assert '<code>77</code>' in text and 'panel_only_77' in text
        assert f'{notices[0].remaining_bytes / GIB:.2f} ГиБ' in text


# ============ Цикл, предохранитель и отправка ============


async def add_customer(db, plan, index, *, limit_bytes):
    db.add(User(id=index, telegram_id=1000 + index, first_name=f'C{index}', status='active', remnawave_id=100 + index))
    await db.flush()
    sub = Subscription(
        id=index,
        user_id=index,
        status='active',
        is_trial=False,
        tariff_id=plan.id,
        end_date=datetime.now(UTC) + timedelta(days=90),
        connected_squads=[WIFI, LTE],
        remnawave_id=100 + index,
        remnawave_short_id=f'cyc{index:03d}',
    )
    db.add(sub)
    await db.flush()
    quota = await initialize_quota(db, sub, plan)
    quota.limit_bytes = limit_bytes
    return sub


@asynccontextmanager
async def cycle_lab(monkeypatch, db):
    from app.database import database

    maker = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(database, 'AsyncSessionLocal', maker)
    monkeypatch.setattr(notify, 'AsyncSessionLocal', maker)

    def panel_user(panel_id):
        return panel(0, panel_id=panel_id, username=f'client_{panel_id}')

    api = SimpleNamespace(get_user_by_id=AsyncMock(side_effect=panel_user), update_user=AsyncMock())

    @asynccontextmanager
    async def client():
        yield api

    yield SimpleNamespace(is_configured=True, get_api_client=client)


@pytest.mark.asyncio
async def test_first_pass_over_the_limit_marks_everyone_and_sends_nothing(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_NOTICE_MAX_PER_CYCLE', 2)
    async with memory_session(monkeypatch, TABLES) as db:
        plan = tariff()
        db.add(plan)
        await db.flush()
        for index in (1, 2, 3):
            await add_customer(db, plan, index, limit_bytes=GIB // 2)
        await db.commit()
        admin = AsyncMock(return_value=True)
        customer_send = AsyncMock(return_value=True)
        monkeypatch.setattr(AdminNotificationService, 'send_admin_notification', admin)
        monkeypatch.setattr(notification_delivery_service, 'send_notification', customer_send)
        async with cycle_lab(monkeypatch, db) as service:
            candidates = await run_quota_cycle(service)
            assert sorted(candidate.subscription_id for candidate in candidates) == [1, 2, 3]
            assert await notify.dispatch(SimpleNamespace(), candidates) == {'skipped': 3}
            customer_send.assert_not_awaited()
            assert admin.await_count == 1
            assert '3 кандидатов' in admin.call_args.args[0] and 'лимита 2' in admin.call_args.args[0]
            # Отметки остаются: следующий проход не повторяет ни рассылку, ни тревогу.
            marks = (await db.execute(select(LteQuota.low_notice_sent_at))).scalars().all()
            assert len(marks) == 3 and all(mark is not None for mark in marks)
            assert await run_quota_cycle(service) == []


@pytest.mark.asyncio
async def test_overflow_alert_is_sent_at_most_hourly(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_NOTICE_MAX_PER_CYCLE', 1)
    admin = AsyncMock(return_value=True)
    customer_send = AsyncMock(return_value=True)
    monkeypatch.setattr(AdminNotificationService, 'send_admin_notification', admin)
    monkeypatch.setattr(notification_delivery_service, 'send_notification', customer_send)
    batch = [LteNoticeCandidate(event='low', remaining_bytes=1, subscription_id=index) for index in (1, 2)]
    assert await notify.dispatch(SimpleNamespace(), batch) == {'skipped': 2}
    assert await notify.dispatch(SimpleNamespace(), batch) == {'skipped': 2}
    assert admin.await_count == 1
    notify._overflow_alert['sent_at'] = time.monotonic() - 3601
    await notify.dispatch(SimpleNamespace(), batch)
    assert admin.await_count == 2
    customer_send.assert_not_awaited()


@asynccontextmanager
async def delivery_lab(monkeypatch, **customer_kwargs):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await customer(db, **customer_kwargs)
        maker = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(notify, 'AsyncSessionLocal', maker)
        send = AsyncMock(return_value=True)
        monkeypatch.setattr(notification_delivery_service, 'send_notification', send)
        yield SimpleNamespace(db=db, sub=sub, send=send, bot=SimpleNamespace())


def buttons(markup):
    return [[(button.text, button.callback_data) for button in row] for row in markup.inline_keyboard]


@pytest.mark.asyncio
async def test_customer_message_type_text_and_buttons(monkeypatch):
    async with delivery_lab(monkeypatch) as lab:
        candidate = LteNoticeCandidate(event='low', remaining_bytes=GIB // 2, subscription_id=lab.sub.id)
        assert await notify.dispatch(lab.bot, [candidate]) == {'sent': 1}
        kwargs = lab.send.call_args.kwargs
        assert kwargs['notification_type'] is NotificationType.LTE_QUOTA_LOW
        assert kwargs['bot'] is lab.bot
        end_date = format_local_datetime(lab.sub.end_date, '%d.%m.%Y %H:%M %Z')
        assert kwargs['telegram_message'] == (
            '📶 <b>LTE почти закончился</b>\n\nПо подписке «Wi-Fi + LTE» осталось 0.50 ГиБ LTE. '
            'Когда пакет закончится, вкладка LTE пропадёт, а Wi-Fi продолжит работать до '
            f'{end_date}.\n\nДокупить LTE можно кнопкой ниже.'
        )
        assert buttons(kwargs['telegram_markup']) == [
            [('📶 Докупить LTE', 'buy_traffic')],
            [('📱 Моя подписка', 'menu_subscription')],
        ]
        assert kwargs['context']['remaining_gib'] == '0.50' and kwargs['context']['topup_available'] is True

        exhausted = LteNoticeCandidate(event='exhausted', remaining_bytes=0, subscription_id=lab.sub.id)
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
        await notify.dispatch(lab.bot, [exhausted])
        kwargs = lab.send.call_args.kwargs
        assert kwargs['notification_type'] is NotificationType.LTE_QUOTA_EXHAUSTED
        assert kwargs['telegram_message'].startswith('📵 <b>LTE закончился</b>\n\nПакет LTE по подписке «Wi-Fi + LTE»')
        assert buttons(kwargs['telegram_markup']) == [
            [('📶 Докупить LTE', f'st:{lab.sub.id}')],
            [('📱 Моя подписка', f'sm:{lab.sub.id}')],
        ]


@pytest.mark.asyncio
async def test_without_a_topup_there_is_no_topup_line_or_button(monkeypatch):
    async with delivery_lab(monkeypatch) as lab:
        lab.sub.is_trial = True
        await lab.db.commit()
        await notify.dispatch(lab.bot, [LteNoticeCandidate(event='exhausted', remaining_bytes=0, subscription_id=1)])
        kwargs = lab.send.call_args.kwargs
        assert 'Докупить' not in kwargs['telegram_message']
        assert buttons(kwargs['telegram_markup']) == [[('📱 Моя подписка', 'menu_subscription')]]


def test_topup_availability_follows_the_purchase_rules():
    now = datetime.now(UTC)
    plan = tariff(device_limit=3, max_topup_traffic_gb=0)
    user = SimpleNamespace(restriction_subscription=False)

    def sub(**overrides):
        values = dict(network_category='wifi_lte', is_trial=False, status='active', end_date=now + timedelta(days=5))
        values.update(overrides)
        return SimpleNamespace(**values)

    assert notify.topup_available(sub(), plan, user)
    assert not notify.topup_available(sub(is_trial=True), plan, user)
    assert not notify.topup_available(sub(network_category='wifi'), plan, user)
    assert not notify.topup_available(sub(status='expired'), plan, user)
    assert not notify.topup_available(sub(end_date=now - timedelta(seconds=1)), plan, user)
    assert not notify.topup_available(sub(), plan, SimpleNamespace(restriction_subscription=True))
    assert not notify.topup_available(sub(), None, user)
    assert not notify.topup_available(sub(), tariff(traffic_topup_enabled=False), user)
    assert not notify.topup_available(sub(), tariff(traffic_topup_packages={}), user)
    capped = tariff(max_topup_traffic_gb=25, traffic_topup_packages={'20': 7900})
    assert notify.topup_available(sub(), capped, user, topup_bytes=5 * GIB)
    assert not notify.topup_available(sub(), capped, user, topup_bytes=6 * GIB)


def test_owner_text_from_settings_wins_and_a_broken_placeholder_falls_back(monkeypatch):
    from app.localization.texts import get_texts

    texts = get_texts('ru')
    values = dict(tariff='Семейный', remaining='0.40', end_date='01.10.2026 12:00 MSK')
    monkeypatch.setattr(settings, 'DONBATON_LTE_LOW_NOTICE_TEXT', '<b>Мало LTE</b>\n{remaining} ГиБ, «{tariff}»')
    assert notify.render_notice_text('low', texts, topup=True, **values) == (
        '<b>Мало LTE</b>\n0.40 ГиБ, «Семейный»\n\nДокупить LTE можно кнопкой ниже.'
    )
    monkeypatch.setattr(settings, 'DONBATON_LTE_LOW_NOTICE_TEXT', 'Осталось {gb} ГиБ')
    with capture_logs() as logs:
        text = notify.render_notice_text('low', texts, topup=False, **values)
    assert text.startswith('📶 <b>LTE почти закончился</b>') and '0.40 ГиБ' in text
    assert any(log['log_level'] == 'warning' for log in logs)
    # Английский клиент без своего текста владельца получает перевод.
    monkeypatch.setattr(settings, 'DONBATON_LTE_LOW_NOTICE_TEXT', '')
    english = notify.render_notice_text('low', get_texts('en'), topup=True, **values)
    assert english.startswith('📶 <b>LTE is almost used up</b>') and english.endswith('button below.')


@pytest.mark.asyncio
async def test_a_failed_or_missing_delivery_never_escapes_dispatch(monkeypatch):
    async with delivery_lab(monkeypatch) as lab:
        lab.send.side_effect = [RuntimeError('telegram is down'), True]
        batch = [
            LteNoticeCandidate(event='low', remaining_bytes=GIB // 2, subscription_id=lab.sub.id),
            LteNoticeCandidate(event='exhausted', remaining_bytes=0, subscription_id=lab.sub.id),
            LteNoticeCandidate(event='low', remaining_bytes=GIB // 2, subscription_id=999),
        ]
        assert await notify.dispatch(lab.bot, batch) == {'failed': 1, 'sent': 1, 'missing': 1}
        assert await notify.dispatch(None, batch) == {'skipped': 3}
        assert await notify.dispatch(lab.bot, []) == {}


@pytest.mark.asyncio
async def test_lte_loop_dispatches_in_background_and_keeps_polling(monkeypatch):
    from app.services.monitoring_service import MonitoringService

    service = MonitoringService.__new__(MonitoringService)
    service.bot = SimpleNamespace()
    service.subscription_service = SimpleNamespace()
    service._lte_notice_tasks = set()
    service.is_running = True
    candidate = LteNoticeCandidate(event='low', remaining_bytes=GIB // 2, subscription_id=1)
    passes = []

    async def fake_cycle(subscription_service):
        passes.append(subscription_service)
        if len(passes) == 2:
            service.is_running = False
            return []
        return [candidate]

    outcomes = []
    real_dispatch = notify.dispatch

    async def recording_dispatch(bot, candidates):
        outcomes.append(await real_dispatch(bot, candidates))
        return outcomes[-1]

    monkeypatch.setattr(lte_quota_service, 'run_quota_cycle', fake_cycle)
    monkeypatch.setattr(notify, 'dispatch', recording_dispatch)
    monkeypatch.setattr(notify, '_notify_customer', AsyncMock(side_effect=RuntimeError('telegram is down')))
    monkeypatch.setattr(settings, 'DONBATON_LTE_POLL_SECONDS', 0)
    await service._lte_loop()
    await asyncio.gather(*list(service._lte_notice_tasks))
    assert len(passes) == 2
    assert outcomes == [{'failed': 1}]


# ============ Миграция и пример окружения ============


def _columns(conn, table):
    return {column['name'] for column in sa.inspect(conn).get_columns(table)}


def test_0127_adds_empty_marks_idempotently_and_downgrade_keeps_rows():
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE subscriptions(id INTEGER PRIMARY KEY)'))
        conn.execute(sa.text('INSERT INTO subscriptions VALUES (1)'))
        with Operations.context(MigrationContext.configure(conn)):
            migration().upgrade()
            migration('0120_legacy_lte_compatibility.py').upgrade()
        conn.execute(
            sa.text(
                'INSERT INTO lte_quotas(subscription_id,limit_bytes,used_bytes,base_used_bytes,period_days,'
                'period_starts_at,period_ends_at,accounting_state) VALUES (1,64424509440,123,123,30,'
                "'2026-09-09','2026-10-09','ready')"
            )
        )
        conn.execute(
            sa.text(
                'INSERT INTO lte_orphan_quotas(panel_id,end_date,original_status,period_starts_at,period_ends_at,'
                "legacy_state) VALUES (77,'2026-12-01','ACTIVE','2026-09-09','2026-10-09','{}')"
            )
        )
        quota_row = 'SELECT subscription_id,limit_bytes,used_bytes,accounting_state FROM lte_quotas'
        orphan_row = 'SELECT panel_id,end_date,original_status FROM lte_orphan_quotas'
        before = (conn.execute(sa.text(quota_row)).all(), conn.execute(sa.text(orphan_row)).all())
        with Operations.context(MigrationContext.configure(conn)):
            migration('0127_lte_quota_notices.py').upgrade()
            migration('0127_lte_quota_notices.py').upgrade()
        marks = {'low_notice_sent_at', 'exhausted_notice_sent_at'}
        for table in ('lte_quotas', 'lte_orphan_quotas'):
            assert marks <= _columns(conn, table)
            stored = conn.execute(sa.text(f'SELECT low_notice_sent_at, exhausted_notice_sent_at FROM {table}'))
            assert stored.one() == (None, None)
        assert (conn.execute(sa.text(quota_row)).all(), conn.execute(sa.text(orphan_row)).all()) == before
        with Operations.context(MigrationContext.configure(conn)):
            migration('0127_lte_quota_notices.py').downgrade()
            migration('0127_lte_quota_notices.py').downgrade()
        for table in ('lte_quotas', 'lte_orphan_quotas'):
            assert not marks & _columns(conn, table)
        assert (conn.execute(sa.text(quota_row)).all(), conn.execute(sa.text(orphan_row)).all()) == before
    engine.dispose()


def test_0127_is_a_no_op_without_lte_tables():
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE subscriptions(id INTEGER PRIMARY KEY)'))
        with Operations.context(MigrationContext.configure(conn)):
            migration('0127_lte_quota_notices.py').upgrade()
            migration('0127_lte_quota_notices.py').downgrade()
        assert set(sa.inspect(conn).get_table_names()) == {'subscriptions'}
    engine.dispose()


def test_env_example_keeps_notice_keys_commented_with_the_code_defaults():
    """Ключ в .env перекрывает админку молча: правка сохраняется, но не действует."""
    lines = [line.strip() for line in ENV_EXAMPLE.read_text(encoding='utf-8').splitlines()]
    active = [line for line in lines if re.match(r'^DONBATON_LTE_[A-Z_]*NOTICE[A-Z_]*=', line)]
    assert active == []
    documented = {}
    for line in lines:
        match = re.match(r'^#\s*(DONBATON_LTE_[A-Z_]*NOTICE[A-Z_]*)=(.*)$', line)
        if match:
            documented[match.group(1)] = match.group(2).strip()
    assert set(documented) == set(NOTICE_KEYS)
    for key, shown in documented.items():
        default = Settings.model_fields[key].default
        expected = {True: 'true', False: 'false'}[default] if isinstance(default, bool) else str(default)
        assert shown == expected, key
