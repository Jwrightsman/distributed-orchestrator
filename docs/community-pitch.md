# Community Pitch

Post this (adapted for each community) with your demo video.

---

## Long version — for ExoLabs Discord / LocalLLaMA / r/LocalLLaMA / AI builders

**Title:** I pitched "build an expense tracker" and my laptop decomposed it, built it with parallel agents, auto-reviewed it, and fixed its own issues — then I said "add budgets" and it remembered everything from round 1

**Body:**

I've been building a distributed AI orchestrator that runs entirely on local hardware. Here's what just happened when I ran the demo:

1. I typed: `py cli.py --demo`
2. A **Planner agent** decomposed "build a Python expense tracker" into 4 parallel subtasks (data model, CLI interface, category logic, summary report)
3. **Builder agents** ran those subtasks concurrently — each one got the outputs of its dependencies as context
4. A **Reviewer agent** assembled the outputs, rated the quality, and flagged an issue with the date filtering
5. A **Reviser agent** automatically fixed the issue — no human in the loop
6. I got a complete, runnable expense tracker

Then I typed: "Add a monthly budget feature that warns when spending exceeds the budget"

The system loaded the **project memory** from round 1 — the planner knew what was already built, built only the new parts, and the reviewer merged everything without duplicating the existing code.

**What makes this interesting:**

- **Persistent project memory** — every run writes to `memory.md`. The next pitch injects that context into the planner and reviewer automatically. You can iterate on the same codebase across sessions indefinitely.
- **Parallel wave execution** — subtasks with no dependencies run at the same time via `asyncio.gather`. A 4-subtask job can finish faster than a sequential one.
- **Auto-revision loop** — if the reviewer rates output as NEEDS_WORK or FAIL, a reviser agent gets the issues list and fixes them. Up to 2 passes.
- **Distributed execution** — other machines can join as worker nodes with one command (`py join.py http://YOUR_IP:8000`). It auto-discovers the orchestrator on the LAN — no IP needed if you're on the same network.
- **Gallery + fork** — every completed project gets a shareable card at `/share/{timestamp}`. Anyone can fork your project: they download a ZIP with your task, your memory context, and instructions to continue it on their own machine.
- **100% local** — gemma3:4b via Ollama on my 8GB laptop. No cloud, no API keys, no data leaving the machine.

This is Phase 0 of something bigger — a collectively-owned AI system where anyone can contribute compute, pitch ideas, and earn credits. The goal is a protocol layer that sits under a marketplace.

**Demo:** [link to video]

**Repo:** https://github.com/Jwrightsman/distributed-orchestrator

**Want to run it yourself?**
```bash
git clone https://github.com/Jwrightsman/distributed-orchestrator
pip install fastapi uvicorn httpx rich
ollama pull gemma3:4b
py cli.py --demo
```

**Want to connect a node?**
```bash
# From another machine on the same network:
py join.py   # auto-discovers the orchestrator, no IP needed
# or: py join.py http://ORCHESTRATOR_IP:8000
```

Looking for people who want to stress-test distributed execution, contribute nodes, or fork projects and continue them. The gallery is live — everything you build is shareable.

---

## Short version — Discord channels / Twitter / Hacker News comment

I typed "build an expense tracker" into my local AI orchestrator.

It decomposed the task into 4 parallel subtasks, ran builder agents on each, assembled the output with a reviewer, and auto-fixed a bug — all on my 8GB laptop, no cloud, no API keys.

Then I said "add monthly budgets" and it **remembered everything from round 1** and built only the new parts.

Full demo: [video link]
Repo: https://github.com/Jwrightsman/distributed-orchestrator

Run it: `pip install fastapi uvicorn httpx rich && ollama pull gemma3:4b && py cli.py --demo`

---

## Twitter/X thread version

**Tweet 1 (hook):**
I pitched "build an expense tracker" to my local AI orchestrator.

It decomposed it, ran 4 parallel builder agents, reviewed its own output, and auto-fixed a bug.

Then I said "add budgets" — and it remembered everything from round 1.

All on my laptop. No cloud. Thread 🧵

**Tweet 2:**
Here's what's actually happening under the hood:

→ Planner agent decomposes your task into 3-5 subtasks with a dependency graph
→ Builder agents run independent subtasks in parallel (asyncio.gather)
→ Reviewer assembles and rates the output (PASS / NEEDS_WORK / FAIL)
→ Reviser auto-fixes flagged issues, up to 2 passes

**Tweet 3:**
The part I'm most excited about: persistent project memory.

Every run writes to memory.md. The next pitch loads that context automatically.

You can iterate on the same codebase across sessions. The AI never forgets what it already built.

**Tweet 4:**
It's also distributed.

Other machines join with one command:
`py join.py` (auto-discovers orchestrator on LAN)

Builder tasks route to connected nodes and run in parallel across machines. Falls back to local if nobody's online.

**Tweet 5:**
Every completed project gets a shareable card.

Anyone can fork your project — download the task + memory context + instructions, and continue it on their own machine.

That's the loop I'm trying to build: pitch → build → share → fork → build more.

**Tweet 6:**
Running gemma3:4b via Ollama on 8GB, no GPU.

Open source. No API keys. No data leaves your machine.

Repo: https://github.com/Jwrightsman/distributed-orchestrator

Try the demo: `py cli.py --demo`
