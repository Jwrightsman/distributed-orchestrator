# Distributed AI Orchestrator

## What this is
A collectively-owned AI system powered by consumer hardware. Phase 0 demo: local planner/builder/reviewer pipeline hitting Ollama.

## Tech stack
- Python 3.14 + FastAPI + httpx + rich
- Ollama for local inference (gemma3:4b for CPU, swap to gemma4 when GPU available)
- No venv currently — deps installed globally via `py -m pip`

## Running it
- `py cli.py "your task"` — CLI interface (local execution)
- `py -m uvicorn server:app --host 0.0.0.0 --port 8000 --reload` — orchestrator server
- `py node.py --server http://ORCHESTRATOR_IP:8000` — join as a worker node
- Output saved to `output/` with timestamps

## Architecture
1. **Planner** — decomposes task into 3-5 subtasks with dependency graph
2. **Builder** — executes each subtask in dependency order, passing context forward
3. **Reviewer** — checks combined output, rates quality, assembles final deliverable

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
