# ADR 0009: Commit authoritative execution state before publication

- Status: Accepted
- Date: 2026-08-23
- Decision scope: execution lifecycle authority, publication ordering, and persistence failure

## Context

Canonical execution state is recorded in SQLite, mirrored in a process-local
live-result cache, announced through lifecycle events, adapted into legacy job
state, and observed by callbacks and artifact/share APIs. Previously, some
paths updated the live cache before attempting persistence, and terminal
persistence returned a Boolean that callers could ignore. A caller could
therefore observe completion, failure, interruption, or cancellation that a
fresh coordinator would still read as queued or running.

Bounded retry is useful for transient SQLite contention, but retry alone does
not define which copy is authoritative after permanent storage failure. The
trusted alpha needs one explicit truth boundary without adopting a workflow
engine, broker, or distributed transaction system.

## Decision

Durable execution storage is the authority for canonical lifecycle snapshots.
Every required snapshot follows this ordering:

```text
construct and validate snapshot
        ↓
commit snapshot durably
        ↓
publish a deep process-local live snapshot
        ↓
emit the normal lifecycle event
        ↓
invoke lifecycle callbacks and legacy mirrors
        ↓
return or expose the result and terminal artifacts/shares
```

The execution service has one required snapshot-commit path. It performs a
finite number of persistence attempts and raises a typed persistence exception
when they are exhausted. Required queued, running, terminal, cancellation, and
callback-metadata writes cannot be represented by an ignorable success-like
Boolean.

A failed progress write leaves the public live snapshot at the last durable
boundary. A start callback and running event occur only after the running
snapshot commits. A normal completed, failed, interrupted, timed-out, or
cancelled event and completion callback occur only after that terminal snapshot
commits.

Terminal artifact access, including access through a share, requires a durable
terminal execution. For a current `sealed` manifest, that execution must bind
the exact published manifest hash. Historical `legacy_live` manifests retain
their explicit compatibility behavior: they still require a durable terminal
execution and are freshly rescanned, but they are not relabeled as sealed.
Artifact generation and manifest sealing may be part of finalization before the
terminal commit, but those bytes are not an authoritative terminal publication
until the commit succeeds.

The same gate applies to compatibility `output/` readers and publishers:
history, gallery, run/status/try pages, CLI history, downloads, and demo-asset
capture. Registered root ownership is resolved before mutable log fields are
trusted. New roots require the terminal snapshot's exact sealed-manifest hash;
unmarked historical roots retain bounded `legacy_live` access, except that a
restart-reconciled terminal row is not evidence that staged terminal material
was ever committed.

DAG project memory is also a compatibility mirror. Its iteration is written
after the normal terminal lifecycle event and before the external completion
callback. Permanent terminal-persistence failure leaves memory unchanged, and
a later memory-write failure cannot reclassify the already durable execution.

A share record may exist before an execution becomes terminal, and its public
view may show the last committed nonterminal snapshot. It cannot publish an
uncommitted terminal snapshot or open terminal artifact access before the
durable terminal gate above.

After permanent terminal-persistence failure, asynchronous execution:

- does not publish the uncommitted terminal snapshot in the live cache;
- does not emit a normal terminal lifecycle event;
- does not invoke the completion callback or legacy terminal mirror;
- does not expose terminal artifacts or shares as authoritative;
- leaves the last durable snapshot intact;
- logs a secret-safe operational error; and
- cleans up process-local controls and tasks where safe without recursively
  constructing another unpersisted terminal result.

An active HTTP operation maps required-persistence failure to `503 Service
Unavailable` using a stable, sanitized error envelope. It does not claim that
the requested transition succeeded.

Persistence-failure telemetry is diagnostic, not lifecycle truth. It may
identify the execution, phase, attempt count, and exception type, but it must
not contain prompts, results, credentials, keys, tokens, nonces, or artifact
contents. Failure to emit diagnostic telemetry cannot mask the original
persistence failure.

Once a terminal snapshot has committed, later lifecycle-event, callback, or
callback-metadata failure does not undo or reclassify it. Such failures remain
diagnostic metadata where that metadata can itself be committed safely.

## Consequences

- A live/API/event/callback terminal observation agrees with what a fresh
  coordinator reads from SQLite.
- Storage unavailability can suppress progress or terminal publication rather
  than returning an optimistic result.
- Restart reconciliation remains the truthful recovery for a durable queued or
  running row whose process-local work disappeared.
- Event and callback consumers must treat normal lifecycle events as
  post-commit notifications, not as the commit mechanism.
- Terminal artifact availability is coupled to a committed execution snapshot;
  current sealed artifacts are additionally coupled to the manifest identity
  bound into that snapshot.
- Finite retries bound coordinator stalls; persistent storage failure remains
  visible to operators.

## Authority boundary

SQLite authority does not make the worker queue, scheduler, node sessions,
coroutines, or model side effects durable. It does not create coordinator high
availability. One coordinator still owns one state directory, and restart
interrupts rather than resumes lost work.

## Rejected alternatives

### Publish to memory first and repair storage later

Rejected because readers and callbacks could observe a state that disappears
after restart. A cache cannot be more authoritative than its durable source.

### Convert persistence failure into another terminal result

Rejected because that second result requires the same unavailable persistence
boundary and can recurse while still publishing no durable truth.

### Treat diagnostic events as lifecycle completion

Rejected because telemetry availability is independent of SQLite authority.
Diagnostics describe a publication failure; they do not complete the
execution.

### Add a generalized transactional outbox

Deferred. This change requires commit-before-publication and safe suppression,
not atomic delivery guarantees to an external broker. A general outbox would
add retention, delivery, replay, and consumer-idempotency policy beyond the
trusted-alpha need.

### Adopt a workflow engine

Deferred. Temporal, Restate, DBOS, Celery, and similar systems would change
scheduling, deployment, side-effect, and recovery semantics. Theme 1 does not
provide arbitrary DAG resumption or coordinator high availability.
