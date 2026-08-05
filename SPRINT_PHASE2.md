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
      run the test suite, `py status.py`, `py cli.py --demo`, `py cli.py --demo-showcase`, and the
      MCP flow end-to-end. Check CI is green on GitHub, not just configured.
- [ ] List anything half-done, stubbed, skipped, or silently failing. Fix or explicitly re-open it
      in SPRINT_AUG2026.md rather than leaving it checked.
- [ ] Verify a **fresh-clone install** works: clone to a clean directory, follow README exactly as a
      stranger would, note every friction point. This is the single most launch-critical check.
- [ ] **Repo visibility:** confirm whether the GitHub repo is public or private. It appears to be
      private or missing as of Aug 5. It must be public before launch. Flag to Jett with the exact
      steps to change it — do not change repository settings yourself.
- [ ] Write an honest audit summary in the Session Log: what's real, what's not, what surprised you.

---

## 1. OUTPUT QUALITY (Aug 5–9) — highest priority

The demo shows the swarm building something. If the output is mediocre, nothing else matters.
This is grinding, iterative work and it is the best possible use of the remaining time.

### 1.1 Eval harness
- [ ] `evals/prompts.json` — 25–30 varied pitches across categories: single-file web app/game,
      CLI tool, data-processing script, small API, algorithm/utility, and 3–4 deliberately vague
      prompts (robustness against ambiguity)
- [ ] `evals/run_evals.py` — runs each pitch through the real pipeline, saves output, scores it
- [ ] Automated scoring per run: (a) did the extractor produce files, (b) does Python parse / does
      HTML have required tags, (c) does it execute without immediate error in a sandbox/subprocess
      with timeout, (d) reviewer-model judgment of "does this satisfy the request" on a 1–5 scale,
      (e) wall-clock time and subtask count
- [ ] `evals/results/` with timestamped JSON + a markdown summary table
- [ ] Baseline run recorded and committed — this is the number to beat

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

- [ ] `tests/test_chaos.py`: node disappears mid-task, node returns malformed/empty output, node is
      very slow (timeout path), node returns a refusal, Ollama restarts mid-pipeline, two nodes
      claim the same task, node reconnects with a stale task ID
- [ ] Verify under stress: circuit breaker opens and recovers, task reclaim reassigns correctly,
      local fallback fires, revision loop terminates (no infinite loops), WebSocket clients survive
      a server restart
- [ ] **Soak test:** 20 consecutive pitches in one server session. Watch for memory growth, SQLite
      bloat, event-buffer leaks, orphaned tasks, degraded latency. Fix what it surfaces.
- [ ] Every failure path produces a clear message rather than a stack trace — assume it happens live
- [ ] Kill-and-restart recovery: server restarts mid-job → nodes reconnect, dashboard recovers state

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
- [ ] Confirm repo is PUBLIC (Jett action)

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
