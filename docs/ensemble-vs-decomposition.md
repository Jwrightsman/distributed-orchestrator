# Ensemble vs decomposition, measured

_August 15, 2026. `qwen3.5:4b`, CPU-only, prompt set v3, one machine._

**September 13 correction:** this historical pilot does not establish equal
compute or real candidate independence. Its unexecuted follow-on proposal is
withdrawn pending measurement repairs; use the [new unactivated pilot plan](experiments/2026-09-13-canonical-pilot-plan.md).

> **This historical pilot was inconclusive.** Its
> [earlier follow-on proposal](experiments/ensemble-vs-decomposition.md) was
> never run and is now withdrawn because its cost definition was invalid.
> The new bounded pilot remains unactivated until identity and cost capture
> prerequisites are met.

The August 2026 external review's sharpest observation came from this project's
own data: a labelled bar chart comes out right **10 times in 10**, and a
playable Snake game **2 times in 10** — same model, same prompts, same harness.
The proposed explanation was architectural rather than a model limit.
Decomposition splits an artifact across builder agents that cannot see each
other, so they must agree on shared interfaces by luck. A chart has almost
nothing to agree about. A game has almost everything.

If that is right, having **one** node write the whole game should do better than
three nodes writing thirds of it. This is the test of that.

---

## What was compared

| | decomposition (current default) | ensemble (new) |
| --- | --- | --- |
| Shape | planner splits the pitch into subtasks; separate builders write pieces; reviewer assembles | every node gets the whole pitch and produces a complete artifact alone |
| Agents per artifact | 1 planner + N builders + reviewer (+ reviser) | 1 |
| Cross-agent agreement needed | yes — names, interfaces, structure | none |
| Cost per attempt | **~50 min** | **~6 min** (median 365 s, range 309–626 s) |
| Selection | n/a — one output | mechanical checks pick the best of N |

**Same builder prompt in both arms.** `ensemble.py` reads the active prompt set,
so the only variable is the architecture.

---

## Result

Both arms scored by **one identical checker**, in a real headless browser,
after the checker itself was repaired (see "The instrument" below).

| architecture | passes | rate | 95% CI |
| --- | --- | --- | --- |
| **Decomposition** | 2/10 | 20% | 6–51% |
| **Ensemble, single candidate** | **12/22** | **55%** | 35–73% |

**Fisher exact, one-sided: p = 0.073.** Two batches, 14 then 8, pooled:
7/14 and 5/8 — consistent with each other, which is worth as much as the pooled
figure.

**Why Fisher and not `evals/compare.py`.** `compare.py` is this project's
instrument for prompt-set comparisons, and it is the right one there: the same
28 prompts run twice gives *paired* data, so it uses an exact one-sided McNemar
test over the prompts that flipped. This comparison has no pairing — the two
arms are different artifacts produced by different architectures, with no
correspondence between run *i* on one side and run *i* on the other. Fisher's
exact test on the 2x2 table is the correct test for unpaired proportions, and
it is implemented in `scripts/ensemble_experiment.py` with a check that it
reproduces the project's published 10/10-vs-2/10 result of p = 0.0004.

Using `compare.py` here would have meant inventing a pairing that does not
exist.

### The verdict is INCONCLUSIVE, and that is the honest headline

A 55% rate against 20% looks like a large win and is not a demonstrated one.
**22 trials were run specifically to try to settle it, and it did not settle**:
p moved from 0.14 at n=14 to 0.073 at n=22, and stalled there.

That stall is the informative part. Fisher's test is limited by the *smaller*
sample, and the baseline is ten runs — so the ensemble arm can be extended
almost indefinitely without crossing 0.05:

    12/22  vs 2/10 -> p = 0.073   (measured)
    15/30  vs 2/10 -> p = 0.096   (at a true rate of 0.50)
    50/100 vs 2/10 -> p = 0.067

**Resolving it requires about 19 runs in *each* arm**, and the decomposition
side costs ~50 minutes per run — roughly 8 more hours of inference, almost all
of it re-measuring the baseline. That is the price of the answer, and it is
worth stating rather than quietly paying or quietly skipping.

This project has deleted two prompt sets that looked like improvements and were
noise. Ensemble does not get promoted on p = 0.073.

---

## Independence-based illustration, not a measured compute comparison

Significance testing on the single-shot rate is the strict question. The
practical question a coordinator faces is different: **given a fixed compute
budget, which architecture delivers a working artifact?**

Historical elapsed times were about 50 minutes for a decomposed attempt and
about six minutes per complete candidate. They do not measure aggregate
hardware work or the correctness of an actual selected ensemble result.
The following table is an oracle calculation under independent candidate
errors, not an observed ensemble outcome or a supported cost-matched arm.

| | budget | P(at least one working artifact) |
| --- | --- | --- |
| Decomposition ×1 | ~50 min | **20%** (6–51%) |
| Ensemble ×5 | ~30 min | **98%** at p=0.55 · **88%** at the pessimistic bound p=0.35 |
| Ensemble ×8 | ~45 min | **99.8%** at p=0.55 · **97%** at the pessimistic bound p=0.35 |

The interval arithmetic does not remove the independence and perfect-selection
assumptions. It cannot establish a robust architecture advantage or cost parity.

Independent resampling constructs independent draws from the empirical marginal
outcomes, so it converges to the same closed form. The gap is Monte Carlo noise,
not evidence about real candidate independence. Perfectly correlated groups
gain nothing: five candidates with marginal success 50% still have any-pass
success 50%. The script now reports intact observed groups and actual selected
outcomes separately when those data are supplied; it never invents groups from
ungrouped trials.

---

## Why decomposition fails, from the failure text

The failures are not subtle, and they differ between the arms.

**Decomposition (8 failures):** six produced a canvas that was never drawn on at
all, one threw `Cannot read properties of undefined`, one was unsteerable. The
signature is *pieces that never connected* — a canvas element from one subtask
and drawing code from another that never met.

**Ensemble (10 failures across 22):** `head is not defined`, `Identifier 'goingRight' has
already been declared`, a blank canvas, two unsteerable, two that never
animated. The signature is *one model losing track of its own program* — an
ordinary small-model failure, not an integration failure.

That difference is the mechanism the review predicted, visible in the error
strings.

---

## The instrument, which was broken and is the reason this took two attempts

The first scoring pass returned **0/14** for ensemble. It was wrong, and the
history is worth recording because it nearly became a published number twice.

1. **The checker had a 1200 ms blind spot.** It waited 1200 ms before its first
   look. A correct Snake that nobody steers walks into a wall in about a second
   — measured deaths at 550/750/850/1100/1250 ms. So it arrived at a death
   screen and reported "GAME OVER visible" and "frame never changes", which are
   also the symptoms of a game that never started.
2. **Visibility was read off an element's own computed style**, so an
   `<h2>GAME OVER</h2>` inside a `display:none` overlay counted as visible.
3. **The forbidden-text scan used `textContent`**, which includes hidden
   descendants, so a visible wrapper around a hidden overlay matched.
4. **Those three fixes were never merged.** They were pushed to a branch after
   its pull request had already been merged, so `master` never received them and
   this experiment scored against the old checker.
5. **Fixing them introduced a fourth problem**: the rewrite dropped the actual
   key-response test, so a self-animating game that ignored the keyboard passed.
   Now the checker loads the artifact a second time, touches nothing, and
   requires that steering outlive not-steering.

**The published 2/10 survived all of it.** Re-scored under the final, harder
checker, decomposition comes out at exactly 2/10 again.

---

## What this does and does not license

**Supported by this data:**

- The historical complete-candidate success estimate is 12/22 against 2/10
  for the separate decomposition baseline; uncertainty remains substantial.
- Observed failure modes motivate a controlled architecture comparison.
- Comparable compute, candidate independence, and actual ensemble-selection
  benefit remain unestablished by these records.

**Not supported, and not claimed:**

- That ensemble's single-shot rate beats decomposition's. p = 0.073, over 22 trials.
- Anything about non-coupled artifacts. The chart is already 10/10 under
  decomposition; there is no headroom and no reason to expect a difference.
- Anything about tasks that genuinely exceed one model's context. Ensemble
  cannot help where a single model physically cannot hold the problem, and
  every artifact here fits in one file.

**To settle it:** ~19 runs per arm, ~8 more hours, almost all of it re-measuring
the decomposition baseline rather than generating more candidates.

---

## For the other execution strategies

[`ROADMAP.md`](../ROADMAP.md) §6 lists map, DAG, single, consensus and ensemble
as first-class strategies a planner would choose between. Nothing else was
built. What this experiment suggests about them, for whoever picks it up:

- **The choice is per-workload and measurable**, which is the useful part. The
  same harness works for any showcase candidate — `--candidate chart` scores the
  uncoupled case in about a fifth of the time.
- **"Ensemble" and "single" are the same generator** with N=1 versus N>1. There
  is no separate "single" strategy to build; it is a parameter.
- **Selection is the hard half, not generation.** Ensemble is only as good as
  the checks that pick the winner, and the checks are what took two attempts to
  get right here. A strategy that needs semantic judgement to select — rather
  than parse/load/draw/respond — inherits a much harder problem.
- **Cost ratios matter more than rates.** Ensemble wins the practical comparison
  on cost per attempt, not on quality per attempt.

Raw data: `scripts/ensemble_results/`. Re-score any run without regenerating it
with `python scripts/ensemble_experiment.py --score-only <dir>`.
