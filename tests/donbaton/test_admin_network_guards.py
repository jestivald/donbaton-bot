"""Админские действия не обходят раздельный учёт Wi-Fi/LTE.

Аудит 26.09.2026: «сменить тариф» в кабинете, бот-админке и массовых действиях
писал ``tariff_id``/устройства/сквады напрямую — мимо ``update_entitlement``.
Wi-Fi → Wi-Fi + LTE оставлял категорию wifi без пакета, 10 → 9 — прежние
120 ГиБ, легаси → сетевой ронял синхронизацию, а админ видел «успех». Заодно
удалялись TrafficPurchase — записи переноса. Отказы правил (``LtePolicyError``,
``ValueError``) превращались в 500 без причины, ночная синхронизация создавала
подписку «сироте», а «сделать платной» оставляло пробный пакет LTE пробным.

Здесь каждое из этих мест проверено на настоящем леджере в SQLite; там, где
отказ зависит от флага соседней ветки (досрочное продление легаси), исключение
подставляется напрямую — код обязан корректно показать любую причину.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import set_committed_value

from app.cabinet.routes import admin_bulk_actions as bulk, admin_users
from app.cabinet.schemas.bulk_actions import BulkActionParams, BulkActionType, BulkExecuteRequest, BulkUserResult
from app.cabinet.schemas.users import SyncFromPanelRequest, UpdateSubscriptionRequest
from app.config import settings
from app.database.models import (
    LteOrphanQuota,
    LteQuota,
    SentNotification,
    Subscription,
    Tariff,
    TrafficPurchase,
    User,
)
from app.handlers.admin import users as bot_admin
from app.services import donbaton_admin_guards as guards
from app.services.lte_quota_service import (
    GIB,
    LtePolicyError,
    import_orphan_quota,
    initialize_quota,
    initialize_trial_quota,
)
from app.services.remnawave_service import RemnaWaveService
from tests.donbaton.test_lte_legacy import IMPORT, TABLES as LEGACY_TABLES
from tests.donbaton.test_lte_quota import LTE, NOW, WIFI, tariff
from tests.fixtures.sqlite_memory import memory_session


TABLES = [*LEGACY_TABLES, SentNotification.__table__]
LEGACY_SQUAD = '00000000-0000-4000-8000-000000000099'


@pytest.fixture(autouse=True)
def lte_configured(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)


# --------------------------------------------------------------------------- #
# Данные
# --------------------------------------------------------------------------- #


def wifi_only(**overrides) -> Tariff:
    return tariff(
        **dict(
            dict(
                id=7,
                name='Только Wi-Fi Личный',
                network_category='wifi',
                lte_traffic_limit_gb=None,
                lte_period_days=None,
                allowed_squads=[WIFI],
                traffic_topup_enabled=False,
                traffic_topup_packages={},
                device_limit=3,
            ),
            **overrides,
        )
    )


def old_plan(tariff_id: int, **overrides) -> Tariff:
    values = dict(
        id=tariff_id,
        name=f'Old {tariff_id}',
        traffic_limit_gb=50,
        device_limit=3,
        allowed_squads=[LEGACY_SQUAD],
        period_prices={'30': 19900},
    )
    values.update(overrides)
    return Tariff(**values)


async def managed_seed(db, *, current: Tariff, is_trial: bool = False):
    """Живая подписка на сетевом тарифе с обычным учётом и одной покупкой-записью."""
    user = User(id=1, telegram_id=123, first_name='Test', status='active', remnawave_id=11)
    catalog = [
        tariff(id=9, name='Wi-Fi + LTE Личный', device_limit=3),
        wifi_only(),
        tariff(id=10, name='Wi-Fi + LTE Семейный', lte_traffic_limit_gb=120, device_limit=7),
        old_plan(4),
    ]
    unique = {plan.id: plan for plan in catalog}
    unique[current.id] = current
    db.add_all([user, *unique.values()])
    await db.flush()
    sub = Subscription(
        id=1,
        user_id=1,
        status='trial' if is_trial else 'active',
        is_trial=is_trial,
        tariff_id=current.id,
        device_limit=current.device_limit,
        end_date=NOW + timedelta(days=25),
        connected_squads=list(current.allowed_squads),
        remnawave_id=11,
        remnawave_short_id='lte001',
    )
    db.add(sub)
    await db.flush()
    if is_trial:
        quota = await initialize_trial_quota(db, sub, legacy_traffic_limit_gb=10, now=NOW)
    else:
        quota = await initialize_quota(db, sub, current, now=NOW)
    # Аудиторская запись переноса: удалять её админская смена тарифа не вправе.
    db.add(TrafficPurchase(subscription_id=sub.id, traffic_gb=5, expires_at=NOW + timedelta(days=10)))
    await db.commit()
    return user, sub, quota, unique


async def legacy_only_seed(db):
    """Старая подписка без категории: легаси-тариф 4 и сетевые 9 / 7 в каталоге."""
    user = User(id=1, telegram_id=123, first_name='Test', status='active', remnawave_id=11)
    db.add_all([user, old_plan(4), old_plan(5, traffic_limit_gb=200), tariff(id=9, device_limit=3), wifi_only()])
    await db.flush()
    sub = Subscription(
        id=1,
        user_id=1,
        status='active',
        is_trial=False,
        tariff_id=4,
        device_limit=3,
        traffic_limit_gb=55,
        purchased_traffic_gb=5,
        end_date=NOW + timedelta(days=25),
        connected_squads=[LEGACY_SQUAD],
        remnawave_id=11,
        remnawave_short_id='old001',
    )
    db.add(sub)
    await db.flush()
    db.add(TrafficPurchase(subscription_id=sub.id, traffic_gb=5, expires_at=NOW + timedelta(days=10)))
    await db.commit()
    return user, sub


async def count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


def preload_subscriptions(user, subscriptions) -> None:
    """Коллекция «уже загружена»: ленивая догрузка в async-сессии роняет MissingGreenlet."""
    if isinstance(user, User):
        set_committed_value(user, 'subscriptions', list(subscriptions))
    else:
        user.subscriptions = list(subscriptions)


def cabinet_boundary(monkeypatch, user, sub, *, sync=None):
    """Кабинетная ручка на настоящей сессии; внешние стороны — двойники."""
    preload_subscriptions(user, [sub])
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(admin_users, '_get_owned_subscription_or_404', AsyncMock(return_value=sub))
    sync = sync or AsyncMock(return_value={'action': 'updated'})
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', sync)
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))
    platega = AsyncMock()
    lava = AsyncMock()
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', platega)
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', lava)
    monkeypatch.setattr('app.database.crud.transaction.create_transaction', AsyncMock())
    return SimpleNamespace(sync=sync, platega=platega, lava=lava)


async def cabinet_call(db, **request):
    return await admin_users.update_user_subscription(
        1, UpdateSubscriptionRequest(subscription_id=1, **request), admin=SimpleNamespace(id=99), db=db
    )


# --------------------------------------------------------------------------- #
# 1. Смена тарифа: кабинет
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('current', 'target'),
    [
        pytest.param(7, 9, id='wifi-to-wifi_lte-would-leave-no-lte'),
        pytest.param(10, 9, id='family-to-personal-would-keep-120-gib'),
        pytest.param(9, 4, id='managed-to-legacy'),
    ],
)
async def test_cabinet_change_tariff_refuses_managed_subscription(monkeypatch, current, target):
    async with memory_session(monkeypatch, TABLES) as db:
        plans = {9: tariff(id=9, device_limit=3), 7: wifi_only(), 10: tariff(id=10, lte_traffic_limit_gb=120)}
        user, sub, quota, _ = await managed_seed(db, current=plans[current])
        before = (sub.tariff_id, sub.device_limit, list(sub.connected_squads), sub.network_category)
        limit_before = quota.limit_bytes
        doubles = cabinet_boundary(monkeypatch, user, sub)

        with pytest.raises(HTTPException) as refused:
            await cabinet_call(db, action='change_tariff', tariff_id=target)

        assert refused.value.status_code == 400
        assert refused.value.detail == guards.NETWORK_TARIFF_CHANGE_REFUSAL
        await db.refresh(sub)
        await db.refresh(quota)
        assert (sub.tariff_id, sub.device_limit, list(sub.connected_squads), sub.network_category) == before
        assert quota.limit_bytes == limit_before
        assert await count(db, TrafficPurchase) == 1  # записи переноса не удалены
        # Отказ не трогает привязки автоплатежа: их отмена — следствие смены тарифа.
        doubles.platega.assert_not_awaited()
        doubles.lava.assert_not_awaited()
        doubles.sync.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('target', [9, 7], ids=['to-wifi_lte', 'to-wifi'])
async def test_cabinet_change_tariff_refuses_legacy_to_network(monkeypatch, target):
    """Легаси → сетевой синхронизация роняла LtePolicyError, а админ видел успех."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, sub = await legacy_only_seed(db)
        cabinet_boundary(monkeypatch, user, sub)

        with pytest.raises(HTTPException) as refused:
            await cabinet_call(db, action='change_tariff', tariff_id=target)

        assert refused.value.status_code == 400
        assert refused.value.detail == guards.NETWORK_TARIFF_CHANGE_REFUSAL
        await db.refresh(sub)
        assert sub.tariff_id == 4 and sub.network_category is None
        assert await count(db, TrafficPurchase) == 1
        assert await count(db, LteQuota) == 0


@pytest.mark.asyncio
async def test_cabinet_change_tariff_between_legacy_plans_still_works_and_reports_sync(monkeypatch):
    """Легаси ↔ легаси — прежнее поведение; сбой панели теперь виден в ответе."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, sub = await legacy_only_seed(db)
        doubles = cabinet_boundary(monkeypatch, user, sub, sync=AsyncMock(return_value={'error': 'panel down'}))

        response = await cabinet_call(db, action='change_tariff', tariff_id=5)

        assert response.success is True
        assert response.message == 'Tariff changed to Old 5 (panel NOT updated: panel down)'
        await db.refresh(sub)
        assert sub.tariff_id == 5 and sub.traffic_limit_gb == 200
        assert await count(db, TrafficPurchase) == 0  # upstream: докупки легаси сбрасываются
        doubles.platega.assert_awaited_once()


# --------------------------------------------------------------------------- #
# 1. Смена тарифа: бот-админка
# --------------------------------------------------------------------------- #


def callback(data: str):
    message = SimpleNamespace(edit_text=AsyncMock())
    return SimpleNamespace(data=data, message=message, answer=AsyncMock())


@pytest.mark.asyncio
async def test_bot_tariff_change_refuses_on_every_step(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, sub, quota, _ = await managed_seed(db, current=tariff(id=10, lte_traffic_limit_gb=120, device_limit=7))
        monkeypatch.setattr(bot_admin, 'get_user_by_id', AsyncMock(return_value=user))
        monkeypatch.setattr(bot_admin, '_resolve_admin_subscription', AsyncMock(return_value=sub))
        update = AsyncMock()
        monkeypatch.setattr(bot_admin.SubscriptionService, 'update_remnawave_user', update)
        admin = SimpleNamespace(id=99)

        listing = callback('admin_sub_change_tariff_1')
        await inspect.unwrap(bot_admin.show_admin_tariff_change)(listing, admin, db)
        listing.answer.assert_awaited_once_with(guards.NETWORK_TARIFF_CHANGE_REFUSAL, show_alert=True)
        listing.message.edit_text.assert_not_awaited()

        chosen = callback('admin_sub_tariff_select_9_1')
        await inspect.unwrap(bot_admin.select_admin_tariff_change)(chosen, admin, db)
        chosen.answer.assert_awaited_once_with(guards.NETWORK_TARIFF_CHANGE_REFUSAL, show_alert=True)

        # Старая кнопка «Подтвердить» — последний рубеж.
        confirm = callback('admin_sub_tariff_confirm_9_1')
        await inspect.unwrap(bot_admin.confirm_admin_tariff_change)(confirm, admin, db)
        text = confirm.message.edit_text.await_args.args[0]
        assert 'Тариф не изменён' in text
        assert 'Сетевой тариф меняется только покупкой' in text
        update.assert_not_awaited()
        await db.refresh(sub)
        await db.refresh(quota)
        assert sub.tariff_id == 10 and sub.device_limit == 7
        assert quota.limit_bytes == 120 * GIB
        assert await count(db, TrafficPurchase) == 1


@pytest.mark.asyncio
async def test_bot_tariff_change_refuses_legacy_to_network_target(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, sub = await legacy_only_seed(db)
        monkeypatch.setattr(bot_admin, 'get_user_by_id', AsyncMock(return_value=user))
        monkeypatch.setattr(bot_admin, '_resolve_admin_subscription', AsyncMock(return_value=sub))

        confirm = callback('admin_sub_tariff_confirm_9_1')
        await inspect.unwrap(bot_admin.confirm_admin_tariff_change)(confirm, SimpleNamespace(id=99), db)

        assert 'Тариф не изменён' in confirm.message.edit_text.await_args.args[0]
        await db.refresh(sub)
        assert sub.tariff_id == 4 and sub.connected_squads == [LEGACY_SQUAD]
        assert await count(db, TrafficPurchase) == 1


# --------------------------------------------------------------------------- #
# 1. Смена тарифа: массовые действия
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize('dry_run', [False, True], ids=['execute', 'dry-run'])
async def test_bulk_change_tariff_skips_managed_subscription(monkeypatch, dry_run):
    async with memory_session(monkeypatch, TABLES) as db:
        user, sub, quota, plans = await managed_seed(db, current=tariff(id=9, device_limit=3))
        monkeypatch.setattr(bulk, '_sync_subscription_to_panel', AsyncMock())

        result = await bulk._do_change_tariff(
            db, user, BulkActionParams(tariff_id=10), plans[10], dry_run=dry_run, sub_override=sub
        )

        assert result.success is False
        assert result.message == bulk.NETWORK_TARIFF_SKIP_MESSAGE
        await db.refresh(sub)
        assert sub.tariff_id == 9 and sub.device_limit == 3
        assert await count(db, TrafficPurchase) == 1


@pytest.mark.asyncio
async def test_bulk_change_tariff_skips_legacy_to_network_target(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, sub = await legacy_only_seed(db)
        target = await db.get(Tariff, 9)

        result = await bulk._do_change_tariff(
            db, user, BulkActionParams(tariff_id=9), target, dry_run=False, sub_override=sub
        )

        assert result.message == bulk.NETWORK_TARIFF_SKIP_MESSAGE
        await db.refresh(sub)
        assert sub.tariff_id == 4 and await count(db, TrafficPurchase) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('by_subscription', [False, True], ids=['user-ids', 'subscription-ids'])
async def test_bulk_summary_counts_network_skips_as_skipped(monkeypatch, by_subscription):
    skipped = BulkUserResult(user_id=1, success=False, message=bulk.NETWORK_TARIFF_SKIP_MESSAGE)
    changed = BulkUserResult(user_id=2, success=True, message='Tariff changed to Old 5')
    results = AsyncMock(side_effect=[skipped, changed])
    monkeypatch.setattr(bulk, '_validate_and_prepare', AsyncMock(return_value=SimpleNamespace(id=5)))
    monkeypatch.setattr(bulk, '_execute_for_subscription' if by_subscription else '_execute_for_user', results)
    ids = {'subscription_ids': [1, 2]} if by_subscription else {'user_ids': [1, 2]}
    request = BulkExecuteRequest(action=BulkActionType.CHANGE_TARIFF, params=BulkActionParams(tariff_id=5), **ids)

    response = await bulk.bulk_execute(request, stream=False, admin=SimpleNamespace(id=99), db=AsyncMock())

    assert (response.success_count, response.error_count, response.skipped_count) == (1, 0, 1)


@pytest.mark.asyncio
async def test_bulk_add_traffic_reports_policy_reason_instead_of_internal_error(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, sub, _, _ = await managed_seed(db, current=tariff(id=9, device_limit=3))

        result = await bulk._do_add_traffic(db, user, BulkActionParams(traffic_gb=10), dry_run=False, sub_override=sub)

        assert result.success is False
        assert result.message == 'Для LTE используйте отдельную покупку пакета'
        assert result.subscriptions == []  # после отката объекты сессии не читаем


@pytest.mark.asyncio
async def test_bulk_extend_reports_policy_reason(monkeypatch):
    db = AsyncMock()
    monkeypatch.setattr(bulk, 'extend_subscription', AsyncMock(side_effect=LtePolicyError('Причина правила')))
    user = SimpleNamespace(id=1, username='u', subscriptions=[])

    result = await bulk._do_extend_subscription(
        db, user, BulkActionParams(days=30), dry_run=False, sub_override=SimpleNamespace(id=1)
    )

    assert (result.success, result.message) == (False, 'Причина правила')
    db.rollback.assert_awaited_once()


# --------------------------------------------------------------------------- #
# 2. Отказы правил в кабинете — 400 с причиной, не 500
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error',
    [LtePolicyError('Прежние индивидуальные условия действуют до окончания'), ValueError('Другая причина')],
    ids=['lte-policy', 'value-error'],
)
@pytest.mark.parametrize(
    'request_kwargs',
    [{'action': 'extend', 'days': 30}, {'action': 'shorten', 'days': 3}],
    ids=['extend', 'shorten'],
)
async def test_cabinet_extend_and_shorten_return_400_with_reason(monkeypatch, error, request_kwargs):
    user = SimpleNamespace(id=1, subscriptions=[])
    sub = SimpleNamespace(id=1, end_date=NOW + timedelta(days=10), status='active', remnawave_id=11)
    cabinet_boundary(monkeypatch, user, sub)
    monkeypatch.setattr(admin_users, 'extend_subscription', AsyncMock(side_effect=error))
    db = AsyncMock()

    with pytest.raises(HTTPException) as refused:
        await cabinet_call(db, **request_kwargs)

    assert refused.value.status_code == 400
    assert refused.value.detail == str(error)
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cabinet_add_traffic_to_network_subscription_is_400(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, sub, _, _ = await managed_seed(db, current=tariff(id=9, device_limit=3))
        cabinet_boundary(monkeypatch, user, sub)

        with pytest.raises(HTTPException) as refused:
            await cabinet_call(db, action='add_traffic', traffic_gb=10)

        assert refused.value.status_code == 400
        assert refused.value.detail == 'Для LTE используйте отдельную покупку пакета'
        assert await count(db, TrafficPurchase) == 1


@pytest.mark.asyncio
async def test_cabinet_create_policy_error_is_400(monkeypatch):
    user = SimpleNamespace(id=1, subscriptions=[])
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(
        'app.database.crud.subscription.create_paid_subscription',
        AsyncMock(side_effect=LtePolicyError('Новый учёт пока не включён')),
    )
    monkeypatch.setattr(admin_users, 'get_tariff_by_id', AsyncMock(return_value=None))
    db = AsyncMock()

    with pytest.raises(HTTPException) as refused:
        await admin_users.update_user_subscription(
            1, UpdateSubscriptionRequest(action='create', tariff_id=9), admin=SimpleNamespace(id=99), db=db
        )

    assert (refused.value.status_code, refused.value.detail) == (400, 'Новый учёт пока не включён')
    db.rollback.assert_awaited_once()


# --------------------------------------------------------------------------- #
# 2. Бот-админка показывает причину
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize('days', [30, -3], ids=['extend', 'shorten'])
async def test_bot_extend_returns_policy_reason_and_rolls_back(monkeypatch, days):
    sub = SimpleNamespace(id=1, end_date=NOW + timedelta(days=10))
    monkeypatch.setattr(bot_admin, '_resolve_admin_subscription', AsyncMock(return_value=sub))
    monkeypatch.setattr(
        'app.database.crud.subscription.extend_subscription',
        AsyncMock(side_effect=LtePolicyError('Прежние условия <до> конца')),
    )
    db = AsyncMock()

    assert await bot_admin._extend_subscription_by_days(db, 1, days, 99) == (False, 'Прежние условия <до> конца')
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_extend_hides_internal_errors(monkeypatch):
    monkeypatch.setattr(bot_admin, '_resolve_admin_subscription', AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(
        'app.database.crud.subscription.extend_subscription', AsyncMock(side_effect=RuntimeError('SQL секрет'))
    )

    assert await bot_admin._extend_subscription_by_days(AsyncMock(), 1, 30, 99) == (False, None)


@pytest.mark.asyncio
async def test_bot_extend_does_not_blame_rules_for_a_failed_panel_sync(monkeypatch):
    """Срок уже записан: ValueError синхронизации — не «отказ правил» с причиной."""
    sub = SimpleNamespace(id=1, end_date=NOW + timedelta(days=40))
    monkeypatch.setattr(bot_admin, '_resolve_admin_subscription', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.database.crud.subscription.extend_subscription', AsyncMock())
    monkeypatch.setattr(
        bot_admin.SubscriptionService, 'update_remnawave_user', AsyncMock(side_effect=ValueError('panel payload'))
    )
    db = AsyncMock()

    assert await bot_admin._extend_subscription_by_days(db, 1, 30, 99) == (False, None)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_extend_screen_shows_escaped_reason(monkeypatch):
    monkeypatch.setattr(
        bot_admin, '_extend_subscription_by_days', AsyncMock(return_value=(False, 'Прежние условия <до> конца'))
    )
    event = callback('admin_sub_extend_days_1_30')

    await inspect.unwrap(bot_admin.process_subscription_extension_days)(event, SimpleNamespace(id=99), AsyncMock())

    text = event.message.edit_text.await_args.args[0]
    assert text == '❌ Ошибка продления подписки\n\nПрежние условия &lt;до&gt; конца'


@pytest.mark.asyncio
async def test_bot_add_traffic_to_network_subscription_returns_reason(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, sub, _, _ = await managed_seed(db, current=tariff(id=9, device_limit=3))
        monkeypatch.setattr(bot_admin, '_resolve_admin_subscription', AsyncMock(return_value=sub))

        outcome = await bot_admin._add_subscription_traffic(db, 1, 10, 99)

        assert outcome == (False, 'Для LTE используйте отдельную покупку пакета')
        assert await count(db, TrafficPurchase) == 1


# --------------------------------------------------------------------------- #
# 3. Сирота LTE: ночная синхронизация и «создать, если нет»
# --------------------------------------------------------------------------- #


def orphan_snapshot():
    """Права только в панели: тот же снимок, что у теста сирот леджера."""
    return {
        'version': 1,
        'captured_at': NOW.isoformat(),
        'divisor': 17,
        'reset_timezone': 'UTC',
        'renewal_requires_expiry': False,
        'panel': {
            'id': 77,
            'status': 'ACTIVE',
            'created_at': (NOW - timedelta(days=400)).isoformat(),
            'expire_at': (NOW + timedelta(days=40)).isoformat(),
            'limit_bytes': 17 * GIB,
            'used_bytes': 0,
            'lifetime_bytes': 9_000,
            'reset_strategy': 'NO_RESET',
            'last_reset_at': None,
            'active_internal_squads': [WIFI, LTE],
        },
        'subscription': None,
        'grants': [],
    }


def panel_user(panel_id: int = 77) -> dict:
    return {
        'id': panel_id,
        'telegramId': 555,
        'shortUuid': f'short{panel_id}',
        'status': 'ACTIVE',
        'expireAt': (NOW + timedelta(days=40)).isoformat().replace('+00:00', 'Z'),
        'trafficLimitBytes': 0,
        'userTraffic': {'usedTrafficBytes': 0},
        'hwidDeviceLimit': 3,
        'activeInternalSquads': [{'uuid': WIFI}, {'uuid': LTE}],
        'subscriptionUrl': 'https://panel.example/sub',
    }


@pytest.mark.asyncio
async def test_nightly_sync_does_not_create_subscription_for_orphan(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await import_orphan_quota(db, snapshot=orphan_snapshot(), **IMPORT, now=NOW)
        user = User(id=5, telegram_id=555, first_name='Linked', status='active')
        db.add(user)
        await db.commit()
        warning = MagicMock()
        monkeypatch.setattr('app.services.remnawave_service.logger.warning', warning)

        await RemnaWaveService()._create_subscription_from_panel_data(db, user, panel_user(77))
        await db.commit()

        assert await count(db, Subscription) == 0  # ни копии LTE-сквада, ни «базовой» подписки
        assert warning.call_args.kwargs['panel_user_id'] == 77
        assert await db.get(LteOrphanQuota, 77) is not None


@pytest.mark.asyncio
async def test_nightly_sync_still_creates_subscription_without_orphan(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await import_orphan_quota(db, snapshot=orphan_snapshot(), **IMPORT, now=NOW)
        user = User(id=5, telegram_id=555, first_name='Linked', status='active')
        db.add(user)
        await db.commit()

        await RemnaWaveService()._create_subscription_from_panel_data(db, user, panel_user(78))
        await db.commit()

        created = (await db.execute(select(Subscription))).scalar_one()
        assert created.remnawave_id == 78


class _PanelApi:
    def __init__(self, found):
        self.found = found

    async def find_users_by_telegram_id(self, _telegram_id):
        return [self.found]

    async def find_users_by_email(self, _email):
        return []

    async def get_user_by_id(self, _panel_id):
        return self.found


@pytest.mark.asyncio
async def test_admin_create_if_missing_refuses_orphan_before_any_change(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await import_orphan_quota(db, snapshot=orphan_snapshot(), **IMPORT, now=NOW)
        user = User(id=5, telegram_id=555, first_name='Linked', status='active')
        db.add(user)
        await db.commit()
        preload_subscriptions(user, [])
        found = SimpleNamespace(id=77, expire_at=NOW + timedelta(days=40))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=_PanelApi(found))
        context.__aexit__ = AsyncMock(return_value=None)
        service = SimpleNamespace(is_configured=True, get_api_client=lambda: context)
        monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', lambda: service)
        monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
        monkeypatch.setattr(admin_users, 'find_foreign_panel_owner', AsyncMock(return_value=None))
        create = AsyncMock()
        monkeypatch.setattr('app.database.crud.subscription.create_paid_subscription', create)

        response = await admin_users.sync_user_from_panel(
            5, None, SyncFromPanelRequest(create_if_missing=True), admin=SimpleNamespace(id=99), db=db
        )

        assert response.success is False
        assert 'lte_orphan_quotas' in response.message
        create.assert_not_awaited()
        await db.rollback()
        assert (await db.get(User, 5)).remnawave_id is None  # связь с сиротой не записана


# --------------------------------------------------------------------------- #
# 4. «Тип → платная» у пробной сетевой подписки
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bot_refuses_trial_to_paid_for_managed_trial(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, sub, quota, _ = await managed_seed(db, current=tariff(id=9, device_limit=3), is_trial=True)
        assert quota.trial_ends_at is not None
        monkeypatch.setattr(bot_admin, '_resolve_admin_subscription', AsyncMock(return_value=sub))
        update = AsyncMock()
        monkeypatch.setattr(bot_admin.SubscriptionService, 'update_remnawave_user', update)

        outcome = await bot_admin._change_subscription_type(db, 1, 'paid', 99, subscription_id=1)

        assert outcome == (False, guards.MANAGED_TRIAL_TO_PAID_REFUSAL)
        await db.refresh(sub)
        assert sub.is_trial is True
        update.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_refuses_paid_to_trial_for_managed_subscription(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, sub, _, _ = await managed_seed(db, current=tariff(id=9, device_limit=3))
        monkeypatch.setattr(bot_admin, '_resolve_admin_subscription', AsyncMock(return_value=sub))

        outcome = await bot_admin._change_subscription_type(db, 1, 'trial', 99)

        assert outcome == (False, guards.MANAGED_PAID_TO_TRIAL_REFUSAL)
        await db.refresh(sub)
        assert sub.is_trial is False


@pytest.mark.asyncio
async def test_bot_type_change_keeps_working_for_legacy_and_uses_selected_subscription(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, sub = await legacy_only_seed(db)
        sub.is_trial = True
        await db.commit()
        resolve = AsyncMock(return_value=sub)
        monkeypatch.setattr(bot_admin, '_resolve_admin_subscription', resolve)
        monkeypatch.setattr(bot_admin, 'get_user_by_id', AsyncMock(return_value=user))
        monkeypatch.setattr(bot_admin.SubscriptionService, 'update_remnawave_user', AsyncMock())

        assert await bot_admin._change_subscription_type(db, 1, 'paid', 99, subscription_id=1) == (True, None)
        resolve.assert_awaited_once_with(db, 1, 1)
        await db.refresh(sub)
        assert sub.is_trial is False


@pytest.mark.asyncio
async def test_bot_type_change_screen_shows_reason(monkeypatch):
    monkeypatch.setattr(
        bot_admin, '_change_subscription_type', AsyncMock(return_value=(False, guards.MANAGED_TRIAL_TO_PAID_REFUSAL))
    )
    event = callback('admin_sub_type_paid_1_s1')

    await inspect.unwrap(bot_admin.change_subscription_type_confirm)(event, SimpleNamespace(id=99), AsyncMock())

    text = event.message.edit_text.await_args.args[0]
    assert text.startswith('❌ Ошибка изменения типа подписки\n\n')
    assert 'покупкой тарифа' in text


# --------------------------------------------------------------------------- #
# 5. Установка даты окончания
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_set_end_date_clears_sent_reminders_and_reports_sync_failure(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, sub = await legacy_only_seed(db)
        db.add_all(
            [
                SentNotification(user_id=1, subscription_id=1, notification_type='expiring', days_before=3),
                SentNotification(user_id=1, subscription_id=1, notification_type='expiring', days_before=1),
            ]
        )
        await db.commit()
        cabinet_boundary(monkeypatch, user, sub, sync=AsyncMock(return_value={'error': 'panel timeout'}))
        new_end = NOW + timedelta(days=60)

        response = await cabinet_call(db, action='set_end_date', end_date=new_end)

        assert response.success is True
        assert (
            response.message == f'Subscription end date set to {new_end.isoformat()} (panel NOT updated: panel timeout)'
        )
        assert await count(db, SentNotification) == 0
        await db.refresh(sub)
        assert sub.end_date == new_end and sub.status == 'active'
