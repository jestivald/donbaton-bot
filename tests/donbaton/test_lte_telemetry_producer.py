"""Canary proofs: each exact LTE route needs its own newly persisted usage.

Lease v2 names the routes proven this cycle and a code for each failed one; the
key disappears only when no route is proven at all.
"""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from scripts.donbaton.lte_telemetry import (
    FAILURE_CODES,
    MAX_ROUTES,
    Config,
    NoRouteProven,
    PanelAPI,
    Producer,
    Route,
    TelemetryError,
    XrayClients,
    cycle_event,
    fetch_asset,
    lease_status,
    read_outbound,
)


NODES = ['85950bae-4671-409c-b090-bbc2adf6e7b3', '97181e55-43ee-47c6-b45e-6ce188e57309']
THIRD = '516dd304-9a41-4507-8545-a46f92f6fe69'
CREDS = [
    '00000000-0000-4000-8000-000000000001',
    '00000000-0000-4000-8000-000000000002',
    '00000000-0000-4000-8000-000000000003',
]
STAMP = datetime(2026, 9, 9, 12, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize('base', ['http://remnawave:3000', 'https://panel.example'])
async def test_panel_client_uses_private_backend_proxy_contract_without_redirects(base):
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {'response': {'id': 11}}

    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    result = await PanelAPI(SimpleNamespace(get=get), base, 'synthetic-test-token').get('/users/11')
    assert result == {'id': 11}
    url, kwargs = calls[0]
    assert url == base + '/api/users/11'
    assert kwargs['allow_redirects'] is False
    assert kwargs['headers']['Authorization'] == 'Bearer synthetic-test-token'
    if base.startswith('http:'):
        assert kwargs['headers']['X-Forwarded-Proto'] == 'https'
        assert kwargs['headers']['X-Forwarded-For'] == '127.0.0.1'
        assert kwargs['headers']['Host'] == 'localhost'
        assert kwargs['headers']['X-Api-Key'] == 'synthetic-test-token'
    else:
        assert 'Host' not in kwargs['headers']
        assert 'X-Forwarded-Proto' not in kwargs['headers']


class Clock:
    def __init__(self):
        self.elapsed = 0

    def monotonic(self):
        return self.elapsed

    def now(self):
        return STAMP + timedelta(seconds=self.elapsed)

    async def sleep(self, duration):
        self.elapsed += duration
        await asyncio.sleep(0)


class Panel:
    """Per-node and per-user knobs, so one route can break while another works."""

    def __init__(self, nodes):
        self.nodes = list(nodes)
        self.counters = {11 + index: [100 * (index + 1), 100 * (index + 1) + 100] for index in range(len(nodes))}
        self.multipliers = {}
        self.node_uuid_override = {}
        self.connected = {}
        self.telegram_ids = {}
        self.broken_paths = {}
        self.hang_after = {}
        self.reads = {}

    async def get(self, path):
        self.reads[path] = self.reads.get(path, 0) + 1
        if path in self.broken_paths:
            raise self.broken_paths[path]
        if path in self.hang_after and self.reads[path] > self.hang_after[path]:
            await asyncio.Event().wait()
        if path.startswith('/nodes/'):
            node = path.rsplit('/', 1)[1]
            return {
                'uuid': self.node_uuid_override.get(node, node),
                'consumptionMultiplier': self.multipliers.get(node, 1),
            }
        user_id = int(path.rsplit('/', 1)[1])
        values = self.counters[user_id]
        value = values.pop(0) if len(values) > 1 else values[0]
        index = user_id - 11
        return {
            'id': user_id,
            'vlessUuid': CREDS[index],
            'telegramId': self.telegram_ids.get(user_id),
            'status': 'ACTIVE',
            'trafficLimitBytes': 0,
            'userTraffic': {
                'lifetimeUsedTrafficBytes': value,
                'lastConnectedNodeUuid': self.connected.get(user_id, self.nodes[index]),
            },
        }


def fixture(nodes=NODES):
    config = Config(
        'test-epoch',
        tuple(Route(node, 11 + index, Path('private')) for index, node in enumerate(nodes)),
        'https://owned.example/asset.js',
        'a' * 64,
    )
    clients = SimpleNamespace(
        credentials=dict(zip(nodes, CREDS, strict=False)),
        proxies={node: 'http://127.0.0.1:' + str(18000 + index) for index, node in enumerate(nodes)},
    )
    clock, panel = Clock(), Panel(nodes)
    redis = SimpleNamespace(set=AsyncMock(return_value=True), delete=AsyncMock())
    fetch = AsyncMock()
    events = []
    producer = Producer(
        config,
        panel,
        redis,
        clients,
        fetch,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        utcnow=clock.now,
        emit=events.append,
    )
    producer.events = events
    return producer, panel, redis, fetch, clock


def route_codes(producer):
    return {event['node_id']: event['code'] for event in producer.events if event['event'] == 'lte_telemetry_route'}


def proxy_of(node):
    return 'http://127.0.0.1:' + str(18000 + NODES.index(node))


@pytest.mark.asyncio
async def test_all_routes_proven_publish_a_complete_v2_lease():
    producer, _, redis, fetch, _ = fixture()
    payload = await producer.run_once()
    assert payload == {
        'version': 2,
        'epoch': 'test-epoch',
        'observed_at': STAMP.isoformat(),
        'node_ids': NODES,
        'failed': {},
    }
    args, kwargs = redis.set.call_args
    assert args[0] == 'donbaton:lte:telemetry:test-epoch'
    assert json.loads(args[1]) == payload
    assert kwargs == {'ex': 180}
    assert fetch.await_count == 2
    assert len({call.args[2] for call in fetch.await_args_list}) == 2
    redis.delete.assert_not_awaited()
    assert route_codes(producer) == dict.fromkeys(NODES, 'ok')
    assert cycle_event(producer.config, payload) == {'event': 'lte_telemetry_ready', 'epoch': 'test-epoch'}


@pytest.mark.asyncio
async def test_one_dead_route_narrows_the_lease_and_the_sibling_still_proves_itself():
    producer, _, redis, _, _ = fixture()

    async def fetch(config, proxy, nonce):
        if proxy == proxy_of(NODES[0]):
            raise aiohttp.ClientProxyConnectionError(None, OSError('synthetic refused'))

    producer.fetch = fetch
    payload = await producer.run_once()
    assert payload['node_ids'] == [NODES[1]]
    assert payload['failed'] == {NODES[0]: 'unreachable'}
    assert json.loads(redis.set.call_args.args[1]) == payload
    redis.delete.assert_not_awaited()  # the healthy route keeps LTE evidence alive
    failed_event = next(event for event in producer.events if event.get('node_id') == NODES[0])
    assert failed_event == {
        'event': 'lte_telemetry_route',
        'node_id': NODES[0],
        'code': 'unreachable',
        'error_type': 'ClientProxyConnectionError',
    }
    assert 'synthetic refused' not in json.dumps(producer.events)  # codes and types only, never error text
    assert cycle_event(producer.config, payload) == {
        'event': 'lte_telemetry_partial',
        'epoch': 'test-epoch',
        'node_ids': [NODES[1]],
        'failed': {NODES[0]: 'unreachable'},
    }


@pytest.mark.asyncio
async def test_every_route_failing_removes_the_lease_and_reports_each_code():
    producer, panel, redis, fetch, _ = fixture()
    panel.multipliers[NODES[0]] = 17
    fetch.side_effect = TimeoutError()
    with pytest.raises(NoRouteProven) as raised:
        await producer.run_once()
    assert raised.value.failed == {NODES[0]: 'multiplier', NODES[1]: 'unreachable'}
    redis.delete.assert_awaited_once_with(producer.config.key)
    redis.set.assert_not_awaited()
    assert route_codes(producer) == {NODES[0]: 'multiplier', NODES[1]: 'unreachable'}
    event = cycle_event(producer.config, error=raised.value)
    assert event['event'] == 'lte_telemetry_unavailable'
    assert event['failed'] == raised.value.failed


@pytest.mark.asyncio
async def test_successful_asset_without_counter_progress_cannot_refresh_that_route():
    producer, panel, redis, fetch, clock = fixture()
    panel.counters[12] = [300]
    payload = await producer.run_once()
    assert clock.elapsed >= 30
    assert fetch.await_count == 2
    assert payload['node_ids'] == [NODES[0]]
    assert payload['failed'] == {NODES[1]: 'not_persisted'}
    redis.delete.assert_not_awaited()


def break_route(panel, fetch, failure, node_index):
    node, user_id = NODES[node_index], 11 + node_index
    if failure == 'regressed':
        panel.counters[user_id] = [100, 50]
    elif failure == 'wrong_node':
        panel.connected[user_id] = '00000000-0000-4000-8000-000000000099'
    elif failure == 'multiplier':
        panel.multipliers[node] = 17
    elif failure == 'panel':
        panel.broken_paths['/users/' + str(user_id)] = TelemetryError('Panel read failed')
    elif failure == 'panel_identity':
        panel.telegram_ids[user_id] = 123
    elif failure == 'panel_node_identity':
        panel.node_uuid_override[node] = '00000000-0000-4000-8000-000000000098'
    elif failure == 'panel_transport':
        panel.broken_paths['/nodes/' + node] = aiohttp.ClientConnectionError('synthetic')
    elif failure in ('asset', 'unreachable'):
        error = TelemetryError('Owned origin asset digest mismatch') if failure == 'asset' else OSError('synthetic')

        async def fetch_side_effect(config, proxy, nonce):
            if proxy == proxy_of(node):
                raise error

        fetch.side_effect = fetch_side_effect


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('failure', 'code'),
    [
        ('regressed', 'regressed'),
        ('wrong_node', 'wrong_node'),
        ('multiplier', 'multiplier'),
        ('panel', 'panel'),
        ('panel_identity', 'panel'),
        ('panel_node_identity', 'panel'),
        ('panel_transport', 'panel'),
        ('asset', 'asset'),
        ('unreachable', 'unreachable'),
    ],
)
async def test_each_failure_is_named_by_its_code_and_spares_the_other_route(failure, code):
    producer, panel, redis, fetch, _ = fixture()
    break_route(panel, fetch, failure, 0)
    payload = await producer.run_once()
    assert payload['node_ids'] == [NODES[1]]
    assert payload['failed'] == {NODES[0]: code}
    assert code in FAILURE_CODES
    assert route_codes(producer) == {NODES[0]: code, NODES[1]: 'ok'}
    redis.set.assert_awaited_once()
    redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_observation_deadline_is_not_persisted_even_when_the_panel_stalls():
    producer, panel, _, _, _ = fixture()
    producer.config = replace(producer.config, observe_seconds=0.05)
    panel.hang_after['/users/11'] = 1  # the baseline read works, every observation read hangs
    payload = await producer.run_once()
    assert payload['failed'] == {NODES[0]: 'not_persisted'}


@pytest.mark.asyncio
async def test_retry_rechecks_every_route_and_uses_new_nonces():
    producer, panel, redis, fetch, _ = fixture()
    fetch.side_effect = TelemetryError('one failed attempt')
    with pytest.raises(NoRouteProven):
        await producer.run_once()
    old_nonces = {call.args[2] for call in fetch.await_args_list}
    fetch.reset_mock(side_effect=True)
    panel.counters = {11: [500, 600], 12: [700, 800]}
    payload = await producer.run_once()
    assert payload['failed'] == {}
    assert fetch.await_count == 2
    assert old_nonces.isdisjoint({call.args[2] for call in fetch.await_args_list})
    redis.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_lease_uses_oldest_proof_and_does_not_extend_its_age():
    producer, _, redis, _, _ = fixture()
    times = iter([STAMP, STAMP + timedelta(seconds=20), STAMP + timedelta(seconds=25)])
    producer.utcnow = lambda: next(times)
    result = await producer.run_once()
    assert result['observed_at'] == STAMP.isoformat()
    assert redis.set.call_args.kwargs['ex'] == 155


@pytest.mark.asyncio
async def test_stale_completed_evidence_is_rejected():
    producer, _, redis, _, _ = fixture()
    times = iter([STAMP, STAMP, STAMP + timedelta(seconds=181)])
    producer.utcnow = lambda: next(times)
    with pytest.raises(TelemetryError, match='stale'):
        await producer.run_once()
    redis.set.assert_not_awaited()
    redis.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_refusal_removes_the_lease():
    producer, _, redis, _, _ = fixture()
    redis.set.return_value = False
    with pytest.raises(TelemetryError, match='acknowledge'):
        await producer.run_once()
    redis.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_cancels_every_route_and_removes_the_lease():
    producer, _, redis, _, _ = fixture()
    cancelled = asyncio.Event()

    async def fetch(config, proxy, nonce):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    producer.fetch = fetch
    cycle = asyncio.create_task(producer.run_once())
    await asyncio.sleep(0.01)
    cycle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cycle
    assert cancelled.is_set()
    redis.delete.assert_awaited_once_with(producer.config.key)
    redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_third_route_is_proven_on_its_own():
    producer, panel, redis, _, _ = fixture([*NODES, THIRD])
    panel.multipliers[THIRD] = 17
    payload = await producer.run_once()
    assert payload['node_ids'] == sorted(NODES)
    assert payload['failed'] == {THIRD: 'multiplier'}
    assert json.loads(redis.set.call_args.args[1]) == payload


def plan(tmp_path, nodes, user_ids=None):
    user_ids = user_ids or [11 + index for index in range(len(nodes))]
    raw = {
        'epoch': 'epoch1',
        'origin_url': 'https://owned.example/asset.js',
        'origin_sha256': 'a' * 64,
        'routes': [
            {'node_id': node, 'user_id': user_id, 'client_config': 'private'}
            for node, user_id in zip(nodes, user_ids, strict=True)
        ],
    }
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(raw))
    return path


def environment(nodes):
    return {'DONBATON_LTE_ACCOUNTING_EPOCH': 'epoch1', 'DONBATON_LTE_NODE_UUIDS': ','.join(nodes)}


def test_private_plan_rejects_epoch_mismatch_and_duplicate_identities(tmp_path):
    path = plan(tmp_path, NODES)
    env = environment(NODES)
    assert Config.load(path, env).node_ids == NODES
    with pytest.raises(TelemetryError, match='epoch'):
        Config.load(path, dict(env, DONBATON_LTE_ACCOUNTING_EPOCH='epoch2'))
    with pytest.raises(TelemetryError, match='distinct'):
        Config.load(plan(tmp_path, NODES, [11, 11]), env)
    with pytest.raises(TelemetryError, match='distinct'):
        Config.load(plan(tmp_path, [NODES[0], NODES[0]]), environment([NODES[0], NODES[0]]))


@pytest.mark.parametrize('nodes', [[NODES[0]], NODES, [*NODES, THIRD]])
def test_private_plan_accepts_one_or_more_routes_matching_the_environment(tmp_path, nodes):
    config = Config.load(plan(tmp_path, nodes), environment(nodes))
    assert config.node_ids == sorted(nodes)
    assert len(config.routes) == len(nodes)


@pytest.mark.parametrize(
    ('nodes', 'env_nodes'),
    [
        ([], []),
        (NODES, [NODES[0]]),
        ([NODES[0]], NODES),
        (NODES, [*NODES, THIRD]),
    ],
)
def test_private_plan_rejects_route_sets_that_disagree_with_the_environment(tmp_path, nodes, env_nodes):
    with pytest.raises(TelemetryError, match='distinct'):
        Config.load(plan(tmp_path, nodes), environment(env_nodes))


def test_private_plan_rejects_more_routes_than_the_bounded_contract(tmp_path):
    nodes = [f'00000000-0000-4000-8000-{index:012d}' for index in range(MAX_ROUTES + 1)]
    with pytest.raises(TelemetryError, match='distinct'):
        Config.load(plan(tmp_path, nodes), environment(nodes))


@pytest.mark.asyncio
async def test_clients_require_one_distinct_credential_per_route(tmp_path, monkeypatch):
    routes = []
    for index, node in enumerate([*NODES, THIRD]):
        path = tmp_path / f'client-{index}.json'
        credential = CREDS[0] if index == 2 else CREDS[index]
        outbound = {'protocol': 'vless', 'settings': {'vnext': [{'users': [{'id': credential}]}]}}
        path.write_text(json.dumps({'outbounds': [outbound]}))
        routes.append(Route(node, 11 + index, path))

    class Process:
        returncode = None

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            self.returncode = 0

    async def spawn(*args, **kwargs):
        return Process()

    async def connected(host, port):
        writer = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
        return None, writer

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(asyncio, 'open_connection', connected)
    clients = XrayClients('/unused', routes)
    with pytest.raises(TelemetryError, match='distinct VLESS credential'):
        await clients.start()
    assert not clients.processes
    unique = XrayClients('/unused', routes[:2])
    await unique.start()
    assert len(unique.processes) == 2
    await unique.close()


def test_lease_status_reads_own_v1_and_v2_leases_and_rejects_foreign_ones():
    config = Config('epoch1', tuple(Route(node, 11 + index, Path('p')) for index, node in enumerate(NODES)), 'u', 'h')
    now = STAMP + timedelta(seconds=30)

    def lease(**changes):
        return {'version': 2, 'epoch': 'epoch1', 'observed_at': STAMP.isoformat(), **changes}

    complete = lease(node_ids=NODES, failed={})
    partial = lease(node_ids=[NODES[1]], failed={NODES[0]: 'unreachable'})
    legacy = {'version': 1, 'epoch': 'epoch1', 'observed_at': STAMP.isoformat(), 'node_ids': NODES}
    assert lease_status(complete, config, now=now) == {'valid': True, 'complete': True, 'node_ids': NODES, 'failed': {}}
    assert lease_status(partial, config, now=now) == {
        'valid': True,
        'complete': False,
        'node_ids': [NODES[1]],
        'failed': {NODES[0]: 'unreachable'},
    }
    assert lease_status(legacy, config, now=now)['complete']
    for foreign in [
        None,
        lease(node_ids=NODES, failed={}, epoch='other'),
        lease(node_ids=NODES, failed={}, observed_at=(STAMP - timedelta(seconds=200)).isoformat()),
        lease(node_ids=NODES, failed={}, observed_at=STAMP.replace(tzinfo=None).isoformat()),
        lease(node_ids=NODES, failed={}, version=3),
        lease(node_ids=[NODES[1]], failed={}),
        lease(node_ids=[NODES[1]], failed={NODES[0]: 'mystery'}),
        lease(node_ids=NODES, failed={NODES[0]: 'unreachable'}),
        lease(node_ids=[NODES[1], NODES[1]], failed={NODES[0]: 'asset'}),
        lease(node_ids=[*NODES, THIRD], failed={}),
        lease(node_ids=[], failed=dict.fromkeys(NODES, 'asset')),
        dict(legacy, node_ids=[NODES[0]]),
    ]:
        assert lease_status(foreign, config, now=now)['valid'] is False


def test_client_config_rejects_direct_fallback(tmp_path):
    path = tmp_path / 'client.json'
    path.write_text(json.dumps({'outbounds': [{'protocol': 'vless'}, {'protocol': 'freedom'}]}))
    with pytest.raises(TelemetryError, match='no fallback'):
        read_outbound(Route(NODES[0], 11, path))


@pytest.mark.asyncio
async def test_asset_request_is_bounded_and_uses_explicit_proxy_and_nonce():
    producer, _, _, _, _ = fixture()

    class Response:
        status = 200  # A server ignoring Range must fail before reading its full body.
        content_length = 999999

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    with pytest.raises(TelemetryError, match='bounded asset range'):
        await fetch_asset(SimpleNamespace(get=get), producer.config, 'http://127.0.0.1:18000', 'nonce123')
    url, kwargs = calls[0]
    assert 'donbaton_nonce=nonce123' in url
    assert kwargs['proxy'] == 'http://127.0.0.1:18000'
    assert kwargs['headers']['Range'] == 'bytes=0-65535'
    assert kwargs['headers']['Accept-Encoding'] == 'identity'
    assert kwargs['allow_redirects'] is False


@pytest.mark.asyncio
async def test_valid_range_requires_complete_expected_body_digest():
    producer, _, _, _, _ = fixture()
    body = b'x' * 65536
    config = replace(producer.config, origin_sha256=hashlib.sha256(body).hexdigest())

    class Response:
        status = 206
        content_length = 65536
        headers = {'Content-Range': 'bytes 0-65535/999999'}
        content = SimpleNamespace(readexactly=AsyncMock(return_value=body))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    session = SimpleNamespace(get=lambda *args, **kwargs: Response())
    await fetch_asset(session, config, 'http://127.0.0.1:18000', 'nonce')
    with pytest.raises(TelemetryError, match='digest'):
        await fetch_asset(session, replace(config, origin_sha256='0' * 64), 'http://127.0.0.1:18000', 'nonce2')


@pytest.mark.asyncio
async def test_client_cleanup_terminates_only_owned_processes():
    class Process:
        returncode = None

        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

        async def wait(self):
            self.returncode = 0

    own, server = Process(), Process()
    clients = XrayClients('/unused', [])
    clients.processes = [own]
    await clients.close()
    assert own.terminated
    assert not server.terminated
    assert not clients.processes
