# Landscape update: who else is building this, and does the architecture still hold

> **Status:** Point-in-time research note; non-normative.
> **Date:** 2026-09-24. **Revision:** `fadbf1a` (master).
> **Continues:** [2026-08-23 comparative architecture audit](2026-08-23-comparative-architecture-audit.md)
> and [2026-09-12 feasibility and research](2026-09-12-feasibility-and-research.md).
> **Evidence note:** external facts come from the linked pages as retrieved on
> the date above. Nothing external was installed or run. Vendor benchmark
> numbers are the vendor's, not ours.

## Short version

1. **Yes, other systems now do parts of this.** In the month since the August
   audit, the "pool several computers for local AI" idea went mainstream:
   NVIDIA shipped a free open-source router for it (PAIR, early September
   2026), Hyperspace shipped friend-group "Pods", and LocalAI has had
   federation for two years. AI Horde has run a volunteer network with
   non-sellable credits since 2022.
2. **The part they commoditize is the part Mycelium should not compete on:**
   sending a request to whichever machine is free.
3. **The part nobody ships well is still Mycelium's:** work done on machines
   owned by *different people*, with explicit consent, a durable record of who
   ran what, and honest labels on whether the result was checked. It also
   still works on 8 GB CPU-only machines, which PAIR and Pods do not target.
4. **The architecture's big bets hold up.** Task-level parallelism rather than
   splitting a model across machines is the same choice NVIDIA made. The
   trust/audit layer is not something any competitor has.
5. **The real risk is not a competitor. It is that the central question is
   still unanswered.** The pilot that would show whether two machines produce
   more *accepted* work than one ([canonical pilot plan](../experiments/2026-09-13-canonical-pilot-plan.md))
   has not been run. There are still no outside users. The code has grown to
   roughly 44,000 lines around a claim that has not been measured.

## What changed since August

| System | What it is | Overlap with Mycelium | Where it differs |
|---|---|---|---|
| [NVIDIA PAIR](https://blogs.nvidia.com/blog/local-ai-ifa-next-gen-agents-nv-pair-rtx-spark/) (beta, announced IFA, Sept 2026) | Free, open-source router: finds PCs on your LAN and sends each independent Ollama/LM Studio request to whichever has capacity. Aimed at parallel subagents. | The dispatch layer. NVIDIA reports about 1.6× on a five-subagent workflow across two RTX 5090 PCs ([coverage](https://gcn.com/nvidia-releases-pair-software-local-ai/21442/)). | Same LAN only. Needs an RTX 20-series GPU or Apple M4 or newer. Built for one owner's machines; no consent, audit, or result checking. |
| [Hyperspace Pods](https://github.com/hyperspaceai/agi/blob/main/docs/PODS.md) (2026) | A family, startup, or group of friends pools laptops into one OpenAI-compatible endpoint via invite links; Raft-replicated membership, API keys, and a budget "treasury". | The social shape: invited friends pooling machines. | Shards big models across machines (the approach Mycelium rejected for 216 ms links). GPU/VRAM-oriented. Part of a wider project with agent payments and a "Proof of Intelligence" chain. No result verification or audit trail is documented. |
| [LocalAI federation](https://localai.io/docs/features/distribute/) (since 2024) | `--p2p --federated` routes whole requests between instances that share a token; also has a separate model-sharding mode. | Whole-request routing across machines, including across networks. | Pure infrastructure: one shared token, no per-person identity, no agent strategy, no records. |
| [AI Horde](https://github.com/Haidra-Org/AI-Horde) (since 2022, ~1.6k stars) | Non-profit volunteer network for image and text generation. Workers earn **kudos that cannot be sold**, only earned by contributing, and spent on queue priority. | Almost exactly the "guild with non-monetary credits" idea, running for years. | Anonymous public volunteers, single requests, no multi-step agent work, no provenance. |
| [solveathome](https://github.com/solveathome/platform) (live since Sept 10, 2026, very small) | "A swarm of AI agents, owned by many people, working one open problem", with peer review, credit chains, standings, and public transcripts. | The closest in spirit: many owners, credits, transparency. | Uses frontier cloud agents (Claude, Codex) on donors' machines, focused on research problems. Tiny and unproven. |
| Several 0–5 star repos ([volunteer-computing topic](https://github.com/topics/volunteer-computing)) | "Donate idle AI compute", browser compute grids, community inference networks. | The idea is in the air. | None show traction. |

Earlier comparators (Exo, Petals, BOINC, Golem/Akash, LangGraph, Temporal and
the rest) are covered in the August audit; nothing found here changes those
verdicts. Exo has moved further toward high-end Apple/NVIDIA clusters.

## Claims that are now wrong

- The README's **Positioning** paragraph says SwarmHarness "notes that no
  existing system ships the combination — volunteer consumer hardware,
  credit-based incentives, no tokens". AI Horde has shipped exactly that since
  2022. The September feasibility review already flagged this; it is now
  clearly false and should be rewritten before any public post.
- MASTER_PLAN §3's "The lane is still open" was true in August for the
  dispatch layer. It no longer is. The open lane is narrower: accountable
  pooled work between people who do not share one owner.

Both are positioning text, which is the project owner's call, so this note
flags them rather than editing them.

## Does the architecture still hold

Checked against the new systems, decision by decision.

**Task-level parallelism, not model sharding. Holds, and got stronger.**
NVIDIA looked at the same problem and chose the same primitive: send whole
independent requests, don't split a model. Hyperspace chose sharding, which
needs fast links between machines. The one second machine here is 216 ms
away, so the reasoning in SPRINT_PHASE2 §4 still stands.

**Strategy separate from placement. Holds.** None of the competitors have
strategies at all; they are routers. This separation is what would let
Mycelium sit *on top of* a router instead of competing with one.

**Attempt authority, enrollment, audit trail, and honest assurance labels.
Holds, and is the actual differentiator.** PAIR assumes one owner on one LAN.
Pods assumes the group trusts each other and records spending, not results.
LocalAI has one shared token. Nobody else records which person's machine
produced which output under which contract, or keeps "finished" separate from
"checked" separate from "correct".

**Custom transport and discovery. Correctly not built.** PAIR, LocalAI and
Hyperspace all do discovery and NAT traversal. The August audit's advice was
to leave only a seam here, and the market has made that advice cheaper to
follow.

**One gap worth noting: the worker only speaks Ollama.** `node.py` hard-codes
`provider="ollama"`. The coordinator can already send planner/reviewer calls
to any OpenAI-compatible endpoint (`provider_base_url` in `config.py`), but
builders cannot. If a worker could point at any OpenAI-compatible endpoint, a
friend's LM Studio, LocalAI instance, PAIR router, or Hyperspace pod would
become one Mycelium worker. The new tools would then add capacity to Mycelium
instead of competing with it. This is a small, bounded change, but it is a
feature and the freeze still applies. Record it; build it only when a real
tester needs it.

**Not an architecture problem, but the main risk: the architecture is far
ahead of the evidence.** The September review's key finding still stands
unchanged: "Its infrastructure is ahead of its evidence for user value."
Since then the work has gone mostly to dashboard design, join-path fixes and
remediation. Those were needed for inviting a friend, but the three things
that decide whether the project matters have not moved:

1. The direct / direct-with-repair / DAG pilot has not been run.
2. There is no demo video or public post.
3. No outside person has used it more than once.

## Is the position worth building

An honest answer, in three parts.

**As a general "pool your computers for AI" product: no.** NVIDIA now gives
that away free with better hardware support, and LocalAI and Hyperspace
already do it. A one-person project should not fight that.

**As "accountable AI work across computers owned by people who trust each
other": possibly, and nobody else ships it.** Its likely users are a
classroom, a lab, a club, or an open-source group with mixed, modest
machines, where it matters who ran what and whether it was checked. That is
a real niche but a small one. It is also unproven: nobody has asked for it
yet.

**As a learning and portfolio project: already a success.** A durable,
audited, multi-machine execution system with 2,400+ tests and candid threat
modelling is unusual work for anyone, let alone a first project.

## Recommended next steps, in order

1. **Rewrite the README positioning paragraph** so it doesn't make a novelty
   claim that is false. Lead with the accountable, multi-owner angle and name
   AI Horde and PAIR as neighbours.
2. **Run the canonical pilot** on the machines you already have. It is budgeted
   at 7 hardware-hours. It answers whether splitting work ever beats one model
   plus one repair.
3. **Get one friend to join** using the invite kit, and run a real task they
   care about. Record what confused them.
4. **Post honestly** (the MASTER_PLAN's Phase C), including the pilot result
   even if it is negative.
5. **Decide at a fixed date.** If nobody returns after one deliberate
   repositioning, freeze the project as a finished portfolio piece rather than
   adding architecture to avoid the result. This is the MASTER_PLAN's own
   day-30 rule, which hasn't been applied yet.

Not recommended now: model sharding, P2P discovery, a points market, or
competing with PAIR on routing.

## Sources

- NVIDIA blog, IFA 2026 local AI and PAIR: https://blogs.nvidia.com/blog/local-ai-ifa-next-gen-agents-nv-pair-rtx-spark/
- PAIR coverage with benchmark and hardware list: https://gcn.com/nvidia-releases-pair-software-local-ai/21442/ and https://www.datastudios.org/post/nvidia-pair-local-ai-agents-distributed-inference-ollama-lm-studio-multi-pc-compute
- Hyperspace Pods documentation: https://github.com/hyperspaceai/agi/blob/main/docs/PODS.md
- Hyperspace Pods announcement: https://x.com/varun_mathur/status/2044882359565312468
- LocalAI P2P and federation: https://localai.io/docs/features/distribute/
- AI Horde repository and kudos model: https://github.com/Haidra-Org/AI-Horde and https://aihorde.net/contribute/workers/
- solveathome: https://github.com/solveathome/platform
- GitHub volunteer-computing topic: https://github.com/topics/volunteer-computing
- Ollama has no built-in clustering (community load balancers instead): https://github.com/K2/olol
