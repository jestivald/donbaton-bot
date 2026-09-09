"""Paid CRUD conversions must activate LTE even when import kept the target tariff ID."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.database.crud.subscription import extend_subscription
from app.database.models import LavaSubscription, SentNotification
from app.services import lte_quota_service
from app.services.lte_quota_service import GIB, apply_panel_policy, initialize_trial_quota
from tests.donbaton.test_lte_legacy import BASELINE, EPOCH, TABLES as LEGACY_TABLES, activate, legacy_seed
from tests.donbaton.test_lte_quota import LTE, NOW, WIFI, sample
from tests.fixtures.sqlite_memory import memory_session


TABLES = [*LEGACY_TABLES, SentNotification.__table__, LavaSubscription.__table__]


@pytest.fixture(autouse=True)
def accounting_config(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_EPOCH', EPOCH)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    nodes = ['00000000-0000-4000-8000-000000000003', '00000000-0000-4000-8000-000000000004']
    monkeypatch.setattr(settings, 'DONBATON_LTE_NODE_UUIDS', ','.join(nodes))

    async def heartbeat(*, now=None):
        return {'version': 1, 'epoch': EPOCH, 'node_ids': nodes, 'observed_at': (now or datetime.now(UTC)).isoformat()}

    monkeypatch.setattr(lte_quota_service, '_read_telemetry_heartbeat', heartbeat)


@pytest.mark.asyncio
async def test_paid_extend_same_imported_trial_tariff_grants_paid_lte_and_devices(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(
            db,
            is_trial=True,
            devices=1,
            custom=True,
            paid_until=NOW + timedelta(days=3),
        )
        quota = await activate(db, sub, target, snapshot)
        original = deepcopy(quota.legacy_state['snapshot'])
        assert sub.tariff_id == target.id

        await extend_subscription(
            db,
            sub,
            30,
            tariff_id=target.id,
            traffic_limit_gb=0,
            device_limit=target.device_limit,
            connected_squads=target.allowed_squads,
        )
        await db.refresh(quota)
        assert not sub.is_trial
        assert quota.legacy_state['runtime'].get('trial_converted_at') is not None
        # The outstanding old delta stays attached to the previous checkpoint;
        # the next successful sample closes it before opening the paid period.
        assert quota.counter_bytes == BASELINE
        result = await apply_panel_policy(
            db,
            sub,
            {},
            panel_user=sample(BASELINE + 17),
            now=datetime.now(UTC) + timedelta(seconds=1),
        )
        assert result['active_internal_squads'] == [WIFI, LTE]
        assert quota.limit_bytes == target.lte_traffic_limit_gb * GIB
        assert quota.base_used_bytes == 0
        assert quota.counter_bytes == BASELINE + 17
        assert quota.legacy_state['runtime']['base_ended']
        assert quota.legacy_state['snapshot'] == original
        assert sub.device_limit == target.device_limit == 3


@pytest.mark.asyncio
async def test_paid_extend_same_new_trial_tariff_clears_single_trial_allowance(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, _, _ = await legacy_seed(db, is_trial=True, paid_until=NOW + timedelta(days=3))
        quota = await initialize_trial_quota(db, sub, legacy_traffic_limit_gb=20, now=NOW)
        sub.tariff_id = target.id
        await db.commit()
        await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=NOW)

        await extend_subscription(db, sub, 30, tariff_id=target.id, device_limit=target.device_limit)
        await db.refresh(quota)
        assert not sub.is_trial
        assert quota.counter_bytes == BASELINE
        await apply_panel_policy(
            db,
            sub,
            {},
            panel_user=sample(BASELINE + 17),
            now=datetime.now(UTC) + timedelta(seconds=1),
        )
        assert quota.trial_ends_at is None
        assert quota.limit_bytes == target.lte_traffic_limit_gb * GIB
        assert quota.base_used_bytes == 0
        assert quota.counter_bytes == BASELINE + 17
        assert quota.period_ends_at - quota.period_starts_at == timedelta(days=30)


@pytest.mark.asyncio
async def test_unpaid_same_tariff_extension_does_not_convert_imported_trial(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, is_trial=True, paid_until=NOW + timedelta(days=3))
        quota = await activate(db, sub, target, snapshot)
        original = deepcopy(quota.legacy_state)
        await extend_subscription(db, sub, 1, tariff_id=target.id, convert_trial=False)
        await db.refresh(quota)
        assert sub.is_trial
        assert quota.legacy_state == original
        assert quota.limit_bytes == 0
