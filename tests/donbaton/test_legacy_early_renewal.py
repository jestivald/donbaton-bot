"""Оплата заранее для перенесённых индивидуальных условий.

~91 подписка, перенесённая 09.09 с нестандартными условиями (4 или 8 устройств,
безлимит), привязана к техническому тарифу 9/10 и помечена
``snapshot.renewal_requires_expiry``. Раньше до ``original_paid_until`` им
отказывали в любой покупке, а в секунду окончания VPN отключался — и без VPN в
РФ клиент не открывает ни Telegram, ни кабинет, чтобы заплатить.

Модель владельца: покупка тарифа на полный срок по обычной цене, без доплат и
пропорций. Новый срок = текущий конец + оплаченный период. До границы действуют
прежние условия (прежний LTE, сквады, больший из двух лимитов устройств, HWID не
сбрасываются), с границы — условия купленного тарифа. Покупка ставит отметку,
и автоплатёж снова работает. ``DONBATON_LEGACY_EARLY_RENEWAL_ENABLED=False``
возвращает прежний замок.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.cabinet.routes.subscription_modules import purchase as cabinet_purchase
from app.cabinet.schemas.subscription import TariffPurchaseRequest
from app.config import settings
from app.database.crud import subscription as subscription_crud, user as user_crud
from app.database.crud.subscription import extend_subscription
from app.database.models import Base, LteQuota, PaymentMethod, Subscription, Tariff, User
from app.services.donbaton_catalog import validate_user_tariff_purchase
from app.services.lte_quota_service import (
    GIB,
    LtePolicyError,
    activate_legacy_quota,
    apply_panel_policy,
    autopay_blocked_by_legacy,
    describe_tariff_switch,
    get_quota_status,
    import_legacy_quota,
    legacy_autopay_blocked,
    legacy_purchase_recorded,
    legacy_terms_keep_devices,
    validate_legacy_renewal,
)
from app.services.pricing_engine import PricingEngine
from tests.donbaton.test_lte_legacy import BASELINE, EPOCH, IMPORT, legacy_seed
from tests.donbaton.test_lte_quota import LTE, NOW, WIFI, sample, tariff
from tests.donbaton.test_tariff_switch import TABLES
from tests.fixtures.sqlite_memory import memory_session


# Боевая сетка ДонБатона (docs/DONBATON.md): Личный — 3 устройства, Семейный — 7.
PRICES = {
    7: {'30': 19900, '90': 52900},
    8: {'30': 29900, '90': 79900},
    9: {'30': 29900, '90': 79900},
    10: {'30': 44900, '90': 119900},
}
WIFI_ONLY = dict(
    network_category='wifi',
    lte_traffic_limit_gb=None,
    lte_period_days=None,
    allowed_squads=[WIFI],
    traffic_topup_enabled=False,
    traffic_topup_packages={},
)
PLANS = {
    7: dict(name='Только Wi-Fi Личный', device_limit=3, **WIFI_ONLY),
    8: dict(name='Только Wi-Fi Семейный', device_limit=7, **WIFI_ONLY),
    10: dict(name='Wi-Fi + LTE Семейный', device_limit=7, lte_traffic_limit_gb=120),
}
_KEEP = object()


@pytest.fixture(autouse=True)
def early_renewal(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_EPOCH', EPOCH)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    # На проде включено с 11.09: другой сетевой тариф покупается в любой момент.
    monkeypatch.setattr(settings, 'DONBATON_TARIFF_SWITCH_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LEGACY_EARLY_RENEWAL_ENABLED', True)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(User, 'get_primary_promo_group', lambda self: None)
    monkeypatch.setattr(PricingEngine, 'resolve_promo_group', staticmethod(lambda user: None))


async def locked_legacy(db, *, devices, hwid=_KEEP, unlimited_lte=False, paid_until=None):
    """Перенос с индивидуальными условиями на технический тариф 10, как у fernandof8.

    ``end_date == original_paid_until`` — через сутки. Тариф 9 («Личный»,
    3 устройства, 60 ГиБ) создаёт сама фикстура переноса.
    """
    sub, personal, snapshot, _ = await legacy_seed(
        db,
        devices=devices,
        custom=True,
        paid_until=paid_until or NOW + timedelta(days=1),
        base_gb=0 if unlimited_lte else 17,
        strategy='NO_RESET' if unlimited_lte else 'MONTH_ROLLING',
    )
    personal.name = 'Wi-Fi + LTE Личный'
    personal.period_prices = PRICES[9]
    plans = {9: personal}
    for number, values in PLANS.items():
        plans[number] = tariff(id=number, period_prices=PRICES[number], **values)
        db.add(plans[number])
    await db.flush()
    if hwid is not _KEEP:
        snapshot['panel']['hwid_device_limit'] = hwid
    quota = await import_legacy_quota(db, sub, plans[10], snapshot=snapshot, **IMPORT, now=NOW)
    await activate_legacy_quota(
        db,
        sub,
        migration_id=IMPORT['migration_id'],
        counter_epoch=EPOCH,
        panel_id=11,
        lifetime_bytes=BASELINE,
        now=NOW,
    )
    user = await db.get(User, sub.user_id)
    user.balance_kopeks = 1_000_000
    await db.commit()
    return sub, quota, plans, datetime.fromisoformat(snapshot['subscription']['end_date'])


async def checkout(db, sub, plan, days):
    """Что делают бот (витрина) и кабинет: котировка, предоплатный гвард, продление.

    Бот для того же тарифа передаёт в котировку лимит устройств подписки и
    сохраняет больший из двух; кабинет — лимит тарифа. Здесь — боевой вариант
    бота, он строже.
    """
    user = await db.get(User, sub.user_id)
    same = sub.tariff_id == plan.id
    quote = await PricingEngine().calculate_tariff_purchase_price(
        plan, days, device_limit=sub.device_limit if same else None, user=user, db=db, subscription=sub
    )
    await validate_user_tariff_purchase(db, user, plan)
    await extend_subscription(
        db,
        sub,
        days,
        tariff_id=plan.id,
        traffic_limit_gb=0,
        device_limit=max(plan.device_limit, sub.device_limit or 0) if same else plan.device_limit,
        connected_squads=plan.allowed_squads,
    )
    await db.commit()
    return quote


# --------------------------------------------------------------------------- #
# Сервисный уровень: общая часть бота и кабинета
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('legacy_devices', 'plan_id', 'days', 'devices_before', 'devices_after', 'lte_gb_after', 'squads_after'),
    [
        pytest.param(4, 10, 30, 7, 7, 120, [WIFI, LTE], id='4-devices-renews-family-lte-30d'),
        pytest.param(4, 9, 30, 4, 3, 60, [WIFI, LTE], id='4-devices-buys-personal-lte-30d'),
        pytest.param(4, 8, 90, 7, 7, 0, [WIFI], id='4-devices-buys-family-wifi-90d'),
        pytest.param(8, 10, 30, 8, 7, 120, [WIFI, LTE], id='8-devices-renews-family-lte-30d'),
    ],
)
async def test_locked_legacy_pays_in_advance_and_gets_plan_terms_after_the_boundary(
    monkeypatch, legacy_devices, plan_id, days, devices_before, devices_after, lte_gb_after, squads_after
):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plans, paid_until = await locked_legacy(db, devices=legacy_devices)
        plan = plans[plan_id]
        assert legacy_autopay_blocked(quota, now=NOW) is True

        quote = await checkout(db, sub, plan, days)
        await db.refresh(quota)

        # Полная цена тарифа за период — ни доплат, ни пропорций.
        assert quote.final_total == PRICES[plan_id][str(days)]
        # Оставшиеся дни прежних условий сохраняются: новый срок — после них.
        assert abs(sub.end_date - (paid_until + timedelta(days=days))) < timedelta(seconds=2)
        assert sub.tariff_id == plan.id
        assert legacy_purchase_recorded(quota.legacy_state)
        assert quota.legacy_state['original_paid_until'] == paid_until.isoformat()

        # --- до границы: прежние условия ---
        inside = NOW + timedelta(hours=1)
        before = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=inside)
        assert before['hwid_device_limit'] == sub.device_limit == devices_before
        assert before['active_internal_squads'] == [WIFI, LTE]  # прежний LTE-пул
        assert quota.limit_bytes == 0  # пакет тарифа ещё не начался
        assert await legacy_terms_keep_devices(db, sub, now=inside) is True
        status = await get_quota_status(db, sub, now=inside)
        assert status['renewal_requires_expiry'] is False
        assert status['legacy_compatibility_until'] == paid_until.isoformat()
        assert legacy_autopay_blocked(quota, now=inside) is False

        # --- с границы: условия купленного тарифа ---
        after = paid_until + timedelta(seconds=1)
        result = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=after)
        assert result['hwid_device_limit'] == sub.device_limit == devices_after
        assert result['active_internal_squads'] == squads_after
        assert quota.limit_bytes == lte_gb_after * GIB
        assert (quota.period_starts_at, quota.period_ends_at) == (paid_until, paid_until + timedelta(days=30))
        assert sub.network_category == plan.network_category
        assert sub.tariff_id == plan.id
        assert await legacy_terms_keep_devices(db, sub, now=after) is False
        await db.commit()
        assert await autopay_blocked_by_legacy(db, sub, now=after) is False
        assert legacy_purchase_recorded((await db.get(LteQuota, sub.id)).legacy_state)

        # Лимит тарифа ставится один раз: дальше его ведут новые покупки и админ.
        sub.device_limit = 11
        later = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=after + timedelta(hours=1))
        assert 'hwid_device_limit' not in later and sub.device_limit == 11


@pytest.mark.asyncio
async def test_unlimited_old_lte_stays_unlimited_until_the_boundary_only(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plans, paid_until = await locked_legacy(db, devices=4, unlimited_lte=True)
        await checkout(db, sub, plans[10], 30)

        inside = NOW + timedelta(hours=1)
        await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=inside)
        assert (await get_quota_status(db, sub, now=inside))['is_unlimited'] is True

        after = paid_until + timedelta(seconds=1)
        result = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE + GIB), now=after)
        status = await get_quota_status(db, sub, now=after)
        # Безлимит был условием прежнего срока; новый тариф — его обычный пакет.
        assert status['is_unlimited'] is False
        assert quota.limit_bytes == 120 * GIB
        assert status['remaining_bytes'] == 120 * GIB
        assert result['active_internal_squads'] == [WIFI, LTE]


@pytest.mark.asyncio
@pytest.mark.parametrize('hwid', [None, 0])
async def test_unlimited_old_devices_are_not_cut_inside_the_window(monkeypatch, hwid):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, plans, paid_until = await locked_legacy(db, devices=4, hwid=hwid)
        described = await describe_tariff_switch(db, sub, plans[9], now=NOW)
        assert described['device_limit_now'] == 0 and described['devices_reset'] is False

        await checkout(db, sub, plans[9], 30)
        inside = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=NOW + timedelta(hours=1))
        assert inside['hwid_device_limit'] == sub.device_limit == 0  # 0 — безлимит, max() его не урезает

        after = await apply_panel_policy(
            db, sub, {}, panel_user=sample(BASELINE), now=paid_until + timedelta(seconds=1)
        )
        assert after['hwid_device_limit'] == sub.device_limit == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(('legacy_devices', 'hwid'), [(8, _KEEP), (4, None)])
async def test_after_the_window_devices_follow_the_plan_even_without_a_purchase(monkeypatch, legacy_devices, hwid):
    """Модель владельца: прежние условия до оплаченной даты, дальше — условия тарифа.

    Бонусные дни (промокод, реферальная награда, админ) покупкой не считаются —
    отметки нет, автоплатёж заблокирован, — но и прежние 8 устройств или
    безлимит за границу не переживают: лимит — тарифа подписки, один раз.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _, paid_until = await locked_legacy(db, devices=legacy_devices, hwid=hwid)
        await extend_subscription(db, sub, 10)  # бонусные дни
        await db.commit()
        assert abs(sub.end_date - (paid_until + timedelta(days=10))) < timedelta(seconds=2)
        assert not legacy_purchase_recorded(quota.legacy_state)

        after = paid_until + timedelta(seconds=1)
        result = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=after)
        assert result['hwid_device_limit'] == sub.device_limit == 7
        assert 'target_devices_applied_at' in quota.legacy_state['runtime']
        await db.commit()
        assert await autopay_blocked_by_legacy(db, sub, now=after) is True


@pytest.mark.asyncio
async def test_disabled_flag_keeps_the_lock_and_names_the_moscow_date(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LEGACY_EARLY_RENEWAL_ENABLED', False)
    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Moscow')
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plans, paid_until = await locked_legacy(db, devices=4)
        user = await db.get(User, sub.user_id)
        moscow = paid_until.astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')

        with pytest.raises(LtePolicyError) as refused:
            await validate_legacy_renewal(db, sub)
        assert f'до {moscow} МСК' in str(refused.value)
        assert 'индивидуальные условия' in str(refused.value)
        status = await get_quota_status(db, sub, now=NOW)
        assert status['renewal_requires_expiry'] is True  # кабинет держит витрину закрытой
        assert legacy_autopay_blocked(quota, now=NOW) is True
        for plan in (plans[10], plans[9]):
            with pytest.raises(LtePolicyError, match='индивидуальные условия'):
                await PricingEngine().calculate_tariff_purchase_price(plan, 30, user=user, db=db, subscription=sub)
        with pytest.raises(LtePolicyError, match='индивидуальные условия'):
            await extend_subscription(db, sub, 30, tariff_id=10, traffic_limit_gb=0, device_limit=7)
        assert sub.end_date == paid_until and not legacy_purchase_recorded(quota.legacy_state)


@pytest.mark.asyncio
async def test_archive_plans_stay_closed(monkeypatch):
    """Архивные тарифы 4/5/6 без категории сети — переход по-прежнему только миграцией."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, _ = await locked_legacy(db, devices=4)
        user = await db.get(User, sub.user_id)
        archive = await db.get(Tariff, 1)
        with pytest.raises(LtePolicyError):
            await validate_user_tariff_purchase(db, user, archive)
        with pytest.raises(LtePolicyError):
            await extend_subscription(db, sub, 30, tariff_id=archive.id, traffic_limit_gb=17, device_limit=4)


# --------------------------------------------------------------------------- #
# Кабинет
# --------------------------------------------------------------------------- #


def _cabinet_boundaries(monkeypatch, db, sub, user, plans):
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
    return reset_devices


@pytest.mark.asyncio
@pytest.mark.parametrize(('plan_id', 'price'), [(9, 29900), (10, 44900)])
async def test_cabinet_purchase_goes_through_without_a_device_reset(monkeypatch, plan_id, price):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plans, paid_until = await locked_legacy(db, devices=4)
        user = await db.get(User, sub.user_id)
        for plan_row in plans.values():
            await db.refresh(plan_row, ['allowed_promo_groups'])
        reset_devices = _cabinet_boundaries(monkeypatch, db, sub, user, plans)

        result = await cabinet_purchase.purchase_tariff(
            TariffPurchaseRequest(tariff_id=plan_id, period_days=30), user=user, db=db
        )

        assert result['success'] is True and result['charged_amount'] == price
        assert sub.tariff_id == plan_id
        assert abs(sub.end_date - (paid_until + timedelta(days=30))) < timedelta(seconds=2)
        # 10 → 9 уменьшает лимит тарифа (7 → 3), но в окне панель держит max():
        # сброс HWID заставил бы клиента заново подключать все устройства.
        reset_devices.assert_not_awaited()
        await db.refresh(quota)
        assert legacy_purchase_recorded(quota.legacy_state)
        assert quota.legacy_state['target_device_limit'] == plans[plan_id].device_limit


@pytest.mark.asyncio
async def test_cabinet_renewal_service_marks_the_purchase(monkeypatch):
    """«Продлить» кабинета и Mini App идёт через SubscriptionRenewalService.finalize."""
    import app.services.subscription_renewal_service as renewal_module
    from app.services.pricing_engine import pricing_engine

    panel = SimpleNamespace(update_remnawave_user=AsyncMock(), create_remnawave_user=AsyncMock())
    monkeypatch.setattr(renewal_module, 'SubscriptionService', lambda: panel)
    monkeypatch.setattr(renewal_module, 'with_admin_notification_service', AsyncMock(return_value=None))
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        sub, quota, _, paid_until = await locked_legacy(db, devices=8)
        user = await db.get(User, sub.user_id)
        loaded = (
            await db.execute(
                select(Subscription).options(selectinload(Subscription.tariff)).where(Subscription.id == sub.id)
            )
        ).scalar_one()
        pricing = await pricing_engine.calculate_renewal_price(db, loaded, 30, user=user)
        assert pricing.final_total == 44900  # тариф 10 без доплаты за 8-е устройство

        await renewal_module.SubscriptionRenewalService().finalize(
            db, user, loaded, pricing, payment_method=PaymentMethod.BALANCE
        )

        stored = await db.get(LteQuota, sub.id)
        await db.refresh(stored)
        assert abs(loaded.end_date - (paid_until + timedelta(days=30))) < timedelta(seconds=2)
        assert legacy_purchase_recorded(stored.legacy_state)
        assert stored.legacy_state['target_device_limit'] == 7


# --------------------------------------------------------------------------- #
# Бот
# --------------------------------------------------------------------------- #


def _callback(data):
    return SimpleNamespace(
        data=data, message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(), bot=MagicMock()
    )


def _state():
    state = MagicMock()
    state.get_data = AsyncMock(return_value={})
    state.update_data = AsyncMock()
    state.clear = AsyncMock()
    return state


def _bot_boundaries(monkeypatch, user):
    from app.handlers.subscription import tariff_purchase as bot

    async def debit(_db, charged_user, amount, *args, **kwargs):
        charged_user.balance_kopeks -= amount
        await _db.commit()
        return True

    panel = SimpleNamespace(update_remnawave_user=AsyncMock(), create_remnawave_user=AsyncMock())
    reset_devices = AsyncMock()
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(bot, 'subtract_user_balance', AsyncMock(side_effect=debit))
    monkeypatch.setattr(bot, 'SubscriptionService', lambda: panel)
    monkeypatch.setattr(bot, 'should_create_panel_account', AsyncMock(return_value=False))
    monkeypatch.setattr(bot, '_reset_panel_devices', reset_devices)
    monkeypatch.setattr(bot, 'create_transaction', AsyncMock())
    monkeypatch.setattr(
        bot,
        'AdminNotificationService',
        lambda _bot: SimpleNamespace(send_subscription_purchase_notification=AsyncMock()),
    )
    monkeypatch.setattr(bot.user_cart_service, 'delete_user_cart', AsyncMock())
    return bot, reset_devices


def _succeeded(callback, marker):
    rendered = callback.message.edit_text.await_args.args[0]
    assert marker in rendered, rendered


@pytest.mark.asyncio
async def test_bot_catalog_purchase_of_the_same_plan_goes_through(monkeypatch):
    """«📦 Тариф» → витрина → Семейный Wi-Fi + LTE на 30 дней у клиента с 4 устройствами.

    Раньше бот отказывал дважды: замок индивидуальных условий и отказ котировки
    «количество устройств закреплено за тарифом» (4 ≠ 7).
    """
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _, paid_until = await locked_legacy(db, devices=4)
        user = await db.get(User, sub.user_id)
        bot, reset_devices = _bot_boundaries(monkeypatch, user)
        callback = _callback('tariff_confirm:10:30')

        await bot.confirm_tariff_purchase.__wrapped__(callback, user, db, _state())

        _succeeded(callback, 'Подписка успешно оформлена')
        assert user.balance_kopeks == 1_000_000 - 44900
        assert abs(sub.end_date - (paid_until + timedelta(days=30))) < timedelta(seconds=2)
        reset_devices.assert_not_awaited()
        await db.refresh(quota)
        assert legacy_purchase_recorded(quota.legacy_state)


@pytest.mark.asyncio
async def test_bot_catalog_purchase_of_another_plan_goes_through(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _, paid_until = await locked_legacy(db, devices=4)
        user = await db.get(User, sub.user_id)
        bot, _ = _bot_boundaries(monkeypatch, user)
        callback = _callback('tariff_confirm:8:90')

        await bot.confirm_tariff_purchase.__wrapped__(callback, user, db, _state())

        _succeeded(callback, 'Подписка успешно оформлена')
        assert user.balance_kopeks == 1_000_000 - 79900
        assert sub.tariff_id == 8
        assert abs(sub.end_date - (paid_until + timedelta(days=90))) < timedelta(seconds=2)
        await db.refresh(quota)
        assert legacy_purchase_recorded(quota.legacy_state)
        assert quota.legacy_state['target_tariff_id'] == 8


@pytest.mark.asyncio
async def test_bot_extend_button_renews_the_current_plan(monkeypatch):
    """«⏰ Продлить» из уведомления за 3 дня у клиента с 8 устройствами (как Allochka)."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, paid_until = await locked_legacy(db, devices=8)
        user = await db.get(User, sub.user_id)
        bot, _ = _bot_boundaries(monkeypatch, user)
        callback = _callback(f'tariff_ext_confirm:{sub.id}:10:30')

        await bot.confirm_tariff_extend.__wrapped__(callback, user, db, _state())

        _succeeded(callback, 'Подписка успешно продлена')
        assert user.balance_kopeks == 1_000_000 - 44900  # без доплаты за 8-е устройство
        assert abs(sub.end_date - (paid_until + timedelta(days=30))) < timedelta(seconds=2)
        after = await apply_panel_policy(
            db, sub, {}, panel_user=sample(BASELINE), now=paid_until + timedelta(seconds=1)
        )
        assert after['hwid_device_limit'] == sub.device_limit == 7


@pytest.mark.asyncio
async def test_bot_extend_button_records_the_purchase(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _, _ = await locked_legacy(db, devices=8)
        user = await db.get(User, sub.user_id)
        bot, _ = _bot_boundaries(monkeypatch, user)
        await bot.confirm_tariff_extend.__wrapped__(_callback(f'tariff_ext_confirm:{sub.id}:10:30'), user, db, _state())
        await db.refresh(quota)
        assert legacy_purchase_recorded(quota.legacy_state)


@pytest.mark.asyncio
async def test_bot_catalog_purchase_keeps_devices_inside_the_window(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, _ = await locked_legacy(db, devices=4)
        user = await db.get(User, sub.user_id)
        bot, reset_devices = _bot_boundaries(monkeypatch, user)
        await bot.confirm_tariff_purchase.__wrapped__(_callback('tariff_confirm:9:30'), user, db, _state())
        assert sub.tariff_id == 9
        reset_devices.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Обычные подписки не задеты
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_same_plan_renewal_of_an_ordinary_subscription_keeps_its_terms(monkeypatch):
    """Для обычного учёта оплаченное продление того же тарифа — прежний initialize_quota."""
    from tests.donbaton.test_tariff_switch import seed

    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plans = await seed(
            db, current=9, used_bytes=5 * GIB, end_date=datetime.now(UTC) + timedelta(days=5)
        )
        before = (quota.limit_bytes, quota.next_limit_bytes, quota.period_starts_at, quota.base_used_bytes)
        plans[9].lte_traffic_limit_gb = 90  # правка тарифа админом не переписывает оплаченное

        await extend_subscription(db, sub, 30, client_purchase=True)
        await db.refresh(quota)

        assert (quota.limit_bytes, quota.next_limit_bytes, quota.period_starts_at, quota.base_used_bytes) == before
        assert quota.legacy_state is None
