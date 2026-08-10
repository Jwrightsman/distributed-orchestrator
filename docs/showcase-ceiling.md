# The showcase game is 2/10, and what actually fixed it

`--demo-showcase` asks the swarm for a playable Snake game in one self-contained
HTML file. Measured over ten consecutive runs, **two were playable**.

This documents what was tried, what the cause turned out to be, and the change
that did work — which was not a better prompt.

## What was measured

`scripts/showcase_reliability.py` runs a showcase N times and opens each result
in headless Chromium. For the game: no uncaught JS error, a canvas something
draws on, a frame that changes without input, response to arrow keys, and no
"GAME OVER" visible before play.

- **0/10** met the strict bar (playing on load)
- **2/10** are playable at all, once you press start
- The other 8 load cleanly, throw **no JavaScript errors**, and never draw
  anything. Their restart button does nothing. Frame hash stays 0.

## Two hypotheses, both tested

**"The reviewer's merge breaks working builder output."** Plausible — merge
defects are this architecture's signature failure, and every failing run
contained a builder output with all the expected mechanics present in the text
(`<!doctype`, `getContext`, `setInterval`, `keydown`, food logic).

**Tested and false.** Extracting each builder's own HTML and running it in the
browser: those are not playable either. The merge is not degrading a working
artifact — nothing working existed to degrade.

The "all mechanics present" signal was pattern-matching, not function. A file
containing the string `setInterval` tells you nothing about whether the loop it
starts does anything. This is the second time in this project that a text search
produced a confident wrong answer; running the artifact produced the right one.

**"A planner that stops fragmenting single files will fix it."** This was the
obvious next move and it got a full eval run (prompt set v5, Aug 10). The
planner change worked mechanically — mean subtasks 3.68 → 2.46 — and the
overall score did not move (16/28 vs v3's 17/28). Its apparent web_app gain,
3/6 → 5/6, is four prompts flipping out of six, p ≈ 0.63. See `prompts/v3.py`:
between any two eval runs 11–18 of 28 prompts flip outcome, so that gain is
noise. **v5 was deleted.** Reducing coupling *inside* the plan is not the fix.

## What that leaves

The failures are **semantically dead, syntactically fine** code: correct-looking
JavaScript that does not animate. Prompt set v3 already carries rules aimed at
this exact class (`the page must load with no console errors`, DOM discipline)
and these files satisfy them — they have no console errors. There is nothing
left for a prompt to say.

A one-shot, fully-working, interactive game is simply past what a 4B model does
reliably. The eval set agrees: `web_app` is 3/6 even after tuning, and the games
are the hardest thing in it.

## The fix: change the artifact, not the prompt

The previous version of this document guessed that "a simpler visual deliverable
— a clock, a chart, a CSS animation — plays to the same 'it opens in your
browser' moment with far less coupling." That guess was measured on Aug 10, same
harness, same model, same prompt set (v3), same browser checks.

| showcase | result | avg run |
| --- | --- | --- |
| `snake` — playable game | **2/10** | ~50 min |
| `clock` — animated analog clock | **3/4** | 28 min |
| `particles` — drifting particle field | **3/4** | 20 min |
| `chart` — labelled bar chart | **4/4** *(confirmation to n=10 in progress)* | 22 min |

Every alternative beat the game, and they are also roughly twice as fast.

**Two things worth keeping from this, because both contradicted a prediction:**

1. **The particle field was expected to be the most reliable and was not.** It
   has no correct answer to get wrong — any code that draws and loops looks
   right — so it should have been unfailable. It threw
   `Cannot access 'particles' before initialization`, a real temporal-dead-zone
   error, and rendered nothing. Absence of a correctness criterion does not
   protect you from the code simply not running.
2. **The clock's one failure is the camera-fatal kind.** It drew a neon rim, no
   hands, no hour markers, and a working digital readout underneath. The rim
   alone cleared the "did it draw anything" bar, and the digital text kept
   ticking, so it looked alive by two of the three obvious signals — and a
   full-pixel canvas diff proved nothing on the canvas ever changed. It was
   caught only by the frame-change check.

That second one is a checker lesson as much as a model one: when a negative
result mattered, the way to settle it was a full-pixel diff plus a screenshot,
not a subsampled hash. `scripts/showcase_rescore.py` re-scores saved artifacts
so a checker fix costs seconds instead of another run.

## What this means for the video

- **The chart can be generated live on camera.** That is the change: the money
  shot becomes "watch it produce one right now" rather than "here is what it
  produced earlier."
- **The game stays**, as `--demo-showcase` and as the honest hard case. It is
  the truthful answer to "what can't it do", and a verified-playable one is
  committed at `docs/demo-assets/snake-game/`. Do not generate it live.
- `docs/demo-script.md` carries the measured rates for both.

## If someone wants to push this further

The remaining lead is the one the eval cannot currently see. At n=28 with
11–18 prompts flipping between runs, this project cannot resolve a change
smaller than about six prompts. **No prompt set has ever been run twice**, so
prompt-change and run-to-run variance have never been separated. That repeat
run costs the same ~9 hours as any other and would tell you what the numbers
in this file are actually worth.
