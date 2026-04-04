# Distributed AI Orchestrator

An open orchestration layer for multi-agent task execution across contributor hardware. A planner agent decomposes work into subtasks, builder agents execute them in parallel across connected machines, and a reviewer agent assembles and validates the final output — all running on local models via Ollama, no cloud APIs required.

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

### Async API

```bash
# Submit a job — returns immediately with job_id
curl -X POST http://localhost:8000/pitch/async \
  -H "Content-Type: application/json" \
  -d '{"task": "Build a REST API for a todo app"}'

# Poll for result
curl http://localhost:8000/jobs/{job_id}
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
ollama_client.py    # Ollama HTTP client
ledger.py           # Contribution ledger (append-only JSON)
extract.py          # Extracts runnable code files from pipeline output
output/             # Saved results, one directory per run
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
| `/nodes` | GET | Connected worker nodes |
| `/nodes/register` | POST | Worker node registration |
| `/tasks/next` | GET | Worker polls for next task |
| `/tasks/{id}/result` | POST | Worker submits completed task |

## Implemented vs. planned

**Implemented**
- [x] Planner → builder → reviewer → reviser pipeline
- [x] Parallel builder execution (wave-based dependency resolution)
- [x] Distributed execution across worker nodes
- [x] Automatic task reclaim when a node goes offline
- [x] Async job API (`/pitch/async`, `/jobs/{id}`)
- [x] WebSocket real-time event feed
- [x] Live web dashboard with stage progress, node activity, credit tracking
- [x] Auto-revision pass on `NEEDS_WORK` reviewer rating
- [x] Contribution ledger with guild standings
- [x] Worker node auto-reconnect and re-registration
- [x] Hardware reporting per node (CPU, RAM, GPU)

**Planned**
- [ ] Exo integration for model sharding across devices
- [ ] Persistent task memory across sessions
- [ ] Agent specialization (fine-tuned models per role)
- [ ] Cryptographic ledger integrity (Merkle tree)
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
