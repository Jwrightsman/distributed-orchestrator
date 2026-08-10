# Handoff — Mycelium / distributed-orchestrator

_Written Aug 10, 2026, at the end of a long session. Paste the prompt at the
bottom into a new Claude Code session._

---

## Read these first, in order

1. `MASTER_PLAN.md` — direction and the launch plan
2. `SPRINT_PHASE2.md` — **the active plan and cross-session memory.** Its Session
   Log has the full history with numbers
3. `CLAUDE.md` — house rules
4. This file

## State right now

**`master` is launch-ready. Do not destabilise it.** 293 tests green, CI green,
the orchestrator is live 24/7 at `167.233.239.33:8000` (Hetzner CX33).

Everything measurable has a number:

| what | measured |
| --- | --- |
| Output quality | **61%** of 28 eval prompts runnable + on-spec (`v3` prompts, was 36%) |
| WAN overhead | 216 ms RTT Indiana→Germany; network is **~2%** of a real pitch |
| `--demo-showcase` | **2/10** playable — do not generate live on camera |
| `--demo` | **2/3** clean, and failures are loud (red panel, exit 1) |
| Restart recovery | 17/17 incl. SIGKILL |
| Soak | 60 pitches, +0.9 MB RSS, no leaks |

## Branches

- `master` — stable, launch-ready
- `experiment/showcase-quality` — holds `docs/showcase-ceiling.md` (a negative
  result, see below)
- `experiment/verification` — **current branch.** New `verification.py` +
  22 tests. Module is written and tested but **not wired into the dispatcher**

## In flight when this was written

`python evals/run_evals.py --prompt-set v5` — started Aug 10, takes ~20 h,
resumable. **Check `evals/results/` for the newest directory and compare its
success rate against v3's 17/28 (61%).**

- If v5 ≥ v3: promote it (`DEFAULT_SET` in `prompts/__init__.py`) and update the
  test in `tests/test_prompt_sets.py` that asserts which set is default
- If v5 < v3: delete `prompts/v5.py`, unregister it, record why in `v3.py`'s
  docstring. That is the rule and it has already been applied once to a v4

## The four things asked for, and their status

1. **Planner tuning (v5)** — written, running. v5 inverts one v3 planner rule
   that told it to split a single file by concern (markup/logic/behaviour). That
   line produced the showcase's three-way fragmentation of one HTML file across
   blind agents. v5 says: a tightly-coupled single file goes to ONE builder whole.
2. **Verification + reputation** — module + tests done on `experiment/verification`.
   **Next: wire it into `routes_pitch.dist_build_fn`** — duplicate a sampled task
   to a second node, call `pool.record_comparison(...)`, and use `pool.rank()`
   when choosing a node. Then surface `routing_weight` on the dashboard node cards.
3. **Org multi-tenancy** — **not started.** Design note: an org can already get a
   private pool today by running its own instance (`node_secret` gates joining;
   `docs/DEPLOY.md` covers it). What does not exist is several orgs isolated on
   ONE orchestrator — that needs per-org identity, task routing by tenant, and
   output visibility rules. Build it on a branch, not on master.
4. **Showcase alternative** — **not started.** See `docs/showcase-ceiling.md`:
   prompting cannot fix the game (2/10, failures have no JS errors and simply
   never animate; builder outputs in isolation are not playable either, so it is
   not the merge). The open idea is a *less coupled* visual deliverable — a
   clock, a chart, a CSS animation — which hits the same "it opens in your
   browser" moment with far less integration risk. That is a demo-design change,
   and it needs measuring with `scripts/showcase_reliability.py`.

## How this project works (earned the hard way)

- **Measure before and after every prompt change.** One variable at a time, or
  the result is unattributable. v3 gained 25 points; the obvious follow-up v4 lost
  22 and was deleted. Without the eval it would have shipped.
- **`evals/rescore.py` re-scores a finished run from saved artifacts** — a scoring
  fix costs seconds, not another 20 h of inference.
- **Verify negative results before acting on them.** A bad `grep -E` (using `\|`,
  which matches a literal pipe) once "proved" a working game had no game logic and
  triggered an hour of wrong work. Run the artifact; don't pattern-match it.
- **Never run tests/servers/Docker while a pipeline run is going.** 8 GB, CPU-only
  — it starves Ollama and wedges the run.
- **Windows specifics:** everything writes UTF-8 explicitly (cp1252 crashed on
  emoji); `SystemRoot` must survive into subprocess envs or sockets die with
  WinError 10106.

## Jett context

No programming experience — make the technical calls, explain in plain language,
warn before anything network-facing. He is at IU and can record the video in
roughly 1–2 weeks; that is the real bottleneck, not code.

**Open question for him:** the brand is spelled `Mycelium` (one L) throughout. He
wrote "Mycellium". Flagged, not yet confirmed.

## Secrets

Not in the repo and must never be. `node_secret` and `pitch_key` live in
`/root/distributed-orchestrator/data/config.json` on the VM, and Jett has copies.
SSH key is `~/.ssh/swarm_orchestrator` on his laptop only.

---

## Paste this into a new session

> I'm continuing work on Mycelium (the distributed-orchestrator repo,
> github.com/Jwrightsman/distributed-orchestrator). Read `HANDOFF.md` first, then
> `MASTER_PLAN.md` and `SPRINT_PHASE2.md` — the sprint file's Session Log is the
> real history and has every measured number.
>
> Short version: master is launch-ready and I don't want it destabilised. An eval
> run comparing prompt set v5 against v3's 61% was in flight — check
> `evals/results/` for the newest run, compare, and either promote v5 or delete it
> per the rule in `prompts/v3.py`'s docstring.
>
> After that, the queue is: (1) wire the verification/reputation module on
> `experiment/verification` into the dispatcher and show reputation on the
> dashboard node cards, (2) org multi-tenancy on its own branch, (3) test a
> less-coupled showcase artifact than the Snake game — see
> `docs/showcase-ceiling.md` for why the game itself is a dead end.
>
> Work on branches, keep master stable, measure every prompt change against the
> eval set before promoting it, and append to SPRINT_PHASE2.md's Session Log as
> you go. I have no programming experience — make the technical calls yourself and
> tell me only what I actually need to act on.
