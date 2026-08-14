# Handoff — Mycelium / distributed-orchestrator

_Rewritten Aug 12, 2026. Paste the prompt at the bottom into a new session._

---

## Read these first, in order

1. `MASTER_PLAN.md` — direction and the launch plan
2. `SPRINT_PHASE2.md` — **the active plan and cross-session memory.** Its Session
   Log has the full history with numbers
3. `CLAUDE.md` — house rules
4. `ROADMAP.md` — **REFERENCE, NOT A WORK QUEUE.** Everything *not* being built
   right now: the long-term vision, deferred engineering, the August 2026
   external review, speculative ideas. Every item is gated on a trigger. **Do
   not pull work from it.** Read it to know why something isn't built, to avoid
   proposing something already rejected, and to avoid rebuilding something the
   sprint files already shipped. Items move out of it into a sprint file only
   when Jett says so.
5. This file

## Jett context — read before planning anything

No programming experience. Make the technical calls, explain in plain language,
warn before anything network-facing, and tell him only what he must act on. He
is comfortable with long autonomous stretches and would rather you keep working
than stop to ask.

**He is NOT at IU yet.** As of Aug 12 he is roughly **1–2 weeks** from
travelling, and the video is recorded **after** he arrives. Do not plan around
campus hardware or a second machine before then.

**Freeze discipline:** when he says he is within ~3 days of leaving, stop feature
work. Switch to: full regression, fresh-clone install check, docs and demo script
final, launch post final. He would rather arrive with a repo that has been stable
for 72 hours than three more features and an untested merge.

## State

`master` has PRs #27–#44 merged. **Two PRs are open and unmerged:**

- **#45 — result binding** (`claude/roadmap-and-binding`). The August review's
  top finding, closed. 376 tests, ruff clean.
- Everything before it is merged; #43 and #44 landed.

**ROADMAP.md now exists** (added Aug 14) and its four integration points are
done: MASTER_PLAN §8 is a pointer to it instead of a second parking lot, it is
item 4 of the read-first list above, and README and CONTRIBUTING link it.

| what | measured |
| --- | --- |
| Output quality | **~57%** of 28 eval prompts, 95% CI 44–69% (`v3`) |
| Eval noise floor | **18 of 28 prompts flip between two identical runs** |
| `--demo-showcase chart` | **10/10** — safe to generate live on camera |
| `--demo-showcase` (Snake) | **2/10** — pre-generate only |
| `--demo` | **6/6 on a fresh Ollama, 0/2 after 5+ h** — restart Ollama before filming |
| WAN overhead | 216 ms RTT Indiana→Germany; network ~2% of a pitch |
| Restart recovery | 17/17 on Linux with Ollama stopped |
| MCP flow | **10/10** end to end with real inference |
| Live smoke | **11/11**, `nodes_used=1`, 7 min |
| Known memory leak | ~1.25 MB/pitch, linear, source not found |

**Nothing is running.** The CPU is free.

## Result binding — what #45 does and does not do

`node_secret` is network admission, **not per-node identity**. Submission used to
trust the `node_id` in the body, so any admitted node could take another node's
credit. Now every handout mints an attempt (id + nonce, distinct from `task_id`),
and settlement requires: right node, matching nonce, unexpired lease, unsettled
attempt. Rejections are 403 + a `result_rejected` event. Settlement is idempotent
— a retry replays the original outcome, never pays twice.

**Not equivalent to per-node keypairs.** It stops an admitted node stealing
credit; it does not stop a holder of the shared secret joining under a chosen
name. Signed receipts, revocation and rotation are deferred to ROADMAP §5 and
noted in `server_state.py`.

**Nodes must run current code to earn credit.** A node on an older build has its
results *recorded but not settled* — deliberate, so no work is lost. Jett's
laptop node needs restarting from current master after #45 merges, or it will
stop earning.

## The finding that reframes everything: the eval is mostly noise

v3 was run against **itself** — same prompts, same model, same machine, nothing
changed (`evals/results/20260811_052310` vs `20260808_050610`):

    run 1: 17/28 (61%)      run 2: 15/28 (54%)
    8 improved, 10 regressed
    CHURN: 18 of 28 prompts changed outcome, with NO cause

That is **higher** churn than any prompt-set comparison ever produced here
(v3→v4 was 14, v3→v5 was 15), so every difference previously attributed to a
prompt change is consistent with dice. Per-category is worse than useless: `api`
went 3/4 → 0/4 and `vague` went 2/4 → 4/4 with nothing changed.

**Consequences, already applied to the docs:**

- README publishes **~57% (95% CI 44–69%)** and shows both runs with the churn.
- `prompts/v3.py` carries the floor: **a net difference of ≤2 prompts is noise.**
  Only v1→v3 ever cleared the bar (one-sided p=0.033, needing 9 of its 11 flips
  one way — it got exactly 9).
- `evals/compare.py` does the arithmetic: churn, exact one-sided McNemar, power,
  and a promote/delete verdict. It reproduces every historical decision.

**Do not write a v6 expecting to see it.** At n=28 this instrument cannot resolve
anything smaller than about six prompts. Growing the prompt set, or averaging
repeated runs, is the only way to measure anything subtler.

## The live orchestrator: fixed and verified (Aug 12)

`/ws/events` returned **404 on every deployment** for months — `requirements.txt`
pinned bare `uvicorn`, which ships with no WebSocket implementation, so the
dashboard silently fell back to 3-second polling and live token streaming never
worked. **Now deployed and verified live:** `/ws/events` returns **101**,
`/nodes` carries `verify_rate`, dashboard and landing page 200, logs clean.

**Why the first deploy silently did nothing** — it will happen to someone else:
the server predates the repo going public, so its code arrived as a tarball with
**no `.git`**. `git pull` failed with `fatal: not a git repository`, the `&&`
chain stopped before the rebuild, and it looked like success. The container's
6-day uptime was the only tell.

It is now a proper git checkout tracking `origin/master`, so the ordinary
one-liner works from here:

    ssh -i ~/.ssh/swarm_orchestrator root@167.233.239.33 \
      "cd /root/distributed-orchestrator && git pull && docker compose up -d --build"

**Always verify after deploying.** `docs/DEPLOY.md` has the WebSocket check
(101 good, 404 stale) and the tarball recovery procedure. Server `data/` backup:
`/root/data-backup-20260812_080526`.

**Jett's laptop node is NOT joined.** Ollama died overnight and took `node.py`
with it; Ollama was restarted, the node deliberately was not, because a
measurement run needs the CPU. To rejoin once measurements are done:

    py node.py --server http://167.233.239.33:8000 --secret <node_secret>

## The showcase: live generation is now safe

Same harness, model and prompt set, every artifact opened in a real browser:

| showcase | result | avg run |
| --- | --- | --- |
| `chart` | **10/10** (Fisher p=0.0004 vs the game; true rate ≥74% at 95%) | 22 min |
| `clock` | 3/4 | 28 min |
| `particles` | 3/4 | 20 min |
| `snake` | 2/10 | ~50 min |

`showcase.py` holds the pitches and per-artifact checks so `cli.py` and the
harness cannot drift. `--demo-showcase [id]` selects one; bare `--demo-showcase`
is still Snake, so every existing doc and the 2/10 stay reproducible. **The game
was not removed** — it is the honest hard case. `docs/demo-script.md` says which
showcase is for which shot.

## Verification is wired, and off by default

`verify_rate: 0`. Sampled duplication to a second node (`exclude_node` stops a
node grading its own homework), background comparison so it never delays the
deliverable, `rank()` giving better nodes *first refusal* rather than exclusion,
and routing weight on the dashboard node cards. Verified in a real browser.

## What to do next, best first

1. **Rejoin Jett's node** (command above). Nothing is measuring now, so this is
   safe and his live network currently shows 0 nodes.
2. **Grow the eval prompt set** if anyone wants to tune prompts again. Nothing
   else makes prompt work measurable.
3. Optional: `clock` and `particles` are only at n=4. The clock is the prettier
   artifact if the chart ever feels too static; ~6 more runs would settle it.

## Do NOT build

**Org multi-tenancy.** Enterprise plumbing for a system with zero external users,
in neither MASTER_PLAN's roadmap nor its parking lot, and an org can already get
a private pool by running its own instance. Confirmed with Jett Aug 10.

## How this project works (earned the hard way)

- **`evals/compare.py` decides promote-or-delete — never eyeball two summary
  files.** A 4–6 prompt category can *never* reach p<0.05 on its own.
- **Verify negative results by running the artifact.** Done repeatedly this
  session: a clock that failed the animation check was genuinely broken (proved
  by a full-pixel canvas diff plus a screenshot), and a `0/7` demo result was
  **Ollama being down**, not a regression — the 0-minute runtimes gave it away.
- **Only running the real thing finds real bugs.** The WebSocket 404, the
  `signal.SIGKILL`-does-not-exist-on-Windows crash, and the leaked server that
  corrupted a later run were all invisible to the test suite.
- **A deploy that did nothing looks exactly like one that worked.** Verify.
- **Never run tests/servers/browsers/inference while a measurement is going.**
  8 GB, CPU-only — it starves Ollama.
- **Windows specifics:** write UTF-8 explicitly; `SystemRoot` must survive into
  subprocess envs; there is no `SIGKILL` — use `Popen.kill()`.

## Secrets

Not in the repo, ever. `node_secret` and `pitch_key` live in
`/root/distributed-orchestrator/data/config.json` on the VM; Jett has copies. SSH
key is `~/.ssh/swarm_orchestrator` on his laptop only. The eval harness reads
`PITCH_KEY` from the environment or an untracked `.pitch_key` file — never ask
him to paste a key into chat.

**Running evals remotely does not free Jett's laptop:** `/pitch/async` hands
builder subtasks to connected nodes, and his laptop is normally the only one, so
work returns to it plus 216 ms each way. Stop `node.py` first if you try it.

---

## Paste this into a new session

> I'm continuing work on Mycelium (github.com/Jwrightsman/distributed-orchestrator).
> Read `HANDOFF.md` first, then `MASTER_PLAN.md` and `SPRINT_PHASE2.md` — the
> sprint file's Session Log is the real history and carries every measured
> number. Then `CLAUDE.md` for house rules.
>
> master is current, all merged, CI green — don't destabilise it. The big finding
> last session: the eval set is mostly noise (18 of 28 prompts flip between two
> identical runs), so the published quality number is now ~57% with a wide
> interval, and prompt tuning at n=28 is not measurable. The live orchestrator's
> WebSocket bug is fixed and deployed.
>
> The queue: (1) score the `--demo` reliability run in `scripts/demo_results/` if
> it finished, and update `docs/demo-script.md`; (2) rejoin my laptop as a node
> once nothing is measuring; (3) if anyone wants to tune prompts again, the eval
> set has to grow first — nothing else makes it measurable; (4) leave org
> multi-tenancy alone, that's decided.
>
> Work on branches, use `evals/compare.py` for any prompt comparison, and append
> to SPRINT_PHASE2.md's Session Log as you go. I have no programming experience —
> make the technical calls yourself and tell me only what I need to act on.
