# Capability evidence shadow experiment

- Status: synthetic baseline complete; live shadow evaluation pending
- Date: 2026-08-26
- Policy version: 1
- Fixture version: 1
- Decision authority: none; this experiment cannot alter production routing

## Hypothesis

Among candidates that already pass the same hard capability matcher, exact-scope
operational evidence may predict deadline completion and required structural
contract-floor outcomes better than retaining the claim-only assignment.

The narrower safety hypothesis is that a conservative evaluator can decline to
express a preference for cold or changed scopes, exclude hard-ineligible
candidates, ignore non-node-attributable failures, and remain deterministic
without affecting the real assignment.

Here, deadline success means that a non-empty output settled by its issued lease
deadline. A timely worker-reported error or empty output is a deadline failure;
its separate settlement category remains observable.

This experiment does not test semantic correctness, hardware attestation,
global node quality, or reputation. Sampled output-shape agreement is not a
truth label and is not a preference dimension.

## Deterministic fixture method

`tests/fixtures/capability_evidence_eval_v1.json` contains ten synthetic cases.
Each candidate declares:

- whether it passed hard eligibility;
- an exact enrollment, descriptor, selected model, and task-class scope;
- scoped deadline and contract-floor counts available before the decision;
- held-out binary deadline and contract-floor labels; and
- optional prior-scope evidence or explicitly excluded non-node causes.

The fixture uses `minimum_samples=5` and a fixed assignment timestamp of
`1700000000`. The script filters out hard-ineligible candidates, calls the same
pure `evaluate_shadow_preference` function used by the server, and calls it
again with reversed candidate order. Any order dependence or mismatch with the
fixture's expected outcome is an invariant failure.

The claim-only baseline is the fixture's actual assigned candidate. The
shadow-with-fallback policy uses the hypothetical preferred candidate when one
exists and retains the actual assignment on `no_preference`. Held-out labels
are read only after the hypothetical decision. The fixture is synthetic and
hand-authored; “held out” describes evaluation order, not independent sampling
from production traffic.

The cases cover:

1. sufficient deadline evidence;
2. a contract-floor tiebreak after equal deadline evidence;
3. cold-start no-preference behavior;
4. descriptor-change reset;
5. selected-model reset;
6. task-class reset;
7. resistance to a few failures through sample minimums and Wilson bounds;
8. exclusion of non-node-attributable causes;
9. filtering of a hard-ineligible candidate with attractive evidence; and
10. a deliberate held-out distribution-shift regression.

Each reset case supplies 20 prior deadline samples and 20 prior contract-floor
samples for the old scope. The evaluator ignores all 40. Across three reset
cases, 120 prior-scope samples are ignored.

The CLI returns zero even when shadow outcomes underperform so that negative
experimental results remain reportable. Malformed fixtures or violated
invariants return exit code 2.

## Commands run

The following baseline commands were recorded from the repository root on
2026-08-26:

```text
python scripts/capability_evidence_eval.py
python -m pytest tests/test_capability_evidence.py tests/test_capability_evidence_eval.py -q
```

At the baseline revision, the focused test command completed with:

```text
38 passed in 14.34s
```

The fixture SHA-256 was
`780c6317d7e41f8bbd9a7a2aec8b88c767c7f3609f6c90f8778e974421d51e24`.

## Exact current synthetic output

The Theme 2.1 branch reran `python scripts/capability_evidence_eval.py` after the
identity-diagnostic addition; it returned zero and emitted a deterministic
version-2 JSON report. Its policy metrics retained these exact summary
values:

| Metric | Claim-only actual | Shadow preference, evaluable cases | Shadow with claim fallback |
| --- | ---: | ---: | ---: |
| Cases | 10 | 6 | 10 |
| Deadline successes | 7 | 5 | 9 |
| Deadline success rate | 0.7 | 0.833333 | 0.9 |
| Contract-floor passes | 6 | 5 | 9 |
| Contract-floor pass rate | 0.6 | 0.833333 | 0.9 |

For the six preference-evaluable cases, the comparable claim-only baseline was
3/6 deadline successes (`0.5`) and 2/6 contract-floor passes (`0.333333`). The
shadow preference was 5/6 (`0.833333`) for both metrics. The complete ten-case
fallback delta was exactly +2 deadline successes and +3 contract-floor passes.

Other exact summary values were:

- shadow outcomes: 5 `different`, 4 `no_preference`, and 1 `same`;
- excluded non-node faults: 3;
- hard-ineligible candidates excluded: 1;
- prior-scope samples ignored after resets: 120; and
- invariant failures: 0.

The version-2 identity summary reported 20 eligible scopes, zero blocked scopes,
and an empty blocking-reason count for this exact-digest fixture.

The deterministic report also exposes, for each reconstructed scope,
`eligible_for_future_active_experiment` and bounded `blocking_reasons`. An exact
immutable model digest has no identity blocker. A missing digest must report
`immutable_model_identity_missing`; the complete bounded reasons are
`legacy_descriptor_identity`, `descriptor_identity_unreconstructable`,
`immutable_model_identity_missing`, and `model_identity_unreconstructable`.
This diagnostic does not change hard eligibility, the fixture's actual
assignment, or its shadow preference.

The per-scenario outcomes were:

| Scenario | Shadow outcome | Result in the fixture |
| --- | --- | --- |
| Sufficient evidence | `different` | improved both labels |
| Contract-floor tiebreak | `different` | improved contract floor; deadline unchanged |
| Cold start | `no_preference` | retained actual |
| Descriptor reset | `no_preference` | ignored 40 old-scope samples |
| Model reset | `no_preference` | ignored 40 old-scope samples |
| Task-class reset | `no_preference` | ignored 40 old-scope samples |
| Few failures | `different` | improved both labels |
| Excluded causes | `same` | excluded three non-node faults |
| Hard-ineligible filter | `different` | excluded the attractive ineligible candidate |
| Distribution shift | `different` | regressed both labels |

The distribution-shift case is important: claim-only passed both held-out
labels while the historically preferred candidate failed both. The aggregate
synthetic improvement therefore must not be quoted as a guaranteed routing
gain.

## Live shadow method

The live phase keeps `capability_evidence_mode="shadow"` and never introduces an
active mode. Actual assignment is durable before the background evaluator is
scheduled. The evidence cutoff is the assignment timestamp; observations
recorded later cannot influence that decision. Admission freezes bounded,
non-secret assignment-time claim inputs. Canonical rematching and exact
descriptor/model scope construction run from that immutable snapshot as bounded
background work outside the production queue lock, so scope capture cannot delay
handout. A later re-registration, session change, drain, or blacklist change
cannot be recorded as though it existed at the earlier decision time.

Graceful shutdown closes new admission and uses a finite background drain. A
scope capture that exceeds the drain is an admission `scope_capture_failed`
event with reason `coordinator_shutdown_during_scope_capture`. An already-running
decision write may outlive the await interval and records its real `completed`
or `decision_write_failed` terminal result when the bounded write returns; the
process-local containment counter exposes the overrun. These are coordinator
health outcomes, never node blame.

The shared hard matcher includes the server-derived output budget. A typed node
whose claimed `limits.max_output_bytes` is lower than the task's
`max_output_bytes` is excluded before candidate-scope capture, with
`insufficient_output_capacity`; equality is eligible. Descriptorless explicit
compatibility sessions retain their legacy behavior because the coordinator
does not invent a typed output-capacity claim. The durable attempt's exact
server-issued output limit remains authoritative after assignment.

For every actual assignment, retain:

- the exact eligible candidate scopes supplied to the evaluator;
- whether the result was `same`, `different`, or `no_preference`;
- the bounded rationale code;
- the actual scope and hypothetical preferred scope; and
- delayed outcomes for deadline completion and required candidate-local
  contract-floor assurance when each outcome is observable.

Evaluation windows must be chronological. Train/aggregate only observations
recorded before a decision, then score the actual and hypothetical choices in a
later window. Results must be reported both overall and by predeclared
operational slices. Random train/test splitting is insufficient because it can
hide descriptor churn and time-dependent distribution shift.

Optional duplicate sampling remains controlled independently by `verify_rate`.
If it is used, its compute cost and selection effects are reported separately.
Agreement is never substituted for a held-out deadline or contract-floor label.

## Metrics

Report counts beside every rate. At minimum, the live report includes:

- admission counts for `disabled`, `not_applicable`, `queue_saturated`,
  `scope_capture_failed`, and `scheduled`;
- `assignment_observation_total`, the sum of all admission outcomes plus
  `orphan_evaluation_total`;
- evaluation counts for `completed`, `evaluator_failed`,
  `decision_write_failed`, and `cancelled_on_shutdown`;
- `orphan_evaluation_total`, counting terminal evaluation rows whose admission
  record did not persist;
- offered, scheduled, completed, skipped, failed, and pending totals;
- the drop/failure numerator, denominator, and derived rate, with bounded
  admission-cohort time-window filtering and latest event time;
- process-lifetime counts of durable health-record write failures, unexpected
  containment failures, and background-task callback failures beside the
  process reset timestamp;
- counts of `same`, `different`, and each `no_preference` rationale;
- evidence coverage by exact scope and metric;
- hypothetical divergence coverage: decisions with one unambiguous different
  preference and observable labels for both policy choices;
- deadline-success and contract-floor pass rates for actual claim-only versus
  hypothetical shadow choice in a delayed window;
- paired rate differences with 95% confidence intervals;
- Wilson intervals for each underlying scoped binary rate;
- calibration by evidence bucket, including whether a larger Wilson lower bound
  predicts the held-out outcome;
- scope resets and the count of old-scope samples correctly ignored;
- cold-start and hard-eligibility violations;
- recent latency and effective-output-byte-rate distributions, labelled as
  coordinator-visible rather than model-only measures;
- handout latency and assignment identity with shadow off versus shadow on; and
- operational fairness slices by task class, selected model, descriptor age,
  evidence age, enrollment age, and new-versus-established scope.

Sampled agreement is reported as its own diagnostic rate and is excluded from
the promotion calculation. Contribution points are excluded entirely.

## Go/no-go thresholds for considering a future active experiment

These thresholds do not activate routing. Meeting all of them permits drafting
a separate active-routing ADR and controlled experiment; missing any threshold
is a no-go.

### Immutable identity prerequisite

Every candidate scope considered for a future active experiment must have an
immutable model digest and an exact reconstructable typed descriptor/model
identity. Any of the four bounded identity blockers above is a no-go. The
diagnostic does not change current production hard eligibility or alter a shadow
preference. It also does not itself suppress collection: a digestless typed
scope continues collecting when the existing evidence resolver can otherwise
reconstruct it; existing exclusion of legacy or incomplete evidence scopes is a
separate boundary.

An active-routing experiment requires all four of the following:

1. immutable model and descriptor identity for every participating scope;
2. all live volume, safety, predictive, and fairness thresholds below;
3. a separate accepted ADR; and
4. a separately reviewed implementation PR.

This document and the `off`/`shadow` modes do not implement active routing.

### Evidence and decision volume

- Every exact candidate scope and every metric used in a hypothetical
  preference has at least 20 pre-decision observations for that metric.
- At least 200 durable shadow decisions are observed after applying the
  20-sample evaluation floor.
- At least 50 decisions are evaluable divergences: `different`, with delayed
  deadline labels and, where the policy used contract evidence, delayed
  contract-floor labels for comparison.

The production diagnostic default of 5 samples does not satisfy this gate. The
live promotion analysis must re-evaluate with a floor of 20.

### Safety and non-interference

- Zero hard-ineligible candidates enter the evaluated candidate set.
- Zero preferences are expressed when any relevant candidate/metric is below
  the 20-sample floor.
- Zero descriptor, model, task-class, role, or enrollment resets inherit prior
  scope evidence.
- Zero production assignments differ because shadow mode is enabled.
- Zero shadow failures fail or cancel a production handout or settlement.
- Shadow-on versus shadow-off p95 handout latency differs by no more than 5 ms
  in a controlled coordinator test, and the reported shadow drop/failure rate
  remains below 1%.

The operational-health rate used by this gate is reproducible from reported
counts:

```text
orphan_evaluation_total = evaluation rows with no persisted admission row
assignment_observation_total = all admission outcomes + orphan_evaluation_total
scheduled = scheduled admissions + orphan_evaluation_total
offered = scheduled + queue_saturated + scope_capture_failed
numerator = queue_saturated + scope_capture_failed + evaluator_failed
            + decision_write_failed + cancelled_on_shutdown
denominator = offered
drop/failure rate = numerator / denominator
```

The rate is unavailable when the denominator is zero. `disabled` and
`not_applicable` are reported as skipped but excluded from offered. Every orphan
evaluation is treated as one inferred scheduled/offered observation, making its
terminal outcome and denominator contribution explicit. Pending is `scheduled`,
including inferred admissions, minus all terminal evaluation outcomes, floored
at zero. A bounded time window selects an inclusive admission-time cohort and
follows those admitted attempts to their evaluation outcomes even when they
finish after the window end; orphan rows are selected by evaluation time.

Any single violation is an immediate no-go regardless of predictive metrics.

### Delayed-window predictive result

- On the chronological held-out window, the hypothetical shadow choice improves
  deadline success by at least 5 percentage points over claim-only, and the
  lower bound of a paired 95% confidence interval for that improvement is above
  zero.
- For decisions in which contract-floor evidence participates, contract-floor
  pass rate also improves by at least 5 percentage points, with the lower bound
  of its paired 95% confidence interval above zero.
- Neither metric may regress overall or in a predeclared sufficiently sampled
  operational slice.

### Operational fairness

- Each reported fairness slice used for a go decision contains at least 20
  evaluable decisions; undersampled slices remain explicitly inconclusive.
- New or reset scopes below the evidence floor have a shadow preference rate of
  exactly zero.
- No sufficiently sampled slice has a point regression greater than 5
  percentage points for deadline success or contract-floor pass rate.
- Among slices with comparable hard requirements, evidence counts, and held-out
  outcome rates, the absolute shadow-preference-rate gap is at most 10
  percentage points. Larger gaps require an explained task or outcome
  difference and a renewed review; otherwise the result is no-go.

Mycelium does not collect demographic attributes. These checks concern access
to work and cold-start behavior across operational cohorts; they are not a
claim of broader social fairness.

## Confounders and limitations

- The current ten cases are hand-authored. Their labels and evidence counts can
  encode the author's expected result.
- Ten cases cannot estimate a stable effect or a fairness disparity.
- The synthetic “held-out” labels are not independent production samples.
- Routing creates selection bias: actual workers receive outcomes while many
  hypothetical alternatives do not.
- Duplicate sampling changes compute load and may not represent unsampled
  traffic.
- Model stochasticity can change contract and agreement results for identical
  prompts.
- Descriptor, selected-model, executor, and task-class churn reset scopes and
  can leave mature workers statistically cold.
- Task mix, prompt difficulty, output contracts, timeouts, and network location
  can change across windows.
- Deadline failures can be censored by cancellation or coordinator policy;
  excluded causes must not be silently relabelled as worker failures.
- Coordinator wall time includes network and orchestration overhead. Effective
  output bytes per second is not token throughput or answer quality.
- Contract floors establish bounded structural assurance only and are absent
  for some tasks.
- Repeated attempts from one enrollment are correlated; treating every row as
  independent can understate uncertainty.
- The existing operational circuit breaker and hard matcher affect which
  attempts become observable, even though evidence does not feed either.
- Operational-health records are best-effort experiment telemetry. Successful
  observations and shadow decisions are durable in the sibling
  `capability-shadow-health.db`, whose writer locks are isolated from
  authoritative `events.db`. Legacy decisions copy forward idempotently.
  Backup/restore must include both databases in format v2; restore remains
  compatible with legacy format v1 without health state. A missing health
  database is otherwise valid only for pre-feature state. Health-store write,
  containment, and callback failures are process-lifetime counters that reset
  on restart; a missing durable row therefore cannot be interpreted as proof
  that no failure occurred.
- Distribution shift can reverse an apparently strong historical preference,
  as the fixture's final case demonstrates.

## Rollback and removal

Immediate rollback is setting:

```json
{
  "capability_evidence_mode": "off",
  "verify_rate": 0.0
}
```

`off` stops new shadow evaluations. Setting `verify_rate` to zero independently
stops duplicate comparison work. Neither change affects hard eligibility,
attempt authority, accepted receipts, contribution accounting, or the existing
operational circuit breaker.

Digestless typed or otherwise non-promotable exact scopes may still retain and
collect shadow evidence while shadow mode is enabled when the existing resolver
can safely reconstruct them. Their future-active diagnostic is not a routing
control, reputation score, correctness verdict, or trust score.

If the experiment is abandoned:

1. leave mode `off`;
2. remove the post-assignment scheduler and protected aggregate route;
3. stop post-terminal evidence projection and startup reconciliation;
4. retain the append-only tables for audit until the operator's retention
   decision; and
5. if physical deletion is required, back up the database and use an explicit
   offline migration rather than bypassing append-only triggers in a running
   coordinator.

No attempt, receipt, descriptor snapshot, or contribution migration is needed
to remove the evaluator. A future active experiment must satisfy the immutable
identity and live-threshold gates, add a new mode under a separately accepted
ADR, and arrive in a separately reviewed implementation PR. It must not
repurpose `shadow` or the deprecated first-refusal hook.
