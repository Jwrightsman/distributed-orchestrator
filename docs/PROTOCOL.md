# Mycelium Execution Protocol v1

This document defines the coordinator, client, and worker contracts implemented
by Mycelium for its private trusted alpha. The key words **MUST**, **MUST NOT**,
**SHOULD**, **SHOULD NOT**, and **MAY** are normative.

The trusted-alpha boundary matters: this protocol prevents an unbound or late
worker result from becoming an execution result, but node admission still uses
one shared secret. It does not provide per-node cryptographic identity, a
generated-code sandbox, or permissionless-network defenses.

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
    "allow_local_fallback": true
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

### Strategy options

`DagOptionsV1` contains `maximum_subtasks` from one through five and booleans
for review and revision. `EnsembleOptionsV1` contains one through five
candidates, bounded concurrency no greater than the candidate count, and a
selection policy of `validated_score` or `first_valid`.

The option discriminator may be inferred only when the supplied fields are
unambiguous. A conflicting strategy and option family is invalid.

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
memory updates are a prerequisite for adding parity later.

## Lifecycle, deadlines, cancellation, and restart

Canonical lifecycle and assurance are separate dimensions.

`lifecycle_status` is one of:

- `queued`
- `running`
- `completed`
- `failed`
- `cancelled`
- `interrupted`

`timeout_seconds` is a total deadline beginning when the canonical execution is
queued. Planning, local generation, worker waits, validation, review, revision,
and artifact registration consume the same remaining budget. Worker leases are
capped by that deadline. Timeout cancels queued and active units, rejects later
worker results, persists a terminal failure with `execution_timeout`, and marks
the result retryable.

`POST /v1/executions/{execution_id}/cancel` is idempotent. It records the
cancellation request and timestamps, signals local work, removes queued worker
units, durably cancels active attempts, rejects late submissions, persists
terminal `cancelled`, and emits an `execution_cancelled` event. Cooperative
cancellation cannot forcibly stop work inside an external service that ignores
task cancellation; its result is nevertheless no longer admissible.

At coordinator startup, persisted canonical executions and legacy jobs left in
`queued` or `running` are not resumable because the scheduler and background
coroutines are process-local. Reconciliation transactionally moves them to
`interrupted`, records a restart marker, reason and timestamp, marks them
retryable, and is idempotent. Active worker attempts are durably interrupted so
late results fail closed. Completion callback failures are logged, emitted, and
added to the persisted result rather than disappearing silently.

The queue itself is still not durable. Restart reconciliation makes its loss
truthful; it does not resume the lost work.

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

## Result model and compatibility projection

`ExecutionResultV1` includes stable execution identity; lifecycle, validation,
and assurance; requested and selected strategy; requested, planned, and
observed placement; consent; timestamps and deadline; units and candidates;
winner explanation; validation summaries; artifact API references; bounded
previews; participation and contribution records; structured errors; and
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

## Server-authoritative worker attempts

### Active-attempt invariant

No worker result may enter operational execution unless it settles the active,
server-issued attempt for that task. Protocol strictness comes from the durable
attempt record, never from worker-supplied fields.

Assignment creates an active attempt with:

- task, execution, execution-unit, and unit-kind bindings;
- assigned node identifier;
- contract version;
- unguessable attempt identifier;
- a high-entropy nonce whose digest, not plaintext, is stored;
- issue time and lease expiry.

For a v1 attempt, the worker must echo `contract_version`, `attempt_id`, nonce,
`node_id`, `execution_id`, `execution_unit_id`, and `execution_unit_kind`, and
the URL task id must match. Missing fields are rejection, not legacy fallback.
The submitted contract version must equal the server-owned contract version.
The attempt must still be active and unexpired.

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
database reopen, and does not award points twice. A changed replay is rejected.

Unknown, queued-but-unleased, expired, reclaimed, cancelled, superseded,
interrupted, wrong-node, wrong-execution, wrong-unit, wrong-kind, missing-field,
or mismatched-version submissions cannot publish an accepted receipt. They may
be retained in a bounded quarantine of 500 rows containing a reason, hash, and
at most a 4 KiB output preview. Quarantine is diagnostic only: it cannot wake a
dispatcher, update normal success statistics, earn points, become an execution
result, or emit `attempt_completed`; rejection emits `result_rejected`.

The legacy `task_results` mapping is only a compatibility mirror written after
settlement. It is not an integrity authority.

## Contribution points

Trusted-alpha contribution records are transactionally stored in SQLite;
`ledger.json` is an atomic compatibility projection. Worker points use the
explicit basis `compute_contribution` and mean that a nonempty, attempt-bound
worker result was accepted. They do **not** mean the candidate was selected,
the final output passed validation, or the output is correct. Points are not
money, a token, payment, transferable value, or a claim on future value.

## Canonical REST API

| Method and path | Meaning |
| --- | --- |
| `POST /v1/executions` | Validate and queue a canonical request; returns HTTP 202 |
| `GET /v1/executions/{id}` | Read durable normalized state/result |
| `POST /v1/executions/{id}/cancel` | Request idempotent cancellation |
| `GET /v1/executions/{id}/artifacts` | Read the authenticated artifact manifest |
| `GET /v1/executions/{id}/artifacts/{path}` | Stream one authenticated artifact |
| `GET /v1/executions/{id}/download` | Stream a temporary ZIP of authenticated artifacts |
| `POST /v1/executions/{id}/shares` | Create a public capability share |
| `DELETE /v1/executions/{id}/shares/{share_id}` | Revoke a share |
| `GET /v1/shares/{token}` | Read one redacted public share |

Read, artifact, cancellation, and share-management routes require viewer access
when `viewer_key` is configured. Canonical submission uses the separate
`pitch_key`. See [ACCESS_CONTROL.md](ACCESS_CONTROL.md) and
[ARTIFACTS.md](ARTIFACTS.md).

## Artifact delivery

`ArtifactManifestV1` exposes only execution id, timestamps, counts, aggregate
bytes, and entries containing normalized relative path, media type, size,
SHA-256, optional source candidate/unit, and creation time. Roots are internal.

The artifact registry accepts strict children of `output/` and
`execution_artifacts/`, rejects symlinks and traversal (including encoded
forms), enforces configured file and byte quotas, rehashes on access, and builds
ZIPs on temporary disk for streaming rather than loading an arbitrary archive
into memory. Registered active executions are never pruned. Retention covers
both storage families. Detailed limits and filtering rules are in
[ARTIFACTS.md](ARTIFACTS.md).

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
ambient access to another execution or to private routes.

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

Worker clients report `DONE` only after the result endpoint accepts settlement.
A rejected result is shown as failure. If both generation and the subsequent
error-report POST fail, the original generation error remains the primary error.

## Compatibility and errors

`POST /pitch`, `/pitch/async`, and `/pitch/distributed` adapt historical bodies
to the canonical service. Bodies containing only `task` and optional
`project_id` remain valid where project memory is supported. `/pitch` defaults
local, `/pitch/async` preserves historical auto placement, and
`/pitch/distributed` preserves documented distributed intent and fallback.
Legacy job status now passes through `running` and can finish as `complete`,
`failed`, `cancelled`, or `interrupted`.

Invalid canonical requests return `422`; missing private resources return
`404`; missing viewer, pitch, or node credentials return `401`; invalid worker
attempt settlement returns `403`; configured limits may return `413`, `429`, or
`503`. Invalid, expired, and revoked share tokens deliberately share one `404`
shape.

## Explicit limitations

Protocol v1 does not provide a durable worker queue, automatic execution resume,
per-node public-key identity, individual node revocation, TLS, multi-user
accounts, a general network-policy enforcement layer, process isolation,
generated-code sandboxing, malicious-output detection, permissionless
settlement, Sybil resistance, or proof that arbitrary generated output is
correct. It is intended for a small private group whose operators and node
holders are known and trusted. These limitations must not be represented as
solved by the normalized API.
