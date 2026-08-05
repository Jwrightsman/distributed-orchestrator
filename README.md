# Distributed AI Orchestrator

[![CI](https://github.com/Jwrightsman/distributed-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/Jwrightsman/distributed-orchestrator/actions/workflows/ci.yml)

An open orchestration layer for multi-agent task execution across contributor hardware. A planner agent decomposes work into subtasks, builder agents execute them in parallel across connected machines, and a reviewer agent assembles and validates the final output — all running on local models via Ollama, no cloud APIs required. If this is useful, drop a star

> **Status (August 2026):** Phase 0 works end-to-end — distributed execution, persistent project memory, auto-revision, live dashboard, credit ledger. Now hardened for WAN use (`node_secret` + `pitch_key` auth, rate limits, disk caps) with a [beginner-friendly deploy guide](docs/DEPLOY.md). Looking for the first external nodes — see below.

## Positioning

Decentralized, incentive-aligned agent networks *without* blockchain are an open lane: [SwarmHarness](https://arxiv.org/abs/2605.28764) (May 2026) maps this exact design space academically and notes that no existing system ships the combination — volunteer consumer hardware, credit-based incentives, no tokens. SwarmHarness is a protocol paper; this repo is a working implementation you can run tonight on two laptops. DePIN GPU marketplaces (token-based) and centralized cloud "agent swarms" are different animals — this is the collectively-owned, local-model one.

## Looking for nodes 🖥️

The network gets real when strangers connect hardware. Joining takes one command — any machine with 8GB RAM:

```bash
python join.py http://ORCHESTRATOR_ADDRESS:8000
```

A public orchestrator address will be posted here when the first community round opens. Until then: run your own on a LAN in minutes, or invite friends over [Tailscale](docs/DEPLOY.md) — and open an issue saying hi if you want in on the first tester wave.

## How it works

```
POST /pitch  (or: python cli.py "your task")
    │
    ├─ PLANNER (local)   decomposes into 3–5 subtasks with dependency graph
    │                    ↓
    │         ┌──────────┴──────────┐
    │         │          │          │     ← independent subtasks run concurrently
    │    BUILDER 1   BUILDER 2  BUILDER 3  (on connected nodes, or local fallback)
    │         │          │          │
    │         └──────────┬──────────┘
    │                    ↓
    ├─ REVIEWER (local)  assembles final output, rates quality
    │                    ↓ (if NEEDS_WORK)
    └─ REVISER (local)   targeted fix pass (up to 2 rounds)
                         ↓
             output/{timestamp}/output.md + extracted code files
```

**`/pitch` vs `/pitch/distributed`**
- `/pitch` or `cli.py` — all agents run locally on your machine
- `/pitch/distributed` — planner and reviewer run locally; builder subtasks are distributed to connected worker nodes and execute in parallel across machines

All agents use local models via [Ollama](https://ollama.com). No data leaves your machine unless you connect worker nodes across a network.

## Quick start

**Requirements:** Python 3.12+ (tested on 3.14), [Ollama](https://ollama.com)

```bash
pip install -r requirements.txt
ollama pull qwen3.5:4b
```

`qwen3.5:4b` (~2.5GB) is the best 8GB-CPU-only pick as of August 2026. No qwen3.5? The orchestrator auto-detects down a ladder: qwen3.5 → gemma4 → phi4-mini → qwen3 → gemma3:4b → gemma3:1b.

### Verify your setup

After installing, run this to confirm everything is wired up correctly:

```
python status.py
```

Expected output:

```
Ollama
  Status:  running
  URL:     http://localhost:11434
  Models:  qwen3.5:4b
  Active:  qwen3.5:4b

Config
  Model:    qwen3.5:4b
  Timeout:  600s
  Retries:  3
```

If Ollama isn't running, start it with `ollama serve` and re-run. If the model is missing, run `ollama pull qwen3.5:4b`.

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

Any machine with 8GB RAM can join as a builder node — one copy-paste line:

**Mac / Linux**
```bash
curl -fsSL https://raw.githubusercontent.com/Jwrightsman/distributed-orchestrator/master/install.sh | bash -s -- http://ORCHESTRATOR_IP:8000
```

**Windows (PowerShell)**
```powershell
$env:SWARM_SERVER="http://ORCHESTRATOR_IP:8000"; irm https://raw.githubusercontent.com/Jwrightsman/distributed-orchestrator/master/install.ps1 | iex
```

The installer checks Python and Ollama, downloads the repo, pulls the model, and starts working. Omit the address to auto-discover an orchestrator on your LAN. Already have the repo?

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
server.py           # App assembly — routers, lifespan, exception handling
server_state.py     # Shared state, SQLite persistence, events, auth, rate limits
routes_pitch.py     # /pitch, /pitch/async, /pitch/distributed, /jobs*
routes_nodes.py     # Worker protocol: register, poll, results, circuit breaker
routes_history.py   # /history*, /share, /gallery
routes_projects.py  # /projects*
routes_events.py    # /health, /events, /ws/events, /standings, /metrics
node.py             # Worker node: polls for tasks, runs inference, reports results
join.py             # One-command node setup
dashboard.py        # Serves the dashboard (HTML in templates/dashboard.html)
orchestrator.py     # Core pipeline: plan → build → review → revise
ollama_client.py    # Ollama HTTP client + token streaming + structured outputs
memory.py           # Persistent project memory across sessions
ledger.py           # Contribution ledger (append-only JSON)
extract.py          # Extracts runnable code files from pipeline output
config.py           # Centralized settings (model, auth keys, provider routing)
tests/              # 114 pytest tests — run with: pytest
docs/DEPLOY.md      # LAN / Tailscale / cloud deployment for beginners
Dockerfile          # + docker-compose.yml: one-command orchestrator + Ollama
output/             # Saved results, one directory per run
projects/           # Persistent project memory (one dir per project)
events.db           # SQLite event log (survives server restarts)
```

## Deploying beyond your machine

See **[docs/DEPLOY.md](docs/DEPLOY.md)** — three copy-paste paths: LAN (no setup), Tailscale (private testers, free), or a 24/7 public orchestrator on Oracle's free tier / Hetzner via `docker compose up`. Each path has a plain-language security note.

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

## Reliability and trust model

This is a **Phase 0 trusted-network prototype**. Here's exactly what's durable and what isn't:

| Thing | Durable? | Notes |
|---|---|---|
| Pipeline output | Yes | Saved to `output/{timestamp}/` on disk after every run |
| Event history | Yes | SQLite (`events.db`) — survives restarts |
| Job status | Yes | SQLite (`events.db`) — `/jobs/{id}` works after restart |
| Project memory | Yes | `projects/<id>/memory.md` on disk |
| Connected nodes | No | Nodes must re-register after server restart |
| Task queue | No | In-flight tasks are reclaimed automatically if a node goes silent; a server restart drops queued-but-not-started tasks |

**Execution guarantees:** Tasks are at-least-once in the distributed path — if a node goes offline mid-task, the task is re-queued and can run on another node. There's no deduplication guard, so in rare race conditions a task may run twice. The final reviewer sees all outputs and merges them, so this typically doesn't affect the result.

**Trust model:** Worker nodes authenticate with a shared secret (`node_secret`), task submission can be gated with `pitch_key`, pitch endpoints are rate-limited (5/min/IP), and `output/` disk usage is capped (`output_max_mb`). All auth is off by default for trusted-network mode; **set both keys before any internet exposure** — [docs/DEPLOY.md](docs/DEPLOY.md) walks through it. No HTTPS out of the box: treat pitched tasks and outputs as public on a public deployment.

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
- [x] Schema-enforced planner output (Ollama structured outputs, with text-parsing fallback)
- [x] WAN hardening — `node_secret` + `pitch_key` auth, rate limits, output disk cap
- [x] Docker + docker-compose deployment; test suite (114) + CI

**Planned**
- [ ] MCP server interface — let any agent app (Claude Desktop etc.) delegate tasks to the swarm
- [ ] Verification & reputation — redundant execution spot-checks, per-node quality scores
- [ ] Exo integration for model sharding across devices

## Hardware

- **Minimum:** 8GB RAM, any CPU — qwen3.5:4b runs on CPU (~1–3 min per agent call)
- **Recommended:** 16GB RAM + GPU — much faster inference
- **Best:** Multiple machines connected as nodes for true parallel execution

## Architecture layers

This is Phase 0 of a three-layer system:

1. **Protocol** — open orchestration layer (this repo)
2. **Guild** — contributor network with compute credits and standings
3. **Marketplace** — commercial layer built on top of the protocol

## Built with

- Python 3.12+ (tested on 3.14) · FastAPI · httpx · rich
- [Ollama](https://ollama.com) for local inference
- qwen3.5:4b (default) · auto-detect ladder for whatever you have pulled
