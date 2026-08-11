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

---

## Anticipated questions — pre-written replies

r/LocalLLaMA rewards fast, specific, non-defensive answers. These are drafted so you can
reply in under a minute without thinking. Edit tone freely; keep the honesty.

### "How is this different from Exo / llama.cpp RPC / distributed inference?"

> Different layer, and worth being precise about. Exo and llama.cpp's RPC mode shard **one model**
> across machines so you can run a model bigger than any single box. This shards **the task** — each
> machine runs its own small model on a different subtask, in parallel, and a reviewer merges the
> results. So it's an orchestration and contribution layer, not an inference backend.
>
> They compose, in principle: an RPC-sharded cluster could be one fat node in this network. I
> prototyped nothing there yet — it's on the list, honestly labelled as unbuilt.

### "Isn't this just a task queue with extra steps? Why not Celery?"

> Fair. The queue part genuinely is boring — long-poll, reclaim on timeout, circuit breaker on
> repeated failure. If that were all it did, Celery would be the right answer.
>
> The parts that aren't a task queue: a planner that decomposes a plain-English pitch into a
> dependency graph, wave-based parallel execution over that graph, a reviewer that has to integrate
> outputs from agents that never saw each other's work, and an auto-revision loop when it doesn't
> hold together. Cross-agent integration is the failure mode unique to this shape, and most of the
> engineering is there rather than in the dispatch.

### "What stops a malicious node from returning garbage?"

> Layered, and I'll be specific about what each layer does and doesn't cover. Nodes authenticate
> with a shared secret, so joining is gated. A node that fails outright trips a circuit breaker and
> sits out. Neither of those catches the interesting case: a node that returns *plausible* garbage.
>
> For that there's sampled redundant execution — set `verify_rate` and a fraction of builder
> subtasks get sent to a second node as well, with the duplicate deliberately routed away from the
> node being checked. The two answers are compared on substance rather than text (same artifact
> kind, both parse, comparable size — two honest small models never agree token-for-token), and
> both nodes' agreement records update. Neither is assumed correct on a single disagreement; over
> samples the consistently-odd node separates itself, and its routing weight drops so it gets
> offered work last. It never gets excluded on reputation alone — that stays the circuit breaker's
> job — and the weights are visible on each node card in the dashboard.
>
> Two honest caveats. Every verified task costs a whole extra inference, which is why it's sampled
> and why it ships **off by default** (`verify_rate: 0`). And it needs at least two nodes to mean
> anything, so it self-disables on a one-node network. It raises the cost of being a bad node; it
> is not a proof of correctness, and I'm not going to claim it is.

### "How good is the output really, on a 4B model?"

> Being straight with you: good enough for a self-contained web app, a CLI tool, or a data script;
> not good enough to architect anything large. Rather than guess, there's an eval harness in the
> repo — 28 varied pitches, scored on whether the extractor produced files, whether they parse,
> whether they actually execute (HTML gets loaded in a headless browser and fails on JS errors),
> plus a model judgment and wall-clock cost. **61% of them come back runnable and on-spec** on
> `qwen3.5:4b`, up from 36% before prompt tuning. A "PASS" rating that doesn't run counts as a
> failure by construction.
>
> The more useful answer is that reliability depends enormously on how *coupled* the thing you
> asked for is. Same model, same prompts, ten consecutive runs each, every result opened in a real
> browser: a labelled bar chart came out correct **10 times out of 10**; a playable Snake game
> came out playable **2 times out of 10**. The game's failures are the interesting kind — no
> JavaScript errors, the page just never animates. One tightly-coupled file split across agents
> that can't see each other is where this architecture is weakest, and I'd rather show you that
> number than hide it.
>
> Raw logs for both are in the repo (`scripts/showcase_results/`), and so is the write-up of what
> did and didn't fix it (`docs/showcase-ceiling.md`) — including a prompt change that looked like
> it worked and turned out to be statistical noise.

### "Is there a token? Are you going to rug this?"

> No token, no blockchain, no fundraise, and that's a deliberate design constraint rather than a
> "not yet". The ledger is an append-only JSON file that counts contributions. If this ever becomes
> a real guild, the interesting problem is governance among people who show up, not a coin.

### "What do I need to join?"

> An 8GB machine and Ollama. One command:
>
> ```
> python join.py http://<orchestrator>:8000
> ```
>
> It checks your deps, pulls the model if you don't have it, registers, and starts taking builder
> tasks. Close the lid whenever you want — in-flight work gets reclaimed and re-assigned.
