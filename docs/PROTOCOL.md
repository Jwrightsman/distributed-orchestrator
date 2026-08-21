# Mycelium Execution Protocol v1

This document defines the coordinator-facing and worker-facing execution
contract implemented by Mycelium. The key words **MUST**, **MUST NOT**,
**SHOULD**, **SHOULD NOT**, and **MAY** are normative.

## Scope and versioning

Protocol v1 productionizes two execution strategies: `dag` version `1` and
`ensemble` version `1`. `direct` is a request alias for ensemble with one
candidate. `auto` is selector policy, not an execution strategy.

The protocol version and strategy version are independent. A coordinator MUST
reject an unsupported `protocol_version`; it MUST NOT silently substitute an
unknown strategy. A compatible implementation MAY add strategy versions while
continuing to read protocol version `1` records.

## ExecutionRequestV1

Requests are strict Pydantic objects: unknown keys and invalid combinations are
rejected with HTTP `422` at REST boundaries.

```json
{
  "protocol_version": "1",
  "task": "Build one self-contained HTML artifact",
  "project_id": "optional-project",
  "strategy": "ensemble",
  "strategy_options": {
    "kind": "ensemble",
    "candidates": 3,
    "concurrency": 2,
    "selection_policy": "validated_score"
  },
  "placement": "auto",
  "requirements": {
    "required_capabilities": ["code"],
    "approved_node_ids": [],
    "allow_local_fallback": true
  },
  "output_contract": {
    "kind": "single_artifact",
    "artifact_count": 1,
    "format": "html",
    "validators": [
      {"name": "artifact_extraction", "required": true},
      {"name": "code_parse", "required": true}
    ]
  },
  "verification": {
    "validators": [],
    "allow_unverified_fallback": true,
    "require_all": true
  },
  "confidentiality": "trusted_guild",
  "timeout_seconds": 1800,
  "max_output_bytes": 1048576,
  "network_policy": "disabled"
}
```

`task` MUST be nonblank and at most 1,000 characters. Candidate count MUST be
between one and five; concurrency MUST NOT exceed candidate count. DAG maximum
subtasks MUST be between one and five. Output sizes, schemas, identifier lists,
file lists, and timeouts are bounded in `execution/contracts.py`.

### Strategy options

`DagOptionsV1` contains:

- `kind: "dag"`
- `maximum_subtasks: 1..5`
- `review_enabled: boolean`
- `revision_enabled: boolean`

`EnsembleOptionsV1` contains:

- `kind: "ensemble"`
- `candidates: 1..5`
- `concurrency: 1..candidates`
- `selection_policy: "validated_score" | "first_valid"`

The discriminator MAY be inferred when the option family is unambiguous.
Conflicting strategy and option families MUST be rejected.

## Strategy semantics

### DAG version 1

DAG retains the existing planner, dependency-aware builder waves, reviewer,
reviser, artifact extraction, project memory, and legacy events. Each builder
subtask becomes an execution unit and travels through the shared dispatcher.
Planning, review, and revision currently execute on the coordinator.

### Ensemble version 1

Every candidate MUST receive the complete task and output contract. Candidate
failures are isolated. Candidate generation MAY run concurrently up to the
requested bound. Every completed candidate is validated independently. Winner
selection MUST use recorded validation evidence; secondary ordering is the
configured policy, validation score, then bounded deterministic tie-breakers.

If no candidate satisfies all required validators, the execution MUST be
`failed` unless `allow_unverified_fallback` is true and a nonempty candidate
completed. Such a selection MUST have status `unverified` and MUST state that
it is not a deterministic correctness claim.

### Direct and auto

`strategy="direct"` MUST be recorded as requested and MUST normalize to
`strategy_selected="ensemble"`, `candidates=1`, `concurrency=1`.

The `conservative-v1` auto selector is deterministic:

1. An explicit non-auto request wins.
2. Explicit ensemble options select ensemble; one candidate is a direct execution request.
3. A single-artifact contract with an available deterministic validator selects ensemble.
4. Missing or ambiguous contract information selects DAG for compatibility.

The coordinator MUST persist `strategy_selected`, `selector_reason`, and
`selector_version`. Protocol v1 MUST NOT call a model to choose a strategy.

## Placement and confidentiality

Placement is orthogonal to strategy.

- `local` MUST run units on the coordinator's model integration.
- `distributed` SHOULD dispatch units to qualifying workers. With no qualifying
  worker it MAY fall back locally only when `allow_local_fallback` is true.
- `auto` selects distributed when a qualifying worker exists and policy permits;
  otherwise it selects local.

`confidentiality="local_only"` MUST NOT dispatch remotely. An explicit
`local_only + distributed` request is invalid. `approved_nodes` MUST include a
nonempty `approved_node_ids` allowlist, and only listed nodes may receive a
unit. Capability requirements MUST be applied to DAG units and candidates.

The selected placement and every fallback reason MUST be recorded. A local
fallback is availability behavior, not evidence that distributed execution
succeeded.

`network_policy` is recorded in v1 but no general network sandbox is claimed.
It does not grant a worker credentials or override deployment controls.

## Validation

Validation is separate from candidate generation and strategy reduction. The
v1 registry supports:

- `nonempty`
- `structured_json`
- `json_schema`
- `file_manifest`
- `code_parse`
- `artifact_extraction`

Evidence contains validator name and version, status, optional score, bounded
evidence, failure reason, and duration. Required validators MUST pass according
to policy before a result is described as verified. An LLM review is not a
deterministic validator.

## ExecutionResultV1

Every normalized result contains:

- durable UUID-style `execution_id` and optional legacy `job_id`;
- protocol, status, task, and project identity;
- requested/selected strategy, versions, options, and selector reason;
- requested/selected placement and fallback reason;
- created, started, completed, and duration timestamps;
- execution-unit and candidate summaries;
- winner and complete selection explanation;
- validation, review, and revision metadata;
- bounded output preview plus artifact references;
- participating nodes and contribution records;
- structured errors and bounded telemetry.

Large artifact bodies SHOULD be stored once and referenced. The normalized
record MUST NOT duplicate unbounded output contents.

Statuses are `queued`, `running`, `completed`, `failed`, and `unverified`.
Legacy job status uses `complete` for compatibility.

## Canonical REST API

`POST /v1/executions` accepts `ExecutionRequestV1`, returns HTTP `202`, and
provides the durable id immediately:

```json
{
  "execution_id": "a4d0...",
  "status": "queued",
  "protocol_version": "1",
  "strategy_requested": "direct",
  "strategy_selected": "ensemble",
  "selector_reason": "Normalized direct to ensemble with one complete candidate."
}
```

`GET /v1/executions/{execution_id}` returns `ExecutionResultV1` from SQLite and
returns `404` for an unknown id. Completed normalized metadata remains readable
after coordinator restart. The in-flight scheduler is not durable in v1.

## Worker execution units

Protocol-v1 queued tasks include:

- `contract_version: "1"`
- `execution_id`
- `strategy`
- `execution_unit_id`
- `execution_unit_kind`
- complete prompt and system prompt
- bounded output-contract and verification-policy summaries
- capability and eligible-node filters
- `max_output_bytes` and a bounded lease duration

On assignment the coordinator adds an unguessable `attempt_id`, lease `nonce`,
assigned node id, assignment time, and expiry. Queue admission MUST enforce the
configured cap atomically for every unit.

## Attempt lifecycle and submissions

For a v1 final result, the node MUST echo:

- `contract_version="1"`
- current `attempt_id` and nonce
- its assigned `node_id`
- matching `execution_id`, `execution_unit_id`, and `execution_unit_kind`
- the task id in the request path

The lease MUST be active and unexpired. A mismatch MUST return `403`, emit a
rejection event where applicable, and MUST NOT award credit. An accepted retry
of the same settled attempt MUST return the original outcome without paying
twice.

Legacy workers MAY return work without the v1 marker. The coordinator MAY
record an otherwise usable legacy result, but an attempt that cannot be bound
to issued credentials MUST receive zero contribution credit.

Token batches for a v1 task MUST echo the same node, attempt, nonce, execution,
and unit identifiers. Unknown tasks do not refresh node liveness. Wrong-node,
wrong-attempt, wrong-nonce, expired, or mismatched-unit streams MUST be rejected.

The deployment still uses a shared node admission secret. Constant-time secret
comparison reduces timing leakage, but the shared secret is not per-node
cryptographic identity.

## Events

New consumers SHOULD handle these events and MUST tolerate additional names:

- `execution_created`, `strategy_selected`
- `execution_unit_queued`
- `attempt_started`, `attempt_completed`
- `candidate_generated`, `candidate_validation_completed`, `candidate_rejected`
- `winner_selected`
- `review_started`, `revision_started`
- `placement_fallback`
- `execution_completed`, `execution_failed`

Legacy events such as `pitch`, `plan`, `build`, `review_start`, `token`, and
`complete` remain available where their legacy pipelines emit them.

## Compatibility and errors

`POST /pitch`, `/pitch/async`, and `/pitch/distributed` are adapters over the
canonical service. A body containing only `task` and optional `project_id` MUST
remain valid. `/pitch` defaults to local placement; `/pitch/distributed`
defaults to distributed with documented fallback; async legacy submission uses
auto placement. Legacy response fields are retained and normalized metadata is
additive.

Invalid requests return `422`; unavailable required placement maps to a
structured execution error (or `503` through the synchronous compatibility
adapter); missing execution ids return `404`; queue saturation and timeouts are
recorded as bounded failures and MAY trigger a visible local fallback.

## Explicit limitations

Protocol v1 does not provide durable queues or leases, process isolation,
general network sandboxing, per-node public-key identity, credential isolation,
permissionless settlement, or proof that arbitrary generated output is correct.
These limitations MUST NOT be represented as solved by the normalized API.
