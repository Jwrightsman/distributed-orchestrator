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

Do not expose port 8000 directly to the public Internet. The three deployment
credentials are instance-wide shared authorities, not user accounts. Durable
per-node bearer enrollment adds individual revocation and attribution, not
public-key or physical-machine identity.

## The three authorities

A trusted-alpha deployment requires three independent random values. Each must
be at least 32 characters, and all three must differ.

| Configuration field | Authority granted to every holder |
| --- | --- |
| `viewer_key` | Read private executions, results, projects, nodes, artifacts, and operator routes; administer shares |
| `pitch_key` | Submit work that consumes coordinator and worker compute |
| `node_secret` | Bootstrap a previously unused durable worker enrollment |

Do not reuse one value for two roles. Give each person or machine only the
authority it needs. The worker generates a distinct enrollment credential at
bootstrap; returning registration and normal session operations do not require
the shared `node_secret`. Enrollment is still a bearer credential and must
travel only over TLS or a private authenticated overlay.

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
Wi-Fi is outside this boundary. `node_enrollment_mode=compat` is explicitly
local-only and any legacy worker session is represented as unenrolled.

## Recommended: private-overlay trusted alpha

Install and authenticate Tailscale, WireGuard, or an equivalent private
overlay on the coordinator before inviting workers. Then deploy on the
coordinator:

```bash
curl -fsSL https://raw.githubusercontent.com/Jwrightsman/distributed-orchestrator/master/deploy.sh \
  | MYCELIUM_PRIVATE_OVERLAY_CONFIRMED=1 bash
```

The confirmation is an operator assertion, not overlay detection. Do not set
it until this host is actually joined to the authenticated overlay.

The script:

- installs the local Docker prerequisites when needed;
- creates or updates `data/config.json` atomically with owner-only permissions
  on POSIX systems;
- generates any missing, short, or duplicate authority with 32 random bytes;
- sets `node_enrollment_mode=required` and records the private-overlay
  transport assertion used by the recommended deployment path;
- preserves valid independent authorities and unrelated settings on rerun;
- upgrades a two-key installation by adding `viewer_key` without rotating a
  valid `node_secret` or `pitch_key`;
- runs strict preflight before launch;
- starts one coordinator process and Ollama; and
- reports success only when `/health` has `status: ok`,
  `private_routes_protected: true`, and `node_enrollment_required: true`.

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

### The overlay still needs TLS

**A worker will not join over plaintext `http://`, overlay or not.** Since
2026-09-05 the worker refuses plaintext transport to every host except loopback,
with no flag, environment variable, or configuration key that relaxes it. An
overlay address is still a network address, and an ACL is an operator assertion
that a contributor cannot verify.

You do not have to give up the overlay. Put a certificate on it — `tailscale
cert` issues one for your tailnet name — and keep the ACL as well; they solve
different problems. Or use the reverse-proxy section below. Either way the
address you hand a contributor starts with `https://`.

### Joining

The owner of each worker machine must explicitly consent before it joins:

```bash
python worker_installer.py
```

The guided installer asks for the address and the invitation code, describes
the model download and CPU/RAM/disk cost in plain language, names every file it
will write, and waits for the owner. It performs the bootstrap enrollment
itself, so **the shared secret is never passed as a command-line argument** —
it is typed with echo off, or read from a file whose permissions are checked
first. After that the machine has its own revocable credential and starts with
`python node.py --server https://ADDRESS` and no secret at all.

`python join.py https://ADDRESS` remains for existing scripted setups, and now
takes the invitation code the same two safe ways the installer does:

| | |
| --- | --- |
| `--ask-secret` | Prompts with the echo off. Nothing reaches argv or shell history. |
| `--secret-file PATH` | Reads it from a file that must be readable only by the running user. |
| `--secret VALUE` | Still works, still exposed: any other user on the machine can read it with `ps`, and the shell records the line in history. It now prints a warning saying exactly that. |

`node.py` takes the same three. Prefer the installer, and send contributors
[JOIN.md](JOIN.md) rather than a command to paste.

**There is no `curl … | bash` installer.** `install.sh` and `install.ps1` were
deleted on 2026-09-05. Piping a URL into a shell gives the person running it no
point at which to read what they are about to run, which is the wrong default
for software whose whole request is "lend me your computer". Contributors clone
the repository and run the installer from inside it.

To leave, `python worker_installer.py uninstall` drains from the coordinator
and removes the credential. An agent must not bypass the consent gate on
someone else's machine.

Pass a coordinator origin only—no path, query, user information, or fragment.
The stock worker intentionally ignores ambient `HTTP_PROXY`/`HTTPS_PROXY`
settings so enrollment and session bearers cannot be inherited by an
unreviewed proxy. Ensure the worker can reach the coordinator directly through
the private overlay or protected TLS endpoint.

Before a consented join, the worker owner may set `model` and a strict
`worker_capability_overrides` object in the worker checkout's `config.json`.
`join.py` uses those settings when it starts `node.py`. For a direct start,
`node.py --model MODEL --capability-overrides PATH` layers a bounded JSON file
over config; `--capabilities` remains for legacy string tags. Override fields
are limited to hardware claims, typed features, executor version, model context
and variant, and maximum context. There is deliberately no model-digest
override and no serial/MAC/device-identifier field.

The worker advertises one immutable descriptor per process session. CPU,
architecture, memory, bounded GPU details, and exact Ollama metadata are
best-effort detections; missing values stay unknown. All detected and overridden
values remain self-reported claims, not attestation or trust. Drain/stop the
worker and start a new session before changing its claim.

## Manual trusted-alpha configuration

The supported generator avoids shell interpolation and never prints generated
values. The example below is valid only after the host has joined an
authenticated private overlay; the explicit argument records that operator
assertion rather than detecting or creating the overlay:

```bash
mkdir -p data
python -c "from config import ensure_trusted_alpha_config as e; e('data/config.json', ollama_url='http://ollama:11434', private_overlay=True)"
python scripts/preflight.py --config data/config.json --state-dir data --mode trusted_alpha
```

The resulting settings include this shape; the placeholders below are not
valid credentials and must never be deployed literally:

```json
{
  "deployment_mode": "trusted_alpha",
  "node_enrollment_mode": "required",
  "private_overlay": true,
  "ollama_url": "http://ollama:11434",
  "viewer_key": "<independent-random-viewer-authority-at-least-32-chars>",
  "pitch_key": "<independent-random-pitch-authority-at-least-32-chars>",
  "node_secret": "<independent-random-node-authority-at-least-32-chars>",
  "public_pitch": false,
  "public_pitch_acknowledged": false,
  "capability_evidence_mode": "off",
  "capability_evidence_min_samples": 5,
  "https_enabled": false,
  "viewer_cookie_secure": false,
  "trust_proxy_headers": false
}
```

The generator writes `.mycelium-trusted-alpha` beside the config. This
out-of-band marker makes a later missing or malformed JSON file fail closed
instead of silently reverting to local defaults. A successfully loaded manual
`deployment_mode: trusted_alpha` config also establishes the marker.

## Operator pre-flight checklist

`scripts/preflight.py` checks the configuration. It cannot check your host, your
certificate, or where your secrets have already been.
[OPERATOR_PREFLIGHT.md](OPERATOR_PREFLIGHT.md) is the list for those — TLS,
secret rotation after any recording, `git log -p` for committed secrets,
firewall to 22/80/443, SSH keys not passwords, unattended upgrades. Work
through it before you invite anybody.

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
and separation, required durable enrollment, declared TLS/private-overlay
transport, public-pitch acknowledgement, HTTPS/cookie coherence, writable
SQLite/artifact/output/project paths, database integrity when one exists, and
availability of the single-coordinator OS lock. `private_overlay=true` is an
operator assertion; preflight cannot inspect Tailscale/WireGuard ACLs.

Configuration loading also validates `capability_evidence_mode` as exactly
`off` or `shadow` and `capability_evidence_min_samples` as an integer from 1
through 1000. The deployment generator leaves evidence mode `off`.

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
  "node_enrollment_mode": "required",
  "private_overlay": false,
  "https_enabled": true,
  "viewer_cookie_secure": true,
  "trust_proxy_headers": false
}
```

Merge them into the existing config; do not replace the authority values.
RC1 does not consume trusted proxy headers, so `trust_proxy_headers: true` is a
preflight error in trusted-alpha mode. Restrict direct access to port 8000 so a
client cannot bypass the proxy. Even with TLS, this remains a small invited
alpha: there are no per-user roles, per-node public keys/attestation,
generated-code sandbox, or high-availability coordinator.

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

## Optional capability-evidence shadow mode

Set `capability_evidence_mode` to `shadow` only when you want protected
counterfactual diagnostics. The coordinator records scoped operational evidence
in either mode; shadow mode additionally evaluates already hard-eligible
candidates after the real assignment. It freezes their assignment-time scopes,
never waits for evidence, and cannot rank, reorder, exclude, or replace
production work. There is no active evidence-routing mode, and
`verify_rate` remains an independent default-off sampled-comparison setting.

After preflight and restart, inspect the viewer-protected aggregate endpoint:

```bash
curl -fsS -b viewer.cookies \
  "$BASE_URL/v1/operator/capability-evidence?limit=100&evidence_role=production"
```

The response reports the configured minimum, cold scopes as
`insufficient_evidence`, and `affects_routing: false`. Agreement means bounded
output-shape agreement, not correctness or trust. Descriptor, selected model,
task class, and evidence-role changes create separate scopes. Only server-owned
lease-expiry and stale-node terminal causes are attributed to workers; caller or
coordinator causes are excluded.

This endpoint is private operational inventory. It returns aggregates rather
than raw observations and the evidence store omits prompt/output bodies, worker
error text, free-form reasons, credentials, nonces, and session secrets. The
rows still live in `events.db` and are included in backups. Contribution points
remain separate accepted-compute records and are never capability evidence.

## Optional distributed tracing

Off by default, and two switches rather than one.

```json
{
  "tracing_enabled": true,
  "tracing_export": false,
  "tracing_endpoint": ""
}
```

`tracing_enabled` lets the coordinator accept a `traceparent` from a worker,
mint one when none arrives, and hand it back - so one cross-machine incident
reads as one thing. Nothing leaves the machine at this setting.

`tracing_export` sends finished spans to `tracing_endpoint` and needs the
optional extra (`pip install opentelemetry-sdk` - the SDK, not
`opentelemetry-api`, which on its own records nothing and is reported as export
being off). **No collector is shipped or configured.** Pointing this at a
collector is a network destination you choose; there is no default.

On a worker, export is that contributor's decision and never a condition of
joining. Turning it on for the coordinator does not turn it on for anyone's
node.

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

### When a worker is refused for its protocol version

Check the window first. It needs no credential:

```bash
curl -s "$BASE_URL/v1/worker-protocol"
```

```json
{"node_protocol_min": "1", "node_protocol_max": "1",
 "supported_worker_protocol_versions": ["1"], "server_version": "0.3.0"}
```

A refused registration returns `426` and says which side is stale. Read
`detail.action`, not just the status:

| `detail.code` | what it means | what to do |
| --- | --- | --- |
| `worker_protocol_version_too_old` | the worker is behind the window | update the worker and rejoin; `detail.action` is `upgrade_worker` |
| `worker_protocol_version_too_new` | the worker is ahead of this coordinator | update the coordinator, or run a worker at a supported version; `detail.action` is `upgrade_coordinator` |
| `invalid_worker_protocol_version` (`422`) | the declared version is not a version token | a hand-edited descriptor; remove the override and let the worker declare its own |

Nothing durable is created by a refusal: no enrollment, no session, no capability
snapshot. Retrying changes nothing until one side moves, and repeated attempts
accumulate no partial state.

Workers already connected are unaffected by a window change. A session
established under a supported version stays valid for its lifetime, so moving the
window does not drop in-flight work.


| Symptom | Action |
| --- | --- |
| Preflight says another coordinator owns the state directory | Stop the other process or select a different state directory; do not delete the lock file to bypass ownership |
| `/health` is `degraded` | Check `docker compose logs ollama` and confirm the configured model pull completed |
| `/health` says private routes are unprotected | Configure a distinct `viewer_key`, run preflight, and restart |
| Preflight rejects legacy-only node admission | Set `node_enrollment_mode` to `required`, upgrade stock workers, and preserve their identity files; do not add a trusted-alpha compatibility bypass |
| Initial worker bootstrap returns 401 | Confirm only that worker received the current `node_secret`; returning workers use their identity file instead |
| Registration says durable enrollment is required | Upgrade the worker and use `--identity-file` if its default user configuration directory is unsuitable |
| Registration says a capability descriptor is required | Upgrade the worker; descriptorless sessions are limited to explicitly unenrolled local compatibility and cannot receive durable enrolled attempts |
| Worker protocol returns a session-specific 401 | The stock worker automatically re-registers; repeated failures require checking coordinator and worker clocks/logs |
| Registration returns 409 | The label or credential belongs to another durable enrollment; do not delete history or reuse the label—inspect the protected enrollment list |
| Registration returns `node_capability_descriptor_conflict` | The live session attempted to change its immutable claim; drain or let current work finish, stop that worker process, and register a fresh session with the intended descriptor |
| A typed task excludes a worker | Inspect viewer-protected `/v1/operator/node-enrollments` with the same bounded requirements and use its stable `reason_codes`; do not infer trust from an eligible result or publish the full descriptor |
| Capability evidence shows `insufficient_evidence` | This is the configured cold-start state, not a negative score; confirm descriptor/model/task-class/role scope and collect more eligible observations without changing routing |
| Shadow counts differ from production assignments | Expected: shadow is a post-assignment counterfactual and `affects_routing` is always false; set mode back to `off` and restart if diagnostics are not wanted |
| Pitch returns 401 | Send the current `pitch_key` on the pitch route |
| Browser logs in but immediately loses the cookie | HTTPS and `viewer_cookie_secure` declarations disagree, or the proxy is not actually serving HTTPS |
| Compose is reachable only locally | This is the safe default; set `MYCELIUM_PUBLISH_ADDRESS` to the private overlay address, not a public wildcard |
