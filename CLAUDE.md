# Distributed AI Orchestrator

## What this is
A collectively-owned AI system powered by consumer hardware. Phase 0 demo: local planner/builder/reviewer pipeline hitting Ollama.

## Tech stack
- Python 3.14 (3.14.3 verified Aug 2026) + FastAPI + httpx + rich
- Ollama for local inference (default: qwen3.5:4b — best 8GB CPU-only pick as of Aug 2026;
  auto-detect ladder: qwen3.5 → gemma4 → phi4-mini → qwen3 → gemma3:4b → gemma3:1b)
- No venv currently — deps installed globally via `py -m pip`

## Running it
- `py cli.py` — interactive mode (pitch tasks, view history/standings)
- `py cli.py "your task"` — one-shot mode
- `py cli.py --history` / `py cli.py --standings`
- `py status.py` — check Ollama, models, config
- `py status.py --server` — also check orchestrator + nodes
- `py -m uvicorn server:app --host 0.0.0.0 --port 8000` — orchestrator server
- `py node.py --server http://ORCHESTRATOR_IP:8000` — join as worker node
- `py join.py http://ORCHESTRATOR_IP:8000` — one-command join (checks deps, pulls model)
- Dashboard at http://localhost:8000/dashboard when server is running

## Architecture
1. **Planner** — decomposes task into 3-5 subtasks with dependency graph
2. **Builder** — executes each subtask in dependency order, passing context forward
3. **Reviewer** — checks combined output, rates quality, assembles final deliverable
4. **Extractor** — pulls code blocks from review output into runnable files
5. **Ledger** — tracks contributions (compute, pitches, reviews) with credits

## Key files
- `config.py` / `config.json` — centralized settings (model, timeout, retries)
- `ledger.py` / `ledger.json` — contribution ledger (guild economics seed)
- `extract.py` — auto-extracts runnable code from pipeline output
- `dashboard.py` — web UI with live events, standings, history viewer

## Distributed execution
- `server.py` — orchestrator that accepts pitches and distributes builder tasks to worker nodes
- `node.py` — worker that connects to orchestrator, polls for tasks, runs them via local Ollama
- POST /pitch/distributed — runs planner/reviewer locally, distributes builder tasks to nodes
- Falls back to local execution if no nodes are connected

## Hardware context
- Jett's machine: 8GB RAM, no GPU (100% CPU inference)
- gemma4 (9.6GB) times out on CPU. Use gemma3:4b for now.
- Future: ExoLabs for distributed inference across multiple devices

## Full project context
- Strategic doc lives in LIFE OS vault: `01 - PROJECTS/In Progress/Distributed AI Orchestrator/_PROJECT.md`
- Three-layer vision: protocol (open) → guild (contributors) → marketplace (commercial)
- This repo is the Phase 0 demo

## API endpoints
- GET /health — server + Ollama status
- POST /pitch — run pipeline locally
- POST /pitch/distributed — distribute to worker nodes
- GET /dashboard — live web UI
- GET /events?since=N — pipeline event stream
- GET /history — past pipeline runs
- GET /history/{timestamp} — full details of a run
- GET /standings — contributor rankings
- GET /ledger — contribution history
- POST /nodes/register — worker node registration
- GET /nodes — connected nodes
- GET /tasks/next — worker polls for work
- POST /tasks/{id}/result — worker submits result

## Instructions for AI assistants working on this codebase

### Who Jett is
- No programming experience. Make all technical decisions yourself.
- Explain things in plain language only when it helps him use the project.
- Never ask him to choose between technical approaches — just pick the right one.

### Security
- ALWAYS warn Jett before any action that opens ports, exposes his IP, or could be accessed by others.
- He can't assess security risk himself. Flag it clearly in plain language.

### What to build next
Read **MASTER_PLAN.md** (project north star — direction, launch plan, division of labor),
then **SPRINT_AUG2026.md**'s Session Log for history, then **SPRINT_PHASE2.md** — that is
the *active* plan and cross-session memory as of Aug 5, 2026. Work its items top to bottom,
check them off, and append to its Session Log. Those files override any older priority list,
including anything cached from previous sessions.

Phase 2's priority is **output quality**, measured — not new features. The eval harness in
`evals/` is the instrument; see its README. Do not tune a prompt without re-running it, and
revert changes that don't move the score.

### What NOT to do
- Don't rewrite things that work. The pipeline is tested and passes.
- Don't add new big features without testing them. Run `py cli.py "test task"` after changes.
- Don't add dependencies unless absolutely necessary.
- Don't build anything related to crypto, tokens, or blockchain. The ledger is a simple JSON file.
- Don't touch the vision/strategy. That's Jett's domain. Just build what's in the priority list above.
- Don't make network-facing changes without warning Jett.

### Testing
After making changes, verify:
1. `py -m pytest -q` — the suite (242 tests, no Ollama needed). This is the fastest signal.
2. `ruff check .` — CI fails on this, so run it before pushing
3. `py -c "from server import app; print('server imports ok')"` — server starts clean
4. `py status.py` — Ollama connection works
5. `py cli.py --history` — CLI works with Ollama stopped too (it reads local files only)
6. `py cli.py "Build a hello world Python script"` — full pipeline runs (takes ~5 min on CPU)

For anything touching the planner/builder/reviewer/reviser prompts, the test suite is not
enough — those changes are judged by measurement:
`py evals/run_evals.py --only web_app` for a fast signal, then the full set.
`py evals/run_evals.py --fake` checks the harness itself without a model.

**CI runs Python 3.14.** Passing locally on an older Python is not proof — `asyncio`
in particular behaves differently (3.12+ raises where 3.11 quietly created an event loop).
Check the actual run on GitHub rather than assuming the badge is current.

### Git workflow
- Commit after each logical change with a descriptive message
- Push to origin/master
- Co-Author line: `Co-Authored-By: Claude <noreply@anthropic.com>`
