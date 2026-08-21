# Mycelium (repo: distributed-orchestrator)

## What this is
A collectively-owned AI system powered by consumer hardware. Execution protocol
v1 supports the existing planner/builder/reviewer DAG and complete-candidate
ensemble execution over the same local or distributed dispatcher.

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
- Every completed run has its own page at /run/{id} — that is the link to share

## Architecture
1. **Execution contract** — strict `ExecutionRequestV1` and normalized durable result
2. **Selector/registry** — deterministic `auto`; production DAG and ensemble strategies
3. **Dispatcher** — placement-independent local/distributed execution units
4. **Validators** — mechanical evidence separated from generation and winner selection
5. **DAG adapter** — existing planner, builders, reviewer/reviser, extraction, and memory
6. **Ensemble adapter** — 1–5 complete candidates; direct is exactly one candidate
7. **Persistence/events** — SQLite execution snapshots plus compatible event stream
8. **Ledger** — tracks contributions (compute, pitches, reviews) with credits

## Key files
- `config.py` / `config.json` — centralized settings (model, timeout, retries)
- `ledger.py` / `ledger.json` — contribution ledger (guild economics seed)
- `extract.py` — auto-extracts runnable code from pipeline output
- `execution/` — contracts, strategy registry, dispatcher, validators, service, SQLite store
- `routes_executions.py` — canonical `POST/GET /v1/executions`
- `docs/PROTOCOL.md` / `docs/ARCHITECTURE.md` — normative behavior and diagrams
- `dashboard.py` — assembles pages: injects `templates/_theme.html`, `_dashboard.css`,
  `_dashboard.js` and fills `<!--SLOT:NAME-->` placeholders. No build step, no npm.
- `templates/` — one file per page. Colours come from `_theme.html` tokens only;
  `tests/test_theme.py` fails the build on a hardcoded colour and
  `tests/test_templates.py` parses the served HTML for unclosed tags.

## Distributed execution
- `server.py` — orchestrator that accepts pitches and distributes builder tasks to worker nodes
- `node.py` — worker that connects to orchestrator, polls for tasks, runs them via local Ollama
- strategy and placement are orthogonal: DAG/ensemble/direct can be local or distributed
- protocol-v1 streams and results echo active attempt, nonce, execution, and unit identity
- documented local fallback applies when allowed and is recorded in the normalized result

## Hardware context
- Jett's machine: 8GB RAM, no GPU (100% CPU inference)
- gemma4 (9.6GB) times out on CPU. Use gemma3:4b for now.
- Model-layer sharding across devices (Exo, llama.cpp RPC) is **not** the direction.
  It needs LAN-class latency because activations cross the wire every token, and the
  only second machine is 216 ms away. Task-level parallelism is the primitive here.
  See ROADMAP §10; the decision and its reasoning are in SPRINT_PHASE2 §4.

## Full project context
- Strategic doc lives in LIFE OS vault: `01 - PROJECTS/In Progress/Distributed AI Orchestrator/_PROJECT.md`
- Three-layer vision: protocol (open) → guild (contributors) → marketplace (commercial)
- This repo is the Phase 0 demo

## API endpoints
- POST /v1/executions — canonical asynchronous versioned execution
- GET /v1/executions/{id} — durable normalized state and result
- GET /health — server + Ollama status
- POST /pitch — run pipeline locally
- POST /pitch/distributed — distribute to worker nodes
- GET /dashboard — live web UI
- GET /run/{id} — permalink for one run (server-rendered, OpenGraph tags)
- GET /status — human-readable network status
- GET /node/{id} — one machine's page
- GET /events?since=N — pipeline event stream
- GET /history — past pipeline runs
- GET /history/{timestamp} — full details of a run
- GET /standings — contributor rankings
- GET /ledger — contribution history
- POST /nodes/register — worker node registration
- GET /nodes — connected nodes
- GET /tasks/next — worker polls for work
- POST /tasks/{id}/result — worker submits result
- POST /tasks/{id}/stream — worker submits attempt-bound token batches

## Instructions for AI assistants working on this codebase

### Who Jett is
- No programming experience. Make all technical decisions yourself.
- Explain things in plain language only when it helps him use the project.
- Never ask him to choose between technical approaches — just pick the right one.

### Security
- ALWAYS warn Jett before any action that opens ports, exposes his IP, or could be accessed by others.
- He can't assess security risk himself. Flag it clearly in plain language.

### What to build next
Read **MASTER_PLAN.md** (project north star), then
**SPRINT_STRATEGY_PROTOCOL.md** and **HANDOFF.md** for current implementation
state. `SPRINT_PHASE2.md` and `SPRINT_AUG2026.md` are historical session logs.
`ROADMAP.md` remains reference, not a work queue.

The bounded execution-strategy sprint is complete. Normal feature-freeze
discipline resumes: do not expand into map, research, debate, consensus,
marketplace, token, blockchain, or sharding work without a new explicit sprint.

### What NOT to do
- Don't rewrite things that work. The pipeline is tested and passes.
- Don't add new big features without testing them. Run `py cli.py "test task"` after changes.
- Don't add dependencies unless absolutely necessary.
- Don't build anything related to crypto, tokens, or blockchain. The ledger is a simple JSON file.
- Don't touch the vision/strategy. That's Jett's domain. Just build what's in the priority list above.
- Don't make network-facing changes without warning Jett.

### Testing
After making changes, verify:
1. `py -m pytest -q` — the complete suite (no Ollama needed). This is
   the fastest signal.
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
- Work on a feature branch and merge through review; do not push unfinished work directly to `master`
