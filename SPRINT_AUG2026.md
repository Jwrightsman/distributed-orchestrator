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
- [x] Verify deps (fastapi, uvicorn, httpx, rich) and Ollama; document Python version in CLAUDE.md
- [x] `ollama pull qwen3.5:4b` (fallbacks: qwen3:4b → gemma3:4b); update config.json default
- [x] Update `auto_detect_model()` ladder: qwen3.5 → gemma4 → phi4-mini → qwen3 → gemma3:4b → gemma3:1b
- [x] Planner via Ollama structured outputs (`format` + JSON schema); keep `_extract_json` as provider fallback
- [x] Test gauntlet passes: `py status.py` ✓ · `py cli.py "Build a hello world Python script"` ✓ (PASS, 18 min) ·
      `py cli.py --demo` ✓ (both pitches PASS, 3028s + 3149s, memory carried 2 iterations)

### 1.2 Test suite + CI  _(protects the whole sprint)_
- [x] `tests/` with pytest: unit coverage for orchestrator parsing/validation (`_extract_json`,
      `_validate_subtasks`, cycle detection, rating/issue extraction), ledger, memory, extract, config.
      Mock `generate()` — no Ollama in CI.
- [x] GitHub Actions: ruff + pytest on push; README badge (+ Docker build job)
- [x] All tests green (131 as of this session's end)

### 1.3 Contained refactor  _(only after tests exist)_
- [x] Split server.py (~1,600 lines) into app assembly + route modules (nodes, pitch/jobs, history/gallery,
      projects, ws/events). No behavior changes — tests prove it.
- [x] Move dashboard HTML into `templates/` served by the same route (enables Week 2 polish). No redesign yet.

### 1.4 Security audit (WAN-readiness)
- [x] `node_secret` enforced on /nodes/register, /tasks/next, /tasks/{id}/result — verified, not assumed
      (+ /tasks/{id}/stream, which also carries node data)
- [x] Optional `pitch_key` config; when set, required on /pitch, /pitch/async, /pitch/distributed
- [x] Rate limits verified on all pitch endpoints (/pitch/distributed had none — added)
- [x] Configurable cap on `output/` total size with oldest-run pruning (`output_max_mb`, default 500)
- [x] Plain-language security summary for Jett in the session log

### 1.5 Deploy plumbing + docs
- [x] Dockerfile + docker-compose.yml (orchestrator; node runs via compose `run` command override)
- [x] `docs/DEPLOY.md` for a beginner — three paths: LAN (video), Tailscale (private testers),
      Oracle free tier / Hetzner (24/7 public, with node_secret + pitch_key). Exact commands, signup
      pointers, plain-language security notes.
- [x] README refresh: Aug 2026 status, new model ladder, Positioning paragraph citing SwarmHarness
      (arXiv 2605.28764) as academic validation of the niche, "Looking for nodes" CTA with join one-liner
- [x] CLAUDE.md "What to build next" → points to MASTER_PLAN.md + this sprint file

**Week 1 exit criteria: tests green in CI, --demo clean on qwen3.5:4b, server refactored, WAN-safe, deploy docs done.**
**→ MET, Aug 1 2026.** All five criteria verified; see Session Log.

---

## Week 2 (Aug 8–14): Showpieces — strict priority order

### 2.1 MCP server interface  _(flagship — the video's best moment)_
- [x] `mcp_server.py` using the official Python MCP SDK, exposing tools:
      `pitch_task(task, project_id?)`, `get_job_status(job_id)`, `get_result(job_id)`,
      `list_projects()`, `continue_project(project_id, task)`
- [x] Wraps the existing async job API (localhost:8000) — thin adapter, no pipeline changes
- [x] stdio transport first (Claude Desktop local); streamable HTTP via `--http` flag
- [x] `docs/MCP.md`: Claude Desktop config snippet + 60-second setup
- [x] Verified end-to-end: an MCP client pitches a task → swarm executes → result returns to the client
      — **passed Aug 2** (run `20260802_044626`): real stdio client → `pitch_task` → job ran → polled
      `get_job_status` → `get_result` returned the deliverable. Exposed a reasoning-leak bug, now fixed
      (see session log).
- [x] On-camera flow documented in docs/demo-script.md ("I ask my AI; it delegates to the swarm")

### 2.2 Showcase demo  _(the visual money shot)_
- [x] `--demo-showcase`: pitches "Build a retro Snake game as a single self-contained HTML file with
      neon styling, scoreboard, and keyboard controls"
- [x] Extractor writes the .html; CLI auto-opens it in the default browser on completion
- [x] Tune prompts until the game is reliably playable with qwen3.5:4b — **verified playable in a real
      browser** (run `20260802_043911`): no JS errors, canvas renders, arrow keys steer, score/collision/
      food/game-over/restart all work. Residual quirk: the model sometimes reuses the game-over overlay
      as a start screen, so the file can open showing "GAME OVER" until you click restart. Prompt now
      forbids a start screen outright; docs/demo-script.md carries the recording workaround.
- [x] Keep --demo (expense tracker) as the memory/iteration story; showcase is the visual pop

### 2.3 Dashboard camera polish
- [x] Readable at 1080p recording: type scale (9–11px floor → 12–13px), contrast (#444/#555 lifted), spacing
- [x] Landing page at `/` — one-paragraph what-this-is, live node count, Join + Dashboard buttons
- [x] Node cards animate on task assignment/completion (glow-pulse while working, bright flash on finish)
- [x] Empty states that look intentional on camera (0 nodes, no history)

### 2.4 Public pitch page  _("try it in your browser")_
- [x] `public_pitch: false` by default. When enabled: per-IP limit (2/hour), global queue cap (3 active),
      task length cap (300 chars), basic content filter
- [x] Simple page at `/try`: type a task → watch live progress → see result. No login.
- [x] Plain-language abuse-risk note for Jett (docs/DEPLOY.md section + session log)

### 2.5 One-line joins
- [x] `install.ps1` (Windows) + `install.sh` (Mac/Linux): check Python/Ollama → install deps →
      pull model → run join.py
- [x] README join section: one copy-paste line per OS
- [x] Cross-platform paths/subprocess audit (all subprocess calls list-arg + guarded; `py` launcher
      hints in printed messages neutralized to `python`; .gitattributes forces LF on shell scripts)

**Week 2 exit criteria: MCP flow works end-to-end; showcase demo reliably produces a playable game; dashboard looks good at 1080p; join is one line per OS.**
**→ MET, Aug 2 2026.** MCP round-trip verified with a real client; showcase game verified playable in a
browser; dashboard type/contrast/animations done; one-line installers for both OSes. 174 tests green.

---

## Week 3 (Aug 15–20): Deploy + freeze

### 3.1 Live public orchestrator  _(needs Jett for account creation)_
- [ ] **JETT: create an Oracle Cloud free tier (or Hetzner) account + SSH key** — DEPLOY.md §3a walks
      through it. This is the only remaining blocker in the whole sprint.
- [x] VM setup automated: `deploy.sh` takes a fresh Ubuntu box to a live, secured orchestrator in one
      command (installs Docker, clones, generates node_secret + pitch_key, starts the stack, pulls the
      model, waits for health, prints the join/pitch commands with the real public IP)
- [ ] Jett's laptop + the VM = a real 2-node network before IU
- [x] Fallback if no account before IU: everything deploy-ready — the remaining work is one SSH command,
      well under the 30-minute budget

### 3.2 Verification seed  _(stretch — only if 3.1 lands early)_
- [ ] Occasional redundant execution: same subtask on 2 nodes, outputs compared
- [ ] Per-node quality score from comparisons + reviewer ratings, feeding routing weight
- [ ] Visible on node cards ("reputation") — talkable in the video

### 3.3 FREEZE — end of day Aug 17
- [ ] Full regression: test gauntlet, --demo, --demo-showcase, MCP flow, fresh-clone install on a
      second environment if available
- [ ] Aug 18–20: bug fixes and docs ONLY
- [x] docs/demo-script.md rewritten for the new feature set — six numbered shots with screen content,
      script, and timing: hook on the playing game → MCP delegate moment → parallel nodes + credits →
      memory iteration → self-checking → join CTA. Includes a prep checklist that front-loads every
      failure that has actually bitten a take, and a mid-take recovery section.
- [x] docs/community-pitch.md final pass: title leads with the result; mentions MCP, live network,
      one-line join _(done early, Aug 1)_

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

### Session 2 — August 1, 2026 (continued, after credit top-up)

**Week 1 CLOSED.** `--demo` passed end-to-end: both pitches PASS (3028s + 3149s), memory carried across
2 iterations. All five Week 1 exit criteria met. Merged to master via [PR #14].

**Four output-quality bugs found by inspecting real runs (not by tests) — every one of them was
invisible to the PASS rating:**
1. The demo's extracted Python had a syntax error while the reviewer rated it PASS — the reviewer grades
   prose, not runnability. Added `extract.check_code_files()` (ast.parse for Python, structural checks for
   HTML) plus an automatic repair pass that quotes the exact defect; leftover problems now print as a CLI
   warning so a PASS can never imply "it runs".
2. The showcase run produced a **pygame Python game** for a task that said "ONE self-contained HTML file".
   Cause: builders only ever saw their own subtask prompt, never the parent task, so "implement the game
   logic" was built in whatever language the model felt like. Added `compose_builder_prompt()` shared by
   the local and both distributed paths, and taught the planner to repeat format constraints into every
   subtask. This was an architectural gap affecting every multi-file task, not just the showcase.

3. **The 4096-token ceiling.** Ollama defaults to a 4096-token context; the reviewer's assembled output
   hit it and got cut off mid-statement. The showcase game was truncated this way, and so was the demo's
   pitch-2 output (`output/20260801_235313` ends mid-JavaScript) — a run that was rated PASS. Now sends
   `num_ctx` from config `context_tokens` (default 8192; measured cost on this machine: 5.8GB → 5.9GB).
4. **Unfenced deliverables extracted nothing.** The showcase reviewer returned a complete HTML document
   with no markdown fences, so the extractor — which only looked for fenced blocks — produced zero files
   and the CLI had nothing to open. Now detects a response that opens with a document signature
   (`<!DOCTYPE html`, `<html`, shebangs) and saves it as one file; fenced blocks still take precedence.

**Also:** `docs/community-pitch.md` rewritten for the current feature set (3.3 item, done early).

**The showcase DOES produce a playable game.** Run `20260802_023417` was verified in a real browser:
no JS errors, canvas renders, game loop runs, arrow keys steer, plus score/collision/food/game-over/
restart. One cosmetic defect — the game-over overlay shipped visible on load — now addressed by stating
the required load-time state in the showcase prompt.

**A correction worth recording, because it cost an hour.** I first reported the game had "no game logic".
That was a bad grep: `\|` inside `grep -E` matches a literal pipe, not alternation, so every mechanic
read as absent. Acting on that, I added a "prefer a builder's artifact over the reviewer's merge"
fallback — then removed it once the corrected check showed the merge was fine. **Verify a negative
result before building on it.** Related: raising context to 16384 to "let the reviewer see more" made
the reviewer exceed its timeout and abort the run; 8192 is the configuration that actually works on this
machine. Both changes were reverted. Kept: `num_ctx` being sent at all (Ollama's 4096 default truncated
deliverables), the budget helper that scales with context, and the timeout raise to 1800s.

**WEEK 2 CLOSED (Aug 2).** MCP round-trip passed with a real stdio client — pitch → job → poll → result.
Showcase game verified playable in a browser. 174 tests green.

**Fifth bug, found by the MCP e2e run:** the reviewer's response contained draft haiku lines followed by
an orphan `</think>`, and all of it shipped as the deliverable. Reasoning models emit these tags even
with `think=false`. Worse, it corrupted grading — `_extract_rating` read NEEDS_WORK off the leaked
reasoning when the reviewer had said FAIL. `strip_thinking()` now sanitizes every model response.

**Known-remaining quality issue (not yet addressed):** when the reviewer decides the builders' work is
unusable, it writes a bracketed refusal into the Final Assembled Output section (`[Cannot be assembled
into a complete, usable deliverable...]`) and the pipeline ships that as the result. A rating of FAIL
plus a bracketed-refusal body should probably trigger a rebuild rather than a delivery. Deliberately
NOT fixed on speculation — needs a couple of real occurrences first to design against.

**Week 3 progress without Jett:** `deploy.sh` automates the entire VM setup (3.1's engineering half),
`docs/demo-script.md` is rewritten shot-by-shot, and community-pitch.md was done Aug 1. CI now also
validates the compose file, both shell scripts, and that templates/ ships inside the image.

**Where things stand:** Weeks 1 and 2 closed. Week 3's writing and automation are done. The sprint is
blocked on exactly one thing: **Jett creating a cloud account** (DEPLOY.md §3a). After that, going live
is one SSH command. 3.2 (verification seed) remains deliberately unbuilt — the sprint gates it on 3.1
landing early, and it hasn't landed.

**Next session:** if Jett has an account, run `deploy.sh` on the VM, then join his laptop as a node and
confirm a real 2-node pitch. If not, there is no remaining unblocked sprint work — resist inventing
some; use spare time on the known-remaining quality issue above (reviewer refusal shipped as
deliverable) only if a second occurrence shows up in a real run.

**Next session:** check the showcase output (`output/<latest>/code/*.html`) — does it have a canvas, a game
loop, and keyboard handlers, and does `check_code_files` pass it? Iterate the prompt if not. Then the MCP
e2e pitch. Then Week 3: **Jett needs to create the Oracle Cloud (or Hetzner) account** — that's the only
blocking human task left.

---

### Session 1 — August 1, 2026

**Completed:** 1.1 (all but the final `--demo` gauntlet run, in progress at time of writing) · 1.2 ·
1.3 · 1.4 · 1.5 · 2.1 (all but the real-inference e2e pitch) · 2.2 scaffold (`--demo-showcase` built;
playability tuning pending) · 2.3 · 2.4 · 2.5. 131 tests, CI green with a Docker-build job.

**Fixed along the way:** qwen3.5 is a *thinking* model — hidden reasoning made CPU inference unusable
(7+ min planner calls); now disabled via capability detection (config `think` to re-enable). `--demo`
crashed on legacy Windows console encoding (cp1252) — stdout forced to UTF-8. Two dormant bugs caught
by the new tests: event-log pruning never ran (called a function that didn't exist), and the planner's
fallback JSON parser mis-read single objects containing arrays.

**Learned the hard way:** running anything heavy (test suites, extra servers, Docker) alongside a demo
run on this 8GB machine can starve/wedge the Ollama runner — one demo run died that way. Rule for
future sessions: while a pipeline run is going, file edits only.

**Next session:** confirm `--demo` completed clean (if yes, tick the 1.1 box — Week 1 exits), run the
full MCP e2e pitch (`scratchpad mcp_e2e.py pitch` pattern — spawn client, pitch, poll, assert result),
then iterate `--demo-showcase` until the Snake game is reliably playable (2.2 exit criterion). After
that Week 2 is done and Week 3 (deploy + freeze) begins — 3.1 needs Jett for the Oracle/Hetzner account.

**For Jett, in plain language:**
- *Security:* your server now has two locks (a node key and a pitch key) plus rate limits and a disk
  cap — all verified by automated tests. Both locks are OFF by default, which is fine at home. Before
  anything goes on the real internet, docs/DEPLOY.md walks through turning them on (it's two random
  strings in a config file).
- *The `/try` page:* there's now a page where strangers could type tasks with no password. It is OFF
  by default. Don't turn it on until launch, and only while you're watching — the risk note is in
  docs/DEPLOY.md.
- *Nothing needs your action yet.* The Oracle Cloud account (Week 3) is the next thing only you can do.
