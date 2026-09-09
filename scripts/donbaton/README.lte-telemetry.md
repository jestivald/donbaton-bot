# Independent LTE accounting canary

`lte_telemetry.py` keeps two local Xray HTTP clients running. Once per minute it
downloads a bounded HTTPS asset range through each exact LTE route and confirms
that the corresponding synthetic user's persisted panel lifetime counter grew
within 30 seconds. It checks the node's `consumptionMultiplier == 1` before and
after, and the user's `userTraffic.lastConnectedNodeUuid` after persistence.

Both routes must succeed before Redis receives:

```json
{"version":1,"epoch":"OPERATOR_CONFIRMED_EPOCH","observed_at":"2026-09-09T12:00:00+00:00","node_ids":["85950bae-4671-409c-b090-bbc2adf6e7b3","97181e55-43ee-47c6-b45e-6ce188e57309"]}
```

Key: `donbaton:lte:telemetry:<epoch>`. The timestamp is the older successful
counter observation. TTL is at most 180 seconds, reduced by that proof's age.
Any failed route removes a previous lease immediately; the next scheduled attempt
rechecks both routes and uses fresh nonces. Startup, stop and an invalid private
plan also remove the configured epoch's key. A Redis outage cannot issue a lease;
an inaccessible prior lease can live only until its existing TTL expires.

This process is independent of the bot's quota consumer and never loads bot
models, sends Telegram messages, buys anything or updates the panel. The operator
creates two distinct panel-only users (`telegramId=null`, global traffic limit 0,
ACTIVE, long expiry). The managed/orphan ledger must not own these service users.
Panel user IDs are numeric in Remnawave 3.4.3.

## Private configuration

Environment (mounted private env file, not image build arguments):

```text
DONBATON_PANEL_URL=http://remnawave:3000
DONBATON_PANEL_TOKEN=PRIVATE_READ_TOKEN
REDIS_URL=redis://PRIVATE_REDIS/0
DONBATON_LTE_ACCOUNTING_EPOCH=OPERATOR_CONFIRMED_EPOCH
DONBATON_LTE_NODE_UUIDS=85950bae-4671-409c-b090-bbc2adf6e7b3,97181e55-43ee-47c6-b45e-6ce188e57309
```

Use the private Docker/loopback panel endpoint on the shared internal network.
For this HTTP connection the client sends the same authenticated trusted-proxy
headers as the bot's panel client. It does not require the public anti-DDoS
site's login cookie. A configured HTTPS API endpoint keeps its original Host
and must accept the Bearer token directly; redirects are rejected.

Mount `/run/secrets/lte-telemetry.json` and both private client JSON files read-only,
readable by container UID 1000, inaccessible to other host users. Example structure:

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

Each private Xray config must contain exactly one VLESS outbound, one server and
the respective service user's credential, preserving production REALITY/XHTTP
options. No direct fallback or chained proxy is accepted. The script replaces
inbounds with a localhost-only HTTP proxy; it never listens publicly. Its HTTP
client always specifies that proxy for origin requests and ignores environment
proxy bypasses. TLS verification stays enabled, redirects are disabled, the
request includes a unique `donbaton_nonce` query and `Accept-Encoding: identity`.

The owned asset must support Range (HTTP 206), exact Content-Length and
Content-Range. An origin ignoring Range fails before its body is read. Its first
64 KiB SHA256 is pinned. Keep the hashed asset available across cabinet releases.
At 64 KiB × two routes/minute the useful payload is **180 MiB/day, 5.27 GiB per
30 days**, plus protocol overhead. Size can be 64–256 KiB after verifying the
panel's ignore-below threshold. A new attempt is at most once a minute.

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

Run as a separate Compose service on a network with access to Redis and the panel,
with private env/config files, `restart: unless-stopped`, `stop_grace_period: 20s`,
`read_only: true`, and a writable private tmpfs at `/tmp`. No published ports,
Docker socket, host PID namespace, CAP_NET_ADMIN or server core process access
are required. SIGTERM cancels the current proof, deletes the lease and stops only
the two child clients. `--once` is diagnostic and deletes the lease on exit; a
failed diagnostic returns nonzero.

Validation: `python -m pytest tests/donbaton/test_lte_telemetry_producer.py -q`.
Local tests cover both routes, partial failure, unchanged/regressed counters,
wrong node/multiplier, retries, oldest timestamp/TTL, epoch conflicts, bounded
range/hash validation and cleanup ownership. Actual node→panel persistence and
the private production route configurations must be proven by the operator before
enabling the paid ledger.
