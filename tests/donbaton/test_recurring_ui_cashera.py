"""Cashera (апстрим 4.16) не предлагает внешнюю автооплату подпискам Wi-Fi / Wi-Fi + LTE.

То же правило, что у СБП Platega и Lava (test_recurring_ui.py): новые привязки сетевым
подпискам не оформляются ни кнопкой, ни подделанным колбэком; живую привязку отключить можно.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.handlers.subscription import autopay, tariff_purchase


def callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


@pytest.fixture
def cashera_enabled(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_cashera_configured', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_cashera_recurrent_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_platega_recurrent_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_lava_recurrent_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
@pytest.mark.parametrize('surface', ['confirm', 'insufficient', 'daily_confirm', 'daily_insufficient'])
def test_managed_purchase_surfaces_have_no_cashera_button(cashera_enabled, category, surface):
    if surface == 'confirm':
        keyboard = tariff_purchase.get_tariff_confirm_keyboard(7, 30, 'ru', category=category)
    elif surface == 'insufficient':
        keyboard = tariff_purchase.get_tariff_insufficient_balance_keyboard(7, 30, 'ru', category=category)
    elif surface == 'daily_confirm':
        keyboard = tariff_purchase.get_daily_tariff_confirm_keyboard(7, 'ru', category=category)
    else:
        keyboard = tariff_purchase.get_daily_tariff_insufficient_balance_keyboard(7, 'ru', category=category)
    assert not any(action.startswith('tariff_cashera:') for action in callbacks(keyboard))


def test_legacy_confirm_keeps_cashera_button(cashera_enabled):
    assert 'tariff_cashera:7' in callbacks(tariff_purchase.get_tariff_confirm_keyboard(7, 30, 'ru'))


def handler_context(monkeypatch, category='wifi_lte', *, binding=None, category_on_tariff=False):
    callback = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    user = SimpleNamespace(id=1, language='ru')
    tariff = SimpleNamespace(id=7, is_daily=False, network_category=category)
    subscription = SimpleNamespace(
        id=10,
        tariff=tariff,
        is_trial=False,
        autopay_enabled=False,
        autopay_days_before=3,
        network_category=None if category_on_tariff else category,
    )
    db = AsyncMock()
    monkeypatch.setattr(autopay, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    monkeypatch.setattr(
        'app.database.crud.platega_subscription.get_active_platega_subscription_by_subscription',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        'app.services.cashera_recurring_cancel.get_cashera_recurring_status',
        AsyncMock(return_value=binding),
    )
    return callback, user, db


@pytest.mark.parametrize('category_on_tariff', [False, True])
async def test_managed_autopay_menu_has_no_cashera_enable(monkeypatch, cashera_enabled, category_on_tariff):
    callback, user, db = handler_context(monkeypatch, category_on_tariff=category_on_tariff)
    await autopay.handle_autopay_menu(callback, user, db)
    actions = callbacks(callback.message.edit_text.await_args.kwargs['reply_markup'])
    assert 'cashera_recurring_enable' not in actions
    assert 'cashera_recurring_cancel' not in actions


async def test_existing_managed_cashera_binding_stays_cancellable(monkeypatch, cashera_enabled):
    callback, user, db = handler_context(monkeypatch, binding={'status': 'ACTIVE'})
    await autopay.handle_autopay_menu(callback, user, db)
    actions = callbacks(callback.message.edit_text.await_args.kwargs['reply_markup'])
    assert 'cashera_recurring_cancel' in actions
    assert 'cashera_recurring_enable' not in actions


async def test_legacy_autopay_menu_keeps_cashera_enable(monkeypatch, cashera_enabled):
    callback, user, db = handler_context(monkeypatch, category=None)
    await autopay.handle_autopay_menu(callback, user, db)
    actions = callbacks(callback.message.edit_text.await_args.kwargs['reply_markup'])
    assert 'cashera_recurring_enable' in actions


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
@pytest.mark.parametrize('category_on_tariff', [False, True])
async def test_forged_managed_cashera_enable_never_reaches_payment_service(
    monkeypatch, cashera_enabled, category, category_on_tariff
):
    callback, user, db = handler_context(monkeypatch, category, category_on_tariff=category_on_tariff)
    enable = AsyncMock()
    monkeypatch.setattr('app.services.payment.cashera.enable_cashera_recurring', enable)
    await autopay.handle_cashera_recurring_enable(callback, user, db)
    enable.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    assert 'Wi-Fi/LTE' in callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs['show_alert'] is True
