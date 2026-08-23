# Mycelium

**An open orchestration layer for multi-agent task execution across contributor hardware.**

[![CI](https://github.com/Jwrightsman/distributed-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/Jwrightsman/distributed-orchestrator/actions/workflows/ci.yml)

The execution protocol can decompose work into a planner/builder/reviewer DAG or generate complete ensemble alternatives, with local and distributed placement selected independently. Generation uses local Ollama models by default; optional external-provider routing is an explicit operator choice, not a requirement.

> The repository is still named `distributed-orchestrator`; **Mycelium** is the project name. The slug is deliberately left alone so existing clone URLs and links keep working.

If this is useful, drop a star.

**Checking it's alive without joining:** every orchestrator serves
[`/status.json`](docs/DEPLOY.md) — node count, tasks completed, uptime and
active model, no auth required. **Agents:** read [AGENTS.md](AGENTS.md) first;
it states plainly that installing this on a machine requires that machine
owner's consent.

> **Status (August 2026):** The current target is a **small private trusted alpha**. Execution protocol v1 has DAG and ensemble strategies, direct-as-one-candidate, deterministic auto-selection, durable server-authoritative worker attempts, truthful lifecycle/assurance fields, authenticated artifact delivery, and explicit redacted shares. The scheduler is still process-local, node identity is still a shared secret, and generated code is not sandboxed. See [the protocol](docs/PROTOCOL.md), [access model](docs/ACCESS_CONTROL.md), and [threat model](docs/THREAT_MODEL.md).

## Positioning

Decentralized, incentive-aligned agent networks *without* blockchain are an open lane: [SwarmHarness](https://arxiv.org/abs/2605.28764) (May 2026) maps this exact design space academically and notes that no existing system ships the combination — volunteer consumer hardware, credit-based incentives, no tokens. SwarmHarness is a protocol paper; this repo is a working implementation you can run tonight on two laptops. DePIN GPU marketplaces (token-based) and centralized cloud "agent swarms" are different animals — this is the collectively-owned, local-model one.

## Looking for nodes 🖥️

The network gets real when invited testers connect hardware they own. Joining takes one command — any machine with 8GB RAM:

```bash
python join.py http://ORCHESTRATOR_ADDRESS:8000
```

There is an internet-reachable orchestrator used for status and invited testing, but it is **not a permissionless public network**. Worker endpoints require a shared key and node operators are admitted deliberately. If you want to volunteer a machine you own, open the **[I'd like to join a machine to the network](https://github.com/Jwrightsman/distributed-orchestrator/issues/new?template=join-the-network.yml)** issue. Do not install or join this software on somebody else's machine without that owner's explicit informed consent.

You don't have to wait for that to try it: `python cli.py "your task"` runs the whole pipeline on one machine, and [docs/DEPLOY.md](docs/DEPLOY.md) has a LAN setup that takes minutes, plus a [Tailscale](docs/DEPLOY.md) path for inviting friends to your own instance.

## How it works

All interfaces now construct one versioned request. Strategy (`dag`,
`ensemble`, `direct`, `auto`) and placement (`local`, `distributed`, `auto`)
are separate choices. The diagram below is the default DAG path retained for
legacy requests; ensemble gives the complete task to each candidate and selects
from structured validation evidence. See [architecture](docs/ARCHITECTURE.md).

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

**Compatibility endpoints**
- `/pitch` or `cli.py` — all agents run locally on your machine
- `/pitch/distributed` — planner and reviewer run locally; builder subtasks are distributed to connected worker nodes and execute in parallel across machines
- `/v1/executions` — canonical asynchronous API for explicit strategy and placement combinations

The canonical API defaults to `placement: local` and `confidentiality: local_only`. Remote-capable canonical requests require an explicit `remote_dispatch_consent: true`; any worker that receives a unit can read that unit's prompt. `network_policy` is recorded intent, not an enforced sandbox or firewall.

## Quick start

**Requirements:** Python 3.12+ (tested on 3.14), [Ollama](https://ollama.com) installed and running

> **On Windows, type `py` instead of `python` everywhere below.** Stock Windows
> ships an alias that intercepts `python` and answers *"Python was not found; run
> without arguments to install from the Microsoft Store"* — even when Python is
> installed and working. `py` is the launcher that actually came with it. This
> is the first thing a Windows visitor hits, so it is worth the sentence.

```bash
git clone https://github.com/Jwrightsman/distributed-orchestrator.git
cd distributed-orchestrator
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
  Mode:        local
  Model:       qwen3.5:4b
  Timeout:     1800s
  Retries:     3
  Node auth:   off (any node can join)
  Pitch auth:  off (anyone can pitch)
  Viewer auth: off (private reads are unprotected)
  Role routing: any node
  Provider:    Ollama only
```

`Status: offline` means Ollama isn't running — start it with `ollama serve` and re-run. If the model is missing, run `ollama pull qwen3.5:4b`. The three displayed `off` lines are expected only for loopback local development. Before binding beyond localhost, configure distinct `node_secret`, `pitch_key`, and `viewer_key`; verify public `/health` reports `private_routes_protected: true`. See [access control](docs/ACCESS_CONTROL.md).

`status.py` reads local files only, so it works with Ollama stopped — as do `python cli.py --history`, `--standings` and `--projects`.

### CLI

```bash
python cli.py "Build a Python script that analyzes a CSV of sales data"
python cli.py "Build one HTML file" --strategy ensemble --candidates 3 --placement local
python cli.py "Build coordinated API components" --strategy dag --placement distributed --allow-remote
python cli.py "Use only invited GPU nodes" --strategy ensemble --candidates 3 --placement distributed --allow-remote --confidentiality approved_nodes --approved-node gpu-a
python cli.py "Make one complete attempt" --strategy direct --placement local
```

`--strategy auto` is conservative and records why it selected a strategy.
Candidate count is bounded to 1–5. The CLI retains local placement by default
to preserve its historical behavior. `--allow-remote` is explicit consent to
send assigned unit prompts to invited contributor nodes; those nodes can read
the prompts they receive.

### Web dashboard

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

> `127.0.0.1` means *this machine only*. Use `--host 0.0.0.0` when you actually
> want other machines to reach it — but note that node, pitch, and viewer auth
> are **off by default** in local mode. On `0.0.0.0`, reachable callers could
> submit work or read private runs. Use trusted-alpha mode with all three
> authorities and a private overlay before doing that: [docs/DEPLOY.md](docs/DEPLOY.md).

Open **http://localhost:8000/dashboard** — pitch tasks from the UI, watch the pipeline run with live stage progress, view extracted code files, and see the guild standings.

### Persistent projects

Tasks are one-and-done by default. Projects let you iterate on the same thing across multiple sessions — each run loads previous output as context so the AI knows what's already been built.

```bash
# Start a new project — prints the project id it created
python cli.py --new-project "My App" "Build a FastAPI todo app with SQLite"

# Continue later — the AI remembers what it already built.
# Use the id printed above (a slug of the name, e.g. "my-app").
python cli.py --project my-app "Add user authentication"
python cli.py --project my-app "Add a React frontend"

# Forgotten the id? List all projects (works with Ollama stopped)
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

# Canonical protocol-v1 submission — returns execution_id immediately
curl -X POST http://localhost:8000/v1/executions \
  -H "Content-Type: application/json" \
  -d '{"task":"Build one HTML file","strategy":"ensemble","strategy_options":{"kind":"ensemble","candidates":3,"concurrency":2},"placement":"local"}'

curl -H "X-Viewer-Key: $VIEWER_KEY" \
  http://localhost:8000/v1/executions/{execution_id}
```

Canonical remote placement is opt-in. Set a non-local confidentiality class
and `remote_dispatch_consent: true` together; omitting either is rejected.
Complete artifacts are retrieved through the authenticated manifest/file/ZIP
APIs, not server paths. Public access to a result requires an explicit
revocable share token. Examples: [docs/ARTIFACTS.md](docs/ARTIFACTS.md) and
[docs/ACCESS_CONTROL.md](docs/ACCESS_CONTROL.md).

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

The installer checks Python and Ollama, downloads the repo, pulls the model, and starts working. Omit the address to auto-discover an orchestrator on your LAN.

Piping a script from the internet into your shell deserves a look first — it's short, and reading it is the right instinct: [install.sh](install.sh) · [install.ps1](install.ps1).

Already have the repo?

```bash
# One-command join (checks deps, pulls model, registers, starts polling):
python join.py http://ORCHESTRATOR_IP:8000

# Or manually:
python node.py --server http://ORCHESTRATOR_IP:8000
```

For DAG, planning and review remain on the coordinator while builder units may be distributed. Ensemble candidates may also be distributed as complete-task units. If a node goes offline mid-attempt, its work can be reclaimed and reassigned; only a result that settles the current server-issued attempt can enter execution.

## Project structure

```
cli.py              # Terminal interface
server.py           # App assembly — routers, lifespan, exception handling
server_state.py     # Shared state, SQLite persistence, events, auth, rate limits
access_control.py   # Viewer auth, signed sessions, public/private route policy
routes_pitch.py     # /pitch, /pitch/async, /pitch/distributed, /jobs*
routes_executions.py # Canonical POST/GET /v1/executions API
routes_access.py    # Viewer sessions, artifacts, explicit share capabilities
routes_nodes.py     # Worker protocol: register, poll, results, circuit breaker
routes_history.py   # /history*, /gallery ( /share/{id} redirects to /run/{id} )
routes_run.py       # /run/{id} — legacy private run page when viewer auth is configured
routes_status.py    # /status and /node/{id} — the read-for-humans pages
routes_try.py       # /try, in its open and invite-only states
routes_projects.py  # /projects*
routes_events.py    # /health, /events, /ws/events, /standings, /metrics
node.py             # Worker node: polls for tasks, runs inference, reports results
join.py             # One-command node setup
dashboard.py        # Assembles pages from templates/ (theme + CSS + JS partials)
templates/          # _theme.html (tokens) + _dashboard.css/.js + one file per page
orchestrator.py     # Core pipeline: plan → build → review → revise
execution/          # Contracts, strategies, dispatch, attempts, validation, artifacts, sharing, persistence
ollama_client.py    # Ollama HTTP client + token streaming + structured outputs
memory.py           # Persistent project memory across sessions
ledger.py           # SQLite contribution points + atomic JSON compatibility projection
extract.py          # Extracts runnable code files from pipeline output
config.py           # Centralized settings (model, auth keys, provider routing)
prompts/            # Versioned prompt sets — v1 is the measured baseline
evals/              # Eval harness: prompts, scoring, results (see evals/README.md)
scripts/            # restart_recovery.py + soak_test.py — no Ollama needed
tests/              # pytest suite — run with: pytest -q
docs/DEPLOY.md      # LAN / Tailscale / cloud deployment for beginners
docs/PROTOCOL.md    # Normative execution and worker protocol v1
docs/ARCHITECTURE.md # Strategy/placement/validation/persistence diagrams
docs/audits/        # Historical, non-normative architecture research
Dockerfile          # + docker-compose.yml: one-command orchestrator + Ollama
output/             # Saved results, one directory per run
projects/           # Persistent project memory (one dir per project)
events.db           # SQLite event log (survives server restarts)
```

## Deploying beyond your machine

See **[docs/DEPLOY.md](docs/DEPLOY.md)** for local, private-overlay, and reverse-proxy deployment mechanics. Treat the hosted path as an internet-reachable **private alpha**, not permissionless public infrastructure: configure all three credentials, use TLS or a private overlay, and verify the current [access-control checklist](docs/ACCESS_CONTROL.md). Before launch, run `python scripts/preflight.py`; day-two procedures and recovery commands are in the [trusted-alpha runbook](docs/TRUSTED_ALPHA_RUNBOOK.md) and [operations guide](docs/OPERATIONS.md).

## API reference

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Public sanitized liveness, node count, and viewer-protection warning |
| `/status.json` | GET | Public sanitized status and build fingerprint |
| `/v1/executions` | POST | Queue a canonical versioned execution |
| `/v1/executions/{id}` | GET | Read durable normalized execution state/result (viewer) |
| `/v1/executions/{id}/cancel` | POST | Cancel queued/running execution (viewer) |
| `/v1/executions/{id}/artifacts*` | GET | Sealed deliverable manifest/files and compatibility views (viewer) |
| `/v1/executions/{id}/audit-download` | GET | Provenance/log/candidate audit bundle (viewer) |
| `/v1/executions/{id}/shares` | POST | Create an explicit redacted share (viewer) |
| `/v1/shares/{token}` | GET | Read one redacted execution capability |
| `/pitch` | POST | Run pipeline, block until complete |
| `/pitch/async` | POST | Submit job, return `job_id` immediately |
| `/pitch/distributed` | POST | Distribute builders to worker nodes |
| `/jobs/{id}` | GET | Poll private async job status and result (viewer) |
| `/jobs` | GET | List recent private jobs (viewer) |
| `/dashboard` | GET | Live web UI (viewer when configured) |
| `/run/{id}` | GET | Private legacy run page (viewer when configured) |
| `/status` | GET | Human-readable private network status |
| `/node/{id}` | GET | Private machine contribution page |
| `/ws/events` | WS | Private real-time pipeline event stream |
| `/history*` | GET | Private past-run details and legacy downloads |
| `/standings`, `/ledger`, `/metrics` | GET | Private contribution and operational detail |
| `/gallery` | GET | Private completed-task gallery |
| `/projects*` | GET/POST | Private persistent project APIs |
| `/nodes` | GET | Private connected-worker details |
| `/nodes/register` | POST | Worker node registration |
| `/nodes/{id}/heartbeat`, `/drain` | POST | Session-bound worker liveness and drain control |
| `/tasks/next` | GET | Session-bound worker poll (long-polls 25s) |
| `/tasks/{id}/result` | POST | Session- and attempt-bound completed task submission |
| `/tasks/{id}/tokens` | POST | Session- and attempt-bound token batches with cumulative budgets |

## Reliability and trust model

This is a **Phase 0 private trusted-alpha system**. Here's exactly what's durable and what isn't:

| Thing | Durable? | Notes |
|---|---|---|
| Pipeline output | Yes | Saved to `output/{timestamp}/` on disk after every run |
| Event history | Yes | SQLite (`events.db`) — survives restarts |
| Job status | Yes | SQLite (`events.db`) — `/jobs/{id}` works after restart |
| Normalized execution metadata | Yes | SQLite `executions` table — strategy, placement, candidates, validation, errors |
| Attempt settlement and accepted receipts | Yes | SQLite — exact replay survives restart; active attempts interrupt on restart |
| Share records and token hashes | Yes | SQLite — expiry and revocation survive restart |
| Artifact manifests | Yes | Terminal baselines, roles, hashes, and seal state in SQLite; files remain under registered roots |
| Project memory | Yes | `projects/<id>/memory.md` on disk |
| Connected nodes and node sessions | No | Workers automatically re-register after restart/session expiry |
| Task queue and running coroutines | No | Restart marks affected canonical executions/jobs/attempts `interrupted`; it does not resume them |

**Execution guarantees:** Distributed compute may be attempted more than once after expiry or reclaim, but only the current active server-issued attempt can settle. Settlement, its accepted receipt, replay response, and compute contribution are atomic and durable. Exact replay is idempotent; changed, unknown, queued-but-unleased, expired, reclaimed, cancelled, interrupted, or mismatched results are rejected and kept only in a bounded diagnostic quarantine. The queue itself remains process-local.

**Trust model:** `node_secret` admits workers, `pitch_key` admits task submissions, and `viewer_key` protects task-, result-, project-, event-, artifact-, and machine-sensitive routes. Explicit share tokens expose one allowlisted redacted result instead of making every execution public. All three keys default empty for local compatibility, and `/health` warns when viewer protection is off. **Set the applicable three keys and use TLS or a private overlay before network exposure.** Attempt binding is structural integrity, not per-node cryptographic identity; an admitted worker can still return plausible wrong output. Full details: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

`scripts/trusted_alpha_harness.py`, `scripts/restart_recovery.py`, and `scripts/soak_test.py` are reproducible operational checks, but do not copy an old pass count into a current claim: rerun them against the commit being deployed. The bounded trusted-alpha harness uses fake executors/workers and no live model. Canonical startup reconciliation truthfully interrupts non-resumable queued/running state rather than leaving it active indefinitely.

> **Known issue, measured and open:** memory grows about **1.25 MB per pitch** on Windows — linear over 120 pitches (67 MB → 218 MB), so it is a leak rather than allocator noise. An earlier "+0.9 MB over 60 pitches" figure came from Linux, where the soak's memory probe read `/proc`; on Windows that silently returned "unmeasured" while the script still printed a clean verdict. Both bugs are fixed — the probe is cross-platform and the verdict is now a per-pitch rate — but the leak's source is not yet found and Linux has not been re-measured with the working probe. At roughly 800 pitches per GB it is not a launch risk; it is a real one for any long-lived orchestrator.

> Run the restart check with **Ollama stopped**. It wants pitches to fail fast so a job reaches a terminal state in seconds; with Ollama up they start real multi-minute inference instead and four unrelated checks fail. The script says so on startup.

## Measured results

Numbers, not adjectives. Every one is reproducible with a command in this repo.

**Output quality — about 57% of 28 pitches produce runnable, on-spec output** on `qwen3.5:4b` (95% CI 44–69%), up from 36% before prompt tuning. A run counts only if the extractor produced files, they parse, they *execute* (Python in a subprocess; HTML in headless Chromium, where an uncaught JS error fails the run), the artifact is the kind that was asked for, and a reviewer model rates it ≥3/5.

| prompt set | overall | algorithm | api | cli | data | vague | web |
| --- | --- | --- | --- | --- | --- | --- | --- |
| v1 (baseline) | 10/28 · 36% | 0/4 | 2/4 | 3/5 | 3/5 | 0/4 | 2/6 |
| **v3 (current), run 1** | 17/28 · 61% | 3/4 | 3/4 | 3/5 | 3/5 | 2/4 | 3/6 |
| **v3 (current), run 2** | 15/28 · 54% | 1/4 | 0/4 | 4/5 | 3/5 | 4/4 | 3/6 |

**Those two rows are the same prompt set, the same model and the same 28 prompts, run twice.** They differ by two prompts overall — and **18 of the 28 individual prompts changed outcome between them.** That is the honest error bar on everything above, and it is why the headline is a range rather than the more flattering 61%.

It is also a warning about reading the category columns: `api` went 3/4 → 0/4 and `vague` went 2/4 → 4/4 with *nothing changed*. A category here has 4–6 prompts, and at that size no result reaches significance on its own.

Reproduce: `python evals/run_evals.py` (~20 h on CPU, resumable), then `python evals/compare.py <run_a> <run_b>`, which does the arithmetic — churn, an exact one-sided McNemar test, and the power of the comparison. Raw results are committed under [`evals/results/`](evals/results/).

**Distribution over the internet costs about 2%.** Measured from a laptop in Indiana to an orchestrator in Germany (`scripts/wan_bench.py`):

| | median |
| --- | --- |
| HTTP round-trip | 216 ms |
| Node registration | 218 ms |
| Result upload (8 KB) | 535 ms |
| Idle long-poll error rate | 0.0% |

A real pitch took 308 s, of which the network accounted for ~7 s. Inference dominates by two orders of magnitude, which is why a worker on the other side of an ocean is as useful as one on your LAN.

**What the numbers don't say:** ~57% is not 100%, the interval is wide, and the failures are honest failures — see [Limitations](#limitations).

## Limitations

Stated plainly, because you'll find them anyway:

- **Small models have a ceiling, and here is where it is.** The default is a 4B model on consumer hardware. Measured: **about 57% of 28 varied pitches produce runnable, on-spec output**, 95% CI 44–69% ([Measured results](#measured-results)). It writes a working single-file web app or a useful script; it will not architect your microservice. The rest mostly fail by writing code that looks right and doesn't run.
- **That number is noisy, and we measured how noisy.** Running the identical prompt set twice moved the score from 17/28 to 15/28 and flipped **18 of the 28 individual prompts**. Treat any single eval number here — ours or yours — as ±2 prompts at best, and distrust per-category figures entirely.
- **A one-shot game is at the edge of what it can do — a simpler artifact is not.** Generating a complete playable Snake game succeeded in **2 of 10 consecutive attempts** (`scripts/showcase_reliability.py`, each checked in a real browser). The failures are mostly blank: **6 of the 10 never draw to the canvas at all**, one throws a JS error, and one draws but dies immediately. (Re-scored Aug 15 after three bugs were found in the checker itself — the score was unchanged, the explanation was not.) Treat `--demo-showcase` as something you run a few times and pick from — a verified-playable example is committed at [`docs/demo-assets/snake-game/`](docs/demo-assets/snake-game/).

  **Giving the whole job to one model instead of splitting it may fix it too, and that is measured but not proven:** 22 independent single-model attempts at the same game came back playable **12 times** against decomposition's 2 in 10 (Fisher p = 0.073 — suggestive, short of significant, and the write-up says so). At equal compute the case is stronger, because one decomposed attempt costs about eight single-model attempts. [docs/ensemble-vs-decomposition.md](docs/ensemble-vs-decomposition.md).

  Shrinking the ask fixes it. The same harness, model and prompts produce a labelled bar chart in **10 of 10 attempts** (`python cli.py --demo-showcase chart`), with all seven labels and values correct every time — Fisher exact p = 0.0004 against the game, though ten-for-ten still only puts the true rate at ≥74% with 95% confidence. An analog clock and a particle field both scored 3/4 at a smaller sample. The lesson is about coupling, not about the model being bad: [`docs/showcase-ceiling.md`](docs/showcase-ceiling.md) has the full write-up and `scripts/showcase_results/` has the raw logs.
- **CPU-only is slow.** A full pitch is minutes, not seconds — the planner, each builder, and the reviewer are each a separate model call, and the reviewer has to re-emit the whole deliverable. A GPU changes this dramatically.
- **Generated code is not sandboxed.** The pipeline writes runnable files to `output/`. It checks that Python parses and HTML is structurally sound, but *you* are responsible for reading anything before you run it.
- **The full threat model is written down**, including the deliberate public allowlist, viewer-protected surfaces, share capabilities, malicious-worker limits, and what still blocks a permissionless network: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). Reporting a vulnerability: [SECURITY.md](SECURITY.md).
- **It's a trusted network, not a trustless one.** `node_secret` is a shared admission password, while a short-lived server session prevents active node-ID collisions; neither is public-key identity. A node that authenticates can still return plausible-looking garbage. Experimental sampled redundant execution remains available only in local mode; trusted-alpha mode disables it until post-hoc evidence has durable semantics. Shape agreement is not proof of correctness.
- **One orchestrator, no failover.** DAG planning/review remain on the coordinator; DAG builder units and complete ensemble candidates may run on workers. If the orchestrator goes down, the swarm stops. Durable records reconcile truthfully on restart, but process-local work is not resumed.
- **No HTTPS, no accounts, no multi-tenancy.** This is a prototype you run for yourself or a group you know.

None of these are secrets being kept until someone notices. What would fix each one — per-node
cryptographic identity, a durable scheduler, layered verification, real sandboxing — is written
down in [ROADMAP.md](ROADMAP.md), along with the trigger that would make it worth building. It is
a reference, not a promise of dates.

## What's built

- [x] Planner → builder → reviewer → reviser pipeline
- [x] Parallel builder execution (wave-based DAG, `asyncio.gather`)
- [x] Distributed execution across worker nodes with automatic task reclaim
- [x] Server-authoritative durable attempts, accepted-result broker, exact replay, and bounded quarantine
- [x] Total execution deadline, cancellation API, and restart reconciliation
- [x] Separate lifecycle, validation outcome, assurance, and check summaries
- [x] Viewer-protected private routes and explicit redacted share capabilities
- [x] Path-safe sealed artifact manifests, deliverable/audit bundles, files, ZIPs, hashes, quotas, and retention
- [x] **Persistent DAG project memory** — `projects/<id>/memory.md` is loaded by the DAG pipeline; ensemble/direct reject `project_id` until selected-result-only updates exist
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
- [x] Durable non-monetary compute-contribution points and compatibility standings
- [x] Worker node hardware reporting (CPU, RAM, GPU) and auto-reconnect
- [x] `/metrics` endpoint — queue depth, latency, blacklisted nodes, job status
- [x] Schema-enforced planner output (Ollama structured outputs, with text-parsing fallback)
- [x] Trusted-alpha access controls — separate node, pitch, and viewer credentials; rate/admission limits; output caps
- [x] Docker + docker-compose deployment; model-free regression suite + CI
- [x] **MCP server interface** — five tools, so any agent app (Claude Desktop etc.) can delegate a build to the swarm ([docs/MCP.md](docs/MCP.md))
- [x] Experimental sampled verification in local mode (`verify_rate`, off by default); trusted-alpha mode disables detached sampling until post-hoc evidence is durable

**What comes next is in [ROADMAP.md](ROADMAP.md)** — the long-term vision, the deferred
engineering, and the findings from an external review in August 2026, each one gated on the
evidence that would make it worth building. Nothing there is a promise, and that is the point.

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

## Contributing

The most useful contribution is joining a node and reporting where you got stuck — see [CONTRIBUTING.md](CONTRIBUTING.md). Prompt changes are judged by measurement, not by reading: [`evals/README.md`](evals/README.md) explains the harness and the numbers.

## License

[MIT](LICENSE).
