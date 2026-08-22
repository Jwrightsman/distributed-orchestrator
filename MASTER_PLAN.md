# MASTER PLAN — Mycelium

_Last updated: August 22, 2026._
_This file is the single source of truth for project direction. Any AI assistant working in this repo (Claude Code, etc.) must read this fully before making changes. It overrides older priority lists in CLAUDE.md._

---

## 1. What this project is

A collectively operated AI orchestration layer that runs on consumer hardware.
Canonical requests choose between a planner/builder/reviewer DAG and independent
complete-candidate ensemble execution; direct is one candidate and auto is a
deterministic selector. Placement is a separate local or explicitly consented
distributed decision. Compute, pitch, and review work are recorded as
non-monetary contribution points. There are no tokens, transfers, wallets, or
claims that points prove correctness.

## 2. Where we actually are (honest status)

**Built and working:** planner→builder→reviewer→reviser DAG; complete-candidate
ensemble; direct-as-one-candidate; deterministic auto-selection;
strategy-independent placement; canonical durable execution records; total
deadlines, cancellation, and restart reconciliation; server-authoritative
durable worker attempts and accepted receipts; validator contract floors and
honest assurance; authenticated artifact delivery; explicit redacted shares;
viewer/pitch/node credential separation; privacy-safe canonical defaults;
persistent DAG project memory; REST, CLI, MCP, events, dashboard, contribution
records, LAN discovery, demo modes, and deployment guides.

**Current trust tier:** a small private trusted alpha. Worker identity still
uses a shared secret, the queue is process-local, `network_policy` is not
enforced, generated code is not sandboxed, and there is no permissionless
network defense. Ensemble/direct reject project memory rather than ignoring it.

**Not established by the current repository record:** a published demo video,
repeat external users, or sustained outside adoption. Check current external
state before making a launch-status claim.

**Diagnosis:** the bottleneck is distribution, not code. Every additional feature built before external users exist is building in a vacuum.

## 3. Reality check — August 2026

- **Technical feasibility:** proven by our own Phase 0. The pipeline works and distributed execution works on LAN. Anonymous or hostile-node operation remains explicitly unsupported; WAN behavior and small-model output quality remain measurable rather than reasons to weaken the trust boundary.
- **The lane is still open.** SwarmHarness (arXiv 2605.28764, May 2026) academically validates this exact niche — decentralized, incentive-aligned agent networks *without* blockchain — and explicitly notes no existing system ships the combination. It is a protocol paper, not a product. This repo is a working implementation in the same design space. Reference it in the README for credibility.
- **Non-competitors:** DePIN GPU networks (Render, Spheron, etc.) are token-based compute marketplaces — a different lane we deliberately avoid. Kimi "Agent Swarm" and similar are centralized cloud products — they normalize swarm UX without touching volunteer hardware or collective ownership. OpenClaw remains the single-machine personal-agent king; we are the multi-machine collective, a different animal.
- **Model tailwind:** Qwen3.5 small series (March 2026) — `qwen3.5:4b` is ~2.5GB, a major quality jump for 8GB CPU-only nodes. Gemma 4 E2B/E4B and Phi-4 Mini are the other strong 8GB picks. Our default model and auto-detect ladder must be refreshed.
- **Ecosystem tailwind:** MCP is now a Linux Foundation standard adopted broadly across agent tooling. Mycelium's shipped MCP adapter lets an authorized client submit and inspect canonical-backed work; private reads need the viewer credential.
- **Outcome tiers (calibrated):** (a) launch + small tester community — very achievable; (b) niche traction, 100+ stars, recurring contributors — plausible with a good video; (c) the full guild/marketplace vision — multi-year, requires collaborators, only reachable through (a) and (b).

## 4. The Prime Directive

**No unbounded feature expansion until the demo video is public.** The bounded
Execution Strategy Protocol v1 sprint and Trusted-Alpha Integrity sprint are
the only authorized exceptions. Their records are
`SPRINT_STRATEGY_PROTOCOL.md` and `SPRINT_TRUSTED_ALPHA_INTEGRITY.md`. Normal
freeze discipline resumes after handoff. Do not infer authorization for map,
research, debate, consensus, marketplace, token, blockchain, federation,
accounts, major UI, sandbox, or model-sharding work.

### Current backend protocol baseline

- `ExecutionRequestV1` is the one entry contract for REST, CLI, MCP, and legacy adapters.
- DAG and ensemble are the only registered production strategies; direct normalizes to ensemble.
- Canonical placement defaults local/local-only; remote-capable requests require recorded consent.
- Lifecycle, validation outcome, and assurance are separate and persisted.
- Server-owned attempts, exact replay, accepted receipts, and compute contributions settle durably and atomically.
- Rejected or late output is quarantined outside the operational broker.
- Non-resumable executions/jobs become retryable `interrupted` after restart.
- Complete artifacts use authenticated, path-confined APIs; public result access uses explicit hashed share tokens.
- Viewer, pitch, and node credentials are separate; empty viewer auth is a warned local-development mode.
- The worker scheduler remains in memory and node admission still uses a shared secret.
- Normative behavior is in `docs/PROTOCOL.md`; security boundaries are in `docs/THREAT_MODEL.md`.

## 5. The 30-day launch plan

### Phase A — Revive (Days 1–2, Claude Code)
1. Verify environment: Python, deps (`fastapi uvicorn httpx rich`), Ollama running.
2. Model refresh: attempt `ollama pull qwen3.5:4b`; fall back to `qwen3:4b`, then `gemma3:4b`. Update `config.json` default and the `auto_detect_model()` preference ladder in `ollama_client.py` to: `qwen3.5` → `gemma4` (e2b/e4b) → `phi4-mini` → `qwen3` → `gemma3:4b` → `gemma3:1b`.
3. Planner reliability upgrade: use Ollama structured outputs (`format` with a JSON schema) for the planner call so subtask JSON is schema-enforced by the runtime instead of regex-and-retry. Keep the existing `_extract_json` path as fallback for providers that lack schema support.
4. Run the full test gauntlet: `py status.py`, `py cli.py "Build a hello world Python script"`, `py cli.py --demo` end-to-end. Fix anything broken by dependency or model drift. Do not proceed to Phase B until `--demo` completes cleanly.

### Phase B — Record (Days 3–7, Jett + Claude Code support)
1. Second machine options, in order of preference: (a) a friend's laptop with that owner's explicit consent; (b) an institutional machine only with explicit administrator authorization; (c) a cloud VM Jett controls joined as a node. Any of the three makes the distributed story real without borrowing hardware silently.
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
2. Internet-reachable **private** alpha (if demand): a 24/7 orchestrator may be used only after independent `node_secret`, `pitch_key`, and `viewer_key` configuration; TLS or a private overlay; verified route denial and WebSocket auth; rate/admission and artifact caps; share expiry/revocation; restart reconciliation; and a current branch verification run. This is still not anonymous-node or permissionless readiness.
3. Turn on GitHub Discussions for coordination. A Discord server only when there are ≥10 active people to talk to.
4. Goal: 3–5 invited external node owners, 10 real pitches from testers, every rough edge they hit logged as an issue.

### Day-30 decision point
- **Traction** (invited external nodes + engaged comments): switch to a user-driven roadmap. Likely candidates come from observed tester problems, not speculative strategy expansion.
- **Silence:** one deliberate repositioning iteration (new angle, new demo, one more launch). If still silence, park the project gracefully — polished README, honest status note. It remains a top-tier portfolio piece either way.

## 6. Division of labor

**Jett (human-only tasks):** press record; edit/caption the video (CapCut or DaVinci Resolve, free); create accounts (Oracle Cloud, Tailscale); recruit the second machine; write nothing from scratch — approve and post the drafts; reply to comments; invite testers.

**Claude Code:** visual follow-up consumes the documented backend contracts but owns page layout, CSS, components, and interface copy. Backend agents keep protocol, access, artifact, lifecycle, and threat-model claims accurate. Both sides preserve the sprint boundary and run current checks before handoff.

## 7. Technical launch baseline

The WAN-era node/pitch checks, rate limits, disk cap, deployment guide, Docker
packaging, model refresh, MCP interface, and structured planner are already in
the repository. Trusted-alpha integrity adds the required private-read,
attempt-authority, lifecycle, validation, artifact, share, privacy-default, and
interface contracts. Do not recreate those systems from the older worklist.

Before any deployment or demo:

1. Run the current full suite, Ruff, server import, and Compose configuration;
   record exact results rather than copying a historical count.
2. Configure independent `node_secret`, `pitch_key`, and `viewer_key`; use TLS
   or Tailscale and verify `/health` reports private routes protected.
3. Keep keyless public pitch off unless its fixed local profile and compute caps
   are intentionally wanted.
4. Review generated artifacts before execution; validation is not a sandbox.
5. Verify the deployed build fingerprint and exercise viewer denial, share
   expiry/revocation, artifact download, cancellation, and worker rejection.
6. Keep the standing rules: no money/tokens/blockchain, no permissionless-node
   claims, no major UI work without Claude's handoff, and no new strategy without
   a new explicit sprint.

## 8. After launch — see ROADMAP.md

This section used to hold a parking lot. It now lives in **[`ROADMAP.md`](ROADMAP.md)**, the
single home for everything not being built right now: the long-term vision, deferred engineering,
the August 2026 external review's findings, and the speculative ideas that predate the code.

Two lists of future work drift apart, so there is only one. **ROADMAP.md is reference, not a work
queue** — every item there is gated on a trigger, and nothing moves into a sprint file until its
trigger fires. The active work is always in `SPRINT_*.md`.

Of the five items this section used to list, two are done: the **MCP server interface shipped**
(five tools; rerun its current checks before deployment) and **verification and reputation is wired** (sampled
duplicate execution, `verify_rate`, per-node routing weight — off by default). Layer sharding,
agent specialization and the guild charter carried over to ROADMAP §10, §9 and §8 respectively.

## 9. Success metrics

**30-day:** video public; ≥1 invited external owner connects a machine with informed consent; ≥10 tester pitches processed; ≥25 GitHub stars (stretch: 100).
**90-day:** ≥5 recurring nodes; first community PR merged; MCP interface remains compatible through current regression coverage; a named list of the guild's first ten members.

---

_The code has been ready since April. The next commit that matters is a video file._
