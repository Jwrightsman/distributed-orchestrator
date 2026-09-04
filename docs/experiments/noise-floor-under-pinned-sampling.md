# Does pinning the generator lower the noise floor, and by how much?

**Status: pre-registered, not run.** Written in September 2026 so the design is
fixed before any data exists. Nothing in this document reports a result, and
nothing in the pull request that added it executed any part of it. The
measurement is an overnight batch on the maintainer's hardware and has not been
run.

**The measured noise floor is ψ = 0.643** — 18 of 28 items changed outcome
between prompt set v3 on Aug 8 and v3 on Aug 11, the only pair of committed
runs made under an identical configuration, 95% CI 0.46–0.79. That is the only
measured value this project has, it is what `docs/eval-methodology.md` §7 rests
on, and nothing below replaces it.

---

## Why this is worth 17 hours

Two facts from PR #71 belong together and were recorded separately.

The first is the power model. `evals/stats.py` computes it exactly, and the
published table confirms the closed-form shape to two decimals: the minimum
detectable effect goes as **δ ∝ √(ψ / n)**. Halving ψ is worth exactly what
doubling n is worth. `scripts/eval_power.py` prints what that means:

| noise floor | items needed to detect 15 points at 80% power |
| --- | --- |
| ψ = 0.643 (measured) | **187** — 182 hours per comparison, which nobody will run |
| ψ = 0.5 (projection) | 148 |
| ψ = 0.4 (projection) | 118 |
| ψ = 0.32 (projection) | **95** — the corpus this project already has |
| ψ = 0.25 (projection) | 74 |

The second is that `config.json` had no temperature and no seed, so every run
this project has ever made took Ollama's own defaults (0.8 and 0, documented).
Unpinned sampling is a plausible contributor to run-to-run noise, it is now
removable, and **nobody has measured how much of the 0.643 it accounts for.**

If pinning halves ψ, the 100-item corpus becomes an instrument that can resolve
an ordinary good prompt change, and the conclusion "no corpus this project will
run resolves 15 points" stops being true. If pinning does nothing, that
conclusion stands and the remaining lever is
[`replicate-endpoint-design.md`](replicate-endpoint-design.md). Either answer
is worth 17 hours; not knowing which is worth nothing.

---

## What is being compared, and what is not

**Pinned against unpinned, both measured in this study.** Not pinned against
the historical 0.643. This distinction is the reason the study has two arms
instead of one, and it is easy to get wrong.

ψ̂ = 0.643 came from a single pair of runs and its 95% CI is 0.46–0.79. A
second unpinned measurement will very likely come in lower than 0.643 for
reasons that have nothing to do with pinning — regression to the mean on a wide
interval. A single-arm study that ran only the pinned configuration and
compared it to 0.643 would attribute that regression to pinning and report a
reduction that was mostly the estimator returning to where it lives.

There is a second reason to expect a lower re-measurement, and it is arithmetic
rather than a hunch. **This is a derivation, not a measurement, and it does not
replace the 0.643:** if two runs of one item are independent draws from that
item's own pass probability p, the item is discordant with probability
2p(1−p), which is at most 0.5. So ψ = E[2p(1−p)] ≤ **0.5 for any mix of items
whatsoever**, and the measured 0.643 sits above the ceiling that any
independent-runs model can produce. That is consistent with noise —
P(X ≥ 18 | n = 28, ψ = 0.5) ≈ 0.09, an ordinary upward fluctuation — and it is
also consistent with the corroborating estimate available from the corpus
itself: the 28 banded items give E[2p̂(1−p̂)] = 0.411, and correcting the
downward bias a five-trial p̂ carries (E[p̂(1−p̂)] = (k−1)/k · p(1−p), so divide
by 0.8) puts it at about 0.51 — beside the 0.521 mean discordant rate
`docs/eval-methodology.md` §1.4 already reports across all ten pairs.

None of that is a new measured ψ and none of it is reported as one. What it
says is narrower and it changes this design: **an unpinned arm run today would
be expected to land nearer 0.5 than 0.643 before pinning does anything at
all.** Hence two arms, and hence the comparison of interest is between them.

---

## Design

Two identical-configuration pairs over the same item subset. Four runs.

| arm | temperature | seed | what it reproduces |
| --- | --- | --- | --- |
| `unpinned` | absent from the request | absent from the request | the shipping default, and every run this project has made |
| `pinned` | `0.0` | `20260904`, the same value on both runs of the pair | a fixed generator |

Each arm is run **twice over the same items**, and its ψ is the fraction of
items whose graded outcome differed between its two runs. Order:
unpinned-1, pinned-1, unpinned-2, pinned-2 — interleaved rather than blocked,
so a machine that gets slower or hotter over fifteen hours does not load onto
one arm.

`config.json` carries `"temperature": null, "seed": null` for the unpinned runs
and `"temperature": 0.0, "seed": 20260904` for the pinned ones. Nothing else
changes between any of the four runs: same corpus digest, same prompt set, same
model digest, same machine, nothing else running.

Both runs of the pinned pair use the **same** seed. That is what makes it a
pinned generator rather than a controlled one; if the two pinned runs used
different seeds the arm would be measuring seed-to-seed variation, which is not
the question.

### The arm, and whether its answer transfers

**`direct` — a single complete candidate, `ensemble.run_ensemble(task, 1, …)`.**
Median 6.1 minutes against decomposition's mean of 29.3, so the whole study
costs 15 hours instead of 72.

**This measures the direct arm's ψ, and that must be said wherever the result
is quoted.** Whether it transfers to the decomposition arm — the one
[`ensemble-vs-decomposition.md`](ensemble-vs-decomposition.md) will actually
use — is a separate question, and the honest answer is that it partly does and
partly does not:

* **What transfers.** Both arms generate through `ollama_client.generate`, so a
  temperature and seed configured for one reach the other identically, and any
  reduction in *token-level* sampling variance is common to both.
* **What does not.** Decomposition has variance the direct arm structurally
  cannot have. A planner emits a JSON plan that can name two subtasks or five;
  builders that cannot see each other must agree on shared interfaces; a
  reviewer re-emits the whole deliverable; a reviser may or may not fire.
  Pinning the sampler does not remove branching, it only makes each branch
  reproducible given identical inputs — and the inputs to builder 3 depend on
  what the planner said. **Decomposition's ψ should therefore fall by less than
  the direct arm's, and could fall by very little.**
* **So the transfer is directional, not numeric.** A reduction measured here
  is an *upper bound* on the reduction decomposition would see. It is
  pre-registered as an upper bound, and any statement of the form "pinning
  halves ψ for the decomposition study" is out of bounds on this evidence.

If the direct arm shows a large reduction and the decomposition study depends
on it, the correct next step is to measure decomposition's ψ directly at
4 × 37 × 29.3 min ≈ 72 hours — a decision to be made after this result, not
assumed by it.

---

## Sizing the subset

**n = 37.** Computed from the resolution required, not chosen for roundness.

The question is whether the two arms' ψ can be told apart when one is 0.643 and
the other is half of it, 0.321. Two criteria, and the binding one wins.

**Interval separation.** ψ is a proportion, and the deliverable is an estimate
with an interval, not a verdict. The 95% Wilson intervals around ψ̂ = 0.643 and
ψ̂ = 0.321 first stop overlapping at **n = 37**: 0.488–0.782 against
0.196–0.485. At n = 36 they still touch.

**Power.** Treating the two arms as independent proportions — conservative,
since they share an item subset and a paired analysis would do better — the
exact one-sided Fisher test reaches 80% power at **n = 34** (0.811), and is
0.821 at n = 37. The discreteness makes this non-monotone; it stays above 0.80
for every n from 34 up.

Interval separation is the stricter criterion and the one the result will be
read against, so **37**.

For scale, at n = 37 the CI half-width on a single arm's ψ is about ±0.15. That
is wide, and it is what 15 hours buys. Distinguishing a *quarter* reduction
rather than a half would need roughly four times the items and four times the
hours, which is why the pre-registered question is "is it halved" and not "what
is it exactly".

### Which 37 items

Fixed here, before any data, and recomputable:

* **The 28 original items**, unchanged — the exact set the 0.643 came from, so
  the unpinned arm can also be reported over those 28 alone and compared
  directly with the historical measurement.
* **Nine development-split taxonomy items**, drawn by
  `random.Random(20260904).sample(sorted(ids), 9)` over the 36 development
  items with `origin: taxonomy`. That draw is:
  `classify-sentiment-keywords`, `kernel-binary-search`, `kernel-word-ladder`,
  `page-colour-picker`, `synth-invoice-lines`, `synth-lorem-paragraphs`,
  `synth-timeseries`, `tests-for-temperature-convert`,
  `transform-word-frequencies`.

A seeded draw rather than "the first nine by id", because the ids sort into
families and the first nine would have been four `analyse-*` and four
`classify-*` — two taxonomy groups standing in for nine. The draw above covers
six.

**The confirmatory 36 are not touched.** This measures the instrument, not the
system, and spending the held-out set on an instrument check would leave
nothing held out for the study it exists for.

**The primary estimate is over all 37.** The 28-item figure is secondary and
reported for continuity, not tested.

---

## Verifying the seed, before anything else

`sampling.py` records seed honouring as **assumed**, not verified. What is
established is that Ollama's API accepts `seed` and `temperature` inside
`options`, and that this project's request carries them. What is not
established is that the runner reproduces an identical completion for
`qwen3.5:4b` on an 8 GB CPU-only machine — that depends on batching, thread
count and KV-cache reuse, and nothing here has measured it.

**If the seed is not honoured, the pinned arm is not pinned**, and the study
measures temperature alone while reporting something else. So this runs first:

1. Ten tasks drawn from the 37-item subset by
   `random.Random(20260905).sample(sorted(subset), 10)`.
2. Each generated **twice** through the direct arm at temperature 0 and seed
   20260904, with nothing else running.
3. Compare the raw completions byte for byte.

Cost: 20 runs × 6.1 min ≈ **2.0 hours**.

Real corpus tasks rather than a short probe prompt, deliberately. Determinism
over thirty tokens says nothing about determinism over a two-thousand-token
deliverable, which is what the study actually generates.

Pre-registered, in advance:

* **10/10 byte-identical** → `sampling.SEED_HONOURING` becomes `"verified"` in
  the same change that records the evidence, and a run that sets a seed reports
  `sampling_pinned: true`.
* **Anything less than 10/10** → the constant stays `"assumed"`, the pinned arm
  is described throughout as *temperature-pinned, seed unhonoured*, and the
  study's finding is about temperature. It still runs — a temperature effect is
  worth knowing — but it is not reported as pinning.
* Either way the count is published. A partial result (say 7/10) is the most
  interesting outcome and the one most likely to be quietly rounded up.

---

## What is measured

**Primary: ψ per arm**, the fraction of the 37 items whose `GradeResult.passed`
differed between that arm's two runs, with a 95% Wilson interval. Graded by
`evals/grading.py` — mechanical checks only, no model judgment, and the
corrected HTML execution check rather than `browser_ok`.

**The comparison: ψ_unpinned against ψ_pinned**, exact one-sided Fisher
(the hypothesis is directional: pinning is expected to reduce noise, and an
increase and a wash lead to the same action). The paired analysis over the same
items is reported alongside as a secondary figure, since it is more powerful
when the two arms' per-item discordance correlates, and the size above does not
assume it.

**Secondary, and recorded before it can become interesting: the pass rate of
each arm**, with its interval. See below.

**Recorded on every run** by `evals/runrecord.py`: corpus digest, model digest,
temperature, seed, seed honouring, wall clock, artifact SHA-256, grader
version. A model digest that changes mid-study invalidates the study; it is
discarded and re-run, not adjusted.

---

## Pre-registered interpretation

Written before any data so no outcome can be reinterpreted afterwards. Read the
first criterion that applies.

**No resolution.** If the 95% intervals on ψ_unpinned and ψ_pinned overlap and
the Fisher p > 0.05, the study **did not resolve it**. That is reported as "not
resolved at 37 items", never as "pinning makes no difference". The two are
different claims and only the first is supported.

**Pinning is the dominant noise source.** All of:
1. ψ_pinned ≤ 0.32 — at or below half the measured floor; and
2. Fisher one-sided p ≤ 0.05 against ψ_unpinned; and
3. the upper end of ψ_pinned's 95% interval is below ψ_unpinned's point
   estimate.

Criterion 1 is absolute rather than relative, and 0.32 is not chosen for
symmetry with 0.643: it is the floor at which the 100 items that already exist
detect 15 points at 80% power. A reduction that does not reach a floor somebody
can act on is the third branch below, whatever its ratio. Criteria 2 and 3 are
the within-study comparison, which is what makes the reduction attributable to
pinning rather than to re-measurement.

Then the operating floor for the direct arm is the measured ψ_pinned, the
corresponding column of `scripts/eval_power.py`'s grid stops being a projection
*for that arm*, and the corpus size the 15-point target needs is read off it.
It does **not** follow that the decomposition study is re-powered — see the
transfer argument above; that needs its own 72-hour measurement.

**Pinning is irrelevant.** Either of:
1. ψ_pinned is within 0.05 of ψ_unpinned and the intervals substantially
   overlap; or
2. ψ_pinned > ψ_unpinned.

Then run-to-run noise does not live in the sampler, and pinning is a
bookkeeping improvement rather than a power intervention. The remaining
candidates — planner branching, subtask count, reviewer context assembly,
numerical non-determinism in the runner — are named here so the next
investigation has somewhere to start. The lever for the corpus becomes
[`replicate-endpoint-design.md`](replicate-endpoint-design.md) or more items,
and `docs/eval-methodology.md`'s conclusion that no runnable corpus resolves
15 points stands unchanged.

**Pinning helps, but not enough.** ψ_pinned is significantly below ψ_unpinned
but above 0.32. Then the achieved value is read off the grid and the corpus
size it implies is stated as a number. The 15-point target is described as
reachable **only if** a bracketed cell in the grid says so at a size somebody
will run. "It moved in the right direction" is not a finding this project
publishes on its own; the finding is the corpus size the new floor implies.

**A low unpinned arm is not a finding about the floor.** If ψ_unpinned comes in
well below 0.643 — which the arithmetic above says to expect — that is
reported as a second estimate of the same quantity with its own interval, and
the two are pooled or presented side by side. It is **not** reported as the
noise floor having improved, and it does not replace 0.643 as the measured
value in `docs/eval-methodology.md` unless the pooled estimate is stated as
such with both pairs named. Nothing about the instrument changed between
August and now; the estimator moved.

**Pinning makes it worse.** ψ_pinned > ψ_unpinned significantly. This gets
published. ROADMAP §2: publish the number that makes us look worse. A plausible
mechanism exists — greedy decoding can lock a model into a failure mode it
would otherwise sample its way out of — so this is not an absurd outcome and it
is written down in advance rather than explained away afterwards.

### The pass rate is a finding in its own right

**A more deterministic arm is not necessarily an equally good arm.** Temperature
0 changes what the model produces, not only how much it varies. The endpoint of
this study is ψ, not success, and the design is not powered to detect a modest
pass-rate difference — at 37 items and this noise floor, nothing under about
30 points would be detectable.

Pre-registered anyway, so it cannot be introduced later as though it had been
planned: **if the arms' pass rates differ by 10 percentage points or more in
either direction, that is a finding requiring its own decision**, reported
alongside ψ with its interval and with an explicit statement that this study
could not test it. It would mean the choice of a study temperature trades
resolution against the thing being measured, and that trade is a decision to
make deliberately — not one to inherit from whichever setting happened to
tighten the interval.

Nothing here changes the shipping temperature. Production keeps both parameters
unset whatever comes out; a change to the default would need its own argument
and its own measurement.

---

## Cost

| | runs | hours |
| --- | ---: | ---: |
| Seed honouring check | 20 | 2.0 |
| Unpinned pair, 37 items × 2 | 74 | 7.5 |
| Pinned pair, 37 items × 2 | 74 | 7.5 |
| **Total** | **168** | **≈ 17** |

At the direct arm's median 6.1 minutes per run, one machine, sequential —
two simultaneous model calls on 8 GB with no GPU thrash rather than
parallelise. An overnight batch and most of a second one.

The same design over the decomposition arm at 29.3 minutes would be about
82 hours all in, or 72 for the four measurement runs without the seed check.
That is the price of the answer for the arm a study would actually use, and it
is not proposed here — see the transfer argument above for why it might
eventually have to be.

---

## Stopping rule

Run all four runs over all 37 items. Then stop.

No interim looks. No extending one arm because its interval is nearly clear of
the other's. No dropping an item because it errored in one arm — a harness
failure is re-run, up to three attempts, and an item that still cannot be run
is named and the result reported with and without it.

If the study is interrupted it resumes; it is not reported partway.
`evals/run_evals.py --resume` exists for exactly this, and
`scripts/eval_study_summary.py` refuses to compute a statistic over a study
with a missing or ungraded cell.

---

## Prerequisites

1. **The seed check above, run first.** Its outcome changes how the main
   result is described.
2. **Ollama up, one model, one machine, nothing else running.** Concurrent load
   on 8 GB wedges inference, and a wedged model mid-study is a missing cell.
3. **A recorded model digest.** If `/api/tags` cannot be reached every run
   records `model_digest` unknown and a mid-study model change becomes
   undetectable. `qwen3.5:4b` is a moving tag.
4. **No re-pull of the model between runs**, for the same reason.
5. **The corpus digest unchanged across all four runs.** A reworded prompt is a
   different measurement.

---

## What this will not answer

* **Decomposition's noise floor.** Measured on the direct arm; the transfer is
  argued above and is directional only.
* **Whether production should pin its temperature.** Different question,
  different endpoint — that one is about output quality, and this study is not
  powered for it.
* **Which of the remaining noise sources dominates, if pinning does not.**
  Planner branching, reviewer context assembly and runner non-determinism are
  named as candidates and separated by nothing here.
* **Whether ψ is stable over time.** Two pairs, one sitting. A floor measured
  in September says nothing about a floor in December, and the reason to
  re-measure it is that this project has already been surprised by one.
