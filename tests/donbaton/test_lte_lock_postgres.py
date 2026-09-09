"""FOR UPDATE must serialize duplicate credits and cumulative samples on PostgreSQL."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database.models import LteQuota, LteTopup, Subscription
from app.services.lte_quota_service import apply_panel_policy, credit_topup
from tests.donbaton.test_lte_quota import LTE, NOW, TABLES, WIFI, sample, seed
from tests.fixtures.postgres_db import postgres_sessions, wait_for_lock_waiter


pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def configure_accounting(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)


async def test_parallel_duplicate_purchase_has_one_durable_bucket(postgres_database):
    async with postgres_sessions(postgres_database, TABLES, count=3) as (first, second, watcher):
        sub, _, _ = await seed(first)
        other = await second.get(Subscription, sub.id)
        locked = asyncio.Event()

        async def credit(db, own_sub, hold):
            if not hold:
                await locked.wait()
            result = await credit_topup(db, own_sub, bytes_amount=40, operation_id='parallel:credit', now=NOW)
            if hold:
                locked.set()
                await wait_for_lock_waiter(watcher)
            await db.commit()
            return result.id

        ids = await asyncio.gather(credit(first, sub, True), credit(second, other, False))
        assert ids[0] == ids[1]
        assert (await first.execute(select(func.count()).select_from(LteTopup))).scalar_one() == 1
        assert (await first.execute(select(func.sum(LteTopup.amount_bytes)))).scalar_one() == 40


async def test_parallel_samples_use_fresh_locked_counter_without_double_charge(postgres_database):
    async with postgres_sessions(postgres_database, TABLES, count=3) as (first, second, watcher):
        sub, quota, _ = await seed(first)
        await apply_panel_policy(first, sub, {}, panel_user=sample(0), now=NOW)
        await first.commit()
        other = await second.get(Subscription, sub.id)
        # Both sessions have read baseline zero before the first one takes lock.
        stale = await second.get(LteQuota, sub.id)
        assert stale.counter_bytes == quota.counter_bytes == 0
        locked = asyncio.Event()

        async def observe(db, own_sub, lifetime, hold):
            if not hold:
                await locked.wait()
            payload = await apply_panel_policy(
                db,
                own_sub,
                {},
                panel_user=sample(lifetime),
                now=NOW + timedelta(seconds=1 if hold else 2),
            )
            if hold:
                locked.set()
                await wait_for_lock_waiter(watcher)
            await db.commit()
            return payload['active_internal_squads']

        rights = await asyncio.gather(observe(first, sub, 100, True), observe(second, other, 120, False))
        assert rights == [[WIFI], [WIFI]]
        await first.refresh(quota)
        assert quota.counter_bytes == 120
        assert quota.used_bytes == 120
        assert quota.base_used_bytes == 100


async def test_rolled_back_payment_does_not_leave_paid_lte(postgres_database):
    async with postgres_sessions(postgres_database, TABLES, count=2) as (first, second):
        sub, _, _ = await seed(first)
        await credit_topup(first, sub, bytes_amount=40, operation_id='rollback:credit', now=NOW)
        await first.rollback()
        assert (await second.execute(select(func.count()).select_from(LteTopup))).scalar_one() == 0
        own_sub = await second.get(Subscription, 1)
        retry = await credit_topup(second, own_sub, bytes_amount=40, operation_id='rollback:credit', now=NOW)
        await second.commit()
        assert retry.amount_bytes == 40
