"""Actual heartbeat consumer: healthy panel GETs cannot conceal frozen accounting."""

import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import lte_quota_service
from app.services.lte_accounting_service import valid_telemetry_heartbeat
from app.services.lte_quota_service import (
    _read_telemetry_heartbeat as production_reader,
    apply_panel_policy,
    get_quota_status,
)
from app.utils.cache import cache
from tests.donbaton.conftest import TEST_LTE_NODES
from tests.donbaton.test_lte_quota import LTE, NOW, TABLES, WIFI, sample, seed
from tests.fixtures.sqlite_memory import memory_session


def proof(at=NOW):
    return {
        'version': 1,
        'epoch': settings.DONBATON_LTE_ACCOUNTING_EPOCH,
        'observed_at': at.isoformat(),
        'node_ids': list(TEST_LTE_NODES),
    }


@pytest.fixture(autouse=True)
def real_consumer_with_fake_redis(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    monkeypatch.setattr(lte_quota_service, '_read_telemetry_heartbeat', production_reader)
    monkeypatch.setattr(cache, '_connected', True)
    monkeypatch.setattr(cache, 'redis_client', AsyncMock())


@pytest.mark.parametrize(
    'change',
    [
        {'version': True},
        {'version': 2},
        {'epoch': 'other'},
        {'observed_at': 'bad'},
        {'observed_at': (NOW + timedelta(seconds=1)).isoformat()},
        {'observed_at': (NOW - timedelta(seconds=181)).isoformat()},
        {'observed_at': NOW.replace(tzinfo=None).isoformat()},
        {'node_ids': [TEST_LTE_NODES[0]]},
        {'node_ids': [TEST_LTE_NODES[0], TEST_LTE_NODES[0]]},
        {'node_ids': [*TEST_LTE_NODES, '00000000-0000-4000-8000-000000000013']},
    ],
)
def test_heartbeat_validation_rejects_untrusted_or_stale_evidence(change):
    assert not valid_telemetry_heartbeat(
        dict(proof(), **change),
        epoch=settings.DONBATON_LTE_ACCOUNTING_EPOCH,
        node_ids=TEST_LTE_NODES,
        now=NOW,
        max_age_seconds=180,
    )
    assert valid_telemetry_heartbeat(
        proof(),
        epoch=settings.DONBATON_LTE_ACCOUNTING_EPOCH,
        node_ids=list(reversed(TEST_LTE_NODES)),
        now=NOW,
        max_age_seconds=180,
    )


@pytest.mark.asyncio
async def test_repeated_frozen_user_gets_cannot_keep_lte_ready_and_good_proof_recovers(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await seed(db)
        cache.redis_client.get.return_value = json.dumps(proof(NOW - timedelta(seconds=181)))
        for offset in (0, 30, 60):
            at = NOW + timedelta(seconds=offset)
            payload = await apply_panel_policy(db, sub, {}, panel_user=sample(1000), now=at)
            assert payload['active_internal_squads'] == [WIFI]
            assert (await get_quota_status(db, sub, now=at))['accounting_state'] == 'telemetry_unavailable'
        cache.redis_client.get.assert_awaited_with(f'donbaton:lte:telemetry:{settings.DONBATON_LTE_ACCOUNTING_EPOCH}')
        assert quota.counter_bytes == 1000
        cache.redis_client.get.return_value = json.dumps(proof(NOW + timedelta(seconds=61)))
        recovered = await apply_panel_policy(
            db,
            sub,
            {},
            panel_user=sample(1010),
            now=NOW + timedelta(seconds=61),
        )
        assert recovered['active_internal_squads'] == [WIFI, LTE]
        assert quota.used_bytes == 10


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [None, 'invalid-json', '[]', '{}'])
async def test_missing_or_malformed_redis_proof_keeps_wifi_only(monkeypatch, raw):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        cache.redis_client.get.return_value = raw
        payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert payload['active_internal_squads'] == [WIFI]


@pytest.mark.asyncio
async def test_cycle_snapshot_is_rechecked_for_age_and_cannot_extend_a_heartbeat(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW, heartbeat=proof())
        assert payload['active_internal_squads'] == [WIFI, LTE]
        late = await apply_panel_policy(
            db,
            sub,
            {},
            panel_user=sample(0),
            now=NOW + timedelta(seconds=181),
            heartbeat=proof(),
        )
        assert late['active_internal_squads'] == [WIFI]
        cache.redis_client.get.assert_not_awaited()  # provided cycle evidence is shared, never refetched per user
