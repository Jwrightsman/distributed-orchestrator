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
| Worker | `node_secret` | `X-Node-Secret` | node registration, polling, streams, and result submission |
| Share holder | generated share token | token in `/v1/shares/{token}` URL | one redacted execution and optional filtered artifacts |

These authorities are deliberately not interchangeable. A worker does not gain
private history access. A pitcher does not gain artifact access. A viewer does
not automatically know the node or pitch secret.

All three configured static secrets default to empty for local-development
compatibility. An empty secret disables its corresponding check. Do not expose
that configuration to an untrusted network.

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
`viewer_cookie_secure` is true. A reverse proxy must pass the correct scheme if
the application is expected to infer HTTPS. Prefer setting
`viewer_cookie_secure=true` in an HTTPS deployment.

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
| GET | `/tasks/next` | `node_secret` |
| POST | `/tasks/{id}/result`, `/tasks/{id}/stream` | `node_secret` + active-attempt binding |

If the corresponding pitch or node secret is empty, that route is open even
when viewer protection is enabled. Viewer authentication is not a substitute
for configuring the protocol credential.

### Viewer-protected

Every other method/path requires viewer authorization, including:

- canonical execution reads, cancellation, artifact manifests/files/ZIPs,
  share creation, and share revocation;
- `/jobs*`, `/events`, and `/ws/events`;
- `/nodes`, `/status`, `/node/*`, `/metrics`, `/ledger`, and `/standings`;
- `/history*`, `/gallery`, `/run/*`, and legacy `/share/*` redirects;
- `/projects*` and dashboard/operator pages.

An unauthorized HTTP request returns `401` with `WWW-Authenticate: Bearer`.
An unauthorized event WebSocket is closed before acceptance with code `4401`.

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

`public_pitch` is off by default. When enabled, `POST /public/pitch` accepts a
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

1. Configure long, independent `viewer_key`, `pitch_key`, and `node_secret`
   values for the authorities you intend to expose.
2. Put TLS in front of the app or use a private overlay network. The application
   does not provide HTTPS itself.
3. Set `viewer_cookie_secure=true` behind HTTPS.
4. Keep `public_pitch=false` unless you intentionally accept public compute use.
5. Verify `/health` reports `private_routes_protected: true`.
6. Confirm unauthenticated private HTTP returns `401` and unauthenticated
   `/ws/events` closes with `4401`.
7. Rotate any shared key after suspected disclosure. Node and pitch keys do not
   currently have individual-holder revocation.

## Residual limitations

This is one shared viewer role, not multi-user authentication or authorization.
There is no password hashing flow, account recovery, MFA, audit identity,
per-project ACL, per-execution owner, session list, or individual viewer
revocation. Static secrets and share tokens are bearer credentials. TLS,
secret distribution, rotation procedures, proxy trust, and host security remain
operator responsibilities.
