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
- give `node_secret` only to owners of invited worker machines.

Use a secret manager or an authenticated encrypted channel. Never send the
entire config when one authority is sufficient. These are shared keys, so the
coordinator cannot distinguish two holders of the same key or revoke only one
holder. Record distribution outside Mycelium.

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
curl -fsS -X POST "$BASE_URL/v1/executions" \
  -H 'Content-Type: application/json' \
  -H 'X-Pitch-Key: PITCH_KEY' \
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

The response contains an `execution_id`. Read durable state with the viewer
cookie:

```bash
curl -fsS -b viewer.cookies "$BASE_URL/v1/executions/EXECUTION_ID"
```

Placement/confidentiality are recorded scheduling intent, not a sandbox. Every
assigned worker can read its prompt. `local_only` cannot use distributed
placement, and remote-capable work requires explicit consent.

## 5. Join a worker

The machine owner must run the consent gate on the worker machine:

```bash
python join.py "$BASE_URL" --secret NODE_SECRET
```

Registration returns a normalized node ID and a one-time plaintext node
session token. The coordinator stores only its SHA-256 digest; the worker keeps
the token only in memory and sends `X-Node-Session` on polling, heartbeat,
streaming, result, and drain calls. The stock worker automatically re-registers
after coordinator restart, reconnect, expiry, or a machine-readable session
rejection.

A different live claimant for the same normalized node ID receives 409. A
stale/expired session can be reclaimed, after which the old session and attempt
path are rejected. This prevents accidental label collision; it does not prove
physical machine identity because `node_secret` remains shared admission.

## 6. Drain or stop a worker

Programmatic workers that retain their node session can stop new assignment
while allowing current work to finish:

```bash
curl -fsS -X POST "$BASE_URL/nodes/NODE_ID/drain" \
  -H 'X-Node-Secret: NODE_SECRET' \
  -H 'X-Node-Session: NODE_SESSION_TOKEN'
```

Wait until `current_task` is empty, then stop the worker. The stock interactive
worker does not expose its in-memory session token as an operator command; wait
until it says it is waiting for tasks, then press Ctrl+C. Closing a worker that
still holds work causes the lease to be reclaimed and reassigned after the
coordinator detects staleness.

## 7. Check health and logs

Public sanitized checks:

```bash
curl -fsS "$BASE_URL/health"
curl -fsS "$BASE_URL/status.json"
```

A trusted-alpha deployment is ready only when `/health` reports `status: ok`
and `private_routes_protected: true`. Private process identity:

```bash
curl -fsS -b viewer.cookies "$BASE_URL/v1/operator/health"
```

It should report `single_coordinator_lock: true`, the expected mode, and one
instance ID. Read container logs without copying config into the transcript:

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
- rotating `node_secret` blocks old admission credentials. Restart also
  invalidates all process-local node sessions, so workers need the new secret
  to re-register.

Restart interrupts queued/running executions and active attempts. Schedule
node-secret rotation during a drain window. Because the keys are shared,
selective holder revocation requires rotating and redistributing the whole
authority.

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
and a SHA-256 index. The tool does not print configuration values. Store the ZIP
as sensitive data: it contains private prompts/results, artifacts, and static
credentials. Copy it off-host and test restore periodically.

Not backed up because it is process-local: pending queue entries, dispatcher
waits, in-flight coroutine state, connected-node sessions, and plaintext node
session tokens. The database records interrupted work, but backup cannot turn
that work into resumable scheduling state.

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
workers re-register and interrupted work must be retried as a new execution.

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
6. `interrupted` work is retryable state, not a resumable queue entry. Submit a
   new execution after confirming the original cannot still become active.

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
