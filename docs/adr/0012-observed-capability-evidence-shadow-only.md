# ADR 0012: Observed capability evidence is durable and shadow-only

- Status: Accepted
- Date: 2026-08-26
- Decision scope: worker-attributed operational observations, exact-scope
  aggregation, sampled agreement, contract-floor assurance, and hypothetical
  placement evaluation

## Context

[ADR 0011](0011-node-capabilities-versioned-claims.md) introduced strict,
versioned node capability descriptors and one deterministic hard-requirement
matcher. A descriptor is still a statement made by a worker. It is useful for
eligibility, but it does not establish that the machine has the advertised
resources, that a model will meet a deadline, or that an answer will be
correct.

The coordinator does observe some narrower facts after assignment: whether a
bound attempt settled before its lease, how long the coordinator waited, how
many output bytes were accepted, whether a candidate passed a required
structural contract floor, and whether two sampled outputs had a comparable
shape. These facts can help operators evaluate a future placement policy only
if identity, attribution, replay, privacy, and cold-start behavior are explicit.

The relevant concepts are deliberately separate:

```text
claim         a node-advertised capability descriptor
observation   a coordinator-recorded operational outcome for one bound attempt
agreement     a paired, coarse output-shape comparison; neither output is a truth label
assurance     a task-specific structural contract-floor result
contribution  accounting credit for accepted compute
attestation   independently established hardware or model identity; not implemented
reputation    a global judgment about a node; not implemented
```

The former sampled-verification pool once exposed process-local routing weights
that could defer a worker's first refusal. That behavior is incompatible with a
shadow-only experiment. The compatibility hook now always returns false, and
sampled agreement neither excludes nor orders workers.

This ADR supersedes ADR 0011 only where that earlier decision describes the
optional sampled-verification routing weight and first-refusal deferral. ADR
0011 remains authoritative for capability claims, snapshots, hard requirements,
matching, and attempt binding.

## Decision

### Exact durable scopes

An observation is usable only when the authoritative attempt contains all of
the following:

- enrollment ID;
- capability descriptor version and digest;
- executor kind, nullable version, and worker protocol taken from the validated
  immutable descriptor snapshot;
- exact selected model provider, name, nullable digest, and snapshot variant;
- execution ID and execution-unit ID;
- a server task class of exactly `candidate` or `dag_subtask`; and
- an evidence role of exactly `production` or `sampled_comparison`.

The evidence store joins the enrollment and immutable descriptor snapshot,
validates the snapshot's canonical JSON, version, and digest, checks the
enrollment's node label, and verifies that the attempt-bound model is exactly
one advertised model. Historical, legacy, corrupt, or incomplete attempts are
excluded. The coordinator never guesses missing identity or copies evidence
from a similar attempt.

The aggregation key is the complete tuple of enrollment, descriptor,
executor, model, task class, and evidence role. Execution and unit IDs remain
on each observation for attribution and replay, but are not aggregation
dimensions. Observation types remain separate metrics within that scope.

A descriptor change, executor change, selected model change, task-class change,
evidence-role change, or new enrollment therefore starts a new evidence scope.
In particular, evidence for an earlier descriptor, model, or task class cannot
bootstrap a changed scope.

### Recorded observations

`node_capability_observations` is append-only and bounded. It records only the
following typed facts:

- explicit accepted-settlement outcome: output, worker-reported error, or empty
  output;
- non-empty output settled before the server-issued lease deadline; a timely
  worker error or empty output remains a deadline-completion failure;
- coordinator wall time from attempt issuance to accepted settlement;
- accepted UTF-8 output byte count and effective output bytes per coordinator
  second;
- explicit worker-attributable terminal outcome: `lease_expired` or
  `node_stale`;
- post-terminal required contract-floor pass or fail; and
- paired sampled output-shape agreement or disagreement.

Output byte rate is not token throughput and coordinator wall time is not a
model-only benchmark. Both include the coordinator-visible execution path and
must be labelled accordingly.

The contract-floor projection is candidate-local. It is recorded only when all
required floor checks actually ran and returned `passed` or `failed`. Skipped
or errored validation is not converted into failure. A final assembled DAG
result is not attributed back to one builder unless that builder candidate has
its own required floor result.

Sampled comparison creates one paired observation for the production attempt
and one for the `sampled_comparison` attempt. It records a versioned comparison
method and pair digest, not either output. The sampled attempt durably names its
exact primary attempt, and issuance plus observation insertion reject a missing
or inconsistent binding; sharing only an execution ID and task class is not
enough. Agreement means only comparable shape. It is not semantic equivalence,
correctness, or trust, and it is not an input dimension in the shadow
preference policy.

### Explicit fault attribution

Fault attribution uses the server-owned `terminal_cause` enum. It never parses
free-form reason or error text.

Accepted settlements use only `settled_output`, `settled_worker_error`, or
`settled_empty_output`. Terminal availability failures use only
`lease_expired` and `node_stale`. The current evidence policy excludes payload
and stream limits, caller cancellation, execution deadline, receipt-binding
failure, enrollment reclamation, session replacement, coordinator restart,
supersession, and every unknown cause. Exclusion is not exoneration or blame;
it means this schema does not have a sufficiently narrow attribution rule for
that event.

### Append-only replay and fail-open projection

Observation IDs are deterministic, domain-separated SHA-256 digests over the
attempt, observation type, and stable subject key. Shadow-decision IDs and
sample-pair IDs use separate domains. An exact duplicate is idempotent and
returns the existing row. Reusing the deterministic ID for different immutable
content raises a conflict. Database triggers reject update and delete of both
observations and shadow decisions.

The corrected deadline-success definition uses the versioned subject key
`nonempty_output_before_lease_v2`. Earlier `lifecycle` deadline rows remain
append-only history, are excluded from current aggregates, and are backfilled
under the versioned key by bounded startup reconciliation. This evolves the
meaning without rewriting a row or reusing its deterministic ID.

Initial settlement projection runs inside the authoritative attempt transaction
under a SQLite savepoint. If optional evidence validation or insertion fails,
only that savepoint is rolled back; attempt settlement, accepted receipt, and
contribution accounting can still commit. Terminal and comparison projections
are best effort. Startup selects only attributable attempts missing one of
their expected deterministic observations, so completed or non-attributable
rows cannot consume the bounded batch.

Candidate-local contract-floor observations and a content-free projection
receipt are committed together in one optional transaction after terminal
execution persistence. A failed transaction leaves no receipt. Startup selects
only terminal executions without that receipt and retries the same bounded
projection; successful and no-op projections then fall out of later batches.
Deterministic observation IDs and the append-only receipt make every retry safe
without weakening terminal execution truth.

Evidence loss is diagnosable but cannot invalidate accepted lifecycle truth or
fail production work. Conversely, evidence rows never become lifecycle
authority.

### Exact-scope aggregation and cold start

Every binary aggregate returns positive, negative, and total sample counts,
the observed rate, and a 95% Wilson interval. The store reports separate
aggregates for deadline completion, contract-floor assurance, and sampled
agreement. It reports accepted-settlement categories and explicit lease or
disconnect counts separately.

Latency and effective byte throughput use a bounded recent window and expose
both the full scoped sample count and the recent median. The API does not
compute a single quality, trust, reputation, or routing score.

`capability_evidence_min_samples` defaults to 5 and is bounded by configuration.
Below the configured minimum, an aggregate is explicitly
`insufficient_evidence`; it is not assigned a pessimistic or optimistic
default. Changing descriptor, model, task class, role, or enrollment produces a
new cold scope even when an earlier scope had many observations.

The default of 5 is a diagnostic policy parameter, not a sufficient threshold
for active routing. The experiment below requires at least 20 scoped samples
per metric before any future promotion can be considered.

### Shadow-only evaluation

Configuration accepts only `off` and `shadow`.
`capability_evidence_mode="off"` is the default. There is no `active` mode and
no hidden fallback that applies a shadow result to production placement.

The real task handout and durable attempt are fixed before shadow work is
scheduled. The coordinator then freezes the exact hard-matched descriptor and
selected-model scopes for the bounded candidate set. Background work consumes
that immutable assignment-time snapshot rather than rereading live node,
session, draining, or blacklist state under the earlier decision timestamp.
The bounded snapshot reads claims only and never waits for evidence. Shadow
aggregation runs asynchronously in a bounded background set. Queue
saturation, missing scope, database failure, evaluator failure, or no running
event loop causes the diagnostic to be skipped without failing or changing the
handout.

Candidates originate from the task's already-hard-eligible set and are checked
again with the ADR 0011 matcher and exact selected-model logic. A shadow
candidate cannot make a hard-ineligible worker eligible. The evaluator reads
only observations recorded at or before the assignment timestamp, preventing
the current attempt's later outcome from leaking into its own hypothetical
decision.

The version-1 policy is conservative and lexicographic rather than a global
scalar:

1. every candidate needs the minimum deadline sample count;
2. if any candidate has contract-floor evidence, every candidate needs the
   minimum for that metric;
3. the same all-candidates rule applies before latency or throughput is used;
4. compare deadline-completion Wilson lower bounds;
5. then compare contract-floor Wilson lower bounds when available;
6. then prefer lower recent median coordinator wall time;
7. then prefer higher recent median effective output bytes per second; and
8. retain the actual assignment on an exact tie; an ambiguous best set that
   excludes the actual assignment produces no preference.

One candidate, insufficient evidence, or an ambiguous alternative produces
`no_preference`. The actual assignment remains unchanged for `same`,
`different`, and `no_preference`. Durable shadow decisions record only the
actual scope, candidate-set digest, policy version, hypothetical outcome, and
bounded reason code.

`verify_rate` remains a separate, default-zero control for the costly duplicate
inference used to produce sampled agreement. Enabling shadow mode does not
enable duplicate work, and enabling sampling does not change routing.

### Operator and privacy boundary

The viewer-protected `GET /v1/operator/capability-evidence` returns bounded,
filtered exact-scope aggregates and grouped shadow-decision counts. Production
role is the default, the result limit is capped at 200, and responses state
`affects_routing: false`. Public status, event, and metrics surfaces do not
receive per-scope evidence.

Observation rows contain no prompt, output body, worker error, free-form reason,
credential, nonce, session secret, or arbitrary telemetry. Metadata keys are
allowlisted by observation type and size-bounded. The protected aggregate
reader does not expose raw observations or metadata. Enrollment, node label,
descriptor digest, and model identity remain operator data rather than public
reputation claims.

### Contribution accounting remains separate

Contribution points are awarded for accepted bound compute under their existing
accounting rule. They are not an evidence metric, assurance result, preference
dimension, or correctness claim. Evidence failure does not reverse
contribution, and contribution totals do not influence the shadow evaluator.

## Consequences

- Operators can inspect durable operational observations without treating node
  claims as measurements.
- Replay and coordinator restart do not duplicate observations.
- A changed descriptor, model, or task class cannot inherit favorable history.
- Cold-start nodes are not penalized by a guessed score; the evaluator declines
  to express a preference.
- Sampled agreement is preserved as a diagnostic without becoming correctness
  or routing weight.
- Optional evidence can remain incomplete after repeated storage failures, but
  missing attempt and contract-floor projections remain eligible for bounded
  startup repair because successful projections no longer consume the batch.
- Exact scoping limits statistical power and increases cold starts, but avoids
  unsupported transfer between materially different execution contexts.
- The existing operational circuit breaker remains separate. This evidence
  subsystem neither mutates its counters nor replaces its explicit safety role.

## Why active routing is deferred

The current evaluation fixture has only ten synthetic cases. It verifies
determinism and boundary behavior, not real-world predictive validity. One case
deliberately demonstrates distribution shift in which the historical
preference loses while the claim-only assignment succeeds. Live observations
will also be affected by routing selection, task mix, model stochasticity,
contract availability, correlated retries, censored failures, network and
coordinator overhead, descriptor churn, and optional duplicate sampling.

No live delayed-window study yet meets the minimum sample, divergence,
non-interference, improvement, or fairness thresholds in
[the shadow experiment](../experiments/capability-evidence-shadow.md).
Activating evidence would
create feedback loops: preferred scopes would receive more work and therefore
more evidence, while new scopes would receive less opportunity to recover.

Any active mode requires a separate ADR, explicit operator configuration,
measured live results, a migration and rollback plan, and renewed threat and
fairness review. It cannot be introduced by changing the meaning of `shadow` or
by reviving first-refusal deferral.

## Rejected alternatives

### Aggregate by node label or enrollment alone

Rejected because a mutable label, changed descriptor, different selected
model, or different task class can have materially different behavior.

### Treat sampled agreement as correctness

Rejected because a pair can agree on two wrong answers or disagree when either
answer is acceptable. The comparison has no external truth label.

### Produce one reputation or routing score

Rejected because availability, structural assurance, latency, throughput, and
agreement have different meanings and sample populations. A scalar would hide
those distinctions and encourage unsupported global claims.

### Penalize missing evidence

Rejected because it creates a cold-start barrier and would turn coordinator
observation coverage into a negative judgment about a new or changed scope.

### Fail settlement when evidence persistence fails

Rejected because optional diagnostic storage must not overturn authoritative
attempt and receipt truth or deny contribution for accepted compute.

### Enable active routing after the synthetic fixture passes

Rejected because fixture success demonstrates implementation invariants only.
It is not representative evidence of future task outcomes.
