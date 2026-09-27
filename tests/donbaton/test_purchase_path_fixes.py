"""Путь покупки клиента: исправления по аудиту 27.09.2026.

1. Покупка и продление ТОГО ЖЕ сетевого тарифа у подписки, чей фактический
   лимит устройств отличается от тарифного (перенесённые 4, 8, 0…), падали
   в движке «закреплено за тарифом» → «❌ Произошла ошибка».
2. Пополнение баланса само покупало технический тариф истёкшей перенесённой
   подписке с индивидуальными условиями.
3. Отказ политики доходит до клиента текстом, а не общим «Произошла ошибка».
4. Кабинет: продление пробной сетевой подписки — пустой список и понятный 400.
5. Мини-апп: сетевой подписке нельзя купить устройства.
6. Сбой продления после списания возвращает деньги.
7. Тексты про устройства.

Модель владельца (docs/DONBATON.md): сетевой тариф — фиксированный лимит
устройств; смена тарифа — обычная покупка на полный срок.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from fastapi import HTTPException
from sqlalchemy import update as sa_update

from app.config import Settings, settings
from app.database.crud import user as user_crud
from app.database.models import Base, Subscription, User
from app.handlers.subscription import tariff_purchase
from app.keyboards import inline as keyboards
from app.services import subscription_auto_purchase_service as auto
from app.services.lte_quota_service import (
    LtePolicyError,
    apply_panel_policy,
    autopay_blocked_by_legacy,
    initialize_quota,
)
from app.services.pricing_engine import PricingEngine, pricing_engine
from app.webapi.routes import miniapp
from tests.donbaton.test_lte_legacy import BASELINE, EPOCH, activate, legacy_seed
from tests.donbaton.test_lte_quota import LTE, NOW, WIFI, sample, tariff
from tests.fixtures.sqlite_memory import memory_session


# Хендлеры и сервисы пишут транзакции, корзины, уведомления: берём всю схему.
TABLES = list(Base.metadata.sorted_tables)
PRICE_30 = 29900  # тариф 9 из legacy_seed: 299 ₽ за 30 дней, 3 устройства


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_EPOCH', EPOCH)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    monkeypatch.setattr(settings, 'DONBATON_TARIFF_SWITCH_ENABLED', True)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(PricingEngine, 'resolve_promo_group', staticmethod(lambda user: None))
    monkeypatch.setattr(User, 'get_primary_promo_group', lambda self: None)


def keep_legacy_lock(monkeypatch):
    """Замок индивидуальных условий — как на проде до решения владельца.

    Параллельная ветка вводит ``DONBATON_LEGACY_EARLY_RENEWAL_ENABLED`` (по
    умолчанию True), при котором ``validate_legacy_renewal`` перестаёт отказывать.
    Тесты показа отказа проверяют именно отказ, поэтому держат флаг выключенным.
    """
    if hasattr(settings, 'DONBATON_LEGACY_EARLY_RENEWAL_ENABLED'):
        monkeypatch.setattr(settings, 'DONBATON_LEGACY_EARLY_RENEWAL_ENABLED', False)


def callback(data):
    cb = MagicMock(spec=types.CallbackQuery)
    cb.data = data
    cb.answer = AsyncMock()
    cb.bot = MagicMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    return cb


def fsm(data=None):
    st = MagicMock()
    st.get_data = AsyncMock(return_value=data or {})
    st.update_data = AsyncMock()
    st.clear = AsyncMock()
    return st


def shown_text(cb) -> str:
    return cb.message.edit_text.await_args.args[0]


def buttons(cb) -> list[tuple[str, str]]:
    markup = cb.message.edit_text.await_args.kwargs['reply_markup']
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


def assert_alert(cb, text_part: str) -> None:
    cb.answer.assert_awaited()
    args, kwargs = cb.answer.await_args
    assert text_part in args[0], args[0]
    assert kwargs.get('show_alert') is True
    assert args[0] != '❌ Произошла ошибка'


async def migrated(db, *, devices=8, custom=False, balance=10**6, **seed):
    """Перенесённая подписка на техническом тарифе 9 (3 устройства)."""
    sub, target, snapshot, _ = await legacy_seed(db, devices=devices, custom=custom, **seed)
    await activate(db, sub, target, snapshot)
    user = await db.get(User, 1)
    user.balance_kopeks = balance
    await db.commit()
    return sub, target, user


async def locked_user(db, user_id):
    """``lock_user_for_pricing`` без ``FOR UPDATE``: SQLite его не знает."""
    return await db.get(User, user_id)


def mute_side_effects(monkeypatch):
    """Всё, что после списания и продления уходит наружу: панель, админы, корзина."""
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', locked_user)
    monkeypatch.setattr(tariff_purchase, 'should_create_panel_account', AsyncMock(return_value=False))
    monkeypatch.setattr(tariff_purchase.SubscriptionService, 'update_remnawave_user', AsyncMock())
    monkeypatch.setattr(tariff_purchase.SubscriptionService, 'create_remnawave_user', AsyncMock())
    monkeypatch.setattr(
        tariff_purchase.AdminNotificationService, 'send_subscription_purchase_notification', AsyncMock()
    )
    monkeypatch.setattr(tariff_purchase.user_cart_service, 'delete_user_cart', AsyncMock())
    monkeypatch.setattr(tariff_purchase.user_cart_service, 'save_user_cart', AsyncMock())


# --------------------------------------------------------------------------- #
# 1. Тот же сетевой тариф при «чужом» лимите устройств
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize('devices', [8, 4, 0])
async def test_bot_quotes_the_same_network_tariff_at_its_own_device_limit(devices, monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, user = await migrated(db, devices=devices)

        buy = callback('tariff_period:9:30')
        await tariff_purchase.select_tariff_period(buy, db_user=user, db=db, state=fsm())
        text = shown_text(buy)
        assert 'Подтверждение покупки' in text
        assert 'Устройств: 3' in text and 'Итого: 299' in text

        extend = callback(f'tariff_extend:{sub.id}:9:30')
        await tariff_purchase.select_tariff_extend_period(extend, db_user=user, db=db, state=fsm())
        text = shown_text(extend)
        assert 'Подтверждение продления' in text
        assert 'Устройств: 3' in text and 'К оплате: 299' in text


@pytest.mark.asyncio
async def test_renewal_screen_shows_tariff_devices_without_device_surcharge(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, user = await migrated(db, devices=8)
        target.device_price_kopeks = 10000  # доплата за устройства у апстрима
        await db.commit()

        cb = callback('subscription_extend')
        await tariff_purchase.show_tariff_extend(cb, user, db)

        assert 'Устройств: 3' in shown_text(cb)
        period_buttons = [text for text, data in buttons(cb) if data.startswith('tariff_extend:')]
        assert period_buttons and all('299' in text for text in period_buttons), period_buttons


@pytest.mark.asyncio
async def test_bot_purchase_of_the_same_network_tariff_goes_through(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, user = await migrated(db, devices=8)
        end_before = sub.end_date
        mute_side_effects(monkeypatch)

        cb = callback('tariff_confirm:9:30')
        await tariff_purchase.confirm_tariff_purchase(cb, db_user=user, db=db, state=fsm())

        assert 'Подписка успешно оформлена' in shown_text(cb)
        await db.refresh(user)
        await db.refresh(sub)
        assert user.balance_kopeks == 10**6 - PRICE_30
        assert sub.end_date - end_before == timedelta(days=30)
        # Покупка тарифа 9 — это его 3 устройства; права переноса до конца
        # оплаченного срока по-прежнему держит apply_panel_policy.
        assert sub.device_limit == 3
        payload = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=NOW + timedelta(days=1))
        assert payload['hwid_device_limit'] == 8


@pytest.mark.asyncio
async def test_saved_cart_buys_the_same_network_tariff_at_its_price(monkeypatch):
    class Stop(BaseException):
        """Остановиться на продлении: дальше панель и уведомления."""

    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, user = await migrated(db, devices=8)
        monkeypatch.setattr(user_crud, 'lock_user_for_pricing', locked_user)
        charged = AsyncMock(return_value=True)
        monkeypatch.setattr(user_crud, 'subtract_user_balance', charged)
        extended = AsyncMock(side_effect=Stop)
        monkeypatch.setattr('app.database.crud.subscription.extend_subscription', extended)

        with pytest.raises(Stop):
            await auto._auto_purchase_tariff(
                db, user, {'cart_mode': 'tariff_purchase', 'tariff_id': 9, 'period_days': 30}
            )

        assert charged.await_args.args[2] == PRICE_30
        assert extended.await_args.kwargs['device_limit'] == 3


@pytest.mark.asyncio
async def test_miniapp_purchase_of_the_same_network_tariff_goes_through(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, user = await migrated(db, devices=8)
        await db.refresh(user, ['subscriptions'])
        await db.refresh(target, ['allowed_promo_groups'])
        monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
        monkeypatch.setattr(miniapp, 'get_tariff_by_id', AsyncMock(return_value=target))
        monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
        charged = AsyncMock(return_value=True)
        monkeypatch.setattr(miniapp, 'subtract_user_balance', charged)
        monkeypatch.setattr(miniapp, 'create_transaction', AsyncMock())
        extended = AsyncMock(return_value=sub)
        monkeypatch.setattr(miniapp, 'extend_subscription', extended)
        monkeypatch.setattr(miniapp.SubscriptionService, 'sync_remnawave_user', AsyncMock())
        monkeypatch.setattr('app.services.user_cart_service.user_cart_service.save_user_cart', AsyncMock())

        payload = miniapp.MiniAppTariffPurchaseRequest(initData='x', tariffId=9, periodDays=30)
        response = await miniapp.purchase_tariff_endpoint(payload, db=db)

        assert response.success
        assert charged.await_args.args[2] == PRICE_30
        assert extended.await_args.kwargs['device_limit'] == 3


@pytest.mark.asyncio
async def test_miniapp_purchase_refusal_is_a_400_with_the_reason(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, user = await migrated(db, devices=3)
        await db.refresh(user, ['subscriptions'])
        await db.refresh(target, ['allowed_promo_groups'])
        monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
        monkeypatch.setattr(miniapp, 'get_tariff_by_id', AsyncMock(return_value=target))
        monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
        monkeypatch.setattr(
            pricing_engine, 'calculate_tariff_purchase_price', AsyncMock(side_effect=LtePolicyError('Причина отказа'))
        )
        charged = AsyncMock()
        monkeypatch.setattr(miniapp, 'subtract_user_balance', charged)

        payload = miniapp.MiniAppTariffPurchaseRequest(initData='x', tariffId=9, periodDays=30)
        with pytest.raises(HTTPException) as error:
            await miniapp.purchase_tariff_endpoint(payload, db=db)

        assert error.value.status_code == 400
        assert error.value.detail['message'] == 'Причина отказа'
        charged.assert_not_awaited()


# --------------------------------------------------------------------------- #
# 2. Пополнение баланса не покупает технический тариф замку
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_topup_does_not_auto_extend_expired_locked_legacy(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, user = await migrated(
            db, custom=True, paid_until=NOW - timedelta(days=1), devices=3, status='expired', balance=100000
        )
        sub.autopay_enabled = True
        await db.commit()
        await db.execute(
            sa_update(Subscription).where(Subscription.id == 1).values(updated_at=NOW - timedelta(hours=1))
        )
        await db.commit()
        # Фоновый автоплатёж и рекуррент такой подписке запрещены — тот же предикат.
        assert await autopay_blocked_by_legacy(db, sub) is True

        monkeypatch.setattr(user_crud, 'lock_user_for_pricing', locked_user)
        charged = AsyncMock(return_value=True)
        monkeypatch.setattr(auto, 'subtract_user_balance', charged)
        extended = AsyncMock()
        monkeypatch.setattr(auto, 'extend_subscription', extended)

        assert await auto.try_auto_extend_expired_after_topup(db, user) is False

        charged.assert_not_awaited()
        extended.assert_not_awaited()


@pytest.mark.asyncio
async def test_topup_still_auto_extends_an_ordinary_network_subscription(monkeypatch):
    """Гвард режет только замок: обычная сетевая подписка продлевается как раньше."""

    class Stop(BaseException):
        pass

    async with memory_session(monkeypatch, TABLES) as db:
        plan = tariff(id=9, device_limit=3)
        user = User(id=1, telegram_id=123, first_name='T', status='active', balance_kopeks=100000)
        db.add_all([plan, user])
        await db.flush()
        sub = Subscription(
            id=1,
            user_id=1,
            tariff_id=9,
            status='expired',
            is_trial=False,
            autopay_enabled=True,
            device_limit=3,
            end_date=NOW - timedelta(days=1),
            connected_squads=[WIFI, LTE],
        )
        db.add(sub)
        await db.flush()
        await initialize_quota(db, sub, plan, now=NOW - timedelta(days=31))
        await db.commit()
        await db.execute(
            sa_update(Subscription).where(Subscription.id == 1).values(updated_at=NOW - timedelta(hours=1))
        )
        await db.commit()
        await db.refresh(sub, ['tariff'])
        assert await autopay_blocked_by_legacy(db, sub) is False

        monkeypatch.setattr(user_crud, 'lock_user_for_pricing', locked_user)
        charged = AsyncMock(side_effect=Stop)
        monkeypatch.setattr(auto, 'subtract_user_balance', charged)

        with pytest.raises(Stop):
            await auto.try_auto_extend_expired_after_topup(db, user)
        assert charged.await_args.args[2] == PRICE_30


# --------------------------------------------------------------------------- #
# 3. Отказ политики доходит до клиента
# --------------------------------------------------------------------------- #


LOCK_REASON = 'индивидуальные условия действуют до окончания оплаченного срока'


@pytest.mark.asyncio
async def test_locked_legacy_sees_the_reason_not_a_generic_error(monkeypatch):
    keep_legacy_lock(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, user = await migrated(db, devices=3, custom=True)

        extend = callback(f'tariff_extend:{sub.id}:9:30')
        await tariff_purchase.select_tariff_extend_period(extend, db_user=user, db=db, state=fsm())
        assert_alert(extend, LOCK_REASON)

        buy = callback('tariff_period:9:30')
        await tariff_purchase.select_tariff_period(buy, db_user=user, db=db, state=fsm())
        assert_alert(buy, LOCK_REASON)

        mute_side_effects(monkeypatch)
        charged = AsyncMock(return_value=True)
        monkeypatch.setattr(tariff_purchase, 'subtract_user_balance', charged)
        for handler, data in (
            (tariff_purchase.confirm_tariff_purchase, 'tariff_confirm:9:30'),
            (tariff_purchase.confirm_tariff_extend, f'tariff_ext_confirm:{sub.id}:9:30'),
        ):
            cb = callback(data)
            await handler(cb, db_user=user, db=db, state=fsm())
            assert_alert(cb, LOCK_REASON)
        charged.assert_not_awaited()


@pytest.mark.asyncio
async def test_renewal_screen_explains_the_lock_before_any_price(monkeypatch):
    keep_legacy_lock(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, user = await migrated(db, devices=3, custom=True)

        cb = callback('subscription_extend')
        await tariff_purchase.show_tariff_extend(cb, user, db)

        assert LOCK_REASON in shown_text(cb)
        callbacks = [data for _, data in buttons(cb)]
        # Докупка LTE прежним условиям доступна; цен продления нет.
        assert callbacks == ['buy_traffic', 'menu_subscription']


@pytest.mark.asyncio
async def test_pre_charge_policy_refusal_shows_its_reason(monkeypatch):
    """Отказ validate_user_tariff_purchase — не «❌ Ошибка списания баланса»."""
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, user = await migrated(db, devices=3)
        mute_side_effects(monkeypatch)
        monkeypatch.setattr(
            'app.services.donbaton_catalog.validate_user_tariff_purchase',
            AsyncMock(side_effect=LtePolicyError('Сначала необходимо сохранить оплаченные остатки')),
        )
        charged = AsyncMock(return_value=True)
        monkeypatch.setattr(tariff_purchase, 'subtract_user_balance', charged)

        cb = callback('tariff_confirm:9:30')
        await tariff_purchase.confirm_tariff_purchase(cb, db_user=user, db=db, state=fsm())

        assert_alert(cb, 'Сначала необходимо сохранить оплаченные остатки')
        cb.message.edit_text.assert_not_awaited()
        charged.assert_not_awaited()


@pytest.mark.asyncio
async def test_miniapp_renewal_refusal_is_a_400_with_the_reason(monkeypatch):
    """Отказ политики — не 502 «Failed to calculate renewal pricing»."""
    keep_legacy_lock(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, user = await migrated(db, devices=3, custom=True)
        await db.refresh(user, ['subscriptions'])
        monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
        monkeypatch.setattr(user_crud, 'lock_user_for_pricing', locked_user)

        payload = miniapp.MiniAppSubscriptionRenewalRequest(initData='x', subscriptionId=sub.id, periodDays=30)
        with pytest.raises(HTTPException) as error:
            await miniapp.submit_subscription_renewal_endpoint(payload, db=db)

        assert error.value.status_code == 400
        assert error.value.detail['code'] == 'renewal_rejected'
        assert LOCK_REASON in error.value.detail['message']


@pytest.mark.parametrize('locked', [True, False])
def test_tariff_button_is_hidden_while_legacy_terms_are_locked(locked, monkeypatch):
    keep_legacy_lock(monkeypatch)
    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(keyboards, 'get_display_subscription_link', lambda sub: None)
    live = SimpleNamespace(
        id=1,
        tariff_id=9,
        actual_status='active',
        status='active',
        network_category='wifi_lte',
        tariff=None,
        traffic_limit_gb=0,
        end_date=None,
        is_daily_paused=False,
        # экран подписки загружает статус квоты до клавиатуры
        _lte_quota_status={'renewal_requires_expiry': locked},
    )

    markup = keyboards.get_subscription_keyboard('ru', has_subscription=True, is_trial=False, subscription=live)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]

    assert ('tariff_list' in callbacks) is (not locked)
