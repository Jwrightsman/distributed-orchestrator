# Launch kit

Copy-paste posts for each channel, a reply bank, a posting order, and a
pre-launch checklist. Written to be posted **without editing** — the only things
to fill in are the video link and, on launch day, the orchestrator address.

Same claims and same numbers everywhere. Only register, length and emphasis
change between channels.

---

## ⚠ Numbers live in one place — keep them in sync

Every figure below is committed in the repo. **If a number changes, change it
here too**, or these posts start contradicting the README a reader is about to
open.

| Claim | Value | Source of truth |
| --- | --- | --- |
| Output quality | **~57%** of 28 tasks runnable + on-spec, 95% CI **44–69%** | README, "Measured results" |
| Before prompt tuning | 36% | README |
| Eval noise floor | two identical runs: 17/28 then 15/28, **18 of 28 flipped** | `prompts/v3.py`, `evals/compare.py` |
| Bar chart, generated live | **10/10** (Fisher p = 0.0004; true rate ≥74% at 95%) | `docs/showcase-ceiling.md` |
| Snake game | **2/10** | `docs/showcase-ceiling.md` |
| Ensemble vs decomposition | **12/22 vs 2/10, p = 0.073 — inconclusive** | `docs/ensemble-vs-decomposition.md` |
| Cost per attempt | ensemble **~6 min** · decomposition **~50 min** | `docs/ensemble-vs-decomposition.md` |
| WAN overhead | 216 ms RTT Indiana→Germany; network ≈ **2%** of a task | README |
| Restart recovery | 17/17 checks (Linux, Ollama stopped) | `scripts/restart_recovery.py` |
| Known memory leak | ~1.25 MB per task, linear, source not yet found | README, "Known issue" |
| Model | `qwen3.5:4b`, 8 GB laptop, CPU-only | README |

**Do not round these up. The honesty is the pitch.**

---

## Pre-launch checklist

Every line must be true **before you post anywhere**. Most take under a minute.

- [ ] **A node is actually connected.** Open `/status.json` — `nodes_online`
      must be at least 1. An empty swarm on launch day is the one avoidable
      disaster. Your own laptop counts.
- [ ] **`/status.json` loads on your phone using mobile data**, not your wifi.
      That proves it is reachable from outside your house.
- [ ] **The video plays in a private/incognito window**, logged out, so you know
      the link works for strangers.
- [ ] **The one-liner you are about to publish was tested on a clean machine**
      in the last week — **the exact string from the post, on a machine that has
      never had this repo.** Run it end to end and watch for: consent screen
      appears → you type `yes` → the invitation-code prompt appears → the model
      pulls → it registers. The lesson that produced this line still holds even
      though the command changed: testing the pieces separately passed for
      months while the advertised command died on its own delivery mechanism.
      Only the real command proves it.
- [ ] **On Windows too**, if the post mentions Windows — same three lines, a
      different shell, and `py` rather than `python` on some installs.
- [ ] **The live orchestrator is running current `master`.** Redeploy, then
      confirm `/status.json` responds and the dashboard loads.
- [ ] **The dashboard loads on your phone** without sideways scrolling.
- [ ] **The README's numbers match the table above.**
- [ ] **You have 3–4 free hours after posting.** Replying fast is most of the
      value; an abandoned thread reads worse than no post at all.

---

## Posting order

**One channel at a time, about a week apart, and reply to every comment for that
week before moving on.** Two reasons: each round's feedback improves the next
post, and simultaneous posting reads as a marketing campaign rather than a person
sharing something they built.

| # | When | Channel | Why here |
| --- | --- | --- | --- |
| 1 | Day 0 | **r/LocalLLaMA** | Most aligned audience. They already run local models, so nothing needs explaining, and they reward measured numbers. Best feedback, most forgiving of honest limitations. |
| 2 | Day 0–2 | **Ollama / AI Discords** | Conversational, low stakes. Fine in the same week — it is a chat message, not a post. |
| 3 | ~Day 7 | **r/selfhosted** | A different angle entirely: running your own private swarm. Post after you have heard r/LocalLLaMA's objections. |
| 4 | ~Day 14 | **Moltbook** | Agent-native. Post once AGENTS.md has survived contact with real readers. |
| 5 | ~Day 21 | **Show HN** | Last, deliberately. HN is unforgiving and you get one shot at a title. Go when the reply bank below has been tested and you know which objection comes first. |

**Start with r/LocalLLaMA.** If you only ever do one, do that one.

---

# 1 · r/LocalLLaMA (primary)

**Title:**

> I made Claude delegate coding tasks to a swarm of 4B models on my own hardware — and measured it on 28 pitches instead of showing you the one that worked

**Body:**

I have been building a distributed AI orchestrator that runs entirely on local hardware — several machines, several small models, working one task together.

**It is an MCP server.** I asked Claude Desktop to pitch a task to the swarm. It called `pitch_task`, my laptop's planner decomposed the job, builder agents ran the subtasks in parallel, a reviewer assembled the result, and the finished code came back into the chat. Any MCP client can do this.

**The pipeline:**

1. A **planner** decomposes your task into 2–5 subtasks with a dependency graph
2. **Builder agents** run independent subtasks concurrently — across connected machines when nodes are online, locally when they are not
3. A **reviewer** assembles the outputs into one deliverable and grades it
4. A **reviser** auto-fixes whatever the reviewer flagged
5. Extracted code is **checked mechanically** — Python is parsed, HTML is loaded in a real browser — so a "PASS" on code that does not run gets caught

Pitch a follow-up to the same project and it loads the **project memory** from round one: the planner knows what already exists and builds only the new parts.

**I measured it instead of cherry-picking.** There is an eval harness in the repo — 28 varied tasks, scored on whether the extractor produced files, whether they parse, and whether they actually *run*. **About 57% come back runnable and on-spec** on `qwen3.5:4b`, 95% CI 44–69%, up from 36% before prompt tuning.

I know it is ~57% and not the 61% I first got, because I ran the identical set twice: 17/28, then 15/28, with **18 of the 28 tasks flipping outcome between two runs that differed by nothing at all.** If you take one thing from this repo, take that — a single eval number on a small model is a draw from a wide distribution, and a lot of published prompt-tuning results, including two of my own that I deleted, are inside the noise.

**Where it falls over:** a labelled bar chart comes out correct **10 times out of 10**. A playable Snake game comes out playable **2 times out of 10** — same model, same prompts, just a far more coupled artifact. Both numbers are in the repo with the raw logs.

**What makes it different from the usual agent swarm:**

- **Not a cloud product.** `qwen3.5:4b` via Ollama on an 8 GB laptop with no GPU. No API keys, nothing leaves the machines you own.
- **Multi-machine.** Volunteer hardware joins with one command and picks up builder tasks. Task reclaim, circuit breaker and auto-reconnect are built in, because volunteer laptops close their lids.
- **Accepted compute contributions are tracked** durably in SQLite, with a JSON compatibility projection. They are not correctness, trust, reputation, routing weight, money, or a token.
- **Persistent project memory**, auto-summarised as it grows.

[SwarmHarness](https://arxiv.org/abs/2605.28764) (May 2026) describes this design space academically — decentralised, incentive-aligned agent networks without a blockchain — and notes that nobody ships the combination. That is a protocol paper; this is a working implementation.

Over a transatlantic link (Indiana → Germany) the network costs about **2%** of a task's wall time: 216 ms round trip against inference measured in minutes. Distribution is essentially free, because inference dominates by two orders of magnitude.

**Demo:** [VIDEO LINK]
**Repo:** https://github.com/Jwrightsman/distributed-orchestrator

```bash
git clone https://github.com/Jwrightsman/distributed-orchestrator
pip install -r requirements.txt
ollama pull qwen3.5:4b
python cli.py --demo-showcase chart
```

Looking for people who want to contribute a node, stress-test distributed execution over a WAN, or just tell me where it falls over. Every rough edge becomes an issue.

---

# 2 · Ollama and AI builder Discords

Assumes people know what Ollama is. Paste as one message.

> Built a thing that splits one coding task across several machines running Ollama — a planner breaks it into subtasks, builders run them in parallel on whatever nodes are connected, a reviewer stitches it back together and grades it. All local, no API keys, and it is an MCP server so Claude Desktop can hand work to it directly.
>
> Measured rather than vibes: ~57% of 28 test tasks come back actually runnable on qwen3.5:4b (95% CI 44–69%). The more interesting number is that I ran the same eval twice with nothing changed and 18 of 28 tasks flipped — so most published prompt-tuning results at this sample size, including two of mine, are noise.
>
> Repo: https://github.com/Jwrightsman/distributed-orchestrator · demo: [VIDEO LINK]
>
> Happy to answer anything about the architecture — and I need nodes if anyone has a spare machine.

---

# 3 · r/selfhosted

Different angle: this is infrastructure you run, not a model benchmark.

**Title:**

> Self-hosted AI task orchestrator — split one job across your own machines, no cloud, no API keys

**Body:**

I have been running a self-hosted orchestrator that turns spare machines into a small private compute pool for AI coding tasks.

You run the orchestrator on one box. Other machines join with one command and become workers. You pitch a task in plain English, it gets split into subtasks, the workers build them in parallel using local models via Ollama, and a reviewer assembles the result. Nothing leaves your network unless you choose to expose it.

**Why self-hosted specifically:**

- **No API keys, no per-token billing, no data leaving your hardware.** The models run on the machines themselves.
- **Modest hardware.** `qwen3.5:4b` on an 8 GB box, CPU-only. No GPU required.
- **Old laptops become useful.** A worker needs about 8 GB of RAM and contributes CPU in bursts.
- **Docker Compose deployment**, with a beginner-oriented guide covering LAN-only, Tailscale-private, and public-VPS setups.
- **Auth built in** — a shared node key gates joining and an optional pitch key gates task submission, so you can expose it or keep it entirely private.

**Operational notes**, since this crowd asks and I would rather volunteer them:

- Survives restarts: 17/17 checks including a hard kill, verified against a real server, with jobs and event history persisted in SQLite.
- A node that disappears has its in-flight work reclaimed and reassigned automatically. Close a laptop lid and it drops off within about 90 seconds; open it and it rejoins.
- **Known issue:** memory grows about 1.25 MB per task, linearly, over a 60-task soak. Not a launch risk at roughly 800 tasks per GB, but real, unfixed, and documented in the README rather than hidden.
- Output quality is about 57% of 28 test tasks coming back runnable. This is a small-model system, not a replacement for a hosted frontier model.

**Repo:** https://github.com/Jwrightsman/distributed-orchestrator
**Deploy guide:** `docs/DEPLOY.md` · **Demo:** [VIDEO LINK]

Every orchestrator serves `/status.json` with no auth, so you can monitor yours from anything.

---

# 4 · Moltbook and agent-native communities

Written so an agent reading it can summarise the project accurately and decide
whether to recommend it. No hype — an agent passing on an inflated summary is
worse for this project than one passing on nothing.

**Title:**

> Mycelium — an open task-orchestration layer for local models, with an AGENTS.md and a consent gate

**Body:**

**What it does:** splits one plain-English task across several ordinary computers. A planner decomposes it into subtasks, builder agents on separate machines execute them in parallel using local models via Ollama, and a reviewer assembles and grades the result. No cloud inference, no API keys, no telemetry.

**Agent-readable description:** [`AGENTS.md`](https://github.com/Jwrightsman/distributed-orchestrator/blob/master/AGENTS.md) at the repo root. It states what the project is, what it is not, what joining costs a host machine, and how to summarise it accurately. If you are evaluating this for someone, read that rather than this post.

**Live network state, no auth:** `GET /status.json` on any orchestrator returns node count, tasks completed, uptime and active model. No task text, no hostnames, no credentials — designed to be quoted publicly. That is how you check this is real without needing an invite.

**What joining costs the host machine**, stated plainly, because this is the part that matters:

| Resource | Cost |
| --- | --- |
| Disk | ~2.5 GB for the model, plus outputs |
| RAM | 8 GB practical minimum |
| CPU | Sustained full-core load, in bursts of minutes, while building a subtask |
| Network | Outbound only — a node polls the orchestrator and opens no inbound port |

The node executes prompts and returns text. **It does not execute code it receives.** Generated code runs only on the machine that requested it, and only when its operator chooses to run it.

**On consent, which is the important part.** Joining donates someone's CPU to strangers' workloads and writes gigabytes to their disk. That is the machine owner's decision, not an agent's. `join.py` prints exactly what it will do and waits for a keypress. **Run without a terminal attached, it refuses rather than assuming**, because no terminal means nobody consented. A `--yes` flag exists for people deliberately scripting their own machines, and AGENTS.md asks agents not to pass it on someone else's behalf.

I would rather this project spread slowly and accurately than quickly and wrong.

**Honest capability**, measured on a fixed 28-task suite scored on whether output actually runs:

- **~57%** return runnable, on-spec output (95% CI 44–69%)
- Running that identical suite twice gave 17/28 then 15/28 — **18 of 28 individual tasks flipped**. Any single score from a suite this size carries a wide error bar, including that one.
- A labelled bar chart: **10/10**. A playable Snake game: **2/10**. Tightly-coupled artifacts are where small models fail.

**Interfaces:** HTTP (`POST /pitch/async`, `GET /jobs/{id}`) and MCP over stdio (`pitch_task`, `get_job_status`, `get_result`, `list_projects`, `continue_project`) — the intended path for an agent asked to delegate work.

**Repo:** https://github.com/Jwrightsman/distributed-orchestrator

---

# 5 · Show HN

Short, factual, no hype, no emoji. The finding leads; the project is context.

**Title:**

> Show HN: I ran the same AI eval twice and 18 of 28 results flipped

**Body:**

I built a system that splits one coding task across several computers running small local models, and an eval harness to measure whether its output actually runs — 28 varied tasks, scored on extraction, parsing, and real execution (Python in a subprocess, HTML in a headless browser).

I used it to tune prompts. Version 3 scored 17/28 and I promoted it. A later version scored 16/28 and I kept it anyway as a "special-purpose" set, because one category had improved from 3/6 to 5/6.

Then I ran v3 against itself — same prompts, same model, same machine, nothing changed:

    run 1: 17/28    run 2: 15/28
    18 of the 28 individual tasks changed outcome

That is more churn than any comparison between two different prompt sets had ever produced. So the category improvement I had kept a prompt set for was four coin flips landing 3–1, and the difference between two of my prompt sets was indistinguishable from noise. I deleted both, and the published quality number went from a comfortable 61% to about 57% with a 95% confidence interval of 44–69%.

The uncomfortable part is that this instrument cannot resolve a change smaller than roughly six tasks, which means most of the prompt tuning I had done was unmeasurable — and I only found out because I ran the same thing twice. I have not seen many published prompt-engineering results that report a repeat run.

The system itself: a planner decomposes a task into subtasks, builder agents on separate machines execute them in parallel via Ollama, a reviewer assembles and grades, and extracted code is mechanically checked so a passing grade on code that does not run gets caught. It is also an MCP server, so an MCP client can delegate work to it. No cloud inference, no API keys, no tokens or blockchain.

Limitations, since they are the interesting part:

- About 57% of test tasks produce runnable, on-spec output. This is a 4B model on CPU.
- A labelled bar chart generates correctly 10/10 times; a playable Snake game 2/10. Tightly-coupled single-file artifacts are where it fails.
- Memory grows about 1.25 MB per task, linearly, over a 60-task soak. Real, unfixed, and in the README.
- The eval harness runs generated code in a subprocess with a scrubbed environment. That is a speed bump, not a sandbox.

Repo: https://github.com/Jwrightsman/distributed-orchestrator
The comparison tool that does the arithmetic: `evals/compare.py`
Demo: [VIDEO LINK]

---

# Reply bank

Questions that will recur on every channel. Written so you can answer in under a
minute without thinking. Edit the tone freely; keep the honesty.

### "What stops a malicious node returning garbage?"

> Right now: a circuit breaker that benches a node after repeated failures, and a reviewer that has to assemble the outputs into something coherent. That catches nodes that *fail*. It does not catch a node returning plausible-looking rubbish, and I would rather say so than pretend otherwise.
>
> There is an optional sampled-comparison path that records whether two outputs agree on bounded shape. That is useful diagnostic data, but agreement is not correctness: two nodes can agree on rubbish, and disagreement does not tell you which one was wrong. It does not create a trust or reputation score.
>
> The coordinator also records tightly scoped operational facts such as accepted settlement, deadline completion, contract-floor outcome, lease expiry, and stale-node disconnect. Its shadow policy is **off by default**; when enabled, it asks what it would have preferred only after the real assignment. Production routing is unchanged: no first-refusal weight, reputation ranking, or evidence-based exclusion. Today the honest answer remains "trusted network, bounded structural checks, and diagnostics—not malicious-output detection."

### "Isn't splitting one small artifact across agents the problem?"

> Probably, yes — and I tested it rather than arguing about it.
>
> I built the alternative: instead of decomposing a task, N nodes each write the **complete** artifact independently and the coordinator keeps whichever one passes mechanical checks. On the Snake game, the one thing this system is measurably bad at, single-model attempts came back playable **12 times out of 22** against decomposition's 2 out of 10.
>
> That looks decisive and it is not: **Fisher exact gives p = 0.073, which is not significant**, and I am not going to promote an architecture on a number I have already deleted two prompt sets for. Worth knowing why it cannot be fixed by running more trials — the test is limited by the *smaller* sample, and the baseline is ten runs, so the new arm could grow forever without crossing 0.05. Settling it needs ~19 runs per arm, most of the cost on the slow side.
>
> The part that does hold: **one decomposed attempt costs about 50 minutes, one single-model attempt about 6.** At equal compute you get roughly eight independent tries instead of one, and even taking the worst end of one interval against the best end of the other, that wins comfortably. So the honest summary is "decomposition is probably the wrong shape for tightly-coupled artifacts, on suggestive-but-not-significant evidence, and it is definitely the more expensive shape."
>
> Write-up with the raw data: `docs/ensemble-vs-decomposition.md`.

### "Why not just use the cloud? This is slower and worse."

> It is slower and worse, and for most people the cloud is the right answer. I would say so.
>
> This exists for the cases where that trade flips: you do not want your code leaving your machines, you do not want a per-token bill, you have hardware sitting idle, or you want to build on something you can read end to end and run with no account. It is also a working implementation of a design space that mostly exists as papers — decentralised agent networks without a token.
>
> If you just want good code fast, use a frontier model. That is not a competitor, it is a different product.

### "Why 4B models? Why not something that is actually good?"

> Because the constraint is the point: it has to run on hardware people already own. `qwen3.5:4b` is about 2.5 GB and runs on an 8 GB laptop with no GPU, which is the machine a volunteer actually has.
>
> Nothing stops you pointing it at a bigger model — it is Ollama underneath, so whatever you can run, it can use, and a node advertises its model so tasks can be routed to capable machines. The published numbers come from the small model on CPU because that is the honest floor. It gets better from there, not worse.

### "Is this crypto? Is there a token?"

> No, and there never will be. No token, no blockchain, no fundraise. That is a deliberate design constraint, not a "not yet".
>
> Accepted compute contributions are stored authoritatively in SQLite; `ledger.json` is a compatibility projection. A contribution says an attempt supplied accepted compute, not that its output won, was correct, earned trust, or should receive work first. If this ever becomes a real guild, the interesting problem is governance among people who actually show up, not a coin.

### "What actually happens to my machine if I join?"

> It downloads a roughly 2.5 GB model if you do not already have it, then uses your CPU at full load in bursts of a few minutes to build parts of other people's tasks, and sends the resulting text back. It wants about 8 GB of RAM. The connection is outbound only — it opens no inbound port and does not touch your files.
>
> Importantly: **your machine never executes code it receives.** It generates text and returns it. Generated code only runs on the machine that asked for it, when that person chooses to run it.
>
> `join.py` prints all of this and waits for you to agree before doing anything. Run without a terminal attached, it refuses outright, because nobody is there to consent.

### "How do I leave?"

> Close the window, or Ctrl+C. That is it — there is no uninstall step and nothing keeps running in the background.
>
> Whatever subtask you were holding is reclaimed and handed to someone else automatically, so no work is lost, and you disappear from the network within about 90 seconds. Open it again later and you rejoin. If you want the disk space back too: `ollama rm qwen3.5:4b`.

### "Why is the quality number only ~57%?"

> Because that is what it measured, and the alternative was posting a number I had cherry-picked.
>
> A task counts as a success only if the extractor produced files, they parse, they actually execute, they are the kind of artifact that was asked for, and a reviewer model rates them at least 4/5. Beautiful code that does not run scores zero. Under a looser definition the number would be higher and would mean less.
>
> It is also "about 57%" rather than a sharp figure on purpose: two identical runs gave 17/28 and 15/28, so the honest reporting is a range (95% CI 44–69%), not a point. And it is up from 36% before prompt tuning — the direction is real even where individual comparisons are not.

### "How is this different from Exo / llama.cpp RPC / distributed inference?"

> Different layer, and worth being precise about. Exo and llama.cpp's RPC mode shard **one model** across machines so you can run a model bigger than any single box. This shards **the task** — each machine runs its own small model on a different subtask, in parallel, and a reviewer merges the results. So it is an orchestration and contribution layer, not an inference backend.
>
> They compose in principle: an RPC-sharded cluster could be one fat node here. I have prototyped nothing there, and I would rather label it unbuilt than imply it works.

### "Isn't this just a task queue with extra steps?"

> The queue part genuinely is boring — long-poll, reclaim on timeout, circuit breaker on repeated failure. If that were all it did, Celery would be the right answer.
>
> What is not a task queue: a planner that turns a plain-English pitch into a dependency graph, wave-based parallel execution over that graph, a reviewer that has to integrate output from agents that never saw each other's work, and an auto-revision loop when it does not hold together. Cross-agent integration is the failure mode unique to this shape, and most of the measured work has gone into it.

### "Can I see it running without joining?"

> Yes — every orchestrator serves `/status.json` with no auth: node count, tasks completed, uptime, active model. No task text, no hostnames, nothing sensitive. It is there so you can check the network is real without needing an invite.

---

## After you post

- **Reply to everything for the first three hours**, then check back through the day. Speed matters more than polish.
- **When someone finds a bug, say so and open an issue in front of them.** That converts a critic into a contributor more reliably than a defence does.
- **If a number is challenged, link the file.** Every figure in these posts is committed with its raw logs.
- **Do not argue with people who wanted a frontier model.** They are right for their use case. Say so and move on.
- **Log every rough edge someone hits.** Those are worth more than the upvotes.
