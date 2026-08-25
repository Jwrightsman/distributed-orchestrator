# ROADMAP — Mycelium

_The single home for everything not being built right now: the long-term vision, deferred
engineering, external review findings, and speculative ideas. Last updated: August 14, 2026._

**What lives where.** `MASTER_PLAN.md` is direction and the launch plan. `SPRINT_*.md` is the
active work with its Session Log. `HANDOFF.md` is state for the next session. **This file is
everything else** — if an idea isn't being built now, it belongs here rather than in a sprint,
and MASTER_PLAN §8 points here instead of keeping its own parking lot.

**How to use it.** Nothing here is a commitment. Every item has a **trigger** — the evidence
that makes it worth building. The project's whole discipline has been not building ahead of
evidence, and a roadmap is the easiest place to lose that. When a trigger fires, the item moves
into a sprint file and gets deleted from here. Review the file when a phase ends or a trigger
fires, not on a schedule.

---

## 1. The vision, unchanged

A collectively-owned intelligence layer running on hardware people already have. Three layers,
serving different people at once:

**The protocol.** Open, free, forkable, owned by nobody. Coordinates work across devices. It
doesn't care who you are. This is the layer that must stay radically open or the rest is
pointless.

**The guild.** Everyone who contributes — compute, ideas, review, agents — is a member. Standing
reflects cumulative contribution. Governance is one member, one vote: standing earns recognition
and a larger share of surplus, never more votes. The guild sets quality standards, decides what
the network won't build, and bargains collectively with commercial users. This is where the
values live.

**The marketplace.** Open by default, pay for exclusivity. Anyone can pitch and get work built,
and the output is open. Commercial exclusivity is paid to the guild, which distributes to the
members who did the work. Commercial clients fund the network; the network serves everyone.

The intelligence accumulated by the protocol — better decomposition, better routing, better
judgment about what works — is the commons, and it grows whether an individual job was free or
paid.

**Who it's for:** everyone, at the layer that fits them. A student pitching a tool for their
side gig. A teenager whose phone contributes overnight and who pitches something nobody in the
network has ever built. A laid-off analyst who contributes a specialized agent and earns from
its use. A founder who pays for speed. A university department contributing idle lab machines.
Nobody excluded, nobody favored.

---

## 2. Permanent constraints

These are not roadmap items. They don't expire, and any proposal that violates one is rejected
regardless of merit.

- **No token, no blockchain, no tradable coin.** Credits are contribution points denominated in
  real work. Tokens invite speculators who care about price rather than the network. If
  trustless settlement is ever genuinely needed, it can be bridged later — starting there
  optimizes for hype over utility.
- **Consent before installation, always.** Joining donates someone's CPU and disk. `join.py`
  asks a human; `AGENTS.md` asks agents not to answer on a human's behalf. Software that can be
  installed unattended onto a machine whose owner never agreed is the one reputational mistake
  that can't be undone.
- **Publish the number that makes us look worse.** The walked-back 61% → ~57%, the 18-of-28
  noise floor, the 2/10 Snake result, the known memory leak. This is the project's most valuable
  asset and it is trivially destroyed by one flattering claim.
- **Capital cannot buy governance.** Money buys compute access. It never buys votes.
- **The protocol stays forkable.** Disagreement should be able to leave and take the code. It is
  the only real check on whoever is running things.
- **Contributor rights are not negotiable.** Your hardware, your choice: opt in and out at any
  time with no penalty to standing already earned. Nothing runs on a contributed machine without
  transparent disclosure of what it does. A contributor's own data never leaves their device
  unless they explicitly permit it. Contributions are recorded in a ledger they can audit.
- **Verify negative results by running the artifact.** A grep once "proved" a working game had no
  game logic. A 0/7 demo result was Ollama being down. Assume the measurement is wrong before
  assuming the system is.

---

## 3. Triggers — what unlocks what

| Trigger | Unlocks |
| --- | --- |
| Video posted, first external nodes join | §4 near horizon |
| Strangers earning credits that matter to them | §5 protocol hardening (identity, durable scheduler, real verification) |
| Repeat requesters with recurring jobs | §6 reliability and product shape |
| Someone actually asks for it | Org multi-tenancy, accounts, quotas (§7) |
| ~10 independently operated nodes doing verifiable work | §8 guild economics becomes meaningful |
| Guild has ~10 real members to govern | Guild charter v0 |
| A trigger doesn't exist for it | It stays in §9 or §10 |

---

## 4. Near horizon — the first things after launch

**MCP as the primary interface.** Already shipped and measured 10/10 end-to-end. The strategic
bet worth making: the swarm is most compelling not as a website but as a *tool other agents
reach for*. "My assistant delegated this build to four volunteers' laptops" is the 2026-native
version of this project. Consider making MCP the headline interface rather than a feature.

**Ensemble execution — BUILT AND MEASURED, Aug 15; result inconclusive, see
[docs/ensemble-vs-decomposition.md](docs/ensemble-vs-decomposition.md).** 12/22 single-shot against
decomposition's 2/10, p = 0.073. Settling it needs ~19 runs per arm, almost all of the cost on the
decomposition side. Not promoted. The original entry follows.

**Ensemble execution.** The sharpest finding from external review, and it comes from our own
data: chart 10/10 vs Snake 2/10 isn't only model weakness. The architecture is good at
independent, cheaply-checkable subtasks and bad at tightly coupled artifacts where blind agents
must agree on shared interfaces. So: have N nodes each produce a *complete* candidate
independently, then select by mechanical checks, instead of decomposing one artifact across
three. May well beat decomposition on exactly the tasks that fail today. Measure with
`compare.py`; remember nothing under ~6 prompts resolves at n=28.

**Grow the eval set.** At n=28 the instrument can't see anything smaller than about six prompts.
Prompt tuning is not measurable until this happens, so any future prompt work is blocked behind
it.

**Domain and HTTPS.** A raw IP with a port reads as sketchy in a launch post. ~$12/year plus
Caddy for automatic certs. An afternoon, and it's the difference between "some guy's IP" and "a
project."

**Narrow the workload claim.** The strongest early product is not "build any app from one
sentence." It's bounded work with cheap verification: test generation, static analysis and
repo-wide refactors, patch candidates evaluated by tests, structured extraction against schemas,
data transformation with deterministic validation, batch classification with auditable samples,
synthetic data with validators. The chart-vs-Snake gap is the evidence, already committed.

---

## 5. Protocol hardening — before strangers earn anything real

_From external review, August 2026. Ordered by severity, which is not the order they were built
in. Server-issued attempt binding shipped first; Theme 2A later added durable per-node bearer
enrollment and individual revocation. Public-key identity and the rest remain deferred._

**Per-node durable bearer enrollment.** Theme 2A now treats `node_secret` as bootstrap admission and
uses a distinct digest-only bearer credential for durable enrollment, restart-stable attribution,
and individual revocation/rotation. A future less-trusted boundary still needs a keypair at first
setup, challenge-response, and signed provenance/result envelopes. Bearer enrollment does not
claim physical-machine identity, attestation, or Sybil resistance.

**Server-issued expiring leases.** Every assignment carries `task_id`, `attempt_id`,
`assigned_node_id`, `payload_hash`, nonce, issue and expiry. The node signs a result envelope
including input and output hashes. Acceptance requires valid signature, matching assignee,
matching nonce, unexpired lease, unsettled attempt.

> **Partly shipped, PR #45 (Aug 14).** Handing out a task now mints an attempt id and a nonce,
> both distinct from `task_id`, with a 900 s lease; settlement requires the right node, matching
> nonce, unexpired lease and an unsettled attempt, and is idempotent on retry. A node on an older
> build has its result *recorded but not settled*, so no work is lost. **Still deferred:**
> `payload_hash`, the signed result envelope, and everything that depends on a keypair — which is
> the item above. Attempt binding stops an admitted node stealing another node's credit; it does
> not stop a holder of the shared secret joining under a chosen name.

**Durable attempt-based scheduler.** Nodes, queues, in-flight assignments, reputation and
breaker state are currently process-local; a restart loses the answers to "was this leased or
merely queued," "can two retries both be paid," "can a late result overwrite a fallback."
Explicit persisted state machine — `queued → leased → running → submitted → verified → settled`,
with `expired / rejected / cancelled / failed / dead_lettered` — each retry a separate attempt,
uniqueness constraints making settlement idempotent. SQLite is fine to start; Postgres only if it
stops being.

**Layered verification.** Duplicate-and-compare mostly measures output *shape*, and when two
outputs disagree the coordinator doesn't know which is wrong. Real verification is task-specific
and stacked: deterministic checks first (schema, compile, tests, checksums, browser assertions),
then hidden canaries with known answers, then redundancy where determinism isn't available, then
tie-breaking to a trusted node or stronger model, then semantic judging *only* after the
deterministic layers, then post-hoc reputation updates from whether downstream work passed.
BOINC learned this over decades: untrusted hosts require replication, validation, signed
software, bounded uploads.

**The verification endgame, beyond redundancy.** The layers above establish *probable* honesty
through repetition and reputation. Cryptographic verification establishes it outright, and the
technology has been maturing on roughly the timeline this project would need it. Two paths, in
increasing order of strength and cost. *Trusted Execution Environments* — Intel SGX/TDX, ARM
CCA, AMD SEV — let a node prove that specific code ran on specific inputs inside a hardware
enclave the node's owner can't tamper with. Available on most modern silicon, practical today,
and the natural bridge. *Zero-knowledge proofs of inference* are the real destination: a node
proves it ran the model faithfully without revealing anything else. Still expensive for large
models, but the direction is clear — sub-second proofs for small networks, sampling-based schemes
that trade a little certainty for a lot of overhead, and funded teams working specifically on
proof-of-inference. Neither belongs in a trusted network; both are what a *permissionless* one
would eventually require, and knowing they exist is the reason a public network isn't a fantasy.

**Transactional ledger.** Credits currently accrue on a non-empty result. A real entry carries
requester, node, task, attempt, result hash, verifier version and evidence, debit and credit,
settlement status, signature — with provisional / verified / disputed / reversed / spent as
distinct states, and issuance only after accepted verification. Centralized and append-only;
this is exactly the wrong place for a blockchain while the economics are still hypothetical.
The intermediate step between "a JSON file" and "a distributed ledger" is *hash-chaining* — a
Merkle structure over entries so anyone can verify the complete history hasn't been rewritten,
with no consensus mechanism and no chain. Tamper-evidence is the property that actually matters;
decentralized consensus is a much more expensive thing that is usually mistaken for it.

**Real sandboxing.** Generated code is not sandboxed today, and the docs say so. Before anything
executes code from untrusted nodes: disposable containers, microVMs or WASM, no network by
default, read-only base, ephemeral workspace, CPU/memory/time/output limits, no inherited
credentials, destroyed after validation. A service boundary, not a helper function inside the
API process.

**Confidentiality classes.** Every task labeled `local_only` / `trusted_guild` /
`approved_nodes` / `public_network`, with the UI stating plainly when a prompt will be visible
to third-party operators.

**The unresolved architectural question: coordinator vs. peer-to-peer.** The original vision was
a protocol nobody owns. What exists is a *central coordinator* that nodes connect to — which is
why joining is one command, why the scheduler can be made durable, and why any of this works at
all today. But it means whoever runs the coordinator holds real power: the queue, the ledger, the
routing. The fork right in §2 is the current answer, and it may be sufficient — anyone can run
their own instance, and federation (§7) would let instances talk. A genuinely peer-to-peer
version would need a different stack: libp2p for transport and NAT traversal, a Kademlia-style
DHT for capability discovery, QUIC for unreliable consumer connections. That's a rebuild of the
foundation, not an addition, and it should only happen if a coordinator turns out to be a real
constraint rather than a theoretical one. Worth deciding deliberately rather than by drift.

**Adversarial protocol tests.** Result submitted under another node's id, replay, expired lease,
duplicate after retry, restart mid-submission, clock skew, duplicate identity registration,
oversized or malformed payload, Sybil registration, colluding verifiers, disk-full, crash
between verification and settlement. Property-based state-machine tests are the right shape.

> The first four exist as of PR #45 (`tests/test_result_binding.py`), as example-based tests
> rather than property-based ones. The rest are open.

---

## 6. Reliability and product shape

**Execution strategies as first-class choices** — map (many independent items), ensemble
(complete candidates, select by test), DAG (dependent subtasks, typed handoffs), single (one
capable node), consensus (duplicate where agreement means something). The planner picks.

> **What building and measuring ensemble taught, Aug 15** — none of the others are built, and
> this is the note for whoever picks them up (full result:
> [docs/ensemble-vs-decomposition.md](docs/ensemble-vs-decomposition.md)):
>
> - **"Single" is not a separate strategy.** It is ensemble with N=1. Build the parameter,
>   not five code paths.
> - **Selection is the hard half, not generation.** Ensemble is only as good as the checks
>   that pick the winner, and those checks took *four* bug fixes to become trustworthy. A
>   strategy needing semantic judgement to select — rather than parse/load/draw/respond —
>   inherits a much harder problem than the one that stopped this experiment being conclusive.
> - **Cost ratio beats success rate.** Ensemble's practical advantage came from costing ~6
>   minutes an attempt against decomposition's ~50, not from being better per attempt. Any
>   strategy comparison that reports quality without cost is measuring the wrong axis.
> - **Pick the strategy per workload, and measure it.** The same harness scores any showcase
>   candidate; the uncoupled case (`--candidate chart`) runs in a fifth of the time.
> - **A ten-run baseline caps what any comparison can prove.** Whatever is compared next,
>   size *both* arms first — see the write-up on why 22 trials could not clear p<0.05.

**Task contracts instead of prompt strings.** Typed inputs, output schema, required
capabilities, verification policy, confidentiality, timeout, output cap, network policy. The
prompt lives *inside* the contract. This is the real protocol abstraction.

**Measured capabilities, not self-reported.** Observe model and quantization digest, tokens/sec,
first-token latency, context success, recent success by category, uptime and churn, memory
failures, canary performance. Schedule on estimated completion, acceptance probability, trust and
cost — not tags a node claims about itself.

**Observability.** Queue depth, lease age and expiry, retries and duplicates, fallback rate,
success by workload/node/model/prompt-version, verifier rejection reasons, latency percentiles,
memory growth, churn, credits per verified result, compute-minutes per accepted result.
OpenTelemetry plus Prometheus-compatible metrics; every job traceable end to end.

**Protocol versioning.** `/v1` routes, advertised `node_protocol_min/max`, server version,
contract versions. A distributed node population can't be upgraded at once, so compatibility and
deprecation rules must exist *before* external operators depend on them.

**Operator experience.** One-command install (exists — and was broken from the day it was
advertised until Aug 14; see the consent/`/dev/tty` fix), key generation, coordinator trust
fingerprint,
bandwidth and disk limits, privacy warning, auto-update policy, drain mode before shutdown,
health diagnostics, signed releases and images, SBOM and lockfile. The safer and duller the node
experience, the longer people leave it running.

**Find the memory leak.** ~1.25 MB per pitch on Windows, linear over 120 pitches. Not a launch
risk at ~800 pitches/GB; a real one for a long-lived public orchestrator.

**Latency, if interactive use ever matters.** Measured WAN overhead is ~2% of a pitch, so this
isn't a problem for batch work — but it becomes one for anything conversational. The levers, in
rough order of value: MoE models where only a fraction of parameters activate per step;
topology-aware routing that prefers nearby capable nodes; speculative decoding where a small
local model drafts while the swarm verifies; batching that trades individual latency for
throughput. Design around interruptible async work rather than continuous streams for as long as
possible.

**A coordination principle worth not losing.** The orchestrator doesn't need to be the smartest
model in the network — it needs to be the best *coordinator*. A well-tuned small planner
directing specialized builders should beat one large model doing everything, and that's the whole
bet the architecture makes. If planner quality ever becomes the bottleneck, route *it* to a
stronger model (the model router already supports this) rather than concluding the swarm needs
bigger models everywhere.

**Smaller issues from the August review, worth tickets rather than a project:** apply the same
verification policy to every distributed route including the direct distributed-pitch path
(`/pitch/async` samples for verification; `/pitch/distributed` does not) · enforce queue limits
atomically at enqueue rather than checking once before a wave · persist reputation and verifier
evidence instead of losing it with the process · finish live config reload (`verify_rate` already
re-reads per pitch; nothing else does, and none of it is tested) · UUIDv7/ULID identifiers rather
than timestamp-derived ones — **narrower than it reads:** job ids, task ids and attempt ids
are already `uuid4().hex`. What is still timestamp-derived is the *run directory* name
(`output/20260815_022131`, from `orchestrator.py`) and the eval/script run stamps, which
collide only if two runs start in the same second.

> **Done Aug 14:** raw internal exception text in production 500s. Two
> `@app.exception_handler(Exception)` handlers were registered in `server.py` and Starlette keys
> them by class, so the later, leaky one silently replaced the hardened one — every 500 returned
> `str(exc)`. The chaos test that should have caught it was asserting the leaky handler's response
> shape. Both fixed.

---

## 7. Scale — only after 5 and 6 hold

Multi-user accounts and project isolation · scoped requester and node credentials · budgets and
quotas · coordinator high availability · multiple guilds · federation between coordinators ·
cross-guild reputation · governance and dispute process · pricing that reflects difficulty,
latency, hardware and redundancy cost.

**Org multi-tenancy sits here, deliberately.** Enterprise plumbing for a system with zero
external users; an org can already get a private pool by running its own instance. Decided
August 10, 2026. Revisit only when someone actually asks.

Federation is consistent with the vision, but a federation of unreliable state machines is worse
than one reliable coordinator.

---

## 8. The guild layer

**Credits are contribution points, not currency** — and should be described that way until
verification makes them mean something. 1 credit ≈ 1 standardized GPU-second, with hardware
multipliers so a Mac Mini and a 4090 earn at honest relative rates.

**Who earns:** idea pitchers (founding stake in what they pitch), compute contributors
(proportional to work verified), quality reviewers (validation is real labor), agent creators
(ongoing royalties when their specialized agent is used), protocol contributors (improvements to
the system itself).

**Anti-freeloading, in order of preference:** make contributing trivially easy first; then
pitch access scaled to recent contribution; then a small stake at risk on a pitch so low-effort
submissions cost something. Tiered access before hard gates.

**Guild charter v0** — written when there are ten real members to govern, not before. Contents:
standing categories and how each is earned, the one-member-one-vote rule, supermajority for
ethical boundaries, any member able to trigger an ethics review, transparency of ledger and
governance, fork rights.

**What the network won't build, decided collectively.** The guild sets the boundary, not a
founder and not a board. The starting position: nothing that facilitates harm, mass surveillance,
or exploitation — and agent execution stays scoped so no task can reach network resources outside
its own sandbox. Boundary changes need a supermajority; any member can flag an active project for
emergency review and halt it pending that review. Slow and deliberate on purpose. A single person
deciding this is the failure mode, whichever direction they decide in.

**Anti-concentration.** No single entity should control a disproportionate share of network
routing — a working rule of thumb is 10%. Concentration is how an open protocol quietly becomes
someone's platform, and it happens through accumulation rather than a decision anyone votes on.
Worth measuring before it needs enforcing.

**Why a guild and not a DAO.** DAOs have a branding problem and a structural one. The branding is
crypto speculation and governance theater. The structure is token-weighted voting, which means
wealth buys power — the exact thing §2 forbids. A guild is a pre-industrial idea that actually
worked: practitioners who set standards, protect members, and bargain collectively. The concept
is used here honestly, not in its crypto form.

**The models that were considered and set aside.** A pure *co-op* optimizes for members but
historically scales badly. A pure *open protocol* scales best but has no way to enforce values —
someone will build something exploitative on it, and that's the price of true openness. A
*startup* maximizes incentive but recreates the extraction it was meant to replace. The
three-layer answer in §1 takes the open protocol at the bottom, the guild's values in the middle,
and commerce on top — because they operate at different layers rather than being alternatives.
Worth re-reading before anyone proposes collapsing them.

**The open economic questions, which matter more than the technical ones.** Do requesters have
recurring jobs? Will operators keep nodes online? Do contributors value earning future compute?
Does verification and redundancy cost more than the work is worth? Are credits scarce enough to
matter and accessible enough to bootstrap? None of these are answerable without users.

---

## 9. The marketplace, and the ideas that make it more than infrastructure

_Speculative. No triggers. Kept because they're the reason the project exists._

**The idea marketplace itself:** pitch → multi-agent evaluation → stake → build → deliver, with
public and auditable evaluation criteria, no black-box rejection, IP retained by the pitcher,
automated duplicate detection.

**The evaluation problem — the hardest unsolved piece of the whole vision.** "If your idea is
good enough, the swarm builds it" requires the system to judge whether an idea is good, which is
asking an AI to predict what an AI swarm can accomplish. False positives waste the network; false
negatives are worse, because they're invisible and they land on exactly the people this is
supposed to serve. Approaches worth trying, cheapest first: *staged* evaluation, where a fast
first pass costs one agent and thirty seconds and only survivors get depth · *human-in-the-loop*
review for anything large, drawn from guild members · *skin in the game*, a small stake of earned
credits at risk, which filters low-effort pitches without gating on wealth · a *portfolio*
posture that approves many ideas at small scale and lets winners emerge, rather than few at large
scale · and *prediction markets*, where other members stake on whether a pitch will succeed,
producing a wisdom-of-crowds signal the evaluator can't generate alone. None of this is
buildable until there are enough members for the social mechanisms to mean anything — but the
design should not assume a single evaluator model can carry it.

**The Dream Queue.** Pitch before bed; the swarm works overnight on the world's largest idle
compute resource; wake to a progress report and a question it needs answered.

> **One measurement already stands against the naive version, and it is worth knowing before
> anyone builds this.** Ollama degrades over a long session on one machine: across seven
> back-to-back `--demo` runs, duration climbed 26 → 33 → 40 → 36 → 47 → 67 → 83 minutes
> (rank correlation with run order **0.96**), the last two died on 30-minute model-call
> timeouts, and restarting Ollama took the *same task* from 83 minutes back to 34 — so it is
> session state, not thermal. `--demo` is **6/6 on a fresh Ollama and 0/2 after 5+ hours.**
> An unattended overnight queue is exactly the shape that hits this. Whatever this becomes
> has to cycle the runtime between jobs, or watch per-job duration and restart on drift.
> Cheap to design in, expensive to discover at 3am.

**Agent specialization marketplace.** Anyone fine-tunes an agent, contributes it, earns royalties
on use. Agents compete for tasks on track record — evolutionary pressure toward quality.

**Proof-of-idea.** Not proof-of-work (burn energy) or proof-of-stake (lock capital). Mining is
contributing good ideas; the better they perform, the more compute you earn.

**Emergent collaboration.** The orchestrator notices three separately-pitched ideas are
components of one system and proposes a merged project with shared stake.

**The transparent factory.** Watch your agents work — which node, which function, which reviewer
flagged what. Trust *and* spectacle; the Twitch stream of AI labor.

**The fork mechanism.** Disagree with a project's direction, fork it, redirect the swarm, let
both run. Open-source governance applied to production.

**Seasonal compute.** Campuses empty over summer; offices idle overnight. Institutional
contribution earning standing that funds scholarships or research.

**Skill trees for nodes.** The network learns that a particular machine is excellent at code
generation between 11pm and 7am, and routes accordingly — institutional memory about its own
infrastructure.

**The far horizon:** a "company" born as an idea and built by a swarm, with no incorporation and
stake distributed to everyone who contributed · a leapfrog where a phone in Lagos participates on
equal terms with a workstation in San Francisco · collective intelligence that grows because
coordination improves, not because any model does · credible counter-power to a handful of
companies controlling meaningful AI capability — not "we're smarter" but "we're yours" · the
science machine, where a hypothesis with no lab funding gets six months of research work in six
days.

---

## 10. Explicitly rejected

**Blockchain, tokens, real-money settlement** — permanent, see §2. **WAN model-layer sharding as
the core abstraction** — token generation dominates and network is ~2% of a pitch; task-level
parallelism is the right primitive. Layer sharding may eventually exist as a LAN super-node
capability, never as the center. **More agent personas** — the limiting factor is verification,
not headcount. **A social marketplace UI, vector-memory platform, mobile apps** — surface area
that doesn't fix identity, durability, verification, sandboxing, or demand. **Auto-installation
by agents** — see §2.

---

## 11. Documentation debt

~~`THREAT_MODEL.md` and `SECURITY.md`~~ **both written, Aug 15** · `ARCHITECTURE.md` with sequence diagrams ·
`PROTOCOL.md` with normative state transitions and invariants · `PRIVACY.md` ·
`OPERATIONS.md` (backup, restore, upgrade, incident recovery) · `docs/adr/` for decision records ·
machine-readable benchmark history · changelog and tagged releases · GitHub milestones and public
issues derived from the sprint plans.

A Mermaid diagram of registration → lease → result → verification → revision → settlement →
failure recovery would do more for new contributors than any prose.

---

## 12. The measurement that matters next

Not a feature. The milestone that makes the guild vision credible:

> **Ten independently operated nodes completing verifiable work through an identity-bound
> protocol, with no incorrect credit settlement, and demonstrable value over a single machine.**

Supporting criteria worth holding to: >95% of jobs reach a correct terminal state across
coordinator and node restarts · zero unauthorized result or credit acceptance under an adversarial
test campaign · <1% duplicate or incorrect settlements · ≥85% verifier-passed success on chosen
narrow workloads · measured cost per accepted result including retries and redundancy · repeat use
by requesters without prompting · four-week node-operator retention · no unbounded coordinator
memory or storage growth.

---

## Changelog

- **2026-08-14** — Created. Consolidates the full project vision, the deferred technical work,
  the August 2026 external review, and the speculative ideas that predate the code.
- **2026-08-14** — Completeness pass against the full design history. Added: contributor rights
  as a permanent constraint (§2); the unresolved coordinator-vs-P2P question and the stack a true
  P2P version would need (§5); latency levers, the coordinator-need-not-be-smartest principle,
  and the smaller review issues (§6); the guild-vs-DAO rationale and the co-op / pure-protocol /
  startup models that were set aside (§8); the idea-evaluation problem, which is the hardest
  unsolved piece of the marketplace vision (§9).
- **2026-08-14** — Checked against MASTER_PLAN, CLAUDE.md and the sprint log's measurements,
  the other two halves of the sanity check. Two contradictions found and resolved *outside*
  this file: CLAUDE.md listed ExoLabs sharding as the future direction (§10 rejects it as the
  core abstraction, and SPRINT §4 has the 216 ms reason), and MASTER_PLAN's 90-day metric
  still listed the MCP interface as a goal after it shipped. One item annotated here: the
  Dream Queue (§9) runs straight into the measured Ollama session degradation.
- **2026-08-14** — Sanity-checked against the repo. Corrected: which §5 item PR #45 actually
  shipped (attempt binding, not per-node identity), the four adversarial scenarios that now have
  tests, the 500-leak item (fixed the same day), the live-config-reload item (partly done), and
  `SECURITY.md` / `THREAT_MODEL.md` (neither existed; both written Aug 15).
- **2026-08-14** — Second completeness pass. Added: the cryptographic verification endgame,
  TEEs then zero-knowledge proofs of inference, which is what a permissionless network would
  eventually require (§5); Merkle hash-chaining as the tamper-evidence step between a JSON file
  and a distributed ledger (§5); the substance of what the network won't build and who decides,
  plus the anti-concentration rule on routing share (§8).
