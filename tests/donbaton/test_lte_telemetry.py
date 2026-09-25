"""Actual heartbeat consumer: healthy panel GETs cannot conceal frozen accounting."""

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from structlog.testing import capture_logs

from app.config import settings
from app.services import lte_quota_service, lte_telemetry_alerts
from app.services.admin_notification_service import AdminNotificationService
from app.services.lte_accounting_service import (
    telemetry_proof,
    telemetry_within_grace,
    timestamp,
    valid_telemetry_heartbeat,
)
from app.services.lte_quota_service import (
    LtePolicyError,
    TelemetryEvidence,
    _read_telemetry_heartbeat as production_reader,
    _resolve_telemetry_evidence,
    apply_panel_policy,
    get_quota_status,
    quota_status_from_subscription,
    run_quota_cycle,
    validate_tariff_for_sale,
)
from app.utils.cache import cache
from tests.donbaton.conftest import TEST_LTE_NODES
from tests.donbaton.test_lte_legacy import TABLES as CYCLE_TABLES
from tests.donbaton.test_lte_quota import LTE, NOW, TABLES, WIFI, sample, seed, tariff
from tests.fixtures.sqlite_memory import memory_session


OTHER_NODE = '00000000-0000-4000-8000-000000000013'
CLOSED = 'LTE telemetry missing beyond grace, LTE closed'


def proof(at=NOW):
    return {
        'version': 1,
        'epoch': settings.DONBATON_LTE_ACCOUNTING_EPOCH,
        'observed_at': at.isoformat(),
        'node_ids': list(TEST_LTE_NODES),
    }


def proof_v2(proven, failed=None, at=NOW):
    """What the v2 canary writes: the proven subset plus a code per failed route."""
    return {
        'version': 2,
        'epoch': settings.DONBATON_LTE_ACCOUNTING_EPOCH,
        'observed_at': at.isoformat(),
        'node_ids': list(proven),
        'failed': dict(failed or {}),
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
        {'version': 2},  # v2 without its failure map is malformed
        {'version': 3},
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
        # The producer proves no route right now (or is restarting), so the
        # anchor is all that is left of a pipeline that never actually broke.
        redis_store[memory_key()] = json.dumps(remembered(NOW - timedelta(minutes=5)))
        with capture_logs() as logs:
            payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert payload['active_internal_squads'] == [WIFI, LTE]
        assert quota_status_from_subscription(sub)['accounting_state'] == 'ready'
        warnings = [log for log in logs if log['event'] == 'LTE telemetry missing, within grace']
        assert [log['log_level'] for log in warnings] == ['warning']
        assert warnings[0]['remaining_seconds'] == 600


@pytest.mark.asyncio
async def test_outage_beyond_the_window_closes_lte_and_says_so(monkeypatch, redis_store):
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        seen_at = NOW - timedelta(minutes=20)
        redis_store[memory_key()] = json.dumps(remembered(seen_at))
        with capture_logs() as logs:
            payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert payload['active_internal_squads'] == [WIFI]
        assert (await get_quota_status(db, sub, now=NOW))['accounting_state'] == 'telemetry_unavailable'
        assert not [log for log in logs if log['event'] == 'LTE telemetry missing, within grace']
        # Closing LTE used to be silent: the grace warnings simply stopped.
        closed = [log for log in logs if log['event'] == CLOSED]
        assert [log['log_level'] for log in closed] == ['warning']
        assert closed[0]['grace_seconds'] == 900
        assert timestamp(closed[0]['last_valid_seen_at']) == seen_at


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


A, B = TEST_LTE_NODES


def accepted(payload, *, nodes=TEST_LTE_NODES, min_routes=1):
    return valid_telemetry_heartbeat(
        payload,
        epoch=settings.DONBATON_LTE_ACCOUNTING_EPOCH,
        node_ids=nodes,
        now=NOW,
        max_age_seconds=180,
        min_routes=min_routes,
    )


@pytest.mark.parametrize(
    ('lease', 'min_routes', 'expected'),
    [
        (lambda: proof_v2([A, B]), 1, True),
        (lambda: proof_v2([B], {A: 'unreachable'}), 1, True),
        (lambda: proof_v2([B]), 1, True),  # a route the canary does not check is simply missing
        (lambda: proof_v2([B], {A: 'unreachable'}), 2, False),
        (lambda: proof_v2([B], {A: 'unreachable'}), 5, False),  # clamped to both configured nodes
        (lambda: proof_v2([A, B]), 5, True),
        (lambda: proof_v2([B, OTHER_NODE], {A: 'asset'}), 1, True),  # an unconfigured node is ignored
        (lambda: proof_v2([OTHER_NODE], dict.fromkeys([A, B], 'asset')), 1, False),
        (lambda: proof_v2([], dict.fromkeys([A, B], 'asset')), 1, False),
        (lambda: proof_v2([B, B], {A: 'asset'}), 1, False),
        (lambda: proof_v2([A, B], {A: 'asset'}), 1, False),  # proven and failed at once
        (lambda: dict(proof_v2([B]), failed=[A]), 1, False),
        (lambda: proof_v2([B], {A: 7}), 1, False),
        (lambda: proof_v2([B], {'not-a-uuid': 'asset'}), 1, False),
        (lambda: proof_v2([B], at=NOW - timedelta(seconds=181)), 1, False),
        (lambda: dict(proof(), node_ids=[B]), 1, False),  # v1 stays all-or-nothing
    ],
)
def test_v2_lease_is_accepted_on_enough_configured_routes(lease, min_routes, expected):
    assert accepted(lease(), min_routes=min_routes) is expected


def test_route_reading_names_what_is_proven_missing_and_why():
    reading = telemetry_proof(
        proof_v2([B, OTHER_NODE], {A: 'not_persisted'}),
        epoch=settings.DONBATON_LTE_ACCOUNTING_EPOCH,
        node_ids=TEST_LTE_NODES,
    )
    assert reading.proven == (B,)
    assert reading.missing == (A,)
    assert reading.failed == {A: 'not_persisted'}
    assert reading.extra == (OTHER_NODE,)
    assert reading.accepted


def test_grace_accepts_a_remembered_partial_proof():
    lease = {
        'payload': proof_v2([B], {A: 'unreachable'}, at=NOW - timedelta(hours=1)),
        'seen_at': (NOW - timedelta(seconds=300)).isoformat(),
    }
    arguments = dict(epoch=settings.DONBATON_LTE_ACCOUNTING_EPOCH, node_ids=TEST_LTE_NODES, now=NOW, grace_seconds=900)
    assert telemetry_within_grace(lease, **arguments)
    assert not telemetry_within_grace(lease, min_routes=2, **arguments)


@pytest.mark.asyncio
async def test_one_dead_bypass_keeps_lte_for_everyone_and_anchors_grace(monkeypatch, redis_store):
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        partial = proof_v2([B], {A: 'unreachable'})
        redis_store[live_key()] = json.dumps(partial)
        payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert payload['active_internal_squads'] == [WIFI, LTE]
        assert quota_status_from_subscription(sub)['accounting_state'] == 'ready'
        # A partial lease is live telemetry: it anchors the grace window like a complete one.
        assert json.loads(redis_store[memory_key()])['payload'] == partial
        evidence = await _resolve_telemetry_evidence(now=NOW)
        assert evidence.state == 'live'
        assert (evidence.proven, evidence.missing, evidence.failed) == ((B,), (A,), {A: 'unreachable'})


@pytest.mark.asyncio
async def test_losing_every_route_falls_back_to_grace_then_closes(monkeypatch, redis_store):
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)
    redis_store[live_key()] = json.dumps(proof_v2([B], {A: 'unreachable'}))
    assert (await _resolve_telemetry_evidence(now=NOW)).state == 'live'
    del redis_store[live_key()]  # the last route failed too: the canary removed its key
    grace = await _resolve_telemetry_evidence(now=NOW + timedelta(seconds=60))
    assert (grace.state, grace.grace_remaining, grace.missing) == ('grace', 840, (A, B))
    assert (await _resolve_telemetry_evidence(now=NOW + timedelta(seconds=901))).state == 'closed'


@pytest.mark.asyncio
async def test_minimum_above_the_node_count_is_clamped_with_one_warning(monkeypatch, redis_store):
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_MIN_ROUTES', 5)
    monkeypatch.setattr(lte_quota_service, '_REPORTED_TELEMETRY_CONFIG', set())
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        redis_store[live_key()] = json.dumps(proof_v2([A, B]))
        with capture_logs() as logs:
            for _ in range(3):
                payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert payload['active_internal_squads'] == [WIFI, LTE]  # the setting alone never closes LTE
        warnings = [log for log in logs if log['event'].startswith('LTE telemetry minimum routes exceeds')]
        assert [(log['requested'], log['nodes']) for log in warnings] == [(5, 2)]
        redis_store[live_key()] = json.dumps(proof_v2([B], {A: 'asset'}))
        payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert payload['active_internal_squads'] == [WIFI]  # clamped to every configured route


@pytest.mark.asyncio
async def test_unconfigured_nodes_in_a_lease_are_ignored_with_one_warning(monkeypatch, redis_store):
    monkeypatch.setattr(lte_quota_service, '_REPORTED_TELEMETRY_CONFIG', set())
    redis_store[live_key()] = json.dumps(proof_v2([B, OTHER_NODE], {A: 'unreachable'}))
    with capture_logs() as logs:
        for _ in range(2):
            evidence = await _resolve_telemetry_evidence(now=NOW)
    assert evidence.state == 'live'
    assert evidence.proven == (B,)
    warnings = [log for log in logs if log['event'] == 'LTE telemetry lease names unconfigured nodes, ignored']
    assert [log['nodes'] for log in warnings] == [(OTHER_NODE,)]


@pytest.mark.asyncio
async def test_one_to_n_configured_nodes(monkeypatch, redis_store):
    monkeypatch.setattr(settings, 'DONBATON_LTE_NODE_UUIDS', f'{A},{B},{OTHER_NODE}')
    # The old v1 canary proves two routes: that no longer covers a third configured one.
    redis_store[live_key()] = json.dumps(proof())
    assert (await _resolve_telemetry_evidence(now=NOW)).state == 'closed'
    redis_store[live_key()] = json.dumps(proof_v2([A, B]))
    evidence = await _resolve_telemetry_evidence(now=NOW)
    assert evidence.state == 'live'
    assert (evidence.missing, evidence.failed) == ((OTHER_NODE,), {OTHER_NODE: 'unmonitored'})
    monkeypatch.setattr(settings, 'DONBATON_LTE_NODE_UUIDS', A)
    redis_store[live_key()] = json.dumps(dict(proof(), node_ids=[A]))
    assert (await _resolve_telemetry_evidence(now=NOW)).state == 'live'


def test_lte_sale_needs_at_least_one_verified_node(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_NODE_UUIDS', ' , ')
    with pytest.raises(LtePolicyError, match='хотя бы одной'):
        validate_tariff_for_sale(tariff())
    monkeypatch.setattr(settings, 'DONBATON_LTE_NODE_UUIDS', A)
    validate_tariff_for_sale(tariff())


@pytest.mark.asyncio
async def test_cycle_that_closes_lte_logs_it_every_cycle_and_alerts_the_owner_once(monkeypatch, redis_store):
    from app.database import database
    from app.services.admin_notification_service import NotificationCategory

    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '-100500')
    monkeypatch.setattr(lte_telemetry_alerts, '_memory', {})
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(AdminNotificationService, 'send_admin_notification', send)
    async with memory_session(monkeypatch, CYCLE_TABLES) as db:
        await seed(db)
        maker = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(database, 'AsyncSessionLocal', maker)
        api = SimpleNamespace(get_user_by_id=AsyncMock(return_value=sample(0)), update_user=AsyncMock())

        @asynccontextmanager
        async def client():
            yield api

        service = SimpleNamespace(is_configured=True, get_api_client=client)
        # The cycle runs on the wall clock: an anchor an hour old is beyond any grace.
        redis_store[memory_key()] = json.dumps(remembered(NOW - timedelta(hours=1)))
        with capture_logs() as logs:
            await run_quota_cycle(service, bot=object())
            await run_quota_cycle(service, bot=object())
    assert api.update_user.await_args_list[0].kwargs['active_internal_squads'] == [WIFI]
    assert len([log for log in logs if log['event'] == CLOSED]) == 2
    send.assert_awaited_once()  # one message for the change, not one per cycle
    assert 'LTE снят у всех' in send.await_args.args[0]
    assert send.await_args.kwargs == {'category': NotificationCategory.INFRASTRUCTURE}
