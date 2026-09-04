# How the eval instrument works, and what it can actually see

`evals/README.md` says how to run the harness. This says what it measures, what
it cannot measure, and how each of those was established.

The short version, because it is the part people skip:

> The eval set has grown from 28 items to 100. At the measured noise floor, 28
> items could detect a **38-percentage-point** difference at 80% power; 100 can
> detect **21 points**. `ROADMAP.md` previously said the instrument "can't see
> anything smaller than about six prompts" — roughly 21 points. That figure was
> a rule of thumb and it was optimistic by about a factor of two.
>
> **Growing the corpus does not fully unblock prompt tuning.** Detecting a
> 15-point improvement needs 187 items and 182 hours of inference per
> comparison. That is not a corpus anyone here will run, and saying so is more
> useful than proposing one.
>
> **Every one of those figures is a function of one measured number**, the
> discordant pair rate ψ = 0.643, and δ ∝ √(ψ/n) means halving it is worth
> exactly what doubling the corpus is worth. At ψ = 0.32 the 100 items that
> already exist would detect 15 points. §7.1 prints the whole grid;
> `docs/experiments/noise-floor-under-pinned-sampling.md` is the 17-hour
> measurement that would say whether pinning the generator gets there. It has
> not been run.

---

## 1. What was audited, and what it found

Before adding anything, the existing instrument was read from the code.

**28 prompts**, in `evals/prompts.json`, six categories of four to six.

**Graded on five dimensions**, mechanical where possible: files were produced,
they parse (via the *production* checker in `extract.py`), they execute, they
are the right kind of artifact, they contain declared keywords — and then a
reviewer-model score of 4 or better.

**The statistical test was already right.** `evals/compare.py` used an exact
one-sided McNemar test on discordant pairs, computed from `math.comb`, with the
directional hypothesis argued for in its docstring. Nothing about the test
needed fixing. What was missing was power.

Five findings, in order of how much they matter:

### 1.1 "Loads without throwing" was doing the work of "runs"

For an HTML artifact, `executes` meant `browser_ok`: no uncaught JavaScript
error and a non-empty body. Under that check, **`web-snake` passed 5 times out
of 5** across the five committed runs.

`scripts/showcase_reliability.py`, asking the same model for the same artifact
on the same machine, measured a playable Snake game at **2 out of 10** — because
it also required that something got drawn, that the frame changed, and that the
arrow keys did anything.

Both numbers are in this repository. They disagree because they are different
checks. The eval's number is the weaker one, and every published `web_app`
figure carries that weakness.

### 1.2 The six-prompt figure was a rule of thumb, not a computation

`compare.py::min_detectable` answers "given this much observed churn, how
lopsided must the split be to clear α". At the observed churn of 11–18 pairs
that comes out at a net difference of about 7–8 prompts, which is where "about
six" came from.

That is a **significance threshold**, not **power**. Power asks how large a real
difference has to be before the instrument would reliably notice it, and it is
the question that decides corpus size. Computed at 80% power, n=28 detects
**38.4 points ≈ 10.8 prompts**, not six.

The rule of thumb was in the right direction and about half the size. Put
another way: six of 28 is 21 percentage points, and **21 points is what the
instrument reaches at n=100, not at n=28.** The roadmap was describing the
corpus this change builds, while believing it described the one it had.

### 1.3 The discordant rate is estimable, and it is 0.643

For a paired binary design, McNemar's power depends on the **discordant pair
rate** — the fraction of items that change outcome between two runs — not on the
total item count directly. An item both arms pass and an item both arms fail
contribute nothing.

The project already had the right data for this without knowing it: prompt set
v3 was run twice, on Aug 8 and Aug 11, with nothing changed. **18 of 28 items
changed outcome** (ψ = 0.643, 95% CI 0.46–0.79). That is the noise floor
`evals/README.md` already published; it is also, exactly, the parameter every
power number in this document depends on.

Across all ten pairs of committed runs the rate ranges 0.32 to 0.64. The
identical-configuration pair is the one used, because every other pair confounds
run-to-run noise with the difference between prompt sets.

### 1.4 Dropping the model judge costs no power

The judge was a plausible suspect for the noise. It is not.

Recomputing all five committed runs with the judge gate removed moves the mean
discordant rate from **0.521 to 0.514**, and leaves the identical-configuration
pair at 0.643 exactly. The noise is in the generator, not in the grader.

The judge was removed from the primary endpoint anyway, on validity grounds
rather than power grounds — see §4 — but the honest report is that removing it
bought nothing.

### 1.5 Nothing checked that the instrument could detect anything

There were no controls. The harness had never been shown a deliberately
degraded arm, and had never been run twice against itself to check that it did
not invent differences. §6.

---

## 2. The task taxonomy

New items are written **from** this list. This matters more than it sounds: a
corpus grown by adding prompts that resemble things the system currently fails
measures whether those particular failures were fixed, not general capability.

The list comes from ROADMAP §4's "narrow the workload claim" — bounded work with
cheap verification — plus the shapes the original corpus already covered.

| taxonomy | what it is | items |
| --- | --- | --- |
| `structured_extraction` | pull declared fields out of messy text | 9 |
| `data_transformation` | deterministic input to output | 15 |
| `test_generation` | write tests against a stated specification | 9 |
| `static_analysis` | scan and report over source text | 8 |
| `synthetic_data` | generate data satisfying declared validators | 9 |
| `batch_classification` | label items by stated rules | 8 |
| `interactive_artifact` | a self-contained page the user opens | 14 |
| `service_endpoint` | a small HTTP service | 12 |
| `algorithmic_kernel` | a self-contained computation | 12 |
| `underspecified_request` | deliberately vague pitches | 4 |

Every item records its `origin`:

* `taxonomy` — written from the list above. The only origin a confirmatory item
  may have; `evals/corpus.py::confirmatory_items` raises otherwise.
* `observed_failure` — written because something broke. Permitted, marked, and
  development-only.
* `legacy` — the original 28. They predate the distinction and have been
  iterated against for four prompt-set versions, so they are development-only
  regardless of how they were written.

The original 28 keep their ids, tasks and categories unchanged, so the five
committed runs remain comparable with anything run against them in future.

---

## 3. Difficulty bands

Power comes from items where the arms can differ. Bands:

| band | pass rate | what it is for |
| --- | --- | --- |
| `floor` | 0–20% | catching a breakthrough |
| `discriminating` | 20–80% | where all the power is |
| `ceiling` | 80–100% | catching a regression |

The project's two published data points are one of each: Snake at 2/10 is floor,
the labelled chart at 10/10 is ceiling.

**Current distribution: 20 discriminating, 5 ceiling, 3 floor, 72 unbanded.**

The 28 banded items were banded from the five committed runs — real recorded
data, five observations each — and the band record says so, including that those
runs used four different prompt sets rather than one fixed arm. It is labelled
`provisional` in the corpus for that reason.

All six banded `web_app` items now carry `known_suspect: true` and a `grader`
of `legacy (pre-correction)` in their band records, because §1.1 reaches every
one of them: the evidence was computed when an HTML artifact "executed" if it
loaded without throwing. That is a stronger statement than `provisional`, it
sits on the item rather than in this document, and
`tests/test_eval_corpus.py` fails if any HTML band loses it.

**`web-snake` is banded `ceiling` at 5/5, and that band is wrong.** It is what
the legacy grading says, computed correctly from the recorded data, and it is
committed unchanged so the band and its evidence match. The same artifact under
the behavioural checker is 2/10 — a floor item. This is §1.1 showing up as a
concrete, visible defect in the corpus rather than as an argument, and it will
correct itself the first time the item is banded with `--live` under the current
grader. It is left in place, labelled, rather than hand-edited to the number we
believe: a band nobody can trace to a run is worse than a band that is wrong for
a recorded reason.

**The 72 new items are unbanded and honestly marked as such.** Banding requires
running them: `scripts/eval_band_corpus.py --live --trials 5` costs about five
trials × 6.1 minutes × 72 items ≈ **37 hours**, or 18 hours for the confirmatory
36 alone. That was not spent in this change, and inventing bands for items that
have never run would be exactly the kind of number this project does not
publish.

Banding is reproducible for a fixed set of outcomes —
`tests/test_eval_corpus.py` pins the classification — so a band cannot drift to
suit a result.

---

## 4. Grading

**Grading validity dominates everything else.** A larger corpus graded loosely
is worse than a small one graded correctly, because it produces confident wrong
answers faster.

### The rules

1. **Mechanical wherever possible.** Code must run. JSON must validate against a
   committed schema. A transform must produce the expected output. ROADMAP §2
   says a negative result is verified by running the artifact; the same applies
   to a positive one.

2. **Deterministic given an artifact.** The same files graded twice give the
   same verdict, and `tests/test_eval_grading.py` asserts it — for a passing
   artifact and a failing one. The one exception is `html_behaviour`, which
   drives a real browser over a timed animation; it reports
   `deterministic: False` in its own result so no caller can mistake it for a
   repeatable check.

3. **No model-judged primary endpoint.** A judge is a second correlated
   probabilistic system, and a judge sharing a family with the generator is
   worse than that. `judge_score` is still collected and recorded, labelled
   `exploratory`, and gates nothing a study reports. The historical `success`
   field keeps the judge gate unchanged so `compare.py` can still place a new
   run beside the five committed ones — and only for that.

4. **No rubrics, therefore no inter-rater agreement figure.** Every item in the
   corpus is graded mechanically. No rubric-graded item exists, so no agreement
   figure is reportable and none is reported. If one is ever added, the rubric
   is written and committed *before* the items are graded, two independent
   gradings of a sample are compared, and the agreement is published whatever it
   comes out at.

5. **No partial credit.** The endpoint is binary. A partial-credit scale would
   have to be pre-registered before any analysis, and none has been.

6. **Ungraded is not failed.** A check that could not run — no browser, a
   missing fixture, an unknown check kind — is recorded as ungraded.
   `scripts/eval_study_summary.py` refuses to compute a statistic over a study
   containing one. Silently scoring an unrun check as a failure is how an
   instrument reports a result it did not measure.

### The checks

Every item gets `parses`, `artifact_kind`, `keywords` and `runs`. Items declare
further checks in `expect.checks`:

| check | what it does |
| --- | --- |
| `parses` | the production checker from `extract.py`, so the eval is never kinder than the pipeline |
| `artifact_kind` | asked for HTML, got Python |
| `keywords` | case-insensitive substrings. **A weak proxy**, kept because it cheaply catches the wrong artifact, and never an item's only check |
| `runs` | Python in a subprocess with the item's fixture inputs present; HTML in a real browser |
| `stdout_contains` | run it, require substrings in what it printed |
| `stdout_json_schema` | run it, parse stdout as JSON, validate against a committed schema |
| `html_behaviour` | load it and require it to *do* something: a canvas drawn on, a frame that changes, response to keys, expected text visible, forbidden text absent |

`html_behaviour` is generalised from `scripts/showcase_reliability.py` — the
checker that produced the published 2/10, and the one that caught a clock which
drew a neon rim, no hands, and a working digital readout underneath. "No console
errors" called that a pass.

The committed schemas set `minItems: 1` deliberately. A script that extracts
nothing and prints `[]` is valid JSON of the right outer type, and a looser
schema would score it as a pass — the vacuous pass this whole instrument exists
to prevent.

### Fixture inputs

Items that transform data name fixture files under `evals/fixtures/inputs/`,
which are copied into the scratch working directory before the artifact runs.
This lets a task be phrased the way a user would phrase it ("read sales.csv and
print the totals per region") and still be graded on what it printed.

The execution check gets the same fixtures. Running a script that was asked to
read `sales.csv` in a directory with no `sales.csv` scores a correct program as
broken — the same shape of mistake as the missing Windows environment variable
that once failed three working API servers.

---

## 5. The held-out split

**64 development, 36 confirmatory.** Committed in `evals/split.lock.json` with a
SHA-256 digest of the confirmatory membership, checked by
`tests/test_eval_corpus.py`.

The rule, fixed before the assignment was made: new taxonomy-written items are
numbered within their taxonomy in the order they were written, and the
odd-numbered ones are confirmatory. The original 28 and the
`underspecified_request` family are development-only — the first because they
have been iterated against, the second because an underspecified prompt has no
mechanical grading contract to hold a confirmatory endpoint to.

**Why this exists.** A held-out set that can be redrawn is not held out. This
project kept prompt set v5 alive for a session on the strength of `web_app`
going 3/6 to 5/6 — a subgroup that looked good after the fact. The digest makes
redrawing the confirmatory set an edit somebody has to make in a diff, next to a
reason.

The development set is for harness debugging and for any future prompt
iteration. The confirmatory set is for pre-registered runs and nothing else.
`evals/run_evals.py --split confirmatory` refuses to run if the lock has drifted.

**36 is not a round number.** `stats.required_n(0.35, 0.643)` is 35: the
pre-registered decomposition study is powered for the ~35-point gap its pilot
suggests, and nothing smaller.

---

## 6. The controls

Every measurement failure in this repository's recent history has been an
instrument reporting something that was not there, or reporting nothing while
appearing to work. So the controls were built before the corpus work, and they
run in CI against a 16-item fixture corpus with a stubbed model — no Ollama, no
network, no live inference.

Reproduce with `python scripts/eval_controls.py`.

### Positive control: a degraded arm must be detected

Two degraded arms, against the same baseline draw so the pairing is real:

* **`truncated`** — output cut off at 45% of its length. The crude failure.
* **`shuffled`** — the arm answers a shuffled prompt-to-task pairing: valid
  output, cleanly executed, for the wrong question. The failure a syntax check
  cannot see.

Measured, at seed 20260903 over 16 fixture items:

```
POSITIVE CONTROL - degraded arm: truncated
default arm passed 11/16, truncated arm passed 0/16

                    default pass  default fail
    truncated pass           0          0
    truncated fail          11          5

  discordant : 11 of 16 (69%, 95% CI 44%-86%)
  McNemar exact  one-sided p = 0.0005   two-sided p = 0.0010
  DETECTED: True

POSITIVE CONTROL - degraded arm: shuffled
default arm passed 11/16, shuffled arm passed 0/16

                    default pass  default fail
    shuffled pass            0          0
    shuffled fail           11          5

  discordant : 11 of 16 (69%, 95% CI 44%-86%)
  McNemar exact  one-sided p = 0.0005   two-sided p = 0.0010
  DETECTED: True
```

### The same artifacts, graded by parse-and-run alone

This is the measurement that justifies output-level grading, and it is the most
useful thing the controls produce:

```
truncated, judged by parses+runs only:   5 discordant, p = 0.0312   detected
shuffled,  judged by parses+runs only:   0 discordant, p = 1.0000   NOT detected
```

**An instrument that checks only "it parsed and it ran" sees the shuffled arm as
identical to the baseline — 16 out of 16, no discordant pairs, nothing to
report.** That is the same weakness as §1.1's `browser_ok`, isolated and
measured.

The truncated arm is only barely visible to the weak instrument too: five
discordant pairs, all one way, p = 0.0312 — and `min_detectable(4)` is None, so
one fewer would have been invisible.

A test in `tests/test_eval_grading.py` records a related finding: a truncation
that happens to land on syntactically valid code (`def report():` followed by a
bare `return`) parses *and* exits cleanly, and is caught only by looking at the
output.

### Negative control: identical configurations must not differ

Two runs of the default arm at different seeds — different draws, not a replay,
because replaying one seed would pass trivially:

```
NEGATIVE CONTROL - the same configuration, twice
                    run 20260904 pass  run 20260904 fail
    run 20260903 pass           5          6
    run 20260903 fail           4          1

  discordant : 10 of 16 (62%, 95% CI 39%-82%)
  McNemar exact  one-sided p = 0.8281   two-sided p = 0.7539
  DIFFERS: False
```

One non-significant negative control is close to no evidence: at α = 0.05, one
draw in twenty is significant by construction. So the control is run over many
identical-configuration pairs and the **false-positive rate** is what is checked:

```
Over 20 identical-configuration pairs, 0 came out significant at alpha=0.05
(0%, 95% CI 0%-16%).  Expected under a correct noise model: about 5%.
```

### Non-vacuity

Every control asserts its own preconditions before its result counts: the corpus
was non-empty, every item was graded, no item was silently skipped, and the
grader returned both outcomes at least once. Each of those is a separate test in
`tests/test_eval_controls.py`, with a test that the guard actually fires.

Four test files in this repository have passed on zero inputs, and a property
campaign passed for a week while three of its rules were structurally
unreachable. The guards are not paranoia.

---

## 7. The power curve

Reproduce with `python scripts/eval_power.py`. ψ = 0.643, α = 0.05, 80% power,
one-sided McNemar exact. Cost from the measured mean of 29.3 minutes per item
over 140 recorded item-runs.

| n | detectable effect | = items | power at 15 points | one run | a comparison |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 28 | 38.4% | 10.8 | 0.19 | 14 h | 27 h |
| 40 | 32.5% | 13.0 | 0.26 | 20 h | 39 h |
| 50 | 29.0% | 14.5 | 0.32 | 24 h | 49 h |
| 60 | 26.5% | 15.9 | 0.37 | 29 h | 59 h |
| 80 | 23.0% | 18.4 | 0.46 | 39 h | 78 h |
| **100** | **20.6%** | **20.6** | **0.55** | **49 h** | **98 h** |
| 120 | 18.8% | 22.6 | 0.62 | 59 h | 117 h |
| 160 | 16.2% | 26.0 | 0.74 | 78 h | 156 h |
| 200 | 14.5% | 29.0 | 0.82 | 98 h | 195 h |
| 300 | 11.8% | 35.4 | 0.94 | 146 h | 293 h |

### The target effect, stated so it can be argued with

**15 percentage points** — a change that takes the suite from 57% to 72%.

It is deliberately smaller than v1 → v3, which moved 10/28 to 17/28: seven
prompts, **25 points**, and the best prompt change this project has ever made.
A target set at the size of the best change ever made is a target that only
catches lightning twice. Fifteen points is the size of an ordinary good change,
which is the thing prompt tuning needs to be able to see.

**Detecting it at 80% power needs n = 187, which is 91 hours for one run and
182 hours for a comparison.** Seven and a half days of continuous CPU on the
reference machine, per comparison, to answer one prompt question.

**That corpus is not proposed, because nobody will run it.** Saying so is the
useful output of this calculation.

### Why 100

100 items is what a comparison can plausibly be paid for: 49 hours a run, 98 for
a paired comparison — call it four days. It detects **20.6 points**, which is
better than 38.4 and is still not 15.

So the honest statement, which contradicts the roadmap's framing and is recorded
here for that reason: **growing the corpus improves the instrument substantially
and does not unblock fine prompt tuning.** A change worth 15 points remains
invisible at any corpus size this project will run. What 100 items buys is the
ability to resolve a *large* change — an architecture swap, a model change, a
prompt rewrite that moves a fifth of the set — without the four-day cost being
wasted on a question the instrument could never have answered.

The confidence interval on ψ is worth stating too, since everything above rests
on it. At the low end (ψ = 0.46) a 15-point effect needs 135 items; at the high
end (ψ = 0.79) it needs 227. The estimate comes from a single pair of runs, and
a second identical-configuration pair would be the cheapest way to tighten it.

### 7.1 The floor is a parameter, so the curve is printed as one

Everything above is computed at one value of ψ. The whole table moves when that
value does, and it moves in a way the closed form makes exact: the minimum
detectable effect goes as **δ ∝ √(ψ / n)**, so **halving ψ is worth exactly what
doubling n is worth**. `scripts/eval_power.py` therefore prints a second table —
the same corpus sizes crossed with a range of noise floors — so that when a
floor is measured, the corpus size it implies falls out rather than being
re-derived.

The default output stays pinned to the measured ψ = 0.643, and every cell at
any other rate is starred and labelled a projection.
`stats.format_psi_cell` raises rather than printing a projected value unmarked,
because this is precisely the kind of table a later reader quotes one cell from.

| noise floor | detectable at n=100 | items needed for 15 points |
| --- | ---: | ---: |
| ψ = 0.643 — **measured** | 20.6 points | 187 |
| ψ = 0.5 — projection | 18.2 points | 148 |
| ψ = 0.4 — projection | 16.3 points | 118 |
| ψ = 0.32 — projection | **14.5 points** | **95** |
| ψ = 0.25 — projection | 12.9 points | 74 |

At ψ = 0.32 — half the measured floor — the 100-item corpus that already exists
detects 15 points at 82% power, and the conclusion above stops being true. That
is the entire reason the next paragraph matters.

**Why 0.5 is on the list, and why it is not a round number.** If two runs of one
item are independent draws from that item's pass probability p, the item is
discordant with probability 2p(1−p), which is at most 0.5 for any p. So
ψ = E[2p(1−p)] ≤ 0.5 for **any** mix of items, and the measured 0.643 sits above
the ceiling that any independent-runs model can produce. That is consistent with
noise — P(X ≥ 18 | n = 28, ψ = 0.5) ≈ 0.09 — and its 95% interval (0.46–0.79)
covers 0.5 comfortably. **This is arithmetic on a model, not a measurement, and
it does not replace the 0.643.** What it says is narrower and it matters for
sizing the next study: a re-measured floor should be expected to come in below
0.643 before anything is done to it, so a study that pins the generator needs an
unpinned control arm rather than a comparison against the historical number.

**What could lower it.** `config.json` had no temperature and no seed, so every
run this project has made took Ollama's own defaults. That is now settable
(`sampling.py`), and whether it accounts for any of the 0.643 is unmeasured.
[`docs/experiments/noise-floor-under-pinned-sampling.md`](experiments/noise-floor-under-pinned-sampling.md)
is the pre-registered design that measures it: two identical-configuration
pairs over a 37-item subset, one pinned and one not, about 17 hours at the
direct arm. It has not been run.

### The other lever, costed since

The alternative to more items is more replicates per item, scoring each item by
its pass *fraction* rather than a single Bernoulli draw, and testing on a
continuous endpoint. This document used to say that was "genuinely more
efficient per unit of inference".

**That was an expectation, and the arithmetic does not support it.**
[`docs/experiments/replicate-endpoint-design.md`](experiments/replicate-endpoint-design.md)
pre-registers the design and costs it: at matched inference cost, k runs on n
items and one run on k×n items come out with the same power, every time. The
signal is delta per item and the variance of a per-item difference falls as
1/k, so power depends on the total run count n×k and barely on how it is split.
Replicates and items are close to the same currency.

**What replicates do buy is resolution inside a corpus that cannot grow.**
Detecting 15 points with single runs needs 187 items — 87 prompts nobody has
written. The confirmatory set is 36 and frozen by a digest, so k is the only
lever a pre-registered study on that set has: one run each gives power 0.29,
and k=5 gives 0.88 on the same 36 items. That, and not efficiency, is the case
for it.

It still changes the endpoint, and this instrument's value comes partly from
five committed runs that are comparable with each other under the binary
definition, so a replicate study is a new series rather than an extension of
the old one.

**And it is not the first thing to do.** Replicates average down within-item
variance; pinning the generator's temperature and seed tries to remove that
same variance at the source, for free.
[`docs/experiments/noise-floor-under-pinned-sampling.md`](experiments/noise-floor-under-pinned-sampling.md)
measures which, in 17 hours, and it is the measurement that decides between the
two designs. Adopting replicates before running it would be spending a week
averaging down noise a config setting might have removed.

---

## 8. What every run records

`evals/runrecord.py`, append-only, one JSON object per line in `runs.jsonl`
beside the existing `results.jsonl`:

corpus version and digest · item id, band, split, taxonomy · arm · strategy and
its configuration · model provider, name and digest · descriptor hash ·
temperature and seed · wall clock and token cost · artifact SHA-256 · grading
method, grader version and outcome · timestamp.

Three properties worth naming:

* **Absent facts are recorded as unknown, never inferred** — the convention the
  provenance envelope uses (ADR 0017), for the same reason: a run that could not
  determine the model digest and a run whose digest matched are different
  situations, and only one supports a comparison.
* **A weak fact is recorded as weak.** `config.json` can now pin `temperature`
  and `seed`, and by default sets neither, so a run still takes Ollama's own
  0.8 and 0 and records both as unknown. When a seed *is* set, the record
  distinguishes the seed reaching the request — established, and asserted
  against the outbound body by `tests/test_sampling.py` — from the runner
  honouring it, which nothing here has measured. A seeded run reports
  `sampling_pinned: false` and lists `model_seed_honoured` as unknown until it
  does. See `sampling.py`.
* **The envelope is reused where there is one.** A run through the normal
  execution path already binds its artifacts to a provenance envelope; that
  digest goes in the record rather than being duplicated.
* **Append-only.** A re-run never overwrites an earlier one. The summariser
  reports how many records were superseded, so a re-run is never invisible.

---

## 9. Reading a result

`scripts/eval_study_summary.py` computes the pre-registered test and prints the
counts and the statistic together. It will not print a p-value on its own, and
it refuses outright when an item is missing from an arm or was not fully graded.

`evals/stats.py::render_paired` is the only sanctioned way to print a paired
result, and it always emits the 2×2 table, n, the discordant count with its
interval, and the power statement, before the p-value. A bare p-value is the
shape of every measurement mistake this project has already published.

The four cells of the table are not decoration. `both_pass` and `both_fail` are
what say whether the corpus had any resolving power at all: a comparison where
every item lands in those two cells produced no evidence, however small the
p-value on the two that moved.
