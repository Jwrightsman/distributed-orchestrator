# Distributed AI Orchestrator

## What this is
A collectively-owned AI system powered by consumer hardware. Phase 0 demo: local planner/builder/reviewer pipeline hitting Ollama.

## Tech stack
- Python 3.14 + FastAPI + httpx + rich
- Ollama for local inference (gemma3:4b for CPU, swap to gemma4 when GPU available)
- No venv currently — deps installed globally via `py -m pip`

## Running it
- `py cli.py "your task"` — CLI interface
- `py -m uvicorn server:app --reload` — FastAPI server (POST /pitch, GET /health)
- Output saved to `output/` with timestamps

## Architecture
1. **Planner** — decomposes task into 3-5 subtasks with dependency graph
2. **Builder** — executes each subtask in dependency order, passing context forward
3. **Reviewer** — checks combined output, rates quality, assembles final deliverable

## Hardware context
- Jett's machine: 8GB RAM, no GPU (100% CPU inference)
- gemma4 (9.6GB) times out on CPU. Use gemma3:4b for now.
- Future: ExoLabs for distributed inference across multiple devices

## Full project context
- Strategic doc lives in LIFE OS vault: `01 - PROJECTS/In Progress/Distributed AI Orchestrator/_PROJECT.md`
- Three-layer vision: protocol (open) → guild (contributors) → marketplace (commercial)
- This repo is the Phase 0 demo
