# ADR 0008: Make canonical execution submission requester-scoped and idempotent

- Status: Accepted
- Date: 2026-08-23
- Decision scope: canonical HTTP submission, execution persistence, and retry behavior

## Context

`POST /v1/executions` creates an execution and starts process-local work. A
client cannot reliably know whether a lost response means that submission
failed or that the coordinator accepted it. Retrying the request without a
stable identity can therefore create duplicate execution rows, model work,
artifacts, callbacks, and contribution records.

The worker-attempt protocol already has exact replay for accepted worker
results, but that protects a different boundary. It does not make the
requester's initial execution submission safe to retry.

The trusted alpha has one shared pitch credential rather than durable user
accounts. Open development mode may have no requester credential at all. The
submission design must work within those limits without representing a network
address as durable identity.

## Decision

The canonical HTTP endpoint accepts an optional `Idempotency-Key` header. A
valid key contains 1–128 printable ASCII characters and is not whitespace-only.
The raw key is neither logged nor stored.

After FastAPI and `ExecutionRequestV1` validation, the coordinator serializes
the validated model with all explicit and defaulted values, sorted object keys,
compact separators, and UTF-8 encoding. SHA-256 of those canonical bytes is the
request digest. Reordered input object keys therefore produce the same digest;
a materially different validated request produces a different digest.

Idempotency keys are requester-scoped:

- when `pitch_key` is configured, the scope is a domain-separated SHA-256
  digest of that configured credential; and
- in open development mode, the scope is a domain-separated SHA-256 digest of
  the direct ASGI peer address in `request.client.host`.

The coordinator does not consume `X-Forwarded-For` or related forwarding
headers. Open-mode peer scoping is a best-effort duplicate boundary for local
development, not durable user identity. The key itself is also stored only as a
domain-separated SHA-256 digest.

SQLite retains one mapping from `(requester_scope_hash,
idempotency_key_hash)` to the canonical request digest and execution ID.
Mappings are retained indefinitely during trusted alpha. The mapping and the
initial queued execution row are created together in one immediate SQLite
transaction. Process-local controls, live-cache publication, lifecycle events,
callbacks, and task scheduling begin only after that transaction commits.

The service allocates one execution ID and one complete initial queued snapshot
before entering its bounded persistence retry loop. Every retry for that live
submission call carries the same candidate. The submission contract has four
distinct outcomes:

1. **Created.** No mapping exists. The transaction creates one execution ID,
   queued snapshot, and mapping. The HTTP response remains `202 Accepted` and
   includes `Idempotency-Replayed: false`.
2. **Recovered creation.** A transaction committed that live call's candidate,
   but the persistence method raised before returning its outcome. A retry may
   recover creation only when the requester scope digest, key digest, canonical
   request digest, stable candidate execution ID, and complete initial snapshot
   all match. The service must also have observed the earlier ambiguous
   persistence exception in that same call; an exact candidate match on attempt
   one remains a historical replay. The service activates a proven recovered
   candidate once and returns `Idempotency-Replayed: false`.
3. **Replayed.** The mapping exists and the canonical request digest matches,
   but it belongs to another candidate allocation. This is an ordinary
   concurrent, earlier-process, or historical replay. The endpoint returns the
   existing durable execution, in any lifecycle state, with
   `Idempotency-Replayed: true`. It does not schedule work or repeat events,
   callbacks, artifacts, or contribution records.
4. **Conflict.** The mapping exists but the request digest differs. The
   endpoint returns `409 Conflict` with stable code `idempotency_conflict` and
   does not mutate or schedule anything.

A mapping whose execution row is absent or invalid is a consistency failure.
The coordinator fails closed with a service-unavailable response rather than
creating a replacement identity.

Requests without the header retain their previous behavior: every accepted
request creates a new execution, and the response omits
`Idempotency-Replayed`. Authentication, canonical validation, authorization,
and rate limiting still run for replays.

Unkeyed creation uses the same stable candidate within its active bounded retry
loop. An already-present execution ID is accepted as recovery only when the
stored canonical request and complete initial row exactly equal that candidate,
and only after the same call observed an earlier persistence exception. An
attempt-one match fails closed. This exception is limited to initial creation;
normal lifecycle writes do not become idempotent merely by execution ID.

Local activation is process-idempotent. A matching active control/task pair
prevents repeat scheduling, while partial or inconsistent local state fails
closed. If the activation preflight, control construction, live-cache
publication, or task registration fails after durable creation, the service
does not publish running state or invoke lifecycle callbacks. It durably
transitions the execution to `interrupted` with stable reason and error code
`submission_activation_failed`, publishes that interruption only after commit,
and returns `503 Service Unavailable` to the still-active request. A later keyed
retry is an ordinary replay of that same interrupted execution.

Activation publication and terminal cancellation share the same process-local
critical section, so cancellation cannot be overwritten by a late queued-cache
write. If a created task exits before terminal truth can be read or committed,
the process retains a fail-closed activation claim instead of scheduling a
replacement task; restart reconciliation remains responsible for converting
the durable nonterminal row to `interrupted`.

## Consequences

- A caller can safely retry one canonical logical submission after a timeout or
  lost response.
- Concurrent requests using the same scope, key, and canonical request converge
  on one execution and one scheduled task.
- A caller must use a key for only one logical request. Reusing it with a
  different validated request is an explicit conflict.
- A shared `pitch_key` means its holders share one requester scope. Individual
  requester isolation requires a future credential/account model.
- Indefinite mappings add bounded SQLite retention per keyed submission and
  become part of backup and restore.
- Key and requester credential plaintext do not enter the mapping table.

## Recovery boundary

Ambiguous-commit recovery is bounded to an exception observed by the same live
submission call and proven by its preallocated candidate identity. It does not
make activation, scheduling, or workflow state durable. A process crash or an
abandoned call after the queued transaction commits can still leave the
execution unscheduled until restart reconciliation marks it `interrupted`.
Retrying with the same key then returns that same interrupted execution without
resuming it. Starting replacement work requires a new key or an unkeyed
submission.

This decision also does not establish exactly-once model calls, worker calls,
artifact writes, callbacks, or other external side effects. It prevents a
canonical retry from intentionally creating a second execution. Side effects
within a started execution retain their existing lifecycle and attempt
semantics.

## Rejected alternatives

### Make the raw key globally unique

Rejected because unrelated requesters must be able to choose the same ordinary
key, and plaintext keys or credentials should not become durable secrets.

### Hash the original HTTP body

Rejected because JSON key order and omitted defaults would make semantically
equivalent validated requests conflict. The protocol model, not transport byte
layout, defines the request.

### Store the mapping and execution in separate commits

Rejected because a crash between commits could leave either duplicate work or
an unusable mapping. The queued snapshot and mapping are one integrity boundary.

### Resume work when a key is replayed

Rejected because idempotency is identity deduplication, not workflow
resumption. The scheduler, coroutines, node sessions, and dispatcher waits
remain process-local.

### Call this exactly-once execution

Rejected because the database cannot atomically control arbitrary external
model or worker side effects. The supported claim is idempotent canonical
submission.

### Extend the header to every interface now

Deferred. Legacy HTTP, CLI, and MCP adapters keep their existing contracts in
this change. The service and persistence primitives are reusable when those
interfaces receive an explicit compatibility design.
