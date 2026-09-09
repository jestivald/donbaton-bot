"""Pending invoice retries and both paid activation paths preserve one trial."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database.crud import subscription as crud
from app.database.models import LteQuota, Subscription, Tariff, User
from app.services.lte_accounting_service import ceil_units
from app.services.lte_quota_service import GIB, LtePolicyError, apply_panel_policy
from tests.donbaton.test_lte_quota import LTE, NOW, TABLES, WIFI, sample
from tests.fixtures.sqlite_memory import memory_session


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=NOW)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz is not None else clock.now.replace(tzinfo=None)

    monkeypatch.setattr(crud, 'datetime', FrozenDateTime)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    return clock


async def seed(db):
    db.add_all(
        [
            User(id=1, telegram_id=90123456, first_name='Trial', status='active'),
            Tariff(id=3, name='Trial', traffic_limit_gb=20, device_limit=1, is_trial_available=True),
        ]
    )
    await db.commit()


async def pending(db, **overrides):
    params = dict(
        user_id=1,
        duration_days=3,
        traffic_limit_gb=20,
        device_limit=1,
        connected_squads=['old-squad'],
        tariff_id=3,
    )
    params.update(overrides)
    return await crud.create_pending_trial_subscription(db, **params)


@pytest.mark.asyncio
async def test_new_pending_and_retry_preserve_original_offer_and_close_access(monkeypatch, clock):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        sub = await pending(db)
        quota = await db.get(LteQuota, sub.id)
        assert quota.limit_bytes == ceil_units(20 * GIB, 17)
        assert quota.period_ends_at - quota.period_starts_at == timedelta(days=3)
        assert quota.trial_ends_at == sub.end_date == NOW + timedelta(days=3)
        assert sub.status == 'pending' and sub.is_trial
        assert sub.network_category == 'wifi_lte' and sub.traffic_limit_gb == 0
        assert sub.connected_squads == [WIFI, LTE]
        assert quota.legacy_state is None and quota.counter_bytes is None
        projection = await apply_panel_policy(db, sub, {}, panel_user=sample(100), now=NOW)
        assert projection['active_internal_squads'] == []
        assert quota.counter_bytes is None

        clock.now += timedelta(days=4)
        retry = await pending(db, duration_days=30, traffic_limit_gb=200, device_limit=7)
        await db.refresh(quota)
        assert retry.id == sub.id
        assert retry.start_date == NOW and retry.end_date == NOW + timedelta(days=3)
        assert retry.device_limit == 1 and retry.connected_squads == [WIFI, LTE]
        assert quota.limit_bytes == ceil_units(20 * GIB, 17)
        assert quota.period_starts_at == NOW and quota.counter_bytes is None
        assert await db.scalar(select(func.count()).select_from(Subscription)) == 1
        assert await db.scalar(select(func.count()).select_from(LteQuota)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('dedicated', [False, True])
@pytest.mark.parametrize('delay_days', [1, 5])
async def test_paid_pending_activation_anchors_full_saved_duration_once(dedicated, delay_days, monkeypatch, clock):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        sub = await pending(db)
        quota = await db.get(LteQuota, sub.id)
        clock.now += timedelta(days=delay_days)

        async def activate():
            if dedicated:
                return await crud.activate_pending_trial_subscription(db, sub.id, sub.user_id)
            # An old callback's generic period must not replace the saved trial.
            return await crud.activate_pending_subscription(db, sub.user_id, 30, subscription_id=sub.id)

        result = await activate()
        await db.refresh(quota)
        assert result.status == 'trial' and result.is_trial
        assert result.start_date == quota.period_starts_at == clock.now
        expected_end = clock.now + timedelta(days=3)
        assert result.end_date == quota.period_ends_at == quota.trial_ends_at == expected_end
        assert quota.counter_bytes is None
        assert quota.limit_bytes == ceil_units(20 * GIB, 17)

        clock.now += timedelta(hours=2)
        assert await activate() is None
        await db.refresh(sub)
        await db.refresh(quota)
        assert sub.end_date == quota.period_ends_at == expected_end
        assert sub.start_date == quota.period_starts_at == expected_end - timedelta(days=3)


@pytest.mark.asyncio
async def test_reused_legacy_pending_gets_first_ledger_before_commit(monkeypatch, clock):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', False)
        sub = await pending(db)
        assert await db.get(LteQuota, sub.id) is None
        monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
        clock.now += timedelta(days=1)
        retry = await pending(db)
        quota = await db.get(LteQuota, sub.id)
        assert retry.id == sub.id and quota is not None
        assert quota.period_starts_at == clock.now
        assert quota.period_ends_at == retry.end_date == clock.now + timedelta(days=3)
        assert retry.traffic_limit_gb == 0 and retry.connected_squads == [WIFI, LTE]


@pytest.mark.asyncio
async def test_observed_pending_trial_cannot_be_reissued_by_activation(monkeypatch, clock):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        sub = await pending(db)
        quota = await db.get(LteQuota, sub.id)
        quota.counter_bytes = 100
        await db.commit()
        clock.now += timedelta(days=1)
        with pytest.raises(LtePolicyError, match='уже использовался'):
            await crud.activate_pending_trial_subscription(db, sub.id, sub.user_id)
        await db.rollback()
        await db.refresh(sub)
        await db.refresh(quota)
        assert sub.status == 'pending'
        assert sub.end_date == quota.period_ends_at == NOW + timedelta(days=3)
        assert quota.counter_bytes == 100


@pytest.mark.asyncio
async def test_flags_off_leave_pending_trial_without_new_ledger(monkeypatch, clock):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', False)
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        sub = await pending(db)
        assert sub.network_category is None and sub.traffic_limit_gb == 20
        assert sub.connected_squads == ['old-squad']
        assert await db.get(LteQuota, sub.id) is None


@pytest.mark.asyncio
async def test_direct_trial_creation_reusing_pending_keeps_original_duration_and_cap(monkeypatch, clock):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        sub = await pending(db)
        quota = await db.get(LteQuota, sub.id)
        clock.now += timedelta(days=1)
        activated = await crud.create_trial_subscription(
            db,
            sub.user_id,
            duration_days=30,
            traffic_limit_gb=200,
            device_limit=7,
            connected_squads=['old-squad'],
            tariff_id=3,
        )
        await db.refresh(quota)
        assert activated.id == sub.id and activated.status == 'trial'
        assert activated.start_date == quota.period_starts_at == clock.now
        assert activated.end_date == quota.period_ends_at == clock.now + timedelta(days=3)
        assert activated.device_limit == 1 and activated.connected_squads == [WIFI, LTE]
        assert quota.limit_bytes == ceil_units(20 * GIB, 17)


@pytest.mark.asyncio
async def test_expired_managed_trial_cannot_be_recycled_into_unpaid_invoice(monkeypatch, clock):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        sub = await pending(db)
        await crud.activate_pending_trial_subscription(db, sub.id, sub.user_id)
        quota = await db.get(LteQuota, sub.id)
        quota.base_used_bytes = quota.limit_bytes
        quota.used_bytes = quota.limit_bytes
        sub.status = 'expired'
        await db.commit()
        clock.now += timedelta(days=4)
        with pytest.raises(LtePolicyError, match='нельзя повторно выдать'):
            await pending(db)
        await db.rollback()
        await db.refresh(sub)
        await db.refresh(quota)
        assert sub.status == 'expired'
        assert quota.base_used_bytes == quota.limit_bytes
