# Community Pitch

Post this (adapted for each community) with your demo video.

---

## Long version — for r/LocalLLaMA / Ollama Discord / r/selfhosted / AI builders

**Title:** I asked Claude to build me a game — it delegated the work to a swarm of small models on my own hardware, and the game opened in my browser

**Body:**

I've been building a distributed AI orchestrator that runs entirely on local hardware — several machines, several small models, working one task together. Two things happened this week that make it worth sharing.

**One: it's now an MCP server.** I asked Claude Desktop to pitch a task to the swarm. It called `pitch_task`, my laptop's planner decomposed the job, builder agents ran the subtasks in parallel, a reviewer assembled the result — and the finished code came back into the chat. Any MCP client can do this now; the swarm is just a tool your existing AI app can reach for.

**Two: the loop is real.** Here's the pipeline it runs:

1. A **Planner** decomposes your task into 3–5 subtasks with a dependency graph (schema-enforced JSON, so it doesn't get mangled)
2. **Builder agents** run independent subtasks concurrently — across connected machines when nodes are online, locally when they're not
3. A **Reviewer** assembles all the outputs into one deliverable and rates it
4. A **Reviser** auto-fixes whatever the reviewer flagged
5. Extracted code is then **checked mechanically** — Python is parsed, HTML is structurally validated — and anything broken gets one more targeted repair pass. A "PASS" that doesn't actually run gets caught.

Then I pitched a follow-up task to the same project, and it loaded the **project memory** from round 1: the planner knew what already existed and built only the new parts.

**What makes this different from the usual agent swarm:**

- **It's not a cloud product.** qwen3.5:4b via Ollama on an 8GB laptop with no GPU. No API keys, no data leaving the machines you own.
- **It's multi-machine.** Volunteer hardware joins with one command and picks up builder tasks. Task reclaim, circuit breaker, and auto-reconnect are built in for when someone's laptop closes.
- **Contributions are tracked.** An append-only credit ledger records compute, pitches, and reviews — the seed of a contributor guild. No tokens, no blockchain, deliberately.
- **Persistent project memory.** Every run appends to `memory.md`, auto-summarized when it grows. You can iterate on the same codebase across sessions indefinitely.

Worth noting: [SwarmHarness](https://arxiv.org/abs/2605.28764) (May 2026) describes this exact design space academically — decentralized, incentive-aligned agent networks without a blockchain — and points out nobody ships the combination. That's a protocol paper. This is a working implementation you can run tonight.

This is Phase 0 of something bigger: an open protocol, then a contributor guild, then a marketplace on top.

**Demo:** [link to video]

**Repo:** https://github.com/Jwrightsman/distributed-orchestrator

**Run it yourself:**
```bash
git clone https://github.com/Jwrightsman/distributed-orchestrator
pip install -r requirements.txt
ollama pull qwen3.5:4b
python cli.py --demo-showcase
```

**Connect a node (this is the part I actually need):**
```bash
# Mac/Linux
curl -fsSL https://raw.githubusercontent.com/Jwrightsman/distributed-orchestrator/master/install.sh | bash -s -- http://ORCHESTRATOR:8000

# Windows PowerShell
$env:SWARM_SERVER="http://ORCHESTRATOR:8000"; irm https://raw.githubusercontent.com/Jwrightsman/distributed-orchestrator/master/install.ps1 | iex
```

Looking for people who want to contribute a node, stress-test distributed execution across the WAN, or just tell me where it falls over. Every rough edge you hit becomes an issue.

---

## Short version — Discord channels / Twitter / Hacker News comment

I asked Claude to build me a game. It delegated the job to a swarm of 4B models running on my own hardware — planner split it up, builders ran in parallel, a reviewer assembled it — and the finished game opened in my browser.

No cloud, no API keys. qwen3.5:4b via Ollama on an 8GB laptop. It's an MCP server, so any AI app can hand work to the swarm, and any machine can join as a worker with one command.

Full demo: [video link]
Repo: https://github.com/Jwrightsman/distributed-orchestrator

Run it: `pip install -r requirements.txt && ollama pull qwen3.5:4b && python cli.py --demo-showcase`

---

## Twitter/X thread version

**Tweet 1 (hook):**
I asked Claude to build me a game.

It delegated the work to a swarm of small models running on my own hardware.

A few minutes later the finished game opened in my browser.

No cloud. No API keys. Thread 🧵

**Tweet 2:**
The swarm is an MCP server, so any AI app can reach it.

Claude called `pitch_task` → my laptop's planner split the job into subtasks → builder agents ran them in parallel → a reviewer assembled the result → it came back into the chat.

**Tweet 3:**
Under the hood:

→ Planner decomposes into 3–5 subtasks with a dependency graph (schema-enforced JSON)
→ Builders run independent subtasks concurrently, across machines
→ Reviewer assembles + rates
→ Reviser auto-fixes what's flagged

**Tweet 4:**
Then it checks its own work *mechanically*.

Extracted Python gets parsed. HTML gets structurally validated. If it doesn't run, that goes back for a targeted repair pass.

A "PASS" that doesn't actually execute doesn't survive.

**Tweet 5:**
It's multi-machine by design.

Any laptop joins with one command and starts taking builder tasks. Task reclaim, circuit breaker, auto-reconnect — because volunteer hardware closes its lid.

Contributions land in an append-only credit ledger. No tokens. No blockchain.

**Tweet 6:**
Persistent memory ties it together.

Every run appends to memory.md, auto-summarized as it grows. The next pitch loads it — the swarm knows what it already built.

**Tweet 7:**
qwen3.5:4b via Ollama. 8GB laptop, no GPU.

Open source, no API keys, nothing leaves the machines you own.

Repo: https://github.com/Jwrightsman/distributed-orchestrator

I need nodes — if you've got a spare machine, one command joins it.
