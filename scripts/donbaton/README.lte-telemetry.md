# Independent LTE accounting canary

`lte_telemetry.py` keeps one local Xray HTTP client per LTE route (1 to 8
routes). Once per minute it downloads a bounded HTTPS asset range through each
exact route and confirms that the corresponding synthetic user's persisted panel
lifetime counter grew within 30 seconds. It checks the node's
`consumptionMultiplier == 1` before and after, and the user's
`userTraffic.lastConnectedNodeUuid` after persistence.

Routes are proven independently. While at least one route is proven, Redis
receives lease **v2**:

```json
{"version":2,"epoch":"OPERATOR_CONFIRMED_EPOCH","observed_at":"2026-09-25T12:00:00+00:00","node_ids":["97181e55-43ee-47c6-b45e-6ce188e57309"],"failed":{"85950bae-4671-409c-b090-bbc2adf6e7b3":"unreachable"}}
```

- `node_ids` — the routes proven in this cycle; `failed` — every other planned
  route with one code (never exception text). A complete cycle has `failed: {}`.
- `observed_at` — the oldest successful counter observation of the cycle.

Key: `donbaton:lte:telemetry:<epoch>`. TTL is at most 180 seconds, reduced by the
proof's age. When **no** route is proven, the key is removed immediately, as
before; a Redis failure or a stop also removes it. Startup, stop and an invalid
private plan remove the configured epoch's key. A Redis outage cannot issue a
lease; an inaccessible prior lease can live only until its existing TTL expires.
Each cycle rechecks every route with fresh nonces.

Failure codes:

| Code | Meaning |
|---|---|
| `unreachable` | the tunnel did not carry the request (proxy, TLS, connect, timeout) |
| `asset` | the origin answered through the tunnel, but not with the pinned 64 KiB range/digest |
| `panel` | the private panel API failed, or no longer presents the owned synthetic user/node |
| `not_persisted` | the payload went through, but the user's lifetime counter did not grow within 30 s |
| `multiplier` | the node's `consumptionMultiplier` is not 1 |
| `wrong_node` | the counter grew, but on another node (`lastConnectedNodeUuid`) |
| `regressed` | the lifetime counter went down |

`not_persisted`, `multiplier` and `wrong_node` mean that accounting on that node
is in question, not only reachability; the bot's owner alert says so.

## Consumer compatibility

The bot accepts v2 from the release that introduced it (see
`docs/DONBATON.md`): LTE stays open while at least
`DONBATON_LTE_TELEMETRY_MIN_ROUTES` configured routes are proven, and the owner
is alerted about missing ones. It still accepts v1 (`{"version":1,…}` with the
full node set) from the previous producer. **An older bot rejects v2 and closes
LTE after its grace window**, so the bot is deployed first and rolled back last.

## Logs

One JSON line per route and cycle, then one summary line:

```text
{"event": "lte_telemetry_route", "node_id": "…", "code": "ok"}
{"event": "lte_telemetry_route", "node_id": "…", "code": "unreachable", "error_type": "ClientConnectorError"}
{"event": "lte_telemetry_ready", "epoch": "…"}                      every route proven
{"event": "lte_telemetry_partial", "epoch": "…", "node_ids": […], "failed": {…}}   lease on a subset
{"event": "lte_telemetry_unavailable", "epoch": "…", "error_type": "NoRouteProven", "reason": "…", "failed": {…}}   no lease
```

`error_type` is only the class name of the underlying error. `ready` keeps its
previous meaning (every route proven). A deploy check that waits for fresh
evidence must count `ready` **or** `partial`: both mean a written lease.

This process is independent of the bot's quota consumer and never loads bot
models, sends Telegram messages, buys anything or updates the panel. The operator
creates one distinct panel-only user per route (`telegramId=null`, global traffic
limit 0, ACTIVE, long expiry). The managed/orphan ledger must not own these
service users. Panel user IDs are numeric in Remnawave 3.4.3.

## Private configuration

Environment (mounted private env file, not image build arguments):

```text
DONBATON_PANEL_URL=http://remnawave:3000
DONBATON_PANEL_TOKEN=PRIVATE_READ_TOKEN
REDIS_URL=redis://PRIVATE_REDIS/0
DONBATON_LTE_ACCOUNTING_EPOCH=OPERATOR_CONFIRMED_EPOCH
DONBATON_LTE_NODE_UUIDS=85950bae-4671-409c-b090-bbc2adf6e7b3,97181e55-43ee-47c6-b45e-6ce188e57309
```

`DONBATON_LTE_NODE_UUIDS` must list exactly the plan's route nodes, without
repeats; a mismatch refuses to start.

Use the private Docker/loopback panel endpoint on the shared internal network.
For this HTTP connection the client sends the same authenticated trusted-proxy
headers as the bot's panel client. It does not require the public anti-DDoS
site's login cookie. A configured HTTPS API endpoint keeps its original Host
and must accept the Bearer token directly; redirects are rejected.

Mount `/run/secrets/lte-telemetry.json` and every private client JSON file
read-only, readable by container UID 1000, inaccessible to other host users.
Example structure:

```json
{
  "epoch": "OPERATOR_CONFIRMED_EPOCH",
  "origin_url": "https://OWNED_CABINET/assets/PINNED_HASH.js",
  "origin_sha256": "SHA256_OF_EXACT_FIRST_65536_UNCOMPRESSED_BYTES",
  "payload_bytes": 65536,
  "interval_seconds": 60,
  "observe_seconds": 30,
  "ttl_seconds": 180,
  "routes": [
    {"node_id":"85950bae-4671-409c-b090-bbc2adf6e7b3","user_id":NUMERIC_SPB_ID,"client_config":"/run/secrets/spb-client.json"},
    {"node_id":"97181e55-43ee-47c6-b45e-6ce188e57309","user_id":NUMERIC_CDN_ID,"client_config":"/run/secrets/cdn-client.json"}
  ]
}
```

Routes need distinct node IDs, distinct numeric user IDs and distinct VLESS
credentials. Each private Xray config must contain exactly one VLESS outbound,
one server and the respective service user's credential, preserving production
REALITY/XHTTP options. No direct fallback or chained proxy is accepted. The
script replaces inbounds with a localhost-only HTTP proxy; it never listens
publicly. Its HTTP client always specifies that proxy for origin requests and
ignores environment proxy bypasses. TLS verification stays enabled, redirects
are disabled, the request includes a unique `donbaton_nonce` query and
`Accept-Encoding: identity`.

The owned asset must support Range (HTTP 206), exact Content-Length and
Content-Range. An origin ignoring Range fails before its body is read. Its first
64 KiB SHA256 is pinned. Keep the hashed asset available across cabinet releases.
At 64 KiB per route and minute the useful payload is **90 MiB/day, 2.64 GiB per
30 days for every route**, plus protocol overhead. Size can be 64–256 KiB after
verifying the panel's ignore-below threshold. A new attempt is at most once a
minute.

## Build and run

Create an isolated context containing only these three files:

```text
Dockerfile                 (copy Dockerfile.lte-telemetry)
lte_telemetry.py
xray                       (reviewed Linux amd64 client binary)
```

Build on the same architecture. Supply the exact bot image digest or local image
ID whose `/app/.venv` contains aiohttp and redis; no dependency download is needed:

```sh
docker build --build-arg BOT_IMAGE=PINNED_BOT_IMAGE --build-arg XRAY_SHA256=PINNED_XRAY_SHA256 -t donbaton-lte-telemetry:RELEASE CONTEXT
```

The base image's `app/` code is whatever that bot image contained. Operator
checks running inside the canary image must therefore judge the lease with
`lease_status()` from this script, not with the bot's validator: an old base
knows only v1.

Run as a separate Compose service on a network with access to Redis and the panel,
with private env/config files, `restart: unless-stopped`, `stop_grace_period: 20s`,
`read_only: true`, and a writable private tmpfs at `/tmp`. No published ports,
Docker socket, host PID namespace, CAP_NET_ADMIN or server core process access
are required. SIGTERM cancels the current proof, deletes the lease and stops only
the child clients. `--once` is diagnostic, deletes the lease on exit and returns
nonzero unless every route was proven.

Validation: `python -m pytest tests/donbaton/test_lte_telemetry_producer.py -q`.
Local tests cover complete and partial leases, removal when every route fails,
each failure code, unchanged/regressed counters, wrong node/multiplier, stalled
observation, retries, oldest timestamp/TTL, one to three routes, epoch and
identity conflicts, bounded range/hash validation, lease reading and cleanup
ownership. Actual node→panel persistence and the private production route
configurations must be proven by the operator before enabling the paid ledger.
