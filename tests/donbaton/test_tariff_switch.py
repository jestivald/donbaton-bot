"""Смена сетевого тарифа = покупка нового тарифа на полный срок.

Модель владельца: доплаты и пропорции нет. Клиент платит полную цену нового
тарифа, оставшиеся дни текущего сохраняются и добавляются к новому сроку, а
условия нового тарифа действуют сразу. Здесь проверяется, что именно эти три
обещания выполняются в леджере, в сроке подписки и на всех трёх поверхностях —
и что при выключенном флаге остаётся прежний запрет.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import purchase as cabinet_purchase, tariff_switch as cabinet_switch
from app.cabinet.schemas.subscription import TariffPurchaseRequest
from app.config import Settings, settings
from app.database.crud import subscription as subscription_crud, user as user_crud
from app.database.crud.subscription import extend_subscription
from app.database.models import LavaSubscription, SentNotification, Subscription, Tariff, User
from app.keyboards import inline as keyboards
from app.services.donbaton_catalog import validate_user_tariff_purchase
from app.services.lte_quota_service import (
    GIB,
    LtePolicyError,
    apply_panel_policy,
    describe_tariff_switch,
    initialize_quota,
    is_paid_tariff_switch,
    update_entitlement,
)
from app.services.pricing_engine import PricingEngine
from app.services.tariff_switch_policy import should_reset_devices_on_switch
from app.webapi.routes import miniapp
from app.webapi.schemas.miniapp import MiniAppTariffSwitchRequest
from tests.donbaton.test_lte_legacy import BASELINE, EPOCH, TABLES as LEGACY_TABLES, activate, legacy_seed
from tests.donbaton.test_lte_quota import LTE, NOW, WIFI, sample, tariff
from tests.fixtures.sqlite_memory import memory_session


TABLES = [*LEGACY_TABLES, SentNotification.__table__, LavaSubscription.__table__]

# Боевая сетка ДонБатона: 9 — «Личный» (60 ГиБ / 2 устройства),
# 10 — «Семейный» (120 ГиБ / 4 устройства), 7 и 8 — те же без LTE.
PLANS = {
    9: dict(id=9, name='Wi-Fi + LTE Личный', lte_traffic_limit_gb=60, device_limit=2),
    10: dict(id=10, name='Wi-Fi + LTE Семейный', lte_traffic_limit_gb=120, device_limit=4),
    7: dict(
        id=7,
        name='Только Wi-Fi Личный',
        network_category='wifi',
        lte_traffic_limit_gb=None,
        lte_period_days=None,
        allowed_squads=[WIFI],
        traffic_topup_enabled=False,
        traffic_topup_packages={},
        device_limit=2,
    ),
    8: dict(
        id=8,
        name='Только Wi-Fi Семейный',
        network_category='wifi',
        lte_traffic_limit_gb=None,
        lte_period_days=None,
        allowed_squads=[WIFI],
        traffic_topup_enabled=False,
        traffic_topup_packages={},
        device_limit=4,
    ),
}


@pytest.fixture(autouse=True)
def switching_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_EPOCH', EPOCH)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    monkeypatch.setattr(settings, 'DONBATON_TARIFF_SWITCH_ENABLED', True)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(User, 'get_primary_promo_group', lambda self: None)


def plan(number: int, **overrides) -> Tariff:
    return tariff(**dict(PLANS[number], period_prices={'30': 29900, '360': 299000}, **overrides))


async def seed(db, *, current=9, used_bytes=0, end_date=None):
    """Живая подписка на сетевом тарифе с обычным (не legacy) учётом."""
    user = User(id=1, telegram_id=123, first_name='Test', status='active', remnawave_id=11, balance_kopeks=100000)
    plans = {number: plan(number) for number in PLANS}
    db.add_all([user, *plans.values()])
    await db.flush()
    sub = Subscription(
        id=1,
        user_id=1,
        status='active',
        is_trial=False,
        tariff_id=current,
        device_limit=plans[current].device_limit,
        end_date=end_date or NOW + timedelta(days=25),
        connected_squads=[WIFI, LTE],
        remnawave_id=11,
        remnawave_short_id='lte001',
    )
    db.add(sub)
    await db.flush()
    quota = await initialize_quota(db, sub, plans[current], now=NOW)
    quota.base_used_bytes = used_bytes
    quota.used_bytes = used_bytes
    await db.commit()
    return sub, quota, plans


# --------------------------------------------------------------------------- #
# Леджер: обычный учёт
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(('current', 'target', 'expected_gb'), [(9, 10, 120), (10, 9, 60), (9, 7, 0), (8, 9, 60)])
async def test_switch_applies_new_allowance_to_the_running_cycle(current, target, expected_gb, monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plans = await seed(db, current=current, used_bytes=7 * GIB)
        anchor_start, anchor_end = quota.period_starts_at, quota.period_ends_at

        await update_entitlement(db, sub, plans[target], now=NOW + timedelta(days=10))

        assert quota.limit_bytes == expected_gb * GIB
        assert quota.next_limit_bytes == expected_gb * GIB
        # Оплаченный расход не прощается: pending_period обнулил бы счётчик, и
        # петля 9 → 10 → 9 выдавала бы бесплатные байты на каждом прыжке.
        assert quota.base_used_bytes == quota.used_bytes == 7 * GIB
        assert quota.pending_period is None
        assert (quota.period_starts_at, quota.period_ends_at) == (anchor_start, anchor_end)
        assert sub.device_limit == plans[target].device_limit
        assert sub.network_category == plans[target].network_category
        assert sub.traffic_limit_gb == 0


@pytest.mark.asyncio
async def test_downgrade_cuts_the_allowance_without_forgiving_spent_bytes(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plans = await seed(db, current=10, used_bytes=100 * GIB)

        await update_entitlement(db, sub, plans[9], now=NOW + timedelta(days=10))

        # Потрачено больше, чем даёт новый тариф — до границы цикла остатка нет.
        assert quota.limit_bytes == 60 * GIB
        assert quota.base_used_bytes == 100 * GIB
        result = await apply_panel_policy(db, sub, {}, now=NOW + timedelta(days=10))
        assert result['active_internal_squads'] == [WIFI]


@pytest.mark.asyncio
async def test_switch_to_wifi_only_drops_lte_at_once(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plans = await seed(db, current=9)
        granted = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=NOW)
        assert granted['active_internal_squads'] == [WIFI, LTE]

        await update_entitlement(db, sub, plans[7], now=NOW + timedelta(days=1))

        assert quota.limit_bytes == 0
        dropped = await apply_panel_policy(
            db, sub, {}, panel_user=sample(BASELINE), now=NOW + timedelta(days=1, seconds=1)
        )
        assert dropped['active_internal_squads'] == [WIFI]


# --------------------------------------------------------------------------- #
# Леджер: окно прежних оплаченных условий
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_legacy_window_moves_devices_now_and_the_allowance_at_the_paid_boundary(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, devices=3)
        quota = await activate(db, sub, target, snapshot)
        bigger = plan(10)
        db.add(bigger)
        await db.flush()
        original = deepcopy(quota.legacy_state['snapshot'])

        await extend_subscription(
            db,
            sub,
            30,
            tariff_id=bigger.id,
            traffic_limit_gb=0,
            device_limit=bigger.device_limit,
            connected_squads=bigger.allowed_squads,
        )
        await db.refresh(quota)

        # Байты прежних условий оплачены до original_paid_until: новый объём
        # ждёт границу, а снимок миграции остаётся нетронутым.
        assert quota.limit_bytes == 0
        assert quota.legacy_state['target_limit_bytes'] == 120 * GIB
        assert quota.legacy_state['target_tariff_id'] == bigger.id
        assert quota.legacy_state['target_device_limit'] == 4
        assert quota.legacy_state['snapshot'] == original

        paid_until = snapshot['subscription']['end_date']
        before = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=NOW + timedelta(days=1))
        assert before['hwid_device_limit'] == sub.device_limit == 4  # устройства — сразу
        after = await apply_panel_policy(
            db, sub, {}, panel_user=sample(BASELINE), now=datetime.fromisoformat(paid_until) + timedelta(seconds=1)
        )
        assert quota.limit_bytes == 120 * GIB
        assert after['active_internal_squads'] == [WIFI, LTE]


@pytest.mark.asyncio
async def test_legacy_window_never_takes_away_devices_already_paid_for(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, devices=15)
        quota = await activate(db, sub, target, snapshot)
        # id 9 в этой фикстуре уже занят тарифом переноса.
        smaller = plan(9, id=91)
        db.add(smaller)
        await db.flush()

        await update_entitlement(db, sub, smaller, now=NOW + timedelta(days=1))
        await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=NOW + timedelta(days=1))

        assert quota.legacy_state['target_device_limit'] == 2
        assert sub.device_limit == 15


@pytest.mark.asyncio
async def test_describe_switch_tells_each_surface_the_same_story(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, plans = await seed(db, current=9)
        immediate = await describe_tariff_switch(db, sub, plans[10], now=NOW)
        assert immediate == {
            'mode': 'immediate',
            'current_category': 'wifi_lte',
            'new_category': 'wifi_lte',
            'device_limit_now': 4,
            'devices_reset': False,
            'lte_limit_gb_now': 120,
            'lte_limit_gb_next': 120,
            'lte_effective_at': None,
            'legacy_until': None,
            'remaining_days': 25,
        }
        downgrade = await describe_tariff_switch(db, sub, plans[7], now=NOW)
        assert downgrade['devices_reset'] is False  # 2 → 2, лимит не уменьшился
        assert downgrade['lte_limit_gb_next'] == 0


@pytest.mark.asyncio
async def test_describe_switch_dates_the_allowance_inside_the_legacy_window(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, devices=3)
        await activate(db, sub, target, snapshot)
        bigger = plan(10)
        db.add(bigger)
        await db.flush()

        described = await describe_tariff_switch(db, sub, bigger, now=NOW)
        assert described['mode'] == 'after_legacy'
        assert described['lte_limit_gb_now'] is None
        assert described['lte_limit_gb_next'] == 120
        assert described['legacy_until'] == described['lte_effective_at'] == snapshot['subscription']['end_date']
        assert described['device_limit_now'] == 4


# --------------------------------------------------------------------------- #
# Отказы, которые остаются
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_custom_terms_still_wait_for_the_paid_end(monkeypatch):
    # Замок индивидуальных условий — поведение при выключенной ранней оплате.
    monkeypatch.setattr(settings, 'DONBATON_LEGACY_EARLY_RENEWAL_ENABLED', False)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, custom=True, devices=15)
        await activate(db, sub, target, snapshot)
        bigger = plan(10)
        db.add(bigger)
        await db.flush()

        with pytest.raises(LtePolicyError, match='индивидуальные условия'):
            await update_entitlement(db, sub, bigger, now=NOW + timedelta(days=1))
        assert sub.tariff_id == target.id and sub.device_limit == 15


@pytest.mark.asyncio
async def test_live_old_subscription_cannot_buy_a_network_plan(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, _, _ = await legacy_seed(db)
        # Старый учёт → сетевой тариф: живая подписка ждёт явной миграции, а не
        # покупки. Правило смены сетевых тарифов на эту пару не распространяется.
        assert is_paid_tariff_switch(sub, target) is False
        with pytest.raises(LtePolicyError, match='после окончания подписки'):
            await update_entitlement(db, sub, target)
        assert sub.network_category is None and sub.tariff_id == 1


@pytest.mark.asyncio
async def test_network_subscription_cannot_walk_back_to_old_accounting(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        managed, _, _ = await seed(db)
        old_plan = Tariff(id=99, name='Old plan', traffic_limit_gb=100, device_limit=3, period_prices={'30': 29900})
        db.add(old_plan)
        await db.flush()
        with pytest.raises(LtePolicyError, match='после окончания подписки'):
            await update_entitlement(db, managed, old_plan, now=NOW)
        managed.end_date = NOW - timedelta(seconds=1)
        # Даже после окончания срока обратный переход остаётся миграцией, а не
        # покупкой: у подписки свой леджер и оплаченные докупки.
        with pytest.raises(LtePolicyError, match='требует отдельной миграции'):
            await update_entitlement(db, managed, old_plan, now=NOW)
        assert managed.tariff_id == 9


@pytest.mark.asyncio
async def test_disabled_flag_keeps_the_previous_refusal(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_TARIFF_SWITCH_ENABLED', False)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plans = await seed(db)
        assert is_paid_tariff_switch(sub, plans[10]) is False
        with pytest.raises(LtePolicyError, match='после окончания подписки'):
            await update_entitlement(db, sub, plans[10], now=NOW)
        assert quota.limit_bytes == 60 * GIB and sub.tariff_id == 9


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['limited', 'trial'])
async def test_only_a_live_paid_subscription_switches(status, monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, plans = await seed(db)
        sub.status = status
        sub.is_trial = status == 'trial'
        assert is_paid_tariff_switch(sub, plans[10]) is False


# --------------------------------------------------------------------------- #
# Путь покупки в боте
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_bot_checkout_quotes_the_full_period_price_for_another_plan(enabled, monkeypatch):
    """Все замки на пути `tariff_select:` → период → `tariff_confirm:` сняты разом.

    Котировка и предоплатный гвард — разные точки входа в одну политику; если бы
    открылась только одна, клиент увидел бы цену и получил отказ после списания.
    """
    monkeypatch.setattr(settings, 'DONBATON_TARIFF_SWITCH_ENABLED', enabled)
    monkeypatch.setattr(PricingEngine, 'resolve_promo_group', staticmethod(lambda user: None))
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, plans = await seed(db)
        user = await db.get(User, sub.user_id)
        if not enabled:
            with pytest.raises(LtePolicyError, match='после окончания подписки'):
                await PricingEngine().calculate_tariff_purchase_price(plans[10], 30, user=user, db=db, subscription=sub)
            with pytest.raises(LtePolicyError, match='после окончания подписки'):
                await validate_user_tariff_purchase(db, user, plans[10])
            return
        quote = await PricingEngine().calculate_tariff_purchase_price(plans[10], 30, user=user, db=db, subscription=sub)
        # Полная цена периода — ни пропорции за остаток, ни скидки за смену.
        assert quote.final_total == 29900
        await validate_user_tariff_purchase(db, user, plans[10])


def test_live_network_subscription_gets_the_catalogue_button(monkeypatch):
    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(keyboards, 'get_display_subscription_link', lambda sub: None)
    live = SimpleNamespace(
        id=1,
        # У сетевой подписки тариф есть всегда (FK RESTRICT); без него апстримовское
        # правило 4.14.0 сочло бы её «старой подпиской без тарифа».
        tariff_id=9,
        actual_status='active',
        status='active',
        network_category='wifi_lte',
        tariff=None,
        traffic_limit_gb=0,
        end_date=None,
        is_daily_paused=False,
    )

    markup = keyboards.get_subscription_keyboard('ru', has_subscription=True, is_trial=False, subscription=live)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]
    # Витрина, а не мгновенное переключение: у сетевых тарифов его нет.
    assert 'tariff_list' in callbacks
    assert 'instant_switch' not in callbacks and 'tariff_switch' not in callbacks

    monkeypatch.setattr(settings, 'DONBATON_TARIFF_SWITCH_ENABLED', False)
    off = keyboards.get_subscription_keyboard('ru', has_subscription=True, is_trial=False, subscription=live)
    off_callbacks = [button.callback_data for row in off.inline_keyboard for button in row if button.callback_data]
    assert 'tariff_list' not in off_callbacks


# --------------------------------------------------------------------------- #
# Срок подписки
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_purchase_of_another_plan_keeps_the_remaining_days(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plans = await seed(db, used_bytes=5 * GIB, end_date=datetime.now(UTC) + timedelta(days=25))
        before = datetime.now(UTC)

        await extend_subscription(
            db,
            sub,
            30,
            tariff_id=10,
            traffic_limit_gb=0,
            device_limit=plans[10].device_limit,
            connected_squads=plans[10].allowed_squads,
        )
        await db.refresh(quota)

        assert sub.tariff_id == 10
        assert sub.start_date >= before
        # Остаток не сгорает: новый срок = сейчас + 30 дн. + остаток 25 дн.
        assert timedelta(days=54, hours=23) < sub.end_date - before < timedelta(days=55, minutes=1)
        assert quota.limit_bytes == 120 * GIB
        assert quota.base_used_bytes == 5 * GIB
        assert sub.device_limit == 4


# --------------------------------------------------------------------------- #
# Сброс устройств
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ('current', 'target', 'expected'), [(9, 10, False), (10, 9, True), (9, 9, False), (7, 8, False), (8, 7, True)]
)
def test_network_devices_reset_only_when_the_limit_shrinks(current, target, expected):
    assert should_reset_devices_on_switch(plan(current), plan(target)) is expected


def test_legacy_pairs_keep_the_upstream_unconditional_reset():
    legacy = Tariff(id=1, name='Old plan', traffic_limit_gb=100, device_limit=9)
    assert should_reset_devices_on_switch(legacy, plan(10)) is True
    assert should_reset_devices_on_switch(plan(10), legacy) is True
    assert should_reset_devices_on_switch(None, plan(10)) is True


# --------------------------------------------------------------------------- #
# Поверхности: кабинет и мини-апп
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['preview_tariff_switch', 'switch_tariff'])
async def test_cabinet_switch_sends_network_plans_to_the_purchase_flow(endpoint, monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, plans = await seed(db)
        user = await db.get(User, sub.user_id)
        monkeypatch.setattr(cabinet_switch, 'resolve_subscription', AsyncMock(return_value=sub))
        monkeypatch.setattr(cabinet_switch, 'get_tariff_by_id', AsyncMock(side_effect=lambda _db, id_: plans[id_]))

        with pytest.raises(HTTPException) as caught:
            await getattr(cabinet_switch, endpoint)(
                TariffPurchaseRequest(tariff_id=10, period_days=30), user=user, db=db, subscription_id=None
            )
        assert caught.value.status_code == 400
        assert caught.value.detail == {
            'code': 'network_purchase_only',
            'message': (
                'Смена тарифа Wi-Fi / Wi-Fi + LTE — это покупка нового тарифа на полный срок: '
                'оставшиеся дни текущего тарифа сохраняются и добавляются к новому сроку.'
            ),
            'use_purchase_flow': True,
        }
        assert sub.tariff_id == 9


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['preview_tariff_switch_endpoint', 'switch_tariff_endpoint'])
async def test_miniapp_switch_sends_network_plans_to_the_purchase_flow(endpoint, monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, plans = await seed(db)
        user = await db.get(User, sub.user_id)
        await db.refresh(user, ['subscriptions'])
        monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
        monkeypatch.setattr(miniapp, 'get_tariff_by_id', AsyncMock(side_effect=lambda _db, id_: plans[id_]))

        with pytest.raises(HTTPException) as caught:
            await getattr(miniapp, endpoint)(
                MiniAppTariffSwitchRequest(init_data='x', tariff_id=10, subscription_id=sub.id), db=db
            )
        assert caught.value.status_code == 400
        assert caught.value.detail['code'] == 'network_purchase_only'
        assert caught.value.detail['use_purchase_flow'] is True
        assert sub.tariff_id == 9


@pytest.mark.asyncio
async def test_cabinet_purchase_of_another_network_plan_goes_through(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plans = await seed(db, used_bytes=3 * GIB, end_date=datetime.now(UTC) + timedelta(days=25))
        user = await db.get(User, sub.user_id)
        for plan_row in plans.values():
            await db.refresh(plan_row, ['allowed_promo_groups'])
        reset_devices = AsyncMock()
        monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
        monkeypatch.setattr(cabinet_purchase, 'get_tariff_by_id', AsyncMock(side_effect=lambda _db, id_: plans[id_]))
        monkeypatch.setattr(cabinet_purchase, 'get_subscription_by_user_id', AsyncMock(return_value=sub))
        monkeypatch.setattr(subscription_crud, 'get_subscription_by_user_id', AsyncMock(return_value=sub))
        monkeypatch.setattr(subscription_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
        monkeypatch.setattr(cabinet_purchase, 'get_server_squad_by_uuid', AsyncMock(return_value=None))
        monkeypatch.setattr(cabinet_purchase.SubscriptionService, 'update_remnawave_user', AsyncMock())
        monkeypatch.setattr(cabinet_purchase.SubscriptionService, 'create_remnawave_user', AsyncMock())
        monkeypatch.setattr(cabinet_purchase, '_reset_switch_devices', reset_devices)
        monkeypatch.setattr(cabinet_purchase.user_cart_service, 'save_user_cart', AsyncMock())
        monkeypatch.setattr(cabinet_purchase, 'create_transaction', AsyncMock(return_value=SimpleNamespace(id=1)))
        monkeypatch.setattr(cabinet_purchase, 'get_user_by_id', AsyncMock(return_value=user))

        async def debit(_db, charged_user, amount, *args, **kwargs):
            charged_user.balance_kopeks -= amount
            await _db.commit()
            return True

        monkeypatch.setattr(cabinet_purchase, 'subtract_user_balance', AsyncMock(side_effect=debit))
        monkeypatch.setattr(cabinet_purchase, 'add_user_balance', AsyncMock())

        result = await cabinet_purchase.purchase_tariff(
            TariffPurchaseRequest(tariff_id=10, period_days=30), user=user, db=db
        )

        assert result['success'] is True
        # Полная цена периода, без пропорции и без скидки за остаток.
        assert result['charged_amount'] == 29900
        assert sub.tariff_id == 10 and sub.device_limit == 4
        await db.refresh(quota)
        assert quota.limit_bytes == 120 * GIB and quota.base_used_bytes == 3 * GIB
        # Апгрейд лимита устройств сбросом HWID не наказывают.
        reset_devices.assert_not_awaited()


@pytest.mark.asyncio
async def test_cabinet_purchase_of_a_smaller_network_plan_clears_devices(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, plans = await seed(db, current=10, end_date=datetime.now(UTC) + timedelta(days=25))
        user = await db.get(User, sub.user_id)
        for plan_row in plans.values():
            await db.refresh(plan_row, ['allowed_promo_groups'])
        reset_devices = AsyncMock()
        monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
        monkeypatch.setattr(cabinet_purchase, 'get_tariff_by_id', AsyncMock(side_effect=lambda _db, id_: plans[id_]))
        monkeypatch.setattr(cabinet_purchase, 'get_subscription_by_user_id', AsyncMock(return_value=sub))
        monkeypatch.setattr(subscription_crud, 'get_subscription_by_user_id', AsyncMock(return_value=sub))
        monkeypatch.setattr(subscription_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
        monkeypatch.setattr(cabinet_purchase, 'get_server_squad_by_uuid', AsyncMock(return_value=None))
        monkeypatch.setattr(cabinet_purchase.SubscriptionService, 'update_remnawave_user', AsyncMock())
        monkeypatch.setattr(cabinet_purchase.SubscriptionService, 'create_remnawave_user', AsyncMock())
        monkeypatch.setattr(cabinet_purchase, '_reset_switch_devices', reset_devices)
        monkeypatch.setattr(cabinet_purchase.user_cart_service, 'save_user_cart', AsyncMock())
        monkeypatch.setattr(cabinet_purchase, 'create_transaction', AsyncMock(return_value=SimpleNamespace(id=1)))
        monkeypatch.setattr(cabinet_purchase, 'get_user_by_id', AsyncMock(return_value=user))

        async def debit(_db, charged_user, amount, *args, **kwargs):
            charged_user.balance_kopeks -= amount
            await _db.commit()
            return True

        monkeypatch.setattr(cabinet_purchase, 'subtract_user_balance', AsyncMock(side_effect=debit))
        monkeypatch.setattr(cabinet_purchase, 'add_user_balance', AsyncMock())

        await cabinet_purchase.purchase_tariff(TariffPurchaseRequest(tariff_id=9, period_days=30), user=user, db=db)

        assert sub.tariff_id == 9 and sub.device_limit == 2
        reset_devices.assert_awaited_once()
