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
- `node_secret` authorizes initial enrollment; it is not a durable node
  identity and is not sent on an enrolled session's normal operations.
- Enrollment credentials are bearer secrets and require TLS or a private
  authenticated overlay.
- Workers can read every prompt assigned to them.
- Placement, confidentiality, and network policy are recorded intent, not an
  operating-system or network sandbox.
- The validator subprocess contains trusted built-in parsers; it is not
  same-user filesystem confidentiality, reliable network denial, or a safe
  generated-code execution environment.
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
| Node enrollment IDs, credential digests, status, rotation/revocation | SQLite | Retained; sessions are reacquired after restart |
| Enrolled capability descriptor snapshots | SQLite | Immutable canonical claim JSON retained by enrollment/hash; attempts keep version/hash references |
| Scoped capability observations and projection receipts | SQLite (`events.db`) | Append-only records retained; bounded missing-only startup reconciliation repairs missed projections without changing settlement |
| Shadow decisions and successful operational-health events | Separate SQLite (`capability-shadow-health.db`) | Append-only decisions and admission/evaluation rows survive; deterministic identities prevent replay double count; writer locks are isolated from authoritative `events.db` |
| Shadow health-store, containment, and callback fallback counters | Memory | Reset at process start; protected reporting exposes their new `reset_at` timestamp |
| Validator-runner operational counters | Memory | Content-free totals reset at process start; terminal validation evidence remains durable with its execution |
| Validator staged output/reference | Temporary filesystem only | Removed with the one-check workspace after process cleanup; abrupt host/process loss can require sensitive temporary-file incident cleanup |
| Shares and revocation metadata | SQLite | Retained; plaintext share token is never stored |
| Contribution records | SQLite | Authoritative; JSON ledger is only a compatibility projection |
| Artifact roots, entries, hashes, roles, seal state | SQLite plus files | Retained if both database and artifact trees are restored together |
| Projects and compatibility output | Files, gated by canonical SQLite authority for current runs | Retained by state directory/backup; staged current output is not completion truth |
| Pending worker queue and dispatcher waits | Memory | Lost; corresponding work is marked interrupted where durable identity exists |
| Connected node registry and node sessions | Memory | Lost; enrolled workers authenticate for a fresh session |
| Plaintext enrollment credential | Worker identity file only | Not recoverable from coordinator state; back up separately and privately |
| Plaintext attempt nonce | Coordinator and assigned-worker process memory | Never stored durably; SQLite and backups contain only its digest |
| Plaintext node session token | Worker process memory only | Not recoverable from coordinator state; the coordinator stores only its digest |

Do not copy only `events.db` and assume a complete recovery.
`capability-shadow-health.db`, artifacts, projects, config, output, and the
compatibility ledger are part of the backup set. Conversely, copying either
live WAL database file directly is not a consistent SQLite snapshot.

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

Best-effort shadow decisions and operational-health rows use the sibling
`capability-shadow-health.db`. Its separate SQLite writer-lock domain prevents
optional experiment writes from contending with authoritative assignment,
attempt, and settlement transactions in `events.db`. On upgrade, pre-isolation
shadow decisions are copied from the legacy `events.db` table idempotently;
that source table is retained as append-only history but receives no new live
decisions.

Background shadow aggregation opens `events.db` through SQLite `mode=ro` plus
`query_only`, skips schema migration and WAL configuration, and performs only
bounded selects against startup-initialized evidence tables. This preserves the
single-coordinator write boundary even if a daemon read finishes after the
graceful drain.

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
backup tool while the service is active rather than filesystem-copying either
database or its `-wal`/`-shm` sidecars separately.

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
queued/active attempts, requests active validator-child termination/reaping,
waits boundedly for cleanup ownership, and rejects late results. A validator's
configured wall time is clamped to this
remaining execution budget. Deadline exhaustion is a
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
its serializer version, execution ID, and creation time. Pre-capability rows
migrate as version 1; only requests with an effective typed resource constraint
use version 2. Replay always uses the mapping's stored version. Never put a raw idempotency key or requester
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

An initial `bootstrap` registration presents the shared `node_secret` and a
high-entropy credential generated and persisted by the worker. SQLite creates
one immutable random `enrollment_id` for the normalized label and stores only a
domain-separated credential digest. Repeating the same label and credential is
idempotent. A different label or credential conflicts; shared admission never
overwrites an existing or revoked enrollment.

A `returning` registration presents the node label and its enrollment
credential, without `node_secret`. After durable authentication the coordinator
issues a random session ID, one-time plaintext session token, and expiry. The
session binds enrollment, label, and credential version, stores only the token
digest, and is process-local/restart-invalidated.

The version-1 worker identity JSON contains the normalized coordinator origin,
normalized node label, enrollment ID, positive `credential_version`, and the
plaintext enrollment credential. Before the first bootstrap, enrollment ID and
credential version are null. The coordinator must be an HTTP(S) origin with no
userinfo, path, query, or fragment. Default files are scoped by a hash of that
origin and live at `%APPDATA%\Mycelium\nodes` on Windows,
`~/Library/Application Support/Mycelium/nodes` on macOS, and
`$XDG_CONFIG_HOME/mycelium/nodes` (or `~/.config/mycelium/nodes`) on Linux.
`MYCELIUM_WORKER_CONFIG_DIR` or `join.py --identity-file` overrides the root or
full path. The credential is intentionally plaintext only in this private
worker-owned file; POSIX permissions must be `0600`, while Windows ACL safety is
an operator-verified best effort.

The stock worker ignores ambient HTTP(S) proxy environment variables for all
coordinator traffic (`trust_env=False`) so enrollment, session, and attempt
bearers are not silently routed to an inherited proxy. Configure direct TLS or
private-overlay reachability instead of relying on `HTTP_PROXY` or
`HTTPS_PROXY`. Credentialed joins also require an explicit coordinator origin;
the worker does not select an unauthenticated LAN-discovery response.

Poll, heartbeat, drain, stream, and result calls require `X-Node-Session`. For
an enrolled session they do not require or send the shared admission secret.
Each operation reads durable enrollment status, so an externally committed
revocation or rotation is enforced at that operation. An idle long poll checks
again before handout; the stock poll returns about every 25 seconds. A separate
janitor revalidates live enrolled sessions every 30 seconds, which is the
nominal maximum detection interval for an otherwise idle live coordinator
(ordinary scheduling delay or unavailable durable storage can add latency;
storage failures are diagnosed and retried). Active workers normally detect
sooner through stream, heartbeat, poll, or result traffic. Rejection invalidates
the session and safely reclaims active attempts.

A node is also dropped after `_NODE_TIMEOUT` (90 seconds) without contact, and
its in-flight work is reclaimed and requeued. That idle time is measured on a
monotonic reading, not on the wall clock, so correcting the coordinator's clock
— an NTP step, a resumed suspended host — does not mass-reclaim healthy nodes.
Worker lease deadlines are unaffected by this and remain absolute wall-clock
points: a correction past a lease deadline still expires that lease, which is
the intended behaviour. `last_seen` in node and session views stays a wall-clock
timestamp and is for operator display.

Coordinator restart clears sessions and active scheduler state, but the same
enrollment credential obtains a new session with the same enrollment ID.
`node_enrollment_mode=required` is mandatory in trusted alpha. Explicit local
`compat` mode may issue an unenrolled legacy session, but it cannot claim a
label found in the enrollment table. Its contributions remain session-scoped
rather than inherited by label, and it is excluded from scoped capability
evidence because it lacks the required immutable enrollment/descriptor binding.

This is stable, independently revocable bearer attribution. It is not a
per-node public key, certificate, physical-machine proof, remote attestation,
or Sybil defense.

### Capability claims and descriptor changes

The stock worker constructs one version-1 capability claim per process session
and reuses it across reconnects. Standard-library probes claim architecture,
logical CPU count, and total physical memory when safely available. A fixed,
three-second, non-shell `nvidia-smi` query claims at most eight distinct NVIDIA
GPU model/memory combinations. Missing tools, timeouts, malformed output, and
unavailable values become null rather than guessed values. The configured
Ollama model is claimed; version, digest, and quantization are included only
when Ollama supplies them for that exact model. Stock isolation is explicitly
`none`, concurrency is one, and its output claim stays at the protocol ceiling.

The output value is a claimed hard placement limit. For a typed session, every
qualification and handout path compares it with the canonical execution's
server-derived output budget. Equality is eligible; a smaller claim is excluded
with `insufficient_output_capacity`. The budget is not added to the typed
resource-requirement object or its hash. A descriptorless local-compatibility
session has no typed capacity to compare, and the coordinator does not invent
one for it.

The concurrency field is only an informational claimed upper bound. The
coordinator does not maintain or enforce per-node slot counts and does not
create multiple slots when a worker advertises a value above one. The stock
worker polls and executes sequentially, so its normal behavior conservatively
remains within the descriptor.

Operators may set `model` and `worker_capability_overrides` in `config.json`.
Direct `node.py` starts also accept `--model MODEL` and
`--capability-overrides PATH`; the bounded JSON file is layered over the config
object. Supported override keys are `hardware`, `features`,
`executor_version`, `model_context_tokens`, `model_variant`, and
`max_context_tokens`. Unknown fields fail startup. Model digest has no override:
only the runtime may supply one. `--capabilities` remains the legacy tag list.
Do not add stable device identifiers to override files.

Detection and overrides are still worker-controlled claims. The coordinator
canonicalizes and hashes the descriptor but does not measure or attest it. A
session cannot change its descriptor: `409 node_capability_descriptor_conflict`
means stop new assignment, let current work finish, stop the worker, and start a
new process/session with the intended descriptor. Do not keep retrying the old
session with changed claims.

The server-issued attempt remains the output authority after assignment. A
larger claim cannot raise the task limit, and reconnect, streaming, or final
result submission cannot renegotiate it. The attempt-specific cap continues to
govern cumulative stream bytes and settlement.

Bootstrap and returning registration for a durable enrollment require the
descriptor. A descriptorless old worker can run only in the explicitly
unenrolled local compatibility mode; it cannot receive a new enrolled attempt
with an unbound claim. Upgrade that worker instead of adding a trusted-alpha
bypass.

The viewer-protected `GET /v1/operator/node-enrollments` returns each normalized
descriptor, version/hash, snapshot count, legacy worker/server tag provenance,
and hard-match diagnostics. For a dry diagnostic, supply one URL-encoded
version-1 JSON object in `resource_requirements` and/or repeat
`required_capability`. Stable `reason_codes` explain exclusion. `GET /nodes`
omits descriptor JSON; `/health` and `/status.json` expose neither descriptors
nor matching details. Treat the full claim and hostname as private inventory
when collecting or sharing diagnostics.

### Capability evidence and shadow operation

Capability evidence is durable, coordinator-recorded operational history. It is
not descriptor verification, semantic correctness, trust, reputation, assurance,
contribution credit, or a routing score. Production assignment uses only the
existing hard matcher, queue, and circuit breaker. Neither sampled agreement nor
any aggregate delays, ranks, or reorders a real handout.

The supported configuration is:

```json
{
  "capability_evidence_mode": "off",
  "capability_evidence_min_samples": 5
}
```

The mode is strictly `off` or `shadow`; `off` is the default and there is no
active mode. The minimum must be an integer from 1 through 1000. In strict or
trusted-alpha configuration an invalid value fails loading; local compatibility
logs a warning and uses the default. `verify_rate` is independent and defaults
to zero.

`shadow` runs only a bounded post-assignment counterfactual over candidates that
already passed the same hard requirements. Admission freezes bounded non-secret
claim inputs at assignment time. Canonical rematching and exact
descriptor/model scope construction run from that immutable snapshot in bounded
background work, outside the production queue lock; handout does not wait for
scope capture or evidence aggregation. Aggregation uses evidence recorded no
later than assignment. It cannot mutate the queue, eligibility, actual
assignment, settlement, contributions, or breaker state. An evidence write or
evaluation failure is contained and never overturns accepted work.

The optional pipeline records its own operational health in two phases.
Admission outcomes are `disabled`, `not_applicable`, `queue_saturated`,
`scope_capture_failed`, and `scheduled`. A scheduled evaluation terminates as
`completed`, `evaluator_failed`, `decision_write_failed`, or
`cancelled_on_shutdown`. Durable rows contain only deterministic event ID,
attempt ID, phase, outcome, bounded reason code, and occurrence time; replay does
not double count them.

Failures that cannot safely record themselves in the health database increment process-local
`durable_health_record_write_failure`, `unexpected_containment_failure`, or
`background_task_callback_failure` counters. The protected report includes the
process `reset_at` time. These counters reset on coordinator start and the
failure path never recursively tries to write another health record.

Use viewer authentication to inspect aggregates:

```bash
curl -fsS -b viewer.cookies \
  "$BASE_URL/v1/operator/capability-evidence?limit=100&evidence_role=production"
```

The response field `shadow_decision_aggregates_available` distinguishes a real
zero from an unavailable isolated decision store. On read failure the protected
endpoint remains available, per-scope shadow counts are null, and a
process-local containment failure is counted without exposing exception text.

Optional filters are `enrollment_id`, `descriptor_hash`, `task_class`
(`dag_subtask` or `candidate`), and `evidence_role` (`production` or
`sampled_comparison`). The response always states `affects_routing=false`.
Binary dimensions include sample counts and Wilson intervals; latency and
throughput are bounded recent medians. Below the configured minimum, read
`insufficient_evidence` as cold start, never as failure. Descriptor, selected
model, task class, and evidence-role changes create separate cold scopes.

Bounded `window_started_at` and `window_ended_at` Unix-timestamp query filters
select an inclusive admission-time cohort and include the corresponding
attempts' evaluation outcomes even when they finish after the window end. The report exposes durable counts by
phase/outcome plus offered, scheduled, completed, skipped, failed, and pending
totals. Reproduce its drop/failure rate as:

```text
orphan_evaluation_total = evaluation rows with no persisted admission row
assignment_observation_total = all admission outcomes + orphan_evaluation_total
scheduled = scheduled admissions + orphan_evaluation_total
offered = scheduled + queue_saturated + scope_capture_failed
skipped = disabled + not_applicable
failed = queue_saturated + scope_capture_failed + evaluator_failed
         + decision_write_failed + cancelled_on_shutdown
drop/failure numerator = failed
drop/failure denominator = offered
drop/failure rate = failed / offered
```

The rate is null when `offered` is zero. An orphan evaluation is a durable
terminal evaluation whose admission write is absent; the report exposes
`orphan_evaluation_total` and infers one scheduled/offered observation for each
orphan so a partial telemetry-write failure cannot hide the outcome or shrink
the denominator. `pending` is scheduled, including those inferred admissions,
minus completed and all evaluation terminal outcomes, bounded at zero. Read this
as experiment pipeline health, not as a node failure or reputation rate.
Cancellation on coordinator shutdown is an operational outcome and is never
blamed on a worker. Shutdown stops new shadow admissions and uses a finite
graceful drain. Scope capture that exceeds the drain is reported as
`scope_capture_failed` with reason
`coordinator_shutdown_during_scope_capture`. An already-running decision write
may finish after the coordinator stops awaiting it and records its truthful
`completed` or `decision_write_failed` result; the drain overrun is also visible
in the process-local containment counter.

Each scope includes an identity-only future-active diagnostic with the bounded
reasons `legacy_descriptor_identity`, `descriptor_identity_unreconstructable`,
`immutable_model_identity_missing`, and `model_identity_unreconstructable`.
Non-promotable scopes remain hard-eligible when their task constraints allow
it. The diagnostic does not alter a hypothetical shadow preference or itself
suppress evidence; in particular, a digestless typed scope continues collecting
when the existing resolver can otherwise reconstruct it. Passing the identity
check is necessary but not sufficient for a future active experiment.

Only server-owned `lease_expired` and `node_stale` terminal causes are charged
as worker failures. Caller cancellation, execution deadline, payload/stream
limits, receipt binding, enrollment reclaim, session replacement, coordinator
restart, supersession, unknown causes, and free-form errors are excluded.
Deadline success requires a non-empty output settled by the issued lease
deadline; a timely worker-reported error or empty output is still a deadline
failure and remains visible in its separate settlement category.
Contract-floor results are structural assurance; sampled agreement compares
output shape. A sampled attempt names its exact production primary; an execution
ID and task class do not establish a pair. Neither is semantic correctness.

Startup reconciliation selects only attributable attempts missing expected
observations. Candidate-local contract-floor observations commit atomically with
an append-only, content-free projection receipt; terminal executions without a
receipt are retried. Completed and excluded rows do not consume the bounded
repair batch. It also backfills the versioned
`nonempty_output_before_lease_v2` deadline observation when an upgraded database
contains only the superseded `lifecycle` definition; current aggregates ignore
the old subject rather than rewriting append-only history.

The endpoint returns aggregates and grouped shadow outcomes, not raw records.
It also returns derived identity blockers and aggregate operational-health
counts, never the underlying health rows. The stores and response omit
prompt/output bodies, worker error text, free-form reasons, credentials, tokens,
nonces, session secrets, artifact contents, and arbitrary exception messages.
All accounting is best effort: its failure cannot alter node selection, handout,
attempt count, settlement, contribution, or execution outcome. The report is
still private operational inventory and its durable portion is included in
the backup as `capability-shadow-health.db`, separately from authoritative
`events.db`.

Session counters and durable lifetime contribution counters are distinct:

- `session_tasks_completed` and `session_contribution_points` reset with the
  process/session;
- `lifetime_tasks_completed` and `lifetime_contribution_points` derive from
  accepted durable contribution rows; and
- legacy `tasks_completed`/`credits_earned` aliases are session projections,
  not lifetime totals.

### Enrollment administration

Run administration locally against the coordinator state directory. Stop or
coordinate with the service only as required by the ordinary one-coordinator
operating procedure; the commands use the same SQLite transaction policy and
the running coordinator observes status on authenticated operations.

```bash
python scripts/node_enrollment_admin.py --state-dir data list
python scripts/node_enrollment_admin.py --state-dir data revoke ENROLLMENT_ID --reason "operator offboarded"
python scripts/node_enrollment_admin.py --state-dir data rotate ENROLLMENT_ID \
  --coordinator https://coordinator.example \
  --identity-output /secure/path/unused-worker-identity.json
```

`list` returns no credential material. Revocation is idempotent and preserves
attempt, receipt, and contribution history. Rotation preserves the enrollment
ID and label, invalidates the old credential/session, and writes the replacement
credential only to the requested private identity file. Transfer that file to
the intended worker through an authenticated secret channel.

For a planned rotation, drain and stop the worker, choose an unused protected
output path, rotate, replace the worker's matching identity file, and restart
it. Verify that the same enrollment ID returns with the incremented credential
version. The command refuses a pre-existing output by default.

If the command reports that commit was not confirmed, keep the prepared output
and rerun the exact command with `--resume-existing`; this safely converges on
the same credential/version after an ambiguous commit. If a committed output
is actually lost, rotate again to a different unused path, producing another
new version. Plaintext cannot and should not be recovered from SQLite. Do not
copy a credential into a command line, chat, ticket, log, or URL.

## Verification evidence

`GET /v1/operator/verification-evidence` (viewer-protected) reports bounded
per-scope verification evidence: subject enrollment ID and node label, identity
class, descriptor hash, executor and model scope, task class, verifier
kind/name/version, counts by outcome and by fault attribution, a sample count
beside every rate with the same `capability_evidence_min_samples` floor and
Wilson interval treatment as capability evidence, the last observed timestamp,
and the legacy row count held separately. Query parameters: `enrollment_id`,
`descriptor_hash`, `task_class`, `verifier_kind`, `limit`.

Read it as evidence, not as a verdict. The response says so in its own
`semantics` block: it is not reputation, not correctness, not assurance, and it
influences no routing, eligibility, settlement, or contribution decision. An
agreement count describes whether two runs produced output of comparable *shape*;
it does not say either output was right, and it is scoped separately from
deterministic-check outcomes so the two can never be added together. A scope below
the minimum sample count reports `insufficient_evidence`, which means "not enough
observations", not "poor".

`not_run` rows record that a verification did not happen and why -- requester
cancellation, coordinator shutdown, coordinator persistence failure, a deadline
that had already passed, or an unavailable verifier. They are excluded from the
attributable sample count, so a cancelled or interrupted run never depresses a
node's observed rate.

Sampled verification remains off: `verify_rate` defaults to `0.0` and
trusted-alpha mode disables it regardless. This surface exists because the
evidence is now durable, not because the feature has been switched on. See
[ADR 0014](adr/0014-durable-verification-evidence.md).

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

## Validator process containment

Use `validator_execution_mode="auto"` for normal operation. It runs
`code_parse`, `structured_json`, and `json_schema` as
`subprocess_isolated`; `nonempty`, `artifact_extraction`,
`artifact_contract`, and `file_manifest` remain `inline_trusted`. Mode
`subprocess` sends every current built-in through the runner. Mode `inline` is
weaker local-development compatibility and trusted-alpha preflight rejects it.
Evidence uses `inline_compatibility` for a normally isolated parser overridden
by that setting; normally inline checks remain `inline_trusted`.

The default runner settings are:

| Setting | Default | Inclusive bounds | Operational purpose |
| --- | --- | --- | --- |
| `validator_subprocess_timeout_seconds` | 10 | 1–120 seconds | per-check wall time, additionally clamped to the execution's remaining deadline |
| `validator_subprocess_memory_mb` | 256 | 128–1,024 MiB | requested address-space ceiling where POSIX provides the resource limit |
| `validator_subprocess_request_max_bytes` | 2 MiB | 16 KiB–16 MiB | maximum serialized V2 JSON control metadata before spawn; not an output-body limit |
| `validator_subprocess_response_max_bytes` | 32 KiB | 1–256 KiB | maximum accepted child response |

The bounds in this table are strict and inclusive; booleans and non-integer
values are invalid. Preflight checks every field. Trusted-alpha
loading/preflight fails on an invalid value, while local mode warns and
restores the default.

The execution's existing `ExecutionRequestV1.max_output_bytes` is the
authoritative raw UTF-8 output budget, with a canonical maximum of 10 MiB. The
runner request setting does not reduce that budget and there is no separate
validator-output configuration. Raising the request limit is neither required
nor the transport fix for large output.

Each child is launched without a shell, with a sanitized environment whose
`TEMP`/`TMP` on Windows or `TMPDIR` on POSIX point into its controlled working directory,
closed inherited file descriptors where supported, a fresh working directory,
and a new process group or platform equivalent. The parent sends one strict versioned
JSON request on stdin and accepts one strict bounded JSON response on stdout.
The launcher fixes child `cwd` to the parent-created private validator
directory; the runner resolves it once and passes it as explicit `stage_root`
to the closed dispatcher. For `code_parse`, that directory contains bounded
copied artifact inputs. For output-consuming checks it contains one exact UTF-8
output file at the fixed reserved path
`__mycelium_validator_input__/output.utf8`. For metadata-only validators forced
through the subprocess it is empty. The request can carry validated normalized
logical names but cannot select or replace the root. Never treat the operator's
launch directory as validator protocol authority.
The registry and parent own validator identity, version, required/optional and
contract-floor status, assurance, behavioral-correctness flag, execution mode,
containment level, and termination classification. Never infer those fields
from child output.

New parent calls emit protocol V2 only. Its bounded JSON contains control
metadata, not the generated body: the output reference carries the fixed
relative path, exact UTF-8 byte length, literal `utf-8` encoding, and lowercase
SHA-256. V1 remains explicitly parseable for compatibility tests; a V2 failure
does not retry V1 or fall back inline. Protocol version and exact validator
identity must match in the response.

For `nonempty`, `structured_json`, and `json_schema` in subprocess mode, the
parent strictly encodes the canonical output, checks its execution byte budget,
creates the reserved directory/file exclusively with private POSIX modes or the
best available inherited Windows ACL behavior, writes and hashes the exact
bytes, and closes the file before spawn. It checks cancellation and the
effective deadline during staging. Candidate artifacts cannot use the reserved
namespace or any descendant.

The child accepts only that fixed reference. It confines the target to the
fresh stage root, rejects links/reparse points and missing or nonregular targets,
opens the file once, and verifies descriptor identity, hard and declared size,
SHA-256, and strict UTF-8 before the allowlisted validator receives the text.
Size, digest, UTF-8, file-type, reference, and oversize failures are stable
content-free infrastructure failures. They never cause inline fallback.

`code_parse` copies only selected regular files from the authoritative
candidate subtree into the private directory. Path, type, identity, file-count,
per-file, aggregate, and relative-path limits are checked before and during
copying; symlinks, special files, traversal, snapshot changes, and out-of-root
paths are not accepted. Destination hard links are never created. A regular
source may itself have other filesystem links; this path always creates a new
byte copy. Selecting another candidate's subtree is an integrity failure.

The `code_parse` copy path currently permits at most 20 files, 10 MiB per file,
10 MiB aggregate, and a 200-character relative path. These are fixed validator
copy bounds, separate from—and tighter than—the artifact delivery ceilings
above. They are not controlled by the four runner settings. An otherwise valid
registered artifact that exceeds them fails `code_parse` with bounded
input-rejection evidence.

When forced through `subprocess`, `artifact_extraction`, `artifact_contract`,
and `file_manifest` receive only normalized logical names in an empty private
directory. The parent still checks the authoritative root and selected subtree,
regular-file type, symlink/reparse/special-file rejection, snapshot membership,
portable duplicates, file count, and relative-path length. It does not copy or
rehash content, so a large artifact does not fail a metadata-only validator
merely because it exceeds the `code_parse` copy-byte ceiling.

Process-tree termination and reaping are attempted before private-workspace
removal; inability to confirm process-tree cleanup is a containment incident,
not a successful result.

The private validator directory lives under the operating system's temporary
directory and can contain generated source for `code_parse` or a reserved
staged-output file for an output-consuming validator; metadata-only subprocess
directories remain empty. Normal Python unwinding removes it, but abrupt
coordinator termination, host power loss, or an uncatchable kill can leave a
`mycelium-validator-*` directory. A temporary-workspace deletion error also can
leave the same kind of stale directory, but it fails closed with
`validator_stage_cleanup_failed` evidence and increments
`staging_cleanup_failures`, distinct from `process_cleanup_failures`. There is
no startup stale-stage sweeper. After an
incident, stop the coordinator, identify only directories with that exact
prefix that are not owned by a live runner, and remove them under the host's
ordinary sensitive-temporary-file policy. Temporary-directory permissions and
normal cleanup are not secure erasure.
Staging is neither artifact sealing nor terminal publication authority.

Spawn failure, timeout, crash, malformed/mismatched/oversized response,
artifact/output staging failure, and output-reference failure produce bounded
error evidence. An unconfirmed process-tree
cleanup is reflected in error evidence or a content-free cleanup counter,
depending on the original outcome. Neither case triggers an inline fallback for
an isolated validator.
Existing validation policy records whether the candidate passes. The explicit
`allow_unverified_fallback` policy may later select and deliver a usable failed
candidate while preserving its failed/partial validation outcome. There are no
runner retries. Do not restart
a whole execution merely to hide a required validator failure; inspect the
stable reason, the execution deadline, and host resource pressure first.

POSIX hosts additionally apply available CPU, address-space, output-file,
open-descriptor, and child-process limits. Windows retains bounded pipes,
wall-clock enforcement, staging, a fresh process group, and process-tree
cleanup on a best-effort basis. Before spawn the parent best-effort creates and
configures a per-run Windows Job Object; immediately after spawn it attempts to
assign the runner with kill-on-close and an active-process limit of one.
Successful assignment strengthens descendant prevention, cleanup, and
coordinator-exit handling, but does not provide the POSIX CPU, address-space,
output-file, or descriptor limits.

Job creation/configuration or assignment may fail, including inside a
restrictive enclosing Job Object. The process is not spawned suspended, so work
or a descendant created before assignment is outside the new Job's guarantees.
The existing process-group and `taskkill /T` path remains the live-parent
fallback. Evidence reports `windows_process_tree_best_effort` whether Job
assignment succeeded or the fallback is in use; do not infer Job membership
from that label. Windows path-race resistance is also best effort where
descriptor-relative no-follow opens are unavailable.
Windows mode bits do not install a private DACL; secure the service account's
temporary root because validator stages inherit that ACL.
Record the operating system when investigating a containment failure.

The process group alone does not kill the child when the coordinator crashes.
A successfully assigned Windows Job Object closes with the coordinator and
requests termination of its assigned runner, but best-effort assignment and the
pre-assignment window leave a residual orphan path. There is no Linux
parent-death signal or durable runner PID registry. A POSIX child installs an
early hard alarm of about 125 seconds; Windows has no equivalent child-side
alarm, so an unassigned runner or pre-assignment escape can outlive the
coordinator and a restart will not discover it. After abrupt coordinator loss,
inspect for a confirmed prior `validator_runner.py` process before returning the
deployment to service. Treat any survivor as a containment incident and
terminate only the confirmed runner tree.

Validation evidence exposes bounded child result detail inside a parent-owned
envelope. Required/optional source, assurance, behavioral-correctness,
execution/containment mode, and applicable termination metadata are
parent-authored and authoritative.

The `validator_process` field of private `/v1/operator/health` reports the
registered classifications, configured mode, runner protocol/containment level,
reset time, and content-free totals from the service-owned canonical executor.
The exact counter fields are `reset_at`, `subprocess_runs`,
`successful_responses`, `validation_failures`, `timeouts`, `crashes`,
`malformed_responses`, `oversized_requests`, `oversized_responses`,
`spawn_failures`, `staging_failures`, `staging_cleanup_failures`,
`cancellations`, `process_cleanup_failures`, `output_staging_failures`,
`output_reference_failures`, `output_size_mismatches`,
`output_digest_mismatches`, `output_utf8_failures`, and
`output_oversized_failures`.
These process counters reset on restart and are not execution authority.
Prompts, output bodies or excerpts, JSON values/keys, schemas, source bytes,
credentials, raw stderr, host paths, private output references or digests, and
arbitrary exception text must never enter logs, evidence, counters, or
operator-health diagnostics. Existing public result/share content policy is
unchanged, but validator-runner metadata must not add the private reference,
path, reference `byte_length`, or digest to those responses. Existing bounded
validator detail that already reports an ordinary output byte count is
unchanged and is not the private reference object.

See
[ADR 0013](adr/0013-parser-heavy-validators-bounded-process-boundary.md) for the
decision and platform boundary.

## Generated artifact safety

Parsing, browser loading, structural checks, contract validation, deterministic
checks, and AI review are distinct evidence. Production `code_parse` only
parses and never imports or executes generated modules. None of these checks is
a malware scan or complete sandbox. Before opening or executing an artifact:

1. inspect the deliverable and its declared contract;
2. read validation outcome and individual evidence, including checks not run;
3. keep audit material separate from the deliverable;
4. scan content with tools appropriate to its type; and
5. execute only in an isolated environment controlled by the operator.

Do not show a generic “verified” badge. Assurance must say what kind of evidence
exists and must never imply general behavioral correctness unless a bounded
behavioral test actually ran and its scope is shown.

## Backup and recovery

`scripts/backup.py` writes backup format v2. It uses SQLite's online backup API
for both `events.db` and `capability-shadow-health.db`, then packages those
independent snapshots, config, projects, output/artifacts, compatibility ledger,
and build metadata in a versioned ZIP with checksums. The archive is sensitive
and receives private POSIX permissions where supported.

`scripts/restore.py` validates the complete archive before mutation, rejects
unsafe entries and collisions, stages on the target filesystem, installs with
rollback-capable renames, and refuses existing managed state without
`--force`. It restores both databases together, removes their stale SQLite
sidecars, and prints the post-restore
preflight command. Stop the coordinator before restore.

Restore accepts current format-v2 archives and legacy format-v1 archives. A v2
backup normally carries `capability-shadow-health.db`; the health database may
be absent only when backing up or restoring pre-feature state. Legacy v1 has no
health-database manifest field, so restoring it preserves the authoritative
state while starting new shadow-health history on the upgraded coordinator.

The `events.db` snapshot includes node enrollment IDs, credential digests,
revocation/rotation state, immutable capability-claim snapshots, and nullable
attempt/receipt descriptor and requirement bindings, scoped capability
observations, legacy pre-isolation shadow decisions, and contribution
attribution. The separate `capability-shadow-health.db` snapshot contains live
append-only shadow decisions and successful operational-health records. A
legacy format-v1 restore copies its old decisions forward idempotently on the
next startup. Process-local shadow fallback counters and their prior reset
timestamp are not restored. Neither snapshot contains a plaintext enrollment
credential. Worker identity files live on the workers and are deliberately
outside the coordinator backup; each worker operator must protect and back them
up separately.

A restore is point-in-time, not an offboarding log. Only revocations and
rotations already present in that snapshot survive. Restoring an older snapshot
can therefore re-enable an enrollment revoked later, restore an older
credential digest or `node_secret`, desynchronize current worker identity files,
and carry a `private_overlay=true` assertion onto a host where it is no longer
true. Before reopening worker access, reconcile every post-snapshot enrollment
change, revoke or rotate as needed, redistribute matching identity files, verify
the current transport/overlay controls, and rerun trusted-alpha preflight.

No backup restores process-local queue entries, dispatcher coroutines, in-flight
node sessions, or plaintext session/attempt credentials. A restored coordinator
reconciles durable active state to interrupted, preserves enrollment identity,
and requires workers to authenticate for new sessions. See the exact commands
in [Trusted Alpha Runbook](TRUSTED_ALPHA_RUNBOOK.md).

Keyed submission mappings are ordinary SQLite state and are restored with the
database. Replaying a restored key therefore returns the captured execution;
it does not recreate post-backup work or resume a captured nonterminal task.

## Frontend-visible contract (Claude Code handoff)

Do not infer UI state from prose or compatibility aliases. Consume these API
fields directly:

| Concern | Fields/source | Display rule |
| --- | --- | --- |
| Enrollment/session | Private `GET /v1/operator/node-enrollments` and `GET /nodes`: `enrollment_id`, `node_id`, `status`, timestamps, live session/drain state, and session/lifetime totals | Use enrollment ID as trust/accounting key and node ID as label; never request or render credential material or `session_token` |
| Capability claim | Private operator enrollment view: descriptor/version/hash, legacy tag provenance, `hard_requirement_eligibility.reason_codes`; `/nodes` has hash/version only | Say “claimed” and “eligible,” never measured, verified, trusted, or attested; `insufficient_output_capacity` compares the claimed node maximum with the server task budget; keep full hardware/model inventory off public views |
| Capability evidence | Private `GET /v1/operator/capability-evidence`: scoped counts/rates/intervals, recent medians, `insufficient_evidence`, grouped shadow outcomes, identity blockers, operational-health counts and process reset/counters, `affects_routing` | Say “observed operational history” and “experiment health”; agreement means shape only; future-active eligibility is an identity prerequisite only; never display correctness, trust, reputation, routing weight, or a global score |
| Deployment protection | Public `/health.private_routes_protected` and `warnings`; private `/v1/operator/health`: `deployment_mode`, `instance_id`, `single_coordinator_lock`, `preflight_warnings` | “Protected” requires the boolean true; do not treat HTTP 200 alone as safe |
| Artifact role | Manifest entry `role` | Label deliverable separately from provenance/log/candidate/internal |
| Manifest integrity | Execution `artifact_integrity_mode`, `sealed_manifest_hash`; manifest `integrity_mode`, `manifest_hash`, `sealed_at` | Say “sealed local hash baseline,” not signed/verified; explain legacy/active/invalid states |
| Deliverable vs audit | `primary_deliverables`, `artifact_manifest_url`, `audit_manifest_url`; `/download` vs `/audit-download` | Default user download is deliverable-only; audit is an explicit secondary action |
| Lifecycle | `lifecycle_status`, timestamps, cancellation/interruption fields, `retryable` | Never derive lifecycle from assurance or the legacy `status` projection |
| Submission replay | `Idempotency-Replayed` response header; structured 409/422/503 `detail.code` values | Preserve the key only with its logical request; never display it as a user identity or an exactly-once guarantee |
| Validation | `validation_outcome`, `validation_summary`, `validation_evidence` | Show passed, failed, skipped, and not-run checks; “partial” is not “correct” |
| Validator containment | Parent-authored validation evidence: execution mode, runner protocol version, containment level, termination reason when applicable, and duration | Describe the actual check and platform boundary; never call a same-user subprocess a sandbox or expose raw child output/stderr |
| Assurance | `assurance_level` plus each evidence item's level and `proves_behavioral_correctness` | Suggested labels: Not checked, Structure checked, Contract validated, Behavior tested, AI reviewed; avoid a generic badge |
| Legacy post-hoc verification | `posthoc_verification_status`, `_started_at`, `_completed_at`, `_agreement`, `_reason` | Separate from terminal validation and scoped capability evidence; trusted-alpha reports `disabled`, not silently pending |
| Share administration | Private list: `share_id`, `created_at`, `expires_at`, `revoked_at`, `last_accessed_at`, artifact/candidate/node-redaction flags | Plaintext token appears only in create response; never expect it from list or persist it in analytics/logs |

Public share responses are a separate redacted model. Do not hydrate private UI
from a public capability response or reveal private candidate/node data that is
absent there. Visual implementation is intentionally outside this operations
change.

## Residual limitations

The trusted-alpha controls make private invited operation reviewable; they do
not provide public-network readiness. Remaining structural limits include shared
instance-wide keys, process-local scheduling and node sessions, no public-key
node identity, no coordinator HA, no generated-code sandbox, no same-user
filesystem confidentiality or guaranteed runner network denial, no remote
attestation, self-reported capability descriptors, no evidence-driven production
routing, no signed artifact provenance, and provisional model quality with
high run-to-run variance. Open-mode peer scoping is not durable identity, and
idempotent submission does not make model, worker, callback, or filesystem side
effects exactly once.

Sampled comparison remains optional and default-off. Its agreement signal is
diagnostic output-shape agreement only. Production polling never defers first
refusal or changes queue order from verification or capability evidence, in any
deployment or evidence mode.

There is likewise no worker concurrency or capacity-weighted scheduling. A
future active evidence experiment requires immutable model and descriptor
identity, every documented live threshold, a separate accepted ADR, and a
separately reviewed implementation PR. None is introduced by the current
diagnostic.
