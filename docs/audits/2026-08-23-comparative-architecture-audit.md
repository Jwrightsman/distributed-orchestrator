# Mycelium Comparative Architecture Audit

> **Status:** Point-in-time architecture audit; non-normative.
> **Audit date:** 2026-08-23
> **Audited revision:** `0ed9be0`
> **Repository revision when archived:** `8a9a09a2920dc376c60b7e63064df61e5c0602d3`
> **Authority:** Current source code, protocol documentation, and accepted ADRs
> supersede this report when they conflict.
> **Evidence note:** Test results quoted from sprint records are
> maintainer-recorded unless the report explicitly states that they were
> independently rerun.

**Repository:** `Jwrightsman/distributed-orchestrator`  
**Project name:** Mycelium  
**Audit date:** 2026-08-23  
**Implementation baseline:** default branch `master`; runtime behavior pinned to the repository's recorded Trusted-Alpha RC1 integrated checkpoint `0ed9be0`. Later records observed during the audit were documentation updates rather than claimed runtime changes.

## Evidence and audit limits

This audit established the repository model from the complete repository tree, the architecture-bearing source modules, worker and coordinator routes, execution protocol models, strategies, persistence, artifact handling, access control, deployment tooling, operations documents, roadmap, ADR inventory, test inventory, and exact immutable source snapshots at the RC1 checkpoint. It also reviewed the repository's recent commit map and release evidence.

The repository records 812 passing tests and 2 skipped tests at `0ed9be0`, plus focused trusted-alpha harness runs. Those are maintainer-recorded results. This environment could not clone the repository or rerun its suite because outbound GitHub DNS was unavailable, so this report does not claim independent test reproduction. The GitHub HTML cache also lagged raw branch content during the audit; immutable commit files and the repository's own checkpoint record were therefore used as the behavioral baseline.

For adjacent systems, the audit prioritized official specifications, official documentation, source repositories, and papers. Maturity statements distinguish observed source and releases from paper proposals and marketing claims.

---

# Executive conclusion

Mycelium is not yet a decentralized compute network. It is a credible, unusually honest trusted-coordinator execution system for local and invited contributor hardware. Its strongest architectural work is not its planner/builder/reviewer prompt pipeline. It is the attempt-authority and artifact-integrity boundary around remote work:

- strategy and placement are independent;
- a remote result must settle the current server-issued attempt;
- attempts bind execution, unit, node, node session, nonce, lease, contract, and output budget;
- exact retries replay the durable settlement rather than earning twice;
- rejected or stale submissions are quarantined;
- terminal artifacts are role-scoped, sealed, hashed, and rechecked on delivery;
- lifecycle, validation outcome, and assurance are represented separately;
- remote dispatch requires explicit consent and a non-local confidentiality class.

Those properties are stronger than the normal early-stage agent orchestrator and stronger than the concrete implementation available in SwarmHarness, because SwarmHarness is currently a protocol paper rather than a found executable system.

The main architectural weakness is a mismatch between the strength of remote attempt settlement and the weakness of top-level execution durability. The scheduler, coroutine graph, node sessions, waits, and verification work are process-local. More seriously, terminal persistence retries can exhaust while the service still emits and returns the in-memory terminal result. After a restart, the last durable row can be reconciled as interrupted. That violates the client-visible completion contract and is the clearest release-blocking defect found.

The right near-term direction is not to import a workflow engine, a blockchain, a DHT, or a general agent framework. It is to harden four narrow boundaries:

1. make submission and terminal publication idempotent and durable;
2. separate revocable node enrollment identity from ephemeral worker sessions;
3. turn capabilities and assurance evidence into versioned, measured contracts;
4. preserve pluggable seams for durable scheduling, validator isolation, provenance, telemetry, and interoperation without implementing permissionless machinery yet.

The recommended alpha product is: **auditable local-AI execution across machines whose operators you deliberately trust**. The product should not claim trustless compute, verified inference, decentralized scheduling, or a functioning incentive market.

---

# 1. Accurate current-architecture model

## 1.1 System boundary

Mycelium is a single-coordinator Python service with multiple adapters and local or remote model execution. The canonical execution service is shared by REST, CLI, MCP, and legacy endpoints. The default generation backend is Ollama; optional external OpenAI-compatible planner/reviewer routing exists as an operator choice.

The deployed trusted-alpha topology is:

```text
requester / CLI / MCP / web
            |
            v
  canonical ExecutionRequestV1
            |
            v
     ExecutionService
       |          |
       |          +--> ExecutionStore / ArtifactStore / event and ledger stores
       |
       +--> StrategySelector --> DAG or Ensemble (Direct = Ensemble N=1)
                                  |
                                  +--> local dispatcher
                                  |
                                  +--> process-local remote task broker
                                                |
                                                v
                                     invited worker over HTTP
                                     Ollama inference on worker
                                                |
                                                v
                                     attempt-bound settlement
                                                |
                                                v
                                   validation / artifact sealing
                                                |
                                                v
                                      terminal ExecutionResultV1
```

It is deliberately one coordinator per state directory. SQLite is configured for concurrent threads/coroutines in one process, not for multiple coordinators or shared network filesystems. The release tooling adds a cross-platform coordinator ownership lock and fail-closed trusted-alpha preflight.

## 1.2 Canonical request and result contracts

`ExecutionRequestV1` is strict and versioned. It carries:

- task text;
- requested strategy and strategy options;
- placement independent of strategy;
- explicit remote-dispatch consent;
- required capabilities and approved node IDs;
- an output contract;
- verification policy;
- confidentiality class;
- deadline, output budget, and recorded network policy.

Output contracts cover text, one artifact, structured JSON, file manifests, and code. JSON Schema is bounded and fixed to Draft 2020-12. Required paths are normalized and bounded. Candidate counts, validators, artifacts, timeouts, task size, and output size are all bounded.

Important limitation: required capabilities are currently free-form strings. They are useful filters, but they are neither a versioned resource ontology nor trustworthy claims. There is no formal CPU/GPU/RAM/storage quantity model, model digest requirement, benchmark result, attestation, or capability namespace.

`ExecutionResultV1` separates:

- lifecycle status: queued, running, completed, failed, cancelled, interrupted;
- validation outcome: passed, failed, partial, not run;
- assurance: unverified, structural, deterministic, model-judged;
- requested, planned, and observed placement;
- attempts, retries, reassignments, fallbacks, participating nodes;
- units and candidates;
- validation evidence;
- artifact and share metadata;
- contribution and operational telemetry.

This separation is an architectural strength. It prevents a completed model call from being mislabeled as a correct result.

## 1.3 Strategy selection and execution

Implemented strategies are:

- **DAG:** reuses the existing planner/builder/reviewer/reviser pipeline. Planning and review remain local; builder units can be local or remote. It supports project memory.
- **Ensemble:** runs one to five complete candidates concurrently, materializes each in an isolated candidate subtree, validates them, and deterministically selects among acceptable candidates.
- **Direct:** represented as ensemble with one candidate rather than a separate code path.
- **Auto:** a conservative deterministic selector that records its reason and selected version.

The ensemble winner key prefers stronger assurance, then validation score, then lower generation latency, then stable candidate order. The code explicitly does not establish semantic correctness. A configured unverified fallback can return the best completed candidate when none passes the acceptance policy.

Strategy selection and placement are orthogonal. This is the correct abstraction: a DAG can run locally or distribute builder units; an ensemble can also run locally or distribute whole candidates.

## 1.4 Scheduling and capability routing

The current scheduler is a bounded in-process queue. Qualifying nodes are selected by:

- required capability subset;
- optional approved-node allowlist;
- current draining or blacklist state;
- deterministic ordering.

The coordinator can fall back locally when allowed. Remote waits use an in-memory broker with a durable accepted-receipt lookup as a recovery path for the specific commit-before-publish race.

What is not durable:

- queued coroutine ownership;
- the scheduling queue as a resumable work source;
- workflow step progress;
- node sessions;
- waiters and event subscribers;
- post-hoc verification tasks;
- general retry plans.

On restart, persisted queued or running executions are truthfully converted to interrupted and retryable because there is no coroutine or workflow state to resume.

## 1.5 Worker lifecycle and remote attempt authority

Worker admission and incarnation are separate only partially:

1. A worker presents the instance-wide node admission secret.
2. It registers a node label and capabilities.
3. The server creates a process-local session ID and one-time bearer token, storing only a digest.
4. Poll, result, stream, heartbeat, and drain operations require the session.
5. An active node ID cannot silently be replaced. A stale session can be reclaimed, and its active attempts can be reclaimed.
6. Coordinator restart invalidates all sessions; workers re-register with backoff.

Remote assignment creates a durable server-issued attempt. Settlement checks the active attempt and binds, among other fields:

- task and attempt;
- execution and unit;
- assignment kind;
- assigned node;
- assigned node session;
- contract or request binding;
- nonce digest;
- lease state;
- output and stream budgets.

Settlement, accepted receipt, and contribution recording occur transactionally. A retry with the same accepted result hash receives exact replay semantics. Conflicting, expired, over-budget, stale, unbound, or wrongly assigned submissions are rejected and can be quarantined.

This is the strongest subsystem in the repository. It is a practical capability/authority-transfer design, not merely a task ID posted to a worker.

Its limits are also clear:

- the shared admission secret is not node identity;
- the session token is an ephemeral bearer credential, not a cryptographic principal;
- an admitted operator can claim a fresh label after restart;
- there is no independent per-node revocation or attribution key;
- there is no Sybil resistance or hardware/model attestation.

## 1.6 Retries, leases, idempotency, and cancellation

Remote attempts have leases, attempt identities, supersession/reclamation states, and idempotent settlement. A late or duplicate result cannot replace an already authoritative settlement simply by reusing a task ID.

Cancellation is cooperative:

- queued work can be removed;
- active attempts can be cancelled or made non-authoritative;
- strategy candidate tasks are explicitly cancelled and gathered when a deadline or operator cancellation fires;
- a worker-side model call may still consume compute until its cancellation path takes effect.

There is no top-level submission idempotency key. Every `submit` creates a new UUID and a new queued execution. Therefore a client timeout and retry can create duplicate work, artifacts, and contribution records even though each individual remote attempt settles exactly once.

## 1.7 Persistence and coordinator recovery

Durable records include:

- canonical request and result snapshots;
- remote attempts and their state transitions;
- accepted receipts and quarantine records;
- contribution records;
- artifact roots, role-scoped manifests, and hashes;
- shares and revocation state;
- selected operational/event data.

Persistence is explicitly not a durable scheduler. Restart reconciliation marks nonterminal execution rows interrupted.

### Critical consistency defect

`ExecutionService._persist_terminal` retries a terminal write three times and returns false after failure. Normal completion, background crash, cancellation, and callback paths call it without checking the result. The service then emits a terminal event, stores a terminal in-memory copy, and returns it. If the durable row is still running, a restart converts it to interrupted.

This can produce two contradictory truths:

```text
client observed: completed / artifact URLs / assurance X
last durable row: running
post-restart row: interrupted / retryable
```

The code comment says a malformed adapter result must never escape while leaving the previous row running, but terminal storage failure can do exactly that. The fix is not simply more retries. Terminal publication must be conditioned on a durable commit, or completion must be represented through a durable outbox/materialization state that can finish after restart.

A related recovery boundary remains after durable remote attempt settlement: an accepted remote receipt can exist before the top-level execution has durably incorporated it into its final result. The broker can recover the receipt during the live process, but after a crash the top-level execution is interrupted rather than materialized to completion.

## 1.8 Validation and assurance

Validation is contract- and task-specific at the structural layer:

- nonempty output;
- JSON parsing and JSON Schema;
- manifest/file requirements;
- artifact extraction;
- code parsing for supported forms;
- explicit requested validators.

The validator aggregation distinguishes contract-floor checks from optional checks. The implementation correctly marks its built-in structural validators as not proving behavioral correctness.

Current gaps:

- generated or supplied code is not executed in a sandbox;
- validators run in the coordinator process or a thread and therefore parse untrusted outputs inside the control plane;
- code parsing is not execution correctness;
- duplicate verification is mostly output-shape comparison;
- post-hoc duplicate verification is disabled in trusted-alpha mode because its status and evidence are not durable;
- there is no hidden-canary, deterministic test harness, trusted tie-breaker, or downstream-outcome reputation loop as a general subsystem.

## 1.9 Shared context and memory

Persistent projects use an append/grow `memory.md` style context that is injected into planner and reviewer prompts. This is implemented for the DAG path. Ensemble and direct reject `project_id` because selected-result-only memory updates and candidate contamination semantics are not implemented.

The current memory is useful but not yet a shared-context architecture. It lacks:

- typed memory records;
- namespace and ownership rules;
- retention and compaction policy;
- conflict resolution;
- provenance links from memory facts to executions/artifacts;
- safe candidate isolation and selected-result commit semantics;
- access policy separate from project access.

## 1.10 Discovery and networking

Implemented:

- direct coordinator URL;
- local/LAN discovery conveniences;
- documented private-overlay deployment such as Tailscale;
- HTTP long polling and streaming;
- worker re-registration and backoff.

Not implemented:

- peer-to-peer discovery;
- DHT routing;
- NAT traversal or relays as a protocol subsystem;
- federated coordinators;
- peer-to-peer inference/model-layer sharding;
- decentralized scheduling.

This is appropriate for the current trusted-alpha claim. NAT traversal and P2P discovery should be treated as a later transport architecture, not a feature added to the current queue.

## 1.11 Privacy, sandboxing, and artifact security

Privacy controls that exist:

- local-only default placement;
- explicit remote consent;
- confidentiality classes and approved-node allowlists;
- operator warning that workers see prompts;
- separate viewer, requester, and node authorities in trusted-alpha deployment;
- authenticated artifact delivery;
- explicit revocable redacted share capabilities;
- no-store/no-referrer/nosniff protections for shares.

Limitations:

- prompts are visible to assigned worker operators;
- `network_policy` is declared intent, not an enforced firewall;
- generated code is not sandboxed;
- validators and artifact extraction operate in the coordinator trust domain;
- share tokens and worker sessions are bearer capabilities;
- transport security depends on TLS/private overlay configuration;
- there is no tenant isolation or secret redaction policy for prompts beyond request-level placement controls.

Artifact security is stronger than typical agent systems. Manifest paths are normalized, symlinks and traversal are guarded, roots are role-scoped, terminal manifests are sealed transactionally, bytes are rehashed on read, and ZIP creation uses one manifest snapshot. This establishes local tamper detection. It does not establish who produced an artifact, whether the host itself is honest, or whether the code is correct. Hashes are unsigned and host-local.

## 1.12 Reputation, credits, and incentives

The implemented ledger records contribution points. Remote settlement can award a fixed amount for an accepted nonempty output. The repository is explicit that these are nonmonetary contribution points.

The ledger does not currently represent verified quality, resource-normalized work, market value, stake, spendable credits, disputes, or identity-stable reputation. Awarding points on accepted delivery is safe as an activity indicator inside a trusted alpha, but it must not be marketed as trust or quality reputation.

## 1.13 Observability and operator experience

Implemented operational features include:

- status and health endpoints with sanitized public output;
- events and WebSocket progress;
- metrics endpoints;
- execution telemetry for placement, attempts, retries, reassignment, fallback, and timings;
- preflight checks;
- backup/restore scripts and tests;
- bounded multi-node and nightly harnesses;
- deploy/install scripts, Compose, LAN and private-overlay guidance;
- drain and session state;
- a single-coordinator lock;
- trusted-alpha runbooks.

The repository's release evidence explicitly does not establish:

- Docker image build/start at the RC1 checkpoint;
- live Ollama quality in the trusted-alpha harness;
- WAN or reverse-proxy behavior;
- production TLS configuration;
- long-running external-node churn;
- scheduled/off-site/encrypted backup;
- coordinator high availability.

The telemetry is custom. It does not yet provide standard distributed trace context across requester, coordinator, worker, model call, validator, settlement, and artifact operations.

---

# 2. Implemented versus planned

| Area | Implemented now | Planned, deferred, or only documented |
|---|---|---|
| Strategies | DAG, ensemble, direct-as-N=1, deterministic auto | Map/consensus as first-class strategies; richer task-specific routing |
| Placement | Local, distributed, auto, local fallback | Data-local, price-aware, topology-aware, or federated placement |
| Typed contracts | Versioned strict request/result, bounded output contracts, JSON Schema | Namespaced/versioned resource quantities, measured capability attestations |
| Attempts | Durable authoritative attempts, leases, nonce/session/node/unit binding, exact replay, quarantine | Signed input/output envelopes tied to per-node public keys |
| Scheduler | Bounded process-local queue and deterministic node filter | Durable queue/workflow state, resumable execution, HA coordinator |
| Identity | Shared admission secret plus ephemeral server sessions | Per-node cryptographic enrollment, independent revocation, Sybil controls |
| Validation | Structural/schema/manifest/code-parse validators | Durable layered verification, hidden canaries, trusted adjudication, sandboxed execution |
| Artifacts | Role-scoped sealed local manifests, rehash on read, authenticated delivery | Signed provenance, external transparency log, reproducible build attestations |
| Memory | DAG project `memory.md` context | Typed shared memory, ensemble/direct commit policy, namespace/retention/conflicts |
| Discovery | Direct URL, LAN convenience, private overlay documentation | libp2p/DHT/NAT traversal, federation |
| Privacy | Local default, consent, confidentiality classes, allowlists | Enforced network policy, confidential computing, end-to-end task encryption where feasible |
| Credits | Contribution points on accepted work | Verification-linked accounting, spendable credits, disputes, incentives |
| Interop | REST, CLI, MCP, legacy adapters | A2A task/artifact adapter, external scheduler/executor interfaces |
| Observability | Events, metrics, execution telemetry, harnesses | OpenTelemetry propagation and production SLOs |
| Deployment | One coordinator, SQLite, Compose/config validation, runbooks, backup/restore | Proven image deployment, HA, rolling upgrades, signed releases/SBOM |
| Permissionless network | None, intentionally | Reputation, market, P2P, Sybil resistance, TEE/ZK/quorum verification |

---

# 3. Comparative subsystem matrix

Legend: **A** = adopt before alpha, **E** = experiment during alpha, **X** = design extension point only, **P** = post-alpha, **N** = permissionless-network phase, **R** = reject.

| Subsystem | Mycelium now | Strongest adjacent reference | Comparative finding | Decision |
|---|---|---|---|---|
| Task identity | Server-generated execution UUID; no request idempotency key | Temporal workflow ID, DBOS workflow ID, Restate invocation identity | Mycelium has strong attempt identity but weak client-submission identity | **A** |
| Terminal completion | Result snapshot persisted, but terminal write failure is not fail-closed | Restate/DBOS durable invocation checkpoints; Temporal event history | Client-visible completion must be downstream of durable commit | **A** |
| Task lifecycle | Explicit lifecycle separated from validation/assurance | A2A task states; Temporal/Restate execution history | Semantics are unusually clear; retain them and add durable transition invariants | **A/X** |
| Strategy selection | DAG/ensemble/direct/auto; deterministic selection | LangGraph/Agent Framework graphs; BOINC app classes | Mycelium's strategy/placement separation is better suited to contributor hardware; do not replace it | **R** for framework rewrite |
| Typed task contract | Strict protocol, output and verification contracts | HarnessAPI typed skills; Golem/Akash/Bacalhau resource specs; A2A skills | Strong base; capabilities need versioned quantitative descriptors | **A/X** |
| Scheduling | In-process bounded queue, capability subset filter | Temporal/Restate/DBOS durability; Bacalhau pluggable schedulers | Persisting every workflow step now is too large; durable candidate queue is a bounded experiment | **E** |
| Capability routing | Self-advertised strings and allowlists | Golem/Akash resource requirements; BOINC host/app-version statistics | Add measured capability evidence before sophisticated scoring | **E** |
| Node identity | Shared secret plus ephemeral session | mTLS/public-key identity proposals; Akash provider identity; A2A auth schemes | Sessions solve incarnation, not attribution/revocation | **A** before wider invited alpha |
| Leases and retries | Strong durable attempt settlement and exact replay | Temporal activity retries; BOINC workunits; marketplace leases | Already a major strength; add property-based state-machine tests, not a redesign | **A** |
| Cancellation | Cooperative process and attempt cancellation | A2A cancel semantics; workflow-engine cancellation scopes | Define idempotent cancellation result and worker resource-stop evidence | **E/X** |
| Coordinator recovery | Truthful interruption, no resume | Temporal/Restate/DBOS replay/checkpoint | Honest behavior is preferable to fake recovery; test narrow resumable units first | **E** |
| Result verification | Structural validators; duplicate shape compare; post-hoc disabled in trusted alpha | BOINC app-specific validators/adaptive replication; deterministic test systems | Build a task-class assurance ladder; never generalize agreement into correctness | **E** |
| Shared context | DAG project memory file | LangGraph checkpointers/stores; Letta namespaced persistent memory | Separate execution checkpointing from long-term memory; add selected-result commit policy | **E/P** |
| Discovery/NAT | Coordinator URL, LAN/private overlay | libp2p AutoNAT/hole punching/relay; Hivemind DHT | Production P2P is an entire subsystem; keep a transport seam only | **X/N** |
| Privacy | Consent, classes, allowlists; worker sees prompt | Petals' explicit privacy warning; sandbox/TEE systems | Current disclosure is honest; enforce network and filesystem policy only when code execution is added | **A/E** |
| Sandboxing | None; code not run as trusted result verification | gVisor, Firecracker, WASM/OCI execution in Bacalhau/Golem | Isolate validators/executable artifacts outside coordinator; choose based on threat and compatibility | **E/P** |
| Reputation | Fixed contribution points for accepted output | BOINC app-version scoped trust and recent average credit | Separate contribution, reliability, and correctness; no global scalar reputation yet | **X/E** |
| Incentives/market | No currency or market | Akash leases/escrow; Golem payments; SwarmHarness local credits proposal | No evidence of demand or scarcity; market mechanics would dominate the product prematurely | **N/R now** |
| Protocol interop | REST, MCP, canonical internal model | A2A tasks/messages/artifacts/cards; MCP tools/resources | Map at adapters; do not let A2A or MCP dictate internal settlement semantics | **X** |
| Provenance | Local hashes and sealed manifests | in-toto/SLSA attestations; Sigstore identities/transparency | Add a provenance envelope seam; signing is useful after stable identities and releases | **X/P** |
| Verifiable compute | No proof of semantic correctness | RISC Zero and other zkVMs for deterministic programs | Proof can show a program ran, not that a stochastic answer is true; only bounded deterministic workloads qualify | **N** |
| Observability | Custom events and metrics | OpenTelemetry traces; Agent Framework built-in OTel | Propagate execution/attempt/unit/validator/artifact trace context without prompt leakage | **E** |
| Operator experience | Strong alpha preflight/runbooks; unproven image/WAN/live-model release path | BOINC provider operations; Akash provider runbooks | Run a production-like dress rehearsal before widening alpha | **A** |

---

# 4. Most important lessons from each system

## 4.1 SwarmHarness

### What it is

SwarmHarness is a May 2026 protocol paper proposing a decentralized layer on HarnessAPI. The paper describes SwarmNode, a Kademlia-style registry, utility routing, local blockchain-free credits, Shapley-like attribution, capability advertisements, NAT traversal, and staged progression from alpha to federation to decentralization.

### What is implemented

No public SwarmHarness repository or package was found in the author and project searches performed for this audit. The paper is therefore the implementation artifact. HarnessAPI source exists; SwarmHarness source was not found.

### Assumptions and claimed guarantees

The design assumes mostly rational, nonmalicious participants for its initial phase. It proposes signed attribution, local ledgers, countersigning, trust history, proof-of-work registration, mTLS, and sandboxing. Byzantine behavior, subjective quality, collusion, ledger convergence/double spending, and efficient cryptographic proof remain open.

The claim that local ledgers avoid double spending is under-specified. Without a serialization authority, consensus, escrow, or conflict-resolution protocol, two partitions can accept incompatible spends. Submitter-signed quality can also collude or equivocate. The DHT description lacks a complete authenticated freshness, conflict, and wire protocol.

### Lesson for Mycelium

Copy the separation of discovery, routing, execution, accounting, and deployment phases. Do not copy the mechanisms yet. Mycelium already has a more concrete and stronger centralized attempt-settlement boundary. Its coordinator is a limitation, but also the reason exact settlement and simple operations are possible.

**Disposition:** DHT, local spendable ledgers, Shapley credit, proof-of-work registration, and permissionless routing are **permissionless-network phase** or **reject now**. A pluggable discovery and accounting interface is **extension point only**.

## 4.2 HarnessAPI

HarnessAPI is a small, real source project and paper that turns typed Python skill folders into HTTP JSON/SSE, OpenAPI, and MCP surfaces. Its strongest idea is a canonical typed handler with generated adapters. It is not a distributed scheduler, workflow engine, trust system, or validation network.

The current implementation can run normal handlers in-process; subprocess/container/Kubernetes isolation is optional and tenant-oriented. Therefore it should not be cited as proof that SwarmHarness jobs are sandboxed by default.

Mycelium already follows the important architectural lesson: one canonical service and typed protocol behind REST, CLI, MCP, and legacy adapters. A dependency or rewrite would add little. HarnessAPI may be useful as an interoperability target for packaging individual capabilities.

**Disposition:** typed skill adapter compatibility is **extension point only**; replacing Mycelium's execution core is **reject**.

## 4.3 Temporal

Temporal demonstrates event-history durability, deterministic workflow replay, stable workflow/run identities, timers, retries, and durable completion. Its central cost is that workflow code must obey replay and side-effect constraints.

Mycelium should borrow stable request identity, durable transition semantics, and clear retry/cancellation rules. It should not rewrite its agent pipeline into a replay-deterministic workflow DSL before product evidence. Model calls, filesystem materialization, legacy pipeline behavior, and callbacks would make such a migration large and constraining.

**Disposition:** idempotent workflow/submission identity is **adopt before alpha**; general workflow replay is **experiment during alpha** then **post-alpha** if justified; a Temporal-style rewrite is **reject**.

## 4.4 Restate

Restate's strongest lesson is durable invocation: completed steps, timers, communication, and shared state are persisted so a handler can continue after failure. Its model makes invocation identity and completion a storage concern rather than a best-effort callback concern.

For Mycelium, the actionable lesson is atomic or recoverable terminal publication. A result should not be visible as terminal until durable state can reproduce it. A completion outbox or materialization state is more relevant than adopting the whole server.

**Disposition:** durable terminal publication is **adopt before alpha**; Restate integration is **extension point only** unless future services need it.

## 4.5 DBOS

DBOS uses Postgres-backed checkpoints and durable workflows/queues. Its exact event/workflow ID pattern is directly relevant to client retries. It also illustrates that durable execution can be added around ordinary application code, but still requires explicit workflow and side-effect boundaries.

**Disposition:** submission key and narrow durable queue experiment are **adopt before alpha** and **experiment during alpha**, respectively. A database platform migration without SQLite evidence is **reject now**.

## 4.6 BOINC

BOINC provides the most important untrusted-compute lessons:

- validators are application-specific;
- exact, fuzzy, homogeneous, or single-result validation fit different workloads;
- replication is costly and should be adaptive;
- host trust is scoped to application/version behavior and repeated valid results;
- contribution accounting and result correctness are separate concepts.

For stochastic agent outputs, duplicate agreement often measures format or convergence, not truth. Mycelium should define task classes with explicit assurance ladders rather than one global verification score.

**Disposition:** task-class verification experiments are **experiment during alpha**; a universal scalar reputation or agreement-equals-correctness rule is **reject**.

## 4.7 Golem

Golem makes resource requirements, provider selection, execution images, allow/deny policies, and price filters explicit. It shows the value of separating requestor policy from provider advertisements.

For Mycelium, the lesson is a versioned capability/resource vocabulary: model identity/digest, context limit, CPU/GPU/RAM/storage, supported executor, network class, and measured performance. Payment and open provider-market machinery are not justified yet.

**Disposition:** resource contract schema is **adopt before alpha** as an extensible minimal model; bidding/payment is **permissionless-network phase**.

## 4.8 Akash

Akash demonstrates how much machinery a real compute marketplace requires: declarative resource specs, provider bidding, leases, escrow, certificates, Kubernetes operations, audits, GPU attributes, monitoring, and provider lifecycle procedures.

The lesson is mostly negative: a market is not a ledger field added to a scheduler. It becomes the product and operating model.

**Disposition:** preserve accounting and pricing interfaces only; market implementation is **permissionless-network phase** and **reject now**.

## 4.9 Bacalhau

Bacalhau's useful architecture is pluggability across executors, input/storage sources, schedulers, and result publishers. It also treats container/WASM execution and data locality as first-class.

Mycelium should make validator/executor, scheduler, artifact publisher/provenance, and identity/discovery boundaries explicit. It should not add all implementations now.

**Disposition:** interface extraction is **extension point only**; a sandboxed validator/executable-artifact pilot is **experiment during alpha**.

## 4.10 Petals

Petals narrows its protocol to tensor/model-block exchange and states its privacy limits clearly: public peers may infer inputs/outputs or alter computation, and arbitrary code should not be treated as safe. Its public swarm demonstrates real distributed inference but does not establish correct or private inference.

Mycelium's task-level parallelism is a better fit for WAN contributor hardware than making model-layer sharding its core. Preserve the repository's current rejection of WAN model sharding as the primary abstraction.

**Disposition:** privacy disclosure lesson is **adopt before alpha**; WAN model-layer sharding is **reject as core architecture**.

## 4.11 Hivemind and libp2p

Hivemind shows decentralized DHT coordination without a master for training-oriented workloads. libp2p provides encrypted transports, multiplexing, peer identity primitives, hole punching, AutoNAT, relays, and cross-language implementations.

The lesson is not to hand-roll NAT traversal or a DHT. If coordinator centrality becomes an observed constraint, place a discovery/transport interface now and later use a mature stack.

**Disposition:** interface only now; P2P implementation is **permissionless-network phase**.

## 4.12 LangGraph

LangGraph separates thread-scoped checkpoints from long-term namespaced stores. It also makes durable execution, streaming, human intervention, and stateful graphs explicit.

Mycelium should adopt the conceptual separation between execution state and memory. Its project `memory.md` should not become the workflow checkpoint or a global shared prompt. Candidate outputs should commit to memory only after selection and policy checks.

**Disposition:** selected-result memory policy is **experiment during alpha**; general checkpointed graph replacement is **reject now**.

## 4.13 Microsoft Agent Framework and AutoGen

AutoGen remains historically useful for typed messages, decoupled agent runtimes, and distributed runtime concepts, but its repository now states maintenance mode and directs new users toward Microsoft Agent Framework. Agent Framework is the active comparison: graph workflows, checkpointing, streaming, human intervention, time travel, OpenTelemetry, MCP, and A2A support.

The lesson is interoperability and observability, not framework adoption. Mycelium has a different core problem: remote authority, contributor-node lifecycle, artifact settlement, and local hardware.

**Disposition:** A2A/MCP/OTel adapters are **extension point only** or **experiment during alpha**; replacing the core with a general agent framework is **reject**.

## 4.14 Letta

Letta demonstrates persistent agent identity and memory, namespaced stores, and Git-backed shared repositories. Its current systems also allow high-powered self-modification of memory, skills, prompts, and harness state.

The useful lesson is explicit memory namespace, provenance, and synchronization. The risky lesson is allowing agents to mutate governing instructions without a controlled review boundary.

**Disposition:** namespaced provenance-linked context is **post-alpha** after a bounded experiment; unrestricted self-modifying shared context is **reject**.

## 4.15 A2A

A2A defines Agent Cards, skills, task/message/artifact objects, task states, streaming, cancellation, subscriptions, authentication schemes, extensions, and REST/gRPC/JSON-RPC bindings. It is an inter-agent interoperability protocol, not a durable scheduler or proof system.

Mycelium's execution/task/artifact models can map to A2A at the requester or federation edge. Internal attempt authority, settlement, validation, and worker sessions should remain Mycelium-specific unless an A2A extension can express them without weakening semantics.

**Disposition:** adapter and extension design is **extension point only**; replacing the worker protocol is **reject**.

## 4.16 MCP

MCP is a host-client-server protocol for tools, resources, prompts, and contextual interaction. Its specification emphasizes explicit user consent and warns that protocol metadata and tool behavior cannot themselves enforce security.

MCP is a strong requester interface for Mycelium and is already implemented. It should not be treated as worker identity, scheduling, durability, or verification.

**Disposition:** continue as interface; no architectural substitution.

## 4.17 SLSA, in-toto, and Sigstore

These systems distinguish provenance from correctness:

- in-toto records which steps occurred, in what order, and by whom;
- SLSA defines incremental supply-chain assurance and provenance expectations;
- Sigstore binds signatures to identities and transparency logs.

Mycelium's sealed manifest is a good local integrity foundation. A future provenance envelope can bind request hash, attempt ID, node identity, model/executor digest, validator versions, artifact hashes, and coordinator settlement. Signing before stable node and release identities would be premature.

**Disposition:** provenance data model interface is **extension point only**; signed release and artifact provenance is **post-alpha**.

## 4.18 gVisor and Firecracker

gVisor offers OCI-compatible isolation through a userspace kernel with compatibility and syscall overhead tradeoffs. Firecracker offers stronger microVM isolation with more infrastructure and host-configuration burden.

Mycelium's first need is not a public arbitrary-code service. It is isolating validators or artifact execution from coordinator credentials and state. A container/WASM/gVisor pilot is likely cheaper. Firecracker becomes rational only for genuinely hostile executable workloads and operators willing to support it.

**Disposition:** sandbox pilot is **experiment during alpha**; hardened execution service is **post-alpha**.

## 4.19 RISC Zero and verifiable computation

A zkVM can prove that a particular compiled deterministic program produced a journal from specified inputs. It cannot prove that an LLM answer is semantically true, that the prompt was appropriate, or that a stochastic model run corresponds to an intended high-level task unless the complete computation and model are inside the proof system.

This can eventually help deterministic validators, transforms, small model components, or accounting calculations. It is not a near-term general solution to untrusted agent inference.

**Disposition:** **permissionless-network phase**; reject as alpha verification architecture.

## 4.20 OpenTelemetry

OpenTelemetry provides standard trace/span propagation across process and service boundaries. Mycelium can map execution ID, unit ID, attempt ID, node session, validator run, settlement, and artifact operations into traces while excluding prompts, secrets, tokens, and raw model output by default.

**Disposition:** **experiment during alpha** and promote if it materially reduces incident diagnosis time.

---

# 5. Architectural risks and blind spots

## Critical

### R1. Terminal result can be published without durable terminal state

Impact: contradictory client history, restart misclassification, duplicate retry work, and untrustworthy audit records. This undercuts the central product claim of auditable execution.

### R2. Submission is not idempotent

Impact: client/network retries create duplicate executions even though worker attempt settlement is exactly-once. This can duplicate compute, artifacts, and points.

## High

### R3. Shared admission authority is being asked to stand in for identity

A leaked node secret admits any label. Sessions prevent live label takeover and bind an incarnation, but do not provide stable attribution or independent revocation.

### R4. Capabilities are self-asserted free-form tags

Routing can be manipulated or simply be inaccurate. Model names do not prove model bytes, quantization, context, speed, memory, or task success.

### R5. The scheduler/process boundary is sharper than the API suggests

Execution records look durable, but the execution itself is not resumable. That is acceptable only if the API and product consistently describe interruption and idempotent resubmission.

### R6. Validator execution shares the coordinator trust domain

Malformed parsers, decompression, artifact extraction, or future executable checks can affect the control plane. `network_policy` is not enforcement.

### R7. Verification evidence is not yet durable as a complete state machine

Trusted-alpha post-hoc verification is disabled for the correct reason, but this means current assurance is mostly structural. Credits are not quality reputation.

### R8. Project memory is a prompt-growth mechanism, not governed shared context

Without selected-result commit, provenance, retention, compaction, and conflict policy, expanding it across strategies would risk contamination and context bloat.

## Medium

### R9. No standard trace propagation

Custom events are useful but cross-machine diagnosis will become expensive under churn, retries, and reverse proxies.

### R10. No tenant fairness or admission policy beyond a bounded queue

A trusted requester can occupy the queue and model capacity. This is acceptable for a tiny guild but should be visible before multiple requesters.

### R11. Local hashes can be mistaken for provenance

The artifact store detects mutation relative to its own recorded baseline. It does not prove author, machine, model, or externally anchored time.

### R12. Release evidence is still environment-limited

The RC1 suite is substantial, but Docker start, live Ollama quality, WAN, TLS/reverse proxy, long-run churn, and real backup operations remain unproven at the recorded checkpoint.

### R13. Marketing language can outrun the security boundary

Terms such as decentralized, incentive-aligned, verified, or swarm can imply properties not present. The repository is mostly candid, but the positioning needs a stricter claim hierarchy.

---

# 6. Areas where Mycelium is already stronger

1. **Remote attempt authority.** It binds the accepted result to the current execution/unit/node/session/nonce/lease/output budget and makes exact settlement replay durable.
2. **Lifecycle versus assurance.** It does not conflate completion, structural validity, and correctness.
3. **Strategy versus placement.** This is cleaner than frameworks where distribution is embedded in a graph or agent topology.
4. **Explicit remote consent and confidentiality.** Many orchestration frameworks assume the operator already understands where prompts go.
5. **Role-scoped sealed artifacts.** Candidate provenance, deliverables, and audit files are distinguished, and bytes are rechecked on read.
6. **Truthful restart behavior.** Marking nonresumable work interrupted is better than implying durability that does not exist.
7. **Canonical service with multiple adapters.** REST, CLI, MCP, and legacy paths converge on one typed execution model.
8. **Candid threat model and roadmap.** Identity, sandboxing, Sybil resistance, verification limits, and central coordination are explicitly documented rather than hidden behind "decentralized" language.
9. **Conservative strategy claims.** The ensemble selector's deterministic evidence is not mislabeled as semantic proof.
10. **Local-first operator control.** The default is local-only, with external routing and remote dispatch requiring explicit choices.

---

# 7. Ideas that should not be copied

| Idea | Source/category | Reason to reject now |
|---|---|---|
| Blockchain/token settlement | DePIN/market systems | No demonstrated market, scarcity, dispute process, or stable identity; would dominate governance and threat model |
| Local spendable ledgers without consensus | SwarmHarness proposal | Partitioned double-spend and conflict resolution are under-specified |
| Shapley-like quality credits | SwarmHarness proposal | Marginal contribution is not observable robustly for stochastic multi-agent work; submitter scoring invites collusion |
| DHT scheduling in trusted alpha | P2P systems | Adds discovery, freshness, partition, abuse, and NAT failure modes without solving current durability defects |
| Duplicate agreement as correctness | Volunteer compute misapplication | Stochastic agents may agree on the same wrong pattern; disagreement does not identify the correct answer |
| One global reputation score | Marketplace/reputation patterns | Conflates uptime, honesty, task-class skill, model version, latency, and correctness |
| LLM judge as proof | Agent frameworks | A judge is another probabilistic model and can be correlated with candidate errors |
| Auto-executing generated code | Agent demos | Current workers and coordinator do not provide a safe execution boundary |
| Temporal-style workflow rewrite | Durable workflow engines | High migration and determinism cost before evidence that general resume is needed |
| A2A or MCP as internal trust protocol | Interop protocols | Neither supplies attempt settlement, identity proof, durable scheduling, or result correctness |
| Global mutable shared prompt memory | Shared-context systems | Causes contamination, hidden policy changes, race/conflict problems, and unbounded context growth |
| WAN model-layer sharding as the core | Petals-like inference | Mycelium's workload and measurements favor task-level async parallelism; sharding changes the product |
| Permissionless or "verified compute" marketing | Market/P2P literature | Current identity, verification, discovery, and incentives do not support the claim |

---

# 8. Recommendation register

Every recommendation includes the problem, repository evidence, cost/risk, phase, and success test.

## A1. Add canonical submission idempotency

- **Problem solved:** duplicate work from client, proxy, CLI, or MCP retries.
- **Evidence:** `submit` always creates a fresh UUID; there is no idempotency key or stable workflow/request ID, while remote settlement already has exact retry semantics.
- **Change:** accept an operator/requester-scoped idempotency key; persist key, canonical request hash, execution ID, and creation outcome atomically. Same key plus same request returns the existing execution; same key plus different request conflicts.
- **Cost/risk:** moderate schema/API work; retention and requester-scope rules must be explicit. A global key namespace would leak collisions across requesters.
- **Phase:** **adopt before alpha**.
- **Test:** concurrent identical submissions produce one execution; mismatched payload returns conflict; crash after insert but before response replays the same execution; expiration/retention behavior is deterministic.

## A2. Make terminal publication fail-closed or durably materialized

- **Problem solved:** clients observing a terminal result that storage cannot reproduce.
- **Evidence:** `_persist_terminal` returns false after three failures; callers ignore it and then emit, remember, and return terminal state. Restart reconciliation changes a remaining running row to interrupted.
- **Change:** choose one of two contracts:
  1. terminal state is published only after one durable transaction commits; or
  2. durable state enters `completion_pending` with an outbox/materialization record, and clients cannot receive final success until materialization is recoverable.
- **Cost/risk:** moderate/high because events, callbacks, artifacts, shares, and legacy adapters must agree on ordering. Blocking forever on a failed database is also wrong; return a durable `interrupted/storage_failed` outcome or service error.
- **Phase:** **adopt before alpha**.
- **Test:** inject failure into every terminal write and crash at every boundary. No client/event/share may claim completed unless a fresh process reads the same terminal result. Accepted remote receipts must remain recoverable without double credit.

## A3. Establish per-node revocable enrollment credentials

- **Problem solved:** shared-secret compromise, label impersonation after restart, and inability to revoke one operator.
- **Evidence:** sessions are process-local bearer credentials and intentionally not identity; the node secret is instance-wide admission.
- **Change:** provision one node enrollment identity per invited operator/machine, initially as a randomly generated node credential stored hashed and revocable. Public-key challenge-response can follow when signed provenance is needed. Keep server sessions as short-lived incarnation credentials.
- **Cost/risk:** moderate operational burden: enrollment UX, recovery, rotation, revocation, migration, and secret storage. Public-key design too early could overcomplicate installation.
- **Phase:** **adopt before widening alpha beyond a tightly controlled shared-secret group**.
- **Test:** revoke one node without rotating all nodes; prevent a second live process from claiming its identity; rotate credentials; restart coordinator and re-enroll session; verify audit attribution remains stable.

## A4. Run a production-like trusted-alpha release gate

- **Problem solved:** configuration-only confidence without proof of the actual deployment path.
- **Evidence:** RC1 records no Docker build/start, live Ollama quality, WAN, or TLS/reverse-proxy evidence.
- **Change:** one repeatable dress rehearsal with a clean coordinator, reverse proxy/private overlay, at least two independently operated workers, live Ollama, deliberate disconnect/restart, cancellation, credential rotation, backup/restore, artifact mutation detection, and log/metric capture.
- **Cost/risk:** low engineering, moderate operational coordination. Avoid turning one successful run into a performance guarantee.
- **Phase:** **adopt before alpha**.
- **Test:** scripted checklist produces an evidence bundle with versions and timestamps; repeat from empty state; recover from coordinator restart and backup; prove prompts are not exposed in public endpoints/logs.

## A5. Define a minimal versioned resource and capability vocabulary

- **Problem solved:** ambiguous free-form routing and future protocol lock-in.
- **Evidence:** `required_capabilities` is a list of strings; node advertisements are self-asserted.
- **Change:** preserve tags, but add versioned structured fields for executor kind, model identifier and digest if known, context limit, RAM, accelerator class/memory, supported artifact/validator types, and network/sandbox class. Separate claimed from observed fields.
- **Cost/risk:** moderate compatibility work. Over-specifying hardware can create brittle matching and privacy concerns.
- **Phase:** **adopt before alpha** for schema/extension fields; measured trust remains an alpha experiment.
- **Test:** old nodes remain compatible; unknown fields are ignored safely; scheduler can distinguish required versus preferred resources; false claims do not automatically raise trust.

## A6. Add property-based attempt/lifecycle tests

- **Problem solved:** state-machine bugs under reordering and crash boundaries.
- **Evidence:** extensive example-based tests exist, but roadmap adversarial cases include restart mid-submission, clock skew, disk full, and crash between verification and settlement.
- **Change:** model attempt and execution transitions and generate operation sequences: issue, poll, stream, renew/heartbeat, settle, retry, expire, reclaim, cancel, restart, and conflicting submission.
- **Cost/risk:** moderate test engineering; model must remain simpler than implementation.
- **Phase:** **adopt before alpha** for core invariants.
- **Test:** invariant suite proves at most one accepted settlement, no credit without accepted receipt, cancelled/superseded attempts cannot win, exact replay is stable, and terminal lifecycle never returns to nonterminal.

## E1. Pilot a durable queue for ensemble candidates only

- **Problem solved:** losing useful independent candidate work on coordinator restart.
- **Evidence:** ensemble units are naturally independent and already have durable remote attempts, while general DAG replay has side-effect and context complexity.
- **Change:** define a `SchedulerBackend` contract and persist ready candidate units, leases, and materialization state. Resume only idempotent ensemble/direct units in the experiment.
- **Cost/risk:** high; duplicate local execution, artifact path identity, and final selection recovery are subtle. It may expose that users prefer cheap resubmission.
- **Phase:** **experiment during alpha**.
- **Test:** crash at each queue/lease/receipt/materialization boundary; after restart complete exactly one final selection without duplicate credit or artifact collision. Compare complexity and recovery value against simple interrupted-and-resubmit behavior.

## E2. Build task-class assurance ladders

- **Problem solved:** structural validation being mistaken for correctness and expensive replication being applied indiscriminately.
- **Evidence:** current validators are structural; duplicate shape compare is weak and disabled post-hoc in trusted alpha.
- **Change:** define two or three bounded classes, for example structured extraction, patch-with-tests, and deterministic transform. Each class specifies deterministic checks, hidden canaries, replication trigger, adjudicator, evidence schema, and assurance ceiling.
- **Cost/risk:** high ongoing domain work; hidden tests can leak; validators can become the product bottleneck.
- **Phase:** **experiment during alpha**.
- **Test:** labeled corpus with known failures; measure false acceptance, false rejection, cost per accepted result, latency, and calibration. Promote only if it improves expected utility over one result plus human review.

## E3. Isolate validator and executable-artifact work

- **Problem solved:** untrusted artifacts and future runtime checks sharing coordinator privileges.
- **Evidence:** parsing/extraction/validation currently run in the coordinator process or threads; generated code is explicitly unsandboxed.
- **Change:** introduce `ValidatorExecutor`; first backend runs in a subprocess/container with no inherited credentials, read-only inputs, ephemeral output, no network, and CPU/memory/time/output limits. Compare WASM/container/gVisor compatibility before considering microVMs.
- **Cost/risk:** moderate/high packaging and cross-platform complexity; sandbox escape risk never becomes zero; model runtimes may not fit the sandbox.
- **Phase:** **experiment during alpha**; production hostile-code service is **post-alpha**.
- **Test:** malicious fixtures attempt network, filesystem traversal, process spawning, resource exhaustion, archive bombs, and secret reads. Coordinator remains healthy and credentials inaccessible. Measure overhead and validator compatibility.

## E4. Measure capabilities instead of trusting advertisements

- **Problem solved:** incorrect routing and misleading node reputation.
- **Evidence:** current capability strings and model names are self-reported.
- **Change:** collect observed model digest where available, first-token latency, throughput, context-limit canaries, memory failures, availability/churn, and success by task class/version. Keep these as evidence, not one global score.
- **Cost/risk:** moderate telemetry/storage; benchmarks consume volunteer resources and may be gamed.
- **Phase:** **experiment during alpha**.
- **Test:** predict deadline success and validator acceptance on held-out jobs better than static tags; detect model/capability drift; allow operators to opt out of nonessential benchmarks.

## E5. Add OpenTelemetry trace propagation

- **Problem solved:** multi-machine incident diagnosis and latency attribution.
- **Evidence:** custom events exist, but no standard cross-process trace connects requester, scheduler, worker, model, validator, settlement, and artifacts.
- **Change:** add a trace context separate from authority fields. Never put secrets, prompt contents, raw output, session tokens, or nonces in attributes.
- **Cost/risk:** low/moderate; cardinality and privacy mistakes can be expensive.
- **Phase:** **experiment during alpha**.
- **Test:** diagnose induced timeout/reassignment/storage-failure scenarios from traces faster than logs alone; verify sensitive-data tests and bounded cardinality.

## E6. Add selected-result memory commits

- **Problem solved:** ensemble/direct cannot participate in projects without contaminating context with losing candidates.
- **Evidence:** the service explicitly rejects `project_id` for ensemble/direct until selected-result-only memory exists.
- **Change:** create a typed memory commit referencing execution ID, selected artifact hash, summary, decisions, and provenance. Losing candidates remain audit artifacts, not project memory.
- **Cost/risk:** moderate; summaries can be wrong and memory can grow without compaction.
- **Phase:** **experiment during alpha**.
- **Test:** repeated project runs do not include losing candidate content; memory can be rebuilt from referenced executions; compaction preserves required facts on a benchmark set.

## X1. Extract scheduler, identity, discovery, validator, and provenance interfaces

- **Problem solved:** preventing current central/local implementations from becoming implicit permanent protocol assumptions.
- **Evidence:** roadmap considers durable scheduling, public-key identity, federation, P2P transport, sandboxing, and signed provenance, but none are alpha requirements.
- **Change:** write narrow behavioral interfaces and ADRs without multiple implementations. Do not add plugin frameworks or dynamic loading unless a second implementation is being tested.
- **Cost/risk:** low/moderate design cost; speculative abstraction can be worse than coupling.
- **Phase:** **design an extension point only**.
- **Test:** one fake/in-memory and one current production adapter satisfy contract tests; interface contains no marketplace- or DHT-specific concepts.

## X2. Define an A2A edge mapping

- **Problem solved:** future interoperation with external agents and coordinators without exposing internal worker mechanics.
- **Evidence:** Mycelium already has task, state, artifact, and streaming concepts; A2A standardizes adjacent public forms.
- **Change:** map Mycelium execution to A2A task, progress messages, artifacts, cancellation, and Agent Card skills. Put attempt settlement and assurance detail in namespaced extensions.
- **Cost/risk:** moderate schema mismatch and version churn; exposing internal IDs can create security coupling.
- **Phase:** **design an extension point only**.
- **Test:** round-trip a bounded task without losing lifecycle, cancellation, artifact, or assurance distinctions; reject unsupported semantics explicitly.

## X3. Define a provenance envelope

- **Problem solved:** future attribution and auditability beyond host-local hashes.
- **Evidence:** manifests establish local integrity but are unsigned and not externally anchored.
- **Change:** record request hash, execution/unit/attempt, artifact hashes, executor/model identifier, node enrollment identity, validator versions/evidence hashes, timestamps, and coordinator settlement ID. Signing is optional until identities stabilize.
- **Cost/risk:** moderate schema/privacy and retention concerns; provenance can expose model/operator metadata.
- **Phase:** **design an extension point only**; signing is **post-alpha**.
- **Test:** reproduce the envelope from durable records; tampering is detected; redaction policy preserves public shares without leaking operator details.

## P1. General durable workflow resume

- **Problem solved:** long DAGs lost to coordinator restart.
- **Evidence:** process-local scheduler and pipeline state; no proof that restart loss dominates current failures or costs.
- **Cost/risk:** very high determinism, side-effect, migration, and debugging cost.
- **Phase:** **post-alpha**, after the bounded candidate-queue experiment.
- **Test:** only proceed if alpha data shows material lost compute/user harm; require crash-boundary conformance and deterministic side-effect wrappers.

## N1. Permissionless identity, reputation, payment, discovery, and proof

- **Problem solved:** open participation among mutually untrusted parties.
- **Evidence:** current trust boundary is invited operators and a trusted coordinator; no market demand, stable identity, or general verification exists.
- **Cost/risk:** existential product and governance complexity.
- **Phase:** **permissionless-network phase**.
- **Test:** trigger only after independently operated nodes perform valuable verifiable work and contributors care about credits; threat model and economic simulations precede implementation.

---

# 9. Proposed architecture decision records

## ADR-0008: Canonical submission identity and idempotency

**Decision:** add requester-scoped idempotency keys bound to canonical request hashes and execution IDs. Define retention, conflict, and replay semantics across REST, CLI, MCP, and legacy adapters.

## ADR-0009: Durable terminal commit precedes terminal publication

**Decision:** no terminal event, callback, share, or successful response may become authoritative until durable state can reproduce the same result. Choose direct transaction or durable completion-outbox semantics.

## ADR-0010: Enrollment identity is distinct from session incarnation

**Decision:** node enrollment provides stable revocable attribution; a server-issued session identifies one live incarnation. Attempt authority binds both. Neither alone is described as Sybil resistance.

## ADR-0011: Scheduler backend contract and durability levels

**Decision:** define levels such as process-local, durable-unit, and resumable-workflow. The API reports the selected durability level. The current backend remains process-local; an ensemble-candidate backend is the first experiment.

## ADR-0012: Versioned capability and resource descriptors

**Decision:** separate claimed static capability, observed benchmark evidence, and policy-derived trust. Unknown fields remain forward-compatible; routing requirements distinguish required and preferred.

## ADR-0013: Assurance evidence is task-class specific

**Decision:** no validator or replication mechanism grants general correctness. Each task class defines accepted evidence, assurance ceiling, replication policy, and adjudication. Contribution points do not automatically become quality reputation.

## ADR-0014: Validator execution is a replaceable isolation boundary

**Decision:** validators receive bounded inputs and return bounded evidence through an executor interface. In-process execution remains allowed only for explicitly trusted structural validators; executable or complex validation uses an isolated backend.

## ADR-0015: Artifact provenance envelope

**Decision:** sealed manifests remain the integrity primitive; an additive provenance envelope binds them to execution, attempt, node enrollment, executor/model, and validator evidence. Signature and transparency backends are optional future adapters.

## ADR-0016: A2A is an edge adapter, MCP is a requester/tool adapter

**Decision:** the internal protocol remains optimized for attempt authority and trusted-coordinator settlement. A2A and MCP map into it without replacing those semantics.

## ADR-0017: Shared context commits only selected, attributable state

**Decision:** project memory stores typed commits referencing accepted executions/artifacts. Losing candidates, raw hidden reasoning, and unreviewed self-modification do not enter shared memory by default.

---

# 10. Prioritized experiment plan

## Priority 0: terminal durability fault campaign

**Hypothesis:** the service can guarantee that every client-visible terminal result is reproducible after restart.  
**Method:** inject database busy, disk-full, permission, commit failure, process kill, and artifact-seal failure at every boundary from strategy completion through event emission.  
**Success:** no contradictory terminal state; no double credit; exact recovery behavior documented.  
**Stop condition:** any adapter bypasses the canonical commit gate.

## Priority 1: idempotent submission race and crash campaign

**Hypothesis:** requester retries create one execution and one eventual result.  
**Method:** parallel submissions, proxy retries, timeout after server commit, restart after key reservation, payload mismatch, key expiration.  
**Success:** one execution for same key/hash, conflict for mismatch, bounded durable index.  
**Stop condition:** scoping cannot prevent cross-requester collisions or information leakage.

## Priority 2: trusted-alpha dress rehearsal

**Hypothesis:** documented operator controls work on the real deployment path.  
**Method:** clean deployment, reverse proxy/private overlay, two external nodes, live model calls, drain/rejoin, credential revoke/rotate, coordinator restart, backup/restore, artifact corruption, cancellation, output-budget abuse.  
**Success:** repeatable evidence bundle and no secret/prompt exposure.  
**Stop condition:** release depends on undocumented manual repair.

## Priority 3: node enrollment pilot

**Hypothesis:** per-node revocable credentials materially improve incident containment without harming one-command onboarding.  
**Method:** invitation token creates a node credential; server stores a digest; session registration proves possession; revoke and rotate one node.  
**Success:** independent revocation and stable attribution with low operator failure rate.  
**Stop condition:** onboarding complexity is disproportionate for invited alpha; fall back to scoped random credentials before public keys.

## Priority 4: durable ensemble-candidate queue spike

**Hypothesis:** a narrow durable-unit scheduler recovers useful work with manageable complexity.  
**Method:** persist ready/leased/accepted/materialized candidate states; crash repeatedly; compare against interrupt/resubmit.  
**Success:** exactly one candidate materialization and winner selection; measurable compute saved.  
**Stop condition:** recovery complexity or artifact cleanup exceeds saved compute at alpha scale.

## Priority 5: task-class verification benchmark

**Hypothesis:** deterministic layered evidence beats either single-result acceptance or unconditional replication.  
**Method:** benchmark structured extraction, patch-with-tests, and deterministic transform with planted failures and malicious outputs.  
**Success:** improved false-accept rate at an acceptable cost/latency increase, with calibrated assurance labels.  
**Stop condition:** no meaningful improvement over human review or deterministic tests alone.

## Priority 6: validator isolation benchmark

**Hypothesis:** common validators can run in a constrained executor with acceptable compatibility and overhead.  
**Method:** run safe and malicious fixtures through subprocess/container/WASM candidates.  
**Success:** no host secret access, bounded resources, coordinator survival, acceptable startup cost.  
**Stop condition:** sandbox cannot support the target validator class or produces false confidence.

## Priority 7: measured capability routing

**Hypothesis:** observed node/model evidence predicts deadline and validation success better than tags.  
**Method:** collect task-class outcomes, throughput, first-token latency, memory failures, context canaries, and churn.  
**Success:** held-out routing improvement and interpretable evidence.  
**Stop condition:** sample size is too low or benchmarks impose unacceptable operator cost.

## Priority 8: OpenTelemetry incident-diagnosis pilot

**Hypothesis:** standard traces reduce diagnosis time for reassignments, timeouts, and persistence faults.  
**Method:** instrument one end-to-end path and run known failure scenarios.  
**Success:** faster root-cause localization with no prompt/secret leakage and bounded cardinality.  
**Stop condition:** maintenance overhead exceeds operational value at current scale.

## Priority 9: selected-result project memory

**Hypothesis:** typed selected-result commits improve continuity without contaminating context.  
**Method:** compare DAG and ensemble project iterations with provenance-linked memory, compaction, and rebuild.  
**Success:** higher task continuity and no losing-candidate leakage on a fixed evaluation set.  
**Stop condition:** summaries become an unverified source of compounding error.

---

# 11. Product and marketing differentiation

## Defensible current positioning

**Mycelium is an auditable local-AI execution layer for coordinating work across machines whose operators you deliberately trust.**

A stronger expanded formulation:

> Run structured multi-agent work across your own and invited machines, with explicit prompt-disclosure controls, attempt-bound remote results, durable settlement records, and artifact integrity that distinguishes completion from assurance.

## Primary early user

The best initial customer/user is not a permissionless marketplace participant. It is a lab, classroom, open-source team, small research group, or technical collective with:

- several underused machines;
- local-model/privacy preferences;
- operators who know one another or can be enrolled deliberately;
- asynchronous bounded tasks;
- outputs that can be checked mechanically or reviewed;
- a need to see who ran what, where, under which contract.

## Differentiation by competitor class

### Versus Temporal, Restate, and DBOS

They are general durable execution platforms. Mycelium's differentiation is agent strategy plus contributor-node authority, local model placement, assurance metadata, and artifact settlement. Mycelium should not claim stronger durability.

### Versus LangGraph and Microsoft Agent Framework

They orchestrate agent graphs and memory. Mycelium adds multi-machine contributor execution, explicit remote consent, worker attempt authority, and local artifact settlement. They are stronger in workflow checkpointing, integrations, and standard observability.

### Versus BOINC

BOINC is mature volunteer batch compute with application-specific validation. Mycelium handles generative tasks, typed artifacts, agent strategies, and interactive operator-visible execution. BOINC is much stronger in volunteer-scale operations and validation history.

### Versus Golem, Akash, and Bacalhau

They emphasize markets, deployable compute jobs, data/executor plugins, and provider operations. Mycelium's wedge is cooperative local-model work without token economics, with human/agent artifacts and explicit assurance. It should not imply equivalent isolation, markets, or provider maturity.

### Versus Petals and Hivemind

They distribute model/training computation. Mycelium distributes task-level work and whole candidates, which is better suited to intermittent consumer nodes and auditable artifacts.

### Versus SwarmHarness

SwarmHarness is a useful conceptual paper for the decentralized future. Mycelium is the implemented trusted control plane today. The accurate contrast is not "paper versus complete decentralized network"; it is "working trusted-alpha coordinator with durable attempt authority versus a proposed permissionless/federated architecture whose ledger, Byzantine, and implementation questions remain open."

## Claims to use

- local-first and contributor-hardware aware;
- explicit remote consent and confidentiality classes;
- typed execution and output contracts;
- authoritative leased attempts with exact retry settlement;
- truthful lifecycle and assurance separation;
- sealed, role-scoped artifact integrity;
- open, forkable, no token required;
- designed for a small private trusted alpha.

## Claims to avoid

- trustless;
- permissionless;
- decentralized scheduler;
- verified AI inference;
- correctness guaranteed by multiple agents;
- secure arbitrary-code execution;
- durable workflow resumption;
- mature reputation or incentive economy;
- production high availability;
- privacy from assigned worker operators.

## Suggested category name

**Trusted compute guild orchestration** or **auditable cooperative AI execution** is more accurate than a decentralized compute marketplace. "Guild" can remain the social vision, while "trusted alpha" states the security boundary.

---

# 12. Maximum recommended implementation themes

## Theme 1: Durable execution truth

Scope:

- idempotent submission;
- fail-closed or recoverable terminal publication;
- crash/fault state-machine tests;
- production-like release evidence.

Why now: this fixes the only finding that can make an already returned success disappear from durable history. It protects the auditability claim without adopting a workflow platform.

## Theme 2: Enrolled trust and typed capability evidence

Scope:

- per-node revocable enrollment credential;
- session remains ephemeral incarnation;
- versioned resource/capability schema;
- measured capability evidence pilot.

Why now: this supports an invited multi-operator alpha while staying honest that it is not permissionless identity or attestation.

## Theme 3: Assurance and execution-safety ladder

Scope:

- task-class verification experiments;
- durable verification evidence;
- validator executor isolation pilot;
- provenance envelope interface.

Why now: Mycelium's product value depends more on trustworthy selection and artifacts than on adding agent personas or strategies.

## Theme 4: Evolution seams, not distributed-system expansion

Scope:

- scheduler, identity, discovery, validator, telemetry, and provenance contracts;
- A2A edge mapping;
- OpenTelemetry pilot;
- durable ensemble-candidate queue spike only.

Why now: it keeps future options open without importing DHT, market, federation, or general workflow complexity before evidence.

---

# Final prioritization

## Adopt before alpha

1. Terminal publication must be durably reproducible.
2. Submission must be idempotent.
3. Core lifecycle/attempt invariants need fault and property-based tests.
4. Run the real deployment path with external nodes, live models, TLS/private overlay, restart, revoke, backup, and restore.
5. Add a minimal structured capability/resource version and, before widening invited participation, per-node revocable enrollment.

## Experiment during alpha

1. Durable ensemble/direct unit queue, not general workflow replay.
2. Task-class verification and adaptive replication.
3. Validator isolation.
4. Measured capability routing.
5. OpenTelemetry.
6. Selected-result memory commits.

## Design an extension point only

1. Scheduler backend.
2. Identity/enrollment provider.
3. Discovery/transport provider.
4. Validator executor and provenance signer.
5. A2A adapter.
6. Accounting/payment/reputation policy.

## Post-alpha

1. General resumable workflows if restart-loss evidence justifies them.
2. Signed provenance and release attestations.
3. Hardened arbitrary-code execution service.
4. Rich shared-context governance and compaction.
5. HA coordinator or federation after one coordinator is operationally proven.

## Permissionless-network phase

1. DHT/NAT/peer discovery.
2. Sybil resistance and decentralized identity.
3. spendable credits, payment, escrow, disputes, or staking;
4. open reputation and market routing;
5. TEE/ZK/quorum verification for selected workloads;
6. coordinator federation or replacement.

## Reject

1. Blockchain/token architecture.
2. Local spendable ledgers without a consistency protocol.
3. Universal agreement-based correctness.
4. A single global reputation score.
5. LLM judge as correctness proof.
6. automatic generated-code execution.
7. a Temporal/LangGraph/Agent-Framework rewrite.
8. DHT scheduling during trusted alpha.
9. global mutable prompt memory.
10. permissionless, trustless, or verified-compute marketing today.

---

# Source key

The audit used the following primary-source groups:

- **Mycelium:** repository source at immutable RC1 checkpoint, protocol, architecture, threat model, operations/runbooks, roadmap, ADRs, tests and release evidence.
- **SwarmHarness:** arXiv paper 2605.28764 and author/project source search.
- **HarnessAPI:** arXiv paper 2605.22733, official repository, release and source tree.
- **Durable execution:** Temporal official documentation/repository; Restate official documentation/repository; DBOS official documentation/repository.
- **Volunteer compute:** BOINC official repository and validator, replication, adaptive replication, and credit documentation.
- **Compute markets/execution:** Golem, Akash, and Bacalhau official documentation and repositories.
- **P2P inference/discovery:** Petals, Hivemind, and libp2p official repositories/documentation.
- **Agent orchestration/context:** LangGraph, Microsoft Agent Framework, AutoGen, and Letta official repositories/documentation.
- **Protocols:** A2A and MCP official specifications and repositories.
- **Provenance/security:** SLSA, in-toto, Sigstore, gVisor, Firecracker, RISC Zero, and OpenTelemetry official specifications, documentation, and repositories.
