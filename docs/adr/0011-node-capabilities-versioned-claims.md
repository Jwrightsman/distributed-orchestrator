# ADR 0011: Node capabilities are versioned claims with deterministic matching

- Status: Accepted
- Date: 2026-08-25
- Decision scope: worker capability registration, hard resource eligibility,
  attempt attribution, and canonical request-hash evolution

> **Partial supersession (2026-08-26):**
> [ADR 0012](0012-observed-capability-evidence-shadow-only.md) supersedes only
> this ADR's historical description of a sampled-verification routing weight and
> first-refusal deferral. Production routing no longer consumes that signal in
> any mode. The versioned-claim, immutable-snapshot, hard-matching, and attempt-
> binding decisions below remain current.

## Context

Mycelium originally routed distributed work with free-form strings such as
`gpu` or `model:qwen3.5:4b` plus flat display metadata. Those fields are useful
for compatibility, but they cannot express bounded memory, model-digest,
context, executor-protocol, or isolation requirements. The scheduler and task
poll route also had separate matching logic, making divergent eligibility
decisions possible.

Adding a defaulted requirement field to `ExecutionRequestV1` creates a second
compatibility problem: serializing the enlarged model would change digests for
idempotency mappings created before the field existed.

This decision must not confuse a worker statement with observation or trust:

```text
capability claim       what the node reports
resource requirement   a request's hard eligibility constraint
descriptor snapshot    the exact claim used for assignment
observed evidence       deferred
trust or correctness    not established by any of the above
```

## Decision

### Versioned bounded claims

Registration may include a strict `NodeCapabilityDescriptorV1` with
`descriptor_version="1"`. It contains one Ollama executor, a bounded model
list, optional architecture/CPU/memory/GPU claims, bounded typed features,
execution limits, and an explicit isolation kind. Unknown values remain null;
the stock worker advertises `isolation.kind="none"`.

The schema rejects unknown fields, unsupported versions, excessive strings and
lists, non-positive or protocol-exceeding limits, and duplicate set-like
entries. It excludes hostnames, serial numbers, MAC addresses, physical
addresses, and other unnecessary stable identifiers. Hostname remains separate
operator metadata.

`limits.max_output_bytes` is a node-advertised upper-bound claim. When the
server has an authoritative execution output budget, the same hard matcher
requires a typed descriptor's claimed maximum to be at least that budget;
equality is sufficient and a lower claim produces
`insufficient_output_capacity`. The server-derived budget is matching context,
not a new `NodeResourceRequirementsV1` field, and it does not change canonical
request or requirement hashing. The claim is neither measurement nor an
attested guarantee.

`limits.max_concurrent_execution_units` is also an informational claimed upper
bound. The coordinator does not maintain per-node slot accounting or enforce
this value, and concurrent custom polls are not a supported way to obtain such
slots. The stock worker polls and executes sequentially, which conservatively
stays within every valid advertised bound. The field does not create scheduler
slots, parallel poll positions, or capacity-weighted placement; values greater
than one are not consumed as concurrency in this protocol version.

Durable enrolled registration requires this descriptor. Descriptorless workers
remain available only through the explicitly unenrolled local compatibility
path; they cannot create a new durable enrolled attempt with a null snapshot
binding.

The stock worker constructs one descriptor after its Ollama readiness check
and reuses that object for all registrations in the process session. It uses
standard-library CPU, architecture, and physical-memory detection, and a fixed,
bounded, non-shell `nvidia-smi` query when available. Missing tools or data are
explicit unknowns. Model digest, executor version, and variant are recorded
only when Ollama returns them for the exact configured model. Strict local
overrides may correct claims, but cannot inject a model digest or raise the
coordinator's output ceiling.

Legacy `capabilities` remain bounded and are evaluated alongside typed
requirements. The coordinator records worker-supplied tags separately as
`claimed_capabilities`; server-added `model:<name>` tags are recorded as
`server_compatibility_capabilities`; their combined compatibility projection
remains `capabilities`.

### Canonical snapshots and session immutability

After schema validation, the descriptor is serialized as compact UTF-8 JSON
with sorted object keys and canonical ordering for set-like lists. Its
lowercase SHA-256 digest and version are bound to the process-local node
session. Re-registering with that session and a different version or digest
fails with `409 node_capability_descriptor_conflict`; the operator must drain
or establish a new session.

For enrolled nodes, every distinct descriptor is stored idempotently in
`node_capability_snapshots`, keyed by `(enrollment_id, descriptor_hash)`. The
canonical JSON and version are validated again on read. A repeated observation
may advance `last_seen_at`; prior JSON is never changed. This is preservation of
a claim, not measurement or attestation.

### Typed hard requirements and one matcher

`ExecutionRequirementsV1.resource_requirements` optionally carries strict
`NodeResourceRequirementsV1`, version `1`. Its deliberately small vocabulary
covers executor kind, worker-protocol version, acceptable provider/model pairs,
an exact model digest, minimum CPU and memory, GPU presence/vendor/memory,
minimum context, required typed features, and allowed isolation kinds.

One pure deterministic matcher evaluates typed requirements, legacy required
tags, the node descriptor, legacy node tags, and bounded server-derived matching
context such as the execution's required output capacity. Scheduler
qualification, eligible-set construction, worker long polling, the under-lock
handout recheck, protected operator diagnostics, and shadow candidate capture
call that matcher. It returns eligibility, stable reason codes, the matched
descriptor hash, and the selected advertised model. Model filtering and
selection share one code path: the configured model wins only when it satisfies
the request, otherwise the canonical first match is bound into the handout by
provider, name, and nullable digest. Provider/name pairs are unique within a
descriptor. The stock worker validates the binding against its immutable
descriptor before invoking Ollama; legacy unbound handouts continue to use its
configured model.
Typed and legacy constraints are both hard when both are supplied. Unknown
claimed data cannot satisfy a corresponding minimum or exact requirement. The
matcher excludes only; it does not rank eligible nodes.

The explicitly unenrolled descriptorless compatibility path has no typed
output-capacity claim. The matcher does not fabricate one or retroactively turn
the compatibility session into a typed descriptor; its documented legacy
matching behavior remains. Any task it receives is still bounded by the exact
server-issued attempt limit.

The pre-existing process-local sampled-verification pool is outside this
matcher. In local mode, an operator who explicitly enables `verify_rate` may
allow its routing weight to defer first refusal among nodes that already pass
hard eligibility. It is off by default and forced off in trusted-alpha mode.
This decision neither derives ranking from descriptors nor adds active evidence
routing.

### Attempt binding

Before a task is handed to a worker, its durable attempt records the enrollment,
node label, session, descriptor version and hash, and the canonical requirement
version and digest. The descriptor JSON is referenced through the immutable
snapshot rather than copied into each attempt. Accepted receipts retain these
bindings. Nullable columns keep historical and compatibility attempts readable.
A claim made in a later session cannot rewrite an earlier attempt.

The attempt also retains the server-issued output limit copied from the
canonical execution. A descriptor can exclude a node whose claimed capacity is
too small, but it cannot raise, replace, or negotiate that attempt limit. Output
streaming and settlement continue to enforce the durable attempt value.

### Canonical submission-hash evolution

Submission mappings now store `request_hash_version`; existing rows migrate to
version `1`. The explicit version-1 projection freezes the pre-capability
request shape and excludes `resource_requirements`. Requests with no effective
typed constraint, including an empty typed object, continue to use version 1
and preserve their prior semantic digest. A request with an effective typed
constraint uses version 2, whose explicit projection includes the complete
versioned requirement block.

On replay, the coordinator validates the stored execution and hashes the new
request with the mapping's stored serializer version. A typed request cannot be
represented by version 1 and therefore conflicts rather than silently matching.
Unknown stored serializer versions fail closed as a consistency error. Raw
request bodies and idempotency keys remain unnecessary.

## Operator and privacy boundary

The viewer-protected `GET /v1/operator/node-enrollments` returns the normalized
descriptor, version, hash, claim/tag provenance, snapshot count, and matcher
diagnostics. It may evaluate a bounded diagnostic requirement supplied by the
operator. `GET /nodes` omits full descriptor JSON, and public `/health` and
`/status.json` do not expose it. Digests are claim identifiers, not proof of
the underlying hardware or model bytes.

Protected evidence aggregates may additionally state whether an exact scope
has the immutable identity needed even to be considered by a future active
experiment. The bounded blockers are `legacy_descriptor_identity`,
`descriptor_identity_unreconstructable`, `immutable_model_identity_missing`,
and `model_identity_unreconstructable`. This derived diagnostic does not change
hard eligibility or assignment and does not itself suppress otherwise valid
shadow collection. Passing it does not establish trust, reputation,
correctness, or attestation.

## Consequences

- Hard resource eligibility is explicit, bounded, versioned, and consistent
  between scheduling and handout.
- A typed node claiming less output capacity than a task requires is excluded
  everywhere the canonical matcher is used, while the issued attempt remains
  the output-budget authority.
- Legacy workers remain usable only as explicitly unenrolled local compatibility
  sessions. Legacy tag requirements remain usable alongside typed descriptors.
- Enrolled attempt history can identify the exact claim and requirements used
  without duplicating large JSON.
- Operators must stop/drain and create a new session to change a live claim.
- A malicious admitted worker can still lie about every claimed value.
- Versioned serializers preserve old idempotency mappings while allowing the
  request contract to evolve deliberately.

## Deferred work

Observed benchmarks, durable operational evidence, descriptor-derived
performance ranking, global reputation, correctness scoring, new active
evidence-based routing, hardware/model attestation, general executor plugins,
and arbitrary Boolean policy languages are deferred. They require separate
evidence, trust, privacy, and migration decisions. This does not remove or
endorse the older optional local sampled-verification first-refusal behavior
described above. In particular, a descriptor hash is not an attestation digest.
No worker concurrency, parallel polling slots, or capacity-weighted scheduling
is authorized by the concurrency-limit claim.

## Rejected alternatives

### Replace legacy strings immediately

Rejected because existing workers and callers use them. They remain an
explicitly separated compatibility layer and are enforced together with typed
constraints.

### Let scheduler and task-poll routes interpret requirements independently

Rejected because two implementations can disagree after a task is queued. One
pure matcher defines the hard eligibility contract.

### Hash every request with the enlarged defaulted model

Rejected because it would create false conflicts for durable pre-upgrade
idempotency mappings. Stored serializer versions make compatibility explicit.

### Treat auto-detection or operator overrides as verified facts

Rejected because both remain node-controlled claims. Trustworthy observation
and attestation require different mechanisms.

### Add arbitrary expressions or performance preferences

Rejected because an unbounded policy language is hard to validate and migrate,
and performance preference is not a hard capability constraint.
