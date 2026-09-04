# Replicates and a continuous endpoint: the other lever, costed

**Status: pre-registered, not run.** Written in September 2026 alongside
[`noise-floor-under-pinned-sampling.md`](noise-floor-under-pinned-sampling.md)
so both designs are costed before either is chosen. Nothing here reports a
result, and nothing in the pull request that added it ran a model. Every power
figure below is a **simulation over a stated model**, labelled as a projection
wherever it appears.

`docs/eval-methodology.md` §7 names this as the remaining lever after corpus
growth and asserts it is "genuinely more efficient per unit of inference". The
first thing to say is that **the arithmetic does not support that sentence**,
and the second is that the design is worth adopting anyway, for a different
reason. Both are below.

---

## The design

**k runs of every item in each arm. The endpoint is the item's pass fraction.**

* Item *i* is run k times in arm A and k times in arm B, and scored by
  `evals/grading.py` exactly as it is today — mechanical checks, no model
  judgment, binary per run.
* Its endpoint is `passes / k` in each arm: a number on
  {0, 1/k, 2/k, …, 1} rather than a single Bernoulli draw.
* The paired difference is `rate_B(i) − rate_A(i)`.
* The test is over those n differences.

Everything else is unchanged: the same corpus, the same grader, the same
`RunRecord` per run, the same append-only log. `runrecord.RunRecord` already
carries a `replicate` field and `latest_per_key` already keys on
`(item, arm, replicate)`, so the harness does not need a new record shape — it
needs a loop and a summariser that averages before it tests.

---

## The test, and why it is Wilcoxon

**Wilcoxon signed-rank, one-sided, exact null.** Implemented in
`evals/stats.py::wilcoxon_signed_rank` from `math.comb`; no scipy, for the
same reason nothing else here uses it.

The choice is argued from what the per-item differences will actually look
like, not from habit:

* **They are discrete and coarse.** At k=5 an item's rate is one of six values
  and a difference is one of eleven. That is not a continuum with a
  measurement error on it; it is a small lattice.
* **They are bounded**, so the effect is compressed at both ends. A ceiling
  item at 5/5 cannot improve, which is not a nuisance to be modelled away —
  it is why the corpus is banded.
* **Most of them are exactly zero.** The corpus deliberately contains floor and
  ceiling items, and both arms score those identically nearly every time. Zeros
  are dropped by Wilcoxon and counted in the result, which is the same
  information McNemar's concordant cells carry.
* **The ties are heavy.** Six possible magnitudes over 36 items means large
  tie groups, and averaged ranks in large groups.

A paired t-test on 36 differences from that distribution is leaning on a
central limit theorem the shape does not earn, and its standard error is
computed from a variance the boundedness makes heteroscedastic — floor and
ceiling items have almost none, middling items have the most. Wilcoxon assumes
only that the differences are symmetric under the null, which a paired design
supplies by construction, and its exact null is computed by counting subsets of
the observed ranks so the ties are handled rather than approximated.

**The t-test is reported as a secondary figure**, not because it is better but
because a large disagreement between the two is a signal about the
distribution, and one worth seeing rather than hiding behind a single choice
made in advance.

Two properties of the implementation are worth knowing before quoting a
p-value, and both were found by checking rather than assumed:

* **With one distinct magnitude the signed-rank test is the sign test.** That is
  exactly what a binary endpoint (k=1) produces, and it is computed as a sign
  test at any size. The normal approximation is anti-conservative by about
  0.04 there and does not improve with n, because the statistic moves in steps
  of (m+1)/2 rather than 1.
* **The tie-corrected normal approximation plateaus rather than converging.**
  Measured over heavily tied vectors: ~0.018 at m=20, ~0.009 at m=30, ~0.008 at
  m=60 and m=100. So the exact null is used up to 60 non-zero differences —
  which covers a confirmatory study — an approximate result says so in its own
  rendering with the residual stated, and `exact=True` forces the exact
  computation for anything near α.

---

## Power and cost, side by side

**PROJECTION.** Wilcoxon has no closed form, so these are Monte Carlo over a
stated generative model, seeded to reproduce exactly. Reproduce with
`python scripts/eval_replicate_power.py`.

The model, printed with the result rather than buried: each item draws its
baseline pass probability from the **28 banded corpus items** — the only
measured item mix this project has — the candidate arm gets that plus 15 points
clipped at 1, and each arm runs the item k times independently. Two things
follow that must travel with any number below:

* **It assumes runs of one item are independent**, under which the discordant
  rate is E[2p(1−p)] and can never exceed 0.5. The measured floor is
  ψ = 0.643, *above* that ceiling. So this model is more optimistic than the
  instrument as measured, and every figure here is an upper bound.
* **Two of the 28 banded items sit at 5/5**, where an additive 15-point effect
  has nowhere to go, so the realised mean effect is slightly under 15 points.
  That is realistic and it is why the corpus is banded.

α = 0.05, one-sided, δ = 15 points, 3000 simulated studies at seed 20260904.
Cost is both arms, at the direct arm's 6.1 min and decomposition's 29.3 min.

| k | n | item-runs/arm | direct | decomposition | power at 15 pts | reachable today? |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 36 | 36 | 7 h | 35 h | 0.29 | yes — the confirmatory set as it stands |
| 2 | 36 | 72 | 15 h | 70 h | 0.55 | yes |
| 3 | 36 | 108 | 22 h | 105 h | 0.70 | yes |
| **5** | **36** | **180** | **37 h** | **176 h** | **0.88** | **yes** |
| 10 | 36 | 360 | 73 h | 352 h | 0.99 | yes |
| 1 | 100 | 100 | 20 h | 98 h | 0.65 | yes — the whole corpus, once |
| **2** | **100** | **200** | **41 h** | **195 h** | **0.93** | **yes** |
| 3 | 100 | 300 | 61 h | 293 h | 0.98 | yes |
| 1 | 187 | 187 | 38 h | 183 h | 0.89 | **no — 87 prompts that do not exist** |

---

## Which buys more resolution

**At matched inference cost: neither, or close enough that it does not decide
anything.**

Put k runs on n items beside one run on k×n items — the same number of
item-runs, the same hours — and the powers land on top of each other. k=5 on 36
items is 0.877 against 0.878 for one run on 180 items; k=3 on 36 is 0.700
against 0.689; k=3 on 100 is 0.978 against 0.979; k=2 on 100 is 0.925 against
0.911. `scripts/eval_replicate_power.py` prints this comparison as its own
column.

One row is not a tie by the script's own 0.02 threshold: k=2 on 36 items comes
out 0.547 against 0.521 for one run on 72. At 3000 trials the standard error on
each is about 0.009, so that gap is roughly two standard errors — the sort of
difference this project would not act on if a model produced it, and it is
recorded here rather than rounded into the pattern.

That is not a surprise once written down. The signal is δ per item and the
variance of a per-item difference falls as 1/k, so to first order power depends
on n × k — the total run count — and barely on how the budget is split between
them. **Replicates and items are close to the same currency.**

So the sentence in `docs/eval-methodology.md` §7 — that replicates are
"genuinely more efficient per unit of inference" — **is not supported by this
arithmetic**, and that document now says so and points here. It was a
reasonable expectation and it is wrong in the direction that matters: adopting
replicates on the strength of an efficiency that is not there would have bought
a changed endpoint and no resolution.

### The reason to adopt it anyway

**Inference cost is not the binding constraint. Item count is.**

The corpus has 100 items and the confirmatory set has 36, and those are the
sizes that exist. Detecting 15 points at the measured floor needs 187 items —
87 prompts that would have to be *written*, from the taxonomy, and then held
out. Writing them is not free, it is not fast, and every one of them is a
judgement call that a later reader has to trust.

Replicates need no new prompts. Two consequences, and they are the whole case:

* **k=2 over the existing 100 items reaches 0.93** — better than the n=200
  single-run design it costs the same as, and reachable without writing a
  single prompt.
* **On the 36-item confirmatory set, replicates are the only lever there is.**
  One run each gives 0.29 and no amount of inference changes that, because the
  held-out set cannot be grown without being redrawn, and a held-out set that
  can be redrawn is not held out. k=5 takes the same 36 items to 0.88.

That second point is the one that decides it. `split.lock.json` freezes the
confirmatory membership with a digest precisely so it cannot be extended to
suit a result, and that constraint makes replicates the only way a
pre-registered study on that set ever resolves an ordinary change.

### What it costs that the table does not show

* **The endpoint changes**, and the five committed runs are comparable with
  each other under the binary definition. A replicate study is a new series;
  it does not extend the old one. That was PR #71's reason for deferring, it
  is still true, and it is a real cost rather than a caveat.
* **Wall clock is wall clock.** k=5 on the confirmatory set is 176 hours of
  decomposition — seven days — for one comparison. The direct arm's 37 hours
  is the version anybody actually runs.
* **`scripts/eval_study_summary.py` refuses an incomplete study**, and a
  replicate study has k times more cells to lose.

---

## The interaction with pinning, stated honestly

**These two levers pull against each other, and only one measurement separates
them.**

Replicates work by averaging down *within-item* variance — the run-to-run
variation that makes the same item pass once and fail the next time. Pinning
temperature and seed is an attempt to remove that same variance at the source.
To the extent pinning succeeds, an item's k runs become k copies of one run,
the pass fraction collapses back to 0 or 1, and replicates buy nothing at all
while costing k times the inference.

So:

* **If ψ falls a lot under pinning** — the "dominant noise source" branch of
  [`noise-floor-under-pinned-sampling.md`](noise-floor-under-pinned-sampling.md)
  — then within-item variance was mostly the sampler, pinning removes it for
  free, and **the replicate design is unnecessary.** Spend the inference on a
  single-run study at the new, lower floor.
* **If ψ does not fall** — the "irrelevant" branch — then within-item variance
  is structural, cannot be configured away, and **replicates are the route**,
  because they are the only thing that averages it down inside a corpus that
  cannot grow.
* **If ψ falls partway**, both remain live and the choice is made on the
  achieved floor: read the corpus size off `scripts/eval_power.py`'s grid, and
  adopt replicates only if that size exceeds the items that exist.

**The measurement that decides is the pinned-versus-unpinned ψ comparison in
[`noise-floor-under-pinned-sampling.md`](noise-floor-under-pinned-sampling.md).**
It costs 17 hours; this design costs 37 at its cheapest and 176 at the arm a
study would use. Running the 17-hour measurement first is the correct order,
and adopting replicates before it would be spending a week to average down
noise that a config setting might have removed.

---

## What would be pre-registered when it runs

Written now so it is not written afterwards.

* **k = 5, n = 36**, the confirmatory set, at the direct arm — 37 hours, power
  0.88 against a 15-point effect under the model above. k=5 rather than k=3
  because 0.70 is not enough to act on, and rather than k=10 because 73 hours
  buys 0.11 more power.
* **Primary test:** Wilcoxon signed-rank on per-item pass-rate differences,
  one-sided, exact, α = 0.05. **Secondary:** paired t on the same differences,
  reported whatever it says.
* **Reported before the p-value, always:** n, the number of non-zero
  differences, the number of exact zeros, W+ and W−.
  `stats.render_wilcoxon` emits them in that order and there is no other
  sanctioned way to print the result.
* **What counts as no difference**, stated in advance: the candidate arm's
  per-item rates are not higher; or they are higher but p > 0.05 at the 36
  items run; or so many differences are exactly zero that the test had nothing
  to work with — which is reported as *no evidence*, not as a null result, the
  same way an all-concordant McNemar table is.
* **Stopping rule:** all 36 items, all k replicates, both arms. No interim
  looks, no extending k because the p-value is close. One whole re-run is
  permitted and is reported as a second study, never pooled.
* **The projection is retired on contact with data.** The powers above come
  from a model whose independence assumption the measured floor already
  contradicts. The realised per-item rate distribution is reported alongside
  the result, and if it differs materially from the 28-item mix used here, the
  power statement is recomputed from what was observed and both are published.

---

## What this design will not answer

* **Whether the effect is uniform across items.** An additive δ is the model's
  assumption, not a finding. A real prompt change probably helps middling items
  and does nothing at the floor and the ceiling.
* **Anything about the binary series.** A replicate study is a new endpoint and
  the five committed runs do not extend into it.
* **Whether k=5 is enough to band an item.** Banding and testing are different
  uses of the same runs, and `scripts/eval_band_corpus.py --live --trials 5`
  already exists for the first.
* **Whether the noise it averages down was worth averaging.** That is the
  pinning measurement's question, and it is the one to run first.
