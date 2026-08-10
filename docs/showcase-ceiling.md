# Why the showcase game is 2/10, and why prompting won't fix it

`--demo-showcase` asks the swarm for a playable Snake game in one self-contained
HTML file. Measured over ten consecutive runs, **two were playable**. This
documents what was tried, what the cause turned out to be, and why the answer is
a workflow change rather than a better prompt.

## What was measured

`scripts/showcase_reliability.py` runs the demo N times and opens each result in
headless Chromium, checking: no uncaught JS error, a canvas something draws on,
a frame that changes without input, response to arrow keys, and no "GAME OVER"
visible before play.

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

## What that leaves

The failures are **semantically dead, syntactically fine** code: correct-looking
JavaScript that does not animate. Prompt set v3 already carries rules aimed at
this exact class (`the page must load with no console errors`, DOM discipline)
and these files satisfy them — they have no console errors. There is nothing
left for a prompt to say.

A one-shot, fully-working, interactive game is simply past what a 4B model does
reliably. The eval set agrees: `web_app` is 3/6 even after tuning, and the games
are the hardest thing in it.

## The answer

Do not generate the showcase live. It is a **"here is what it produced"** shot,
not a **"watch it produce one"** shot:

- A verified-playable game is committed at `docs/demo-assets/snake-game/`
- `docs/demo-script.md` carries the measured rates and the recording workaround
- Generating several and picking one is legitimate — the swarm genuinely made it

This is honest framing, not a workaround for a broken claim. The deliverable is
real. What is unreliable is one-shot success, and the video never has to assert
otherwise.

## If someone wants to try anyway

Cheapest experiment with any chance: shrink the ask. A single-file game is a
large, tightly-coupled artifact and this architecture splits work across agents
that cannot see each other. A simpler visual deliverable — a clock, a chart, a
CSS animation — plays to the same "it opens in your browser" moment with far
less coupling. That is a demo-design change, not a prompt change, and it is
where the remaining leverage is.
