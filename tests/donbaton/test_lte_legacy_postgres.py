"""Real PostgreSQL locks/JSON: migration, samples and rollback preserve paid rights."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database.models import LteQuota, Subscription, TrafficPurchase
from app.services.lte_quota_service import apply_panel_policy, import_legacy_quota
from tests.donbaton.test_lte_legacy import BASELINE, EPOCH, GIB, IMPORT, TABLES, activate, legacy_seed
from tests.donbaton.test_lte_quota import LTE, NOW, WIFI, sample
from tests.fixtures.postgres_db import postgres_sessions, wait_for_lock_waiter


pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_EPOCH', EPOCH)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)


async def test_parallel_duplicate_import_keeps_one_original_snapshot(postgres_database):
    async with postgres_sessions(postgres_database, TABLES, count=3) as (first, second, watcher):
        sub, target, source, _ = await legacy_seed(first, grants=[(1, NOW + timedelta(days=30))])
        other = await second.get(Subscription, sub.id)
        locked = asyncio.Event()

        async def transfer(db, own_sub, hold):
            if not hold:
                await locked.wait()
            quota = await import_legacy_quota(db, own_sub, target, snapshot=source, **IMPORT, now=NOW)
            if hold:
                locked.set()
                await wait_for_lock_waiter(watcher)
            await db.commit()
            return quota.legacy_state['snapshot']

        snapshots = await asyncio.gather(transfer(first, sub, True), transfer(second, other, False))
        assert snapshots == [source, source]
        assert (await first.execute(select(func.count()).select_from(LteQuota))).scalar_one() == 1
        assert (await first.execute(select(func.count()).select_from(TrafficPurchase))).scalar_one() == 1


async def test_parallel_samples_refresh_legacy_json_instead_of_losing_or_doubling_usage(postgres_database):
    async with postgres_sessions(postgres_database, TABLES, count=3) as (first, second, watcher):
        initial_used = 17 * GIB - 34
        sub, target, source, _ = await legacy_seed(first, used_bytes=initial_used)
        quota = await activate(first, sub, target, source)
        other = await second.get(Subscription, sub.id)
        stale = await second.get(LteQuota, sub.id)
        assert stale.counter_bytes == BASELINE
        locked = asyncio.Event()

        async def record(db, own_sub, amount, hold):
            if not hold:
                await locked.wait()
            await apply_panel_policy(
                db,
                own_sub,
                {},
                panel_user=sample(BASELINE + amount),
                now=NOW + timedelta(seconds=1 if hold else 2),
            )
            if hold:
                locked.set()
                await wait_for_lock_waiter(watcher)
            await db.commit()

        await asyncio.gather(record(first, sub, 2, True), record(second, other, 3, False))
        await first.refresh(quota)
        assert quota.counter_bytes == BASELINE + 3
        assert quota.used_bytes == 3
        assert quota.legacy_state['runtime']['used_units'] == initial_used + 51
        assert quota.legacy_state['snapshot'] == source


async def test_import_rollback_leaves_all_old_subscription_and_grant_fields(postgres_database):
    async with postgres_sessions(postgres_database, TABLES, count=2) as (first, second):
        sub, target, source, _ = await legacy_seed(first, grants=[(1, NOW + timedelta(days=30))])
        await import_legacy_quota(first, sub, target, snapshot=source, **IMPORT, now=NOW)
        await first.rollback()
        saved = await second.get(Subscription, 1)
        assert saved.tariff_id == source['subscription']['tariff_id']
        assert saved.network_category is None
        assert saved.traffic_limit_gb == source['subscription']['traffic_limit_gb']
        assert saved.purchased_traffic_gb == source['subscription']['purchased_traffic_gb']
        assert saved.end_date.isoformat() == source['subscription']['end_date']
        assert (await second.execute(select(func.count()).select_from(LteQuota))).scalar_one() == 0
        assert (await second.execute(select(func.count()).select_from(TrafficPurchase))).scalar_one() == 1
