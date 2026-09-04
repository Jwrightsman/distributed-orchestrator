# Eval harness

Measures whether the swarm actually produces **runnable, on-spec output** — the
number SPRINT_PHASE2 §1 tunes the planner/builder/reviewer/reviser prompts
against. Without it, prompt changes are guesswork.

> **Read [`docs/eval-methodology.md`](../docs/eval-methodology.md) before
> drawing a conclusion from anything here.** It carries the power analysis, the
> controls, the difficulty bands and the held-out split, and it corrects two
> figures this file used to imply. The short version: the corpus is 100 items,
> the instrument detects a **20.6-point** difference at 80% power (not the
> ~6 prompts §4 of the roadmap claimed at n=28, which was a rule of thumb and
> optimistic by about a factor of two), and a 15-point change needs 187 items
> and 182 hours per comparison — so it is still out of reach.
>
> Two things in this file describe the **legacy** endpoint. `success` still
> includes the model-judge gate, kept so new runs stay comparable with the five
> committed ones. The **primary** endpoint is `primary_pass`: mechanical checks
> only, no model judgment, and a stricter HTML check. See "Two endpoints" below.

## Running it

```bash
# Full set against the real pipeline. Hours on CPU — safe to interrupt.
python evals/run_evals.py

# Continue where an interrupted run left off
python evals/run_evals.py --resume 20260806_101500
python evals/run_evals.py --resume 20260806_101500 --retry-failed   # also re-run errors

# Smaller slices while iterating on a prompt
python evals/run_evals.py --only web_app
python evals/run_evals.py --id web-snake --id cli-todo
python evals/run_evals.py --limit 5

# Run the pitches on another machine that has the model
python evals/run_evals.py --orchestrator http://1.2.3.4:8000 --pitch-key KEY

# Plumbing self-test — no Ollama, finishes in seconds
python evals/run_evals.py --fake
```

Ollama must be running for a local run (`python status.py` to check). Each
prompt is written to disk the moment it finishes, so a crash or a Ctrl-C never
costs more than the run in flight.

## Budget the time before you start

This is the thing to plan around: at the measured **29.3 minutes per item**
(mean over the 140 recorded item-runs; median 20.0), **a full local run of the
100-item corpus is about 49 hours** — each item a planner call, 3–5 builder
calls and a reviewer call that re-emits the whole deliverable. A *comparison*
is two of those. The original 28-item set was 14 hours; growing the corpus
bought resolving power and cost proportionally.

Three ways to make the loop usable, in order of what to reach for:

1. **Iterate on a slice, confirm on the full set.** `--split development`
   keeps the confirmatory 36 unspent. `--only web_app` is now 14 items,
   `--taxonomy interactive_artifact` is the same family more precisely, and
   `--band discriminating` is the slice where a paired test actually has power.
   `--id a --id b --id c` is faster still. Keep the change if the slice moves,
   then pay for a full run before believing it — and remember a slice this size
   cannot reach significance on its own.
2. **Run it somewhere else.** `--orchestrator http://host:8000 --pitch-key KEY`
   pitches to a running orchestrator instead of the local pipeline, so a
   24/7 server or a spare desktop does the work while your laptop stays free.
   The deliverable comes back as text and is re-extracted and scored locally,
   so the numbers mean exactly the same thing.
3. **`--concurrency N`** puts N pitches in flight at once. Leave it at 1 for a
   single local Ollama — parallel requests just contend for the same CPU. Raise
   it only when the far end can genuinely serve them.

`--no-judge` skips the model-judgment step, which matters when the scoring
machine has no Ollama (a remote run scored from a laptop, for instance). It
produces a **weaker, mechanical-only score**, and the summary says so. Never
compare a `--no-judge` number against a judged one.

## Two endpoints, and which one to quote

| | `primary_pass` | `success` (legacy) |
| --- | --- | --- |
| Model judgment | none | requires judge >= 4 |
| HTML "executes" | loaded, drew, changed, responded (per the item's declared behaviour) | loaded without throwing |
| Used by | the pre-registered studies, `scripts/eval_study_summary.py` | `compare.py`, for continuity with the five committed runs |

`judge_score` is still collected and recorded on both. It is labelled
`exploratory` and gates nothing a study reports. Removing it from the gate was
a validity decision, not a power one: recomputing the five committed runs
without it moves the discordant rate from 0.521 to 0.514.

**The HTML difference is not cosmetic.** Under the legacy check, `web-snake`
passed 5 of the 5 committed runs. `scripts/showcase_reliability.py`, asking the
same model for the same artifact, measured a playable Snake at 2 of 10. Both
numbers are in this repository and they disagree because they are different
checks.

## Controls, bands and the held-out split

```bash
python scripts/eval_controls.py       # does the instrument detect anything? (no model needed)
python scripts/eval_power.py          # what can it see, and what a lower noise floor would buy
python scripts/eval_replicate_power.py  # k runs on n items, against one run on k*n items
python scripts/eval_band_corpus.py --from-results   # difficulty bands from the committed runs
python scripts/eval_study_summary.py <study_dir> --paired <arm_a> <arm_b>
```

None of those runs a model. The six banded `web_app` items carry
`known_suspect: true`, because their evidence predates the HTML execution
check being corrected — treat any `web_app` band as an upper bound until it is
re-banded with `--live`.

`evals/split.lock.json` freezes the 36-item confirmatory set with a digest, and
`tests/test_eval_corpus.py` fails if it moves. **The confirmatory set is for
pre-registered runs only.** Iterate on `--split development`.

## What gets scored

Five dimensions per prompt, mechanical wherever possible:

| Dimension | How |
| --- | --- |
| Files produced | Did the extractor write anything at all |
| Parses | `extract.check_code_files` — the *production* checker, so the eval is never kinder than the pipeline |
| Executes | Python: run the entrypoint in a subprocess. HTML: load it in headless Chromium and fail on uncaught JS errors, falling back to structural checks when no browser is present |
| Judged | Reviewer model rates 1–5 on "does this satisfy the request" |
| Cost | Wall-clock seconds and subtask count |

A prompt counts as a **success** only when it produced files, they parse, they
run, they are the *kind* of artifact requested, they contain the declared
keywords, and the judge scored ≥ 4. Beautiful prose with no runnable file
scores zero — that is the whole point, and it is the failure mode this
architecture actually has.

The per-run `summary.md` also reports **where** runs died (no files, parse
failure, wrong artifact kind, missing keywords, judged below bar), which is
where prompt tuning should aim.

## Reading the results

`evals/results/<run_id>/` holds:

- `results.jsonl` — one record per prompt, written as it completes
- `summary.json` — machine-readable rollup
- `summary.md` — the table to compare runs by, and the one worth committing

## Tuning loop

1. Run the full set, commit the result as the baseline.
2. Change **one** prompt (planner, builder, reviewer or reviser).
3. Re-run — `--only <category>` first for a fast signal, then the full set.
4. **Decide with `evals/compare.py`, not by eye.** Keep the change only if it
   moves the score by more than the harness's own noise. A change that "feels
   better" but does not move the number is not an improvement.

```bash
python evals/compare.py <baseline_run_id> <candidate_run_id>
python evals/compare.py            # the two most recent runs
```

Target: **≥80% success on `qwen3.5:4b`**.

### Do not trust a difference you have not tested

This set is small and the model is stochastic, and the project has already made
one wrong call by reading two `summary.json` files side by side. `compare.py`
reports the things that actually decide it:

- **Churn** — how many prompts flipped *in each direction*. Two runs can both
  score 17/28 and disagree on fourteen prompts. A success-rate diff hides this
  completely, and it is the single most important number on the page.
- **Exact McNemar** on the discordant pairs, one-sided (a candidate is only ever
  promoted on evidence of improvement, so the hypothesis is directional).
- **Power** — how lopsided the flips had to be to mean anything at all.

Two consequences worth knowing before you argue about a result:

- **A category is never sufficient on its own.** Categories here have 4–6
  prompts, and with four discordant pairs *no split reaches p<0.05, not even
  4–0*. "But `web_app` improved" is not evidence. That is exactly the reasoning
  that kept prompt set v5 alive for a session before it was deleted.
- **v1 → v3, this project's best change, is p = 0.033** — and only because the
  test is one-sided. It barely cleared. Nothing subtler than that has ever been
  resolvable here.

### The noise floor, measured

v3 was run against **itself** on Aug 11 — same prompts, same model, same
machine, nothing changed:

```
python evals/compare.py 20260808_050610 20260811_052310

run 1: 17/28 (61%)     run 2: 15/28 (54%)
8 improved, 10 regressed
CHURN: 18 of 28 prompts changed outcome, with no cause
```

**Two identical runs disagree on 64% of the set.** That is higher churn than any
prompt-set comparison this project has ever produced (v3→v4 was 14, v3→v5 was
15), which means those differences are entirely consistent with dice.

Consequences, and they are not subtle:

- **A net difference of ≤2 prompts is noise.** Measured, not estimated.
- **Per-category numbers are worse than useless.** `api` went 3/4 → 0/4 and
  `vague` went 2/4 → 4/4 between two runs of the same prompts.
- **The headline score is a range, not a number.** Pooling both runs gives
  32/56 ≈ 57%, 95% CI 44–69%. That is what the README publishes.
- **Only v1 → v3 has ever cleared the bar** (one-sided p=0.033), and it needed 9
  of its 11 flips to go one way. It got exactly 9.

To resolve anything smaller, the sample has to grow — more prompts, or the same
prompts repeated and averaged. Another single 28-prompt run cannot answer a
question this instrument has already been shown unable to see.

**That noise floor is now doing more work than it looks.** ψ = 18/28 = 0.643 is
the discordant pair rate, and for a paired binary design it is what McNemar's
power depends on — not the item count directly. Every figure in
`docs/eval-methodology.md` rests on it, and it comes from a single pair of runs
(95% CI 0.46–0.79). A second identical-configuration pair is the cheapest way to
tighten it, and worth more than most prompt experiments.

**And lowering it is worth exactly what growing the corpus is worth**, since
the detectable effect goes as √(ψ/n). `config.json` can now pin `temperature`
and `seed` — both unset by default, so nothing about a normal run changes —
and whether that lowers ψ is unmeasured.
`docs/experiments/noise-floor-under-pinned-sampling.md` pre-registers the
17-hour measurement; `python scripts/eval_power.py` prints what each possible
answer would buy, with every unmeasured floor marked as a projection.

## Notes

- **Executing model output.** Scoring runs generated code in a subprocess with
  a scrubbed environment, a scratch working directory and a hard timeout. That
  is a speed bump, not a sandbox. Use `--no-exec` for prompt sets you did not
  write.
- **Browser checks are optional.** With Playwright installed, HTML is loaded in
  real Chromium and uncaught JS errors fail the run (`browser_ok` in the
  results). Without it, only structure is checked (`static_ok`). The outcome
  field always records which one ran, so a summary never implies a browser
  check that did not happen. Set `EVAL_CHROMIUM_PATH` if Chromium lives
  somewhere Playwright cannot find.
- **Scripts wanting stdin** are counted as running — they started fine and only
  stopped for lack of a human. The outcome is labelled `needs_stdin`.
- **A missing third-party import** counts as a failure (`missing_dependency`),
  since a deliverable that cannot run on a clean machine is not runnable.
