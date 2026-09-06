# ROADMAP — Mycelium

_The single home for everything not being built right now: the long-term vision, deferred
engineering, external review findings, and speculative ideas. Last updated: August 31, 2026._

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

**Ensemble vs decomposition — PRE-REGISTERED Sep 3, not run. See
[docs/experiments/ensemble-vs-decomposition.md](docs/experiments/ensemble-vs-decomposition.md).**
The pilot was unpaired against one artifact. The pre-registered design pairs three arms
(decomposition, ensemble at the cost-matched five candidates, direct as baseline) over the 36-item
locked confirmatory set, with **success at equal compute** as the primary endpoint and the compute
ratio measured rather than assumed. Powered for the ~35-point gap the pilot suggests (p=0.82 at
n=36) and nothing smaller. **~39 hours of inference**, plus ~18 hours to band the confirmatory
items first. The no-difference criterion and the criterion for dropping decomposition as the
default are both written down in advance.

**Ensemble execution.** The sharpest finding from external review, and it comes from our own
data: chart 10/10 vs Snake 2/10 isn't only model weakness. The architecture is good at
independent, cheaply-checkable subtasks and bad at tightly coupled artifacts where blind agents
must agree on shared interfaces. So: have N nodes each produce a *complete* candidate
independently, then select by mechanical checks, instead of decomposing one artifact across
three. May well beat decomposition on exactly the tasks that fail today. Measure with
`compare.py`; remember nothing under ~6 prompts resolves at n=28.

**Grow the eval set — DONE Sep 3, and the conclusion is not the one this entry expected.
See [docs/eval-methodology.md](docs/eval-methodology.md).** The corpus is now **100 items**
(64 development, 36 in a digest-locked confirmatory set), banded by difficulty, written from a
documented task taxonomy rather than from a failure log, and graded mechanically with no
model-judged primary endpoint.

The **"about six prompts" figure was a rule of thumb and it was optimistic by roughly a factor of
two.** It was a significance threshold at the observed churn, not a power calculation. Computed at
80% power from the measured discordant rate (ψ = 0.643, from the one pair of identical-configuration
runs): **n=28 detects 38.4 points ≈ 10.8 prompts; n=100 detects 20.6 points.**

**Growing the corpus did not fully unblock prompt tuning, and no achievable corpus does.**
Detecting a 15-point change — smaller than v1 → v3's 25 points, and about the size of an ordinary
good change — needs **n = 187**, which is 91 hours per run and 182 hours per comparison on the
reference machine. That is not proposed. What 100 items
buys is the ability to resolve a *large* change (an architecture swap, a model change, a prompt
rewrite that moves a fifth of the set) for about four days of CPU, instead of spending four days on
a question the instrument could never answer. The remaining lever is replicates per item with a
continuous endpoint, which changes the endpoint and should be pre-registered separately.

The instrument now has controls: a deliberately degraded arm is detected (p = 0.0005), two runs of
an identical configuration are not (0 false positives in 20 pairs), and an arm answering a
*shuffled* prompt-to-task pairing is **invisible** to the parse-and-run checks the old harness
relied on. That last one is measured, not argued: the old HTML check scored `web-snake` 5/5 while
`showcase_reliability.py` measured the same artifact at 2/10.

**Domain and HTTPS — NO LONGER OPTIONAL, 2026-09-05; REACHABLE, 2026-09-06.** A raw IP with a
port read as sketchy in a launch post; that was the whole argument, and it was an aesthetic one.
It became a hard prerequisite instead: the worker refuses plaintext HTTP to any non-loopback
host, with no flag, environment variable, or configuration key that permits it, so an operator
cannot invite anybody until TLS is in front. ~$12/year plus Caddy for automatic certs, and
`tailscale cert` covers the overlay path.

That left a prerequisite with no instructions, which is a prerequisite nobody meets.
[docs/DEPLOY.md](docs/DEPLOY.md) is now written around two paths — private overlay first,
public domain second — for somebody who has never administered a server, with the Caddyfiles
in `deploy/` and an executable host preflight in `scripts/deploy_preflight.py`. **Recommendation
for a first invited node: Path A**, because it removes the entire category of problem where a
stranger can reach the coordinator at all, which matters more than the one extra thing a
contributor has to install. See [docs/OPERATOR_PREFLIGHT.md](docs/OPERATOR_PREFLIGHT.md).

**Worker onboarding — DONE 2026-09-05.** `python worker_installer.py` replaces clone-install-pull-
run-paste-a-secret with one guided command, and [docs/JOIN.md](docs/JOIN.md) is written for
somebody who has barely used a terminal. The onboarding cost was never the interesting part; what
made this worth doing is that the same change closed the two things a contributor could not
evaluate for themselves — whether the transport was safe, and whether their machine would run what
the coordinator sent.

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

> **Hash-chaining shipped, Theme 3C.** A linear chain rather than a Merkle tree: a tree buys
> efficient inclusion proofs, which matter only when proving membership to a third party without
> handing over the whole log, and nobody needs that yet — the upgrade path and its trigger are in
> [ADR 0017](docs/adr/0017-provenance-is-a-binding-of-identity-not-a-claim-of-correctness.md).
> Links are written inside the settlement transaction, so settlement atomicity is unchanged;
> pre-chain entries are the genesis boundary and are not retrofitted;
> `python scripts/ledger_chain_admin.py verify` reports the first break with its index. It is
> tamper **evidence**, not tamper proofing: an operator with database access can rewrite every
> entry and every link and it verifies clean, which is asserted by a test rather than hoped for.
> The rest of the item — requester/verifier/evidence fields, provisional/verified/disputed/
> reversed/spent states, issuance only after accepted verification — remains open. Artifacts
> also now carry a provenance envelope binding them to the identity that produced them, which
> establishes no correctness and is not signed.

**Real sandboxing.** Generated code is not sandboxed today, and the docs say so. Before anything
executes code from untrusted nodes: disposable containers, microVMs or WASM, no network by
default, read-only base, ephemeral workspace, CPU/memory/time/output limits, no inherited
credentials, destroyed after validation. A service boundary, not a helper function inside the
API process.

> **Narrow prerequisite implemented (August 2026):** parser-heavy trusted
> built-in validators use a bounded, versioned child-process protocol with
> fail-closed evidence, wall-clock/process cleanup, and available POSIX resource
> limits. `code_parse` receives bounded copied bytes; metadata-only validators
> forced through the runner receive validated logical names without copying
> artifact content. Production validation still never imports or executes
> generated code. The child shares the coordinator's OS user, does not guarantee
> filesystem confidentiality or network denial, and is not the hostile-code
> sandbox described above. Containers, microVMs, WASM, behavioral execution,
> and arbitrary validator plugins remain deferred.

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

> **All twelve now have coverage (Theme 1.4).** A Hypothesis `RuleBasedStateMachine`
> drives the real coordinator against a reference model of the intended protocol
> (`tests/protocol_model.py`), with ten global invariants asserted after every generated
> step under adversarial ordering, injected persistence faults, coordinator restarts, and
> clock movement. The first four keep their example-based tests in
> `tests/test_result_binding.py` and are now generated as well; the other eight have
> dedicated deterministic coverage in `tests/test_adversarial_scenarios.py`.
>
> Two of the twelve assert *documented behaviour rather than resistance*, because the
> project does not claim resistance. Sybil registration: a holder of `node_secret` gets N
> enrollments for N labels, and what is asserted is that each is separately attributed,
> separately revocable, and earns exactly its own credit. Colluding verifiers: sampled
> agreement is off by default, describes output shape, and moves no eligibility, ordering,
> settlement, or credit decision.
>
> The campaign's findings, the invariant list, the seams introduced, what it does not cover,
> and its measured CI cost are in
> [`docs/adversarial-campaign.md`](docs/adversarial-campaign.md). Theme 1.5 added a standing
> coverage floor: a run that does not reach settlement, replay, idempotency conflict, an
> injected fault, a restart with an outstanding handout, and a janitor reclaim now fails CI
> rather than reporting green. Its first run showed the campaign had still been reaching zero
> accepted settlements, which is written up there in full.

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

> **Trace propagation done, Theme 4B; the metrics half is not.** W3C trace context
> now crosses the coordinator/worker boundary - handout, result, token batch,
> heartbeat, drain, registration - and into the validator subprocess as
> environment rather than payload. A unit keeps one trace across reassignment,
> which a test found was not true of the first implementation and is the whole
> point of asking where a job went. Off by default; the OpenTelemetry SDK is an
> optional extra and nothing imports it at module scope. Propagation and export
> are separate switches, because accepting a trace ID costs a contributor nothing
> while exporting their machine's spans is telemetry - export is off by default
> and never a condition of joining. Span attributes are a keyword-only allowlist,
> so an unknown key is a `TypeError` rather than something a scanner has to
> catch; high-cardinality identifiers are span attributes and appear in no metric
> label. **Nothing about diagnosis time is claimed, because nothing was
> measured** - that needs external nodes and a diagnoser who did not write the
> code, and the protocol including what would count as no improvement is in
> [docs/experiments/trace-diagnosis-time.md](docs/experiments/trace-diagnosis-time.md).
> Still open from this line: queue depth, lease age, retry and duplicate rates,
> fallback rate, success by workload, verifier rejection reasons, latency
> percentiles, memory growth, churn, credits per verified result, and
> Prometheus-compatible exposition - `/metrics` is still a fixed JSON object.
> See [ADR 0018](docs/adr/0018-trace-context-is-propagated-export-is-the-contributors-choice.md).

**Protocol versioning.** **Done, Theme 4A** for the worker half: `/v1/worker-protocol`
advertises `node_protocol_min`/`node_protocol_max` and the server version without a
credential, an out-of-window worker is refused before any enrollment or session exists with
distinct too-old/too-new codes, checking happens at registration and session establishment
rather than per request, and `docs/PROTOCOL.md` carries the breaking-change definition and
deprecation policy. Nothing was bumped: the window holds exactly version 1. Contract and
execution versions below remain as described. See
[ADR 0015](docs/adr/0015-worker-protocol-compatibility-window.md). Original item:
`/v1` routes, advertised `node_protocol_min/max`, server version,
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
evidence instead of losing it with the process (**verifier evidence done, Theme 3B-1** —
durable, scoped, append-only, replay-safe, and never authoritative over terminal state, per
[ADR 0014](docs/adr/0014-durable-verification-evidence.md); reputation remains unbuilt and is
not planned, and nothing was re-enabled) · finish live config reload (`verify_rate` already
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
or exploitation — and any future generated-code execution must stay scoped so no task can reach
network resources outside its sandbox. That sandbox is a future requirement, not a current
guarantee; `network_policy` remains recorded intent. Boundary changes need a supermajority; any
member can flag an active project for
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

- **2026-09-06** — TLS became a hard prerequisite yesterday and there was no way to meet it.
  This closes that: two deployment paths written out in full, the coordinator pinned where a
  proxy has to be in front of it, and the parts of the pre-flight that a program can check
  turned into a program.

  **The bind, and the trap under it.** `docker-compose.yml` publishes a literal
  `127.0.0.1:8000:8000`. It previously published `${MYCELIUM_PUBLISH_ADDRESS:-127.0.0.1}` — a
  safe default with an override, and the override is the whole problem. **Docker does not
  consult ufw.** A published port becomes a rule in Docker's own iptables chain, evaluated
  before the chain ufw manages, so `ufw deny 8000` reports "deny" on a port that is answering
  the entire Internet and `ufw status` will never say otherwise. That is the single most likely
  way this deployment ends up exposed while its owner believes it is not, so the address is now
  a literal that no `.env` file can move, and DEPLOY.md documents the trap with the two commands
  that actually reveal it (`ss -tlnp`, `sudo iptables -L DOCKER -n`).

  **Self-signed certificates cannot work, and now we say so.** A worker trusts the certifi
  bundle and nothing else, and builds its client with `trust_env=False` — the same decision that
  stops an ambient `HTTPS_PROXY` inheriting enrollment bearers also means `SSL_CERT_FILE` cannot
  add a private CA. Verified by execution rather than by reading: with `trust_env=False`, httpx
  ignores a bogus `SSL_CERT_FILE` entirely rather than failing. Both documented paths therefore
  obtain publicly-trusted certificates, and `scripts/tls_local_check.py` lets an operator
  confirm theirs before inviting anybody — it serves the certificate to a fully verifying client
  on loopback and reports what a worker would do. The test suite runs the **real** installer
  (`worker_installer.fetch_protocol_window`) against a TLS-terminated stub, with the untrusted
  case asserted as a control, because a passing test that passed for the wrong reason would look
  identical.

  **Registration is not rate-limited, and that is written down rather than papered over.**
  `_check_rate_limit` exists but is wired only into the pitch routes; `POST /nodes/register`
  does not call it, so invitation codes can be guessed as fast as the network allows. A failed
  bootstrap is also not logged in a way an operator would notice — a plain 401, no event, no
  counter, only the access log. No limiter was invented here: the existing one is pitch-scoped,
  and coupling registration to it changes worker-protocol behaviour, since stock workers
  re-register automatically after a session expires. It is a finding for its own change, in
  THREAT_MODEL §14 and DEPLOY.md, and it is why the entropy floor exists.

  **`scripts/deploy_preflight.py`** turns the checkable half of OPERATOR_PREFLIGHT.md into
  something executable: publicly bound ports read from the kernel's own socket table, whether
  the coordinator's port is among them, SSH password and root login, unattended upgrades, state
  and database file modes, credential strength, certificate validity and expiry, and whether the
  protocol window answers over HTTPS. Read-only, and it prints no credential value — asserted by
  a test that generates three real credentials and greps the whole report for them. Its entropy
  estimator over-rates prose, which is the one direction that would be dangerous, so a shape
  check refuses a typed passphrase outright rather than pricing it.

  **`scripts/secret_history_scan.py`** scans every blob in the object database rather than the
  working tree, because deleting a credential in a later commit changes nothing. Its first two
  rule sets were too loose and are kept as regression tests: `keyHandlers` supplied the word
  "key" and a worktree name supplied the entropy on ten benchmark-result lines; then an
  assignment-shaped rule matched `credential_version=normalize_credential_version(...)`. A
  scanner that cries wolf is one people stop running. [docs/SECRET_ROTATION.md](docs/SECRET_ROTATION.md)
  covers all four credentials, what each breaks, and the demo-recording case.

  **Two authorities on Path A.** Tailnet membership and Mycelium enrollment are independent and
  both must be revoked; removing a device from the tailnet leaves a working bearer credential,
  and revoking the enrollment leaves the device on the network. Said in DEPLOY.md, THREAT_MODEL
  §4a, SECRET_ROTATION.md, and JOIN.md, which now has a section for contributors handed a
  `.ts.net` address.

  DEPLOY.md states plainly that none of this has been reviewed by a security professional.

- **2026-09-05** — The three exposures the previous entry recorded are closed, and macOS
  stopped being a platform nobody had ever run this on. The two halves are unrelated
  except in who they are for: somebody on a Mac being handed an invitation code.

  **Secrets and argv.** `join.py` and `node.py` grew `--secret-file PATH`, checked by the
  same `read_owner_only_text` that guards the identity file rather than a second copy of
  that check, and `--ask-secret`, which reads it with `getpass`. `--secret` still works
  — removing it would break setups that already script it — but now prints a warning
  naming both exposures (`ps`, and the shell's history file) and both alternatives, and
  says so in `--help` too. Prompting is opt-in rather than automatic on purpose: a
  returning worker needs no code at all, and an unconditional prompt would hang every one
  of them. `join.py` no longer rewrites `sys.argv` to hand over to `node.main`; it passes
  a function argument, so the code never reaches an argument vector even in-process.

  **`curl … | bash`.** `install.sh` and `install.ps1` are deleted. Not fixed — deleted.
  The form itself is the problem: the download and the execution are one command, so
  there is no point at which the person running it can read what is about to run, and
  nothing to check the bytes against. That is the wrong default for software whose entire
  request is "lend me your computer". README, `docs/demo-script.md`,
  `docs/community-pitch.md`, `docs/DEPLOY.md`, `.gitattributes`, and the CI shell-parse
  step all pointed at them and were updated; `tests/test_join_consent.py` now scans the
  whole tree for a one-liner rather than asserting the deleted script's contents. Deleting
  it also closed a fourth exposure nobody had reported: `install.ps1` read the invitation
  code from `$env:SWARM_SECRET` and appended it to a child's argument list, so the code was
  in the environment *and* in argv.

  **macOS.** The gap was never the identity path — that already targeted
  `~/Library/Application Support/Mycelium/` — it was that nothing around it had ever been
  executed on a Mac. The installer now distinguishes "Ollama is not installed" from
  "installed but never opened", which is the normal state of a Mac two minutes after
  installing it, because macOS starts the service and creates the `ollama` command only
  when the application is first opened. It prefers the daemon check over the command,
  reports Apple Silicon versus Intel, and changes no search path, shell profile, trust
  store, launch agent, or quarantine attribute — asserted across every worker module, not
  just the installer.

  CI grew a **macOS job on `macos-14`** (Apple Silicon, free for public repositories),
  because a green Windows run skips every POSIX permission assertion and a green Linux run
  never touches `Application Support`. That job is what makes the 0600 claim on macOS an
  executed one rather than a reasoned one.

- **2026-09-05** — Joining is one guided command, and plaintext transport is gone. The
  second half is the one that matters: the worker now refuses `http://` to every host
  except loopback, with **no flag, environment variable, or configuration key** that
  relaxes it, and the check lives inside `normalize_coordinator` — the single function
  the join flow, `node.py`, the enrollment admin tool, and identity-file validation
  already share, so there is no path around it rather than a discouraged one. This
  deliberately breaks the private-overlay HTTP join that DEPLOY.md recommended. That is
  the point: an overlay ACL is an operator assertion, and a contributor being invited to
  lend a laptop is not in a position to audit it. `tailscale cert` keeps the overlay path
  open for operators who want it.

  `worker_installer.py` walks nine steps, refuses to run as root, names every file before
  writing one, and — because it performs the bootstrap enrolment itself — means the shared
  invitation code never has to reach `node.py --secret`. It is typed with echo off or read
  from a permission-checked file, travels as exactly one header on one request, and appears
  in no output, log, temp file, or traceback. Credential generation, atomic write, and 0600
  are Theme 2A's, reused rather than rewritten. `worker_installer.py uninstall` drains from
  the coordinator, deletes the credential, and says what it deliberately left behind.

  The contributor-safety property is now a test rather than a sentence in AGENTS.md:
  `tests/test_contributor_safety.py` hands the worker a task carrying Python, shell
  fragments, command substitution, and a path traversal, and asserts inference output and a
  byte-for-byte unchanged filesystem. Writing it found one real defect — coordinator-supplied
  task titles were printed straight into Rich, so an operator could colour a volunteer's
  terminal and plant clickable links in it. Fixed and tested. Two known holes are recorded
  rather than closed: `join.py --secret` and `node.py --secret` still put a secret in argv
  where `ps` can read it (kept for existing scripted setups, and no longer needed by the
  installer path), and `install.sh` is still advertised as `curl … | bash` in its own
  header. **Both were closed the next day — see the 2026-09-05 entry below this one.**
  One sentence in the original version of this entry claimed the README no longer pointed
  at `install.sh`; it did, at the top of its own "Worker nodes" section, and that is
  corrected rather than quietly deleted because a wrong reassurance in a security note is
  worse than the gap it was describing.

  Docs: [docs/JOIN.md](docs/JOIN.md) for contributors,
  [docs/OPERATOR_PREFLIGHT.md](docs/OPERATOR_PREFLIGHT.md) for the things no program can
  check on an operator's behalf, THREAT_MODEL §6a for what a contributor is and is not
  exposed to, and DEPLOY for the Caddy configuration TLS now requires.

- **2026-09-04** — The generator is pinnable, and the power curve is a function of the
  noise floor rather than a table computed at one value of it. `config.json` gained
  `temperature` and `seed`; both ship unset, so the request carries neither key and
  nothing about a normal run changed. What a seed establishes is recorded carefully:
  Ollama accepting the field is verified from its API docs and asserted against the
  outbound request body, the runner *honouring* it on this model and hardware is not,
  and a seeded run therefore reports `sampling_pinned: false` until somebody measures
  it. `scripts/eval_power.py` now prints detectable effect across the corpus-size grid
  crossed with ψ ∈ {0.643, 0.5, 0.4, 0.32, 0.25}, defaulting to the measured 0.643 with
  every other cell starred as a projection — because δ ∝ √(ψ/n) means **halving ψ is
  worth exactly what doubling the corpus is worth**, and at ψ = 0.32 the 100 items that
  already exist would detect the 15-point target that needs 187 at the measured floor.
  Two measurements are pre-registered and **neither was run**:
  `docs/experiments/noise-floor-under-pinned-sampling.md` (two identical-configuration
  pairs, pinned against unpinned, 37 items computed from the interval separation
  required, ≈17 h at the direct arm) and
  `docs/experiments/replicate-endpoint-design.md`. The second corrects a claim this
  project published: replicates are **not** more efficient per unit of inference — at
  matched cost, k runs on n items and one run on k×n items have the same power. Their
  real value is that item count, not inference, is the binding constraint: the
  confirmatory 36 are frozen by a digest, one run each gives power 0.29, and k=5 gives
  0.88 on the same items. Which design to choose is decided by the noise-floor
  measurement, and that is written down rather than argued later. Finally, the six
  banded `web_app` items now carry `known_suspect: true` in their own records — the
  §1.1 grading defect recorded on the items it reaches — and the two published `web_app`
  figures that lacked the qualification (README's `web` column, `docs/showcase-ceiling.md`)
  now carry it.
- **2026-09-03** — The eval instrument was given controls, mechanical grading, a banded and
  split corpus, and a computed power analysis. Three things it found are worth more than the
  new corpus. First, **the "about six prompts" figure in §4 was a rule of thumb and optimistic
  by about a factor of two**: it was a significance threshold at the observed churn, not power.
  Computed at 80% power from ψ = 0.643, n=28 sees 38.4 points (10.8 prompts) and n=100 sees 20.6.
  Second, **the HTML execution check was "loads without throwing"**, under which `web-snake`
  passed 5 of 5 committed runs while `showcase_reliability.py` measured the same artifact at
  2 of 10 — every published `web_app` number carries that weakness. Third, and the reason the
  entry above does not claim prompt tuning is unblocked, **no corpus this project will run
  resolves a 15-point change**: it needs 187 items and 182 hours per comparison, so the honest
  report is that growing the corpus buys the ability to see a large change cheaply, not a fine
  one at all. The controls detect a truncated arm (p=0.0005) and a shuffled-pairing arm
  (p=0.0005), find no difference between identical configurations (0 significant in 20 pairs),
  and — the useful measurement — show that the *shuffled* arm is completely invisible to
  parse-and-run grading: 16 of 16 pass, zero discordant pairs. Dropping the model judge from the
  primary endpoint was done on validity grounds and bought no power at all (mean discordant rate
  0.521 → 0.514). The decomposition study is pre-registered, sized, costed at ~39 hours, and
  **not run**; its no-difference criterion is written down before any data exists. Grading is
  deterministic, ungraded is distinguished from failed, run records are append-only, and the
  summariser refuses to compute a statistic over an incomplete study. See
  `docs/eval-methodology.md` and `docs/experiments/ensemble-vs-decomposition.md`.
- **2026-09-03** — Theme 4B propagated W3C trace context across the coordinator/worker
  boundary and into the validator subprocess, off by default, with the OpenTelemetry SDK
  as an optional extra that nothing imports at module scope. Propagation and export are
  separate switches: accepting a coordinator-minted trace ID costs a contributor nothing,
  exporting their machine's spans is telemetry, and the second is off by default and never
  a condition of joining. Span attributes are an allowlist enforced by a keyword-only
  signature rather than by scanning for secrets, which is finding F8's lesson applied with
  the polarity the right way round. Two defects were found by tests asserting their own
  preconditions: an API-only OpenTelemetry install would have reported that it was
  exporting while sending nothing, and a reassigned unit started a second trace so the
  coordinator's view of one job was split in two. It also unblocked the campaign - the
  requester clients were never entered as context managers, so every request tore down its
  own event loop and took the background execution task with it, which is what had made
  `execution_cancelled_while_running` and `provenance_envelope_created` look structurally
  unreachable. Both are floored now, and fixing the honest-worker rule's prerequisites took
  `settlement_accepted` from 2 to 30 at an unchanged budget. No diagnosis-time improvement
  is claimed; the deferred experiment is written down instead. See ADR 0018.
- **2026-09-03** — Theme 3C bound artifacts to a durable provenance envelope and made the
  contribution ledger tamper-evident. The envelope is created when a manifest seals, binds
  identity facts that already existed separately, is canonically hashed so replay resolves to
  the identical record, records absent facts as unknown rather than inferring them, reserves an
  unpopulated signature slot, and ships in audit bundles so a recipient can check the artifact
  hashes offline. The ledger chain rides the settlement transaction rather than weakening it.
  Neither establishes correctness and neither is tamper-proof — an operator with database access
  can rewrite the whole ledger, and a test asserts that this is undetectable rather than
  pretending otherwise. Nothing is signed. See ADR 0017.
- **2026-09-03** — Theme 4A gave the worker protocol a compatibility window, a distinguishable
  refusal, and a written deprecation policy, without bumping anything: the window holds
  exactly version 1 and this defines the mechanism rather than exercising it. It also wrote
  down six extension seams as boundaries with contract tests rather than building a plugin
  framework to host one implementation each — no registry, no dynamic import, no
  backend-selection config, no new indirection. Three boundary violations were found: SQLite
  specifics in routes (fixed), accounting policy inlined in the settlement transaction (partly
  fixed, the rest deferred with a strict xfail reproducer), and a worker-supplied hostname on
  the legacy registration (deferred to the next breaking version). A2A is recorded as an edge
  adapter mapping only, implemented nowhere; the durable ensemble-candidate queue is deferred
  with a measurement trigger. See ADR 0015 and ADR 0016.
- **2026-09-03** — Theme 3B-1 made post-hoc verification evidence durable. It is a separate
  append-only table that references an execution, attempt, and receipt and cannot write back
  to any of them, because ADR 0009 makes terminal state monotonic and verification happens
  after terminal. Scoped like capability evidence plus verifier identity and version, with
  deterministic IDs so replay, restart, and repeated callbacks converge on one row.
  Deterministic checks and agreement have disjoint vocabularies and separate scopes, so
  agreement is never aggregated into a pass rate. No default changed: `verify_rate` is still
  `0.0` and trusted alpha still disables sampled verification. No reputation, no score, no
  ranking; contribution points are untouched. Task-class assurance ladders are deferred to
  Theme 3B-2 with the conditions for re-enabling written down in ADR 0014.
- **2026-09-02** — Theme 1.5 closed the two loose ends Theme 1.4 left. The adversarial
  campaign now asserts a non-vacuous coverage floor over a whole run, which immediately
  showed that finding F4 had not actually been fixed: the campaign was still reaching zero
  accepted settlements, so its settlement, credit, and replay invariants were passing about
  runs in which almost nothing happened. And finding F7 is fixed rather than merely recorded
  — node staleness now reads an elapsed duration from a monotonic source, so correcting the
  coordinator's wall clock no longer mass-reclaims healthy nodes' in-flight work. Lease
  deadlines stay absolute and stay on the wall clock. CI then caught a third thing the
  local runs had not: the campaign's own secret-leak probe was short enough to match by
  coincidence and was failing builds at random — a measurement asserting a finding that was
  not there, which is §2's rule running backwards. Details in
  `docs/adversarial-campaign.md`.
- **2026-09-02** — Theme 1.4 closed the §5 adversarial-tests item. All twelve scenarios now
  have coverage: a property-based state-machine campaign over the attempt, settlement,
  enrollment, and execution-lifecycle machines, plus deterministic tests for the eight that
  were open. Two scenarios are asserted as documented behaviour rather than resistance, which
  is what the project actually claims. Findings in `docs/adversarial-campaign.md`; the
  unflattering one — the campaign passing vacuously before it reached settlement — is written
  up there in full, per §2.
- **2026-08-31** — Added Theme 3A's bounded process boundary for trusted
  parser-heavy built-ins as a narrow prerequisite, while keeping real
  generated-code sandboxing, reliable network denial, behavioral execution,
  containers, microVMs, and WASM explicitly deferred. Corrected the guild
  section so its no-network sandbox is a future requirement rather than a
  description of current enforcement.
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
