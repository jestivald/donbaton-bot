"""The upstream ledger must never own Wi-Fi/LTE quota or ordinary top-ups."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models import Subscription, Tariff, TrafficAccountingState, TrafficPurchase, User
from app.services import traffic_accounting_service as accounting, traffic_purchase_service as purchases
from app.services.traffic_accounting import AccountingConflict, TrafficAccount
from app.services.traffic_accounting_delivery import deliver_account_projection
from app.services.traffic_accounting_recreation import ensure_account_recreated
from app.services.traffic_accounting_store import initialize_account
from app.services.traffic_accounting_worker import TrafficAccountingWorker
from tests.services.test_traffic_accounting_service import NOW, PanelDouble
from tests.services.test_traffic_accounting_store import database


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
@pytest.mark.parametrize('category_source', ['subscription', 'tariff'])
@pytest.mark.parametrize('limit', [0, 100])
async def test_regular_topup_refuses_before_any_financial_or_panel_change(
    monkeypatch, category, category_source, limit
):
    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        user = await db.get(User, 1)
        user.balance_kopeks = 12345
        sub.traffic_limit_gb = limit
        if category_source == 'subscription':
            sub.network_category = category
        else:
            db.add(Tariff(id=9, name='Network', network_category=category))
            sub.tariff_id = 9
        await db.commit()
        migrate = AsyncMock()
        monkeypatch.setattr(accounting, 'ensure_managed_account', migrate)
        with pytest.raises(AccountingConflict, match='Для LTE используйте отдельную покупку пакета'):
            await purchases.prepare_traffic_purchase(db, sub, requested_gb=10)
        with pytest.raises(AccountingConflict, match='Для LTE используйте отдельную покупку пакета'):
            await purchases.add_subscription_traffic(db, sub, 10)
        migrate.assert_not_awaited()
        assert user.balance_kopeks == 12345
        assert sub.traffic_limit_gb == limit
        assert await db.scalar(select(func.count()).select_from(TrafficPurchase)) == 0
        assert await db.get(TrafficAccountingState, 1) is None


async def test_topup_rechecks_category_after_row_lock(monkeypatch):
    sub = SimpleNamespace(
        network_category=None,
        tariff_id=None,
        traffic_limit_gb=100,
        traffic_accounting_mode='legacy',
        add_traffic=Mock(),
    )
    db = SimpleNamespace(refresh=AsyncMock(), add=Mock(), commit=AsyncMock())

    async def lock(db, subscription):
        subscription.network_category = 'wifi_lte'

    monkeypatch.setattr(purchases, '_lock_subscription_row', lock)
    with pytest.raises(AccountingConflict, match='Для LTE используйте отдельную покупку пакета'):
        await purchases.add_subscription_traffic(db, sub, 10)
    sub.add_traffic.assert_not_called()
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
@pytest.mark.parametrize('phase', [None, 'prepared', 'ready', 'recreating'])
async def test_network_accounting_entrypoints_never_write_panel_even_with_existing_fence(monkeypatch, category, phase):
    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.network_category = category
        sub.traffic_limit_gb = 100
        sub.remnawave_id = 123
        if phase:
            state = await initialize_account(
                db,
                1,
                TrafficAccount('masked', 0, 0, ()),
                strategy='MONTH',
                timezone='UTC',
                anchor=NOW,
                next_refresh_at=None,
            )
            state.phase = phase
            sub.traffic_accounting_mode = 'managed' if phase == 'ready' else 'migrating'
        await db.commit()
        factory = async_sessionmaker(db.bind, expire_on_commit=False)
        panel = PanelDouble()
        for operation in (accounting.ensure_managed_account, ensure_account_recreated, deliver_account_projection):
            with pytest.raises(AccountingConflict):
                await operation(factory, panel, 1, now=NOW)
        assert panel.writes == []
        if phase is None:
            assert await db.get(TrafficAccountingState, 1) is None
        else:
            await db.refresh(state)
            assert state.phase == phase


@pytest.mark.parametrize('category_source', ['subscription', 'tariff'])
@pytest.mark.parametrize('phase', [None, 'prepared', 'ready', 'recreating'])
async def test_worker_excludes_network_candidates_including_stale_categories(monkeypatch, category_source, phase):
    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.traffic_limit_gb = 100
        sub.end_date = NOW + timedelta(days=30)
        if category_source == 'subscription':
            sub.network_category = 'wifi_lte'
        else:
            db.add(Tariff(id=9, name='Network', network_category='wifi'))
            sub.tariff_id = 9
        db.add(TrafficPurchase(subscription_id=1, traffic_gb=10, expires_at=NOW + timedelta(days=10)))
        if phase:
            state = await initialize_account(
                db,
                1,
                TrafficAccount('masked', 0, 0, ()),
                strategy='MONTH',
                timezone='UTC',
                anchor=NOW,
                next_refresh_at=None,
            )
            state.phase = phase
        await db.commit()
        worker = TrafficAccountingWorker(async_sessionmaker(db.bind, expire_on_commit=False))
        process = AsyncMock()
        monkeypatch.setattr(worker, '_process_candidate', process)
        assert await worker.run_once(now=NOW) == 0
        process.assert_not_awaited()


async def test_worker_keeps_ordinary_finite_package_candidate(monkeypatch):
    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.traffic_limit_gb = 100
        sub.end_date = NOW + timedelta(days=30)
        db.add(TrafficPurchase(subscription_id=1, traffic_gb=10, expires_at=NOW + timedelta(days=10)))
        await db.commit()
        worker = TrafficAccountingWorker(async_sessionmaker(db.bind, expire_on_commit=False))
        process = AsyncMock()
        monkeypatch.setattr(worker, '_process_candidate', process)
        assert await worker.run_once(now=NOW) == 1
        process.assert_awaited_once_with(1, 'legacy', now=NOW)


async def test_prepare_rechecks_network_category_after_accounting_lease(monkeypatch):
    from contextlib import asynccontextmanager

    sub = SimpleNamespace(
        id=1,
        user_id=1,
        network_category=None,
        tariff_id=None,
        traffic_limit_gb=100,
        purchased_traffic_gb=0,
        end_date=NOW,
        status='active',
        is_trial=False,
    )
    locked = SimpleNamespace(**vars(sub))
    locked.network_category = 'wifi_lte'
    db = SimpleNamespace(bind=None)

    @asynccontextmanager
    async def client():
        yield object()

    monkeypatch.setattr(purchases, 'SubscriptionService', lambda: SimpleNamespace(get_api_client=client))
    monkeypatch.setattr(accounting, 'ensure_managed_account', AsyncMock())
    monkeypatch.setattr(accounting, 'lock_accounting_subscription', AsyncMock(return_value=(locked, False)))
    fresh = AsyncMock()
    monkeypatch.setattr(accounting, '_fresh_panel', fresh)
    with pytest.raises(AccountingConflict, match='Для LTE используйте отдельную покупку пакета'):
        await purchases.prepare_traffic_purchase(db, sub)
    fresh.assert_not_awaited()
