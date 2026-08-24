# Access control for the trusted alpha

Mycelium uses three independent instance-wide credentials and explicit share
capabilities. This is intentionally smaller than an account system, but it
prevents the old failure mode where knowing the server address implied being
able to read every task, result, event, project, and machine record.

## Authorities

| Authority | Configuration | Accepted transport | Scope |
| --- | --- | --- | --- |
| Viewer | `viewer_key` | `X-Viewer-Key`, exact Bearer token, or signed session cookie | private reads and trusted operator actions |
| Pitcher | `pitch_key` | `X-Pitch-Key` | canonical and compatibility task submission |
| Worker admission | `node_secret` | `X-Node-Secret` | instance-wide permission to register and use the worker protocol |
| Node session | server-issued bearer token | `X-Node-Session` | one current normalized node registration; required in addition to admission after registration |
| Share holder | generated share token | token in `/v1/shares/{token}` URL | one redacted execution and optional filtered artifacts |

These authorities are deliberately not interchangeable. A worker does not gain
private history access. A pitcher does not gain artifact access. A viewer does
not automatically know the node or pitch secret.

All three configured static secrets default to empty in `deployment_mode:
local` for compatibility. An empty static secret disables its corresponding
check. `trusted_alpha` startup requires all three, at least 32 characters each,
and pairwise distinct. The deployment generator preserves valid independent
values and writes them atomically without printing them. Do not expose local
fail-open configuration to an untrusted network.

## Viewer credentials

### Header or Bearer token

Send either:

```http
X-Viewer-Key: YOUR_VIEWER_KEY
```

or:

```http
Authorization: Bearer YOUR_VIEWER_KEY
```

Configured static keys are compared in constant time.

### Browser session

Exchange the key for a signed cookie:

```http
POST /v1/viewer/session
Content-Type: application/json

{"viewer_key":"YOUR_VIEWER_KEY"}
```

The response sets `mycelium_viewer` as an HttpOnly, SameSite=Lax cookie. The
cookie contains issue time, expiry, and a random nonce, signed with HMAC-SHA256
using `viewer_key`; it never contains the static key. Default lifetime is eight
hours and the implementation clamps it between one minute and seven days.
Rotating `viewer_key` invalidates every existing cookie.

The cookie is marked Secure when the request is HTTPS or
`viewer_cookie_secure` is true. Set `https_enabled=true` and
`viewer_cookie_secure=true` explicitly for a TLS reverse-proxy deployment.
Trusted-alpha RC1 does not consume trusted proxy headers, so
`trust_proxy_headers=true` fails preflight; do not rely on a forwarded scheme
to repair an incoherent cookie configuration.

Log out with:

```http
DELETE /v1/viewer/session
```

Sessions are stateless. There is no per-session server revocation list; rotate
the viewer key to invalidate all sessions before their expiry.

## Route matrix

The viewer middleware is deny-by-default when `viewer_key` is configured. Its
public exceptions are method-aware.

### Deliberately public without a viewer credential

| Method | Path | Data exposed |
| --- | --- | --- |
| GET | `/` | landing page |
| GET | `/try` | keyless pitch page/state |
| GET | `/static/*` | static assets |
| GET | `/health` | sanitized liveness, counts, model names, viewer-protection warning |
| GET | `/status.json` | sanitized public status/build fingerprint |
| GET | `/v1/shares/{token}` | one redacted execution capability |
| GET | `/v1/shares/{token}/artifacts*` | filtered artifacts only when the share permits them |
| POST | `/public/pitch` | fixed server-owned public profile, only when enabled |
| POST | `/v1/viewer/session` | credential exchange |
| DELETE | `/v1/viewer/session` | cookie deletion |

`/status.json` and `/health` do not expose task text, result text, hostnames,
hardware detail, attempt ids, nonces, or project ids. `/health` intentionally
reports `private_routes_protected` and a warning list.

### Separately authenticated protocol routes

These routes bypass the viewer middleware because a different authority owns
them:

| Method | Path | Separate control |
| --- | --- | --- |
| POST | `/v1/executions` | `pitch_key` + pitch rate limit |
| POST | `/pitch`, `/pitch/async`, `/pitch/distributed` | `pitch_key` + pitch rate limit |
| POST | `/nodes/register` | `node_secret` |
| GET | `/tasks/next` | `node_secret` + current node session |
| POST | `/tasks/{id}/result`, `/tasks/{id}/stream`, `/tasks/{id}/tokens` | `node_secret` + current node session + active-attempt binding |
| POST | `/nodes/{id}/heartbeat`, `/nodes/{id}/drain` | `node_secret` + current node session |

If the corresponding pitch or node secret is empty in local mode, its static
admission check is open even when viewer protection is enabled; node-session
requirements after registration still apply. Viewer authentication is not a
substitute for configuring the protocol credential.

## Canonical submission requester scope

`Idempotency-Key` on `POST /v1/executions` is scoped separately from viewer
access. The endpoint first performs its ordinary pitch authentication, request
validation, and rate limiting. When `pitch_key` is configured, a
domain-separated digest of that configured credential is the requester scope.
All holders of the shared pitch credential therefore share one scope; the
system does not infer individual identity among them.

When pitching is open in local development mode, the direct ASGI peer host is
hashed as a best-effort scope. Mycelium does not consume `X-Forwarded-For`,
`Forwarded`, or similar headers. NAT, proxying, and address changes make peer
scoping unsuitable for authorization, accounting, abuse attribution, or
durable user identity.

Only the domain-separated scope digest, idempotency-key digest, canonical
request digest, execution ID, and creation time are stored. The raw pitch
credential and raw idempotency key must not appear in events, application logs,
diagnostics, or metrics. Requester-scoped idempotency prevents duplicate
canonical submissions; it grants no read access and does not make model or
worker side effects exactly once.

### Viewer-protected

Every other method/path requires viewer authorization, including:

- canonical execution reads, cancellation, artifact manifests/files/ZIPs,
  artifact sealing, share creation/listing, and single/all share revocation;
- `/jobs*`, `/events`, and `/ws/events`;
- `/nodes`, `/status`, `/node/*`, `/metrics`, `/ledger`, and `/standings`;
- `/history*`, `/gallery`, `/run/*`, and legacy `/share/*` redirects;
- `/projects*`, `/v1/operator/health`, and dashboard/operator pages.

An unauthorized HTTP request returns `401` with `WWW-Authenticate: Bearer`.
An unauthorized event WebSocket is closed before acceptance with code `4401`.

## Node registration sessions

`node_secret` answers only “may this client join this coordinator?” Successful
`POST /nodes/register` also returns normalized `node_id`, non-secret
`session_id`, one-time plaintext `session_token`, `session_started_at`, and
`session_expires_at`. The coordinator retains only the token's SHA-256 digest;
the stock worker keeps plaintext in memory and never writes it to config.

Sessions last at most 24 hours and are process-local. Registration presenting
the exact live `X-Node-Session` is idempotent. A different live claimant for
the same normalized ID receives `409 node_id_in_use`; an expired or 90-second
stale session can be reclaimed, invalidating its old token and closing/requeuing
work bound to that session. Coordinator restart invalidates all sessions, and
the stock worker automatically re-registers after a machine-readable `401`
with `action=register_again`.

Every handout records `assigned_session_id` in the durable attempt. Session
authorization supplements attempt ID, nonce, node, execution, unit, kind,
contract, state, and lease binding; it never replaces those checks. A session
token is still a bearer credential, not public-key identity or proof of a
physical machine. Any `node_secret` holder can register another available ID.

Private node views expose `session_id`, start/expiry, draining/current-task
state, session counters, and durable lifetime contribution counters. They MUST
NOT expose `session_token`. Compatibility `tasks_completed` and
`credits_earned` are session projections; new clients use the explicit session
and lifetime fields.

## Share capabilities

Viewer-authorized callers create a share with:

```http
POST /v1/executions/{execution_id}/shares
Content-Type: application/json
X-Viewer-Key: YOUR_VIEWER_KEY

{
  "expires_in_seconds": 86400,
  "allow_artifact_download": false,
  "redact_node_identity": true,
  "include_candidate_details": false
}
```

The default expiry is seven days. A numeric expiry must be between 60 seconds
and 30 days; JSON `null` creates a non-expiring share. The token is generated
from 32 random bytes and returned only in the creation response. SQLite stores
only its SHA-256 hash. Keep the token like a password.

Public use:

```http
GET /v1/shares/{token}
GET /v1/shares/{token}/artifacts
GET /v1/shares/{token}/artifacts/{relative_path}
GET /v1/shares/{token}/download
```

The last three require `allow_artifact_download=true`. Invalid, expired, and
revoked tokens all return the same `404` shape. A share token cannot authorize
`/v1/executions/{other_id}`, private events, project memory, or any other
execution.

Revoke with viewer authorization:

```http
DELETE /v1/executions/{execution_id}/shares/{share_id}
```

List active metadata or revoke every share for the execution without returning
plaintext tokens:

```http
GET /v1/executions/{execution_id}/shares
DELETE /v1/executions/{execution_id}/shares
```

List records include share ID, creation/expiry/revocation/last-access times and
the artifact, node-redaction, and candidate-detail flags. The token exists only
in the create response. Public capability responses set `Cache-Control:
no-store`, `Referrer-Policy: no-referrer`, and `X-Content-Type-Options:
nosniff`. Server unhandled-error logging applies `redact_share_token_path()`.
Uvicorn and reverse-proxy access-log configuration remains an operator
responsibility; both must avoid raw token paths because the URL itself is a
credential.

The public execution projection is allowlist-based. It omits job/project ids,
absolute paths, raw output references, attempt ids, nonces, credit records,
private telemetry, raw logs, and unbounded validator diagnostics. It includes
task text and a bounded output preview because sharing those is the purpose of
the capability. Node identities are hidden by default; candidate detail is off
by default.

Shares are live views, not immutable snapshots. Revocation prevents future
server reads but cannot retract copied content. Artifact retention can remove
files while the execution share itself remains valid.

## Keyless public pitching

`public_pitch` is off by default. In trusted-alpha mode it is rejected unless
`public_pitch_acknowledged=true` records the operator's explicit abuse-risk
acceptance. When enabled, `POST /public/pitch` accepts a
body containing only `task`. Any supplied strategy, candidate, placement,
project, validator, requirements, or confidentiality field is rejected rather
than silently honored.

The server-owned profile is:

| Field | Value |
| --- | --- |
| strategy | `direct` (one ensemble candidate) |
| candidates / concurrency | 1 / 1 |
| placement | `local` |
| confidentiality | `local_only` |
| project | none |
| timeout | 120 seconds total |
| output cap | 65,536 bytes |
| network-policy intent | `disabled` (recorded, not sandbox-enforced) |

Admission limits are two requests per source IP per hour, one active public
execution per source, three active public executions globally, and one global
local inference slot. The source-IP limit depends on correct reverse-proxy
configuration. The response includes a one-hour share token with node identity
redacted and artifact download enabled; it does not make the private job or
execution routes public.

## Client configuration

The CLI and MCP adapter read:

```text
PITCH_KEY=...
VIEWER_KEY=...
```

`PITCH_KEY` is sent on submissions. `VIEWER_KEY` is sent for private job,
event, node, and result reads. `/health` uses the stable public
`nodes_online: integer` schema; detailed `/nodes` output requires the viewer
key. Events are flat objects, not nested under `data`.

Canonical HTTP clients may also send `Idempotency-Key`. They must retain it
with the exact logical request, reuse it only for transport retries, and inspect
`Idempotency-Replayed`. A matching retry returns the existing execution; a
changed request under the same scope/key returns `409 idempotency_conflict`.
CLI, MCP, and compatibility endpoints do not adopt this header in Theme 1.

## Fail-open warning and deployment checklist

When `viewer_key` is empty, the middleware deliberately permits private routes
for backwards-compatible local development. The server logs a warning and
`/health` returns:

```json
{
  "private_routes_protected": false,
  "warnings": [
    "viewer_key is not configured; task-, result-, project-, and machine-sensitive routes are unprotected"
  ]
}
```

Before binding beyond localhost:

1. Set `deployment_mode=trusted_alpha` and configure independent 32+-character
   `viewer_key`, `pitch_key`, and `node_secret` values.
2. Put TLS in front of the app or use a private overlay network. The application
   does not provide HTTPS itself.
3. Set `https_enabled=true` and `viewer_cookie_secure=true` behind HTTPS; leave
   unsupported proxy-header trust off.
4. Run `scripts/preflight.py`; exactly one coordinator must own the state
   directory.
5. Keep `public_pitch=false` unless you set its acknowledgement and intentionally
   accept public compute use.
6. Require both `/health.status == "ok"` and
   `/health.private_routes_protected == true`.
7. Confirm unauthenticated private HTTP returns `401` and unauthenticated
   `/ws/events` closes with `4401`.
8. Rotate any shared key after suspected disclosure. Node and pitch keys do not
   currently have individual-holder revocation.
9. Confirm application and proxy logs do not capture idempotency, pitch,
   viewer, node, session, attempt, or share credentials.

## Residual limitations

This is one shared viewer role, not multi-user authentication or authorization.
There is no password hashing flow, account recovery, MFA, audit identity,
per-project ACL, per-execution owner, session list, or individual viewer
revocation. Static secrets and share tokens are bearer credentials. TLS,
secret distribution, rotation procedures, proxy trust, and host security remain
operator responsibilities. Node sessions reduce active-label collision but do
not add durable or cryptographic machine identity. Share revocation cannot
recall copied content, and one coordinator process remains the only supported
owner of a state directory. A shared pitch key creates one shared submission
scope, while open-mode peer scoping is development-grade rather than user
identity.
