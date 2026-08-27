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

The selected model digest remains nullable because the runtime may not provide
one. A digestless typed scope is still exact relative to the immutable
descriptor claim and remains eligible for observation and shadow evaluation.
It is not, however, eligible to be promoted into any future active experiment:
provider and model name alone are not immutable model identity.

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
scheduled. The coordinator freezes a bounded set of non-secret assignment-time
claim inputs rather than retaining credentials or rereading later live node,
session, draining, or blacklist state under the earlier decision timestamp.
Canonical hard rematching and exact descriptor/selected-model scope construction
then run from that immutable snapshot in bounded background work, outside the
production queue lock. Handout does not wait for matching, scope capture, or
evidence aggregation. The evaluator uses only observations at or before the
assignment cutoff. Queue
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
bounded reason code. They live in the optional sibling
`capability-shadow-health.db`, not authoritative `events.db`, so a bounded
shutdown may abandon a running best-effort writer without retaining write access
to attempt or settlement authority. Pre-isolation decision rows are copied
forward idempotently on startup and remain append-only in their legacy source.

`verify_rate` remains a separate, default-zero control for the costly duplicate
inference used to produce sampled agreement. Enabling shadow mode does not
enable duplicate work, and enabling sampling does not change routing.

### Shadow operational health accounting

The coordinator records bounded operational health for the optional shadow
pipeline separately from node observations and hypothetical decisions. An
admission record has exactly one of these outcomes:

```text
disabled
not_applicable
queue_saturated
scope_capture_failed
scheduled
```

An admitted background evaluation has at most one terminal outcome:

```text
completed
evaluator_failed
decision_write_failed
cancelled_on_shutdown
```

Outcome-to-reason classification is fixed and content-free:

| Phase/outcome | Allowed reason code |
| --- | --- |
| admission / `disabled` | `mode_disabled` |
| admission / `not_applicable` | `legacy_descriptor_identity`, `nonproduction_attempt`, or `unsupported_task_class` |
| admission / `queue_saturated` | `background_queue_limit_reached` |
| admission / `scope_capture_failed` | `scope_capture_failed` or `coordinator_shutdown_during_scope_capture` |
| admission / `scheduled` | `evaluation_scheduled` |
| evaluation / `completed` | `decision_persisted` |
| evaluation / `evaluator_failed` | `evaluator_failed` |
| evaluation / `decision_write_failed` | `decision_write_failed` |
| evaluation / `cancelled_on_shutdown` | `coordinator_shutdown` |

The durable append-only `capability_shadow_operational_events` record lives
alongside those decisions in sibling `capability-shadow-health.db`, not
authoritative `events.db`, and
contains only `event_id`, `attempt_id`, `phase`, `outcome`, bounded `reason_code`, and
`occurred_at`. Event IDs are deterministic per attempt and phase, and the schema
also has a unique `(attempt_id, phase)` constraint, so an exact replay is
idempotent and conflicting reuse is rejected. Update/delete triggers preserve
append-only history. Existing state directories create the empty table through
the health store's own idempotent schema path; no historical task or evidence
backfill is inferred. The separate database gives best-effort telemetry an
independent SQLite writer-lock domain, so it cannot contend with authoritative
attempt, assignment, or settlement writes.

Operational records, new log/metric fields, and protected responses never
contain a prompt, output body, worker error text, credential, session token,
attempt nonce, artifact content, or arbitrary exception message. They describe
experiment-pipeline health, not worker behavior, reputation, trust, or
correctness.

Successful health-record writes are durable. A failure of that store cannot
reliably record itself, so process-lifetime counters separately retain
`durable_health_record_write_failure`, `unexpected_containment_failure`, and
`background_task_callback_failure`, together with their process `reset_at`
timestamp. Those counters reset on process start and are not reconstructed from
SQLite. The failure-recording path never recursively records its own failure.
Backup format v2 includes both `events.db` and
`capability-shadow-health.db`. Restore accepts legacy format v1 without the
health database; health state is optional only for pre-feature state.
Process-local fallback counters remain outside both databases.

All operational accounting is best effort. Neither a record nor failure to
write one may alter candidate eligibility, selected node, queue or handout,
attempt creation, settlement, contribution, or execution outcome. Admission and
evaluation remain bounded background work; production does not wait for this
telemetry.

Graceful shutdown requests stop new shadow admissions, immediately cancel
ordinary evaluator work, and allow scope capture or an already-running decision
write a finite drain interval. A capture that exceeds the interval is classified
as `scope_capture_failed` with
`coordinator_shutdown_during_scope_capture`. A decision write that exceeds the
interval is no longer awaited by the coordinator; its bounded worker-thread
operation records `completed` or `decision_write_failed` when it returns, so a
committed decision is never mislabeled as cancellation. A drain overrun also
increments the process-local containment counter. None of these outcomes blame
the node.

Background evidence aggregation uses an already-initialized SQLite connection
opened with `mode=ro` and `query_only`; it performs no schema migration or WAL
configuration. A timed-out daemon may therefore finish a bounded read after the
coordinator lock is released, but it cannot mutate or acquire writer authority
over `events.db`. All optional shadow writes target only the isolated health
database.

For a selected admission cohort, the protected report derives:

```text
orphan_evaluation_total = evaluation rows with no persisted admission row
assignment_observation_total = all admission outcomes + orphan_evaluation_total
scheduled = scheduled admissions + orphan_evaluation_total
offered = scheduled + queue_saturated + scope_capture_failed
skipped = disabled + not_applicable
failed = queue_saturated + scope_capture_failed + evaluator_failed
         + decision_write_failed + cancelled_on_shutdown
pending = max(0, scheduled - completed - evaluator_failed
                 - decision_write_failed - cancelled_on_shutdown)
drop/failure numerator = failed
drop/failure denominator = offered
drop/failure rate = numerator / denominator
```

The rate is null when the denominator is zero. A durable evaluation may exist
without its admission row when the earlier best-effort write failed. The report
exposes such rows as `orphan_evaluation_total` and counts each as an inferred
scheduled/offered observation, ensuring a persisted terminal failure cannot
disappear or silently shrink the denominator. Optional time windows select an
inclusive admission-time cohort and include terminal evaluation rows for those
same attempts even when evaluation finishes after the window end; orphan rows
are selected by their evaluation timestamp. Counts and the
numerator/denominator therefore reproduce the reported rate.

### Future active-experiment identity prerequisite

Each exact evidence scope receives a derived, bounded diagnostic containing
`eligible_for_future_active_experiment` and ordered `blocking_reasons`. At
minimum, `immutable_model_identity_missing` blocks a nullable model digest. The
complete bounded reasons are `legacy_descriptor_identity`,
`descriptor_identity_unreconstructable`, `immutable_model_identity_missing`,
and `model_identity_unreconstructable`. This diagnostic is computed from
identity already present in the scope and immutable snapshot. It is not stored
as lifecycle or routing authority.

The diagnostic does not change current hard eligibility, actual assignment,
shadow candidate membership, or hypothetical preference. Shadow observations
continue for digestless typed scopes and any other scope that the existing
evidence resolver can safely reconstruct. Existing exclusion of legacy or
incomplete evidence scopes is separate from this diagnostic. A true identity
diagnostic is only one necessary prerequisite, not a promotion decision,
quality label, attestation, trust flag, or reputation score.

### Operator and privacy boundary

The viewer-protected `GET /v1/operator/capability-evidence` returns bounded,
filtered exact-scope aggregates, grouped shadow-decision counts, future-active
identity diagnostics, and shadow operational-health totals. Production role is
the default, the result limit is capped at 200, and responses state
`affects_routing: false`. Operational reporting includes durable counts by phase
and outcome, offered/scheduled/completed/skipped/failed/pending totals, explicit
drop/failure numerator and denominator, latest event time, optional bounded time
windows, and process-local fallback counters with their reset timestamp. Public
status, event, and metrics surfaces do not receive per-scope evidence or shadow
health details.

Observation rows contain no prompt, output body, worker error, free-form reason,
credential, nonce, session secret, or arbitrary telemetry. Metadata keys are
allowlisted by observation type and size-bounded. The protected aggregate
reader does not expose raw observations, operational records, or metadata.
Enrollment, node label, descriptor digest, and model identity remain operator
data rather than public reputation claims. The response explicitly labels the
new counts as operational experiment health, not node reputation.

### Contribution accounting remains separate

Contribution points are awarded for accepted bound compute under their existing
accounting rule. They are not an evidence metric, assurance result, preference
dimension, or correctness claim. Evidence failure does not reverse
contribution, and contribution totals do not influence the shadow evaluator.

## Consequences

- Operators can inspect durable operational observations without treating node
  claims as measurements.
- Operators can distinguish shadow admission drops, evaluator failures, durable
  decision-write failures, and shutdown cancellation without blaming a node or
  affecting production.
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

Any active experiment requires all four of the following before implementation:

1. immutable model and descriptor identity with no identity blocker;
2. every live volume, safety, predictive, and fairness threshold in the shadow
   experiment;
3. a separate accepted ADR; and
4. a separately reviewed implementation PR.

It also requires explicit operator configuration, a migration and rollback
plan, and renewed threat and fairness review. It cannot be introduced by
changing the meaning of `shadow` or by reviving first-refusal deferral. This ADR
does not introduce active evidence-based routing.

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
