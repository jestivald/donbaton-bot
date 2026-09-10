"""Шов между политикой Wi-Fi/LTE и общим писателем панели (4.8.0).

До 4.8.0 политику накладывали семь вызовов ``apply_panel_policy`` внутри двух
копий кода «отправить подписку в панель». Апстрим схлопнул копии в
``panel_sync.push_subscription``, и политика переехала в одну точку —
``panel_sync.managed``. Здесь проверяется, что при переезде не потерялось ровно
то, ради чего она существует: лимит панели, набор сквадов по остатку квоты и
литеральный отзыв доступа.

Сам леджер проверяется в ``test_lte_quota.py`` — он не менялся.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import (
    LteQuota,
    LteTopup,
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
    tariff_promo_groups,
)
from app.external.remnawave_api import TrafficLimitStrategy, UserStatus
from app.services.lte_quota_service import LtePolicyError, apply_panel_policy, initialize_quota
from app.services.panel_sync import WEBHOOK, apply_managed_policy, build_panel_payload, reproject_after_create
from app.services.panel_sync.projection import PanelSnapshot, project_onto_subscription
from tests.fixtures.sqlite_memory import memory_session


WIFI = '00000000-0000-4000-8000-000000000001'
LTE = '00000000-0000-4000-8000-000000000002'
NOW = datetime.now(UTC).replace(microsecond=0)
TABLES = [
    User.__table__,
    PromoGroup.__table__,
    Tariff.__table__,
    tariff_promo_groups,
    Subscription.__table__,
    LteQuota.__table__,
    LteTopup.__table__,
]


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)


def tariff(**overrides):
    values = dict(
        name='Wi-Fi + LTE',
        network_category='wifi_lte',
        traffic_limit_gb=0,
        lte_traffic_limit_gb=60,
        lte_period_days=30,
        allowed_squads=[WIFI, LTE],
        period_prices={'30': 29900},
        is_daily=False,
        custom_traffic_enabled=False,
        is_trial_available=False,
        show_in_gift=False,
        custom_days_enabled=False,
        traffic_topup_enabled=True,
        traffic_topup_packages={'10': 5900},
    )
    values.update(overrides)
    return Tariff(**values)


async def seed(db, **sub_overrides):
    plan = tariff()
    user = User(id=1, telegram_id=123, first_name='Test', status='active', remnawave_id=11)
    db.add_all([user, plan])
    await db.flush()
    values = dict(
        id=1,
        user_id=1,
        status='active',
        is_trial=False,
        tariff_id=plan.id,
        end_date=NOW + timedelta(days=90),
        # Тарифный набор намеренно шире выдаваемого: политика обязана считать
        # сквады сама, а не переписывать сюда что придёт.
        connected_squads=[WIFI, LTE],
        remnawave_id=11,
        remnawave_short_id='lte001',
    )
    values.update(sub_overrides)
    sub = Subscription(**values)
    db.add(sub)
    await db.flush()
    quota = await initialize_quota(db, sub, plan, now=NOW)
    quota.limit_bytes = 100  # крошечный леджер, чтобы исчерпание наступало быстро
    await db.commit()
    sub.tariff = plan
    return sub, quota, plan


def sample(lifetime, *, panel_id=11):
    return SimpleNamespace(
        id=panel_id,
        status=UserStatus.ACTIVE,
        user_traffic=SimpleNamespace(lifetime_used_traffic_bytes=lifetime, used_traffic_bytes=0),
    )


async def payload_for(db, sub):
    user = await db.get(User, sub.user_id)
    return build_panel_payload(user, sub, multi_tariff=False, now=NOW)


@pytest.mark.asyncio
async def test_managed_payload_carries_quota_squads_and_never_a_real_limit(monkeypatch):
    """Три состояния квоты дают ровно те же поля, что и до 4.8.0."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        base = await payload_for(db, sub)

        # Пробы ещё не было: базовой точки нет, LTE не выдаётся.
        pending = await apply_managed_policy(db, sub, base, now=NOW)
        assert pending.active_internal_squads == (WIFI,)

        # Первая проба ставит базовую точку — появляется LTE.
        ready = await apply_managed_policy(db, sub, base, panel_user=sample(1000), now=NOW)
        assert ready.active_internal_squads == (WIFI, LTE)

        # Расход добит до лимита: LTE снимается, Wi-Fi остаётся.
        exhausted = await apply_managed_policy(db, sub, base, panel_user=sample(1100), now=NOW + timedelta(seconds=30))
        assert exhausted.active_internal_squads == (WIFI,)
        assert exhausted.status == UserStatus.ACTIVE

        for projected in (pending, ready, exhausted):
            # Лимит панели у управляемой подписки ВСЕГДА снят: считает леджер.
            assert projected.traffic_limit_bytes == 0
            assert projected.traffic_limit_strategy == TrafficLimitStrategy.NO_RESET
            assert projected.managed is True

        # Купленные права в базе политика не переписывает.
        assert sub.connected_squads == [WIFI, LTE]


@pytest.mark.asyncio
async def test_revoked_access_reaches_the_panel_as_a_literal_empty_list(monkeypatch):
    """Пустой список сквадов — это отзыв доступа, и он обязан уехать в PATCH.

    ``PanelPayload.update_kwargs`` обычно выбрасывает пустой список: в базе
    пустота значит «дефолт колонки», а не «снять инбаунды». Для политики
    Wi-Fi/LTE всё наоборот, и уронить её пустоту значит оставить доступ
    включённым у истёкшей подписки.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db, status='expired', end_date=NOW - timedelta(days=1))
        projected = await apply_managed_policy(db, sub, await payload_for(db, sub), now=NOW)

        assert projected.active_internal_squads == ()
        kwargs = projected.update_kwargs(user_id=11, now=NOW)
        assert kwargs['active_internal_squads'] == []


@pytest.mark.asyncio
async def test_narrow_patch_cannot_drop_the_policy_fields(monkeypatch):
    """``sync_squads=False`` и узкие наборы полей политику не отменяют.

    До 4.8.0 политика накладывалась ПОСЛЕ фильтра ``only_fields`` и возвращала
    свои поля обратно — отсюда обещание «override every managed payload,
    including sync_squads=False callers».
    """
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        projected = await apply_managed_policy(db, sub, await payload_for(db, sub), panel_user=sample(1000), now=NOW)

        kwargs = projected.update_kwargs(user_id=11, now=NOW, only_fields={'description'})
        assert kwargs['active_internal_squads'] == [WIFI, LTE]
        assert kwargs['traffic_limit_bytes'] == 0
        assert kwargs['traffic_limit_strategy'] == TrafficLimitStrategy.NO_RESET
        # Узкая правка всё же осталась узкой: даты в ней нет.
        assert 'expire_at' not in kwargs


@pytest.mark.asyncio
async def test_second_pass_does_not_resample_an_already_projected_payload(monkeypatch):
    """Готовый набор полей повторно через леджер не гоняется."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await seed(db)
        projected = await apply_managed_policy(db, sub, await payload_for(db, sub), panel_user=sample(1000), now=NOW)
        again = await apply_managed_policy(db, sub, projected, panel_user=sample(9999), now=NOW)

        assert again is projected
        assert quota.counter_bytes == 1000  # вторая проба счётчик не двигала


@pytest.mark.asyncio
async def test_new_account_gets_its_lte_right_after_the_first_sample(monkeypatch):
    """У нового аккаунта проба берётся из ответа на create — и досылается LTE.

    Без этого только что оплаченный Wi-Fi + LTE оставался бы без LTE до
    следующего прохода воркера квот.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        created = await apply_managed_policy(db, sub, await payload_for(db, sub), now=NOW)
        assert created.active_internal_squads == (WIFI,)

        update = AsyncMock(return_value=sample(0))
        await reproject_after_create(db, sub, created, sample(0), update=update, now=NOW)

        sent = update.await_args.kwargs
        assert sent['user_id'] == 11
        assert sent['active_internal_squads'] == [WIFI, LTE]
        # Досылается ТОЛЬКО политика: ни даты, ни описания второй запрос не несёт.
        assert 'expire_at' not in sent
        assert 'description' not in sent


@pytest.mark.asyncio
async def test_plain_subscription_passes_through_untouched():
    """Обычная подписка проходит шов насквозь — тем же объектом, без базы."""
    sub = SimpleNamespace(network_category=None, tariff=None)
    payload = SimpleNamespace(managed=False)

    assert await apply_managed_policy(None, sub, payload, now=NOW) is payload


@pytest.mark.asyncio
async def test_managed_tariff_without_a_quota_still_refuses_to_reach_the_panel(monkeypatch):
    """Тариф категории Wi-Fi/LTE без инициализированной квоты — не продаём."""
    sub = SimpleNamespace(network_category=None, tariff=tariff())
    payload = SimpleNamespace(managed=False)

    with pytest.raises(LtePolicyError, match='инициализированной'):
        await apply_managed_policy(None, sub, payload, now=NOW)


def managed_subscription(**overrides):
    values = dict(
        id=1,
        network_category='wifi_lte',
        status=SubscriptionStatus.ACTIVE.value,
        end_date=NOW + timedelta(days=30),
        traffic_used_gb=0.0,
        traffic_limit_gb=0,
        device_limit=3,
        connected_squads=[WIFI, LTE],
        remnawave_short_uuid='short',
        subscription_url=None,
        subscription_crypto_link=None,
        updated_at=None,
        last_webhook_update_at=None,
        grace_candidate_reason=None,
        grace_candidate_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_panel_cannot_rewrite_the_entitlement_of_a_managed_subscription():
    """Обратное направление: панель для Wi-Fi/LTE — исполнитель, не источник прав.

    Сквады и лимит в панели у такой подписки — это проекция остатка квоты,
    которую бот сам туда и записал. Прочитать их назад значит подменить
    купленные права расходом: у исчерпавшего LTE «пропал» бы сервер.
    """
    sub = managed_subscription()
    snapshot = PanelSnapshot(
        status='ACTIVE',
        expire_at=NOW + timedelta(days=999),
        traffic_limit_gb=500,
        device_limit=99,
        squads=(WIFI,),
        traffic_used_gb=7.5,
    )

    changed = project_onto_subscription(sub, snapshot, policy=WEBHOOK, now=NOW)

    assert sub.connected_squads == [WIFI, LTE]
    assert sub.traffic_limit_gb == 0
    assert sub.device_limit == 3
    assert sub.end_date == NOW + timedelta(days=30)
    # Расход — не право, его переносим как обычно.
    assert sub.traffic_used_gb == 7.5
    assert changed == {'traffic_used_gb'}


def test_panel_disable_still_reaches_a_managed_subscription():
    """Админ погасил аккаунт руками в панели — это донести обязаны."""
    sub = managed_subscription()

    project_onto_subscription(sub, PanelSnapshot(status='DISABLED'), policy=WEBHOOK, now=NOW)

    assert sub.status == SubscriptionStatus.DISABLED.value


def test_plain_subscription_still_takes_squads_from_the_panel():
    """Сторож сужает политику только для Wi-Fi/LTE — остальные не задеты."""
    sub = managed_subscription(network_category=None, connected_squads=['old'])

    project_onto_subscription(sub, PanelSnapshot(status='ACTIVE', squads=(WIFI,)), policy=WEBHOOK, now=NOW)

    assert sub.connected_squads == [WIFI]


@pytest.mark.asyncio
async def test_policy_writer_refuses_anything_beyond_the_policy():
    """Узкий вход в писателя не должен стать лазейкой для произвольных полей."""
    from app.services.panel_sync import patch_panel_policy

    api = SimpleNamespace(update_user=AsyncMock(return_value=sample(0)))

    with pytest.raises(ValueError, match='patch_panel_policy'):
        await patch_panel_policy(api, user_id=11, fields={'expire_at': NOW})
    api.update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_ledger_reconciliation_writes_through_the_panel_writer(monkeypatch):
    """Цикл квот пишет в панель тем же писателем, что и все остальные."""
    from app.services.lte_quota_service import reconcile_subscription

    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        api = SimpleNamespace(
            get_user_by_id=AsyncMock(return_value=sample(1000)),
            update_user=AsyncMock(return_value=sample(1000)),
        )

        await reconcile_subscription(db, sub, api)

        sent = api.update_user.await_args.kwargs
        assert sent['user_id'] == 11
        assert sent['traffic_limit_bytes'] == 0
        assert sent['traffic_limit_strategy'] == TrafficLimitStrategy.NO_RESET
        assert sent['active_internal_squads'] == [WIFI, LTE]


@pytest.mark.asyncio
async def test_projection_matches_the_ledger_contract_field_for_field(monkeypatch):
    """Перевод набора полей в словарь леджера и обратно ничего не теряет.

    ``apply_panel_policy`` остаётся единственным местом, где живут правила, а
    ``panel_sync.managed`` — только переводчиком. Сравниваем оба представления.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        base = await payload_for(db, sub)

        through_payload = await apply_managed_policy(db, sub, base, panel_user=sample(1000), now=NOW)
        through_dict = await apply_panel_policy(
            db,
            sub,
            {
                'status': base.status,
                'traffic_limit_bytes': base.traffic_limit_bytes,
                'traffic_limit_strategy': base.traffic_limit_strategy,
                'active_internal_squads': list(base.active_internal_squads),
            },
            panel_user=sample(1000),
            now=NOW,
        )

        assert through_payload.status == through_dict['status']
        assert through_payload.traffic_limit_bytes == through_dict['traffic_limit_bytes']
        assert through_payload.traffic_limit_strategy == through_dict['traffic_limit_strategy']
        assert list(through_payload.active_internal_squads) == through_dict['active_internal_squads']
