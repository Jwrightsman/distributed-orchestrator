# Distributed AI Orchestrator

An open orchestration layer for multi-agent task execution across contributor hardware. A planner agent decomposes work into subtasks, builder agents execute them in parallel across connected machines, and a reviewer agent assembles and validates the final output — all running on local models via Ollama, no cloud APIs required. If this is useful, drop a star

> **Experimental — Phase 0.** Designed for trusted local networks. Not security-hardened. Do not expose to the public internet.

## How it works

```
POST /pitch  (or: python cli.py "your task")
    ↓
PLANNER    decomposes task into 3–5 subtasks with dependency graph
    ↓
BUILDERS   execute subtasks in parallel (locally or across worker nodes)
    ↓
REVIEWER   validates output, assembles final deliverable, flags issues
    ↓
REVISER    (if needed) runs a targeted fix pass on reviewer-flagged issues
    ↓
output/{timestamp}/output.md   + extracted code files
```

All agents run on local models via [Ollama](https://ollama.com). No data leaves your machine unless you connect worker nodes across a network.

## Quick start

**Requirements:** Python 3.12+, [Ollama](https://ollama.com)

```bash
pip install fastapi uvicorn httpx rich
ollama pull gemma3:4b
```

### CLI

```bash
python cli.py "Build a Python script that analyzes a CSV of sales data"
```

### Web dashboard

```bash
python -m uvicorn server:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000/dashboard** — pitch tasks from the UI, watch the pipeline run with live stage progress, view extracted code files, and see the guild standings.

### Persistent projects

Tasks are one-and-done by default. Projects let you iterate on the same thing across multiple sessions — each run loads previous output as context so the AI knows what's already been built.

```bash
# Start a new project
python cli.py --new-project "My App" "Build a FastAPI todo app with SQLite"

# Continue later — the AI remembers what it already built
python cli.py --project my-app "Add user authentication"
python cli.py --project my-app "Add a React frontend"

# List all projects
python cli.py --projects
```

The dashboard sidebar also shows all projects with a **Continue** button that sets context for the next pitch.

Each project stores a `memory.md` file that grows with every run — task history, what was built, key decisions. This gets injected into the planner and reviewer prompts automatically, so nothing gets repeated and each iteration builds on the last.

### Async API

```bash
# Submit a job — returns immediately with job_id
curl -X POST http://localhost:8000/pitch/async \
  -H "Content-Type: application/json" \
  -d '{"task": "Build a REST API for a todo app"}'

# Poll for result
curl http://localhost:8000/jobs/{job_id}

# Continue a project via API
curl -X POST http://localhost:8000/pitch/async \
  -H "Content-Type: application/json" \
  -d '{"task": "Add authentication", "project_id": "my-app-abc123"}'
```

## Worker nodes

Any machine with Ollama can join as a builder node.

```bash
# One-command join (checks deps, pulls model, registers, starts polling):
python join.py http://ORCHESTRATOR_IP:8000

# Or manually:
python node.py --server http://ORCHESTRATOR_IP:8000
```

The orchestrator handles planning and review locally. Builder subtasks are distributed to connected nodes and executed in parallel where dependencies allow. If a node goes offline mid-task, its work is automatically reclaimed and re-queued.

## Project structure

```
cli.py              # Terminal interface
server.py           # Orchestrator: HTTP API, WebSocket events, task distribution
node.py             # Worker node: polls for tasks, runs inference, reports results
join.py             # One-command node setup
dashboard.py        # Live web dashboard (served from server.py)
orchestrator.py     # Core pipeline: plan → build → review → revise
ollama_client.py    # Ollama HTTP client + token streaming
memory.py           # Persistent project memory across sessions
ledger.py           # Contribution ledger (append-only JSON)
extract.py          # Extracts runnable code files from pipeline output
config.py           # Centralized settings (model, provider routing, timeout)
output/             # Saved results, one directory per run
projects/           # Persistent project memory (one dir per project)
events.db           # SQLite event log (survives server restarts)
```

## API reference

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Server + Ollama status, connected nodes |
| `/pitch` | POST | Run pipeline, block until complete |
| `/pitch/async` | POST | Submit job, return `job_id` immediately |
| `/pitch/distributed` | POST | Distribute builders to worker nodes |
| `/jobs/{id}` | GET | Poll async job status and result |
| `/jobs` | GET | List recent jobs |
| `/dashboard` | GET | Live web UI |
| `/ws/events` | WS | Real-time pipeline event stream |
| `/history` | GET | Past pipeline runs |
| `/history/{ts}` | GET | Full run detail with output and code files |
| `/standings` | GET | Contributor rankings by credits |
| `/metrics` | GET | Queue depth, latency, node count, job status |
| `/gallery` | GET | Completed tasks as shareable cards |
| `/projects` | GET/POST | List or create persistent projects |
| `/projects/{id}` | GET | Project metadata, memory, iteration list |
| `/nodes` | GET | Connected worker nodes |
| `/nodes/register` | POST | Worker node registration |
| `/tasks/next` | GET | Worker polls for next task (long-polls 25s) |
| `/tasks/{id}/result` | POST | Worker submits completed task |

## What's built

- [x] Planner → builder → reviewer → reviser pipeline
- [x] Parallel builder execution (wave-based DAG, `asyncio.gather`)
- [x] Distributed execution across worker nodes with automatic task reclaim
- [x] **Persistent project memory** — `projects/<id>/memory.md` injected into every run; planner and reviewer know what's already been built
- [x] Async job API (`/pitch/async`, `/jobs/{id}`) — fire and forget, poll or stream
- [x] Live token streaming — watch builder output appear character by character via WebSocket
- [x] WebSocket real-time event feed with polling fallback
- [x] SQLite event persistence — event history survives server restarts
- [x] Live web dashboard — stage progress, node activity, circuit breaker badges, token stream
- [x] Swarm Gallery — browse all completed tasks, fork any as a new pitch
- [x] Model router — planner/reviewer can route to any OpenAI-compatible API (Grok, OpenAI, Groq) while builders stay local
- [x] Circuit breaker — nodes that fail 3× get blacklisted 60s, auto-recover
- [x] Long-polling on `/tasks/next` — near-zero idle network traffic on worker nodes
- [x] Trace IDs — every pitch gets a UUID that flows through all pipeline events
- [x] Auto-revision loop (up to 2 passes) — reviewer-flagged issues get targeted fixes
- [x] Contribution ledger with guild standings and credit tracking
- [x] Worker node hardware reporting (CPU, RAM, GPU) and auto-reconnect
- [x] `/metrics` endpoint — queue depth, latency, blacklisted nodes, job status

**Planned**
- [ ] Exo integration for model sharding across devices
- [ ] Agent specialization (fine-tuned models per role)
- [ ] Network-layer authentication for node registration

## Hardware

- **Minimum:** 8GB RAM, any CPU — gemma3:4b runs on CPU (~30–60s per agent call)
- **Recommended:** 16GB RAM + GPU — much faster inference
- **Best:** Multiple machines connected as nodes for true parallel execution

## Architecture layers

This is Phase 0 of a three-layer system:

1. **Protocol** — open orchestration layer (this repo)
2. **Guild** — contributor network with compute credits and standings
3. **Marketplace** — commercial layer built on top of the protocol

## Built with

- Python 3.12+ · FastAPI · httpx · rich
- [Ollama](https://ollama.com) for local inference
- gemma3:4b (CPU) · gemma4 (GPU)
