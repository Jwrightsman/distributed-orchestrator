# Trusted Alpha Runbook

This runbook assumes one invited-alpha coordinator, a private overlay or
restricted TLS proxy, and an operator with local access to the coordinator's
state directory. Commands use placeholders; never paste real credentials into
source control, tickets, or shared shell transcripts.

## 1. Preflight and start

For the Compose layout:

```bash
cd /path/to/distributed-orchestrator
python scripts/preflight.py \
  --config data/config.json \
  --state-dir data \
  --mode trusted_alpha
docker compose up -d --build
```

Preflight must pass before the coordinator starts. It intentionally fails if
another process holds the state-directory lock. Do not delete or rename the
lock file to bypass that result.

Compose publishes to loopback unless `MYCELIUM_PUBLISH_ADDRESS` is set. For an
overlay, put its private address in `.env`. For an Internet-facing proxy, keep
the app on loopback, terminate TLS at the proxy, set `https_enabled` and
`viewer_cookie_secure` true, and keep `trust_proxy_headers` false. See
[Deployment](DEPLOY.md).

## 2. Distribute credentials

Treat each static credential as an instance-wide authority:

- give `viewer_key` only to operators/readers who may see all private runs and
  administer artifacts and shares;
- give `pitch_key` only to submitters allowed to spend instance compute; and
- give `node_secret` only for an invited worker's initial enrollment bootstrap.

Use a secret manager or an authenticated encrypted channel. Never send the
entire config when one authority is sufficient. Viewer, pitch, and bootstrap
authorities remain shared within their roles. After bootstrap, each stock
worker has a distinct enrollment credential in its identity file; do not copy
that file back into the coordinator config or reuse it for another node.

## 3. Viewer login

HTTP clients can send `X-Viewer-Key` or `Authorization: Bearer`. Browsers should
exchange the static key for a signed HttpOnly cookie:

```bash
BASE_URL=https://mycelium.example
read -rsp 'viewer_key: ' MYCELIUM_VIEWER_KEY; echo
export MYCELIUM_VIEWER_KEY
python - <<'PY' | curl -fsS -c viewer.cookies \
  -H 'Content-Type: application/json' \
  --data-binary @- "$BASE_URL/v1/viewer/session"
import json, os
print(json.dumps({"viewer_key": os.environ["MYCELIUM_VIEWER_KEY"]}))
PY
unset MYCELIUM_VIEWER_KEY
```

Protect `viewer.cookies` as a credential and remove it when finished. Log out
with `DELETE /v1/viewer/session`. The cookie is signed from `viewer_key`,
contains no plaintext static key, and becomes invalid when that key rotates.

## 4. Submit and observe an execution

Local remains the default. This canonical example explicitly consents to a
distributed direct execution and records `trusted_guild` confidentiality:

```bash
IDEMPOTENCY_KEY="$(python -c 'import uuid; print(uuid.uuid4())')"
curl -fsS -D submission.headers -X POST "$BASE_URL/v1/executions" \
  -H 'Content-Type: application/json' \
  -H 'X-Pitch-Key: PITCH_KEY' \
  -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
  --data-binary @- <<'JSON'
{
  "protocol_version": "1",
  "task": "Draft a small static HTML status page",
  "strategy": "direct",
  "strategy_options": {"kind": "ensemble", "candidates": 1, "concurrency": 1},
  "placement": "distributed",
  "remote_dispatch_consent": true,
  "confidentiality": "trusted_guild",
  "network_policy": "disabled",
  "max_output_bytes": 1048576
}
JSON
```

The response contains an `execution_id`; `submission.headers` contains
`Idempotency-Replayed: false`. If the response is lost, repeat the same body
with the same in-memory or privately retained key. A matching retry returns the
same execution and `Idempotency-Replayed: true`; it does not schedule another
task. Reusing that key for a different validated body returns `409
idempotency_conflict`.

Do not place the key in command history shared with other people, server logs,
diagnostic bundles, metrics, or issue reports. A key identifies one logical
submission, not a person. All trusted-alpha callers using the shared
`pitch_key` share its requester scope.

Read durable state with the viewer cookie:

```bash
curl -fsS -b viewer.cookies "$BASE_URL/v1/executions/EXECUTION_ID"
```

Placement/confidentiality are recorded scheduling intent, not a sandbox. Every
assigned worker can read its prompt. `local_only` cannot use distributed
placement, and remote-capable work requires explicit consent.

Without `Idempotency-Key`, every accepted canonical POST creates a new
execution. Open development mode scopes a key to the direct peer address only;
that is best-effort local behavior and not durable user identity.

## 5. Join a worker

The machine owner must run the consent gate on the worker machine:

```bash
python join.py "$BASE_URL" --secret NODE_SECRET
```

On first join, the stock worker generates a high-entropy enrollment credential,
writes it to its coordinator-scoped private identity file, and bootstraps with
`node_secret`. Use `--identity-file PATH` to choose an explicit protected path;
otherwise the worker uses the documented per-user Mycelium configuration
directory and a filename derived from a hash of the normalized coordinator.
Those directories are `%APPDATA%\Mycelium\nodes` on Windows,
`~/Library/Application Support/Mycelium/nodes` on macOS, and
`$XDG_CONFIG_HOME/mycelium/nodes` or `~/.config/mycelium/nodes` on Linux.
`MYCELIUM_WORKER_CONFIG_DIR` overrides the configuration root. Use a coordinator
origin only: paths, user information, queries, and fragments are rejected.

Registration returns the immutable enrollment ID plus a one-time plaintext
session token. The coordinator stores only domain-separated enrollment and
session-token digests. The worker sends only `X-Node-Session` on normal polling,
heartbeat, streaming, result, and drain calls. After restart/reconnect it uses
the identity-file credential, not `node_secret`, to obtain a new session for
the same enrollment.

Before joining, set the intended `model` in the worker's `config.json`. If
best-effort detection needs correction, add a strict
`worker_capability_overrides` object there; `join.py` passes through the worker
configuration when it starts `node.py`. A direct node start may instead layer
`--capability-overrides PATH` and `--model MODEL`; `--capabilities` remains the
legacy tag flag. Overrides may contain `hardware`, `features`,
`executor_version`, `model_context_tokens`, `model_variant`, and
`max_context_tokens`. Never add serial numbers, MAC addresses, enrollment
secrets, or an invented model digest.

The worker displays the resulting descriptor version and hash. It reuses that
one claim across reconnects in the same process. Architecture, CPU, memory,
bounded GPU data, and exact Ollama metadata are best-effort; unavailable values
remain unknown. Detection and overrides are claims, not measurement,
attestation, trust, or correctness.

Trusted-alpha enrolled registration requires this descriptor. Do not use local
descriptorless compatibility as an enrollment workaround; upgrade the worker
if the coordinator returns `node_capability_descriptor_required`.

Protect and back up the worker identity file separately. POSIX mode must be
`0600`; a malformed, wrong-coordinator, wrong-label, or dangerously permissive
file fails closed. Windows file protection is best effort through the current
user account/ACLs and must be checked by the worker operator. Enrollment is
durable attribution, not proof of a physical machine or Sybil resistance.
The stock worker ignores ambient HTTP(S) proxy variables to keep these bearers
out of inherited proxies; provide direct private-overlay or reviewed TLS
reachability.

## 6. Drain or stop a worker

Programmatic workers that retain their node session can stop new assignment
while allowing current work to finish:

```bash
curl -fsS -X POST "$BASE_URL/nodes/NODE_ID/drain" \
  -H 'X-Node-Session: NODE_SESSION_TOKEN'
```

Wait until `current_task` is empty, then stop the worker. The stock interactive
worker does not expose its in-memory session token as an operator command; wait
until it says it is waiting for tasks, then press Ctrl+C. Closing a worker that
still holds work causes the lease to be reclaimed and reassigned after the
coordinator detects staleness.

Changing a capability descriptor requires the same drain/stop procedure and a
fresh process session. A `409 node_capability_descriptor_conflict` is not a
retry signal for the old session. Confirm `current_task` is empty, stop the
worker, change its configuration, then restart it and verify the new descriptor
hash in the protected operator view. Historical attempts keep the prior hash.

## 7. Check health and logs

Public sanitized checks:

```bash
curl -fsS "$BASE_URL/health"
curl -fsS "$BASE_URL/status.json"
```

A trusted-alpha deployment is ready only when `/health` reports `status: ok`,
`private_routes_protected: true`, and `node_enrollment_required: true`. Private
process and enrollment views:

```bash
curl -fsS -b viewer.cookies "$BASE_URL/v1/operator/health"
curl -fsS -b viewer.cookies "$BASE_URL/v1/operator/node-enrollments"
```

It should report `single_coordinator_lock: true`, the expected mode, and one
instance ID. Enrollment entries expose the descriptor version/hash, normalized
claim, snapshot count, legacy worker/server tag provenance, and
`hard_requirement_eligibility` with stable `reason_codes`. This view is
viewer-protected; `/health` and `/status.json` deliberately omit full hardware
and model claims. Read container logs without copying config into the transcript:

```bash
docker compose ps
docker compose logs --tail 200 orchestrator ollama
```

Logs and events can contain operational identifiers and failure details; task
content may be sensitive. Share only the minimum necessary excerpt. Reverse
proxy access logs must redact share-token path segments.

## 8. Rotate credentials

Back up first. Rotate one authority at a time with a local command that writes
atomically and does not print the new value:

```bash
python - viewer_key <<'PY'
import secrets, sys
import config

name = sys.argv[1]
if name not in {"viewer_key", "pitch_key", "node_secret"}:
    raise SystemExit("invalid authority name")
settings = config.load("data/config.json", strict=True)
settings[name] = secrets.token_urlsafe(32)
config.save(settings, "data/config.json")
PY
python scripts/preflight.py --config data/config.json --state-dir data \
  --mode trusted_alpha --skip-lock-check
docker compose restart orchestrator
```

Retrieve the new value locally and redistribute only through the secure channel
used for that authority. Effects:

- rotating `viewer_key` invalidates every viewer cookie;
- rotating `pitch_key` rejects future submissions using the old value; and
- rotating `node_secret` blocks old *bootstrap* admission. Already enrolled
  workers continue returning with their individual credentials.

Restart interrupts queued/running executions and active attempts. Schedule
shared-authority rotation during an appropriate maintenance window.

Rotate or revoke one enrollment without touching other workers:

```bash
python scripts/node_enrollment_admin.py --state-dir data list
python scripts/node_enrollment_admin.py --state-dir data revoke ENROLLMENT_ID \
  --reason "operator offboarded"
python scripts/node_enrollment_admin.py --state-dir data rotate ENROLLMENT_ID \
  --coordinator "$BASE_URL" \
  --identity-output /secure/transfer/unused-node-identity.json
```

The command never prints the new credential and refuses an existing output by
default. For planned rotation, drain and stop the worker first. Deliver the
replacement identity file through an authenticated secret channel, replace the
worker's coordinator-scoped file, restart it, and verify that the same
enrollment ID returns at the incremented credential version. Remove the
transfer copy afterward.

If the command says the commit was not confirmed, retain the prepared output
and rerun the exact command with `--resume-existing` to converge on the same
candidate. If a committed output is lost, use a different unused output path
and rotate again; the database cannot recover plaintext. Rotation/revocation is
observed on the enrollment's next authenticated operation. A 30-second janitor
check is the nominal maximum for an otherwise idle live coordinator, subject
to ordinary scheduler delay and durable-store availability; failed checks are
diagnosed and retried. Active leases are reclaimed safely after invalidation.

## 9. Back up

The coordinator may remain online during backup; SQLite is captured through
its online backup API rather than by copying a live WAL database:

```bash
python scripts/backup.py \
  --state-dir data \
  --destination /secure/backups/mycelium-$(date +%F-%H%M).zip
```

The versioned ZIP includes a consistent `events.db` snapshot, config, projects,
output, execution artifacts, compatibility ledger when present, build metadata,
and a SHA-256 index. Enrollment IDs, digests, revocation, attribution, and
rotation version are in SQLite. The tool does not print configuration values.
Store the ZIP as sensitive data: it contains private prompts/results, artifacts,
static credentials, and authentication digests. Copy it off-host and test
restore periodically.

Not backed up because it is process-local: pending queue entries, dispatcher
waits, in-flight coroutine state, connected-node sessions, and plaintext node
session tokens. Worker identity files and plaintext enrollment credentials are
also not coordinator state; back them up separately on each worker. The
database records interrupted work, but backup cannot turn that work into
resumable scheduling state.

## 10. Restore

Restore into an empty state directory when possible. Stop the coordinator so
the OS lock and all open state are released:

```bash
docker compose stop orchestrator
python scripts/restore.py /secure/backups/mycelium-YYYY-MM-DD-HHMM.zip \
  --state-dir data
python scripts/preflight.py --config data/config.json --state-dir data \
  --mode trusted_alpha
docker compose up -d
```

Restore verifies archive layout, regular-file types, path confinement,
case/Unicode collisions, JSON, SQLite, and every checksum before mutation. It
rejects traversal, symlinks, special files, duplicates, and unexpected entries;
installation uses staged same-filesystem renames with rollback. Existing
managed state is refused unless `--force` (also `--overwrite`) is explicit.
Use that flag only after making a separate backup and confirming the
coordinator is stopped.

Stale SQLite `-wal`, `-shm`, and journal sidecars are removed during a
successful replacement. Restored process-local queues/sessions do not exist;
durable enrollment IDs/revocations remain, workers authenticate for new
sessions, and interrupted work must be retried as a new execution.

The snapshot is the authority as of its capture time: only revocations and
rotations inside it remain. An older restore can re-enable a worker revoked
later, restore an older credential digest or shared `node_secret`, leave current
worker identity files at incompatible versions, and restore a
`private_overlay=true` assertion that is false on the new host. Before reopening
worker access, reconcile all enrollment changes since the snapshot, revoke or
rotate as needed, distribute matching identity files, validate the current
overlay/TLS controls, and rerun preflight.

## 11. Update

Use a clean checkout and a recoverable backup:

```bash
python scripts/backup.py --state-dir data --destination /secure/backups/
git pull --ff-only
docker compose build
python scripts/preflight.py --config data/config.json --state-dir data \
  --mode trusted_alpha --skip-lock-check
docker compose stop orchestrator
python scripts/preflight.py --config data/config.json --state-dir data \
  --mode trusted_alpha
docker compose up -d
```

Then check `/health`, private operator health, logs, and the expected build
fingerprint in `/status.json`. A restart marks queued/running canonical and
legacy work interrupted/retryable; it does not resume it. The deployment script
is safe to rerun and performs the same credential-preserving preflight path.

## 12. Revoke shares

Creating a share returns its plaintext random token once. SQLite stores only a
hash. Treat the returned URL as a bearer credential and avoid proxy/browser
history where possible. List active metadata (never plaintext tokens):

```bash
curl -fsS -b viewer.cookies \
  "$BASE_URL/v1/executions/EXECUTION_ID/shares"
```

Revoke one or all:

```bash
curl -fsS -X DELETE -b viewer.cookies \
  "$BASE_URL/v1/executions/EXECUTION_ID/shares/SHARE_ID"
curl -fsS -X DELETE -b viewer.cookies \
  "$BASE_URL/v1/executions/EXECUTION_ID/shares"
```

Revoked, expired, and invalid capabilities all return the same public not-found
shape. Revocation prevents future access; it cannot recall data a recipient
already downloaded.

## 13. Handle stuck, cancelled, or interrupted work

1. Read `GET /v1/executions/{id}` with viewer authorization.
2. Distinguish `lifecycle_status` from validation and assurance. A running or
   queued lifecycle is not evidence of correctness; a completed lifecycle can
   still be unverified or partial.
3. Inspect `deadline_at`, unit summaries, placement telemetry, cancellation,
   interruption, and structured errors.
4. Cancel idempotently if work should stop:

   ```bash
   curl -fsS -X POST -b viewer.cookies \
     "$BASE_URL/v1/executions/EXECUTION_ID/cancel"
   ```

5. Late worker results after cancellation, expiry, reclaim, or restart are
   rejected and quarantined for bounded diagnostics; they do not wake dispatch
   or earn contribution points.
6. `interrupted` work is retryable state, not a resumable queue entry. Reusing
   the original idempotency key returns that same interrupted execution and
   does not restart it. After confirming the original cannot still become
   active, intentionally submit replacement work with a new key or no key.
7. A `503` with `execution_persistence_unavailable` means the requested
   authoritative state was not committed. Do not treat cancellation or
   completion as successful; read the execution again after storage health is
   restored. An `idempotency_consistency_error` requires sanitized diagnostics
   and database integrity investigation rather than deleting the mapping.

If the coordinator itself is unresponsive, collect diagnostics, stop it once,
run full preflight, and restart. Do not start a second process against the same
directory as a recovery technique.

## 14. Collect a diagnostic bundle

Start with secret-free or sanitized material:

```bash
mkdir -p diagnostic
python scripts/preflight.py --config data/config.json --state-dir data \
  --mode trusted_alpha --json --skip-lock-check > diagnostic/preflight.json
curl -fsS "$BASE_URL/health" > diagnostic/health.json
curl -fsS "$BASE_URL/status.json" > diagnostic/status.json
docker compose ps > diagnostic/compose-ps.txt
docker compose logs --tail 300 orchestrator ollama > diagnostic/logs.txt 2>&1
```

Review every file before sharing. Logs can contain task or node details. Do not
include `config.json`, viewer cookies, share URLs/tokens, the raw database,
backups, prompts, or generated artifacts unless the recipient is authorized
and the bundle is encrypted. Record timestamps, build fingerprint, OS, Python,
Docker, and reproduction steps separately.

Treat normalized capability descriptors as private hardware/model inventory.
Do not add the protected enrollment response or override JSON to a shareable
bundle unless the recipient is authorized and the inventory is necessary.

Do not add idempotency keys, attempt nonces, or node session tokens to the
bundle. Persistence failure logs should identify only the execution, commit
phase, bounded attempt count, and safe error type.

## 15. Uninstall

Drain workers, take and verify a final backup, then stop the deployment:

```bash
docker compose down
```

This leaves `data/` and the Ollama model volume recoverable. Remove the checkout,
state directory, backup archives, overlay ACL/device entry, and Docker volume
only when the machine owner explicitly chooses permanent deletion. `docker
compose down -v` deletes the model volume; deleting `data/` destroys config,
database, projects, and artifacts. Neither operation is required merely to stop
participating.
