# Handoff — Mycelium / distributed-orchestrator

_Rewritten Aug 14, 2026. Paste the prompt at the bottom into a new session._

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
   sprint files already shipped. Items move into a sprint file only when Jett
   says so.
5. This file

## Jett context — read before planning anything

No programming experience. Make the technical calls, explain in plain language,
warn before anything network-facing, and tell him only what he must act on. He
is comfortable with long autonomous stretches and would rather you keep working
than stop to ask.

**He is NOT at IU yet.** As of Aug 14 he is roughly **1–2 weeks** from
travelling, and the video is recorded **after** he arrives. Do not plan around
campus hardware or a second machine before then.

**Freeze discipline:** when he says he is within ~3 days of leaving, stop feature
work. Switch to: full regression, fresh-clone install check, docs and demo script
final, launch post final. He would rather arrive with a repo that has been stable
for 72 hours than three more features and an untested merge.

## State

`master` has PRs #27–#45 merged. **#45 (result binding) is merged** — it was
already in when this session started, despite the previous handoff saying
otherwise.

**PR #46 is open, CI green** (`claude/mycelium-roadmap-rehearsal-e3d93e`):
ROADMAP integration, the demo-script rehearsal executed in full, **five** bug
fixes, and deploy verification. 390 tests, ruff clean. Merge it unless you find
a reason not to.

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
| Planner non-determinism | same pitch → **5, 1 and 4 subtasks**; 181 s, 82 s, 97 s |
| Builder subtask length | 41–329 s observed on qwen3.5:4b, CPU |
| Known memory leak | ~1.25 MB/pitch, linear, source not found |
| MCP flow, re-verified Aug 14 | **10/10** after fixing the checker that had silently stopped working |
| Demo shots executed | **all 7**; chart PASS, memory PASS, snake FAIL (the documented 8-in-10) |
| Cost of running two pipelines at once | **~3x wall clock on both** — chart 81 min vs ~22, `--demo` 76 vs ~35 |

**Nothing is running.** The CPU is free.

---

## ⚠ Two things need Jett, in this order

### 1. Merge #46, then redeploy the live orchestrator

The live box has not picked up #43, #44 or #45. Merge #46 first so the deploy
check below exists on the server side.

```bash
ssh -i ~/.ssh/swarm_orchestrator root@167.233.239.33 "cd /root/distributed-orchestrator && git pull && docker compose up -d --build"
```

Then **prove it actually landed** — from an up-to-date `master` checkout on his
laptop:

```bash
python scripts/verify_deploy.py http://167.233.239.33:8000
```

`MATCH` means the live server is running exactly that code. `STALE` means it is
not, and the script prints what to check. This is new in #46 and exists because
a deploy that silently did nothing looks identical to one that worked — which
has already cost this project a day. `/status.json` now carries a `build` hash
computed inside the running process, so a `git pull` that succeeded while the
rebuild didn't cannot fake it.

If it prints **"no build field"**, the rebuild did not happen: the image
predates #46 entirely.

### 2. Restart his laptop node from current code

**It is not currently joined.** After #45, a node running older code has its
results *recorded but not settled* — it works but earns nothing, deliberately,
so no work is lost. And without #46 it gets evicted mid-build on any subtask
over 90 seconds (see below), which is most of them.

```bash
cd C:\Users\wrigh\Projects\distributed-orchestrator
git pull
py node.py --server http://167.233.239.33:8000 --secret <node_secret>
```

The secret is in `/root/distributed-orchestrator/data/config.json` on the VM;
he has a copy. Never ask him to paste it into chat.

---

## What the dress rehearsal found (Aug 14) — read this before touching the node path

`docs/demo-script.md` had never been executed by anyone. Running it literally
found three code bugs. All are fixed in #46, all have regression tests confirmed
to fail with the fix removed.

**1. A node streaming tokens did not count as alive.** `last_seen` was refreshed
only by `/tasks/next` and `/tasks/{id}/result`, so a node was "alive" only
*between* tasks. Any builder subtask longer than `_NODE_TIMEOUT` (90 s) looked
silent — while the node posted a token batch every 0.3 s. Observed on a real
pitch: the subtask was reclaimed out from under the node, the node was evicted,
the task was re-queued into an empty registry, and the node was paid **+0
credits** for a 329-second build it went on to finish. `/metrics` said
`nodes_online: 0` while the node's terminal showed it building.

Note how this interacts with #45: the reclaim invalidates the attempt, so #45's
settlement check *correctly* refuses to pay. Attempt binding working as designed
on top of a broken liveness signal is exactly what produces "my laptop stopped
earning and nobody can say why."

After the fix, the same work re-run gave five subtasks at 87/138/282/236/269 s —
four past the old cutoff — with `nodes_online` never below 1, zero reclaims, and
all five paid.

**2. Every 500 echoed its exception text.** Two `@app.exception_handler(Exception)`
handlers were registered in `server.py`; Starlette keys them by class, so the
later, leaky one silently replaced the hardened one. Live on the public
orchestrator. The chaos test that should have caught it was asserting the
*leaky* handler's response shape — it passed throughout.

**3. `scripts/mcp_e2e.py` could no longer verify the MCP flow at all.** It
matched job ids with `job_\d+`, but they became `job_{uuid4().hex}`, so every
run failed at step 3. **The 10/10 in the sprint log was true when recorded and
had quietly stopped being reproducible** — the check guarding the video's
differentiator was dead and nothing said so. Fixed, and 10/10 again.

**4. The committed fallback Snake game is not the clean copy the script
implied.** `docs/demo-assets/snake-game/` is a folder of transcripts; the
openable file is `code/index.html`, and it **also opens on GAME OVER**. It
animates, so it is playable, but the restart click applies to it too.

**5. The advertised one-line join could never finish.** `curl … | bash -s -- URL`
gives bash the downloaded script as stdin, so `install.sh`'s closing
`exec join.py` inherited a pipe, and join.py's consent gate correctly refuses
without a terminal. The installer did all its work and stopped on "Not running
in a terminal, so nobody can consent." Fixed by handing over on `/dev/tty` —
**not** with `--yes`, which would consent on the machine owner's behalf. A test
blocks that shortcut. It survived because the launch checklist tested
`join.py` directly, which is a different code path from the piped one-liner.

**Verified fixed on a clean Debian container with a real terminal**, which is
the only way to prove it: the old installer reports `isatty False` and refuses;
the new one reports `isatty True` and reaches `Type 'yes' to join`. A human
still consents — the fix is `/dev/tty`, never `--yes`.

---

## The demo script is now honest about the rebuilt dashboard

Prep gained two missing prerequisites — **start a worker node** (Shots 2 and 3
are blank without one) and **clear `events.db`** (Live Activity opens showing
entries from days earlier). The "15-minute prep" claim was replaced with a real
budget: ~2 hours, or ~35 minutes on the short path.

Shot 3 no longer says "dashboard, full frame": after the rebuild the plan is in
**Overview**, node cards in **Nodes**, credits on the node card or in **Guild**.
And **with one machine nothing runs in parallel** — five subtasks queued at once
went strictly sequentially through one node — so that shot needs a second
machine or a different caption. Do not narrate parallel execution over one node.

`docs/demo-script.md` ends with a provenance section: what was actually executed
in the rehearsal and what is still on trust (Shot 1's live chart, the Claude
Desktop MCP take, the `--demo` memory run, Snake generation).

---

## The finding that reframes everything: the eval is mostly noise

v3 was run against **itself** — same prompts, same model, same machine, nothing
changed (`evals/results/20260811_052310` vs `20260808_050610`):

    run 1: 17/28 (61%)      run 2: 15/28 (54%)
    8 improved, 10 regressed
    CHURN: 18 of 28 prompts changed outcome, with NO cause

That is **higher** churn than any prompt-set comparison ever produced here, so
every difference previously attributed to a prompt change is consistent with
dice. Per-category is worse than useless: `api` went 3/4 → 0/4 and `vague` went
2/4 → 4/4 with nothing changed.

- README publishes **~57% (95% CI 44–69%)** and shows both runs with the churn.
- `prompts/v3.py` carries the floor: **a net difference of ≤2 prompts is noise.**
- `evals/compare.py` does the arithmetic and reproduces every historical decision.

**Do not write a v6 expecting to see it.** At n=28 this instrument cannot resolve
anything smaller than about six prompts. Growing the prompt set, or averaging
repeated runs, is the only way to measure anything subtler. The rehearsal added
a reason: the *planner* is non-deterministic too — the same pitch decomposed
into 5, 1 and 4 subtasks on three consecutive runs.

---

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

## What to do next, best first

1. **Merge #46**, then the two Jett actions above.
2. **Grow the eval prompt set** if anyone wants to tune prompts again. Nothing
   else makes prompt work measurable.
3. Optional: `clock` and `particles` showcases are only at n=4. The clock is the
   prettier artifact if the chart ever feels too static; ~6 more runs settle it.

## Do NOT build

**Org multi-tenancy.** Enterprise plumbing for a system with zero external users;
an org can already get a private pool by running its own instance. Confirmed with
Jett Aug 10, and recorded in ROADMAP §7.

Anything else in `ROADMAP.md`. It is reference. Every item is gated on a trigger
and none of them have fired.

## How this project works (earned the hard way)

- **Only running the real thing finds real bugs.** Three code bugs in one
  rehearsal, after the suite, the eval harness and careful reading all missed
  them. The WebSocket 404, the Windows `SIGKILL`, and the soak's `/proc` probe
  were the same shape.
- **A test pinned to the wrong contract is worse than no test.** The chaos test
  asserted the leaky 500 handler's response shape and passed for months.
- **`evals/compare.py` decides promote-or-delete — never eyeball two summary
  files.** A 4–6 prompt category can *never* reach p<0.05 on its own.
- **Verify negative results by running the artifact.** A grep once "proved" a
  working game had no game logic; a 0/7 demo result was Ollama being down.
- **A deploy that did nothing looks exactly like one that worked.** Now checkable:
  `scripts/verify_deploy.py`.
- **Never run tests/servers/browsers/inference while a measurement is going.**
  8 GB, CPU-only — it starves Ollama. **Measured Aug 14: two pipelines at once
  roughly triples the wall clock of both** (chart 81 min against a documented
  22). Any timing taken while something else ran is worthless — throw it out
  rather than publishing it.
- **A failed `echo` is not evidence a script died.** A chain script whose log
  redirect broke on a quoting bug was assumed dead and relaunched; it was still
  running, and the two copies then ran three pipelines concurrently. Check for
  the *process*, not for output.
- **Windows specifics:** write UTF-8 explicitly; `python` hits the Microsoft
  Store alias — use `py`; `SystemRoot` must survive into subprocess envs; there
  is no `SIGKILL` — use `Popen.kill()`.

---

## Paste this into a new session

> I'm continuing work on Mycelium (github.com/Jwrightsman/distributed-orchestrator).
> Read `HANDOFF.md` first, then `MASTER_PLAN.md` and `SPRINT_PHASE2.md` — the
> sprint file's Session Log is the real history and carries every measured
> number. Then `CLAUDE.md` for house rules. `ROADMAP.md` is reference only —
> don't pull work from it.
>
> State: master has #27–#45 merged. PR #46 is open and CI-green — merge it
> unless you find a reason not to. It carries the ROADMAP integration, the first
> real dress rehearsal of the demo script, and three bugs that rehearsal found:
> nodes being evicted mid-build because streamed tokens weren't a heartbeat,
> 500s echoing their exception text, and the advertised one-line join never
> reaching its consent prompt.
>
> Two things need me and I'd like them queued up: redeploying the live
> orchestrator (#46 adds `scripts/verify_deploy.py` to prove it landed), and
> restarting my laptop node from current code so it earns credits again.
>
> Standing rules: use `evals/compare.py` for any prompt comparison, don't write
> a v6 at n=28, org multi-tenancy stays dead, verify negative results by running
> the artifact. Work on branches. I have no programming experience — make the
> technical calls yourself and tell me only what I need to act on.
