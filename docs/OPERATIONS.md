# Operations and Failure Boundaries

This document describes what the trusted-alpha coordinator actually preserves
and what an operator must not infer from it. The supported target is one
coordinator on a local filesystem serving a small private trusted alpha.

## Non-negotiable operating limits

- Exactly one coordinator process may own one state directory.
- Scheduler queues, connected workers, dispatcher waits, and node sessions are
  process-local.
- Restart interrupts queued/running work; it does not resume scheduling.
- Required execution snapshots commit before live-cache, lifecycle-event,
  callback, compatibility-mirror, response, or terminal artifact/share
  publication.
- Canonical idempotency preserves one submission identity; it does not restart
  lost work or make external side effects exactly once.
- `node_secret` is shared admission, not public-key machine identity.
- Workers can read every prompt assigned to them.
- Placement, confidentiality, and network policy are recorded intent, not an
  operating-system or network sandbox.
- A sealed artifact manifest is local hash evidence, not a signature or an
  attestation of origin, safety, or correctness.
- Contribution points record accepted compute. They are not money, payment,
  candidate selection, or proof of correctness.
- Generated code and other artifacts are not automatically safe to execute.

## State and durability map

| State | Durable | Restart behavior |
| --- | --- | --- |
| Canonical executions, lifecycle, validation, telemetry | SQLite | Retained; queued/running become `interrupted` and retryable |
| Scoped canonical submission mappings | SQLite | Retained indefinitely; replay resolves to the same execution, including after interruption |
| Attempt authority, nonce digests, settlement receipts, quarantine | SQLite | Retained; active attempts become `interrupted`; exact settled replay remains durable |
| Shares and revocation metadata | SQLite | Retained; plaintext share token is never stored |
| Contribution records | SQLite | Authoritative; JSON ledger is only a compatibility projection |
| Artifact roots, entries, hashes, roles, seal state | SQLite plus files | Retained if both database and artifact trees are restored together |
| Projects and compatibility output | Files, gated by canonical SQLite authority for current runs | Retained by state directory/backup; staged current output is not completion truth |
| Pending worker queue and dispatcher waits | Memory | Lost; corresponding work is marked interrupted where durable identity exists |
| Connected node registry and node sessions | Memory | Lost; workers must register again |
| Plaintext attempt nonce or node session token | Client memory only | Not recoverable by the coordinator |

Do not copy only `events.db` and assume a complete recovery. Artifacts,
projects, config, output, and the compatibility ledger are part of the backup
set. Conversely, copying a live WAL database file directly is not a consistent
SQLite snapshot.

## Single-coordinator ownership

Startup rejects common multi-worker launch settings and acquires an exclusive
OS advisory lock at `.mycelium-coordinator.lock` in the state directory. The
lock is obtained before database migrations, restart reconciliation, or
background cleanup and is held until lifespan shutdown. POSIX uses `flock`;
Windows uses a nonblocking `msvcrt` byte-range lock.

The lock file contains bounded diagnostic metadata: random process instance
ID, PID, start time, and deployment mode. Metadata is not ownership authority;
the kernel lock is. The file is intentionally not unlinked on shutdown, which
avoids an inode-replacement race. Process death releases the kernel lock
automatically. Operators must not delete the file to bypass a live owner.

Separate state directories may have separate coordinators. Multiple Uvicorn or
Gunicorn workers against one directory are unsupported even if only one seems
busy. Network filesystems with unreliable advisory-lock semantics are also
unsupported. There is no leader election, automatic failover, or HA mode.

Private `GET /v1/operator/health` exposes the current `instance_id`, deployment
mode, preflight warnings, and `single_coordinator_lock` state. Public `/health`
exposes only the sanitized protection flag and warnings.

See [ADR 0006](adr/0006-single-coordinator-invariant.md).

## SQLite policy

All production `events.db` stores use the shared connection policy:

- WAL journal mode on filesystem databases;
- foreign keys enabled per connection;
- a 10-second connection and busy timeout;
- `synchronous=NORMAL`;
- bounded retry only for SQLite busy/locked failures; and
- a process-wide migration lock per resolved database path.

Critical settlement paths use explicit immediate transactions so the attempt
transition, immutable receipt, replay response, and unique compute contribution
commit together. The one-coordinator invariant prevents cross-process scheduler
split-brain; WAL and retry policy manage ordinary alpha-level concurrency among
threads/coroutines inside that process. They do not make arbitrary numbers of
writers safe, and finite retry can still surface a persistent storage fault.

Keyed canonical submission has a separate immediate transaction that creates
the queued execution and its `execution_submissions` mapping together. Mapping
lookup occurs under the same write boundary, so concurrent matching requests
have one create outcome and one replay outcome. A mapping is never overwritten
with `INSERT OR REPLACE`; a changed request conflicts and a missing mapped
execution fails closed.

For diagnosis, run `PRAGMA quick_check` through preflight or a read-only SQLite
tool only after considering the sensitivity of the database. Use the online
backup tool while the service is active rather than filesystem-copying
`events.db`, `-wal`, or `-shm` separately.

## Lifecycle, cancellation, and restart

Canonical lifecycle values are `queued`, `running`, `completed`, `failed`,
`cancelled`, and `interrupted`. `status` remains a compatibility projection and
must not drive new lifecycle UI or automation.

SQLite is the lifecycle authority. The service constructs and validates a
snapshot, commits it with finite retry, then updates the deep live snapshot,
emits its normal lifecycle event, invokes callbacks/legacy mirrors, and returns
or exposes it. A failed progress write leaves readers at the last durable
boundary. A failed terminal write emits no normal terminal event, invokes no
completion callback, publishes no terminal live snapshot, and exposes no
terminal artifact/share as authoritative.

Required-persistence exhaustion raises a typed operational error. Active HTTP
operations return `503` with
`detail.code=execution_persistence_unavailable`; they do not claim completion
or cancellation. Asynchronous work logs only execution identity, phase, attempt
count, and safe error type, cleans up safe process-local resources, and leaves
the durable row unchanged. A diagnostic event about that failure is not a
lifecycle event.

After a terminal snapshot commits, a later event, callback, or
callback-metadata failure cannot undo or reclassify it. Operators should
investigate the diagnostic while continuing to treat the committed terminal row
as authority.

After terminal events, project-memory publication, and completion-callback
handling finish, redundant process-local terminal request/result snapshots are
evicted. `GET` and idempotent replay continue to load the same authoritative
terminal row from SQLite. Queued/running snapshots and failed-persistence
boundaries remain cached while active.

One total deadline begins at canonical submission and covers semaphore wait,
planning, execution, review, revision, validation, and final manifest work.
Cancellation is idempotent, records request/terminal timestamps, cancels
queued/active attempts, and rejects late results. Deadline exhaustion is a
retryable terminal failure; coordinator restart is an interrupted retryable
terminal state. Neither creates resumable scheduler state.

Late, unknown, wrong-bound, expired, reclaimed, cancelled, superseded, or
interrupted results cannot enter the operational broker. A bounded quarantine
may retain reason, output hash, and at most a small preview for diagnosis. It
does not wake dispatch, update normal success/liveness, or earn points.

Retrying a transport operation and retrying lost work are different actions. A
matching `Idempotency-Key` retrieves the original execution; if it is
`interrupted`, that replay does not schedule it again. After reading the
original terminal record and deciding to run replacement work, submit with a
new key or without a key. Do not rewrite an interrupted record to queued or
start a second coordinator in an attempt to recover its in-memory work.

## Canonical submission idempotency

Callers may attach `Idempotency-Key` only to `POST /v1/executions`. Use a random
or otherwise collision-resistant value, retain it with the logical request, and
reuse it only when retrying that same validated request. A create response has
`Idempotency-Replayed: false`; a matching replay has `true`. A different request
under the same scope and key returns `409` with
`detail.code=idempotency_conflict` and leaves the original unchanged.

`false` covers both a normal creation and recovery by the still-active request
of its own preallocated candidate after an unknown commit outcome. `true`
always means an ordinary concurrent or historical replay and never starts or
resumes work.

If the initial row commits but activation preflight, live-cache publication,
control construction, or task registration fails, the canonical request
returns `503` with
`detail.code=submission_activation_failed` and the stable execution ID. The
execution has already been committed as `interrupted` with the same reason and
is safe to inspect. Retry the identical request with the same key to retrieve
that interrupted record; the retry returns `Idempotency-Replayed: true` and
does not schedule it. Investigate coordinator logs for the sanitized exception
type before submitting replacement work under a new key.

An unkeyed live call can recover only its own exact initial candidate during
its bounded persistence retries. A later unkeyed HTTP retry still creates a new
execution, so callers that need transport retry safety must supply a key.

Configured `pitch_key` holders share one requester scope. The mapping stores
only domain-separated scope and key digests plus the canonical request digest,
execution ID, and creation time. Never put a raw idempotency key or requester
credential in application logs, diagnostics, issue reports, metrics, or event
data.

When pitching is open, the direct ASGI peer address supplies a best-effort
development scope. Mycelium ignores forwarding headers; NAT, proxies, address
changes, and multiple local callers make this unsuitable as user identity.
Trusted-alpha mappings have no TTL and are included in SQLite backup/restore.

An `idempotency_consistency_error` response means a mapping did not resolve to
a valid execution. Treat it as storage corruption or incomplete recovery: stop
submitting under that key, collect sanitized diagnostics, run integrity checks,
and do not delete or replace the mapping manually while the coordinator is
active.

## Node registration and identity boundary

`POST /nodes/register` checks the shared `node_secret`, normalizes/bounds the
node ID, and issues a random session ID, one-time plaintext session token, and
expiry. The coordinator stores only the token's SHA-256 digest. Tokens are
process-local and restart-invalidated.

Poll, heartbeat, drain, stream, and result calls require `X-Node-Session` bound
to the normalized node. Registration with the current live token is
idempotent. A different live claimant for the same ID receives 409; a stale or
expired ID can be reclaimed, and work bound to the replaced session is closed
or requeued. The stock worker automatically re-registers after an explicit
session rejection or reconnect.

This protects against accidental/colliding labels and session mix-ups. It does
not prove hardware identity or prevent a `node_secret` holder from registering
another available label. There is no per-node public key, individual
certificate, remote attestation, or Sybil defense.

Session counters and durable lifetime contribution counters are distinct:

- `session_tasks_completed` and `session_contribution_points` reset with the
  process/session;
- `lifetime_tasks_completed` and `lifetime_contribution_points` derive from
  accepted durable contribution rows; and
- legacy `tasks_completed`/`credits_earned` aliases are session projections,
  not lifetime totals.

## Confidentiality and worker trust

Remote dispatch requires explicit consent and a non-local confidentiality
policy. `trusted_guild` means any admitted suitable worker may receive the
unit. `approved_nodes` records a node allowlist. These constraints guide the
scheduler but do not isolate worker processes, encrypt prompts from workers, or
verify that a claimed node is the intended physical machine.

`network_policy` is also declarative. The coordinator does not build a network
namespace, firewall, VM, or generated-code sandbox from it. The current stock
worker sends prompts to its local model and returns text; it does not execute
generated code. Mechanical validation/generated artifact execution occurs on
the requesting/coordinator side and must be treated as untrusted-code handling.

Never dispatch secrets or regulated/sensitive data to nodes merely because a
contract says `approved_nodes`. Use machines and network controls whose owners
you trust, and assume assigned prompt text is visible to them.

## Output and streaming bounds

`ExecutionRequestV1.max_output_bytes` is the execution-wide output contract
(1 KiB through 10 MiB). The server binds an attempt to that cap. Result bodies,
stream batches, cumulative streamed text, field lengths, quarantine previews,
session registry size, and WebSocket client queues/fanout are separately
bounded. Exceeding an attempt's output or stream budget rejects settlement; it
does not truncate and accept a different result as equivalent.

These are resource-abuse controls, not content validation. An output below the
byte cap can still be malicious, incorrect, or expensive for a downstream
consumer. Reverse proxies should enforce compatible request limits and
timeouts rather than buffering unbounded bodies before the application sees
them.

## Artifacts, roles, and integrity modes

Artifact paths are normalized relative POSIX paths. Absolute paths, Windows
drive/ADS syntax, backslashes, traversal, NULs, deeply encoded traversal,
symlinks, special files, out-of-root resolution, and configured file/count/
aggregate limit violations are rejected. Public manifests never contain
absolute server paths.

Every entry has one role:

- `deliverable`: selected output intended for the requester;
- `provenance` or `log`: audit evidence;
- `candidate_source`: non-winning/source candidate material; or
- `internal`: coordinator-only supporting material.

`GET /v1/executions/{id}/download` contains deliverables only.
`GET /v1/executions/{id}/audit-download` contains provenance, logs, candidate
source, and internal artifacts. A viewer asking for `role=all` receives a
deprecated compatibility view; new clients must keep deliverable and audit
surfaces distinct.

Legacy history, gallery, run, status/try, CLI-history, archive, and demo-capture
paths also enforce terminal publication. For current registered roots, the
durable execution must be terminal and bind the exact sealed manifest. Do not
copy a staged `output/` directory or interpret `full_log.json` as completion
authority. Unmarked historical runs retain the documented live-rescan
compatibility path; restart-reconciled staged runs remain hidden.

Manifest integrity modes mean:

| Mode | Meaning |
| --- | --- |
| `active` | Execution is still writing; entries may be refreshed within registered active roots |
| `sealed` | Entry metadata and a canonical manifest hash form an immutable local baseline |
| `legacy_live` | Older root can be scanned but has no sealed immutable baseline |
| `invalid` | Integrity state cannot be trusted; retrieval must fail closed |
| `none` | Execution has no registered manifest |

Sealing re-scans within bounds and stores the canonical SHA-256 manifest hash.
Every retrieval rechecks confinement, symlinks, size, and content hash; mutation
after sealing fails integrity checks. The manifest is not signed, has no
external timestamp, and does not establish authorship, behavioral correctness,
malware safety, or resistance to a coordinator administrator who can alter
both files and database.

## Sharing and access administration

Private routes are protected by `viewer_key` through header/Bearer auth or a
signed HttpOnly cookie. Pitch and worker endpoints retain their separate
authorities. The public allowlist is method-specific and deliberately small.

Execution shares are random bearer capabilities. Creation returns plaintext
once; durable storage contains only its hash and metadata. Shares can expire,
be listed without token disclosure, and be revoked singly or per execution.
Public responses redact server paths, internal review metadata, unrequested
candidate details, node identity by default, and artifact roles outside the
share's selected deliverable scope. Artifact access is separately opt-in.

Revocation stops later coordinator access but cannot recall copied data. Share
URLs can leak through browser history, referrers, screenshots, or proxy logs;
responses use no-store/no-referrer/nosniff headers, and proxies must redact the
token path too.

## Contributions are not payments

The authoritative contribution basis is `compute_contribution`: one accepted,
bound attempt supplied compute. `points_are_monetary` is false. Acceptance does
not mean the candidate won, validators passed, output was correct, or anyone is
owed money. `ledger.json` and historical credits are compatibility projections
of SQLite data, not an independent payment ledger, token, blockchain, or
fundraising mechanism.

Contribution `task` metadata is restricted to fixed non-sensitive labels and
free-form `details` are discarded. Startup idempotently redacts older SQLite
rows and regenerates `ledger.json` before ledger routes are served. Backups or
copied ledgers made before that upgrade may still retain the historical text;
operators must rotate or separately sanitize those copies.

The event log follows the same upgrade boundary. New persisted and replayed
events contain only allowlisted structural telemetry. Startup idempotently
redacts historical `events.data` payloads before event routes are served;
generated tokens remain live-stream-only. Offline database backups or exports
created before this upgrade may still contain historical prompt or output text
and require operator-managed rotation or sanitization.

## Generated artifact safety

Parsing, browser loading, structural checks, contract validation, deterministic
checks, and AI review are distinct evidence. None is a malware scan or complete
sandbox. Before opening or executing an artifact:

1. inspect the deliverable and its declared contract;
2. read validation outcome and individual evidence, including checks not run;
3. keep audit material separate from the deliverable;
4. scan content with tools appropriate to its type; and
5. execute only in an isolated environment controlled by the operator.

Do not show a generic “verified” badge. Assurance must say what kind of evidence
exists and must never imply general behavioral correctness unless a bounded
behavioral test actually ran and its scope is shown.

## Backup and recovery

`scripts/backup.py` uses SQLite's online backup API, then packages the database,
config, projects, output/artifacts, compatibility ledger, and build metadata in
a versioned ZIP with checksums. The archive is sensitive and receives private
POSIX permissions where supported.

`scripts/restore.py` validates the complete archive before mutation, rejects
unsafe entries and collisions, stages on the target filesystem, installs with
rollback-capable renames, and refuses existing managed state without
`--force`. It removes stale SQLite sidecars and prints the post-restore
preflight command. Stop the coordinator before restore.

No backup restores process-local queue entries, dispatcher coroutines, in-flight
node sessions, or plaintext session/attempt credentials. A restored coordinator
reconciles durable active state to interrupted and workers register again. See
the exact commands in [Trusted Alpha Runbook](TRUSTED_ALPHA_RUNBOOK.md).

Keyed submission mappings are ordinary SQLite state and are restored with the
database. Replaying a restored key therefore returns the captured execution;
it does not recreate post-backup work or resume a captured nonterminal task.

## Frontend-visible contract (Claude Code handoff)

Do not infer UI state from prose or compatibility aliases. Consume these API
fields directly:

| Concern | Fields/source | Display rule |
| --- | --- | --- |
| Node session | Private `GET /nodes`: `session_id`, `session_started_at`, `session_expires_at`, `draining`, `current_task`, `session_tasks_completed`, `session_contribution_points`, `lifetime_tasks_completed`, `lifetime_contribution_points` | Keep session and lifetime totals visually distinct; never request or render `session_token` |
| Deployment protection | Public `/health.private_routes_protected` and `warnings`; private `/v1/operator/health`: `deployment_mode`, `instance_id`, `single_coordinator_lock`, `preflight_warnings` | “Protected” requires the boolean true; do not treat HTTP 200 alone as safe |
| Artifact role | Manifest entry `role` | Label deliverable separately from provenance/log/candidate/internal |
| Manifest integrity | Execution `artifact_integrity_mode`, `sealed_manifest_hash`; manifest `integrity_mode`, `manifest_hash`, `sealed_at` | Say “sealed local hash baseline,” not signed/verified; explain legacy/active/invalid states |
| Deliverable vs audit | `primary_deliverables`, `artifact_manifest_url`, `audit_manifest_url`; `/download` vs `/audit-download` | Default user download is deliverable-only; audit is an explicit secondary action |
| Lifecycle | `lifecycle_status`, timestamps, cancellation/interruption fields, `retryable` | Never derive lifecycle from assurance or the legacy `status` projection |
| Submission replay | `Idempotency-Replayed` response header; structured 409/422/503 `detail.code` values | Preserve the key only with its logical request; never display it as a user identity or an exactly-once guarantee |
| Validation | `validation_outcome`, `validation_summary`, `validation_evidence` | Show passed, failed, skipped, and not-run checks; “partial” is not “correct” |
| Assurance | `assurance_level` plus each evidence item's level and `proves_behavioral_correctness` | Suggested labels: Not checked, Structure checked, Contract validated, Behavior tested, AI reviewed; avoid a generic badge |
| Post-hoc verification | `posthoc_verification_status`, `_started_at`, `_completed_at`, `_agreement`, `_reason` | Separate from terminal validation; trusted-alpha currently reports `disabled`, not silently pending |
| Share administration | Private list: `share_id`, `created_at`, `expires_at`, `revoked_at`, `last_accessed_at`, artifact/candidate/node-redaction flags | Plaintext token appears only in create response; never expect it from list or persist it in analytics/logs |

Public share responses are a separate redacted model. Do not hydrate private UI
from a public capability response or reveal private candidate/node data that is
absent there. Visual implementation is intentionally outside this operations
change.

## Residual limitations

The trusted-alpha controls make private invited operation reviewable; they do
not provide public-network readiness. Remaining structural limits include shared
instance-wide keys, process-local scheduling and node sessions, no public-key
node identity, no coordinator HA, no generated-code sandbox, no remote
attestation, no signed artifact provenance, and provisional model quality with
high run-to-run variance. Open-mode peer scoping is not durable identity, and
idempotent submission does not make model, worker, callback, or filesystem side
effects exactly once.
