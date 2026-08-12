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

**Session 4's work is merged.** PR #27 → `master`, CI green on the merge commit
(tests + docker, Python 3.14). 314 tests, ruff clean. `master` is launch-ready.

**A measurement is running as of Aug 11, 05:23 UTC:** `evals/results/20260811_052310`
— prompt set **v3 run against itself**, to establish the noise floor described
below. Expect 9–20 hours. It is resumable (`--resume 20260811_052310
--retry-failed`). **Do not run tests, servers or browsers on this machine until
it finishes** — check whether it is still going before doing anything CPU-heavy.

When it lands, compare it prompt-by-prompt against `evals/results/20260808_050610`
(the original v3 run, 17/28). The churn between those two runs is **pure
run-to-run variance with zero prompt difference** — the first honest error bar
this project has ever had. Write the number into `prompts/v3.py` and use it as
the threshold for every future promote-or-delete decision.

| what | measured |
| --- | --- |
| Output quality | **~57%** of 28 eval prompts runnable + on-spec, 95% CI 44-69% (`v3`) |
| WAN overhead | 216 ms RTT Indiana→Germany; network is **~2%** of a pitch |
| `--demo-showcase chart` | **10/10** — safe to generate live on camera |
| `--demo-showcase` (Snake) | **2/10** — pre-generate only |
| `--demo` | **2/3** clean, and failures are loud (red panel, exit 1) |
| Restart recovery | 17/17 incl. SIGKILL |
| Soak | 60 pitches, +0.9 MB RSS, no leaks |

## The live orchestrator: FIXED and verified (Aug 12)

`/ws/events` returned **404 on every deployment** for months — `requirements.txt`
pinned bare `uvicorn`, which ships with no WebSocket implementation, so the
dashboard silently fell back to 3-second polling and live token streaming never
worked at all. Fixed in the repo, and now **deployed and verified on the live
box**: `/ws/events` returns **101**, `/nodes` carries the new `verify_rate`
field, dashboard and landing page both 200, container logs clean.

**Why the first deploy attempt silently did nothing, because it will happen
again to someone:** the server was set up before the repo went public, so its
code arrived as a tarball with **no `.git` directory**. `git pull` failed with
`fatal: not a git repository`, the `&&` chain stopped before the rebuild, and
the deploy looked like it had worked. The container's uptime (6 days) was the
tell.

It is now a proper git checkout tracking `origin/master`, so the ordinary
one-liner works from here on:

    ssh -i ~/.ssh/swarm_orchestrator root@167.233.239.33       "cd /root/distributed-orchestrator && git pull && docker compose up -d --build"

**Always verify after deploying** — a deploy that did nothing looks identical to
one that worked. `docs/DEPLOY.md` now has an "Updating a running orchestrator"
section with the exact WebSocket check (101 good, 404 stale) and the tarball
recovery procedure. A backup of the server's `data/` is at
`/root/data-backup-20260812_080526`.

**Jett's laptop node was stopped** (Ollama died overnight, taking `node.py` with
it; Ollama has been restarted). It was deliberately left unjoined while a
measurement run was using the CPU. To rejoin:

    py node.py --server http://167.233.239.33:8000 --secret <node_secret>

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
   | `chart` | **10/10** (Fisher p=0.0004 vs the game; true rate >=74% at 95%) | 22 min |
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

- `master` — stable, launch-ready, **contains session 4's work** (PR #27, CI green)
- `claude/mycelium-v5-eval-7bc393` — merged (PR #27), safe to delete
- `claude/eval-compare-tool`, `claude/eval-docs`, `claude/session-log-compare` —
  merged (PRs #28–30), safe to delete
- `experiment/showcase-quality`, `experiment/verification` — already merged, stale

## What to do next, best first

1. **Score the v3-repeat run** when it finishes (see State above):

       python evals/compare.py 20260808_050610 20260811_052310

   It will print a NOISE FLOOR rather than a comparison, because both runs used
   v3. That churn number is the error bar everything else depends on — write it
   into `prompts/v3.py` rule 3 and use it as the bar every future candidate has
   to clear.
2. **Get the WebSocket fix onto the live server.** Already merged, so on the VM:
   `cd /root/distributed-orchestrator && git pull && docker compose up -d --build`.
   Confirm with a WebSocket upgrade request to `/ws/events`: **101 means fixed,
   404 means it is still serving the old image.** Needs Jett's SSH key.
3. Only then: a conditional v6, if the noise floor says it could ever be seen.
4. Optional, cheap: `clock` and `particles` are only measured at n=4. If the
   chart's dataset ever feels too static for the video, the clock is the
   prettier artifact and would need ~6 more runs to know if it clears the bar.

## Do NOT build

**Org multi-tenancy.** Enterprise plumbing for a system with zero external
users, in neither MASTER_PLAN's roadmap nor its parking lot, and an org can
already get a private pool by running its own instance. Jett confirmed this call
on Aug 10. Revisit only if a real person asks for it.

## How this project works (earned the hard way)

- **Measure before and after every prompt change**, one variable at a time —
  and now, know that a delta under ~6 prompts is not distinguishable from noise.
- **`evals/compare.py` decides promote-or-delete — do not eyeball two summary
  files.** It reports churn (how many prompts flipped *each way*, which a
  success-rate diff hides), an exact one-sided McNemar test, and the power of
  the comparison. It reproduces every historical decision. Two things it will
  tell you that are easy to get wrong: a 4–6 prompt category can **never** reach
  p<0.05 on its own, and v1 → v3 — the best change ever made here — only clears
  at p=0.033 and needed 9 of its 11 flips to go one way.
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
