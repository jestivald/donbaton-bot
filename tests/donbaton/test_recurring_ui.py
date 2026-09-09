import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.handlers.subscription import autopay, tariff_purchase


def callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


@pytest.fixture
def recurring_enabled(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_platega_recurrent_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_lava_recurrent_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
@pytest.mark.parametrize('surface', ['confirm', 'insufficient', 'daily_confirm', 'daily_insufficient'])
def test_managed_purchase_surfaces_exclude_external_authorization(recurring_enabled, category, surface):
    if surface == 'confirm':
        keyboard = tariff_purchase.get_tariff_confirm_keyboard(7, 30, 'ru', category=category)
        expected = 'tariff_confirm:7:30'
    elif surface == 'insufficient':
        keyboard = tariff_purchase.get_tariff_insufficient_balance_keyboard(7, 30, 'ru', category=category)
        expected = 'balance_topup'
    elif surface == 'daily_confirm':
        keyboard = tariff_purchase.get_daily_tariff_confirm_keyboard(7, 'ru', category=category)
        expected = 'daily_tariff_confirm:7'
    else:
        keyboard = tariff_purchase.get_daily_tariff_insufficient_balance_keyboard(7, 'ru', category=category)
        expected = 'balance_topup'
    actions = callbacks(keyboard)
    assert expected in actions
    assert not any(action.startswith(('tariff_sbp:', 'tariff_lava:')) for action in actions)


def test_managed_direct_balance_topup_remains_available_without_recurring(monkeypatch, recurring_enabled):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)
    actions = callbacks(
        tariff_purchase.get_tariff_insufficient_balance_keyboard(
            7,
            30,
            'ru',
            missing_kopeks=12300,
            category='wifi_lte',
        )
    )
    assert 'topup_amount|stars|12300' in actions
    assert 'tariff_sbp:7' not in actions
    assert 'tariff_lava:7' not in actions


def test_legacy_default_preserves_both_recurring_providers(recurring_enabled):
    actions = callbacks(tariff_purchase.get_tariff_confirm_keyboard(7, 30, 'ru'))
    assert 'tariff_sbp:7' in actions
    assert 'tariff_lava:7' in actions


def test_all_checkout_and_cart_restore_calls_supply_explicit_category():
    """An omitted call-site argument silently falls back to legacy provider buttons."""
    guarded = {
        'get_tariff_confirm_keyboard',
        'get_tariff_insufficient_balance_keyboard',
        'get_daily_tariff_confirm_keyboard',
        'get_daily_tariff_insufficient_balance_keyboard',
    }
    tree = ast.parse(Path(tariff_purchase.__file__).read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in guarded
    ]
    assert calls
    assert all(any(keyword.arg == 'category' for keyword in call.keywords) for call in calls)


def handler_context(monkeypatch, category='wifi_lte', *, record=None, category_on_tariff=False):
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
        AsyncMock(return_value=record),
    )
    return callback, user, db


@pytest.mark.parametrize('category_on_tariff', [False, True])
async def test_managed_autopay_menu_has_no_sbp_entry_without_existing_binding(
    monkeypatch,
    recurring_enabled,
    category_on_tariff,
):
    callback, user, db = handler_context(monkeypatch, category_on_tariff=category_on_tariff)
    await autopay.handle_autopay_menu(callback, user, db)
    actions = callbacks(callback.message.edit_text.await_args.kwargs['reply_markup'])
    assert 'sbp_recurring_menu' not in actions
    assert 'sbp_recurring_enable' not in actions
    assert 'sbp_recurring_cancel' not in actions


@pytest.mark.parametrize('enabled', [True, False])
async def test_existing_managed_binding_can_be_cancelled_even_if_feature_disabled(monkeypatch, enabled):
    monkeypatch.setattr(type(settings), 'is_platega_recurrent_enabled', lambda self: enabled)
    callback, user, db = handler_context(monkeypatch, record=SimpleNamespace(status='ACTIVE', next_charge_at=None))
    await autopay.handle_autopay_menu(callback, user, db)
    actions = callbacks(callback.message.edit_text.await_args.kwargs['reply_markup'])
    assert 'sbp_recurring_cancel' in actions
    assert 'sbp_recurring_menu' not in actions
    assert 'sbp_recurring_enable' not in actions
    cancel = AsyncMock()
    menu = AsyncMock()
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', cancel)
    monkeypatch.setattr(autopay, 'handle_autopay_menu', menu)
    await autopay.handle_sbp_recurring_cancel(callback, user, db)
    cancel.assert_awaited_once_with(db, 10)
    menu.assert_awaited_once()


async def test_direct_managed_menu_does_not_offer_creation_but_keeps_existing_cancel(monkeypatch, recurring_enabled):
    callback, user, db = handler_context(monkeypatch)
    await autopay.handle_sbp_recurring_menu(callback, user, db)
    callback.message.edit_text.assert_not_awaited()
    assert 'Wi-Fi/LTE' in callback.answer.await_args.args[0]
    callback, user, db = handler_context(monkeypatch, record=SimpleNamespace(status='ACTIVE', next_charge_at=None))
    await autopay.handle_sbp_recurring_menu(callback, user, db)
    actions = callbacks(callback.message.edit_text.await_args.kwargs['reply_markup'])
    assert 'sbp_recurring_cancel' in actions
    assert 'sbp_recurring_enable' not in actions


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
async def test_forged_managed_enable_callback_never_reaches_payment_service(monkeypatch, recurring_enabled, category):
    callback, user, db = handler_context(monkeypatch, category)
    enable = AsyncMock()
    monkeypatch.setattr('app.services.payment.platega.enable_platega_sbp_recurring', enable)
    await autopay.handle_sbp_recurring_enable(callback, user, db)
    enable.assert_not_awaited()
    assert 'Wi-Fi/LTE' in callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs['show_alert'] is True


@pytest.mark.parametrize(
    'error', [ValueError('Выберите доступный период продления'), RuntimeError('provider-internal-detail')]
)
async def test_enable_explains_business_rejection_and_keeps_provider_failure_generic(
    monkeypatch, recurring_enabled, error
):
    callback, user, db = handler_context(monkeypatch, None)
    monkeypatch.setattr('app.services.payment.platega.enable_platega_sbp_recurring', AsyncMock(side_effect=error))
    await autopay.handle_sbp_recurring_enable(callback, user, db)
    message = callback.answer.await_args.args[0]
    if isinstance(error, ValueError):
        assert message == str(error)
    else:
        assert str(error) not in message
        assert 'Попробуйте позже' in message
