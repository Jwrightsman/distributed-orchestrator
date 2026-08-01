# SPRINT — August 1–20, 2026

_This file is the tactical execution plan for the pre-IU build sprint. It temporarily amends
MASTER_PLAN.md Section 4 (Prime Directive): the demo video is blocked until Jett returns to IU
(~Aug 20), so this window is for launch-amplifying build work. The amended directive:_

> **Everything built in this sprint must either appear on camera in the launch video or reduce
> friction for the first ten strangers who join. Feature freeze at end of day August 17 —
> after that, bug fixes and documentation only. The video happens the week of August 20.**

_Claude Code: read MASTER_PLAN.md first, then this file. Work top to bottom. Check items off
(`- [x]`) and append to the Session Log at the bottom as you complete work, then commit this
file — it is the sprint's persistent memory across your sessions. If the repo is ever left in
a broken state at the end of a session, fixing it is the first task of the next session. The
repo must be demo-able at all times._

---

## Week 1 (Aug 1–7): Foundation

### 1.1 Revive + model refresh
- [ ] Verify deps (fastapi, uvicorn, httpx, rich) and Ollama; document Python version in CLAUDE.md
- [ ] `ollama pull qwen3.5:4b` (fallbacks: qwen3:4b → gemma3:4b); update config.json default
- [ ] Update `auto_detect_model()` ladder: qwen3.5 → gemma4 → phi4-mini → qwen3 → gemma3:4b → gemma3:1b
- [ ] Planner via Ollama structured outputs (`format` + JSON schema); keep `_extract_json` as provider fallback
- [ ] Test gauntlet passes: `py status.py` · `py cli.py "Build a hello world Python script"` · `py cli.py --demo`

### 1.2 Test suite + CI  _(protects the whole sprint)_
- [ ] `tests/` with pytest: unit coverage for orchestrator parsing/validation (`_extract_json`,
      `_validate_subtasks`, cycle detection, rating/issue extraction), ledger, memory, extract, config.
      Mock `generate()` — no Ollama in CI.
- [ ] GitHub Actions: ruff + pytest on push; README badge
- [ ] All tests green

### 1.3 Contained refactor  _(only after tests exist)_
- [ ] Split server.py (~1,600 lines) into app assembly + route modules (nodes, pitch/jobs, history/gallery,
      projects, ws/events). No behavior changes — tests prove it.
- [ ] Move dashboard HTML into `templates/` served by the same route (enables Week 2 polish). No redesign yet.

### 1.4 Security audit (WAN-readiness)
- [ ] `node_secret` enforced on /nodes/register, /tasks/next, /tasks/{id}/result — verified, not assumed
- [ ] Optional `pitch_key` config; when set, required on /pitch, /pitch/async, /pitch/distributed
- [ ] Rate limits verified on all pitch endpoints
- [ ] Configurable cap on `output/` total size with oldest-run pruning
- [ ] Plain-language security summary for Jett in the session log

### 1.5 Deploy plumbing + docs
- [ ] Dockerfile + docker-compose.yml (orchestrator; node variant if simple)
- [ ] `docs/DEPLOY.md` for a beginner — three paths: LAN (video), Tailscale (private testers),
      Oracle free tier / Hetzner (24/7 public, with node_secret + pitch_key). Exact commands, signup
      pointers, plain-language security notes.
- [ ] README refresh: Aug 2026 status, new model ladder, Positioning paragraph citing SwarmHarness
      (arXiv 2605.28764) as academic validation of the niche, "Looking for nodes" CTA with join one-liner
- [ ] CLAUDE.md "What to build next" → points to MASTER_PLAN.md + this sprint file

**Week 1 exit criteria: tests green in CI, --demo clean on qwen3.5:4b, server refactored, WAN-safe, deploy docs done.**

---

## Week 2 (Aug 8–14): Showpieces — strict priority order

### 2.1 MCP server interface  _(flagship — the video's best moment)_
- [ ] `mcp_server.py` using the official Python MCP SDK, exposing tools:
      `pitch_task(task, project_id?)`, `get_job_status(job_id)`, `get_result(job_id)`,
      `list_projects()`, `continue_project(project_id, task)`
- [ ] Wraps the existing async job API (localhost:8000) — thin adapter, no pipeline changes
- [ ] stdio transport first (Claude Desktop local); streamable HTTP if straightforward (remote orchestrator)
- [ ] `docs/MCP.md`: Claude Desktop config snippet + 60-second setup
- [ ] Verified end-to-end: an MCP client pitches a task → swarm executes → result returns to the client
- [ ] On-camera flow documented in docs/demo-script.md ("I ask my AI; it delegates to the swarm")

### 2.2 Showcase demo  _(the visual money shot)_
- [ ] `--demo-showcase`: pitches "Build a retro Snake game as a single self-contained HTML file with
      neon styling, scoreboard, and keyboard controls"
- [ ] Extractor writes the .html; CLI auto-opens it in the default browser on completion
- [ ] Tune prompts until the game is reliably playable with qwen3.5:4b (iterate; this is output-quality work)
- [ ] Keep --demo (expense tracker) as the memory/iteration story; showcase is the visual pop

### 2.3 Dashboard camera polish
- [ ] Readable at 1080p recording: type scale, contrast, spacing
- [ ] Landing page at `/` — one-paragraph what-this-is, live node count, Join + Dashboard buttons
- [ ] Node cards animate on task assignment/completion (visible from across a room)
- [ ] Empty states that look intentional on camera (0 nodes, no history)

### 2.4 Public pitch page  _("try it in your browser")_
- [ ] `public_pitch: false` by default. When enabled: per-IP limit (2/hour), global queue cap,
      task length cap, basic content filter
- [ ] Simple page: type a task → watch live progress → see result. No login.
- [ ] Plain-language abuse-risk note for Jett before this ever goes live

### 2.5 One-line joins
- [ ] `install.ps1` (Windows) + `install.sh` (Mac/Linux): check Python/Ollama → install deps →
      pull model → run join.py
- [ ] README join section: one copy-paste line per OS
- [ ] Cross-platform paths/subprocess audit (Jett is on Windows; most testers will be Mac/Linux)

**Week 2 exit criteria: MCP flow works end-to-end; showcase demo reliably produces a playable game; dashboard looks good at 1080p; join is one line per OS.**

---

## Week 3 (Aug 15–20): Deploy + freeze

### 3.1 Live public orchestrator  _(needs Jett for account creation)_
- [ ] Jett creates Oracle Cloud free tier (or Hetzner) account + SSH key — DEPLOY.md walks through it
- [ ] Claude Code configures the VM over SSH: Docker or systemd, Ollama + qwen3.5:4b (Oracle ARM 24GB
      handles CPU inference for planner/reviewer; or route planner/reviewer to a free-tier API via the
      existing model router), node_secret + pitch_key set, orchestrator live 24/7
- [ ] Jett's laptop + the VM = a real 2-node network before IU
- [ ] Fallback if no account before IU: everything deploy-ready; 30-minute task from the dorm

### 3.2 Verification seed  _(stretch — only if 3.1 lands early)_
- [ ] Occasional redundant execution: same subtask on 2 nodes, outputs compared
- [ ] Per-node quality score from comparisons + reviewer ratings, feeding routing weight
- [ ] Visible on node cards ("reputation") — talkable in the video

### 3.3 FREEZE — end of day Aug 17
- [ ] Full regression: test gauntlet, --demo, --demo-showcase, MCP flow, fresh-clone install on a
      second environment if available
- [ ] Aug 18–20: bug fixes and docs ONLY
- [ ] docs/demo-script.md rewritten for the new feature set — shot-by-shot, so recording at IU is
      follow-the-script: hook (MCP delegate or typed pitch) → parallel nodes + credits → showcase
      game opens in browser → memory iteration → reviser fires → leaderboard → "join right now" CTA
      with the live address
- [ ] docs/community-pitch.md final pass: title leads with the result; mentions MCP, live network,
      one-line join

**Sprint exit criteria: stable repo, green CI, live (or 30-min-ready) public orchestrator, shot-by-shot
video script, launch post drafted. Jett arrives at IU and records.**

---

## Not in this sprint (do not build)
Exo / llama.cpp RPC model sharding · guild charter or governance code · tokens/blockchain (never) ·
dashboard framework rewrite · agent fine-tuning · mobile anything · MCP surface beyond the five tools ·
any Week-2/3 item before its predecessors' exit criteria are met.

## Standing rules
Test after every change · commit per logical change with descriptive messages + Co-Authored-By line ·
never leave the repo broken at session end · warn Jett in plain language before anything network-facing ·
when in doubt, choose the option that shows up on camera.

---

## Session Log
_Claude Code: append one entry per session — date, what was completed (reference item numbers), what's
next, any warnings for Jett._

<!-- sessions append below -->
