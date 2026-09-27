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
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from sqlalchemy import update as sa_update

from app.config import settings
from app.database.crud import user as user_crud
from app.database.models import Base, Subscription, User
from app.handlers.subscription import tariff_purchase
from app.services import subscription_auto_purchase_service as auto
from app.services.lte_quota_service import (
    autopay_blocked_by_legacy,
    initialize_quota,
)
from app.services.pricing_engine import PricingEngine
from tests.donbaton.test_lte_legacy import EPOCH, activate, legacy_seed
from tests.donbaton.test_lte_quota import LTE, NOW, WIFI, tariff
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
