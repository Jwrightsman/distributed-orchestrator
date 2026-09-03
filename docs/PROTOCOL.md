# Mycelium Execution Protocol v1

This document defines the coordinator, client, and worker contracts implemented
by Mycelium for its private trusted alpha. The key words **MUST**, **MUST NOT**,
**SHOULD**, **SHOULD NOT**, and **MAY** are normative.

The trusted-alpha boundary matters: a shared secret admits initial enrollment,
then a distinct per-node bearer credential authenticates durable returning
identity and independently revocable sessions bind work. This prevents an
unbound or late worker result from becoming an execution result. It does not
provide public-key or physical-machine identity, a generated-code sandbox, or
permissionless-network defenses.

## Scope and versioning

Protocol v1 has two production execution strategies: `dag` version `1` and
`ensemble` version `1`. `direct` is a request alias for ensemble with one
candidate. `auto` is a deterministic selector policy, not another strategy.

Protocol and strategy versions are independent. The coordinator rejects an
unsupported `protocol_version`, unknown fields, unknown strategies, malformed
JSON Schema, and incoherent strategy, placement, confidentiality, or consent
combinations with HTTP `422` at the canonical REST boundary.

## Canonical request

`ExecutionRequestV1` is a strict, bounded Pydantic model. Its privacy-safe
defaults are local placement and local-only confidentiality:

```json
{
  "protocol_version": "1",
  "task": "Build one self-contained HTML artifact",
  "strategy": "auto",
  "placement": "local",
  "remote_dispatch_consent": false,
  "requirements": {
    "required_capabilities": [],
    "approved_node_ids": [],
    "allow_local_fallback": true,
    "resource_requirements": null
  },
  "verification": {
    "validators": [],
    "allow_unverified_fallback": true,
    "require_all": true
  },
  "confidentiality": "local_only",
  "timeout_seconds": 1800,
  "max_output_bytes": 1048576,
  "network_policy": "disabled"
}
```

Remote-capable canonical calls must make consent explicit. For example:

```json
{
  "protocol_version": "1",
  "task": "Generate three complete alternatives",
  "strategy": "ensemble",
  "strategy_options": {
    "kind": "ensemble",
    "candidates": 3,
    "concurrency": 2,
    "selection_policy": "validated_score"
  },
  "placement": "distributed",
  "remote_dispatch_consent": true,
  "confidentiality": "trusted_guild"
}
```

`task` is nonblank and at most 1,000 characters. Candidate count, concurrency,
DAG subtasks, identifiers, schemas, file lists, output sizes, and deadlines are
bounded in `execution/contracts.py`.

`requirements.resource_requirements` optionally contains strict
`NodeResourceRequirementsV1` with `requirement_version="1"`. Its hard,
bounded fields are `allowed_executor_kinds`,
`required_worker_protocol_version`, `acceptable_models` (provider/name pairs),
`exact_model_digest`, `minimum_logical_cpus`, `minimum_memory_bytes`,
`gpu_required`, `allowed_gpu_vendors`, `minimum_gpu_memory_bytes`,
`minimum_context_tokens`, `required_features`, and
`allowed_isolation_kinds`. Omitted or effectively empty typed requirements
preserve previous behavior. `required_capabilities` remains the legacy string
compatibility contract; when both forms are present both must match.

The execution's independent `max_output_bytes` is authoritative server matching
context, not a `NodeResourceRequirementsV1` field. It therefore does not change
the resource-requirement or canonical request hash projections described below.

### Strategy options

`DagOptionsV1` contains `maximum_subtasks` from one through five and booleans
for review and revision. `EnsembleOptionsV1` contains one through five
candidates, bounded concurrency no greater than the candidate count, and a
selection policy of `validated_score` or `first_valid`.

The option discriminator may be inferred only when the supplied fields are
unambiguous. A conflicting strategy and option family is invalid.

## Idempotent canonical submission

`POST /v1/executions` accepts an optional `Idempotency-Key` request header. A
valid value contains 1–128 printable ASCII characters, contains no control
characters, and is not empty or whitespace-only. The coordinator does not
normalize an otherwise valid value and never stores or logs its plaintext.

After the body is parsed and validated as `ExecutionRequestV1`, the coordinator
selects an explicit request-hash serializer. Version 1 projects exactly the
pre-capability request shape. Requests with no effective typed constraint keep
that serializer and their pre-upgrade semantic digest. Version 2 adds the
complete versioned typed requirement block and is selected only when it has an
effective constraint. Both serializers use explicit/defaulted values, sorted
JSON keys, compact separators, UTF-8, and SHA-256; original HTTP byte layout and
object-key order do not affect the result.

The key is scoped to the requester. When `pitch_key` is configured, a
domain-separated SHA-256 digest of that configured credential defines the
scope. Otherwise, open development mode uses a domain-separated digest of the
direct ASGI peer address from `request.client.host`. Mycelium does not trust
`X-Forwarded-For` or related headers, and open-mode peer scoping is not durable
user identity. The key itself is stored only as a separately domain-separated
SHA-256 digest.

The initial queued execution and its `execution_submissions` mapping, including
`request_hash_version`, commit in one immediate SQLite transaction. Existing
rows migrate with version `1`. On replay the stored version selects the
serializer; an unrepresentable typed change conflicts and an unknown stored
version fails closed as a consistency error. Only after commit may the service create
process-local controls, publish its live snapshot and creation event, or
schedule work. A keyed request has these responses:

| Condition | HTTP behavior |
| --- | --- |
| No existing mapping | Existing `202` body plus `Idempotency-Replayed: false` |
| The same live call recovers its preallocated candidate after an unknown commit outcome | Existing `202` body plus `Idempotency-Replayed: false` |
| Same scope, key, and canonical request from another candidate allocation | Existing execution with `202` and `Idempotency-Replayed: true` |
| Same scope and key, different canonical request | `409` with `detail.code=idempotency_conflict` and the existing execution ID |
| Mapping does not resolve to a valid execution | Fail-closed `503` with `detail.code=idempotency_consistency_error` |
| Durable creation succeeds but local activation setup fails | Fail-closed `503` with `detail.code=submission_activation_failed` and the interrupted execution ID |

A recovered creation is proven by the requester/key/request digests and the
stable execution ID allocated before the live call's bounded persistence retry
loop, after that call has observed an ambiguous persistence exception. An exact
candidate match on the first attempt is still an ordinary replay. A proven
recovered creation is activated once. An ordinary replay returns the existing
execution whether it is queued, running, completed, failed, cancelled, or
interrupted. It does not emit another creation event, schedule another task,
invoke callbacks, recreate artifacts, or create contributions. Authentication,
request validation, authorization, and rate limiting still run.

If the durable activation preflight, local control construction, live-cache
publication, or task registration fails after durable creation, the service
publishes no running state. It first commits the execution as `interrupted` with
reason and error code `submission_activation_failed`, then returns `503`.
Reusing the same key later returns that same interrupted execution with
`Idempotency-Replayed: true` and does not reschedule it.

Without the header, every external submission continues to create a new
execution, and the response omits `Idempotency-Replayed`. The live service call
may internally recover an exact initial row after an unknown commit outcome,
but an exact row found before that call observes a persistence exception fails
closed, and a later unkeyed HTTP retry has no deduplication identity. Mappings
are retained indefinitely during trusted alpha.

This is submission deduplication, not workflow resumption or exactly-once
external side-effect execution. A replay after restart returns the same
interrupted execution; starting replacement work requires a new key or no key.
See [ADR 0008](adr/0008-idempotent-canonical-execution-submission.md).

## Strategy semantics

### DAG version 1

DAG uses the existing planner, dependency-aware builder waves, optional review,
optional revision, artifact extraction, and project-memory path. Each builder
subtask is an execution unit and uses the shared dispatcher. Planning, review,
and revision execute on the coordinator; builder units may execute locally or
on admitted workers according to placement.

### Ensemble version 1

Each candidate receives the complete task and output contract. Candidate
generation, directory creation, materialization, extraction, and validation
are isolated so one candidate failure does not fail unrelated candidates.

Each completed candidate is validated independently. With
`selection_policy="validated_score"`, accepted candidates are ordered by
assurance strength, meaningful validator score, lower generation latency, and
stable candidate identifier. Output length is not a quality tie-breaker.
`first_valid` means the first candidate to finish with acceptable validation
evidence, not the first candidate in input order.

If no candidate satisfies the required validation policy, the execution fails
unless `allow_unverified_fallback` is true and a usable candidate completed. An
unverified fallback has lifecycle `completed`, validation outcome `failed` or
`partial`, and compatibility status `unverified`. Its assurance still records
the strongest limited evidence that actually passed (for example `structural`);
that evidence is not a general correctness claim.

### Direct and auto

`strategy="direct"` is recorded as requested and normalizes to ensemble with
one candidate and concurrency one.

Selector `conservative-v2` is deterministic:

1. An explicit non-auto strategy wins.
2. Explicit ensemble options select ensemble; one candidate represents direct
   execution.
3. A contract with deterministic JSON Schema conformance may select ensemble.
4. Extraction, parsing, and manifest checks are structural only and do not by
   themselves cause auto-selection to claim deterministic comparison.
5. Missing or ambiguous contract information selects DAG for compatibility.

The normalized result records the selected strategy, selector reason, and
selector version. Selection never spends a model call.

## Placement, confidentiality, and remote consent

Placement is independent from strategy:

- `local` runs execution units through the coordinator's model integration.
- `distributed` sends eligible units to admitted workers. If none is available,
  local fallback occurs only when `allow_local_fallback` is true.
- `auto` chooses a qualifying worker when policy permits and otherwise chooses
  local execution.

`confidentiality="local_only"` prohibits remote dispatch. `approved_nodes`
requires a nonempty allowlist, and capability and blacklist filters apply before
assignment. Remote-capable placement (`auto` or `distributed` with a
non-local-only confidentiality class) requires
`remote_dispatch_consent=true`. Consent is rejected when the request is not
remote-capable, so the stored bit has an unambiguous meaning.

The result distinguishes requested, planned, and observed placement. It records
`observed_placements`, local and distributed unit counts, fallbacks, attempts,
reassignments, and retries. A mixed run is reported as mixed; the legacy
`placement_selected` field becomes null when it cannot truthfully represent the
observed placements.

Legacy pitch adapters may synthesize remote consent to preserve their documented
historical placement behavior. That compatibility exception does not change the
canonical defaults.

### What `network_policy` means today

`network_policy` records caller intent as `disabled`, `restricted`, or
`allowed`. It is **not currently enforced** by an OS sandbox, firewall, tool
broker, or worker runtime. It does not prevent Ollama, generated code later run
by an operator, or a custom model provider from using the network. Do not treat
the field as a security boundary.

## Project memory

DAG supports bounded project memory through its existing project pipeline.
Ensemble and direct reject `project_id` with a validation error. They do not
silently accept an identifier while ignoring its memory. Selected-result-only
memory updates are a prerequisite for adding parity later. A DAG iteration is
added only after the canonical terminal snapshot and its normal lifecycle event
are published; terminal-persistence failure therefore leaves project memory at
its previous committed iteration.

## Lifecycle, deadlines, cancellation, and restart

Canonical lifecycle and assurance are separate dimensions.

`lifecycle_status` is one of:

- `queued`
- `running`
- `completed`
- `failed`
- `cancelled`
- `interrupted`

Durable storage is authoritative for each lifecycle snapshot. Required state
follows this order: construct and validate, commit to SQLite, publish a deep
process-local live snapshot, emit the normal lifecycle event, invoke callbacks
or legacy mirrors, and return or otherwise expose the state. A failed progress
write cannot advance the live snapshot. Normal terminal events, completion
callbacks, compatibility terminal mirrors, and terminal artifact/share
publication require a committed terminal snapshot.

`timeout_seconds` is a total deadline beginning when the canonical execution is
queued. Planning, local generation, worker waits, validation, review, revision,
and artifact registration consume the same remaining budget. Worker leases are
capped by that deadline. A canonical validator child, including repair-time
parsing and registry validation, receives no more than the lesser of its
configured timeout and this remaining budget. Timeout
cancels queued and active units, requests termination/reaping of an active
validator process, rejects later worker results, persists a terminal failure
with `execution_timeout`, and marks the result retryable. Failure to confirm
process-tree cleanup is separately counted and must be treated as a containment
incident.

`POST /v1/executions/{execution_id}/cancel` is idempotent. It records the
cancellation request and timestamps, signals local work, removes queued worker
units, durably cancels active attempts, rejects late submissions, persists
terminal `cancelled`, and emits an `execution_cancelled` event. Cooperative
cancellation cannot forcibly stop work inside an external service that ignores
task cancellation; its result is nevertheless no longer admissible.

Required persistence uses finite retries and raises a typed error after they
are exhausted. An active HTTP cancellation or synchronous compatibility
request then returns a sanitized `503` and does not claim the transition.
Permanent asynchronous terminal-persistence failure leaves the last durable
state intact, suppresses the normal terminal event and completion callback,
does not publish an uncommitted live snapshot or terminal artifact/share, and
cleans up safe process-local resources without recursively constructing another
unpersisted terminal result.

At coordinator startup, persisted canonical executions and legacy jobs left in
`queued` or `running` are not resumable because the scheduler and background
coroutines are process-local. Reconciliation transactionally moves them to
`interrupted`, records a restart marker, reason and timestamp, marks them
retryable, and is idempotent. Active worker attempts are durably interrupted so
late results fail closed. Once terminal state is committed, later event,
callback, or callback-metadata failure does not undo or reclassify it. A
secret-safe persistence diagnostic is telemetry, not authoritative lifecycle
truth.

Legacy `output/` consumers (`/history`, `/gallery`, `/run`, status/try views,
CLI history, downloads, and demo capture) use the same terminal publication
gate. Current execution-linked runs require artifact-root ownership plus the
sealed manifest hash committed in the terminal execution. Unmarked historical
records retain bounded live-file compatibility, but a restart-reconciled row is
never treated as proof that its previously staged terminal files committed.

The queue itself is still not durable. Restart reconciliation makes its loss
truthful; it does not resume the lost work. See
[ADR 0009](adr/0009-durable-terminal-commit-before-publication.md).

## Validation outcome and assurance

`validation_outcome` is `passed`, `failed`, `partial`, or `not_run`.
`assurance_level` is `unverified`, `structural`, `deterministic`, or
`model_judged`.

Every normalized result includes a validation summary containing checks run,
checks passed, checks failed, checks not run, an assurance level, and whether
any evidence establishes behavioral correctness. The current built-in
structural and contract validators do not establish general behavioral
correctness.

### Contract floors

The output contract creates mandatory validators that explicit policy cannot
remove:

| Contract | Required floor |
| --- | --- |
| any / text | nonempty |
| `structured_json` | nonempty + valid JSON; JSON Schema when supplied |
| `file_manifest` | nonempty + extraction + exact artifact contract + exact normalized manifest |
| `single_artifact` | nonempty + extraction + exact count/format contract; supported parser for Python/HTML formats |
| `code` | nonempty + extraction + artifact contract + supported parser checks |

Explicit validators add requirements. Contract floors always use AND
semantics. `verification.require_all` applies only to explicit required
validators: true requires all; false requires at least one. Optional validators
never decide acceptance.

JSON Schema uses draft 2020-12 and is itself validated when the request is
parsed. Manifest paths use normalized POSIX-relative paths. Absolute paths,
drives, backslashes, parent traversal, dot or empty segments, duplicate
normalized paths, wrong exact counts, and mechanically checkable wrong formats
are rejected.

`nonempty`, artifact extraction, artifact contract, file manifest, code parse,
and valid JSON are structural evidence. JSON Schema provides deterministic
contract-conformance evidence. None proves that arbitrary code behaves as the
requester intended. Unsupported code formats are reported as not checked.

### Validator execution boundary

The registry is a closed allowlist of versioned built-ins. Its parent-owned
execution classification is:

| Class | Validators in `auto` |
| --- | --- |
| `subprocess_isolated` | `code_parse`, `structured_json`, `json_schema` |
| `inline_trusted` | `nonempty`, `artifact_extraction`, `artifact_contract`, `file_manifest` |

`validator_execution_mode="auto"` is the default and follows this table.
`subprocess` forces every current built-in through the compatible runner.
`inline` is an explicit weaker local-development mode; trusted-alpha preflight
rejects it. In that mode evidence labels normally inline checks
`inline_trusted` and overridden isolated parsers `inline_compatibility`. An
isolated validator never silently falls back inline after a runner failure.

New parent calls exchange strict `ValidatorRunnerRequestV2` and
`ValidatorRunnerResponseV2` JSON over bounded stdin/stdout. The request contains
control metadata only: protocol and built-in identity/version, an optional
output reference, the validator-specific bounded contract projection,
validated normalized logical filenames, and parent-clamped limits. It never
contains the generated output body and cannot name a module, executable, shell
command, arbitrary callable, database, credential, session, nonce, or unrelated
execution.

The V2 output reference has this exact shape and fixed server-owned path:

```json
{
  "relative_path": "__mycelium_validator_input__/output.utf8",
  "encoding": "utf-8",
  "byte_length": 12345,
  "sha256": "<64 lowercase hexadecimal characters>"
}
```

The path and encoding are literals, `byte_length` is an integer from zero
through 10,485,760, SHA-256 is lowercase hexadecimal, and unknown fields are
forbidden. `nonempty`, `structured_json`, and `json_schema` require the
reference; file-only validators reject it. The child cannot select a different
relative or host path.

V1 remains explicitly parseable and round-trippable for compatibility and
focused tests, including its inline output field. New production parent calls
emit V2 only. Parsing first requires a bounded string `protocol_version` and
then selects exactly that strict model. A missing, malformed, or unknown
version fails clearly; malformed V2 is not interpreted as V1; and a V2 failure
does not cause a V1 retry or inline execution. Response protocol version,
validator identity, and validator version must match the request.

Unknown fields, invalid or mismatched identity/version, malformed JSON,
excessive depth/items/strings, a recognized bare or delimiter-prefixed absolute
POSIX/Windows/UNC host-path pattern, any `file://` pattern in response keys or
values, and over-limit request/response bytes fail closed. The response wire
model has no host-path field, and child path-like detail is never authoritative.

`validator_subprocess_request_max_bytes` bounds only the serialized JSON control
metadata. Its default is 2 MiB. The referenced raw UTF-8 output is independently
bounded by the execution's authoritative `ExecutionRequestV1.max_output_bytes`,
whose canonical maximum is 10,485,760 bytes. Exact-boundary output can reach an
applicable isolated validator without JSON escaping or JSON-in-JSON expansion
consuming the control-message budget. Increasing the V1 request limit is not the
transport solution and no second configurable output ceiling exists.

The response contains only identity/version, boolean outcome, bounded optional
score/detail, and bounded failure reason. Child detail and ordinary validation
reasons are bounded but non-authoritative. The authoritative parent adds
required/optional and contract-floor source, assurance, behavioral-correctness,
duration, execution mode, runner protocol version, platform containment level,
and termination reason when applicable. No child response can raise assurance
or claim behavioral correctness. V2 recursively forbids private reference field
names in response detail. For output-consuming validators, detail, score, and
ordinary failure reasons must also match the selected built-in's closed response
shape; a child attempt to echo output or transport metadata is rejected as a
malformed response before evidence is assembled.

Artifact-aware subprocess checks can receive only normalized logical names from
the authoritative selected candidate subtree. Canonical strategy paths bind
both repair-time parsing and registry validation to the current validated entry
snapshot and shared executor. The parent rejects traversal, symlinks, special
files, duplicate portable paths, out-of-root inputs, names outside the supplied
snapshot, excessive file counts, and overlong relative paths.

`code_parse` additionally receives fresh regular-file byte copies. Its content
path enforces per-file and aggregate size limits, checks authoritative size and
SHA-256 when supplied, and never creates hard links. The current fixed copy
limit is 20 files, 10 MiB per file, 10 MiB aggregate, and 200 characters per
relative path. Standalone compatibility callers without an ArtifactStore retain
the root/path/type/size boundary without the optional snapshot hash check.

For every output-consuming subprocess check, the parent strictly encodes the
canonical output as UTF-8, enforces the execution byte budget before spawn, and
exclusively creates
`__mycelium_validator_input__/output.utf8` inside the fresh workspace. It uses
descriptor-relative and no-follow operations where supported, applies POSIX
mode `0700` to the reserved directory and `0600` to the file, writes and hashes
the exact bytes, and closes the file before process creation. The reserved
namespace and every descendant are forbidden candidate artifact paths, with
portable collision handling. The output path is never derived from task data,
model text, a manifest, or a caller-selected field.

The child confines the fixed path to the stage root and rejects missing files,
symlinks, Windows reparse points, directories, FIFOs, sockets, devices, and
other nonregular targets. It opens the file once and validates through that
descriptor, checking regular-file identity, the hard maximum, exact declared
byte length, constant-time SHA-256 equality over the bytes consumed, and strict
UTF-8 decoding before calling the selected built-in. Reference, file-type,
size, digest, UTF-8, and oversize failures use stable content-free reasons and
fail as runner infrastructure errors. The child never receives a coordinator
artifact root.

The bounded output-reference reason family is
`validator_output_reference_missing`, `validator_output_reference_invalid`,
`validator_output_file_missing`, `validator_output_file_not_regular`,
`validator_output_size_mismatch`, `validator_output_digest_mismatch`,
`validator_output_invalid_utf8`, and `validator_output_oversized`. Parent-side
exclusive-write failure is reported separately as content-free
`validator_output_staging_failed`. None includes a path, digest, body, excerpt,
or decoder error text.

When `artifact_extraction`, `artifact_contract`, or `file_manifest` is forced
through `subprocess`, the request carries only the validated names and bounded
contract projection. The child runs in an empty private directory; the parent
does not copy or rehash artifact content for those metadata-only checks. The
same root/subtree confinement, regular-file and symlink/special-file rejection,
snapshot-membership, file-count, portable-duplicate, and relative-path checks
still apply, while content-copy byte limits do not.

Process-tree termination and reaping are attempted before private-workspace
removal on success, validation or reference failure, malformed response, crash,
spawn failure, timeout, or cancellation. Failure to
confirm process-tree cleanup remains a bounded, counted containment incident.
Temporary-workspace deletion failure records fail-closed
`validator_stage_cleanup_failed` evidence and increments the distinct
`staging_cleanup_failures` counter; deletion failure can still leave a stale
stage. Staging is validator input, not artifact publication or a sealed-manifest
replacement. Copy limits are distinct from artifact delivery quotas and the
configurable serialized request/response limits.

The child uses the current Python interpreter without a shell, a sanitized
environment whose `TEMP`/`TMP` on Windows or `TMPDIR` on POSIX point into its controlled
working directory, closed inherited descriptors where supported, and a fresh
process group. Spawn failure, timeout, crash, malformed or oversized response,
and artifact/output staging or output-reference failure become bounded error
evidence with stable parent-supplied reasons. An unconfirmed process-tree
cleanup is reflected in error evidence or
the content-free cleanup counter, depending on the original outcome. Existing
required/optional aggregation then records whether validation accepts the
candidate; the isolated check never falls back inline. Afterward, the explicit
`allow_unverified_fallback` policy may still select and deliver a usable failed
candidate with its validation outcome preserved as described above. Runner
failure itself does not define a new lifecycle transition.

Parent-authored evidence may record protocol version `2`, execution mode,
containment level, termination reason, and existing bounded validator detail.
It does not expose the output reference, reserved private path, expected or
observed digest, generated body, output excerpt, or child environment. Logs,
metrics, and operator counters follow the same content-free rule;
validator-runner metadata adds none of those fields to public executions or
shares. This does not redefine their existing result-content policy.
Output-reference counters use only fixed low-cardinality failure categories.

On POSIX the child also applies available standard-library CPU, address-space,
file-size, open-descriptor, and child-process limits. Windows retains the
wall-clock deadline, bounded pipes, staging, protocol checks, and process-tree
cleanup on a best-effort basis, but does not claim those POSIX resource limits.
A child still runs as the coordinator's operating-system user, so neither
platform provides mandatory filesystem confidentiality or reliable network
denial through this boundary.
On Windows the stage inherits the host temporary root's ACL; POSIX mode bits do
not establish a private DACL, so operators must secure that root.
The process group is not a parent-death primitive: abrupt coordinator loss is
not an ordinary cancellation path. POSIX installs an early hard child alarm;
Windows has no equivalent child-side alarm. A successfully assigned parent-owned
Job Object adds best-effort kill-on-close cleanup, but the pre-assignment and
restrictive-outer-Job gaps remain, and restart does not discover an unassigned
validator orphaned by the prior process.

Generated code is parsed as data only. Production validation does not import a
generated module, run its top-level statements, invoke a shell/build script,
install packages, execute tests, start a browser, or perform generated-code
networking. This boundary adds parser-process containment, not behavioral
validation or a hostile-code sandbox. See
[ADR 0013](adr/0013-parser-heavy-validators-bounded-process-boundary.md).

## Result model and compatibility projection

`ExecutionResultV1` includes stable execution identity; lifecycle, validation,
and assurance; requested and selected strategy; requested, planned, and
observed placement; consent; timestamps and deadline; units and candidates;
winner explanation; validation summaries; role-scoped artifact API references,
sealed-manifest identity and integrity mode; bounded previews; participation
and contribution records; post-hoc verification state; structured errors; and
bounded telemetry.

The older `status` field remains an explicit compatibility projection. The
canonical service projects lifecycle `completed` with validation outcome
`passed` as `status="completed"`; other lifecycle-completed outcomes project as
`status="unverified"`. New clients must use `lifecycle_status` for control flow
and `validation_outcome` plus `assurance_level` for trust decisions.

Canonical responses do not publish absolute artifact paths. The historical
`output_reference` name now points to the authenticated artifact API when an
artifact root is available. Filesystem paths may remain in authenticated legacy
adapter payloads for compatibility and must not be copied into public shares.

`posthoc_verification_status` is a separate field, not a replacement for
terminal validation or assurance. It can be `disabled`, `not_requested`,
`pending`, `running`, `completed`, or `failed`, with bounded timestamps,
agreement, and reason fields. Trusted-alpha reports this legacy execution-level
field as `disabled`; it is not the scoped capability-evidence aggregate described
below. Canonical submission idempotency does not run or authorize duplicate
verification. A UI MUST NOT render disabled, failed, or shape-agreement state as
evidence that a result passed or failed its original validators.

## Node registration sessions and bounded worker I/O

The protocol separates durable enrollment, a human label, a live incarnation,
and lease authority:

```text
enrollment_id = immutable enrolled contributor identity
node_id       = normalized display label
session_id    = one live worker-process incarnation
attempt_id    = one leased execution authority
```

`POST /nodes/register` has explicit `bootstrap` and `returning` enrollment
actions. Bootstrap requires `X-Node-Secret` plus a high-entropy worker-proposed
`enrollment_credential`. Returning registration requires the node label and
that per-node credential but not the shared secret. The coordinator stores only
a domain-separated credential digest. Bootstrap resolves the node label first: a
label held by a revoked enrollment is rejected as revoked even when the exact
original credential is presented, because otherwise revocation would be undoable
by the party it was used against. Only then does a matching bootstrap retry
return the same enrollment, and only a label nobody holds falls through to the
credential check. A label or credential collision returns a stable conflict and
never overwrites the existing row.

Only after durable enrollment succeeds does registration issue a non-secret
`session_id`, a random plaintext `session_token` returned to the worker, and
start/expiry timestamps. The coordinator stores only the session-token digest.
The stock worker persists the enrollment credential in its private,
coordinator-scoped identity file and keeps the session token only in memory.
Identity schema version 1 also binds the normalized coordinator origin and node
label, nullable pre-bootstrap enrollment ID/version, and positive enrolled
`credential_version`. Coordinator URLs with userinfo, paths, queries, or
fragments are rejected. The stock worker deliberately ignores ambient HTTP(S)
proxy environment variables so bearer credentials are not silently routed to
an inherited proxy, and it requires an explicit origin instead of selecting an
unauthenticated LAN-discovery response.

Poll, heartbeat, drain, stream, token-stream, and result routes require the
current `X-Node-Session`. An enrolled session recovers its enrollment binding
server-side and does not present `X-Node-Secret` again. Every operation checks
that the durable enrollment is active, still owns the label, and has the
session's credential version. Revocation or rotation rejects the operation,
invalidates the incarnation, and reclaims active work. Sessions are
process-local, expire after at most 24 hours, and become invalid on coordinator
restart. The returning credential obtains a fresh session while keeping the
same enrollment ID.

An enrolled `bootstrap` or `returning` registration must include a strict
`capability_descriptor` with `descriptor_version="1"`:

```json
{
  "descriptor_version": "1",
  "executor": {
    "kind": "ollama",
    "version": "0.12.3",
    "worker_protocol_version": "1"
  },
  "models": [{
    "provider": "ollama",
    "name": "qwen3.5:4b",
    "digest": null,
    "context_tokens": 8192,
    "variant": null
  }],
  "hardware": {
    "architecture": "x86_64",
    "logical_cpu_count": 8,
    "total_memory_bytes": 17179869184,
    "gpus": null
  },
  "features": [],
  "limits": {
    "max_concurrent_execution_units": 1,
    "max_output_bytes": 10485760,
    "max_context_tokens": 8192
  },
  "isolation": {"kind": "none"}
}
```

All fields and collections are bounded and unknown fields are forbidden. Each
model provider/name pair is unique within a descriptor, so one runnable name
cannot claim conflicting digests or capacities. A model digest is nullable and,
when present, is a normalized SHA-256 supplied by the runtime; a model name is
not converted into a digest. The hardware block
excludes hostnames, serial numbers, MAC addresses, and physical identifiers.
Descriptor limits cannot exceed protocol ceilings. Unsupported descriptor or
resource-requirement versions fail validation with machine-readable
`unsupported_capability_descriptor_version` or
`unsupported_resource_requirement_version` error types.

`limits.max_output_bytes` is a claimed hard placement limit. Every task-bound
matcher caller supplies the execution's server-derived output budget; a typed
node must claim a value greater than or equal to it. Exact equality is eligible,
while a lower claim yields `insufficient_output_capacity`. This is exclusion
based on a node claim, not capacity measurement or attestation.

`limits.max_concurrent_execution_units` is an informational claimed upper
bound. The coordinator does not maintain or enforce per-node slot counts, and
it does not treat values above one as parallel slots. The stock worker polls
and executes sequentially, so its normal behavior conservatively stays within
the claim. This field does not enable worker concurrency, parallel polling, or
capacity-weighted scheduling.

Canonical compact sorted-key JSON defines the descriptor SHA-256 hash. For an
enrolled node, each distinct JSON snapshot is stored idempotently by enrollment
and hash and validated on read. The session binds the descriptor version/hash;
reusing its token with a different claim returns `409` with
`detail.code=node_capability_descriptor_conflict` and
`action=drain_or_establish_new_session`. Registration responses echo the
normalized descriptor, version, and hash. The stock worker checks that echo and
reuses one constructed descriptor across reconnects in the same process.

The shared matcher also selects the advertised model for a handout. It keeps
the worker's configured model when that model satisfies the request; otherwise
it chooses the canonical first model satisfying acceptable-model, exact-digest,
and context constraints. The handout carries the selected provider, name, and
nullable advertised digest. The stock worker validates that binding against
its immutable process descriptor and passes the selected name to Ollama.
Handouts from older coordinators have no binding and retain the configured-model
behavior.

The same matcher is authoritative for initial qualification, eligible-node-set
construction, worker long polling, the under-lock handout recheck, protected
operator diagnostics, and shadow candidate capture. No route may add a separate
output-capacity interpretation that can disagree with those decisions.

These values are node claims, whether populated by best-effort detection or an
operator override. Persistence and hashing do not measure, attest, or verify
them. Legacy `capabilities` remain accepted: worker values are exposed as
`claimed_capabilities`, coordinator-added `model:<name>` tags as
`server_compatibility_capabilities`, and their combined compatibility view as
`capabilities`.

The next authenticated operation observes revocation or rotation. Independently,
the coordinator janitor checks live enrolled sessions every 30 seconds; this is
the nominal idle detection bound, subject to ordinary scheduler delay and
durable-store availability. Failed store checks do not assume validity or
revocation; they are diagnosed and retried on a later sweep.

`node_enrollment_mode=required` rejects a legacy-only registration with a
stable worker-upgrade response and is mandatory in trusted alpha. Explicit
local `compat` mode may issue an unenrolled session for an old worker. Such a
session has `enrollment_id=null`, retains the old shared-secret checks, and
cannot claim a label already present in the durable enrollment table.
The stock enrollment-capable worker fails closed if a coordinator does not
explicitly confirm the requested action, enrollment ID, and credential version;
it never silently downgrades its private identity to a legacy shared-secret
session.

A descriptorless compatibility session advertises no typed output-capacity
claim. The matcher does not fabricate one, so its explicit legacy compatibility
behavior remains. If assigned, it is still constrained by the exact output
limit in the server-issued attempt.

Registration strings and capabilities are bounded, and the server normalizes
node IDs to at most 64 ASCII characters. `max_output_bytes` is an execution
contract limit from 1 KiB through 10 MiB and is copied into each issued
attempt. Worker result output must fit that attempt-specific UTF-8 byte cap;
worker error text is bounded twice, at 2048 characters by the request model and
again at 2048 UTF-8 bytes before settlement, so multi-byte text cannot use the
character limit to exceed the byte limit. A token-stream batch is bounded, the
cumulative streamed bytes cannot exceed the same attempt cap, an attempt can
emit at most 250,000 batches, and the rate is capped at 120 batches per second.
Crossing the byte/batch or rate boundary closes the stream and returns a
machine-readable `413` or `429` without allowing settlement to bypass the
result cap.

The attempt cap remains authoritative even when a descriptor advertises a
larger value. Neither registration, polling, streaming, nor result submission
lets a worker raise it.

## Server-authoritative worker attempts

### Active-attempt invariant

No worker result may enter operational execution unless it settles the active,
server-issued attempt for that task. Protocol strictness comes from the durable
attempt record, never from worker-supplied fields.

Assignment creates an active attempt with:

- task, execution, execution-unit, and unit-kind bindings;
- assigned enrollment (nullable for historical/compatibility work), node, and
  node-session identifiers;
- the enrollment credential version for a newly enrolled assignment;
- the assigned capability-descriptor version and hash (nullable for historical
  or descriptor-less compatibility work);
- the selected model provider, name, and optional digest; the evidence scope
  resolves the variant from that exact model in the immutable descriptor;
- the resource-requirement version and canonical digest, covering typed and
  legacy hard constraints;
- the task class (`dag_subtask` or `candidate`) and evidence role (`production`
  or `sampled_comparison`);
- contract version;
- unguessable attempt identifier;
- a high-entropy nonce whose digest, not plaintext, is stored;
- issue time, lease expiry, and the attempt-specific output cap.

For a v1 attempt, the worker must echo `contract_version`, `attempt_id`, nonce,
`node_id`, `execution_id`, `execution_unit_id`, and `execution_unit_kind`, and
the URL task id must match. The authenticated session must match the attempt's
assigned enrollment and active-session authority. Active settlement also
requires the issued session and credential version; a newly registered session
cannot take over an old active lease. Missing fields are rejection, not legacy
fallback. The submitted contract version must equal the server-owned contract
version. The attempt must still be active and unexpired.

The accepted flow is:

```text
unit queued
→ worker lease issued and attempt persisted
→ submission checked against server attempt
→ attempt, receipt, and compute contribution settle in one SQLite transaction
→ accepted receipt published
→ dispatcher consumes a receipt matching its execution and unit
```

The dispatcher never treats a bare `task_id -> result` dictionary as authority.
Its broker checks task, execution, unit, unit kind, and contract version. A
durable receipt lookup closes the commit-before-in-memory-publish window.

### Settlement, replay, rejection, and quarantine

SQLite uniqueness permits one active attempt per task, one accepted receipt per
task, and one compute-contribution record per attempt. `BEGIN IMMEDIATE` plus a
conditional active-to-settled update makes concurrent settlement exactly once.
An exact retry of a settled attempt returns its stored response, including after
database reopen, and does not award points twice. A current session for the
same active enrollment may recover that exact response after an incarnation
change; a different enrollment or a changed payload is rejected. This does not
make the old process-local session usable after restart.

Unknown, queued-but-unleased, expired, reclaimed, cancelled, superseded,
interrupted, wrong-node, wrong-execution, wrong-unit, wrong-kind, missing-field,
or mismatched-version submissions cannot publish an accepted receipt. They may
be retained in a bounded quarantine of 500 rows containing a reason, hash, and
at most a 4 KiB output preview. Quarantine is diagnostic only: it cannot wake a
dispatcher, update normal success statistics, earn points, become an execution
result, or emit `attempt_completed`; rejection emits `result_rejected`.

The legacy `task_results` mapping is only a compatibility mirror written after
settlement. It is not an integrity authority.

Accepted output is bounded by the server-owned attempt cap before the atomic
transition. Streaming counters and limit state live with the durable attempt,
so reconnecting or switching protocol endpoints cannot reset the cumulative
budget. Streaming is progress/liveness telemetry; it is never an accepted
result and never wakes dispatch by itself.

## Contribution points

Trusted-alpha contribution records are transactionally stored in SQLite;
`ledger.json` is an atomic compatibility projection. Worker points use the
explicit basis `compute_contribution` and mean that a nonempty, attempt-bound
worker result was accepted. They do **not** mean the candidate was selected,
the final output passed validation, or the output is correct. Points are not
money, a token, payment, transferable value, or a claim on future value.
New per-node summaries group by immutable enrollment ID and retain the node
label as metadata. Historical node-label-only rows remain readable with missing
enrollment attribution; they are not backfilled or inherited by a new session.

## Scoped capability evidence and shadow evaluation

Capability evidence is coordinator-recorded operational history, not a worker
claim, assurance result, correctness judgment, trust score, reputation, or
production routing input. Its immutable scope consists of:

- enrollment ID and descriptor version/hash;
- executor kind, executor version, and worker-protocol version;
- selected model provider, name, optional digest, and variant;
- task class (`dag_subtask` or `candidate`); and
- evidence role (`production` or `sampled_comparison`).

An observation is excluded when any required attempt binding or immutable
descriptor snapshot is missing, corrupt, historical-only, or inconsistent. The
coordinator never guesses a model or merges scopes by node label. Changing the
descriptor, selected model, task class, or evidence role creates a cold scope.

Typed observations are accepted settlement outcome, deadline completion,
coordinator wall seconds, UTF-8 output bytes, effective output bytes per
coordinator second, worker-attributable lease expiry or stale-node disconnect,
candidate-local contract-floor pass/fail when the required floor ran to a
terminal result, and paired sampled shape agreement. The two attributable
terminal causes come from the bounded server-owned `terminal_cause` field.
Deadline completion passes only when a non-empty output settles by the issued
lease deadline. A timely worker-reported error or empty output remains a
deadline-completion failure and is also retained in its settlement category.
This definition is stored under the versioned subject key
`nonempty_output_before_lease_v2`; current aggregates ignore the superseded
`lifecycle` deadline subject, and startup reconciliation backfills the versioned
observation without modifying append-only history.
Each sampled attempt durably binds the exact production attempt it compares;
sharing an execution ID and task class alone is insufficient.
Payload or stream limits, caller cancellation, execution deadline, receipt
binding, enrollment reclaim, session replacement, coordinator restart,
supersession, unknown causes, and free-form error text are not worker evidence.
Exclusion means the policy declined attribution; it does not mean success.

Binary aggregates expose numerator, denominator, rate, and a Wilson interval.
Latency and effective throughput expose total sample counts and bounded recent
medians. A scope below `capability_evidence_min_samples` is explicitly
`insufficient_evidence`, not poor evidence. Contract-floor outcome describes
structural contract assurance, not semantic correctness. Sampled agreement
describes output shape only, never correctness or trust, and is not used by the
shadow preference policy.

`capability_evidence_mode` is strictly `off` or `shadow` and defaults to `off`.
`capability_evidence_min_samples` defaults to 5 and must be an integer from 1 to
1000. `shadow` schedules a bounded counterfactual after the production handout
is durable. Admission freezes bounded non-secret node claim inputs at assignment
time. Canonical rematching and exact descriptor/model scope construction run
from that immutable snapshot as bounded background work outside the production
queue lock. Handout never waits on scope capture or evidence, and the evaluator
uses only observations recorded by the assignment cutoff. Shadow work never
changes eligibility, queue order, assignment, settlement, contribution credit,
or the circuit breaker. There is no active evidence-routing mode.
`verify_rate` is an independent, default-off sampled-comparison control.

Observation, pair, and shadow-decision IDs are deterministic and
domain-separated. An exact duplicate is idempotent; reuse of the same ID for
different immutable content conflicts. SQLite triggers reject update and delete
of evidence rows. Settlement evidence is attempted under a savepoint, so an
evidence failure cannot overturn attempt settlement, its receipt, or contribution
credit. Startup selects only attributable attempts missing expected observations.
Contract-floor observations and an append-only, content-free projection receipt
commit together; terminal executions lacking that receipt are the bounded retry
set. Complete and non-attributable rows therefore cannot starve later gaps.

The protected aggregate endpoint exposes no raw observations or metadata. The
evidence store contains no prompt, output body, worker-error text, free-form
reason, credential, nonce, session secret, or arbitrary telemetry. It is part of
`events.db` and therefore part of coordinator backup and restore.
The response exposes `shadow_decision_aggregates_available`; if the isolated
shadow database cannot be read, the endpoint remains available, per-scope
shadow counts are null rather than fabricated as zero, and the process-local
containment counter increments.

Shadow decisions and operational health use the sibling
`capability-shadow-health.db`. Decisions remain append-only and deterministic;
pre-isolation rows in `events.db` are copied forward idempotently at startup.
Operational health uses the distinct append-only
`capability_shadow_operational_events` table, with only `event_id`, `attempt_id`,
`phase`, `outcome`, bounded `reason_code`, and `occurred_at`. A deterministic
event primary key plus unique attempt/phase identity makes replay idempotent;
the health schema creates no invented historical event backfill. This separate
database keeps all best-effort shadow writes out of the authoritative
`events.db` writer-lock domain.
Admission outcomes are `disabled`, `not_applicable`,
`queue_saturated`, `scope_capture_failed`, and `scheduled`. Evaluation outcomes
are `completed`, `evaluator_failed`, `decision_write_failed`, and
`cancelled_on_shutdown`.

During graceful shutdown, new admission is closed and background work has a
finite drain. Admission whose scope capture exceeds that drain uses
`scope_capture_failed` with
`coordinator_shutdown_during_scope_capture`. An in-flight durable decision write
retains ownership of its eventual `completed` or `decision_write_failed`
terminal result rather than being mislabeled as cancellation.
Background evidence aggregation uses an already-migrated SQLite `mode=ro` and
`query_only` connection; it cannot modify authoritative `events.db` if a daemon
read survives the drain.

The durable report exposes counts by phase and outcome plus:

```text
orphan_evaluation_total = evaluation rows with no persisted admission row
assignment_observation_total = all admission outcomes + orphan_evaluation_total
scheduled = scheduled admissions + orphan_evaluation_total
offered = scheduled + queue_saturated + scope_capture_failed
skipped = disabled + not_applicable
failed = queue_saturated + scope_capture_failed + evaluator_failed
         + decision_write_failed + cancelled_on_shutdown
drop_failure_numerator = failed
drop_failure_denominator = offered
drop_failure_rate = failed / offered
```

The rate is null for a zero denominator. The report exposes
`orphan_evaluation_total`; each orphan terminal evaluation is treated as one
inferred scheduled/offered observation so it remains visible in the matching
numerator and denominator. `pending_total` is scheduled, including inferred
admissions, minus all evaluation terminal outcomes, bounded at zero. A time
window selects an inclusive admission-time cohort and includes the corresponding
attempts' evaluation events even when they finish after the window end; orphan
rows are selected by their own evaluation time. Store-write,
unexpected-containment, and background-callback failures are process-local
counters named `durable_health_record_write_failure`,
`unexpected_containment_failure`, and `background_task_callback_failure`, with
a process `reset_at` timestamp. They are not durable and never recursively try
to record their own storage failure.

Operational accounting is best effort and cannot alter or delay production
eligibility, node selection, handout, attempt creation, settlement,
contribution, or execution lifecycle. Its records and reports omit prompts,
outputs, worker error text, credentials, tokens, nonces, artifact contents, and
arbitrary exception messages. They measure experiment-pipeline health, not node
reputation.

Every aggregate scope also reports a derived
`eligible_for_future_active_experiment` boolean and bounded
`blocking_reasons`: `legacy_descriptor_identity`,
`descriptor_identity_unreconstructable`, `immutable_model_identity_missing`,
and `model_identity_unreconstructable`. This is an identity prerequisite only.
It neither changes hard eligibility, assignment, or shadow preference nor
itself suppresses observations; a digestless typed scope continues collecting
when the existing evidence resolver can otherwise reconstruct it.

## Durable post-hoc verification evidence

Post-hoc verification happens after an execution is terminal. ADR 0009 makes
terminal state monotonic and never reclassified, so a verification result is not
allowed to touch it. Evidence is therefore a separate append-only record that
references an execution, unit, attempt, and accepted receipt without mutating any
of them, with no foreign key back into them and with update/delete refused by
database triggers.

Each record carries an exact scope -- subject enrollment ID and identity class,
descriptor version and hash, executor kind/version, worker protocol version,
selected model provider/name/digest/variant, task class, and verifier
kind/name/version. Changing any of these starts a cold scope; history is never
inherited across a descriptor, model, or verifier-version change. Task class
reuses the existing `dag_subtask` / `candidate` vocabulary. Rows without enrolled
identity are classified `legacy`, aggregated separately, and never resolved to an
enrollment through a reusable node label.

`evidence_id` is a domain-separated SHA-256 over the authoritative identities plus
verifier identity and version, so replay safety does not depend on in-memory
de-duplication. Accepted-result replay, restart reconciliation, repeated
callbacks, event redelivery, and an identical verifier re-run all converge on one
row. A re-run at a different verifier version appends a new row and never
overwrites the earlier one.

Two verifier kinds exist and they do not share an outcome vocabulary:
`deterministic_check` is `passed` / `failed` / `not_run`, and
`sampled_reexecution` is `agreed` / `disagreed` / `not_run`. `verifier_kind` is
part of the scope key, so agreement can never contribute to a pass rate. Nothing
in the model is named for correctness. Structural contract-floor validators
(`nonempty`, `artifact_extraction`, `artifact_contract`, `file_manifest`) cannot
be recorded here at all, because a structural failure is not a statement about
semantic correctness.

Every record carries an explicit fault attribution, and only `subject_output` may
carry an outcome about the subject -- enforced in code and again as a table
constraint. Requester cancellation, coordinator shutdown, coordinator persistence
failure, a pre-assignment deadline, and verifier unavailability can only record
`not_run`, and `not_run` rows are excluded from the attributable sample count, so
a cancellation or restart cannot depress a scope's observed rate. Malformed or
mismatched authority credentials are refused outright: those are security events
belonging to attempt authority, not evidence about anyone's work.

Evidence writes are best-effort with respect to the execution path. A failure
increments a process-local counter and never fails an execution, alters
settlement, changes eligibility, or delays a handout.

This is evidence, not reputation, not correctness, and not assurance. It
influences no routing, eligibility, settlement, or contribution decision, and
there is no score, ranking, or leaderboard. Task-class assurance ladders are
deferred to Theme 3B-2. See
[ADR 0014](adr/0014-durable-verification-evidence.md).

## Canonical REST API

| Method and path | Meaning |
| --- | --- |
| `POST /v1/executions` | Validate and queue a canonical request; optional scoped `Idempotency-Key`; returns HTTP 202 |
| `GET /v1/executions/{id}` | Read durable normalized state/result |
| `POST /v1/executions/{id}/cancel` | Request idempotent cancellation |
| `GET /v1/executions/{id}/artifacts` | Read deliverable entries; `role=audit` selects audit material and deprecated `role=all` selects both |
| `POST /v1/executions/{id}/artifacts/seal` | Return the committed sealed baseline; active/legacy state is not promoted through this route |
| `GET /v1/executions/{id}/artifacts/{path}` | Stream one authenticated artifact |
| `GET /v1/executions/{id}/download` | Stream a temporary deliverable ZIP |
| `GET /v1/executions/{id}/audit-download` | Stream a temporary non-deliverable audit ZIP |
| `POST /v1/executions/{id}/shares` | Create a public capability share |
| `GET /v1/executions/{id}/shares` | List active share metadata without plaintext tokens |
| `DELETE /v1/executions/{id}/shares/{share_id}` | Revoke a share |
| `DELETE /v1/executions/{id}/shares` | Revoke all shares for an execution |
| `GET /v1/shares/{token}` | Read one redacted public share |
| `GET /v1/operator/health` | Read private deployment mode, instance, lock, and preflight state |
| `GET /v1/operator/capability-evidence` | Read protected scoped aggregates, shadow-only decisions, identity blockers, and operational-health counts; never raw observations or events |

Read, artifact, cancellation, and share-management routes require viewer access
when `viewer_key` is configured. Canonical submission uses the separate
`pitch_key`. See [ACCESS_CONTROL.md](ACCESS_CONTROL.md) and
[ARTIFACTS.md](ARTIFACTS.md).

## Artifact delivery

`ArtifactManifestV1` exposes only execution id, timestamps, counts, aggregate
bytes, integrity mode, optional sealed hash/timestamp, and entries containing a
normalized relative path, role, media type, size, SHA-256, optional source
candidate/unit, and creation time. Roots are internal. Roles are `deliverable`,
`provenance`, `log`, `candidate_source`, and `internal`.

The artifact registry accepts strict children of `output/` and
`execution_artifacts/`, rejects symlinks and traversal (including encoded
forms), enforces configured file and byte quotas, rehashes on access, and builds
ZIPs on temporary disk for streaming rather than loading an arbitrary archive
into memory. Terminal finalization seals an immutable SQLite entry baseline and
canonical manifest hash after applying winner scope. Every later file and ZIP
read resolves and re-hashes the live file against that baseline; drift fails
closed. Historical `legacy_live` roots remain rescanned and MUST NOT be labeled
sealed. Registered active executions, including final manifest refresh, are
never pruned. Retention covers both storage families. Detailed limits and
filtering rules are in
[ARTIFACTS.md](ARTIFACTS.md).

Terminal manifest, file, and ZIP publication, whether private or reached
through a share, additionally requires a durable terminal execution snapshot.
For a sealed root, the manifest hash must match the hash bound into that
snapshot. Historical `legacy_live` roots retain their labeled, freshly
rescanned compatibility behavior. Finalization may prepare files and a seal
before the terminal commit, but those materials are not authoritative or
retrievable through terminal APIs until the commit succeeds. A share record
may exist earlier and show the last committed nonterminal execution state; it
does not bypass the terminal artifact gate.

The sealed hash is local integrity evidence, not a signature, independent
timestamp, malware scan, behavioral verdict, or defense against a host able to
alter both files and SQLite. Private delivery defaults to deliverables; audit
material requires its explicit manifest or ZIP route.

## Viewer access and explicit shares

Viewer authorization is independent of node and pitch admission. A configured
static viewer key may be sent as `X-Viewer-Key`, as an exact Bearer token, or
exchanged at `POST /v1/viewer/session` for a signed, expiring HttpOnly cookie.
Static-key and signature checks use constant-time comparison. Rotating the key
invalidates existing cookies.

When `viewer_key` is empty, private routes intentionally remain open for local
development compatibility. Startup logging and public `/health` both warn that
they are unprotected. This mode is not suitable for a reachable deployment.

Shares are explicit bearer capabilities. The server stores only a SHA-256 token
hash. A share may expire, be revoked, allow artifact downloads, redact node
identity, and include or omit candidate detail. Invalid, expired, and revoked
tokens all return `404`. Public responses are constructed from an allowlist and
omit project/job ids, raw filesystem paths, attempt credentials, credit detail,
private telemetry, and unbounded validator diagnostics. A share token grants no
ambient access to another execution or to private routes. Share artifacts are
deliverables by default; candidate source requires the candidate-detail flag,
and provenance, logs, and internal roles are never shareable. Without a winner,
candidate-scoped entries are excluded.

Share responses use `no-store`, `no-referrer`, and `nosniff` headers. Application
unhandled-error logging redacts token path segments. Uvicorn and reverse-proxy
access-log redaction remains an operator responsibility because the URL is the
credential. Revocation prevents future use but cannot recall copied content.

## Keyless public pitch profile

`POST /public/pitch` is disabled unless `public_pitch` is enabled. It accepts
only `task`; caller attempts to set strategy, candidates, placement, project,
validators, or confidentiality are rejected. The server-owned profile is one
direct candidate, concurrency one, local placement, `local_only`, no project,
120-second total deadline, 64 KiB output cap, and recorded disabled network
intent.

Admission adds two requests per source per hour, one active execution per
source, three active public executions globally, and a global inference
semaphore of one. The response creates a one-hour, node-redacted share with
artifact permission so the public caller can retrieve only that capability.

## Events and interface schema

Events are flat objects: `id`, `type`, and `time` are peers of event-specific
fields. Consumers must not expect a nested `data` object and must tolerate new
event names. `/health` exposes `nodes_online` as an integer, not a detailed node
list. Detailed node records require viewer access. CLI and MCP may send
`VIEWER_KEY` for private reads and `PITCH_KEY` for submission.

Normal execution lifecycle events are post-commit notifications. If a
persistence-failure diagnostic event is emitted, it may describe the phase and
bounded attempt count, but it is not a queued, running, or terminal transition
and must contain no prompt, result, credential, token, nonce, key, or artifact
content. Failure to persist or emit diagnostics does not change the last
durable lifecycle.

Persisted events are structural telemetry, not a prompt or output log. A
central per-event allowlist is applied before the bounded in-memory cache,
SQLite, and WebSocket publication; HTTP and WebSocket replay apply the same
policy defensively. Startup idempotently rewrites historical event payloads to
remove free-form task, output, error, reason, and message fields while retaining
row identity, type, time, and safe structural fields. Generated token text is
available only on the live WebSocket path and is never added to SQLite or replay.

Worker clients report `DONE` only after the result endpoint accepts settlement.
A rejected result is shown as failure. If both generation and the subsequent
error-report POST fail, the original generation error remains the primary error.

Private node records distinguish current session state/counters from durable
lifetime contribution totals. Compatibility `tasks_completed` and
`credits_earned` remain session projections. Clients MUST NOT expect a session
token, enrollment credential, or credential digest in node-list or event
payloads. `GET /nodes` exposes descriptor version/hash but omits the full
descriptor. Viewer-protected `GET /v1/operator/node-enrollments` additionally
returns the normalized claim, claim/tag provenance, snapshot count, and stable
hard-requirement match diagnostics. Its optional bounded
`resource_requirements`, repeated `required_capability`, and
`required_output_capacity_bytes` query parameters are diagnostic only. Public
`/health` and `/status.json` expose none of this detail.

Viewer-protected `GET /v1/operator/capability-evidence` accepts `limit` from 1
through 200 (default 100), plus optional `enrollment_id`, `descriptor_hash`,
`task_class`, and `evidence_role` filters. Bounded operational time-window
parameters `window_started_at` and `window_ended_at` accept finite,
non-negative Unix timestamps and select an inclusive admission cohort plus its
matching evaluation rows. The evidence role defaults to `production`. The response returns the configured mode
and minimum, scoped aggregates, future-active identity diagnostics, grouped
shadow outcomes, durable admission/evaluation counts, offered/scheduled/
completed/skipped/failed/pending totals, `orphan_evaluation_total`, explicit
drop/failure numerator and denominator, process-local fallback counters and
`reset_at`, category meanings, and `affects_routing=false`. It labels these
values operational experiment
health, not node reputation, and never exposes raw observation/event records,
prompts, outputs, errors, credentials, nonces, or artifact contents.
`GET /nodes`
contains no capability-evidence score, reputation, trust flag, or routing weight.

## Deployment, SQLite, and coordinator ownership

`deployment_mode=local` preserves fail-open developer defaults.
`deployment_mode=trusted_alpha` fails startup/preflight unless viewer, pitch,
and node secrets are independent and at least 32 characters, cookie/TLS intent
is coherent, durable enrollment is required, TLS or a private authenticated
overlay is declared, config and state paths pass safety checks, and public pitch
has an explicit acknowledgement when enabled. `/health` publishes only safe
protection state; the private `/v1/operator/health` identifies the process,
mode, held lock, preflight warnings, and the content-free
`validator_process` registry/runner diagnostics and process-local counters.

Preflight also validates `validator_execution_mode`,
`validator_subprocess_timeout_seconds`, `validator_subprocess_memory_mb`,
`validator_subprocess_request_max_bytes`, and
`validator_subprocess_response_max_bytes`. Trusted alpha accepts `auto` or
`subprocess` and rejects `inline`; local mode warns that `inline` weakens parser
containment. Invalid trusted values are errors. Invalid local values warn and
fall back to bounded defaults. The defaults are `auto`, 10 seconds, 256 MiB,
2 MiB request bytes, and 32 KiB response bytes. Inclusive accepted ranges are
1–120 seconds, 128–1,024 MiB, 16 KiB–16 MiB request bytes, and 1–256 KiB
response bytes. Booleans and other non-integer numeric values are invalid.
The request-byte setting is the V2 JSON control-envelope limit; it does not
reduce the execution's separately authoritative output byte budget. Output
staging has no additional operator-configurable ceiling.

Exactly one coordinator may own a state directory. An operating-system lock is
acquired before migrations and background work; a second process fails closed.
All production `events.db` access, including append-only capability observations,
uses the shared SQLite policy: WAL mode,
foreign keys on, a 10-second busy timeout, `synchronous=NORMAL`, bounded busy
retry, per-path migration serialization, and explicit immediate transactions at
integrity boundaries. This improves one-process concurrency; it is not a
multi-coordinator protocol. Best-effort shadow decisions and operational-health
writes use the sibling `capability-shadow-health.db`, so their writer locks
cannot contend with authoritative attempt, assignment, or settlement writes in
`events.db`.
Backup format v2 captures both SQLite databases as durable state. Restore also
accepts legacy format v1 without `capability-shadow-health.db`; that database is
optional only for state predating shadow operational-health persistence. Queues,
in-flight coroutines, node sessions, and breaker state remain process-local.

`execution_submissions` is an additive, indefinitely retained trusted-alpha
table containing only requester-scope, idempotency-key, and canonical-request
digests, request-hash version, execution identity, and creation time. An immediate transaction
creates its mapping and the queued execution together. The table is included in
ordinary SQLite backup/restore; it does not make queued work resumable.

## Compatibility and errors

`POST /pitch`, `/pitch/async`, and `/pitch/distributed` adapt historical bodies
to the canonical service. Bodies containing only `task` and optional
`project_id` remain valid where project memory is supported. `/pitch` defaults
local, `/pitch/async` preserves historical auto placement, and
`/pitch/distributed` preserves documented distributed intent and fallback.
Legacy job status now passes through `running` and can finish as `complete`,
`failed`, `cancelled`, or `interrupted`. The idempotency header applies only to
canonical `POST /v1/executions`; legacy HTTP, CLI, and MCP interfaces are
unchanged.

Invalid canonical requests return `422`; missing private resources return
`404`; missing viewer, pitch, returning-enrollment, or session credentials
normally return `401`; missing or malformed registration fields return `422`;
enrollment or live descriptor conflicts return `409`; an old worker or an
enrollment-capable worker missing its descriptor in required mode receives a
stable `426` worker-upgrade error; revoked enrollment returns `403`;
invalid worker attempt settlement fails closed. Configured limits may return `413`, `429`, or
`503`. Invalid, expired, and revoked share tokens deliberately share one `404`
shape. Invalid idempotency keys return `422` with
`detail.code=invalid_idempotency_key`; a changed request under an existing scope
and key returns `409` with `detail.code=idempotency_conflict`. Required
persistence failure returns `503` with
`detail.code=execution_persistence_unavailable`; a broken durable mapping uses
`detail.code=idempotency_consistency_error`. A durable creation whose local
activation setup was contained uses `503` with
`detail.code=submission_activation_failed` and its stable execution ID. These
envelopes contain no raw key, requester credential, prompt, or result.

## Explicit limitations

Protocol v1 does not provide a durable worker queue, automatic execution resume,
durable node sessions, per-node public-key identity, physical-machine identity,
built-in TLS, multi-user accounts, a general network-policy enforcement layer,
general hostile-code process isolation, generated-code sandboxing,
malicious-output detection, permissionless settlement, Sybil resistance,
externally anchored artifact attestation, durable
execution-level post-hoc verification, or proof that arbitrary generated output
is correct. One coordinator process is the only supported owner of a state
directory. It is intended for a small private group whose operators and node
holders are known and trusted. These limitations must not be represented as
solved by the normalized API. Idempotent canonical submission is not durable
scheduling, user identity, workflow resumption, or exactly-once execution of
external effects.

The bounded validator process isolates trusted built-in parser failures. It
does not provide container, VM, WASM, gVisor, or Firecracker isolation;
same-user filesystem confidentiality; reliable network denial; arbitrary
validator plugins; generated-code execution; or behavioral proof.

Capability descriptors are self-reported claims, not observed performance,
trust, correctness, physical-machine identity, or hardware/model attestation.
The hard matcher excludes but does not rank. Scoped operational evidence and
sampled shape agreement do not verify the descriptor or result. Production
routing is unchanged in every evidence mode: there is no first-refusal weight,
trusted score, reputation rank, or active evidence policy. A future active
experiment requires immutable model/descriptor identity, every live experiment
threshold, a separate accepted ADR, and a separately reviewed implementation
PR. Protocol v1 implements none of that active routing and does not implement
worker concurrency.
