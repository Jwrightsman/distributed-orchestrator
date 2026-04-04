# Distributed AI Orchestrator

## What this is
A collectively-owned AI system powered by consumer hardware. Phase 0 demo: local planner/builder/reviewer pipeline hitting Ollama.

## Tech stack
- Python 3.14 + FastAPI + httpx + rich
- Ollama for local inference (gemma3:4b for CPU, swap to gemma4 when GPU available)
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
