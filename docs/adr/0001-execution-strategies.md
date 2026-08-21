# ADR 0001: First-class execution strategies

- Status: Accepted
- Date: 2026-08-21
- Decision scope: execution-strategy protocol v1

## Context

Mycelium originally exposed one orchestration shape: a planner decomposed a
task, builders completed dependent pieces, and a reviewer/reviser assembled the
result. A later ensemble experiment sent the complete task to independent
candidates, but that experiment kept a second orchestration implementation and
was not available through the production interfaces.

The two concerns that had become coupled were:

1. **strategy** — how work is compiled and reduced; and
2. **placement** — where an execution unit runs.

That coupling makes combinations such as ensemble on contributor nodes
awkward, encourages copied orchestration paths, and prevents a caller from
describing its intent in a stable protocol.

## Decision

Execution protocol v1 makes strategy a versioned, recorded property of every
canonical execution. Strategy selection, placement selection, dispatch,
validation, and persistence are separate responsibilities.

The production strategy registry contains exactly two implementations:

- `dag` version 1 adapts the existing planner → builders → reviewer → reviser
  pipeline;
- `ensemble` version 1 generates complete alternatives and selects from
  structured validation evidence.

`direct` is a request alias for `ensemble` with one candidate. It is recorded
as the requested strategy but MUST NOT have an independent orchestration path.

`auto` is a deterministic selector, not a strategy implementation. Protocol v1
uses explicit request and output-contract fields and does not spend an LLM call
on selection. Ambiguous requests select DAG to preserve compatibility.

Placement is selected separately as `local`, `distributed`, or `auto`. A
strategy produces execution units; a shared dispatcher decides where each unit
runs. Strategies do not implement separate local and distributed transports.

Validation is independent from candidate generation and winner selection.
Validators return versioned evidence. An ensemble winner is selected from that
evidence, and an outcome without deterministic confirmation is labelled
`unverified`.

## Why the production set is intentionally small

DAG already carries project memory, review, revision, artifact extraction, and
the compatibility contract. Ensemble is the only additional shape backed by an
existing experiment and measured evidence. Productionizing these two exercises
the extension seams without multiplying untested modes before trusted alpha
testing.

Map, research, consensus, debate, marketplace, token, blockchain, and
model-sharding modes are outside this decision. Adding an identifier to the
registry is not enough to claim a new production strategy; a future mode needs
its own contract, dispatch semantics, validation policy, persistence coverage,
and measured acceptance criteria.

## Consequences

- REST, CLI, MCP, and compatibility endpoints construct one canonical request.
- Legacy pitch requests still resolve to DAG behavior.
- Strategy and placement metadata survive coordinator restarts.
- Complete-candidate distributed work uses the same worker lease protocol as
  DAG units.
- The registry and validator interfaces add modest indirection, but remove the
  larger cost of copied orchestration implementations.
- Protocol version and strategy version advance independently.

## Rejected alternatives

### One orchestration implementation per strategy/placement pair

Rejected because six initially useful combinations would create six places for
queueing, fallback, security, events, and persistence behavior to drift.

### A separate direct strategy

Rejected because it is behaviorally identical to ensemble with a candidate
count of one.

### LLM-based automatic selection

Rejected for protocol v1 because it adds cost, latency, and nondeterminism to a
decision that can begin with conservative explicit rules.

### Promote every proposed strategy now

Rejected because an extensible registry is not evidence that a mode is safe or
useful. The trusted alpha needs a small, testable protocol surface.
