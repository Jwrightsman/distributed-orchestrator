# MASTER PLAN — Mycelium

_Last updated: August 21, 2026._
_This file is the single source of truth for project direction. Any AI assistant working in this repo (Claude Code, etc.) must read this fully before making changes. It overrides older priority lists in CLAUDE.md._

---

## 1. What this project is

A collectively-owned AI orchestration layer that runs on consumer hardware. A planner agent decomposes a pitched task into subtasks, builder agents execute them in parallel across volunteer machines, a reviewer validates and assembles the result, and a reviser auto-fixes flagged issues. Contributions (compute, pitches, reviews) are tracked in an append-only credit ledger — the seed of a guild economy. Phase 0 of a three-layer vision: **open protocol → contributor guild → marketplace**. No tokens, no blockchain, no cloud dependency.

## 2. Where we actually are (honest status)

**Built and working:** full planner→builder→reviewer→reviser pipeline; parallel
wave-based DAG execution; production complete-candidate ensemble execution;
direct-as-one-candidate and deterministic auto-selection; strategy-independent
local/distributed placement; normalized durable execution records; distributed
workers with task reclaim, attempt-bound streams/results, circuit breaker, and
auto-reconnect; persistent project memory; async APIs; events; live dashboard;
gallery; contribution ledger; LAN discovery; demo modes; and deployment guides.

**Never happened:** the demo video. The community posts. External users. The repo has 0 stars and has been dormant since early April.

**Diagnosis:** the bottleneck is distribution, not code. Every additional feature built before external users exist is building in a vacuum.

## 3. Reality check — August 2026

- **Technical feasibility:** proven by our own Phase 0. The pipeline works. Distributed execution works on LAN. Remaining unknowns (WAN latency, stranger nodes, small-model output quality) are testable, not speculative.
- **The lane is still open.** SwarmHarness (arXiv 2605.28764, May 2026) academically validates this exact niche — decentralized, incentive-aligned agent networks *without* blockchain — and explicitly notes no existing system ships the combination. It is a protocol paper, not a product. This repo is a working implementation in the same design space. Reference it in the README for credibility.
- **Non-competitors:** DePIN GPU networks (Render, Spheron, etc.) are token-based compute marketplaces — a different lane we deliberately avoid. Kimi "Agent Swarm" and similar are centralized cloud products — they normalize swarm UX without touching volunteer hardware or collective ownership. OpenClaw remains the single-machine personal-agent king; we are the multi-machine collective, a different animal.
- **Model tailwind:** Qwen3.5 small series (March 2026) — `qwen3.5:4b` is ~2.5GB, a major quality jump for 8GB CPU-only nodes. Gemma 4 E2B/E4B and Phi-4 Mini are the other strong 8GB picks. Our default model and auto-detect ladder must be refreshed.
- **Ecosystem tailwind:** MCP is now a Linux Foundation standard adopted by every major lab. Exposing this orchestrator as an MCP server (post-launch) would let any agent app delegate tasks to the swarm.
- **Outcome tiers (calibrated):** (a) launch + small tester community — very achievable; (b) niche traction, 100+ stars, recurring contributors — plausible with a good video; (c) the full guild/marketplace vision — multi-year, requires collaborators, only reachable through (a) and (b).

## 4. The Prime Directive

**No unbounded feature expansion until the demo video is public.** The user
explicitly authorized the bounded Execution Strategy Protocol v1 sprint on
August 21; that exception is complete and documented in
`SPRINT_STRATEGY_PROTOCOL.md`. It superseded the older freeze only for DAG,
ensemble/direct/auto, placement, validation, persistence, REST/CLI/MCP, and
adjacent worker-attempt hardening. Normal freeze discipline now resumes. In
particular, do not implement map, research, debate, consensus, marketplace,
token, blockchain, or model-sharding features without a new explicit sprint.

### Current backend protocol baseline

- `ExecutionRequestV1` is the one entry contract for REST, CLI, MCP, and legacy adapters.
- DAG and ensemble are the only registered production strategies; direct normalizes to ensemble.
- Strategy and placement are orthogonal, with confidentiality and capability filtering.
- Canonical execution metadata and final normalized results survive SQLite reopening.
- Protocol-v1 streams and results are bound to active node/task/attempt/nonce/unit leases.
- The worker scheduler remains in memory and node admission still uses a shared secret.
- Normative behavior is in `docs/PROTOCOL.md`; diagrams are in `docs/ARCHITECTURE.md`.

## 5. The 30-day launch plan

### Phase A — Revive (Days 1–2, Claude Code)
1. Verify environment: Python, deps (`fastapi uvicorn httpx rich`), Ollama running.
2. Model refresh: attempt `ollama pull qwen3.5:4b`; fall back to `qwen3:4b`, then `gemma3:4b`. Update `config.json` default and the `auto_detect_model()` preference ladder in `ollama_client.py` to: `qwen3.5` → `gemma4` (e2b/e4b) → `phi4-mini` → `qwen3` → `gemma3:4b` → `gemma3:1b`.
3. Planner reliability upgrade: use Ollama structured outputs (`format` with a JSON schema) for the planner call so subtask JSON is schema-enforced by the runtime instead of regex-and-retry. Keep the existing `_extract_json` path as fallback for providers that lack schema support.
4. Run the full test gauntlet: `py status.py`, `py cli.py "Build a hello world Python script"`, `py cli.py --demo` end-to-end. Fix anything broken by dependency or model drift. Do not proceed to Phase B until `--demo` completes cleanly.

### Phase B — Record (Days 3–7, Jett + Claude Code support)
1. Second machine options, in order of preference: (a) a friend's laptop for an evening; (b) an IU computer-lab machine; (c) a free Oracle Cloud ARM VM (24GB RAM free tier — can run `qwen3.5:4b` on CPU) joined as a node. Any of the three makes the distributed story real.
2. Follow `docs/video-setup.md` exactly. Record `--demo-live` with the dashboard visible: planner decomposing, tasks routing to both machines, credits ticking on the leaderboard, second pitch loading memory from the first, reviser firing.
3. Cut to 60–90 seconds. Structure: hook (task typed, both machines light up) → swarm magic (parallel builders, live credits) → memory wow (iteration 2 remembers iteration 1) → self-fix (reviser) → guild payoff (leaderboard) → CTA (`python join.py` one-liner + repo link).
4. If no second machine is obtainable this week, record the honest solo version rather than delaying: "distributed pipeline is built and tested — I need nodes." Shipped honesty beats unshipped polish.

### Phase C — Post (Days 7–10, Jett)
1. Primary: r/LocalLLaMA. Title leads with the result, not the architecture. Refresh `docs/community-pitch.md` first (Claude Code task).
2. Secondary, spaced 1–2 days apart: Ollama Discord, Exo Discord/GitHub Discussions, r/selfhosted.
3. Hacker News "Show HN" only after incorporating first-wave feedback (week 3–4), not day one.
4. Reply to every comment for at least seven days. Feedback replies outrank feature work.

### Phase D — First external nodes (Days 10–30)
1. Private testers first: Tailscale (free). Testers install Tailscale, join the tailnet by invite, run `python join.py http://<tailscale-ip>:8000`. Zero public port exposure; laptop stays the orchestrator.
2. Public alpha (if demand): 24/7 orchestrator on Oracle free tier or Hetzner (~€4/mo). Requirements before exposure: `node_secret` set and enforced on every node endpoint; a `pitch_key` gate on `/pitch*`; rate limits verified; output-directory disk cap. (Claude Code: Section 7.)
3. Turn on GitHub Discussions for coordination. A Discord server only when there are ≥10 active people to talk to.
4. Goal: 3–5 external nodes, 10 real pitches from strangers, every rough edge they hit logged as an issue.

### Day-30 decision point
- **Traction** (external nodes + engaged comments): switch to a user-driven roadmap. Likely candidates: MCP server interface, web-based pitch UX, verification/reputation beyond the circuit breaker.
- **Silence:** one deliberate repositioning iteration (new angle, new demo, one more launch). If still silence, park the project gracefully — polished README, honest status note. It remains a top-tier portfolio piece either way.

## 6. Division of labor

**Jett (human-only tasks):** press record; edit/caption the video (CapCut or DaVinci Resolve, free); create accounts (Oracle Cloud, Tailscale); recruit the second machine; write nothing from scratch — approve and post the drafts; reply to comments; invite testers.

**Claude Code (everything in the codebase):** all of Section 5 Phase A; Section 7 hardening; deployment docs; keeping CLAUDE.md and README current; drafting posts and replies for Jett's approval.

## 7. Technical worklist (Claude Code — launch-path only)

1. Model refresh + structured-output planner (Phase A above).
2. **Security audit for WAN exposure:** confirm `node_secret` is checked on `/nodes/register`, `/tasks/next`, `/tasks/{id}/result`; add optional `pitch_key` (config) required on `/pitch`, `/pitch/async`, `/pitch/distributed` when set; verify rate limiting on pitch endpoints; add a configurable cap on total `output/` size with oldest-run pruning.
3. **`docs/DEPLOY.md`** written for a beginner, three paths: (a) LAN-only for the video, (b) Tailscale private testing, (c) Oracle-free-tier / Hetzner public orchestrator — exact commands, account-signup pointers, and a plain-language security note for each.
4. **Dockerfile + docker-compose** for the orchestrator (optional but strongly preferred — makes the VPS path nearly copy-paste).
5. **README refresh:** status line updated to August 2026; a short "Positioning" paragraph citing SwarmHarness (arXiv 2605.28764) as academic validation of the niche; a prominent "Looking for nodes" CTA with the `join.py` one-liner; model requirements updated to the new ladder.
6. **`docs/community-pitch.md` refresh:** title leads with the demo result; body mentions persistent memory, parallel waves, auto-revision, credits/guild standings, one-command join; ends with the CTA.
7. Keep the standing rules: no crypto/tokens/blockchain; no big rewrites of working code; test after every change; warn Jett before anything network-facing.

## 8. After launch — see ROADMAP.md

This section used to hold a parking lot. It now lives in **[`ROADMAP.md`](ROADMAP.md)**, the
single home for everything not being built right now: the long-term vision, deferred engineering,
the August 2026 external review's findings, and the speculative ideas that predate the code.

Two lists of future work drift apart, so there is only one. **ROADMAP.md is reference, not a work
queue** — every item there is gated on a trigger, and nothing moves into a sprint file until its
trigger fires. The active work is always in `SPRINT_*.md`.

Of the five items this section used to list, two are done: the **MCP server interface shipped**
(five tools, an end-to-end check at 10/10) and **verification and reputation is wired** (sampled
duplicate execution, `verify_rate`, per-node routing weight — off by default). Layer sharding,
agent specialization and the guild charter carried over to ROADMAP §10, §9 and §8 respectively.

## 9. Success metrics

**30-day:** video public; ≥1 external node connected by a stranger; ≥10 external pitches processed; ≥25 GitHub stars (stretch: 100).
**90-day:** ≥5 recurring nodes; first community PR merged; ~~MCP interface shipped~~ — **done Aug 2026, ahead of plan** (five tools, 10/10 end to end with real inference); a named list of the guild's first ten members.

---

_The code has been ready since April. The next commit that matters is a video file._
