"""Canary proofs require both exact LTE routes and newly persisted usage."""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.donbaton.lte_telemetry import (
    Config,
    PanelAPI,
    Producer,
    Route,
    TelemetryError,
    XrayClients,
    fetch_asset,
    read_outbound,
)


NODES = ['85950bae-4671-409c-b090-bbc2adf6e7b3', '97181e55-43ee-47c6-b45e-6ce188e57309']
CREDS = ['00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000002']
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
    def __init__(self):
        self.counters = {11: [100, 200], 12: [300, 400]}
        self.multiplier = 1
        self.connected_override = None
        self.telegram_id = None

    async def get(self, path):
        if path.startswith('/nodes/'):
            return {'uuid': path.rsplit('/', 1)[1], 'consumptionMultiplier': self.multiplier}
        user_id = int(path.rsplit('/', 1)[1])
        values = self.counters[user_id]
        value = values.pop(0) if len(values) > 1 else values[0]
        index = user_id - 11
        return {
            'id': user_id,
            'vlessUuid': CREDS[index],
            'telegramId': self.telegram_id,
            'status': 'ACTIVE',
            'trafficLimitBytes': 0,
            'userTraffic': {
                'lifetimeUsedTrafficBytes': value,
                'lastConnectedNodeUuid': self.connected_override or NODES[index],
            },
        }


def fixture():
    config = Config(
        'test-epoch',
        tuple(Route(node, 11 + index, Path('private')) for index, node in enumerate(NODES)),
        'https://owned.example/asset.js',
        'a' * 64,
    )
    clients = SimpleNamespace(
        credentials=dict(zip(NODES, CREDS, strict=True)),
        proxies={node: 'http://127.0.0.1:' + str(18000 + index) for index, node in enumerate(NODES)},
    )
    clock, panel = Clock(), Panel()
    redis = SimpleNamespace(set=AsyncMock(return_value=True), delete=AsyncMock())
    fetch = AsyncMock()
    producer = Producer(
        config, panel, redis, clients, fetch, sleep=clock.sleep, monotonic=clock.monotonic, utcnow=clock.now
    )
    return producer, panel, redis, fetch, clock


@pytest.mark.asyncio
async def test_two_positive_persisted_counters_publish_exact_epoch_and_node_set():
    producer, _, redis, fetch, _ = fixture()
    payload = await producer.run_once()
    assert payload == {'version': 1, 'epoch': 'test-epoch', 'observed_at': STAMP.isoformat(), 'node_ids': NODES}
    args, kwargs = redis.set.call_args
    assert args[0] == 'donbaton:lte:telemetry:test-epoch'
    assert json.loads(args[1]) == payload
    assert kwargs == {'ex': 180}
    assert fetch.await_count == 2
    assert len({call.args[2] for call in fetch.await_args_list}) == 2
    redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_failed_route_clears_previous_lease_and_cancels_sibling():
    producer, _, redis, _, _ = fixture()
    sibling_cancelled = asyncio.Event()

    async def fetch(config, proxy, nonce):
        if proxy.endswith('18000'):
            await asyncio.sleep(0)
            raise TelemetryError('route unavailable')
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    producer.fetch = fetch
    with pytest.raises(TelemetryError):
        await producer.run_once()
    redis.delete.assert_awaited_once_with(producer.config.key)
    redis.set.assert_not_awaited()
    assert sibling_cancelled.is_set()


@pytest.mark.asyncio
async def test_successful_asset_without_counter_progress_cannot_refresh_health():
    producer, panel, redis, fetch, clock = fixture()
    panel.counters[12] = [300]
    with pytest.raises(TelemetryError, match='not persisted'):
        await producer.run_once()
    assert clock.elapsed >= 30
    assert fetch.await_count == 2
    redis.delete.assert_awaited_once()
    redis.set.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['regression', 'wrong_node', 'weighted', 'real_user'])
async def test_invalid_accounting_identity_or_units_invalidate_health(failure):
    producer, panel, redis, _, _ = fixture()
    if failure == 'regression':
        panel.counters[11] = [100, 50]
    if failure == 'wrong_node':
        panel.connected_override = '00000000-0000-4000-8000-000000000099'
    if failure == 'weighted':
        panel.multiplier = 17
    if failure == 'real_user':
        panel.telegram_id = 123
    with pytest.raises(TelemetryError):
        await producer.run_once()
    redis.delete.assert_awaited_once()
    redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_rechecks_both_routes_and_uses_new_nonces():
    producer, panel, redis, fetch, _ = fixture()
    fetch.side_effect = TelemetryError('one failed attempt')
    with pytest.raises(TelemetryError):
        await producer.run_once()
    old_nonces = {call.args[2] for call in fetch.await_args_list}
    fetch.reset_mock(side_effect=True)
    panel.counters = {11: [500, 600], 12: [700, 800]}
    await producer.run_once()
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


def test_private_plan_rejects_epoch_mismatch_and_duplicate_identities(tmp_path):
    raw = {
        'epoch': 'epoch1',
        'origin_url': 'https://owned.example/asset.js',
        'origin_sha256': 'a' * 64,
        'routes': [
            {'node_id': node, 'user_id': 11 + index, 'client_config': 'private'} for index, node in enumerate(NODES)
        ],
    }
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(raw))
    env = {'DONBATON_LTE_ACCOUNTING_EPOCH': 'epoch1', 'DONBATON_LTE_NODE_UUIDS': ','.join(NODES)}
    assert Config.load(path, env).node_ids == NODES
    with pytest.raises(TelemetryError, match='epoch'):
        Config.load(path, dict(env, DONBATON_LTE_ACCOUNTING_EPOCH='epoch2'))
    raw['routes'][1]['user_id'] = 11
    path.write_text(json.dumps(raw))
    with pytest.raises(TelemetryError, match='distinct'):
        Config.load(path, env)


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
