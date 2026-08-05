# Eval harness

Measures whether the swarm actually produces **runnable, on-spec output** — the
number SPRINT_PHASE2 §1 tunes the planner/builder/reviewer/reviser prompts
against. Without it, prompt changes are guesswork.

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

This is the thing to plan around: **a full local run on CPU is on the order of
15–25 hours** (28 prompts, each a planner call, 3–5 builder calls and a
reviewer call that re-emits the whole deliverable). That is fine once for a
baseline, but §1.2 wants a re-run after *every* prompt change, and that does
not fit in a sprint if each iteration costs a day.

Three ways to make the loop usable, in order of what to reach for:

1. **Iterate on a slice, confirm on the full set.** `--only web_app` is six
   prompts (~3-5 hours) and covers the category the demo depends on.
   `--id a --id b --id c` is faster still. Keep the change if the slice moves,
   then pay for a full run before believing it.
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
4. Keep the change if the score moved up; revert it if it did not. A change
   that "feels better" but does not move the number is not an improvement.

Target: **≥80% success on `qwen3.5:4b`**.

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
