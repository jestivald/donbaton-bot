"""Old renewal links must never turn an absent new period into free access."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.config import settings
from app.database.crud.subscription import _apply_base_limit_preserving_active_purchases, _housekeep_expired_purchases
from app.database.models import Subscription, Tariff, TrafficPurchase, User
from app.handlers.subscription.lte import format_lte_status
from app.services.daily_subscription_service import DailySubscriptionService
from app.services.pricing_engine import PricingEngine
from tests.fixtures.sqlite_memory import memory_session


WIFI = '00000000-0000-4000-8000-000000000001'
LTE = '00000000-0000-4000-8000-000000000002'


@pytest.fixture
def launch_tariff(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    return Tariff(
        id=9,
        name='Личный',
        network_category='wifi_lte',
        traffic_limit_gb=0,
        lte_traffic_limit_gb=60,
        lte_period_days=30,
        allowed_squads=[WIFI, LTE],
        period_prices={'30': 29900},
        device_limit=3,
        is_daily=False,
        is_trial_available=False,
        show_in_gift=False,
        custom_days_enabled=False,
        custom_traffic_enabled=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('period', [1, 7, 90, 180, 360, 3650, True, 30.0, '30', 0, -1])
async def test_unlisted_or_malformed_period_has_no_price(launch_tariff, period):
    with pytest.raises(ValueError, match='срок отсутствует'):
        await PricingEngine().calculate_tariff_purchase_price(launch_tariff, period)


@pytest.mark.asyncio
async def test_approved_thirty_days_keeps_approved_price(launch_tariff):
    quote = await PricingEngine().calculate_tariff_purchase_price(launch_tariff, 30)
    assert quote.final_total == 29900
    assert quote.period_days == 30


@pytest.mark.asyncio
@pytest.mark.parametrize('price', [None, 0, -1, True, 29900.5, '29900'])
async def test_bad_stored_price_cannot_issue_a_free_period(launch_tariff, price):
    launch_tariff.period_prices = {'30': price}
    with pytest.raises(ValueError, match='срок отсутствует'):
        await PricingEngine().calculate_tariff_purchase_price(launch_tariff, 30)


@pytest.mark.asyncio
async def test_migrated_subscription_without_loaded_tariff_cannot_use_classic_pricing():
    subscription = SimpleNamespace(id=1, tariff_id=9, tariff=None, network_category='wifi_lte')
    with pytest.raises(ValueError, match='Условия тарифа не загружены'):
        await PricingEngine().calculate_renewal_price(None, subscription, 30)


@pytest.mark.asyncio
@pytest.mark.parametrize('stale_category', [False, True])
async def test_generic_housekeeping_preserves_imported_purchase_archive(stale_category, monkeypatch):
    tables = (User.__table__, Tariff.__table__, Subscription.__table__, TrafficPurchase.__table__)
    async with memory_session(monkeypatch, tables) as db:
        now = datetime.now(UTC)
        user = User(id=1, telegram_id=990000001, balance_kopeks=12345)
        sub = Subscription(
            id=1,
            user_id=1,
            status='active',
            network_category='wifi_lte',
            end_date=now + timedelta(days=45),
            traffic_limit_gb=0,
            purchased_traffic_gb=0,
            device_limit=3,
            connected_squads=[],
        )
        db.add_all([user, sub])
        await db.flush()
        db.add_all(
            [
                TrafficPurchase(
                    id=1,
                    subscription_id=1,
                    traffic_gb=500,
                    created_at=now - timedelta(days=5),
                    expires_at=now + timedelta(days=330),
                ),
                TrafficPurchase(
                    id=2,
                    subscription_id=1,
                    traffic_gb=20,
                    created_at=now - timedelta(days=40),
                    expires_at=now - timedelta(days=10),
                ),
            ]
        )
        await db.commit()
        if stale_category:
            sub.network_category = None
            await db.flush()
            await db.execute(
                update(Subscription)
                .where(Subscription.id == 1)
                .values(network_category='wifi_lte')
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            assert sub.network_category is None
        assert await _housekeep_expired_purchases(db, sub, now=now) == 0
        assert await _apply_base_limit_preserving_active_purchases(db, sub, 0, now=now) == (0, 0)
        await db.commit()
        purchases = list((await db.execute(select(TrafficPurchase).order_by(TrafficPurchase.id))).scalars())
        assert [(item.id, item.traffic_gb) for item in purchases] == [(1, 500), (2, 20)]
        assert purchases[0].expires_at == now + timedelta(days=330)
        assert sub.end_date == now + timedelta(days=45)
        assert user.balance_kopeks == 12345
        # The scheduled worker rechecks the managed category under its row lock.
        await DailySubscriptionService()._reset_subscription_traffic(db, sub.id, [purchases[1]])
        await db.commit()
        assert list((await db.execute(select(TrafficPurchase.id).order_by(TrafficPurchase.id))).scalars()) == [1, 2]
        assert sub.traffic_limit_gb == 0


def test_legacy_no_reset_text_does_not_promise_an_unpaid_refill(monkeypatch):
    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Moscow')
    quota = dict(
        remaining_bytes=1024**3,
        period_ends_at='2026-10-01T12:00:00+00:00',
        legacy_compatibility_until='2026-10-01T12:00:00+00:00',
        period_mode='legacy_no_reset',
        accounting_state='ready',
        exhausted=False,
    )
    text = format_lte_status('wifi_lte', quota)
    assert 'осталось 1.00 ГиБ' in text
    assert 'Прежние оплаченные условия действуют до 01.10.2026 15:00 MSK' in text
    assert 'Обновление пакета' not in text
    quota['is_unlimited'] = True
    quota['remaining_bytes'] = 0
    text = format_lte_status('wifi_lte', quota)
    assert 'LTE: безлимит' in text
    assert 'осталось' not in text
    assert 'закончился' not in text
