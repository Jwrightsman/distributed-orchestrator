# SPRINT PHASE 2 — August 5–20, 2026

_Continues SPRINT_AUG2026.md, which was completed early (Aug 1–4). Read MASTER_PLAN.md first,
then SPRINT_AUG2026.md's Session Log, then this file. This file is now the active plan and your
persistent memory across sessions._

> **Phase 2 directive: the infrastructure is built. What decides the launch now is whether the
> swarm produces genuinely good output, reliably, without breaking on camera. Nothing in this
> phase adds new user-facing surface area except where explicitly listed. Feature freeze end of
> day August 17. Video recorded the week of August 20.**

_Check items off (`- [x]`), append to the Session Log, and commit this file as you go._

---

## 0. AUDIT FIRST (do this before anything else, one session)

The previous sprint completed faster than planned. Verify what is actually true before building on it.

- [ ] Confirm each SPRINT_AUG2026.md item marked complete is genuinely working, not just present:
      run the test suite, `python status.py`, `python cli.py --demo`, `python cli.py --demo-showcase`,
      and the MCP flow end-to-end. Check CI is green on GitHub, not just configured.
      **PARTIALLY DONE (Aug 5).** Verified: 235 tests + ruff green on a fresh clone, `status.py`,
      the read-only CLI commands, MCP server booting over real stdio with all five tools, CI genuinely
      green on master. **Not verifiable in that session** — no Ollama and no reachable model host:
      `--demo`, `--demo-showcase`, and the MCP flow with real inference. Those three need a session
      on Jett's machine and are the remaining audit work.
- [x] List anything half-done, stubbed, skipped, or silently failing. Fix or explicitly re-open it
      in SPRINT_AUG2026.md rather than leaving it checked. — one false checkbox found and re-opened
      (§2.5 `py`→`python`), four bugs found and fixed. See Session Log.
- [x] Verify a **fresh-clone install** works: clone to a clean directory, follow README exactly as a
      stranger would, note every friction point. This is the single most launch-critical check.
      — done on a clean container; three friction points found and fixed.
- [x] **Repo visibility:** was **PRIVATE**; Jett made it **PUBLIC** on Aug 5, same session.
      Verified: anonymous clone with credentials disabled succeeds, and raw file access returns 200.
      Git history was scanned before publishing — no secrets committed.
- [x] Write an honest audit summary in the Session Log: what's real, what's not, what surprised you.

---

## 1. OUTPUT QUALITY (Aug 5–9) — highest priority

The demo shows the swarm building something. If the output is mediocre, nothing else matters.
This is grinding, iterative work and it is the best possible use of the remaining time.

### 1.1 Eval harness
- [x] `evals/prompts.json` — 28 pitches: 6 web app, 5 CLI, 5 data processing, 4 API, 4 algorithm,
      4 deliberately vague
- [x] `evals/run_evals.py` — runs each pitch through the real pipeline, saves output, scores it.
      `--only/--id/--limit` for fast slices, `--resume` for interrupted runs, `--fake` for a
      model-free plumbing self-test
- [x] Automated scoring per run: (a) extractor produced files, (b) parses via the *production*
      checker, (c) executes — Python in a subprocess, HTML in headless Chromium (uncaught JS errors
      fail the run) with a static fallback, (d) reviewer-model judgment 1–5, (e) wall clock + subtasks
- [x] `evals/results/` with timestamped JSONL + summary.json + a markdown summary table, including a
      "where runs failed" breakdown so tuning has somewhere to aim
- [ ] **Baseline run recorded and committed — this is the number to beat.** BLOCKED: needs Ollama.
      The harness is verified end-to-end against a fake backend (0 plumbing failures across all 28
      prompts); one command on Jett's machine produces the real baseline: `python evals/run_evals.py`

### 1.2 Tune against the baseline
- [ ] Iterate on planner prompt: subtask granularity (too many tiny subtasks fragments the output;
      too few defeats the swarm story), explicit file-boundary instructions, dependency correctness
- [ ] Iterate on builder prompt: complete-file output, no placeholders/TODOs, no prose outside code
      fences, consistent naming across subtasks so files actually integrate
- [ ] Iterate on reviewer prompt: catch missing integration between subtask outputs specifically —
      that is the failure mode unique to this architecture
- [ ] Iterate on reviser prompt: fix without regressing working parts
- [ ] Re-run evals after each change set; keep changes that move the score, revert those that don't
- [ ] **Target: ≥80% of prompts produce runnable, on-spec output on qwen3.5:4b.** Record the final
      score in the README — a real, measured number is credible and rare in this space.

### 1.3 Showcase reliability
- [ ] Run `--demo-showcase` 10 times consecutively; the Snake game must be playable in ≥8
- [ ] Same for `--demo` (expense tracker + memory iteration)
- [ ] If a demo is flaky, tune its specific prompt until it isn't — these two run on camera

---

## 2. RESILIENCE (Aug 9–12) — do not crash on camera

- [x] `tests/test_chaos.py`: node disappears mid-task, node returns malformed/empty output, node is
      very slow (timeout path), node returns a refusal, Ollama restarts mid-pipeline, two nodes
      claim the same task, node reconnects with a stale task ID — 22 tests, all green
- [ ] Verify under stress: circuit breaker opens and recovers, task reclaim reassigns correctly,
      local fallback fires, revision loop terminates (no infinite loops), WebSocket clients survive
      a server restart
      **MOSTLY DONE (Aug 5)** in `tests/test_chaos.py` + `tests/test_resilience.py`: circuit breaker
      opens/recovers/isolates ✓, reclaim reassigns to a live node ✓, local fallback fires when the
      node build fails ✓, revision loop is bounded by `_MAX_REVISIONS` and a run always terminates ✓.
      **Outstanding: WebSocket clients surviving a server restart** — needs a real server restart,
      not an in-process client.
- [ ] **Soak test:** 20 consecutive pitches in one server session. Watch for memory growth, SQLite
      bloat, event-buffer leaks, orphaned tasks, degraded latency. Fix what it surfaces.
- [x] Every failure path produces a clear message rather than a stack trace — assume it happens live
      — found by the restart check below: a job that failed because Ollama was down reported
      httpx's "All connection attempts failed", which tells a viewer nothing. Both `generate` and
      `generate_stream` now name the cause and the fix, for the two failures an audience actually
      triggers (Ollama not running; model never pulled). 4 tests in `tests/test_error_messages.py`.
- [x] Kill-and-restart recovery: server restarts mid-job → nodes reconnect, dashboard recovers state
      — `scripts/restart_recovery.py` drives a real uvicorn server: **17/17 checks pass**, including
      SIGKILL (not a graceful stop). Jobs and event history survive via SQLite, the node registry
      correctly empties so nodes re-register, dashboard and landing page still render, and a
      WebSocket client reconnects and resumes receiving. No Ollama required to run it.

---

## 3. LIVE NETWORK (Aug 12–16)

- [ ] If the 24/7 orchestrator from SPRINT_AUG2026 §3.1 is not live: deploy it now (Oracle free tier
      or Hetzner per docs/DEPLOY.md). Jett handles account creation only; you configure over SSH.
- [ ] Confirm a real remote node connects over the internet — not just LAN — with `node_secret` set.
      This is the first true test of the distributed claim.
- [ ] Measure and record real WAN numbers: task round-trip latency, throughput vs local-only,
      failure rate over a multi-hour run. Put honest numbers in the README; the local-AI community
      respects measured results and punishes vague claims.
- [ ] Harden anything the WAN test exposes (timeouts tuned for real latency, retry backoff, etc.)

---

## 4. STRETCH — only if 1–3 are fully done, on a branch, hard cutoff Aug 16

**llama.cpp RPC sharding** — lets the swarm run *one large model* across several machines, rather
than many small models on each. For the r/LocalLLaMA audience specifically this is the single most
compelling possible capability: "four laptops, none of which can run a 30B model, running a 30B
model together."

- [ ] Branch `feat/rpc-sharding`. Prototype `llama.cpp` `rpc-server` across 2 machines.
- [ ] If it works: expose as an optional backend alongside Ollama; document clearly; merge only if
      it does not destabilize the main path.
- [ ] **If not working cleanly by Aug 16, abandon and leave on the branch.** Do not let this
      jeopardize a stable repo. Note the outcome in the Session Log either way — a documented
      "we tried this, here's what we learned" is itself good README material.

---

## 5. FREEZE + LAUNCH PREP (Aug 17–20)

- [ ] **Aug 17 end of day: feature freeze.** After this, bug fixes and documentation only.
- [ ] Full regression: test suite, chaos tests, soak test, `--demo`, `--demo-showcase`, MCP flow,
      fresh-clone install, live-network smoke test
- [ ] README final: measured eval score, real WAN latency numbers, green CI badge, Positioning
      paragraph (SwarmHarness arXiv 2605.28764), prominent "Looking for nodes" CTA, one-line join
      per OS, honest limitations section (small-model ceiling, trusted-network assumption)
- [ ] `docs/demo-script.md` final — shot-by-shot for the new feature set, timed, so recording at IU
      is follow-the-script with zero decisions to make
- [ ] `docs/community-pitch.md` final — r/LocalLLaMA post drafted in full, title leading with the
      result, plus 3–4 anticipated-question replies pre-written
- [x] Confirm repo is PUBLIC (Jett action) — done Aug 5, verified by anonymous clone

---

## 6. Optional but high-value (Jett, ~30 min total, any time)

A **quiet beta** before the loud launch. Not the video — just 2–3 individuals, DM'd directly:
someone from an AI Discord, a CS classmate, anyone with a laptop. Ask them to run the one-line join
against the live orchestrator and tell you where they got stuck. Every friction point they hit is
one that would otherwise hit 500 people at once on launch day. This is the cheapest possible
insurance on the launch and requires no video production.

---

## Not in this phase
New user-facing features · dashboard redesign · guild/governance code · agent fine-tuning ·
tokens/blockchain (never) · MCP tools beyond the existing five · mobile · anything in
MASTER_PLAN.md §8 except the §4 stretch above.

## Standing rules
Test after every change · never leave the repo broken at session end · commit per logical change
with descriptive messages + Co-Authored-By · warn Jett in plain language before anything
network-facing or before any action he must take himself · prefer measured numbers over claims ·
when in doubt, choose what makes the video safer.

---

## Session Log
_Append one entry per session: date, items completed, what's next, warnings for Jett._

<!-- sessions append below -->

### Session 1 — August 5, 2026 · AUDIT + §1.1 + §2.1

**Environment note that shapes this whole entry:** this session ran in a cloud container with **no
Ollama and no way to get one** — `ollama.com`, the GitHub release binaries, `huggingface.co` and
`registry.ollama.ai` are all blocked by the environment's egress policy (403/timeout), and only PyPI
is reachable. Non-standard ports are blocked too, so the live orchestrator on :8000 was unreachable
from here. **Nothing requiring real inference could be run.** Everything below is either genuinely
executed or explicitly labelled as not verified. No result in this entry is assumed.

#### What is real (verified by running it)

- **235 tests + ruff green**, from a genuinely fresh clone into an empty directory on Python 3.11.
- **CI is genuinely green on GitHub**, not just configured — checked the actual run list; the latest
  master run (merge of PR #18) succeeded, as did every run since Aug 2.
- **`status.py` degrades correctly** with Ollama stopped: a clear "start it with `ollama serve`"
  message, no stack trace.
- **The MCP server is real.** It boots as an actual subprocess over stdio, completes the MCP
  handshake, advertises exactly the five expected tools, and returns a friendly connection error
  rather than crashing when the orchestrator is down. `mcp.server.mcpserver.MCPServer` genuinely
  exists in the `mcp` 2.0.0 SDK — I checked, because the import path looked unusual.
- **No secrets in git history.** Scanned every commit for the live `node_secret` and `pitch_key`:
  zero hits. `config.json` is correctly gitignored. The repo is safe to make public.

#### What was NOT real (found by the audit)

1. **A checked box that was false.** SPRINT_AUG2026 §2.5 claimed `py` launcher hints were
   "neutralized to `python`". They were not — **27 occurrences** remained in program output and docs,
   including the dashboard's "no nodes connected" empty state and the MCP connection-error message.
   `py` is the Windows launcher; on Mac and Linux it does not exist. Every one of those instructions
   was broken for most of r/LocalLLaMA. Fixed, and the item re-opened-then-closed in SPRINT_AUG2026.
2. **`cli.py --history` was broken.** It ran the Ollama pre-flight check *before* dispatching
   `--history`, `--standings` and `--projects`, so all three failed on a machine without Ollama
   running. The code even had a `# flags that don't need Ollama` comment — the check just sat above
   it. This is the first command a curious stranger runs, and it is item 3 in CLAUDE.md's own test
   list, and it was shipped broken. Fixed, with 6 regression tests pinning the ordering.
3. **README install drift.** The quick start said `pip install fastapi uvicorn httpx rich` while
   `requirements.txt` lists five packages — `mcp` was missing, so a stranger following the README
   could not run the flagship MCP feature. Both README and video-setup now point at
   `requirements.txt`.
4. **Run directories collided.** Found the moment the eval harness ran: `run_pipeline` created
   `output/<timestamp>` with a bare `mkdir()`, and timestamps are only second-resolution. Two pitches
   finishing in the same second raised `FileExistsError` — it killed 27 of 28 eval prompts. Reachable
   in production through `/pitch/async`, where jobs genuinely overlap. Fixed with a shared
   `make_run_dir()` used by both the local and distributed paths.
5. **Retried results were paid twice.** `POST /tasks/{id}/result` granted 5 credits on every call.
   A node retrying after a dropped connection — the ordinary case on flaky wifi — got paid twice for
   one task, inflating the ledger and the leaderboard that appears on camera. Credits and
   `tasks_completed` are now gated on the task actually having been in flight; the result is still
   recorded either way so late work is never lost.

#### What surprised me

- **The false checkbox was the most valuable find, and it was invisible to the test suite.** Nothing
  tests printed strings, so 27 broken instructions sat behind a green CI badge. The lesson matches
  the one already in the Aug 1 log: ratings and checkmarks describe intent, not behavior.
- **Two of the five bugs were found by building the eval harness, not by looking for bugs.** The
  run-directory collision had been latent since April and would have surfaced during the §2 soak
  test — 20 consecutive pitches — or on a busy launch day. Building the instrument found things the
  tests were never going to.
- **The audit's most launch-critical check could not be done from a cloud session at all.** A
  fresh-clone install is verifiable here; a fresh-clone *run* is not. That asymmetry is worth
  planning around — the remaining audit items need Jett's machine.
- The suite ran in 3 seconds and I nearly did not look at timing; the chaos tests then pushed it to
  30s because one test waited out a real 25-second long poll. Now 5.5s total.
- **The audit's own instruction proved itself.** "Check CI is green, not just configured" — my first
  push went red on GitHub while passing locally. **CI runs Python 3.14; my container had 3.11.**
  `_emit()` used `asyncio.get_event_loop()`, which on 3.12+ raises `RuntimeError` outside a
  coroutine but on 3.11 quietly creates a loop. The new chaos tests call the janitor synchronously
  and tripped it. So the production code carried a latent 3.14 incompatibility on the version the
  project actually targets — invisible until something called `_emit` from a sync context. Fixed
  (and the same fix stopped broadcasts being garbage-collected before delivery). **A green local
  suite is not evidence about CI here.** Verified green on GitHub afterwards.

#### Built this session

- **§1.1 eval harness — complete.** `evals/` with 28 prompts, five scoring dimensions, resumable
  runs, a markdown summary table, and a "where runs failed" breakdown. It reuses the *production*
  file checker so the eval can never grade more kindly than the pipeline ships. Verified end-to-end
  against a fake backend: 0 extraction, parse, execution or judge failures across all 28 prompts —
  the only failures were the deliberate artifact/keyword mismatches the fake should produce. 33 tests
  cover the scoring rules, including that no judge score can rescue code that does not run.
- **§2.1 chaos tests — complete.** 22 tests: node vanishing mid-task and its work being reclaimed and
  re-assigned, malformed/empty/refusal output, stale task IDs, circuit breaker opening and recovering,
  two nodes racing for one task, Ollama unreachable, pipeline errors surfacing as messages rather than
  stack traces, and 20 nodes churning with half disappearing without losing queued work.
- Split the janitor loop's body into `_cleanup_pass()` so reclaim is tested directly rather than
  inferred. No behavior change.

#### What is next

1. **On Jett's machine, the three audit items I could not run:** `--demo`, `--demo-showcase`, and the
   MCP flow with real inference.
2. **Then the baseline: `python evals/run_evals.py`.** This is the gate for all of §1.2 — there is no
   point tuning a prompt without a number to move. Expect hours on CPU; it is resumable, so
   interrupting it is safe.
3. Then §1.2 tuning, one prompt change at a time, keeping only changes that move the measured score.

§1.2, §1.3, §2's soak test and all of §3 remain untouched and unclaimed — every one of them needs
real inference or a reachable server.

#### For Jett, in plain language

**1. The repo is PRIVATE. It must be public before launch. Here is exactly how — please do this
yourself, I deliberately did not touch your repository settings:**

1. Go to `https://github.com/Jwrightsman/distributed-orchestrator`
2. Click **Settings** (top right of the repo, next to Insights)
3. Scroll to the very bottom — the red **Danger Zone** box
4. Click **Change visibility** → **Make public**
5. It will ask you to type `Jwrightsman/distributed-orchestrator` to confirm, then click the button

I checked the entire git history first: **your server keys are not in it**, and `config.json` is
excluded from the repo. It is safe to publish.

**2. One thing to know before you flip it:** the sprint notes in the repo contain your server's IP
address (167.233.239.33) and the fact that ports 22 and 8000 are open on it. That is not a leak —
you have to publish the address anyway for people to join — but once the repo is public, anyone can
see the server exists. Both locks (node key and pitch key) are on, so joining or pitching without
your keys returns "unauthorized". I would leave it as is; just don't be surprised by traffic.

**3. The photo you sent has your real keys in it.** Treat that image like a password: don't post it,
and don't include it in the video or any screenshot. If it ever leaks, the fix is to change the two
strings in `data/config.json` on the server and restart it — nothing else breaks.

**4. Nothing else needs your action right now.** The next session's work needs your laptop (the one
with Ollama) rather than a decision from you.

**UPDATE, same session — the repo is now PUBLIC.** Jett flipped it. Verified from outside: a clone
with no credentials works, so a stranger can genuinely get the code now. Consequences worth knowing:

- The CI badge in the README now renders for everyone. It is green.
- Deploying to the live server got simpler — the VM can `git pull` instead of being handed a
  tarball over SSH. Updated above.
- **Everything in this repo is now world-readable, including these sprint files** — which name the
  server (167.233.239.33) and say ports 22 and 8000 are open on it. That was always going to be
  public (people need the address to join), and both locks are on, so this is a "know it, not fix
  it" item. Nothing here contains a key.
- The repo is discoverable but not announced. There is no CTA pointing at the live orchestrator yet
  — the README still says an address "will be posted here". That is the right state until the video
  is recorded; flipping to a real address is a launch-day edit, not a today edit.

#### Addendum — §2 continued (still Aug 5, no laptop needed)

Two more §2 items closed without a model, by driving a **real uvicorn server** rather than an
in-process test client (`scripts/restart_recovery.py`, 17/17):

- **Kill-and-restart recovery works, including SIGKILL.** Jobs and event history survive the
  restart through SQLite; the node registry correctly empties so workers re-register; dashboard and
  landing page render; a WebSocket client reconnects and resumes. That closes the last outstanding
  piece of the "verify under stress" item too.
- **It immediately found a bad failure message.** With Ollama down, a failed job reported httpx's
  `All connection attempts failed` — which is what would appear on the dashboard if Ollama hiccups
  mid-demo. `generate` and `generate_stream` now explain themselves for the two failures an
  audience actually causes: Ollama not running ("Start it with: ollama serve") and the model never
  pulled ("Pull it with: ollama pull <model>"). The timeout path already did this; connection and
  404 did not.

Lesson consistent with the rest of this audit: the in-process test client could not have found
either of these. Running the real thing did.
