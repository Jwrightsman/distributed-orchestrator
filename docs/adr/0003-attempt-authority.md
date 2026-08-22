# ADR 0003: Server-Issued Attempts Are Result Authority

- Status: Accepted
- Date: 2026-08-21
- Decision scope: trusted-alpha worker result integrity

## Context

The original worker path treated the presence of a task id in a general result
dictionary as operational progress. Protocol-v1 binding existed, but result
strictness was selected from the worker's submitted `contract_version`. A
worker assigned a v1 task could therefore omit the version and binding fields
and reach the legacy path. Queued, late, reclaimed, or otherwise unbound output
could also be retained in the same channel a dispatcher watched.

A task id is visible in events and logs. It is routing identity, not settlement
authority. An in-memory dictionary cannot atomically decide an attempt, preserve
exact replay through restart, or prevent two concurrent submissions from both
appearing accepted.

## Decision

The server-issued durable attempt record is the sole authority for distributed
result admission.

Before a worker receives a unit, the coordinator persists an active attempt
containing task, execution, execution-unit, unit-kind, assigned-node, contract,
issue-time, and lease bindings. It mints an unguessable attempt id and nonce and
stores only the nonce's SHA-256 digest.

For a v1 attempt, settlement requires the worker to echo every binding. The
server derives strictness from the attempt row, never from the submitted
version. Missing values are rejection, not legacy fallback. The attempt must be
active and inside its lease.

Settlement runs in one SQLite write transaction:

1. locate the authoritative active or replayed attempt;
2. validate all bindings and the canonical submission hash;
3. conditionally transition `active` to `settled`;
4. insert one immutable accepted-result receipt;
5. store the replay response and result hash;
6. insert the unique non-monetary compute contribution, if earned;
7. commit before publishing the receipt in memory.

Uniqueness permits one active attempt and one accepted receipt per task and one
compute contribution per attempt. An exact replay returns the original durable
response after restart. A changed replay fails.

The dispatcher consumes an `AcceptedResultReceipt`, not a general result map.
It verifies task, execution, unit, unit kind, and contract version. If in-memory
publication is lost after commit, it reloads the durable receipt.

Rejected output may be retained only in a separate bounded quarantine with a
reason, output hash, and bounded preview. Quarantine can never satisfy a
dispatcher, update normal node success state, earn points, become final output,
or emit normal attempt completion.

## State model

Attempt states are:

```text
active
  ├─ settled
  ├─ expired
  ├─ reclaimed
  ├─ cancelled
  ├─ superseded
  └─ interrupted
```

Only `active → settled` publishes an accepted receipt. Every other terminal
state rejects later output. Coordinator startup interrupts active attempts
because their process-local queue and dispatcher wait cannot be resumed.

## Consequences

- A worker cannot downgrade v1 by omitting `contract_version`, attempt id,
  nonce, execution id, unit id, or unit kind.
- Unknown, queued-but-unleased, expired, reclaimed, cancelled, superseded, and
  wrong-bound results cannot enter operational execution.
- Concurrent submissions settle at most once.
- Exact idempotent replay survives a process restart.
- Contribution points can be transactionally tied to accepted compute without
  implying selection or correctness.
- SQLite contains bounded accepted output in immutable receipts, increasing the
  database's data-sensitivity and retention footprint.
- The process-local compatibility `task_results` map may remain for old readers
  but is populated only after settlement and is not authoritative.

## Durability boundary

Attempt credentials, terminal state, accepted receipts, replay responses,
result hashes, quarantine rows, and compute contributions are durable. The
worker queue, connected nodes, background coroutines, and dispatcher waits are
not. Restart therefore interrupts rather than resumes active attempts. This is
an integrity decision, not a durable-scheduler claim.

Accepted receipts currently store result output in SQLite, bounded by the
worker result model and execution output policy. A future large-output design
may replace that body with a bounded content reference, but must preserve the
same atomic authority and binding checks.

## Security boundary

Attempt authority proves that a result corresponds to one admitted active
lease. It does not prove the physical identity of the worker, correct hardware,
honest inference, absence of collusion, or behavioral correctness. Every worker
still shares `node_secret` and may choose a claimed `node_id`. Per-node keys,
individual revocation, signed envelopes, and Sybil defenses remain outside this
decision.

## Rejected alternatives

### Trust the submitted contract version

Rejected because an attacker can omit the marker that selects strict checking.
Policy must come from server-owned state.

### Keep a `task_id -> result` authority map

Rejected because a task id is not a credential, bindings are not encoded in the
map key, concurrent settlement is not atomic, and restart loses replay state.

### Store suspicious results beside accepted results

Rejected because any shared consumer or future refactor could accidentally wake
dispatch with unbound output. Diagnostic quarantine is a separate table and
API-internal concept.

### Pay after a later final validator

Rejected for compute contribution points. A valid worker may donate compute
whose candidate is not selected. Compute acceptance, candidate selection, and
validated final outcome are separate facts and must remain separately named.

### Make the entire scheduler durable in this sprint

Rejected as outside the bounded trusted-alpha change. Durable integrity records
are required now; queue resumption is a larger scheduling and recovery design.
