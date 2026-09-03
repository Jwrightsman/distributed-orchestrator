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

Keep the validator runner in its recommended default mode, or force every
built-in through it:

```json
{
  "validator_execution_mode": "auto",
  "validator_subprocess_timeout_seconds": 10,
  "validator_subprocess_memory_mb": 256,
  "validator_subprocess_request_max_bytes": 2097152,
  "validator_subprocess_response_max_bytes": 32768
}
```

`auto` runs `code_parse`, `structured_json`, and `json_schema` in a child
process and keeps `nonempty`, `artifact_extraction`, `artifact_contract`, and
`file_manifest` inline. `subprocess` runs every current built-in in a child.
`code_parse` receives bounded copied bytes. In forced `subprocess` mode,
`artifact_extraction`, `artifact_contract`, and `file_manifest` receive only
validated normalized logical names and an empty private working directory; the
parent applies root/subtree, regular-file, symlink/special-file,
snapshot-membership, file-count, and path checks but does not copy or rehash
artifact content for those metadata-only checks.
Output-consuming child checks use runner protocol V2. The parent stages the
exact canonical UTF-8 output at the fixed reserved private-workspace path and
sends only its fixed relative path, literal encoding, exact byte length, and
lowercase SHA-256 in the JSON control envelope. The 2 MiB request setting limits
that metadata, not the output body; the execution's existing
`max_output_bytes` remains authoritative up to the canonical 10 MiB maximum.
Do not raise the control limit to compensate for a large valid output.

New parent calls never emit V1. V1 remains explicitly parseable for focused
compatibility tests, and a malformed or failed V2 run is never retried as V1 or
inline. Candidate artifacts cannot occupy the reserved
`__mycelium_validator_input__` namespace.
Trusted-alpha preflight rejects `inline`; it is weaker local-development
compatibility only. In local evidence, an overridden isolated parser is labeled
`inline_compatibility`, not `inline_trusted`. Preflight checks all five fields and rejects booleans,
non-integers, and values outside these strict inclusive ranges: timeout 1–120
seconds, memory 128–1,024 MiB, request 16 KiB–16 MiB, and response 1–256 KiB.
Local mode
warns and restores a bounded default for invalid values.

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

The descriptor's `limits.max_output_bytes` is also a claimed hard placement
limit. A typed node is eligible only when that claim is at least the canonical
task's server-issued `max_output_bytes`; equality is eligible and a lower claim
is reported as `insufficient_output_capacity`. The task value is matching
context derived by the coordinator, not a duplicate resource requirement or a
change to the canonical request hash. After handout, the exact task value stored
on the durable attempt remains authoritative for streaming and settlement. A
larger descriptor claim cannot raise it.

`limits.max_concurrent_execution_units` is only an informational upper-bound
claim today. The coordinator does not maintain or enforce per-node slot counts,
and the stock worker polls and executes sequentially, conservatively staying
within that bound. The coordinator does not issue per-node parallel slots or
capacity-weight assignments.

Trusted-alpha enrolled registration requires this descriptor. Do not use local
descriptorless compatibility as an enrollment workaround; upgrade the worker
if the coordinator returns `node_capability_descriptor_required`. Where local
descriptorless compatibility is explicitly enabled, the matcher does not
fabricate a typed output-capacity claim; the existing legacy matching behavior
continues, while any assigned durable attempt still enforces its server-issued
output limit.

Protect and back up the worker identity file separately. POSIX mode must be
`0600`; a malformed, wrong-coordinator, wrong-label, or dangerously permissive
file fails closed. Windows file protection is best effort through the current
user account/ACLs and must be checked by the worker operator. Enrollment is
durable attribution, not proof of a physical machine or Sybil resistance.
The stock worker ignores ambient HTTP(S) proxy variables to keep these bearers
out of inherited proxies; provide direct private-overlay or reviewed TLS
reachability.

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

For an execution that reached validation, inspect each evidence item's
parent-authored execution mode, runner protocol version, containment level,
duration, and termination reason when present. Treat bounded child detail and
ordinary validation reasons as non-authoritative. The child cannot set
assurance, required/optional status, contract-floor source, or behavioral-
correctness claims. Content-free runner counters distinguish starts, valid
responses, validation failures, timeouts, crashes, malformed responses,
oversized requests and responses, spawn and staging failures, cancellations,
process-tree and staging-workspace cleanup failures, plus output staging,
reference, size, digest, UTF-8, and oversize failures. They reset on
coordinator restart and are operational diagnostics, not lifecycle authority.
Workspace deletion failure records `validator_stage_cleanup_failed` evidence
and increments `staging_cleanup_failures`; it may still leave a stale stage.

The counters and classifications are under the protected operator-health
response's `validator_process.runner.process_local_counters` and
`validator_process.validators` fields. Do not copy that protected response to a
public incident report.

On POSIX, confirm the expected CPU, address-space, output-file, descriptor, and
child-process limits are available. On Windows, expect wall-clock enforcement,
bounded pipes, private validator directories, a fresh process group, and
best-effort cleanup, but not those POSIX resource-limit guarantees. Always
record OS and Python version with a runner incident. Do not diagnose a failure
by switching trusted alpha to `inline`. On Windows, verify that the service
account's temporary root has an appropriately private ACL; validator working
directories and any staged `code_parse` bytes inherit it because POSIX mode bits
do not install a Windows DACL. The same applies to the reserved staged-output
file. A private temporary ACL reduces incidental exposure but a same-user child
boundary is not mandatory access control or guaranteed confidentiality.

### Inspect scoped capability evidence

Operational observations are recorded independently of policy mode. Shadow
evaluation is off by default. To enable shadow diagnostics, add these bounded
settings to `data/config.json`, run preflight, and restart the one coordinator
process:

```json
{
  "capability_evidence_mode": "shadow",
  "capability_evidence_min_samples": 5
}
```

The only modes are `off` and `shadow`; there is no active routing mode. The
minimum must be an integer from 1 through 1000. After the real assignment is
durable, admission freezes bounded non-secret assignment-time claim inputs.
Canonical rematching and candidate-scope construction run from that snapshot in
bounded background work outside the production queue lock, so handout never
waits for scope capture or evidence. Shadow work cannot rank, reorder, or replace
the assignment. `verify_rate` is a separate default-off sampled-comparison
control; trusted-alpha keeps that duplicate path disabled.

Read the protected aggregates with the same viewer cookie:

```bash
curl -fsS -b viewer.cookies \
  "$BASE_URL/v1/operator/capability-evidence?limit=100&evidence_role=production"
```

Optional filters are `enrollment_id`, `descriptor_hash`, `task_class`, and
`evidence_role`. Confirm the response says `affects_routing: false`. Treat
`insufficient_evidence: true` as cold start, not a bad worker. Descriptor,
selected-model, task-class, and evidence-role changes deliberately start a new
scope. Contract-floor rates describe structural assurance; sampled agreement
describes output shape, not correctness or trust. A durable comparison binds an
exact primary attempt rather than any unit sharing the execution.

The same protected surface reports shadow-pipeline operational health. Admission
outcomes are `disabled`, `not_applicable`, `queue_saturated`,
`scope_capture_failed`, and `scheduled`; evaluation outcomes are `completed`,
`evaluator_failed`, `decision_write_failed`, and `cancelled_on_shutdown`.
Inspect durable counts by phase and outcome together with offered, scheduled,
completed, skipped, failed, and pending totals. The report must expose both
`drop_failure_numerator` and `drop_failure_denominator`, not only a percentage:

```text
orphan_evaluation_total = evaluation rows with no persisted admission row
assignment_observation_total = all admission outcomes + orphan_evaluation_total
scheduled = scheduled admissions + orphan_evaluation_total
offered = scheduled + queue_saturated + scope_capture_failed
drop/failure numerator = queue_saturated + scope_capture_failed
                         + evaluator_failed + decision_write_failed
                         + cancelled_on_shutdown
drop/failure denominator = offered
```

The rate is unavailable when `offered` is zero. The report's
`orphan_evaluation_total` identifies terminal evaluation rows whose admission
write is absent; each is counted as one inferred scheduled/offered observation
so the outcome remains in a reproducible numerator and denominator. Bounded
`window_started_at` and `window_ended_at` Unix-timestamp query filters select an
inclusive admission-time cohort and include terminal evaluation records for
those attempts even when they finish after the window end; orphan rows are
selected by their evaluation time. `pending` makes scheduled admissions without
a terminal evaluation visible. Shutdown cancellation is experiment health, not
a node failure. Graceful shutdown has a finite drain: capture that exceeds it is
`scope_capture_failed` with
`coordinator_shutdown_during_scope_capture`, while an already-running decision
write records its true eventual completed/write-failed result rather than a
false cancellation. A drain overrun increments the process-local containment
counter.

Successful observations are small append-only durable rows containing only an
event ID, attempt ID, phase, bounded outcome and reason code, and timestamp.
Process-lifetime fallback counters separately expose
`durable_health_record_write_failure`, `unexpected_containment_failure`, and
`background_task_callback_failure` beside their process `reset_at`. A failure
of this health recording is not recursively recorded and must never affect an
eligible set, selected node, handout, settlement, execution, or attempt count.

Each exact scope also reports `eligible_for_future_active_experiment` and bounded
`blocking_reasons`: `legacy_descriptor_identity`,
`descriptor_identity_unreconstructable`, `immutable_model_identity_missing`,
and `model_identity_unreconstructable`. These are future-experiment
prerequisites only: they do not change hard eligibility, actual assignment,
shadow preference, or collection of otherwise valid shadow evidence. A
digestless typed scope continues collecting when the existing resolver can
otherwise reconstruct it. Do not describe the diagnostic as correctness,
reputation, or trust.

No active experiment is authorized by this report. It would require immutable
model and descriptor identity, every live volume/safety/predictive/fairness
threshold in the experiment specification, a separate accepted ADR, and a
separately reviewed implementation PR. There is still no active evidence mode.

Only `lease_expired` and `node_stale` are worker-attributable terminal failures.
Caller cancellation, execution deadline, payload/stream limits, receipt binding,
enrollment reclaim, session replacement, coordinator restart, supersession, and
unknown causes are excluded. Contribution points remain separate accepted-compute
records. To stop shadow evaluation, set `capability_evidence_mode` back to `off`,
run preflight, and restart; stored observations remain available for diagnosis.

## 7a. Read verification evidence

```bash
curl -s -H "Cookie: mycelium_viewer=$VIEWER_SESSION" \
  "http://127.0.0.1:8000/v1/operator/verification-evidence?limit=50" | python -m json.tool
```

Optional filters: `enrollment_id`, `descriptor_hash`, `task_class`,
`verifier_kind`.

What you are looking at, and what you are not:

- **Not a verdict on a contributor.** It is not reputation, not correctness, and
  not assurance. Nothing here has influenced routing, eligibility, settlement, or
  contribution points, and there is no score or ranking.
- **`insufficient_evidence: true` means "not enough observations",** not "poor".
  Every rate is printed beside its sample count and a Wilson interval; read them
  together or not at all.
- **Agreement is about shape.** A `sampled_reexecution` scope counts whether two
  runs produced comparable output, not whether either was right. It is scoped
  separately from `deterministic_check` outcomes, so the two are never summed.
- **`counts_by_attribution` explains missing results.** Rows attributed to
  `requester_cancelled`, `coordinator_shutdown`,
  `coordinator_persistence_failure`, `pre_assignment_deadline`, or
  `verifier_unavailable` mean the check did not run and the reason was ours or the
  requester's, not the node's. They are excluded from the sample count.
- **`legacy_row_count`** is evidence without enrolled identity. It is held
  separately and never merged into an enrollment's scope.

In trusted alpha this surface will usually be empty, because `verify_rate` is
`0.0` and trusted-alpha mode disables sampled verification regardless. That is
expected. See [ADR 0014](adr/0014-durable-verification-evidence.md).

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

## 8a. Check ledger integrity

### Verify the ledger chain

```bash
python scripts/ledger_chain_admin.py verify
python scripts/ledger_chain_admin.py --state-dir /srv/mycelium verify --json
```

A clean run prints the chained entry count, the pre-chain entry count, and
`chain: intact`. A break prints the first failing index, its entry ID, the reason,
and the expected and observed digests, and exits non-zero. Output carries an
index, an ID, and two digests - no credentials, prompts, outputs, or artifact
contents can appear, because none of them are in the chained columns.

**What a clean result means:** no entry was changed without also recomputing every
link after it. That catches disk corruption, a partial restore, a truncated file,
and a casual edit.

**What it does not mean:** that the recorded work happened, that it was correct,
that anyone is owed anything, or that this coordinator's own operator has not
rewritten the ledger. Someone with write access who edits an entry *and*
recomputes every downstream link produces a chain that verifies clean, and this
mechanism cannot tell. It is tamper evidence, not tamper proofing, and it is not
consensus.

**On a break:** stop trusting standings computed from this ledger until you know
why. Check for a partial restore or disk trouble first; `pre-chain entries` tells
you how much of the ledger the chain covers at all.

## 9. Back up

The coordinator may remain online during backup; SQLite is captured through
its online backup API rather than by copying a live WAL database:

```bash
python scripts/backup.py \
  --state-dir data \
  --destination /secure/backups/mycelium-$(date +%F-%H%M).zip
```

The format-v2 ZIP includes consistent independent snapshots of `events.db` and
its sibling `capability-shadow-health.db`, plus config, projects, output,
execution artifacts, the compatibility ledger when present, build metadata,
and a SHA-256 index. Enrollment IDs, digests, revocation, attribution, rotation
version, and scoped capability observations are in `events.db`; live shadow
decisions and successful operational-health records are in
`capability-shadow-health.db`. Pre-isolation decisions retained by an older
`events.db` or legacy-v1 restore are copied forward idempotently at startup. The
separate writer-lock domains ensure optional experiment writes cannot contend
with authoritative attempt, assignment, or settlement writes. The tool does
not print configuration values.
Store the ZIP as sensitive data: it contains private prompts/results, artifacts,
static credentials, and authentication digests. Copy it off-host and test
restore periodically.

Not backed up because it is process-local: pending queue entries, dispatcher
waits, in-flight coroutine state, connected-node sessions, plaintext node
session tokens, and the three shadow operational fallback counters and their
`reset_at`. Worker identity files and plaintext enrollment credentials are also
not coordinator state; back them up separately on each worker. The database
records interrupted work, but backup cannot turn that work into resumable
scheduling state.

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

Restore accepts current format v2 and legacy format v1. Version 1 predates the
health-database manifest field and restores without
`capability-shadow-health.db`; the upgraded coordinator starts new health
history. In v2, a missing health database is valid only for a backup of
pre-feature state. Restore verifies archive layout, regular-file types, path
confinement, case/Unicode collisions, JSON, every SQLite database present, and
every checksum before mutation. It
rejects traversal, symlinks, special files, duplicates, and unexpected entries;
installation uses staged same-filesystem renames with rollback. Existing
managed state is refused unless `--force` (also `--overwrite`) is explicit.
Use that flag only after making a separate backup and confirming the
coordinator is stopped.

Stale SQLite `-wal`, `-shm`, and journal sidecars for both databases are removed during a
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
   interruption, structured errors, and validation evidence. A
   `validator_timeout`, crash, malformed/oversized response, spawn failure,
   output staging/reference/integrity failure, or process-tree cleanup failure
   is a failed check, not permission to retry that
   isolated validator inline. Confirm the child has been reaped and inspect
   whether the staging directory was removed before investigating host pressure
   or configuration. A `validator_stage_cleanup_failed` result is fail-closed
   evidence and increments `staging_cleanup_failures`; remove only a confirmed
   stale validator directory after confirming no live runner owns it. Never
   include the staged output, private relative path, reference byte length, or
   expected/observed digest in an incident ticket or copied operator response.
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

A validator child still running after the parent reported timeout/cancellation,
or after the coordinator crashed, is a containment incident. POSIX children
have an early hard alarm; Windows has no equivalent child-side alarm or restart
orphan discovery. Record the execution ID when available, timestamps,
OS/Python version, bounded termination reason when present, and process relationship without
capturing command arguments, environment, staged source, raw stderr, or output
content. Stop the affected coordinator once, clean up only the confirmed child
process tree, and keep trusted alpha unavailable until the cleanup path is
understood. The runner is not a same-user security boundary.

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

Treat the protected capability-evidence and shadow-health response the same way.
The new operational rows, counters, metrics, and response omit prompts, output
bodies, worker error text, arbitrary exception messages, credentials, attempt
nonces, session secrets, and artifact contents. Their scoped model, timing, and
outcome aggregates are still private operational inventory. Do not include the
raw database or protected response in a shareable bundle. The health report is
best-effort experiment telemetry, not execution authority or node reputation.

Apply the same handling to validator-runner evidence and process counters. They
are designed to omit content, but their execution IDs, timings, platform
containment levels, and failure distribution remain private operational data.
Do not add staged files, request/response bodies, schemas, raw stderr, child
environment, process arguments, the reserved output-reference path, reference
byte length, or expected/observed output digest to the bundle. Runner protocol
V2 does not change the existing policy for the execution result itself; it only
keeps its private transport metadata out of evidence and diagnostics.

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
