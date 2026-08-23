# Deployment

Mycelium RC1 is intended for a small private trusted alpha: one protected
coordinator and invited workers. It is not a permissionless network, a
multi-tenant service, or a public-Internet-ready application. Workers receive
the prompts assigned to them, and generated artifacts are not sandboxed or
automatically safe to execute.

## Choose a deployment shape

| Shape | Mode | Reachability | Appropriate use |
| --- | --- | --- | --- |
| Local-only development | `local` | Loopback | Development and evaluation on one owned machine |
| Private-overlay trusted alpha | `trusted_alpha` | Tailscale, WireGuard, or equivalent | Recommended invited alpha |
| Internet-facing reverse proxy | `trusted_alpha` | Restricted TLS proxy; app port remains private | Only when an overlay is impractical |

Do not expose port 8000 directly to the public Internet. The three static
credentials are instance-wide shared authorities, not user accounts, and node
admission is not public-key identity.

## The three authorities

A trusted-alpha deployment requires three independent random values. Each must
be at least 32 characters, and all three must differ.

| Configuration field | Authority granted to every holder |
| --- | --- |
| `viewer_key` | Read private executions, results, projects, nodes, artifacts, and operator routes; administer shares |
| `pitch_key` | Submit work that consumes coordinator and worker compute |
| `node_secret` | Register a worker on the instance and obtain a process-local node session |

Do not reuse one value for two roles. Give each person or machine only the
authority it needs. A node session narrows subsequent worker-protocol calls,
but the shared `node_secret` still admits any holder under an available node
label; it is not a per-machine identity credential.

## Local-only development

`local` is the compatibility default. No config file is required when the app
is bound to loopback:

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Or use Compose, which also publishes to loopback by default:

```bash
docker compose up -d --build
```

Empty credentials retain the historical local workflow. Startup and
`/health` warn that private routes are unprotected. Binding local mode beyond
loopback with disabled credentials emits an additional preflight warning. Do
not interpret compatibility mode as a secure LAN default; shared or public
Wi-Fi is outside this boundary.

## Recommended: private-overlay trusted alpha

Install and authenticate Tailscale, WireGuard, or an equivalent private
overlay on the coordinator before inviting workers. Then deploy on the
coordinator:

```bash
curl -fsSL https://raw.githubusercontent.com/Jwrightsman/distributed-orchestrator/master/deploy.sh | bash
```

The script:

- installs the local Docker prerequisites when needed;
- creates or updates `data/config.json` atomically with owner-only permissions
  on POSIX systems;
- generates any missing, short, or duplicate authority with 32 random bytes;
- preserves valid independent authorities and unrelated settings on rerun;
- upgrades a two-key installation by adding `viewer_key` without rotating a
  valid `node_secret` or `pitch_key`;
- runs strict preflight before launch;
- starts one coordinator process and Ollama; and
- reports success only when `/health` has both `status: ok` and
  `private_routes_protected: true`.

Credential values are deliberately not printed, even under shell tracing.
Their storage location and authority names are printed. Transfer individual
values from `data/config.json` through a secret manager or another secure
channel; do not paste the whole file into chat, tickets, or logs.

Compose publishes only on `127.0.0.1` by default. To publish on the
coordinator's private overlay address, create a local `.env` file in the
checkout and recreate the service:

```bash
printf 'MYCELIUM_PUBLISH_ADDRESS=%s\n' "$(tailscale ip -4)" > .env
docker compose up -d
```

Restrict the overlay ACL so only invited operators and workers can reach port
8000. Do not set `MYCELIUM_PUBLISH_ADDRESS=0.0.0.0` as a shortcut.

The owner of each worker machine must explicitly consent before it joins:

```bash
python join.py http://OVERLAY_ADDRESS:8000 --secret NODE_SECRET
```

`join.py` describes the model download and CPU/RAM/disk cost and waits for the
owner. An agent must not bypass that gate on someone else's machine.

## Manual trusted-alpha configuration

The supported generator avoids shell interpolation and never prints generated
values:

```bash
mkdir -p data
python -c "from config import ensure_trusted_alpha_config as e; e('data/config.json', ollama_url='http://ollama:11434')"
python scripts/preflight.py --config data/config.json --state-dir data --mode trusted_alpha
```

The resulting settings include this shape; the placeholders below are not
valid credentials and must never be deployed literally:

```json
{
  "deployment_mode": "trusted_alpha",
  "ollama_url": "http://ollama:11434",
  "viewer_key": "<independent-random-viewer-authority-at-least-32-chars>",
  "pitch_key": "<independent-random-pitch-authority-at-least-32-chars>",
  "node_secret": "<independent-random-node-authority-at-least-32-chars>",
  "public_pitch": false,
  "public_pitch_acknowledged": false,
  "https_enabled": false,
  "viewer_cookie_secure": false,
  "trust_proxy_headers": false
}
```

The generator writes `.mycelium-trusted-alpha` beside the config. This
out-of-band marker makes a later missing or malformed JSON file fail closed
instead of silently reverting to local defaults. A successfully loaded manual
`deployment_mode: trusted_alpha` config also establishes the marker.

## Preflight

For a source checkout whose state is the current directory:

```bash
python scripts/preflight.py
python scripts/preflight.py --json
```

For the Compose layout:

```bash
python scripts/preflight.py \
  --config data/config.json \
  --state-dir data \
  --mode trusted_alpha
```

Preflight returns nonzero for an unsafe trusted-alpha deployment and never
prints credential values. It validates JSON, deployment mode, authority length
and separation, public-pitch acknowledgement, HTTPS/cookie coherence, writable
SQLite/artifact/output/project paths, database integrity when one exists, and
availability of the single-coordinator OS lock.

An active coordinator legitimately holds the lock. Use
`--skip-lock-check` only to validate its other settings while it is running;
the result includes a warning that the lock probe was skipped. Stop the
coordinator and run the full command before a restore or controlled restart.

## Internet-facing reverse proxy

Prefer an overlay. If browser clients cannot use one, keep the application
published on loopback and put a maintained TLS reverse proxy on the same host.
The proxy must:

- use a valid certificate and redirect HTTP to HTTPS;
- forward WebSocket upgrades for `/ws/events`;
- enforce request-body and connection limits;
- restrict operator access where possible; and
- redact `/v1/shares/<token>` paths from access logs, because a share URL is a
  bearer capability.

Set these fields before restarting:

```json
{
  "deployment_mode": "trusted_alpha",
  "https_enabled": true,
  "viewer_cookie_secure": true,
  "trust_proxy_headers": false
}
```

Merge them into the existing config; do not replace the authority values.
RC1 does not consume trusted proxy headers, so `trust_proxy_headers: true` is a
preflight error in trusted-alpha mode. Restrict direct access to port 8000 so a
client cannot bypass the proxy. Even with TLS, this remains a small invited
alpha: there are no per-user roles, per-node public keys, generated-code
sandbox, or high-availability coordinator.

## Viewer login and health

Private APIs accept `X-Viewer-Key`, `Authorization: Bearer`, or a signed
HttpOnly viewer cookie. The browser session endpoint is:

```text
POST /v1/viewer/session
{"viewer_key":"..."}
```

The cookie is signed, short-lived, and does not contain the static key. Rotating
`viewer_key` invalidates existing viewer cookies.

Two public, sanitized checks remain available:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/status.json
```

`/health` is deployment-ready only when `status` is `ok` and
`private_routes_protected` is `true`. With viewer authorization, private
`GET /v1/operator/health` reports the process `instance_id`, deployment mode,
preflight warnings, and whether the coordinator lock is held. It contains no
credential values.

## Public pitch is an explicit exception

`public_pitch` lets unauthenticated visitors spend compute through the bounded
public profile. Trusted-alpha preflight rejects it unless both fields are true:

```json
{
  "public_pitch": true,
  "public_pitch_acknowledged": true
}
```

Rate, task-length, and concurrency limits reduce abuse; they do not eliminate
it. Enable this only for a supervised event and disable it afterward. It does
not make private execution or artifact routes public.

## Updating and recovery

Use the procedures in [Trusted Alpha Runbook](TRUSTED_ALPHA_RUNBOOK.md). Back up
before an update, keep one coordinator per `data/` directory, and expect queued
or running executions to become interrupted rather than resume after restart.
The backup/restore tools and their non-recoverable process-local state are
documented there and in [Operations](OPERATIONS.md).

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Preflight says another coordinator owns the state directory | Stop the other process or select a different state directory; do not delete the lock file to bypass ownership |
| `/health` is `degraded` | Check `docker compose logs ollama` and confirm the configured model pull completed |
| `/health` says private routes are unprotected | Configure a distinct `viewer_key`, run preflight, and restart |
| Worker registration returns 401 | Confirm only that worker received the current `node_secret`; do not send `viewer_key` or `pitch_key` |
| Worker protocol returns a session-specific 401 | The stock worker automatically re-registers; repeated failures require checking coordinator and worker clocks/logs |
| Registration returns 409 | The normalized node ID already has a live different session; choose another ID or wait for the old session to become stale |
| Pitch returns 401 | Send the current `pitch_key` on the pitch route |
| Browser logs in but immediately loses the cookie | HTTPS and `viewer_cookie_secure` declarations disagree, or the proxy is not actually serving HTTPS |
| Compose is reachable only locally | This is the safe default; set `MYCELIUM_PUBLISH_ADDRESS` to the private overlay address, not a public wildcard |
