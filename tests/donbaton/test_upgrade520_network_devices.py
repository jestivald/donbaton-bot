"""Upstream purchase selection must not sell slots managed by the LTE ledger."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes.subscription_modules import purchase
from app.cabinet.schemas.subscription import TariffPurchasePreviewRequest
from app.config import settings
from app.database.crud.subscription import extend_subscription
from app.database.models import User
from app.services.lte_quota_service import apply_panel_policy
from app.services.pricing_engine import PricingEngine, pricing_engine
from app.services.tariff_purchase_devices import DeviceSelectionError, resolve_purchase_device_selection
from app.services.tariff_purchase_quote import calculate_purchase_quote
from app.utils.subscription_utils import resolve_tariff_purchase_device_limit
from tests.donbaton.test_legacy_early_renewal import TABLES, locked_legacy
from tests.donbaton.test_lte_legacy import EPOCH
from tests.donbaton.test_lte_quota import LTE, WIFI
from tests.fixtures.sqlite_memory import memory_session
from tests.services.test_tariff_purchase_quote import make_tariff


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
@pytest.mark.parametrize('kind', ['new', 'renewal', 'trial', 'classic', 'switch'])
def test_network_selection_is_fixed_for_every_resolved_target(category, kind):
    tariff = make_tariff(network_category=category, device_limit=3, max_device_limit=10)
    target = (
        None
        if kind == 'new'
        else SimpleNamespace(
            tariff_id=7 if kind in ('renewal', 'trial') else None if kind == 'classic' else 8,
            network_category=category,
            device_limit=8,
            is_trial=kind == 'trial',
        )
    )
    assert resolve_tariff_purchase_device_limit(tariff, target) == 3
    devices = resolve_purchase_device_selection(tariff, target, None)
    assert devices.effective_limit == devices.included_limit == 3
    assert not devices.selectable
    assert devices.min_limit is devices.max_limit is None
    assert devices.reason == 'fixed_network_plan'
    # Saved carts and repeated checkout run through this same resolver.
    for selected in (3, 5, 8):
        with pytest.raises(DeviceSelectionError, match='device_selection_not_available'):
            resolve_purchase_device_selection(tariff, target, selected)
    if target is not None:
        assert target.device_limit == 8


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
def test_network_subscription_blocks_selection_even_with_ordinary_destination(category):
    target = SimpleNamespace(tariff_id=7, device_limit=8, is_trial=True, network_category=category)
    with pytest.raises(DeviceSelectionError, match='device_selection_not_available'):
        resolve_purchase_device_selection(make_tariff(), target, 5)


async def test_ordinary_tariffs_keep_upstream_device_prices_and_renewal_limit():
    tariff = make_tariff(device_limit=3, max_device_limit=10)
    quote = await calculate_purchase_quote(tariff, None, None, period_days=30, selected_device_limit=5)
    assert quote.devices.selectable and quote.devices.effective_limit == 5
    assert quote.pricing.devices_price == 20000
    target = SimpleNamespace(tariff_id=7, device_limit=8, is_trial=False)
    assert resolve_purchase_device_selection(tariff, target, None).effective_limit == 8


async def test_quote_passes_the_selected_subscription_to_pricing(monkeypatch):
    target = SimpleNamespace(tariff_id=7, device_limit=8, is_trial=False)
    validate = AsyncMock()
    monkeypatch.setattr(pricing_engine, '_validate_tariff_purchase_transition', validate)
    quote = await calculate_purchase_quote(
        make_tariff(network_category='wifi', device_limit=3, traffic_limit_gb=0, allowed_squads=[WIFI]),
        None,
        target,
        period_days=30,
    )
    assert validate.await_args.kwargs['subscription'] is target
    assert quote.devices.effective_limit == 3
    assert quote.pricing.devices_price == 0


@pytest.fixture(autouse=True)
def network_policy(monkeypatch):
    for name, value in {
        'DONBATON_LTE_ENABLED': True,
        'DONBATON_LTE_ACCOUNTING_READY': True,
        'DONBATON_LTE_ACCOUNTING_EPOCH': EPOCH,
        'DONBATON_WIFI_SQUAD_UUIDS': WIFI,
        'DONBATON_LTE_SQUAD_UUIDS': LTE,
        'DONBATON_TARIFF_SWITCH_ENABLED': True,
        'DONBATON_LEGACY_EARLY_RENEWAL_ENABLED': True,
        'MULTI_TARIFF_ENABLED': False,
        'SALES_MODE': 'tariffs',
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(User, 'get_primary_promo_group', lambda self: None)
    monkeypatch.setattr(PricingEngine, 'resolve_promo_group', staticmethod(lambda user: None))


@pytest.mark.parametrize('devices', [8, 0])
async def test_quote_and_purchase_preserve_paid_legacy_devices_until_boundary(monkeypatch, devices):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plans, boundary = await locked_legacy(db, devices=devices)
        plan = plans[9]
        quote = await calculate_purchase_quote(plan, await db.get(User, sub.user_id), sub, period_days=30)
        assert quote.devices.effective_limit == 3
        assert not quote.devices.selectable
        assert quote.pricing.devices_price == 0
        await extend_subscription(
            db,
            sub,
            30,
            tariff_id=plan.id,
            device_limit=quote.devices.effective_limit,
            traffic_limit_gb=0,
            connected_squads=plan.allowed_squads,
        )
        before = await apply_panel_policy(db, sub, {'status': 'ACTIVE'}, now=boundary - timedelta(seconds=1))
        assert before['hwid_device_limit'] == sub.device_limit == devices
        after = await apply_panel_policy(db, sub, {'status': 'ACTIVE'}, now=boundary + timedelta(seconds=1))
        assert after['hwid_device_limit'] == sub.device_limit == 3
        assert quota.legacy_state['original_paid_until'] == boundary.isoformat()


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
async def test_cabinet_rejects_network_target_before_quote_or_wallet(monkeypatch, category):
    from fastapi import HTTPException

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    target = SimpleNamespace(id=8, network_category=category)
    monkeypatch.setattr(purchase, 'get_subscription_by_id_for_user', AsyncMock(return_value=target))
    monkeypatch.setattr('app.services.cispay_recurrent_guard.require_no_cispay', AsyncMock())
    db = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await purchase._resolve_tariff_purchase_target(
            db,
            SimpleNamespace(id=1),
            make_tariff(),
            TariffPurchasePreviewRequest(tariff_id=7, subscription_id=8, period_days=30, selected_device_limit=5),
        )
    assert error.value.status_code == 409
    assert error.value.detail['code'] == 'device_selection_not_available'
    db.commit.assert_not_awaited()
