# Handoff — Mycelium / distributed-orchestrator

_Rewritten Aug 10, 2026, end of session 4. Paste the prompt at the bottom into a
new Claude Code session._

---

## Read these first, in order

1. `MASTER_PLAN.md` — direction and the launch plan
2. `SPRINT_PHASE2.md` — **the active plan and cross-session memory.** Its Session
   Log has the full history with numbers
3. `CLAUDE.md` — house rules
4. This file

## Jett context — read this before planning anything

No programming experience. Make the technical calls, explain in plain language,
warn before anything network-facing, and tell him only what he must act on.

**He is NOT at IU yet.** As of Aug 10 he is roughly **1–2 weeks** from
travelling, and the video is recorded **after** he arrives. Do not plan around
campus hardware or a second machine before then. (An earlier handoff said he was
already there — that was wrong and cost planning accuracy.)

**Freeze discipline:** when he says he is within ~3 days of leaving, stop
feature work. Switch to: full regression, fresh-clone install check, docs and
demo script final, launch post final. He would rather arrive with a repo that
has been stable for 72 hours than three more features and an untested merge.

## State right now

**`master` is launch-ready and was not touched this session.** All work is on
`claude/mycelium-v5-eval-7bc393` (pushed, no PR opened yet). 314 tests green,
ruff clean.

| what | measured |
| --- | --- |
| Output quality | **61%** of 28 eval prompts runnable + on-spec (`v3`) |
| WAN overhead | 216 ms RTT Indiana→Germany; network is **~2%** of a pitch |
| `--demo-showcase chart` | **4/4** — safe to generate live on camera |
| `--demo-showcase` (Snake) | **2/10** — pre-generate only |
| `--demo` | **2/3** clean, and failures are loud (red panel, exit 1) |
| Restart recovery | 17/17 incl. SIGKILL |
| Soak | 60 pitches, +0.9 MB RSS, no leaks |

## ⚠️ One thing needs Jett, and it affects the video

**The live orchestrator is running an image with a broken WebSocket.** Found
this session: `requirements.txt` pinned bare `uvicorn`, which ships with **no
WebSocket implementation**, so `/ws/events` returns 404 on every deployment —
verified 404 on the live box, on Jett's local server, and on a fresh one. The
dashboard silently falls back to 3-second polling and **live token streaming has
never worked**. That is the dashboard's best moment on camera.

Fixed in the repo (`websockets` added to requirements.txt, verified: the upgrade
returns 101 and the dashboard console is clean). **The live server still needs a
redeploy to pick it up** — that needs the SSH key, which is only on Jett's
laptop. Until then the live dashboard has no live stream.

Why nothing caught it for months: no test drives the WebSocket through a real
server, and `TestClient` implements WebSockets itself instead of going through
uvicorn's protocol layer, so a TestClient test passes either way.
`tests/test_runtime_deps.py` now pins the dependency.

## The most important finding: what the eval can and cannot measure

**Between any two eval runs, 11–18 of the 28 prompts flip outcome.** Half the
set is unstable run to run.

| pair | up | down | net |
| --- | --- | --- | --- |
| v1 → v3 | 9 | 2 | **+7** |
| v3 → v4 | 4 | 10 | **-6** |
| v3 → v5 | 7 | 8 | **-1** |

So this instrument resolves large effects and nothing smaller. **v5 was deleted**
on that basis (16/28 vs v3's 17/28, McNemar p ≈ 1.0). The previous session had
kept it as a "special-purpose set" because web_app went 3/6 → 5/6 — that is
three prompts up and one down out of six, p ≈ 0.63. Noise. The full reasoning is
in `prompts/v3.py`'s docstring, which is where the promote-or-delete rule lives.

**The highest-value eval nobody has run: the same prompt set twice.** No set has
ever been repeated, so prompt-change and run-to-run variance have never been
separated. It costs the same ~9 hours as any other run and would tell you what
every number in this project is actually worth. Do this before writing a v6.

## What shipped this session

1. **v5 deleted**, its measured run preserved at `evals/results/20260810_041455`
   (it existed only in an untracked worktree and would have been lost).
2. **Showcase alternatives measured** — the launch's money shot. Same harness,
   model and prompt set as the game, all checked in a real browser:

   | showcase | result | avg run |
   | --- | --- | --- |
   | `chart` | **4/4** (n=10 confirmation was running at session end) | 22 min |
   | `clock` | 3/4 | 28 min |
   | `particles` | 3/4 | 20 min |
   | `snake` | 2/10 | ~50 min |

   `showcase.py` holds the pitches and per-artifact checks so `cli.py` and the
   harness cannot drift apart. `--demo-showcase [id]` selects one; bare
   `--demo-showcase` is still Snake so every existing doc and the 2/10 stay
   reproducible. **The game was not removed** — it is the honest hard case.
3. **Verification wired into the dispatcher**, off by default (`verify_rate: 0`).
   Sampled duplication to a second node (`exclude_node` stops a node grading its
   own homework), background comparison so it never delays the deliverable,
   `rank()` giving better nodes *first refusal* rather than exclusion, and
   routing weight on the dashboard node cards. Verified in a real browser.
4. **The WebSocket fix above.**

## Branches

- `master` — stable, launch-ready, untouched this session
- `claude/mycelium-v5-eval-7bc393` — **this session's work**, pushed, no PR yet
- `experiment/showcase-quality`, `experiment/verification` — already merged, stale

## What to do next, best first

1. **Finish/confirm the chart at n=10** if the run did not complete — check
   `scripts/showcase_results/` for the newest log. `scripts/showcase_rescore.py`
   re-scores saved artifacts without regenerating. Then update the number in
   `docs/showcase-ceiling.md`, `docs/demo-script.md` and this file.
2. **Get the WebSocket fix onto the live server** (needs Jett's SSH key).
3. **Open a PR for this branch** and merge once CI is green.
4. **Run v3 against itself** for a real noise floor, before any new prompt set.
5. Only then: a conditional v6, if the noise floor says it could ever be seen.

## Do NOT build

**Org multi-tenancy.** Enterprise plumbing for a system with zero external
users, in neither MASTER_PLAN's roadmap nor its parking lot, and an org can
already get a private pool by running its own instance. Jett confirmed this call
on Aug 10. Revisit only if a real person asks for it.

## How this project works (earned the hard way)

- **Measure before and after every prompt change**, one variable at a time —
  and now, know that a delta under ~6 prompts is not distinguishable from noise.
- **`evals/rescore.py`** re-scores a finished eval from saved artifacts;
  **`scripts/showcase_rescore.py`** does the same for showcase runs. A scoring
  fix costs seconds, not another 9–20 hours.
- **Verify negative results by running the artifact.** Done twice this session:
  a bad glob "proved" the committed Snake asset was missing (it was one
  directory deeper), and a clock that failed the animation check turned out to
  be genuinely broken — settled by a full-pixel canvas diff and a screenshot,
  not by the subsampled hash that flagged it.
- **Only running the real server finds server bugs.** The WebSocket 404, the
  bad Ollama error messages, and the restart-recovery gaps were all invisible to
  the in-process test client.
- **Never run tests/servers/Docker/browsers while a pipeline or eval is going.**
  8 GB, CPU-only — it starves Ollama.
- **Windows specifics:** write UTF-8 explicitly everywhere; `SystemRoot` must
  survive into subprocess envs or sockets die with WinError 10106.

## Secrets

Not in the repo and must never be. `node_secret` and `pitch_key` live in
`/root/distributed-orchestrator/data/config.json` on the VM, and Jett has
copies. SSH key is `~/.ssh/swarm_orchestrator` on his laptop only. The harness
reads `PITCH_KEY` from the environment or an untracked `.pitch_key` file — never
ask him to paste a key into chat.

**Note on running evals remotely:** pitching to the live orchestrator does *not*
free Jett's laptop. `/pitch/async` hands builder subtasks out to connected
nodes, and his laptop is the only connected node, so the work returns to it plus
216 ms each way. It only helps if his local `node.py` processes are stopped
first.

---

## Paste this into a new session

> I'm continuing work on Mycelium (github.com/Jwrightsman/distributed-orchestrator).
> Read `HANDOFF.md` first, then `MASTER_PLAN.md` and `SPRINT_PHASE2.md` — the
> sprint file's Session Log is the real history and carries every measured number.
> Then `CLAUDE.md` for house rules.
>
> master is launch-ready and I don't want it destabilised. Session 4's work is on
> `claude/mycelium-v5-eval-7bc393`, pushed with no PR yet.
>
> The queue, best first: (1) confirm the `chart` showcase at 10 runs if that
> didn't finish, and update the number everywhere it appears; (2) open a PR for
> that branch and merge it once CI is green; (3) run prompt set v3 against itself
> to get a real noise floor — no set has ever been run twice, and until that
> exists no prompt change under about six prompts can be believed; (4) leave org
> multi-tenancy alone, that's decided.
>
> One thing that needs me: the live orchestrator still serves a 404 on
> /ws/events until it's redeployed with the websockets fix. Tell me the exact
> commands and I'll run them.
>
> Work on branches, measure every prompt change against the eval set before
> promoting anything, and append to SPRINT_PHASE2.md's Session Log as you go. I
> have no programming experience — make the technical calls yourself and tell me
> only what I actually need to act on.
