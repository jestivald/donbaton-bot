"""Actual heartbeat consumer: healthy panel GETs cannot conceal frozen accounting."""

import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from app.config import settings
from app.services import lte_quota_service
from app.services.lte_accounting_service import telemetry_within_grace, timestamp, valid_telemetry_heartbeat
from app.services.lte_quota_service import (
    TelemetryEvidence,
    _read_telemetry_heartbeat as production_reader,
    apply_panel_policy,
    get_quota_status,
    quota_status_from_subscription,
)
from app.utils.cache import cache
from tests.donbaton.conftest import TEST_LTE_NODES
from tests.donbaton.test_lte_quota import LTE, NOW, TABLES, WIFI, sample, seed
from tests.fixtures.sqlite_memory import memory_session


OTHER_NODE = '00000000-0000-4000-8000-000000000013'


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


def live_key():
    return f'donbaton:lte:telemetry:{settings.DONBATON_LTE_ACCOUNTING_EPOCH}'


def memory_key():
    return f'{live_key()}:last-valid'


def remembered(seen_at=NOW, observed_at=None, **payload_changes):
    """What the consumer keeps behind the window: an old proof plus when it saw it."""
    return {'payload': dict(proof(observed_at or seen_at), **payload_changes), 'seen_at': seen_at.isoformat()}


@pytest.fixture
def redis_store(real_consumer_with_fake_redis):
    """Dict-backed Redis: the live lease and the grace anchor are distinct keys.

    One shared `return_value` would let the consumer answer a memory read with
    the live lease, and that confusion is exactly what these tests must catch.
    """
    store = {}

    async def read(key):
        return store.get(key)

    async def write(key, value, ex=None, **_options):
        store[key] = value
        store[f'{key}!ttl'] = ex
        return True

    cache.redis_client.get.side_effect = read
    cache.redis_client.set.side_effect = write
    return store


@pytest.mark.parametrize(
    ('field', 'change'),
    [
        ('payload', {'version': True}),
        ('payload', {'version': 2}),
        ('payload', {'epoch': 'other'}),
        ('payload', {'node_ids': [TEST_LTE_NODES[0]]}),
        ('payload', {'node_ids': [TEST_LTE_NODES[0], TEST_LTE_NODES[0]]}),
        ('payload', {'node_ids': [*TEST_LTE_NODES, OTHER_NODE]}),
        ('lease', {'payload': None}),
        ('lease', {'seen_at': 'bad'}),
        ('lease', {'seen_at': NOW.replace(tzinfo=None).isoformat()}),
        ('lease', {'seen_at': (NOW + timedelta(seconds=1)).isoformat()}),
        ('lease', {'seen_at': (NOW - timedelta(seconds=901)).isoformat()}),
    ],
)
def test_grace_rejects_a_memory_that_no_longer_proves_the_same_pipeline(field, change):
    lease = remembered(NOW - timedelta(seconds=300), observed_at=NOW - timedelta(hours=1))
    broken = dict(lease, payload=dict(lease['payload'], **change)) if field == 'payload' else dict(lease, **change)
    arguments = dict(epoch=settings.DONBATON_LTE_ACCOUNTING_EPOCH, node_ids=TEST_LTE_NODES, now=NOW)
    assert not telemetry_within_grace(broken, grace_seconds=900, **arguments)
    # The remembered proof is an hour stale by construction: forgiving that is
    # the entire point, and only the consumer's own `seen_at` bounds the window.
    assert telemetry_within_grace(lease, grace_seconds=900, **arguments)
    assert not telemetry_within_grace(lease, grace_seconds=0, **arguments)
    assert telemetry_within_grace(
        lease, epoch=arguments['epoch'], node_ids=list(reversed(TEST_LTE_NODES)), now=NOW, grace_seconds=900
    )


@pytest.mark.asyncio
async def test_live_proof_keeps_lte_and_records_the_grace_anchor(monkeypatch, redis_store):
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        redis_store[live_key()] = json.dumps(proof())
        payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert payload['active_internal_squads'] == [WIFI, LTE]
        assert quota_status_from_subscription(sub)['accounting_state'] == 'ready'
        lease = json.loads(redis_store[memory_key()])
        assert lease['payload'] == proof()
        assert timestamp(lease['seen_at']) == NOW
        # The anchor must outlive the window it serves, and nothing beyond it.
        assert redis_store[f'{memory_key()}!ttl'] == 900 + settings.DONBATON_LTE_MAX_SAMPLE_AGE_SECONDS


@pytest.mark.asyncio
async def test_short_producer_outage_inside_the_window_keeps_lte_and_warns_once(monkeypatch, redis_store):
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        # The producer drops its lease when either route fails, so the anchor is
        # all that is left of a pipeline that never actually broke.
        redis_store[memory_key()] = json.dumps(remembered(NOW - timedelta(minutes=5)))
        with capture_logs() as logs:
            payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert payload['active_internal_squads'] == [WIFI, LTE]
        assert quota_status_from_subscription(sub)['accounting_state'] == 'ready'
        warnings = [log for log in logs if log['event'] == 'LTE telemetry missing, within grace']
        assert [log['log_level'] for log in warnings] == ['warning']
        assert warnings[0]['remaining_seconds'] == 600


@pytest.mark.asyncio
async def test_outage_beyond_the_window_closes_lte(monkeypatch, redis_store):
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        redis_store[memory_key()] = json.dumps(remembered(NOW - timedelta(minutes=20)))
        with capture_logs() as logs:
            payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert payload['active_internal_squads'] == [WIFI]
        assert (await get_quota_status(db, sub, now=NOW))['accounting_state'] == 'telemetry_unavailable'
        assert not [log for log in logs if log['event'] == 'LTE telemetry missing, within grace']


@pytest.mark.asyncio
async def test_zero_grace_stays_strict_and_never_touches_the_memory(monkeypatch, redis_store):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        redis_store[memory_key()] = json.dumps(remembered(NOW - timedelta(seconds=1)))
        payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert payload['active_internal_squads'] == [WIFI]
        assert quota_status_from_subscription(sub)['accounting_state'] == 'telemetry_unavailable'
        # A disabled window may not even cost a read, let alone write an anchor.
        assert [call.args[0] for call in cache.redis_client.get.await_args_list] == [live_key()]
        cache.redis_client.set.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'epoch': 'another-epoch'}, {'node_ids': [TEST_LTE_NODES[0], OTHER_NODE]}])
async def test_memory_from_another_epoch_or_node_pair_is_not_grace(monkeypatch, redis_store, change):
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        redis_store[memory_key()] = json.dumps(remembered(NOW - timedelta(seconds=30), **change))
        payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert payload['active_internal_squads'] == [WIFI]
        assert quota_status_from_subscription(sub)['accounting_state'] == 'telemetry_unavailable'


@pytest.mark.asyncio
async def test_a_broken_memory_read_is_strict(monkeypatch, redis_store):
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)

    async def read(key):
        if key == memory_key():
            raise RuntimeError('redis is down')
        return redis_store.get(key)

    cache.redis_client.get.side_effect = read
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        redis_store[memory_key()] = json.dumps(remembered(NOW - timedelta(seconds=30)))
        payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert payload['active_internal_squads'] == [WIFI]
        assert quota_status_from_subscription(sub)['accounting_state'] == 'telemetry_unavailable'


@pytest.mark.asyncio
async def test_a_broken_memory_write_leaves_nothing_to_lean_on_next_cycle(monkeypatch, redis_store):
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)
    cache.redis_client.set.side_effect = RuntimeError('redis is down')
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        redis_store[live_key()] = json.dumps(proof())
        live = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert live['active_internal_squads'] == [WIFI, LTE]
        assert memory_key() not in redis_store
        del redis_store[live_key()]
        outage = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW + timedelta(seconds=30))
        assert outage['active_internal_squads'] == [WIFI]


@pytest.mark.asyncio
async def test_a_cycle_resolves_the_memory_once_for_every_subscription(monkeypatch, redis_store):
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        evidence = TelemetryEvidence(payload=None, last_valid=remembered(NOW - timedelta(minutes=5)))
        for offset in (0, 30, 60):
            payload = await apply_panel_policy(
                db, sub, {}, panel_user=sample(0), now=NOW + timedelta(seconds=offset), heartbeat=evidence
            )
            assert payload['active_internal_squads'] == [WIFI, LTE]
        cache.redis_client.get.assert_not_awaited()  # resolved once per cycle, never per user
        # A shared anchor still ages out inside the cycle, exactly like a lease.
        late = await apply_panel_policy(
            db, sub, {}, panel_user=sample(0), now=NOW + timedelta(seconds=901), heartbeat=evidence
        )
        assert late['active_internal_squads'] == [WIFI]
