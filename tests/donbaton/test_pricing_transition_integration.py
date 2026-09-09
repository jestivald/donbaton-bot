"""Real quote lookups reject legacy grant loss before a price reaches debit."""

from datetime import timedelta

import pytest

from app.config import settings
from app.database.models import User
from app.services.lte_quota_service import LtePolicyError
from app.services.pricing_engine import PricingEngine
from tests.donbaton.test_lte_legacy import EPOCH, TABLES, activate, legacy_seed
from tests.donbaton.test_lte_quota import LTE, NOW, WIFI
from tests.fixtures.sqlite_memory import memory_session


@pytest.fixture(autouse=True)
def accounting_config(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_EPOCH', EPOCH)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    # The pricing integration exercises actual DB entitlement lookups. Promo
    # relations are independent and are not needed by these synthetic users.
    monkeypatch.setattr(PricingEngine, 'resolve_promo_group', staticmethod(lambda user: None))


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_context', [False, True])
async def test_expired_legacy_with_valid_grants_has_no_purchase_quote(explicit_context, monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, _, grants = await legacy_seed(
            db, status='expired', paid_until=NOW - timedelta(days=1), grants=[(500, NOW + timedelta(days=10))]
        )
        user = await db.get(User, sub.user_id)
        user.balance_kopeks = 100000
        await db.commit()
        kwargs = dict(db=db, subscription=sub) if explicit_context else {}
        with pytest.raises(LtePolicyError, match='перенести действующие докупки'):
            await PricingEngine().calculate_tariff_purchase_price(target, 30, user=user, **kwargs)
        assert user.balance_kopeks == 100000
        assert sub.tariff_id == 1 and sub.network_category is None
        assert grants[0].traffic_gb == 500


@pytest.mark.asyncio
async def test_expired_legacy_without_live_grants_can_quote_new_paid_term(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, _, _ = await legacy_seed(
            db, status='expired', paid_until=NOW - timedelta(days=1), grants=[(500, NOW - timedelta(hours=1))]
        )
        user = await db.get(User, sub.user_id)
        quote = await PricingEngine().calculate_tariff_purchase_price(target, 30, user=user)
        assert quote.final_total == 29900
        assert sub.tariff_id == 1 and sub.network_category is None


@pytest.mark.asyncio
async def test_imported_custom_early_renewal_cannot_quote_through_purchase_entry(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, custom=True, devices=15)
        await activate(db, sub, target, snapshot)
        user = await db.get(User, sub.user_id)
        with pytest.raises(LtePolicyError, match='индивидуальные условия'):
            await PricingEngine().calculate_tariff_purchase_price(target, 30, user=user)
        assert sub.device_limit == 15


@pytest.mark.asyncio
async def test_renewal_missing_managed_marker_still_checks_old_purchase_rows(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, _, _ = await legacy_seed(
            db, status='expired', paid_until=NOW - timedelta(days=1), grants=[(500, NOW + timedelta(days=10))]
        )
        sub.tariff = target
        sub.tariff_id = target.id
        await db.commit()
        with pytest.raises(LtePolicyError, match='перенести действующие докупки'):
            await PricingEngine().calculate_renewal_price(db, sub, 30)


@pytest.mark.asyncio
async def test_detached_buyer_cannot_bypass_database_entitlement_check(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, _, _ = await legacy_seed(db, paid_until=NOW - timedelta(days=1))
        user = await db.get(User, sub.user_id)
        db.expunge(user)
        db.expunge(target)
        with pytest.raises(LtePolicyError, match='проверить оплаченные LTE-пакеты'):
            await PricingEngine().calculate_tariff_purchase_price(target, 30, user=user)
