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

## The v5 result — finished, and it is the most useful finding here

`evals/results/20260810_041455`. **v5 = 16/28 (57%) vs v3's 17/28 (61%)** — one
prompt behind, which is noise at n=28. The category split is the real result:

| category | v3 | v5 |
| --- | --- | --- |
| **web_app** | 3/6 | **5/6** |
| **data_processing** | **3/5** | 1/5 |

Mean subtasks fell 3.86 → 2.46, so the change landed as intended: the planner
stopped fragmenting single files. Tightly-coupled deliverables (web apps)
improved more than any single-category change measured on this project.
Separable work (data processing) got worse for the same reason.

**Kept, not promoted, not deleted.** v3 remains the default. Use v5 where the
deliverable is one coupled file — notably the showcase:

    PROMPT_SET=v5 python cli.py --demo-showcase

**The highest-value next experiment is a v6** that makes the rule conditional
instead of global: keep v5's "one coupled file goes to one builder" AND restore
v3's willingness to split genuinely separable work. Both branches in one prompt.
It should beat both — but measure it, do not assume.

**Also worth running:** `PROMPT_SET=v5 python scripts/showcase_reliability.py
--runs 10`. The showcase is 2/10 on v3, and v5 is the set that fixes web apps.
That is the cheapest shot at making the demo's weakest number better.

## The four things asked for, and their status

1. **Planner tuning (v5)** — **DONE and measured** (see the v5 section above).
   Kept as a special-purpose set for coupled single-file work; v3 stays default.
   Follow-up is a conditional v6.
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
> Short version: master is launch-ready and I don't want it destabilised. Prompt
> tuning is measured and settled for now — v3 is the default at 61%, and v5 is
> kept as a special-purpose set that is much better on single-file web apps
> (5/6 vs 3/6) and worse on separable work.
>
> The queue, best first: (1) build a conditional "v6" planner combining v5's
> rule for coupled artifacts with v3's for separable ones, and measure it against
> both; (2) run `PROMPT_SET=v5 python scripts/showcase_reliability.py --runs 10`
> — the showcase is our worst number at 2/10 and v5 is the set that fixes web
> apps; (3) wire the verification/reputation module on `experiment/verification`
> into the dispatcher and show reputation on the dashboard node cards; (4) org
> multi-tenancy on its own branch.
>
> Work on branches, keep master stable, measure every prompt change against the
> eval set before promoting it, and append to SPRINT_PHASE2.md's Session Log as
> you go. I have no programming experience — make the technical calls yourself and
> tell me only what I actually need to act on.
