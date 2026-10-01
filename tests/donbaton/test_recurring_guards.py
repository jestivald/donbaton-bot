"""Old callbacks and direct APIs cannot create external recurring Wi-Fi/LTE payments."""

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.config import settings


PROVIDERS = [
    (
        'platega',
        'purchase_tariff_with_sbp_recurring',
        'enable_platega_sbp_recurring',
        'PlategaPaymentMixin',
        'create_platega_sbp_subscription',
        '_PlategaSbpAgent',
    ),
    (
        'lava',
        'purchase_tariff_with_lava_recurring',
        'enable_lava_recurring',
        'LavaPaymentMixin',
        'create_lava_recurrent_subscription',
        '_LavaRecurrentAgent',
    ),
    (
        'cashera',
        'purchase_tariff_with_cashera_recurring',
        'enable_cashera_recurring',
        'CasheraPaymentMixin',
        'create_cashera_recurrent_subscription',
        '_CasheraRecurrentAgent',
    ),
]


@pytest.fixture(autouse=True)
def recurrent_enabled(monkeypatch):
    # The managed guard must work even when every provider integration is on.
    monkeypatch.setattr(type(settings), 'is_platega_recurrent_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_lava_recurrent_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_cashera_recurrent_enabled', lambda self: True)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,purchase,enable,mixin,create,agent', PROVIDERS)
@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
@pytest.mark.parametrize('managed_enabled', [False, True])
async def test_purchase_rejects_managed_before_pending_row_or_provider(
    monkeypatch,
    provider,
    purchase,
    enable,
    mixin,
    create,
    agent,
    category,
    managed_enabled,
):
    module = import_module(f'app.services.payment.{provider}')
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', managed_enabled)
    stub = AsyncMock()
    provider_factory = MagicMock()
    monkeypatch.setattr('app.database.crud.subscription.create_sbp_pending_subscription', stub)
    monkeypatch.setattr(module, agent, provider_factory)
    db = MagicMock()
    plan = SimpleNamespace(id=7, network_category=category, is_active=True)
    with pytest.raises(ValueError, match='оплачиваются с баланса'):
        await getattr(module, purchase)(db, user=SimpleNamespace(id=1), tariff=plan)
    assert db.mock_calls == []
    stub.assert_not_awaited()
    provider_factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,purchase,enable,mixin,create,agent', PROVIDERS)
@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
@pytest.mark.parametrize('marker_on_tariff', [False, True])
async def test_enable_and_direct_mixin_reject_both_entitlement_sources(
    monkeypatch,
    provider,
    purchase,
    enable,
    mixin,
    create,
    agent,
    category,
    marker_on_tariff,
):
    module = import_module(f'app.services.payment.{provider}')
    plan = SimpleNamespace(id=7, network_category=category if marker_on_tariff else None)
    subscription = SimpleNamespace(id=1, network_category=None if marker_on_tariff else category, autopay_enabled=True)
    db = MagicMock()
    provider_factory = MagicMock()
    monkeypatch.setattr(module, agent, provider_factory)
    # Enable rejects before constructing the provider, even without the local
    # subscription marker when the explicitly loaded tariff is already managed.
    with pytest.raises(ValueError, match='внешняя автооплата пока недоступна'):
        await getattr(module, enable)(db, user_id=1, subscription=subscription, tariff=plan)
    provider_factory.assert_not_called()
    # Full PaymentService/mixin callers also have the same earliest boundary.
    with pytest.raises(ValueError, match='внешняя автооплата пока недоступна'):
        await getattr(getattr(module, mixin)(), create)(db, user_id=1, subscription=subscription, tariff=plan)
    assert db.mock_calls == []
    assert subscription.autopay_enabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,purchase,enable,mixin,create,agent', PROVIDERS)
async def test_legacy_enable_keeps_existing_provider_contract(
    monkeypatch, provider, purchase, enable, mixin, create, agent
):
    module = import_module(f'app.services.payment.{provider}')
    operation = AsyncMock(return_value={'status': 'PENDING', 'redirect_url': 'https://payments.example/confirm'})
    factory = MagicMock(return_value=SimpleNamespace(**{create: operation}))
    monkeypatch.setattr(module, agent, factory)
    sub = SimpleNamespace(id=1, network_category=None)
    plan = SimpleNamespace(id=7, network_category=None)
    result = await getattr(module, enable)(None, user_id=1, subscription=sub, tariff=plan)
    assert result['status'] == 'PENDING'
    operation.assert_awaited_once_with(None, user_id=1, subscription=sub, tariff=plan)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'callback_prefix,handler_name',
    [
        ('tariff_sbp', 'purchase_tariff_with_sbp'),
        ('tariff_lava', 'purchase_tariff_with_lava'),
        ('tariff_cashera', 'purchase_tariff_with_cashera'),
    ],
)
async def test_old_telegram_callback_gets_clear_rejection(monkeypatch, callback_prefix, handler_name):
    from app.handlers.subscription import tariff_purchase

    # Explicitly simulate a stale button resolving a managed tariff via raw lookup.
    plan = SimpleNamespace(id=7, is_active=True, network_category='wifi_lte')
    monkeypatch.setattr(tariff_purchase, 'get_tariff_by_id', AsyncMock(return_value=plan))
    callback = SimpleNamespace(data=f'{callback_prefix}:7', answer=AsyncMock())
    await getattr(tariff_purchase, handler_name)(callback, SimpleNamespace(id=1, language='ru'), None)
    assert 'оплачиваются с баланса' in callback.answer.call_args.args[0]
    assert callback.answer.call_args.kwargs['show_alert'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['platega', 'lava', 'cashera'])
async def test_raw_cabinet_purchase_rejects_disabled_managed_sales(monkeypatch, provider):
    module = import_module(f'app.cabinet.routes.subscription_modules.{provider}_recurrent')
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', False)
    plan = SimpleNamespace(id=7, is_active=True, network_category='wifi')
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=plan))
    with pytest.raises(HTTPException) as failure:
        await getattr(module, f'purchase_with_{provider}_recurrent')(tariff_id=7, user=SimpleNamespace(id=1), db=None)
    assert failure.value.status_code == 400
    assert 'оплачиваются с баланса' in failure.value.detail
