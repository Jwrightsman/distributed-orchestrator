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
- [x] **Baseline run recorded and committed — this is the number to beat.**
      **v1 baseline: 10/28 = 36%** (`evals/results/20260806_195850`, qwen3.5:4b, ~52 min/prompt).
      Getting an honest number required fixing three scorer/environment bugs; see Session Log 3.

### 1.2 Tune against the baseline
> **UNBLOCKED and underway (Aug 8, on Jett's machine).** First iteration measured and promoted:
> **v3 scores 17/28 = 61% against v1's 36%**, same prompts, same model, one variable changed.

- [ ] Iterate on planner prompt: subtask granularity (too many tiny subtasks fragments the output;
      too few defeats the swarm story), explicit file-boundary instructions, dependency correctness
      — untouched so far, deliberately: v3 changed only the builder so the gain was attributable.
- [x] Iterate on builder prompt: complete-file output, no placeholders/TODOs, no prose outside code
      fences, consistent naming across subtasks so files actually integrate
      — **v3 (`prompts/v3.py`), promoted to default.** Four rules, each aimed at a measured failure:
      every name must be imported (fixed the api NameErrors), standard library unless the task names
      a package (vague 0/4 → 2/4), trace your own test assertions by hand (algorithm 0/4 → 3/4), and
      no JS console errors (web 2/6 → 3/6). No category regressed.
- [ ] Iterate on reviewer prompt: catch missing integration between subtask outputs specifically —
      that is the failure mode unique to this architecture
- [ ] Iterate on reviser prompt: fix without regressing working parts
- [ ] Re-run evals after each change set; keep changes that move the score, revert those that don't
- [ ] **Target: ≥80% of prompts produce runnable, on-spec output on qwen3.5:4b.** Record the final
      score in the README — a real, measured number is credible and rare in this space.

### 1.3 Showcase reliability
- [x] Run `--demo-showcase` 10 times consecutively; the Snake game must be playable in ≥8
      — **MEASURED AND FAILING: 2/10.** Not 8/10. `scripts/showcase_reliability.py`, 10 real runs
      (~6 hours), each opened in headless Chromium. **0/10** met the strict bar (starts playing on
      load, no "GAME OVER" before play); **2/10** are playable at all once you press start. The other
      8 never draw to the canvas and their restart button does nothing — frame hash stays 0 with no
      JS error to explain it. This is a genuine capability limit, not a scoring artifact: verified by
      hand on a failing run.
- [x] Same for `--demo` (expense tracker + memory iteration)
      — **2/3 clean** (`scripts/demo_reliability.py`, runs 35-56 min each). Checks the demo's
      actual on-camera claim, not just completion: two pipeline runs produced, extracted Python
      parses, and memory.md records BOTH iterations. Two takes were clean — memory carried and the
      code parsed. One aborted after pitch 1 (invalid Python from pitch 1, then pitch 2 failed), so
      memory recorded a single iteration and the demo exited 1. Materially better than the showcase's
      2/10, and the failure is loud rather than silent: the run stops with a red panel and a non-zero
      exit, so a bad take is obvious while filming rather than discovered in the edit.
- [ ] If a demo is flaky, tune its specific prompt until it isn't — these two run on camera
      **Not yet attempted.** Each iteration costs ~6 hours (10 runs) to validate, so this needs a
      dedicated window. Mitigation already in place: one verified-playable game is committed at
      `docs/demo-assets/snake-game/` and docs/demo-script.md tells Jett to press restart before
      rolling. **The video does not depend on a live generation succeeding.**

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
- [x] **Soak test:** 20 consecutive pitches in one server session. Watch for memory growth, SQLite
      bloat, event-buffer leaks, orphaned tasks, degraded latency. Fix what it surfaces.
      — `scripts/soak_test.py`, run at 20 and 60 pitches against a real server with the model
      stubbed (leaks are infrastructure, not model behavior). **60 pitches: +0.9 MB RSS, 96 KB
      SQLite, 0 orphaned tasks, latency flat (-3%), 60 output dirs — clean.** It surfaced one thing:
      the per-IP rate limit was hardcoded at 5/min, so the 6th pitch in a minute 429s. Now
      configurable (`pitch_rate_max` / `pitch_rate_window`), default unchanged.
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
- [x] Measure and record real WAN numbers: task round-trip latency, throughput vs local-only,
      failure rate over a multi-hour run. Put honest numbers in the README; the local-AI community
      respects measured results and punishes vague claims.
      — `scripts/wan_bench.py`, Indiana laptop → Hetzner Germany (167.233.239.33):
      **HTTP round-trip 216 ms median · node registration 218 ms · 8 KB result upload 535 ms ·
      idle long-poll error rate 0.0%.** One real end-to-end pitch completed in 308 s, of which the
      network accounted for ~7 s — **about 2%**. That is the honest headline: over a transatlantic
      link, distribution costs almost nothing, because inference dominates by two orders of magnitude.
      **Measurement caveat worth keeping:** the first attempt reported 1513 ms RTT because it ran
      while the eval saturated the client's CPU — it was timing contention, not the network. Re-run
      on an idle machine. Never benchmark a network from a busy box.
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

### Session 4 — August 10, 2026 · v5 resolved · §1.3 showcase alternatives · verification wired

Branch `claude/mycelium-v5-eval-7bc393`. Jett's machine, real Ollama, prompt set v3 throughout.

#### v5 is deleted, and the comparison found something more useful than v5

The v5 run finished at 09:19 (`evals/results/20260810_041455`, now committed — it lived only in
an untracked worktree and would have been lost). **16/28 = 57% against v3's 17/28 = 61%.**

The mechanism worked exactly as designed: mean subtasks 3.68 → 2.46, `no_files_extracted` 2 → 0,
`parse_failed` 1 → 0. The planner did stop fragmenting single files. The score still did not move,
so the rule in `prompts/v3.py` says delete, and it is deleted.

**The previous session kept it as a "special-purpose set" on the strength of web_app 3/6 → 5/6.
That was wrong, and here is the number that shows it.** Comparing per-prompt records across all
four runs:

| pair | up | down | net |
| --- | --- | --- | --- |
| v1 → v3 | 9 | 2 | **+7** |
| v3 → v4 | 4 | 10 | **-6** |
| v3 → v5 | 7 | 8 | **-1** |
| v4 → v5 | 10 | 5 | +5 |

**Between any two runs, 11–18 of the 28 prompts flip outcome.** Half the set is unstable run to
run. v3 vs v5 is 8 discordant one way and 7 the other — McNemar p ≈ 1.0. The web_app "gain" is
3 up and 1 down across six prompts, p ≈ 0.63: four coin flips landing 3–1.

So the instrument resolves large effects (+7, -6) and nothing smaller. Written into `v3.py` as a
rule for future sessions, along with the measurement nobody has done: **no prompt set has ever
been run twice**, so prompt-change and run-to-run variance have never been separated. A repeat run
of v3 against itself costs the same ~9 hours as any other run and would give a true noise floor.
That is now the highest-value eval remaining, ahead of any new prompt set.

#### §4 verification wired into the dispatcher (was: module with no callers)

`verification.py` existed and was tested but nothing called it. Now connected, **off by default**
(`verify_rate: 0.0`):

- Sampled duplication — a fraction of builder subtasks also go to a second node. The duplicate
  carries `exclude_node` so `/tasks/next` cannot hand it back to the node being checked.
- `record_comparison` runs in the **background**. Waiting for the second opinion would charge every
  sampled task the slower node's latency for no benefit to the deliverable, and a failing spot
  check must never be able to fail the artifact it was checking.
- `rank()` decides **first refusal, not eligibility**. Nodes pull work here rather than being
  assigned it, so a worse-rated node defers ~1.5s while a better-rated one is also waiting, then
  takes the work regardless. Poor record means offered last, never starved; exclusion stays the
  circuit breaker's job.
- `/nodes` merges each node's record into its payload; dashboard node cards show routing weight,
  agreement and sample count — hidden until a node has actually been checked.

Behaviour at `verify_rate=0` is unchanged by construction: the deferral only triggers when waiting
nodes have *different* weights, which cannot happen before any sampling has occurred. 23 tests.

`README.md` and `docs/community-pitch.md` now describe the mechanism instead of promising it,
with both honest caveats: it costs a whole extra inference per sampled task, and it self-disables
below two nodes.

#### §1.3 the showcase, solved by changing the artifact rather than the prompt

`docs/showcase-ceiling.md` closed with a guess: a less coupled visual deliverable would hit the
same "it opens in your browser" moment with far less integration risk. Measured — same harness,
same model, same prompt set (v3), same real-browser checks, round-robin so partial results stay
comparable:

| showcase | screen (n=4) | confirmation | avg run |
| --- | --- | --- | --- |
| `chart` — labelled bar chart | **4/4** | running to n=10 | 22 min |
| `clock` — animated analog clock | 3/4 | — | 28 min |
| `particles` — drifting particle field | 3/4 | — | 20 min |
| `snake` — playable game | (2/10, prior) | — | ~50 min |

Every alternative beat the game at roughly half the runtime. **The chart is the winner and is safe
to generate live on camera**, which upgrades the video's money shot from "here is what it produced"
to "watch it produce one now". The game stays as `--demo-showcase` and as the honest hard case.

**Both of my predictions were wrong, which is the argument for measuring rather than reasoning:**

1. The particle field should have been unfailable — no correct answer to get wrong — and it threw
   `Cannot access 'particles' before initialization` and drew nothing. No correctness criterion
   does not protect you from the code not running.
2. The clock's single failure is the camera-fatal kind: a neon rim, **no hands, no hour markers**,
   and a digital readout still ticking underneath. It looked alive by two of three obvious signals.
   Verified genuine by a **full-pixel** canvas diff (0 of 360,000 bytes changed over 2.5s) plus a
   screenshot — the checker's subsampled hash flagged it, but a subsampled hash is exactly the kind
   of evidence this project has been burned by, so it was settled by running the artifact.

New: `showcase.py` holds the pitches and per-artifact checks, imported by both `cli.py` and the
harness, so the thing measured cannot drift from the thing demoed. `--demo-showcase [id]` selects
one; bare `--demo-showcase` is still Snake so every existing doc and the 2/10 number stay
reproducible. `scripts/showcase_rescore.py` re-scores saved artifacts, the showcase equivalent of
`evals/rescore.py`.

#### A months-old production bug, found by verifying a UI change in a real browser

`requirements.txt` pinned bare `uvicorn`, which ships **without a WebSocket implementation**.
`/ws/events` returned **404 on every deployment** — verified on a fresh local server, on Jett's own
server, and on the **live orchestrator**. The dashboard has been silently falling back to 3-second
polling, and live token streaming has never worked outside a machine that happened to have
`websockets` installed for another reason.

Nothing caught it because **no test drives the WebSocket through a real server**, and `TestClient`
implements WebSockets itself rather than going through uvicorn's protocol layer — so a TestClient
test passes whether or not a deployed server can accept a connection. Same lesson as the
restart-recovery and soak work: only running the real thing finds this class of bug.

Fixed by declaring `websockets` explicitly (cheaper than `uvicorn[standard]`, which drags in five
more packages). Verified after the fix: the upgrade request returns **101** and the dashboard
console is clean. `tests/test_runtime_deps.py` pins it, since CI installs from requirements.txt.

**Outstanding and needs Jett:** the live orchestrator still serves 404 until it is redeployed, and
that needs the SSH key on his laptop. Merge to master first, then `git pull && docker compose up -d
--build` on the VM.

#### Org multi-tenancy: still not built, deliberately

Agreed with Jett's call. It is enterprise plumbing for a system with zero external users, it is in
neither MASTER_PLAN's roadmap nor its parking lot, and an org can already get a private pool by
running its own instance. Revisit only if a real person asks.

### Session 3 — August 6–8, 2026 · §1.1 baseline + §1.2 first iteration (Jett's machine, real Ollama)

**§1.1 baseline: v1 = 10/28 (36%).** `evals/results/20260806_195850`, qwen3.5:4b, ~52 min/prompt,
~20 hours wall clock.

**§1.2 first iteration: v3 = 17/28 (61%), promoted to default.** `evals/results/20260808_050610`.
Same prompts, same model, one variable — only the builder prompt differs (planner/reviewer/reviser
are byte-identical to v2), so the gain is attributable. No category regressed:

| category | v1 | v3 |
| --- | --- | --- |
| algorithm | 0/4 | **3/4** |
| vague | 0/4 | **2/4** |
| web_app | 2/6 | **3/6** |
| api | 2/4 | **3/4** |
| cli_tool | 3/5 | 3/5 |
| data_processing | 3/5 | 3/5 |

The two big gains map exactly to the two rules aimed at them: "trace one input through your own
implementation by hand" for the algorithm category, and "standard library unless the task names a
package" for the vague one. That is the measurement loop working — the rules were derived from the
baseline's actual failure text, not from taste.

**Three scorer/environment bugs found, each of which moved the number.** Worth recording because
every one made the instrument dishonest in a different direction:

1. **Windows cp1252 crash killed 44% of the first attempt.** Any deliverable containing an emoji
   crashed `write_text`. Fixed repo-wide (`encoding="utf-8"` everywhere); the invalidated run was
   deleted rather than kept as a comparable number.
2. **Correct CLI tools scored as broken.** The harness runs code with no arguments, so a tool that
   requires them exits non-zero with a usage message — counted as "does not run". Three genuinely
   correct tools were being failed. `needs_args` now counts as ran, with a test proving a usage line
   *inside* a traceback still fails.
3. **`SystemRoot` was stripped from the exec environment**, so on Windows every socket call died with
   `WinError 10106` before reaching any of the program's own logic. Three API servers were scored as
   broken code. Proven by opening one socket with and without the variable.

**The most important correction went the other way.** Playwright was never installed, so every HTML
run silently fell back to a static structure check — the "uncaught JS errors fail the run" criterion
had never once executed. Installing chromium and re-scoring **lowered** v1 from 39% to 32% (before
the socket fix), because two "passing" web apps throw on load. A measuring instrument that gets
stricter and finds more problems is the one to trust; 39% was the comfortable number, 36% is the
true one.

**Method note for future sessions:** `evals/rescore.py` recomputes the mechanical checks for a
finished run from its saved artifacts. All four corrections above were applied to already-completed
runs in seconds instead of re-running ~20 hours of inference. Judge scores carry over; the original
`results.jsonl` is preserved as `.pre-rescore`.

**Running at time of writing:** `scripts/showcase_reliability.py --runs 10` (§1.3) — generates the
Snake game ten times and checks each in headless Chromium for JS errors, a painted canvas, a
self-running game loop, arrow-key response, and no "GAME OVER" visible before play. Validated first
against the two games already on disk, both of which it correctly rejects for not starting on load.

**§1.3 measured, and it fails the bar: the showcase is 2/10, not 8/10.** Ten real runs, each checked
in a real browser. Zero met the strict bar (playing on load); two are playable after pressing start;
the other eight never draw anything and their restart button is inert — with no JS error to explain
it, which is why only actually running them found this. One failing run was verified by hand to rule
out a checker artifact.

**What that means for the video, plainly:** do not generate the game live on camera. A verified
playable one is committed at `docs/demo-assets/snake-game/` and the demo script already says to press
restart before rolling. The showcase is a "here is what it produced" shot, not a "watch it produce
one" shot. That is an honest framing — the deliverable is real, it was made by the swarm, and the
video never has to claim it works first time.

**Two open leads if a future session has ~6 hours to spend on this:** the failures are consistent
(canvas never painted, restart handler inert), which smells like the reviewer merging a start-screen
state machine it does not fully wire up, rather than random model noise. Worth reading the builder
transcripts of a failing run against a passing one before touching the prompt — `docs/demo-assets/`
now holds a passing example to diff against.

**Still open:** a re-measured §3 WAN number (the first attempt is unusable — it was taken while the
eval saturated the CPU, so it timed contention rather than the network), `--demo` reliability, and
the planner/reviewer prompt iterations.

---

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

**Soak test done too (§2), same trick — real server, stubbed model.** 60 consecutive pitches:
+0.9 MB RSS, 96 KB SQLite, zero orphaned tasks, latency flat, one output directory per pitch (which
also proves the run-directory collision fix holds under back-to-back load). No leaks to fix.

It surfaced one operational thing: **the per-IP pitch rate limit was hardcoded at 5/minute**, so the
6th pitch inside a minute returns 429. That is a sane public default but it blocked the soak, and it
would bite on camera during a run of quick demo pitches. Now configurable via `pitch_rate_max` /
`pitch_rate_window`, default unchanged.

**Two §5 launch-prep items done early** (both were listed but missing):
- README now has an honest **Limitations** section — small-model ceiling, CPU slowness, generated
  code is not sandboxed, trusted-network rather than trustless, single orchestrator with no failover.
  It also states the verified restart/soak numbers, which is the kind of measured claim r/LocalLLaMA
  rewards.
- `docs/community-pitch.md` now carries **six pre-written replies** to the questions that post will
  actually get: how this differs from Exo/llama.cpp RPC, why not Celery, what stops a malicious
  node, how good the output really is, is there a token, and what you need to join. Written to be
  posted in under a minute without thinking.

**Still genuinely blocked on a machine with Ollama:** the eval baseline and everything downstream of
it (§1.2, §1.3), plus `--demo`, `--demo-showcase`, and the MCP flow with real inference. §3 needs
the SSH key, which lives only on Jett's laptop.

#### Addendum 2 — the §1.2 schedule problem, fixed before it bit

Sizing the work revealed a planning problem worth more than another test: **a full local eval run is
15-25 hours on CPU** (28 prompts × planner + 3-5 builders + a reviewer that re-emits the whole
deliverable). One baseline at that cost is fine. But §1.2 wants a re-run after *every* prompt change,
and at a day per iteration that does not fit in the days remaining. The tuning loop, as specified,
was not executable.

Three changes to the harness make it executable:

- **`--orchestrator URL --pitch-key KEY`** runs the pitches on a machine that has the model — the
  24/7 server, a spare desktop — instead of pinning the laptop. The deliverable comes back as text,
  is re-extracted locally and scored by the same code, so the number means the same thing. Verified
  end-to-end against a real server (auth, polling, `/history` fetch, local re-extraction, headless
  browser execution) plus both failure paths: a wrong pitch key and an unreachable host both record
  a readable error rather than dying.
- **`--concurrency N`** puts N pitches in flight. Pointless against one local Ollama (they contend
  for the same CPU) — it exists for the remote case.
- **`--retry-failed`** re-runs prompts that errored on a previous `--resume`. Without it, a prompt
  that died on a network blip three hours into a 20-hour run was silently dropped from the results.

Also `--no-judge`, for scoring a remote run from a laptop with no model. It produces a deliberately
**weaker, mechanical-only score**; `is_success` takes an explicit flag, the summary records it, and
the runner prints a warning, so a mechanical number can never be quietly compared against a judged
one. Two tests pin that.

**Recommended loop for the next session,** now written into `evals/README.md`: iterate on
`--only web_app` (six prompts, ~3-5 hours, and the category the demo depends on), then pay for a
full run before believing a change.

---

### Session 2 — August 5, 2026 (later) · Step 1 verdict + launch-readiness

#### Addendum 3 — §1.2 is BLOCKED from a cloud session. Proof, not assumption.

Jett asked the right question: is §1.2 *actually* blocked, or was that assumed? Tested directly.

**Result: (b) not reachable — network restriction in the session environment.**

| Test | Result |
| --- | --- |
| `167.233.239.33:8000` direct TCP (no proxy) | timeout |
| `167.233.239.33:8000` via agent proxy | timeout |
| `167.233.239.33:22` raw socket | timeout |
| `api.github.com:443` | **HTTP 200** |
| `portquiz.net:443` (third-party control) | **OK** |
| `portquiz.net:8000` (third-party control) | timeout |
| `portquiz.net:22` (third-party control) | timeout |

The controls are the point. `portquiz.net` exists to answer "is this port open outbound" and it has
8000 and 22 listening — both time out, while 443 to the same host succeeds. **The restriction is
this environment's egress policy, not the orchestrator.**

**This says nothing about whether Jett's server is healthy.** It very likely is; this session simply
cannot see it. Do not read this as an outage.

**Consequence:** the `--orchestrator` remote path built last session — verified working against a
real server — cannot help here, because the blocked hop is the network itself. §1.2, §1.3, the eval
baseline, `--demo`, `--demo-showcase`, the MCP flow with real inference and all of §3 are unstartable
from a cloud session. They need a session with local Ollama, or one whose network allows arbitrary
outbound ports.

**The pitch key was deliberately NOT requested.** Jett offered to paste it from the Hetzner console.
It would be useless — no key opens a blocked TCP port — and it is a live credential. Asking for a
secret that cannot help is pure downside. If a future session *can* reach port 8000, the harness
reads it from `PITCH_KEY` in the environment or an untracked `.pitch_key` file; `.gitignore` covers
both. Nothing needs it committed, ever.

#### Step 3 — launch-readiness work (all doable without a model)

- **Governance files the public repo lacked.** LICENSE (MIT), CONTRIBUTING.md, three issue templates
  (including a dedicated **Node won't connect** form — the failure that actually stops people
  contributing — which asks for the four things that diagnose it), a PR template, and an issue
  chooser linking Discussions and DEPLOY.md.
- **Versioned prompt sets (`prompts/`).** v1 is the current prompts, extracted **byte-identical**
  and pinned by tests so a reword has to be a conscious decision to break comparability. v2 is an
  **unmeasured candidate** aimed at the failure modes this project has actually logged (wrong output
  format, cross-agent name drift, truncation, reviewer refusals shipped as the deliverable) — it
  exists so the next session with a model can measure in one command instead of starting cold.
  Select with `--prompt-set`, `PROMPT_SET`, or `prompt_set` in config.json; the run summary records
  which set produced a score.
  - Two bugs found while wiring it: `routes_pitch` bound `BUILDER_SYSTEM` **by value** at import, so
    a prompt switch would have applied to local builds but not to work sent to worker nodes — the
    two halves of a comparison silently disagreeing. And the **Dockerfile would have shipped without
    `prompts/`** (`COPY *.py` skips package directories), dying at startup with ModuleNotFoundError.
    Verified both ways by simulating the image's exact file layout; CI now asserts the sets load
    inside the built image.
- **README read cold as a stranger.** Four things were wrong, not merely improvable: Quick start
  never said to **clone the repo** (it opened with `pip install -r requirements.txt`, impossible
  before you have the repo); the documented `status.py` output showed `Timeout: 600s` against a real
  default of 1800 and omitted four lines the command prints; "114 pytest tests" (259); and
  `prompts/`, `evals/`, `scripts/` were missing from the structure. All 11 relative links verified.
  Two things were checked and found **correct** rather than "fixed" — `install.sh` does forward its
  argument and `install.ps1` does read `$env:SWARM_SERVER`.
- **Docker build:** no daemon in this environment, so it could not be built here. CI builds it on
  every push and is green, and the image's file layout was simulated locally both with and without
  the fix to confirm the diagnosis.
- **Demo fallback assets — tooling built, folder deliberately EMPTY.**
  `scripts/capture_demo_asset.py` archives a real run (deliverable, code, plan, review, builder
  transcripts) into the committed `docs/demo-assets/`, with a manifest recording model, prompt set,
  rating and mechanical checks. It **refuses** to capture a run whose code fails `check_code_files`
  unless forced, so "known good" means something.
  **Nothing was captured, on purpose.** Every asset must be a real run captured verbatim, and this
  session had no model. Hand-writing plausible "example output" would have put fabricated material
  in the exact folder Jett reaches for when a take goes wrong — worse than an empty folder. First
  two to capture, both in the demo script: `--demo-showcase` (Snake) and `--demo` (memory across
  two iterations).
