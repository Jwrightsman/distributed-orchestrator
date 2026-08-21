# Execution Architecture

Mycelium separates four decisions that the old pitch route combined: request
validation, strategy selection, placement, and output validation. REST, CLI,
MCP, and compatibility endpoints all enter the same service.

## Request and strategy selection

```mermaid
flowchart TD
    I[REST / CLI / MCP] --> R[ExecutionRequestV1]
    R --> S[StrategySelector conservative-v1]
    S -->|explicit dag or compatible auto| D[DAG strategy v1]
    S -->|ensemble, direct, or contracted auto| E[Ensemble strategy v1]
    S --> M[Recorded selector reason and version]
    D --> X[ExecutionService]
    E --> X
    X --> P[(SQLite execution record)]
```

`direct` has no strategy class. The selector records the request as `direct`
and invokes ensemble with one candidate. Adding a future strategy requires a
contract, registry entry, strategy adapter, validators, and tests; it does not
require new route-specific transport code.

## DAG execution

```mermaid
sequenceDiagram
    participant S as ExecutionService
    participant D as DagStrategy
    participant P as Existing pipeline
    participant X as Dispatcher
    participant V as ValidatorRegistry
    S->>D: execute(request, DagOptionsV1)
    D->>P: plan task with bounded maximum
    loop dependency-ready builder wave
        P->>D: build_fn(subtask, dependency context)
        D->>X: execute dag_subtask unit
        X-->>D: attempt result + placement metadata
        D-->>P: builder output
    end
    P->>P: review and optional revision
    P->>P: extract artifacts and update project memory
    P-->>D: legacy pipeline result
    D->>V: validate final output and artifacts
    D-->>S: normalized strategy outcome
```

The adapter preserves the mature pipeline; it does not copy planning, review,
revision, extraction, or memory into an HTTP route.

## Ensemble execution

```mermaid
flowchart TD
    E[EnsembleStrategy] --> C[Compile 1..5 complete candidate units]
    C --> B[Bounded concurrency semaphore]
    B --> X1[Dispatcher candidate 1]
    B --> X2[Dispatcher candidate 2]
    B --> XN[Dispatcher candidate N]
    X1 --> A1[Persist candidate artifact]
    X2 --> A2[Persist candidate artifact]
    XN --> AN[Persist candidate artifact]
    A1 --> V1[Independent validation]
    A2 --> V2[Independent validation]
    AN --> VN[Independent validation]
    V1 --> W[Evidence-based winner selection]
    V2 --> W
    VN --> W
    W -->|required validators pass| OK[completed]
    W -->|none pass, fallback allowed| U[unverified]
    W -->|no usable candidate| F[failed]
```

One failed candidate does not cancel the batch. Distributed candidates carry
the complete task and contract, not a decomposed fragment.

## Placement-independent dispatch

```mermaid
flowchart LR
    U[Execution unit] --> P{Placement decision}
    P -->|local| L[Local Ollama executor]
    P -->|distributed| Q[Atomic bounded worker queue]
    Q --> N[Qualifying assigned node]
    N --> R[Bound attempt result]
    R --> O[DispatchResult]
    L --> O
    Q -->|no node / timeout / worker failure| G{Fallback allowed?}
    G -->|yes| L
    G -->|no| F[Failed DispatchResult]
```

The placement decision filters confidentiality, approved nodes, capabilities,
and blacklist state before the strategy dispatches. Every fallback is attached
to the unit and normalized result.

## Validation and reduction

```mermaid
flowchart TD
    O[Generated output + artifact references] --> R[ValidatorRegistry]
    R --> N[nonempty]
    R --> J[structured JSON / JSON Schema]
    R --> M[file manifest]
    R --> C[code parse]
    R --> A[artifact extraction]
    N --> E[Structured evidence]
    J --> E
    M --> E
    C --> E
    A --> E
    E --> G{Required evidence passes?}
    G -->|yes| W[Eligible for verified winner]
    G -->|no| X[Rejected candidate]
    X --> U{Unverified fallback allowed?}
    U -->|yes| H[Explicit unverified result]
    U -->|no| F[Failed result]
```

Strategy generation cannot declare itself correct. The validator registry owns
mechanical evidence; the strategy owns reduction and explanation.

## Legacy endpoint adaptation

```mermaid
flowchart TD
    P1[POST /pitch] -->|default local| A[Pitch adapter]
    P2[POST /pitch/async] -->|default auto placement| A
    P3[POST /pitch/distributed] -->|default distributed| A
    A --> R[ExecutionRequestV1]
    R --> S[ExecutionService]
    S --> N[ExecutionResultV1]
    N --> C[Compatibility payload]
    C --> L[Legacy fields retained]
    C --> M[Normalized metadata added]
```

`routes_pitch.py` owns authentication, rate limiting, callbacks, legacy job
shape, and response adaptation. It does not own strategy or transport logic.

## Persistence and events

```mermaid
flowchart LR
    S[ExecutionService] -->|queued/running/final snapshots| DB[(executions table)]
    S --> EV[Existing event store and websocket broadcaster]
    DB --> GET[GET /v1/executions/id]
    EV --> OLD[Existing consumers]
    EV --> NEW[Protocol-aware consumers]
```

The idempotent migration creates `executions` beside existing `jobs` and
`events`; it does not delete or reinterpret legacy history. Request JSON,
selection, placement, bounded candidate/validation summaries, errors, and the
normalized result are durable. The worker queue, leases, in-flight tasks, and
background coroutines remain process-local.

## Worker trust boundary

Admission still uses one shared node secret. Protocol-v1 assignments add
structural attempt binding: assigned node, task, attempt id, nonce, execution,
unit, and lease expiry must match on token streams and results. This prevents a
different admitted node from settling someone else's active attempt under its
own id, but it does not establish cryptographic node identity. Per-node keys,
revocation, rotation, signed receipts, isolation, and permissionless settlement
remain prerequisites for a less-trusted network.
