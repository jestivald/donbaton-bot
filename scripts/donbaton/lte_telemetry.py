#!/usr/bin/env python3
"""Independent LTE canary: real tunnel payload -> persisted panel counter -> Redis lease."""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
import signal
import socket
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

import aiohttp
from redis.asyncio import Redis


class TelemetryError(RuntimeError):
    """A canary cannot establish a current accounting proof."""


# A lease is published while at least one route is proven, so the consumer can
# keep LTE open through a single dead bypass. Each failed route is named by one
# of these codes; exception text never leaves the process (it can hold URLs).
FAILURE_CODES = {
    'unreachable': 'The exact LTE route did not deliver the owned asset request',
    'asset': 'The owned origin did not return the pinned bounded asset range',
    'panel': 'The private panel API did not confirm the synthetic identity or node',
    'not_persisted': 'Canary payload was not persisted within the observation deadline',
    'multiplier': 'LTE node consumption multiplier is not 1:1',
    'wrong_node': 'Canary traffic was persisted on a different node',
    'regressed': 'Canary lifetime counter regressed',
}
MAX_ROUTES = 8


class RouteFailure(TelemetryError):
    """One route cannot be proven this cycle; `code` is its public reason."""

    def __init__(self, code):
        super().__init__(FAILURE_CODES[code])
        self.code = code


class NoRouteProven(TelemetryError):
    """Every route failed: the lease is removed instead of being narrowed."""

    def __init__(self, failed):
        super().__init__('No LTE route established a current proof')
        self.failed = dict(failed)


@dataclass(frozen=True)
class Route:
    node_id: str
    user_id: int
    client_config: Path


@dataclass(frozen=True)
class Config:
    epoch: str
    routes: tuple[Route, ...]
    origin_url: str
    origin_sha256: str
    payload_bytes: int = 65536
    interval_seconds: int = 60
    observe_seconds: int = 30
    ttl_seconds: int = 180

    @property
    def node_ids(self):
        return sorted(route.node_id for route in self.routes)

    @property
    def key(self):
        return 'donbaton:lte:telemetry:' + self.epoch

    @classmethod
    def load(cls, path, environ=os.environ):
        raw = json.loads(Path(path).read_text())
        epoch = environ.get('DONBATON_LTE_ACCOUNTING_EPOCH', '').strip()
        if not re.fullmatch(r'[A-Za-z0-9:_-]{1,128}', epoch) or raw.get('epoch') != epoch:
            raise TelemetryError('Private plan and configured accounting epoch must match')
        expected = sorted(
            str(UUID(value.strip())) for value in environ['DONBATON_LTE_NODE_UUIDS'].split(',') if value.strip()
        )
        routes = tuple(
            Route(str(UUID(route['node_id'])), route['user_id'], Path(route['client_config']))
            for route in raw['routes']
        )
        if (
            not 1 <= len(routes) <= MAX_ROUTES
            or len(set(expected)) != len(expected)
            or sorted(route.node_id for route in routes) != expected
            or any(type(route.user_id) is not int or route.user_id <= 0 for route in routes)
            or len({route.user_id for route in routes}) != len(routes)
        ):
            raise TelemetryError(
                f'1 to {MAX_ROUTES} routes with distinct node IDs and numeric synthetic user IDs are required'
            )
        origin = urlsplit(raw['origin_url'])
        if origin.scheme != 'https' or not origin.hostname or origin.username or origin.password or origin.fragment:
            raise TelemetryError('Owned origin must be an explicit HTTPS asset URL')
        if not re.fullmatch(r'[a-f0-9]{64}', raw['origin_sha256']):
            raise TelemetryError('A SHA256 of the exact bounded asset range is required')
        config = cls(
            epoch=epoch,
            routes=routes,
            origin_url=raw['origin_url'],
            origin_sha256=raw['origin_sha256'],
            payload_bytes=raw.get('payload_bytes', 65536),
            interval_seconds=raw.get('interval_seconds', 60),
            observe_seconds=raw.get('observe_seconds', 30),
            ttl_seconds=raw.get('ttl_seconds', 180),
        )
        for value, lower, upper in [
            (config.payload_bytes, 65536, 262144),
            (config.interval_seconds, 60, 90),
            (config.observe_seconds, 25, 35),
            (config.ttl_seconds, 120, 180),
        ]:
            if type(value) is not int or not lower <= value <= upper:
                raise TelemetryError('Probe size/timing is outside the bounded deployment contract')
        return config


def read_outbound(route):
    config = json.loads(route.client_config.read_text())
    outbounds = config.get('outbounds', [])
    if len(outbounds) != 1 or outbounds[0].get('protocol') != 'vless':
        raise TelemetryError('Each private client must contain exactly one VLESS outbound, with no fallback')
    outbound = copy.deepcopy(outbounds[0])
    settings = outbound.get('settings', {})
    servers = settings.get('vnext', [])
    if len(servers) != 1 or len(servers[0].get('users', [])) != 1:
        raise TelemetryError('A canary route must pin one server and one credential')
    if outbound.get('proxySettings') or outbound.get('streamSettings', {}).get('sockopt', {}).get('dialerProxy'):
        raise TelemetryError('Chained outbound routing is not allowed for a canary')
    credential = str(UUID(servers[0]['users'][0]['id']))
    return outbound, credential


class XrayClients:
    """Own one local HTTP proxy process per route; never stop a server/core service."""

    def __init__(self, binary, routes):
        self.binary, self.routes = Path(binary), routes
        self.processes, self.proxies, self.credentials = [], {}, {}
        self.directory = None

    async def start(self):
        self.directory = tempfile.TemporaryDirectory(prefix='donbaton-lte-canary-')
        try:
            for index, route in enumerate(self.routes):
                outbound, credential = read_outbound(route)
                with socket.socket() as reservation:
                    reservation.bind(('127.0.0.1', 0))
                    port = reservation.getsockname()[1]
                config = {
                    'log': {'loglevel': 'none'},
                    'inbounds': [{'listen': '127.0.0.1', 'port': port, 'protocol': 'http', 'settings': {}}],
                    'outbounds': [outbound],
                }
                path = Path(self.directory.name) / f'route-{index}.json'
                path.write_text(json.dumps(config))
                process = await asyncio.create_subprocess_exec(
                    str(self.binary),
                    'run',
                    '-c',
                    str(path),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                self.processes.append(process)
                for _ in range(100):
                    if process.returncode is not None:
                        raise TelemetryError('Canary client exited before startup')
                    try:
                        reader, writer = await asyncio.open_connection('127.0.0.1', port)
                        del reader
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(0.05)
                else:
                    raise TelemetryError('Canary local proxy startup timed out')
                self.proxies[route.node_id] = f'http://127.0.0.1:{port}'
                self.credentials[route.node_id] = credential
            if len(set(self.credentials.values())) != len(self.routes):
                raise TelemetryError('Each canary requires a distinct VLESS credential')
        except BaseException:
            await self.close()
            raise

    async def close(self):
        for process in reversed(self.processes):
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        self.processes.clear()
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None


class PanelAPI:
    def __init__(self, session, url, token):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise TelemetryError('Invalid private panel API URL')
        self.session, self.url, self.token = session, url.rstrip('/'), token
        self.headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
        if parsed.scheme == 'http':
            # The private Docker/loopback API has the same trusted-proxy
            # contract as the bot's RemnaWaveAPI client. Bearer auth is still
            # required; this route never depends on a public shield cookie.
            self.headers.update(
                {
                    'X-Api-Key': token,
                    'X-Forwarded-Proto': 'https',
                    'X-Forwarded-For': '127.0.0.1',
                    'X-Real-IP': '127.0.0.1',
                    'X-Forwarded-Host': 'localhost',
                    'Host': 'localhost',
                }
            )

    async def get(self, path):
        async with self.session.get(
            self.url + '/api' + path,
            headers=self.headers,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as response:
            if response.status != 200:
                raise TelemetryError('Panel read failed')
            data = await response.json()
            if not isinstance(data.get('response'), dict):
                raise TelemetryError('Invalid panel response envelope')
            return data['response']


def counter(user, route, credential):
    # The panel no longer presents the owned canary identity (or a readable
    # counter): the route cannot be judged, which is a panel-side failure.
    if (
        user.get('id') != route.user_id
        or user.get('telegramId') is not None
        or user.get('vlessUuid') != credential
        or user.get('status') != 'ACTIVE'
        or user.get('trafficLimitBytes') != 0
    ):
        raise RouteFailure('panel')
    traffic = user.get('userTraffic')
    value = traffic.get('lifetimeUsedTrafficBytes') if isinstance(traffic, dict) else None
    if type(value) is not int or value < 0:
        raise RouteFailure('panel')
    return value


def verify_node(node, route):
    if node.get('uuid') != route.node_id:
        raise RouteFailure('panel')
    multiplier = node.get('consumptionMultiplier')
    if type(multiplier) not in (int, float) or multiplier != 1:
        raise RouteFailure('multiplier')


def _aware(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError('naive timestamp')
    return moment


def lease_status(payload, config, *, now, max_age_seconds=180):
    """Read-only judgement of a stored lease against this private plan.

    For operator hooks that run inside the canary image, whose bundled bot code
    may predate lease v2. `valid` means a current lease of this epoch proving at
    least one planned route (v1: all of them); `complete` means every route.
    """
    invalid = {'valid': False, 'complete': False, 'node_ids': [], 'failed': {}}
    planned = set(config.node_ids)
    try:
        if not isinstance(payload, dict) or payload.get('epoch') != config.epoch:
            return invalid
        version = payload.get('version')
        age = (now - _aware(payload['observed_at'])).total_seconds()
        listed = payload['node_ids']
        if type(version) is not int or not 0 <= age <= max_age_seconds or not isinstance(listed, list):
            return invalid
        proven = {str(UUID(node_id)) for node_id in listed}
        if not proven or len(proven) != len(listed) or not proven <= planned:
            return invalid
        if version == 1:
            failed = {}
            if proven != planned:
                return invalid
        elif version == 2:
            raw = payload['failed']
            if not isinstance(raw, dict) or any(code not in FAILURE_CODES for code in raw.values()):
                return invalid
            failed = {str(UUID(node_id)): code for node_id, code in raw.items()}
            if len(failed) != len(raw) or set(failed) & proven or proven | set(failed) != planned:
                return invalid
        else:
            return invalid
    except (KeyError, TypeError, ValueError, AttributeError):
        return invalid
    return {'valid': True, 'complete': not failed, 'node_ids': sorted(proven), 'failed': dict(sorted(failed.items()))}


async def fetch_asset(session, config, proxy, nonce):
    origin = urlsplit(config.origin_url)
    query = [(key, value) for key, value in parse_qsl(origin.query, keep_blank_values=True) if key != 'donbaton_nonce']
    query.append(('donbaton_nonce', nonce))
    url = urlunsplit((origin.scheme, origin.netloc, origin.path, urlencode(query), ''))
    size = config.payload_bytes
    async with session.get(
        url,
        proxy=proxy,
        allow_redirects=False,
        headers={'Range': f'bytes=0-{size - 1}', 'Accept-Encoding': 'identity', 'Cache-Control': 'no-cache'},
        timeout=aiohttp.ClientTimeout(total=12),
    ) as response:
        if (
            response.status != 206
            or response.content_length != size
            or response.headers.get('Content-Encoding', 'identity') != 'identity'
            or not re.fullmatch(rf'bytes 0-{size - 1}/\d+', response.headers.get('Content-Range', ''))
        ):
            raise TelemetryError('Origin did not provide the exact bounded asset range')
        body = await response.content.readexactly(size)
        if hashlib.sha256(body).hexdigest() != config.origin_sha256:
            raise TelemetryError('Owned origin asset digest mismatch')


def print_event(event):
    print(json.dumps(event), flush=True)


class Producer:
    def __init__(
        self,
        config,
        panel,
        redis,
        clients,
        fetch,
        *,
        sleep=asyncio.sleep,
        monotonic=time.monotonic,
        utcnow=lambda: datetime.now(UTC),
        emit=print_event,
    ):
        self.config, self.panel, self.redis, self.clients, self.fetch = config, panel, redis, clients, fetch
        self.sleep, self.monotonic, self.utcnow, self.emit = sleep, monotonic, utcnow, emit

    async def invalidate(self):
        await self.redis.delete(self.config.key)

    async def read_panel(self, path):
        try:
            return await self.panel.get(path)
        except Exception as error:  # HTTP status, envelope, transport or timeout
            raise RouteFailure('panel') from error

    async def prove_route(self, route):
        """Return when the route's own payload was persisted; raise RouteFailure otherwise."""
        verify_node(await self.read_panel('/nodes/' + route.node_id), route)
        credential = self.clients.credentials[route.node_id]
        before = counter(await self.read_panel('/users/' + str(route.user_id)), route, credential)
        try:
            await self.fetch(self.config, self.clients.proxies[route.node_id], uuid4().hex)
        except TelemetryError as error:  # the tunnel answered, but not with the pinned asset range
            raise RouteFailure('asset') from error
        except Exception as error:  # proxy/tunnel/TLS/timeout: the route did not carry the request
            raise RouteFailure('unreachable') from error
        deadline = self.monotonic() + self.config.observe_seconds
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise RouteFailure('not_persisted')
            try:
                # read_panel turns every panel error into `panel`, so a timeout
                # escaping here is the observation deadline itself.
                user = await asyncio.wait_for(self.read_panel('/users/' + str(route.user_id)), remaining)
            except TimeoutError as error:
                raise RouteFailure('not_persisted') from error
            if self.monotonic() > deadline:
                raise RouteFailure('not_persisted')
            after = counter(user, route, credential)
            if after < before:
                raise RouteFailure('regressed')
            if after > before:
                if user['userTraffic'].get('lastConnectedNodeUuid') != route.node_id:
                    raise RouteFailure('wrong_node')
                observed = self.utcnow()
                verify_node(await self.read_panel('/nodes/' + route.node_id), route)
                return observed
            if self.monotonic() >= deadline:
                raise RouteFailure('not_persisted')
            await self.sleep(min(2, max(0.01, deadline - self.monotonic())))

    def record(self, route, result):
        """One log event per route and cycle: its node and code, never exception text."""
        event = {'event': 'lte_telemetry_route', 'node_id': route.node_id}
        if isinstance(result, datetime):
            event['code'] = 'ok'
        else:
            event['code'] = result.code if isinstance(result, RouteFailure) else 'unreachable'
            cause = result.__cause__ if isinstance(result, RouteFailure) else result
            if cause is not None:
                event['error_type'] = type(cause).__name__
        self.emit(event)
        return event['code']

    async def run_once(self):
        """Prove every route independently and publish the proven subset.

        Lease v2: `node_ids` are the routes proven this cycle, `failed` maps the
        rest to a code, `observed_at` is the oldest successful observation. The
        key is removed only when no route is proven, so one dead bypass no longer
        withdraws the evidence the other routes still provide.
        """
        routes = self.config.routes
        tasks = [asyncio.create_task(self.prove_route(route)) for route in routes]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            proven, failed = {}, {}
            for route, result in zip(routes, results, strict=True):
                code = self.record(route, result)
                if code == 'ok':
                    proven[route.node_id] = result
                else:
                    failed[route.node_id] = code
            if not proven:
                raise NoRouteProven(failed)
            observed = min(proven.values())
            age = (self.utcnow() - observed).total_seconds()
            if age < 0 or age >= self.config.ttl_seconds:
                raise TelemetryError('Completed canary evidence is stale or in the future')
            payload = {
                'version': 2,
                'epoch': self.config.epoch,
                'observed_at': observed.isoformat(),
                'node_ids': sorted(proven),
                'failed': dict(sorted(failed.items())),
            }
            ttl = min(self.config.ttl_seconds, max(1, int(self.config.ttl_seconds - age)))
            if await self.redis.set(self.config.key, json.dumps(payload, separators=(',', ':')), ex=ttl) is not True:
                raise TelemetryError('Redis did not acknowledge the telemetry lease')
            return payload
        except BaseException:
            # No proof at all, a storage failure or a stop: never leave the
            # previous lease standing for evidence this cycle could not renew.
            for task in tasks:
                task.cancel()
            try:
                await self.invalidate()
            finally:
                await asyncio.gather(*tasks, return_exceptions=True)
            raise


def cycle_event(config, payload=None, error=None):
    """The single summary line of a cycle; per-route lines come from Producer.record.

    `ready` keeps its old meaning: every route proven. `partial` is a lease on a
    proven subset, `unavailable` means no lease. Exception text is never logged:
    HTTP errors can contain private URLs.
    """
    if error is not None:
        event = {
            'event': 'lte_telemetry_unavailable',
            'epoch': config.epoch,
            'error_type': type(error).__name__,
            'reason': str(error) if isinstance(error, TelemetryError) else 'request_or_storage_failed',
        }
        if isinstance(error, NoRouteProven):
            event['failed'] = error.failed
        return event
    if not payload['failed']:
        return {'event': 'lte_telemetry_ready', 'epoch': config.epoch}
    return {
        'event': 'lte_telemetry_partial',
        'epoch': config.epoch,
        'node_ids': payload['node_ids'],
        'failed': payload['failed'],
    }


async def serve(args):
    os.umask(0o077)
    redis = Redis.from_url(os.environ['REDIS_URL'], socket_connect_timeout=3, socket_timeout=3)
    key = 'donbaton:lte:telemetry:' + os.environ.get('DONBATON_LTE_ACCOUNTING_EPOCH', '').strip()
    clients = None
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await redis.delete(key)  # old evidence cannot survive a producer restart or invalid plan
        config = Config.load(args.config)
        clients = XrayClients(args.xray, config.routes)
        await clients.start()
        async with aiohttp.ClientSession(trust_env=False, auto_decompress=False) as session:
            panel = PanelAPI(session, os.environ['DONBATON_PANEL_URL'], os.environ['DONBATON_PANEL_TOKEN'])

            async def fetch(cfg, proxy, nonce):
                await fetch_asset(session, cfg, proxy, nonce)

            producer = Producer(config, panel, redis, clients, fetch)
            while not stopped.is_set():
                started = time.monotonic()
                cycle = asyncio.create_task(producer.run_once())
                stopping = asyncio.create_task(stopped.wait())
                done, _ = await asyncio.wait([cycle, stopping], return_when=asyncio.FIRST_COMPLETED)
                if stopping in done:
                    cycle.cancel()
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
                try:
                    event = cycle_event(config, await cycle)
                except asyncio.CancelledError:
                    break
                except Exception as error:
                    event = cycle_event(config, error=error)
                print_event(event)
                if args.once:
                    if event['event'] != 'lte_telemetry_ready':
                        raise TelemetryError('Diagnostic cycle did not establish every route proof')
                    break
                delay = max(1, config.interval_seconds - (time.monotonic() - started))
                try:
                    await asyncio.wait_for(stopped.wait(), delay)
                except TimeoutError:
                    pass
    finally:
        try:
            await redis.delete(key)
        finally:
            if clients is not None:
                await clients.close()
            await redis.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--xray', type=Path, default=Path('/usr/local/bin/xray'))
    parser.add_argument(
        '--once',
        action='store_true',
        help='Diagnostic cycle, nonzero unless every route is proven; lease removed on exit',
    )
    args = parser.parse_args()
    try:
        asyncio.run(serve(args))
    except Exception as error:
        print(json.dumps({'event': 'lte_telemetry_stopped', 'error_type': type(error).__name__}), flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
