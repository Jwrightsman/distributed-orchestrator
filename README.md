# Distributed AI Orchestrator
experimental, not secure, trusted-network only.
A collectively-owned AI system powered by consumer hardware. Pitch an idea, watch AI agents decompose it into subtasks, build each piece, and assemble a final deliverable — all running on local machines, no cloud APIs.

## How it works

```
You pitch a task
    ↓
PLANNER decomposes into subtasks with dependencies
    ↓
BUILDERS execute each subtask (locally or across worker nodes)
    ↓
REVIEWER checks quality and assembles the final output
    ↓
Output saved to output/
```

The planner, builders, and reviewer are all AI agents running on local models via [Ollama](https://ollama.com). No data leaves your machine unless you connect to a network of nodes.

## Quick start

### 1. Install dependencies

You need [Python 3.12+](https://python.org) and [Ollama](https://ollama.com).

```bash
# Install Python packages
pip install fastapi uvicorn httpx rich

# Pull a model (gemma3:4b works on 8GB RAM)
ollama pull gemma3:4b
```

### 2. Pitch a task from the terminal

```bash
python cli.py "Build a Python script that analyzes a CSV of sales data and generates a report"
```

You'll see the planner decompose it, each builder produce output, and the reviewer assemble the final result.

### 3. Use the web dashboard

```bash
python -m uvicorn server:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000/dashboard** in your browser. Pitch tasks from the UI and watch the pipeline work.

## Join as a worker node

Got a spare machine? Help build things.

```bash
# On your machine:
pip install httpx
ollama pull gemma3:4b

# Connect to an orchestrator:
python node.py --server http://ORCHESTRATOR_IP:8000
```

Your machine registers as a worker node. When someone pitches a task, builder subtasks get distributed to connected nodes. The orchestrator handles planning and review; your machine handles the building.

## Project structure

```
├── cli.py              # Terminal interface — pitch a task
├── server.py           # Orchestrator server — distributes work
├── node.py             # Worker node — join the network
├── dashboard.py        # Live web dashboard
├── orchestrator.py     # Core pipeline: plan → build → review
├── ollama_client.py    # Talks to Ollama's local API
└── output/             # Saved results with timestamps
```

## API

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Server + Ollama status, connected nodes |
| `/pitch` | POST | Run full pipeline locally |
| `/pitch/distributed` | POST | Distribute builder tasks to worker nodes |
| `/nodes/register` | POST | Worker node registers itself |
| `/nodes` | GET | List connected nodes |
| `/tasks/next` | GET | Worker polls for next task |
| `/tasks/{id}/result` | POST | Worker submits completed task |
| `/dashboard` | GET | Live web dashboard |

## The vision

This is Phase 0 of a larger project: a protocol where anyone can pitch an idea and a network of consumer devices builds it. Three layers:

1. **Protocol** (open) — the orchestration layer, free and open-source
2. **Guild** (contributors) — people who contribute compute, ideas, and reviews
3. **Marketplace** (commercial) — where value gets created on top of the protocol

Read more about the vision in the [project docs](https://github.com/Jwrightsman/distributed-orchestrator).

## Hardware requirements

- **Minimum:** 8GB RAM, any CPU (gemma3:4b runs on CPU, ~30-60s per agent call)
- **Better:** 16GB RAM + dedicated GPU (much faster inference)
- **Ideal:** Multiple machines connected as nodes

## Status

- [x] Local planner/builder/reviewer pipeline
- [x] CLI interface
- [x] FastAPI server with API
- [x] Live web dashboard
- [x] Worker node registration and task distribution
- [x] Distributed execution (planner/reviewer local, builders distributed)
- [ ] Exo integration for model sharding across devices
- [ ] Persistent memory across sessions
- [ ] Agent specialization (fine-tuned agents for code, research, design)
- [ ] Stake tracking and contribution ledger

## Built with

- Python 3.14 + FastAPI + httpx + rich
- Ollama for local inference
- gemma3:4b (CPU) / gemma4 (GPU) via Google's Gemma models
