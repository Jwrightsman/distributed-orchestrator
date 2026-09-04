# Does ensemble beat decomposition at equal compute?

**Status: pre-registered, not run.** Written in September 2026 so the design is
fixed before any data exists. Nothing in this document reports a result, and
nothing in this pull request executed any part of it.

**This is not the August experiment.** [`docs/ensemble-vs-decomposition.md`](../ensemble-vs-decomposition.md)
reports a measurement that was made and came out inconclusive: 12/22 for a
single ensemble candidate against 2/10 for decomposition on one artifact, Fisher
exact one-sided p = 0.073. That document remains the record of what happened.
This one is the design for settling it, and it differs from the pilot in three
ways that matter:

1. **It is paired.** The pilot ran both arms against one task (Snake) many
   times, so the comparison was unpaired and Fisher's exact test was correct
   there. Running both arms over a *corpus* pairs them by item, and a paired
   design gets far more out of the same number of runs.
2. **The primary endpoint is success at equal compute**, not success per
   attempt. The pilot's own note is that the practical advantage came from cost
   — roughly six minutes an attempt against fifty.
3. **The compute ratio is measured, not assumed.** The arms are configured to
   be cost-matched; whether they actually were is a finding the summariser
   reports, and the equal-compute claim is withdrawn if they were not.

---

## Hypothesis

> Given a fixed compute budget, ensemble execution produces a working artifact
> on more corpus items than decomposition does.

The mechanism proposed by the August 2026 external review, and the reason to
expect it: decomposition splits an artifact across builder agents that cannot
see each other, so they must agree on shared interfaces by luck. Ensemble asks
one agent for the whole thing and buys independent attempts instead. A chart has
almost nothing to agree about; a game has almost everything.

**The null**: at equal compute, the two architectures produce working artifacts
on the same items, and the difference observed in the pilot was the small
sample it was.

---

## Arms

Three, all running the **same builder prompt** so the comparison isolates the
architecture rather than measuring a new prompt. `ensemble.py` reads the active
prompt set for exactly this reason.

| arm | strategy | configuration | attempts per item |
| --- | --- | --- | --- |
| `decomposition` | `dag` | the shipping default: planner, N builders, reviewer, reviser | 1 |
| `ensemble_5` | `ensemble` | `candidates: 5`, `concurrency: 1` | 1 (five candidates, mechanically selected) |
| `direct` | `direct` | `candidates: 1` | 1 |

**Why five candidates.** Cost-matching is the whole point, so the count comes
from the measured costs rather than from a round number:

* A decomposed run costs, over the 140 item-runs recorded in `evals/results/`,
  a mean of **29.3 minutes** and a median of 20.0.
* One complete candidate costs a median of **6.1 minutes** (365 s), from the
  August experiment's 22 trials.
* 29.3 / 6.1 = **4.8 candidates**. Five is the nearest whole number, and it is
  also the ceiling execution protocol v1 places on one execution, so the
  cost-matched arm is expressible without splitting it across executions.

On medians the ratio is 3.3 rather than 4.8, which is a real spread and the
reason the *realised* ratio is reported alongside the result rather than taken
on faith. `scripts/eval_study_summary.py` compares the arms' measured wall clock
and refuses to describe the comparison as equal-compute if they came out more
than 25% apart.

**`direct` is a baseline, not a cost-matched arm.** It costs about a fifth of
the other two. It is there to separate "ensemble helps" from "one agent writing
the whole thing helps", which the pilot could not distinguish because its
ensemble arm was N=1 throughout.

---

## The corpus

The **36-item confirmatory set**, frozen in
[`evals/split.lock.json`](../../evals/split.lock.json) and digest-checked by
`tests/test_eval_corpus.py`. It was committed before this study was designed and
cannot be redrawn afterwards.

Every item in it was written from the task taxonomy in
[`eval-methodology.md`](../eval-methodology.md), not from a failure log. None
carries `origin: observed_failure`; `evals/corpus.py` refuses to return a
confirmatory item that does.

The 28 original items are **excluded**. They have been iterated against for four
prompt-set versions and are development-set material.

---

## Primary endpoint

**Success at equal compute**, where success is `GradeResult.passed` from
`evals/grading.py`: every mechanical check ran and every one passed. No model
judgment is involved anywhere in the primary endpoint. `judge_score` is
collected and recorded, labelled exploratory, and gates nothing.

**Compute is wall-clock model time**, in seconds, as recorded in
`RunRecord.wall_clock_seconds`. Chosen over token counts because it is the cost
that actually binds on this hardware — an 8 GB CPU-only machine — and because
token telemetry is not currently captured end-to-end, so a token-based endpoint
would be a promise rather than a measurement. Token counts are recorded when
available and reported as a secondary figure; when they are absent the record
says `token_cost` is unknown rather than implying zero.

**Both comparisons are reported. Equal-compute is primary.**

* *Equal-attempt*: `decomposition` against `ensemble_5`, one attempt each, McNemar exact, one-sided.
* *Equal-compute*: the same table, reported only if the measured wall-clock
  ratio between the arms is within ±25%. Outside that band, the study reports
  an equal-attempt result and states plainly that the equal-compute endpoint was
  not established.

Secondary, and pre-registered so they cannot be introduced afterwards as if they
had been planned: `direct` against `decomposition`; per-taxonomy pass rates
(descriptive only — no taxonomy group is large enough to carry a test, which
`evals/stats.py::min_detectable` will say so in as many words); and the
distribution of failure modes in each arm.

---

## Sample size, and what it can see

Computed, not asserted. `scripts/eval_power.py` prints this and
[`eval-methodology.md`](../eval-methodology.md) carries the whole curve.

* Discordant pair rate **ψ = 0.643** (18 of 28), from the only pair of
  committed runs made under an identical configuration — v3 on Aug 8 against v3
  on Aug 11. 95% CI 0.46–0.79.
* At **n = 36**, the minimum detectable effect at 80% power and α = 0.05 is
  **34.2 percentage points**, which is 12.3 items.
* Power at the effect the pilot suggests (55% against 20%, a 35-point gap) is
  **0.82**.
* `stats.required_n(0.35, 0.643)` is **35**. The confirmatory set is 36.

**This study is powered for a large effect and nothing else.** A 20-point
advantage would need 107 items and would not be detected here; a 15-point
advantage would need 187. If ensemble's real advantage at equal compute is
modest, this study returns "no difference" and that will be a power statement,
not evidence of equivalence. It is written down here so it cannot be reported
as equivalence later.

Using ψ from the noise floor is a **conservative assumption**, and deliberately
so: it is the discordance between two runs of the *same* configuration, and two
different architectures should disagree at least that often. If the realised
discordance comes out lower, the study is better powered than planned. It is
reported either way.

---

## Cost

36 items × (29.3 + 5×6.1 + 6.1) minutes ≈ **39 hours** of inference on the
reference machine, or about 34 using medians. One machine, sequential, because
two simultaneous model calls on 8 GB with no GPU thrash rather than parallelise.

That is the price of the answer. It is stated here rather than discovered
halfway through.

---

## Stopping rule

**Run all 36 items in all three arms. Then stop.** No interim looks, no
extending the ensemble arm because the p-value is close, no early stop because
it already looks decided.

This rule is not decoration. The August experiment moved from p = 0.14 at n=14
to p = 0.073 at n=22 and stalled, and the temptation at that point was to keep
adding trials to one arm until it crossed 0.05 — which, as
`min_trials_for_significance` shows, it never would have, because the limit was
in the baseline.

If the study is interrupted, it resumes; it is not reported partway.
`scripts/eval_study_summary.py` refuses to compute a statistic over a study with
a missing or ungraded cell, so a partial study cannot be reported by accident.

**One re-run is permitted, and only as a whole**: repeating all three arms over
all 36 items to measure this study's own noise floor, reported as a second
study rather than pooled into the first. Pooling a repeat into an inconclusive
result is how a stopping rule stops meaning anything.

---

## Ties and partial successes

There are none, by construction, and this is stated in advance so no scale gets
invented mid-analysis.

* **The endpoint is binary.** An item passed or it did not. There is no partial
  credit, because no partial-credit scale was pre-registered.
* **An ungraded item is not a failure.** If a check could not run — no browser,
  a missing fixture — the item is ungraded, and the summariser refuses to
  produce a statistic until it is graded or the study is re-run. It is never
  silently scored zero.
* **An item both arms fail, or both pass, is not a tie to be broken.** It is a
  concordant pair and contributes nothing to McNemar's test, which is correct
  and is why the corpus is banded.
* **A pipeline error** — the model unreachable, a crash in the harness — is a
  failed run of the *instrument*, not a failed artifact. Those items are re-run.
  If an item cannot be run in some arm after three attempts, the study reports
  it as excluded, names it, and reports the result with and without it.

---

## What is pinned, and how a change is detected

Recorded on every run by `evals/runrecord.py`:

| fact | source | why |
| --- | --- | --- |
| model provider, name, **digest** | Ollama `/api/tags` at run start | a model update mid-study invalidates it |
| temperature, seed | `config.json` — **which does not currently set either** | see below |
| descriptor hash | the provenance envelope (ADR 0017), for runs that go through the normal execution path | which executor produced this |
| corpus version and digest | `evals/corpus.py` | a reworded prompt is a different measurement |
| grader version | `evals/grading.py::GRADER_VERSION` | the ensemble pilot was scored 0/14 by a broken checker |
| wall clock, tokens when available | the harness | the primary endpoint depends on it |
| artifact SHA-256 | `runrecord.artifact_digest` | so a result can be re-scored without regenerating it |

**Temperature and seed are not currently pinnable, and that is a prerequisite,
not a footnote.** `config.json` has no setting for either, so every local run
takes whatever Ollama defaults to. `evals/runrecord.py` records both as unknown
rather than implying they were fixed — but a study whose generator temperature
is not pinned has one more uncontrolled variable than this design assumes, and
the fix is a config setting, not a caveat. **Add it before running.**

**A model update during the study invalidates the study.** This is not a
hypothetical: `qwen3.5:4b` is a moving tag, and re-pulling it between arms would
silently make the two halves incomparable. The provenance envelope from Theme 3C
gives the model digest and the descriptor hash needed to detect it, and
`scripts/eval_study_summary.py` prints a warning when the digests in one study
disagree. Where a run could not obtain a digest, the record says
`model_digest` is unknown rather than implying it was checked — following the
envelope's own convention of recording absent facts rather than inferring them.

If a model change is detected, the study is discarded and re-run. It is not
adjusted for.

---

## What counts as no difference

Stated in advance, so the result cannot be reinterpreted afterwards. Any of
these is a null result and gets published as one:

* `ensemble_5` does not pass more confirmatory items than `decomposition`.
* Or it passes more, but McNemar's exact one-sided p > 0.05 at the 36 items run.
* Or the arms' measured wall clock came out more than 25% apart, so what was
  measured is not the equal-compute endpoint whatever the p-value says.
* Or the discordant count is small enough that no split could have cleared
  α = 0.05 — `stats.min_detectable` returning None — in which case the study
  had no resolving power and says so instead of reporting a p-value.

A null result here means **this study could not resolve it at 36 items**, not
that the architectures are equivalent. The distinction is the entire content of
the sample-size section above.

---

## What would count as evidence decomposition should stop being the default

Also stated in advance, and deliberately harder to satisfy than the null,
because changing a shipping default is the more consequential move:

**All four, together:**

1. `ensemble_5` beats `decomposition` on the confirmatory set at McNemar exact
   one-sided **p < 0.05**;
2. the measured wall-clock ratio between the arms is **within ±25%**, so the
   win is at equal compute rather than at more compute;
3. the advantage is **not confined to one taxonomy group** — it appears in at
   least three of the nine taxonomy families, descriptively, since no single
   family is large enough to carry a test;
4. and `ensemble_5` does not regress any item that `decomposition` passes by
   more than **a third of the items it gains**. A change that wins on average
   while breaking a quarter of what already worked is not an improvement to a
   default.

If 1 and 2 hold but 3 or 4 does not, the finding is that **ensemble is better
for a nameable class of work**, which is a routing question for the selector,
not a change of default. That is a different and smaller claim, and it gets
written as one.

Nothing here promotes anything on a category result. A taxonomy group in this
corpus has four to eight items; `min_detectable(4)` is None, meaning no split of
four discordant pairs reaches α = 0.05 in any arrangement. "But `interactive_artifact`
improved" is the reasoning that kept prompt set v5 alive for a session.

---

## Prerequisites before this can run

1. **The confirmatory items must be banded.** 72 of the 100 corpus items,
   including all 36 confirmatory ones, have never been run and carry `band:
   null`. Banding costs `36 × 5 × 6.1 min ≈ 18 hours` at the cheapest arm
   (`scripts/eval_band_corpus.py --live --trials 5`). Running the study against
   an unbanded set is permitted — the bands do not change the test — but the
   band distribution decides whether the corpus had resolving power, and that is
   worth knowing before spending 39 hours rather than after.
2. **Ollama up, one model, one machine, nothing else running.** Concurrent load
   on 8 GB wedges inference, and a study interrupted by a wedged model is a
   study with missing cells.
3. **A recorded model digest.** If `/api/tags` cannot be reached, every run
   records `model_digest` unknown and a mid-study model change becomes
   undetectable. Fix that before starting, not after.
4. **A pinned temperature and seed in `config.json`.** Neither exists today. See
   the pinning table above: the harness records them as unknown, which is honest
   but leaves the generator's own settings uncontrolled across arms.

---

## What this study will not answer

* **Anything about non-coupled artifacts at the ceiling.** The chart is already
  10/10 under decomposition. There is no headroom there and no reason to expect
  a difference.
* **Anything about tasks exceeding one model's context.** Ensemble cannot help
  where a single model physically cannot hold the problem, and every artifact in
  this corpus fits in one file.
* **Whether a better selector would change the answer.** Ensemble is only as
  good as the mechanical checks that pick the winner, and those checks took two
  attempts to get right in the pilot. This study fixes the selector and varies
  the architecture. Varying the selector is a separate experiment.
* **Anything about distributed placement.** Both arms run locally. Strategy and
  placement are orthogonal in this system and mixing them here would confound
  the comparison with network latency.
