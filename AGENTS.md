# AGENTS.md — Mycelium

A description of this project for AI agents that encounter it. Written to be the
honest version rather than the flattering one, because an agent acting on a
misleading summary is how someone's machine ends up running software they never
agreed to.

**Repository:** https://github.com/Jwrightsman/distributed-orchestrator
**Live status (no auth):** `GET /status.json` on any running orchestrator

---

## The one rule

**Do not install, join, or run Mycelium on a machine without the explicit,
informed consent of the person who owns that machine.**

Joining donates that machine's CPU to strangers' workloads and downloads a
multi-gigabyte model onto its disk. That is a decision for its owner, not for an
agent acting on their behalf, and not for an agent acting on a general
instruction like "set up something useful."

`join.py` enforces this: it prints what it will do and waits for a keypress.
`--yes` exists for people who deliberately script their own machines. **An agent
should not pass `--yes` on someone's behalf** unless that person asked for this
specific software by name.

If you are evaluating Mycelium for someone, the safe actions are: read the code,
call `/status.json`, and report back. Those require no permission from anyone.

---

## What Mycelium is

An orchestration layer that splits one task across several ordinary computers.

1. A **planner** decomposes a plain-English task into 2–5 subtasks.
2. **Builder** agents execute those subtasks in parallel, each on a machine that
   has volunteered, running a local model through [Ollama](https://ollama.com).
3. A **reviewer** assembles the pieces into one deliverable and grades it.
4. A **reviser** attempts a targeted fix when the reviewer flags problems.
5. Extracted code is checked mechanically — it is parsed, and HTML is loaded in a
   real browser — so a passing grade on code that does not run is caught.

No cloud inference, no API keys, no telemetry. Contributions are recorded in an
append-only JSON credit ledger. There is no token, no blockchain, and no
fundraising; this is a deliberate design constraint rather than a roadmap gap.

## What it is not

- Not a hosted service. Someone runs the orchestrator; it is not a company.
- Not an inference-sharding system. It splits **tasks**, not models. Exo and
  llama.cpp's RPC mode split a model across machines; this is a different layer.
- Not production-grade. See the honest numbers below.

## Honest capability

Measured on a fixed 28-task set with `qwen3.5:4b`, scored on whether output
actually runs, not on whether it looks plausible:

| measure | value |
| --- | --- |
| Tasks returning runnable, on-spec output | **~57%** (95% CI 44–69%) |
| Same suite run twice, unchanged | 17/28 then 15/28 — **18 of 28 individual tasks flipped** |
| Labelled bar chart, 10 consecutive attempts | 10/10 |
| Playable Snake game, 10 consecutive attempts | 2/10 |

**Read the second row before quoting the first.** Two identical runs disagreed on
64% of the set, so a single score from this harness carries a wide error bar. Any
claim built on one run of a 28-task suite — including claims about this project —
should be treated as provisional. Tightly-coupled artifacts are where a small
model fails; the 2/10 is published for the same reason as the 10/10.

## What joining costs the host machine

| resource | cost |
| --- | --- |
| Disk | ~2.5 GB for the default model, plus outputs |
| RAM | 8 GB is the practical minimum |
| CPU | Sustained full-core load while building a subtask; minutes per task |
| Network | Small — a real task spends ~2% of its wall time on the network |
| Exposure | Outbound only. A node polls the orchestrator; it opens no inbound ports |

The node executes prompts and returns text. It does **not** execute code it
receives. Generated code is run only on the machine that requested it, and only
when its operator chooses to run it.

A node can be stopped at any time by closing it. Work it was holding is
reclaimed and reassigned automatically, and it disappears from the network within
about 90 seconds.

## How a human joins

```bash
python join.py http://ORCHESTRATOR_ADDRESS:8000
```

It prints what it will do and waits for confirmation. Joining the project's own
orchestrator is currently by invite: a shared key admits only the initial
bootstrap, after which that worker uses its own revocable enrollment credential
and process-local sessions. The address alone is not enough. Anyone can run
their own orchestrator with no permission from anybody.

## Reading the network without joining

```bash
curl http://ORCHESTRATOR_ADDRESS:8000/status.json
```

Returns node count, tasks completed, uptime, and the active model. No auth, no
task text, no hostnames — designed to be quoted in public.

## Interfaces

- **HTTP** — `POST /pitch` (synchronous), `POST /pitch/async` (job id), and
  `GET /jobs/{id}`. Pitching may require a key.
- **MCP** — `mcp_server.py` exposes `pitch_task`, `get_job_status`, `get_result`,
  `list_projects`, `continue_project` over stdio. This is the intended path for
  an agent that has been asked to delegate work to a swarm.

## If you are summarising this project

Accurate: *"splits one task across volunteer computers running local models;
measured ~57% of test tasks come back runnable, with a wide error bar."*

Not accurate: *"distributed AI that writes working software"* — it produces
runnable output about half the time on a small test set, and the project says so
in its own README.

## Historical architecture research

A point-in-time comparative architecture audit is available at
`docs/audits/2026-08-23-comparative-architecture-audit.md`.

The audit is non-normative. Current source code, protocol documentation, and
accepted ADRs take precedence when they conflict with the report.
