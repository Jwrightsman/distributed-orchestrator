# Mycelium feasibility and research assessment

**Mycelium is worth continuing as a small, private system for verifiable local-AI work across trusted computers. Its strongest assets are execution accountability, explicit trust boundaries, and the ability to compare orchestration strategies. Its central unproven claim is that distributing a task creates enough additional useful output to justify coordination, verification, and operator effort.**

The project has advanced well beyond a planner demo. Its infrastructure is ahead of its evidence for user value. The next stage should establish a workload where it wins repeatedly against a strong single-machine baseline. A public, anonymous compute network or broad autonomous software factory would be a much larger and currently unsupported proposition.

This assessment uses application revision `418a44d`, checked-in evaluation records, and primary external sources checked on September 12, 2026. It is accompanied by the [code audit](2026-09-12-code-audit.md), [reproduction evidence](2026-09-12-reproductions.json), [source inventory with immutable commits](2026-09-12-source-index.json), and [next-session prompt](2026-09-12-next-session.md).

## The project today

There is substantial engineering here. Canonical requests distinguish strategy from placement. Durable execution records and accepted worker attempts preserve authoritative state before publication. Enrollment, process sessions, and attempts have distinct identities; per-node revocation exists. Artifact access, sealed manifests, explicit shares, privacy defaults, and bounded validator subprocesses address real failure modes. The full local regression suite returned **2,490 passing tests**, with 41 skips and two expected failures.

The implementation also distinguishes lifecycle completion, validation outcome, assurance, and artifact integrity. That distinction is valuable: a job can finish without its output being correct, and a file can retain its original bytes without those bytes implementing the right behavior. This is a good foundation for credible results and future integrations.

Several limitations are deliberately documented: one coordinator owns process-local scheduling; restarts interrupt rather than resume active work; worker capabilities are self-reported; shadow evidence does not control routing; production validators do not execute generated programs; contributor points do not establish correctness or Sybil resistance. These boundaries fit a trusted alpha.

The audit found thirteen actionable defects, including a bypass of evaluation `--no-exec`, unconstrained project-memory paths, insufficient reverse-proxy credential redaction, an incorrect tailnet binding assumption, false-positive review ratings, orphan sibling builders, lost DAG output constraints, and experimental bookkeeping errors. Six offline probe groups reproduce the relevant runtime and data-processing failures. The passing tests are useful evidence of existing contracts, not a release certificate for untested cases. Details, prerequisites, and repairs are in the [code audit](2026-09-12-code-audit.md).

My assessment by claim:

| Claim | Assessment | What would change the assessment |
|---|---|---|
| Several owner-approved computers can perform independent local-model work | Established engineering mechanism | Sustained deployment would improve operational confidence |
| A trusted group can use Mycelium as an auditable batch service | Feasible; good near-term direction | Repeated use of one concrete workload with measured operator cost |
| Decomposition reliably improves small-model coding | Unproven; some local evidence points toward complete candidates for coupled tasks | Paired comparison against direct generation and direct-with-repair |
| More nodes improve throughput | Plausible for independent jobs | Two/three-node measured accepted-results/hour versus one node |
| More nodes improve a single answer's quality | Task- and verifier-dependent | Actual grouped ensemble selection, including false accepts and correlated failures |
| Quantization makes the current network broadly capable | Helpful capacity trend; insufficient by itself | Measured quality/cost frontier on the target hardware |
| An anonymous volunteer network can safely provide reliable useful work | Not supported by the present trust model | Separate admission, abuse, verification, privacy, and incentive evidence |
| The project has a defensible product advantage | Potential, not established | Repeat requesters choosing it over a simpler alternative |

## What the existing measurements establish

The five committed 28-item runs contain 140 item-runs. Recounting their current JSONL success fields gives 10, 17, 11, 16, and 15 successes. These correspond to the baseline, v3, v4, v5, and repeated v3 prompt sets. None of those historical rows contains the newer `primary_pass` field. They are historical legacy-endpoint evidence. [Local evaluation records](../../evals/results/).

The two v3 runs do reproduce the headline arithmetic: 17/28 and 15/28, or 32/56 pooled successes (57.1%). Their paired outcomes are more informative:

| First v3 run / repeated v3 run | Pass | Fail |
|---|---:|---:|
| Pass | 7 | 10 |
| Fail | 8 | 3 |

Eighteen of 28 items changed outcome. Only seven passed both times, while 25 passed at least once. This suggests substantial instability and a potentially useful candidate-selection opportunity. It does **not** demonstrate a deployable 25/28 success rate: an oracle looking at completed trials is different from a verifier selecting the right result in advance. These are also repeated observations of the same development tasks, not 56 independent draws from the universe of user requests.

The original browser criterion was weaker than playability. The repository documents a 2/10 playable Snake result and a 10/10 chart result, while the older broad-suite HTML test passed Snake in all five recorded runs. Those results concern different task wording/pipelines/checks and should not be blended into one capability percentage. The new corpus has 100 items and 36 confirmatory items, but the 28 legacy entries still lack explicit behavioral checks. A named primary endpoint remains only as strong as its item rubric. [Evaluation methodology](../eval-methodology.md), [grader](../../evals/grading.py), [corpus](../../evals/prompts.json).

There is a more interesting architectural lead in `scripts/ensemble_results/pooled.json`: 12/22 independent complete-artifact candidates pass, compared with the historical 2/10 decomposition baseline; its recorded one-sided Fisher p-value is about 0.073. This is a small, historical, non-budget-matched comparison. It is useful motivation for another experiment, not proof that deployed ensemble selection solves Snake. The experiment's resampling routine independently samples Boolean outcomes and therefore cannot establish that real candidate failures are independent. [Experiment and raw material](../../scripts/ensemble_experiment.py).

The mean recorded generation time across all 140 item-runs is 1,755 seconds, or 29.25 minutes; the median is 1,200 seconds, or 20 minutes. These measurements come from historical DAG runs on the recorded setup. Extrapolating that mean to 100 items gives about 48.75 hours for one arm. A large comparison can consume days before establishing much. This makes the integrity of the grader, study manifest, cost capture, and stopping rule a prerequisite for further inference spending.

Two conclusions follow. First, “a 4B model has a ceiling” is too broad an explanation for all failures: strategy, context handling, reviewer behavior, and grading defects are also involved. Second, enlarging the corpus alone does not fix a biased or incomplete measurement. The current work on sampling/noise is valuable, but it should run on a repaired instrument with a real single-model control.

## Small models and TurboQuant

Several different improvements are often bundled together as model miniaturization. They have different effects:

| Improvement | What it changes | Relevance to Mycelium |
|---|---|---|
| Better small-model training and distillation | Capability available at a given parameter/compute budget | Can turn narrow tasks into reliable worker jobs |
| Weight quantization | Memory and bandwidth for the learned parameters | Makes a model fit; may change task accuracy |
| KV-cache quantization | Memory for attention state as context/concurrency grows | Can allow longer contexts or more simultaneous requests |
| Hybrid/recurrent attention | How much state grows with context | Already relevant to Qwen3.5; changes cache-saving estimates |
| Speculative decoding / multi-token prediction | Token generation throughput with compatible implementation | May reduce latency; must measure overhead and acceptance |
| Mixture of experts | Active compute per token versus total parameter storage | A small active-parameter count does not imply small total RAM |

The NVIDIA-authored paper *Small Language Models are the Future of Agentic AI* argues that repeated specialized agent tasks suit small models and that heterogeneous systems can retain larger models where needed. It is a position paper, not evidence that every orchestration pipeline benefits from more agents. Its useful implication here is to specialize the workload before expanding the network. [Belcak et al., 2025](https://arxiv.org/abs/2506.02153).

TurboQuant is real research in online vector quantization. The paper reports near-neutral downstream quality at approximately 3.5 bits per channel in its KV-cache experiments, with some degradation at lower precision. This is compression of attention-related vectors, not a demonstrated conversion of a small model into a larger model's reasoning capability. [Zandieh et al., *TurboQuant*, 2025/ICLR 2026](https://arxiv.org/abs/2504.19874).

Google's March 2026 post reports at least 6× KV-memory reduction in selected experiments. Its “up to 8×” result concerns attention-logit computation using 4-bit TurboQuant versus 32-bit unquantized keys on H100 hardware. It is not an 8× end-to-end CPU speed measurement for Mycelium, and the blog's broad language about model size should be read in that narrower experimental context. [Google Research](https://research.google/blog/turboquant-redefining-ai-efficiency-with-extreme-compression/).

### The estimate specific to the current default model

Qwen3.5-4B's published configuration has 32 layers, with full attention every fourth layer: eight full-attention layers and 24 linear-attention layers. Full attention uses four KV heads of dimension 256. The local wrapper defaults to 8,192 context tokens. [Qwen configuration](https://huggingface.co/Qwen/Qwen3.5-4B/raw/main/config.json), [local model wrapper](../../ollama_client.py).

For a single sequence, a simplified full-attention KV estimate is:

`KV bytes = 2 × full-attention layers × KV heads × head dimension × context tokens × bytes/value`

At FP16, this gives 32,768 bytes per token. The following are calculations, **not measured resident-memory results**:

| Context | FP16 full-attention KV | Ideal 4-bit equivalent | Ideal 3.5-bit equivalent |
|---|---:|---:|---:|
| 8,192 | 256 MiB | 64 MiB | 56 MiB |
| 32,768 | 1 GiB | 256 MiB | 224 MiB |
| 131,072 | 4 GiB | 1 GiB | 896 MiB |

These values omit recurrent state, vision components, quantization metadata/alignment, scratch buffers, batching, implementation choices, and the model weights. At the current 8K default, even ideal 3.5-bit cache compression saves about **200 MiB** of this component. That is useful on a constrained machine but is far smaller than shrinking the entire model footprint sixfold. Long-context and concurrent workloads are where the potential becomes more substantial. Doubling sequence concurrency can also multiply cache allocation; exact behavior needs measurement on the selected backend.

For perspective, 4 billion weights at an ideal four bits each require about 2 GB in decimal units before overhead. Nine billion require about 4.5 GB. A 35B MoE model with only 3B active parameters still has roughly 35B parameters to store or offload; four-bit weight arithmetic alone is about 17.5 GB. These are storage calculations, not model recommendations or measured requirements.

The general relationship is useful: if KV cache is fraction `f` of total memory and cache compression is `c`, total-memory reduction factor is `1 / ((1 - f) + f/c)`. At `f=0.1` and `c=6`, total memory improves by about 1.09×. At `f=0.6`, it improves by 2×. Measuring the memory breakdown should precede choosing the optimization.

### Runtime availability matters

Ollama's current documentation exposes `f16`, `q8_0`, and `q4_0` KV-cache types; quantized cache depends on Flash Attention support. It describes q8 as roughly half the FP16 cache memory and q4 as roughly a quarter, with model/task-dependent precision effects. The choice is global to the server. This is the practical documented baseline to compare first. [Ollama FAQ](https://docs.ollama.com/faq).

Two inspected upstream llama.cpp TurboQuant PRs, #21089 and #21131, were closed without merging; the Ollama TurboQuant issue #15051 remained open at inspection. This does not establish that every upstream implementation avenue is absent. It establishes that the reviewed proposals are not evidence of shipped support. TheTom's TurboQuant fork is a concrete experimental codebase, with a default-branch commit dated September 6, 2026 in the retrieved snapshot. Treat it as a separately pinned runtime experiment, including compatibility and quality testing. [CPU PR](https://github.com/ggml-org/llama.cpp/pull/21089), [second PR](https://github.com/ggml-org/llama.cpp/pull/21131), [Ollama issue](https://github.com/ollama/ollama/issues/15051), [fork](https://github.com/TheTom/llama-cpp-turboquant).

The right experiment holds model weights, task, prompt, context, hardware, generation budget, and evaluation fixed while changing cache implementation. Report prefill, decode, peak resident memory, actual GPU offload, and accepted-result quality. Changing model size, precision, and strategy simultaneously would make a win uninterpretable.

### A tailwind with a strategic consequence

Better small models enlarge the population of computers able to contribute. They also improve the single-machine alternative. If tomorrow's laptop can answer the whole request well, it has less reason to split that request among other laptops. Mycelium benefits most where users have **more independent work than one machine can finish**, where several independent proposals can be cheaply checked, or where a trusted group owns different specialist hardware/models. The product must deliver value that survives a better local baseline.

## When distribution helps

Task distribution is a sensible layer for ordinary networks because a worker receives a prompt and returns an artifact-sized response. Model sharding can require communication throughout token generation and has a different latency/bandwidth profile. Exo's current emphasis on topology-aware placement and high-bandwidth Apple clusters illustrates that distinction. Petals uses distributed model blocks for collaborative inference; it is not the same decomposition algorithm. [Exo](https://github.com/exo-explore/exo), [Petals](https://github.com/bigscience-workshop/petals).

For independent batches, useful capacity can approximately add across workers, subject to eligibility, availability, coordinator capacity, and verification. For one DAG request, its dependency chain and coordinator stages remain serial. A simple upper-bound model is `speedup = 1 / (s + (1-s)/n)`, where `s` is the serial fraction and `n` is equal-speed workers, before communication and straggler overhead. With `s=0.4`, four workers yield only 1.82× and infinitely many workers approach 2.5×. These are illustrative assumptions; Mycelium needs per-stage measurements to estimate its own `s`.

The reviewer currently assembles and re-emits a complete deliverable on the coordinator. More workers can produce more text that the same reviewer must consume. If the fragments have inconsistent names or interfaces, aggregation becomes another generative programming task. Longer context may preserve fragments, but does not guarantee that the reviewer integrates them correctly. This is a plausible mechanism behind coupled-artifact failures, not a demonstrated attribution for every failure.

Controlled agent-scaling research supports making architecture conditional on the task: one study tested 180 configurations and found improvements on parallelizable tasks alongside substantial degradation on sequential planning tasks. Its measured percentages and capability thresholds come from its own models and benchmarks; they are not constants to transplant into Mycelium. The transferable lesson is to benchmark direct, sequential-repair, and coordinated variants under comparable budgets. [Kim et al., 2025](https://arxiv.org/abs/2512.08296).

Ensembles can help when candidate errors differ and a verifier can recognize useful answers. With independent candidates of success probability `p`, an oracle's best-of-N success is `1-(1-p)^N`. At `p=0.2`, five candidates reach only 67.2%; at `p=0.55`, five reach 98.2%. Those illustrative numbers assume independence and perfect selection, neither established here. Perfectly correlated candidates gain nothing. A weak verifier can create additional opportunities for false acceptance: five incorrect candidates with independent 1% false-accept probability give a 4.9% chance that at least one is mistakenly accepted.

The decision metric should be **correctly accepted results per total hardware-hour**, with wall-clock latency and requester rework reported alongside it. A five-node answer in one minute consumed about five node-minutes of inference, plus orchestration and checking. Lower latency and lower total compute are different wins. Owner-donated electricity and attention are still costs even when no API invoice exists.

## Comparable codebases and what to borrow

Eleven repository snapshots were checked for current default branch, commit date, archive flag, and root license metadata. Seven specific files across five projects were retrieved for source-level inspection. The [source index](2026-09-12-source-index.json) records commit SHAs, paths, file hashes, and inspected symbols. Repository license metadata is a starting point; check the exact file and dependency notices before copying. No external project was installed or executed.

| Project | Relationship and useful material | Suggested use | Root license metadata |
|---|---|---|---|
| [LocalAI](https://github.com/mudler/LocalAI) | Whole-request federation, backend abstraction, distributed inference | Highest-priority practical comparison; inspect federation load selection and proxy boundaries | MIT |
| [LocalAGI](https://github.com/mudler/LocalAGI) | Local agent orchestration, job handling, explicit pause/cancel and model integration | Compare the minimum requester experience and cancellation ownership | MIT |
| [BOINC](https://github.com/BOINC/boinc) | Volunteer scheduling, result validation, adaptive replication, contribution accounting | Learn validation and operator-incentive patterns; do not copy the entire client/server architecture | LGPL-3.0 |
| [LangGraph](https://github.com/langchain-ai/langgraph) | Durable graph state, task execution, retry and commit boundaries | Study task ownership and persistence semantics; no framework rewrite required | MIT |
| [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | Small agent loop with distinct model and environment interfaces | Strong simple coding baseline and trajectory format reference | MIT |
| [smolagents](https://github.com/huggingface/smolagents) | Compact agent framework with multiple execution-environment integrations | Learn interface separation; generated-code tools would change Mycelium's worker trust model | Apache-2.0 |
| [Exo](https://github.com/exo-explore/exo) | Clustered model inference and topology-aware placement | Possible future stronger-model backend in an owner-controlled cluster | Apache-2.0 |
| [Petals](https://github.com/bigscience-workshop/petals) | Collaborative model-block inference | Architectural reference for failure/network tradeoffs | MIT |
| [Hivemind](https://github.com/learning-at-home/hivemind) | Decentralized learning and peer discovery infrastructure | Research reference if independent federation becomes a demonstrated need | MIT |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | Inference engine, hardware backends, quantization, RPC | Benchmark engine behavior and expose a narrow provider interface | MIT |
| [TheTom TurboQuant fork](https://github.com/TheTom/llama-cpp-turboquant) | Experimental cache implementation | Optional isolated runtime arm after stable-runtime measurements | MIT |

**LocalAI deserves particular attention.** Its official documentation distinguishes federation, where an entire request is routed to one worker, from model sharding. Its current documentation also describes a PostgreSQL/NATS distributed mode. That makes it a direct comparator for the infrastructure underneath a local-AI workload service. Mycelium should justify the additional strategy/verification layer against this simpler whole-request-routing option. [LocalAI distributed modes](https://localai.io/docs/features/distribute/index.print.html).

The most concrete source-reading route is:

1. `LocalAI/core/p2p/federated.go`: `SelectLeastUsedServer`, request accounting, and membership reconciliation. Compare scheduling complexity to the actual alpha workload.
2. `LocalAI/core/p2p/federated_server.go`: cancellation and proxy boundaries. Treat this as a protocol comparison, not an instruction to replace HTTPS polling with P2P.
3. `BOINC/sched/validator.cpp` and `sched/credit.cpp`: separate validation decisions from contribution accounting. Its application-specific replication policies are a more useful inspiration than a universal reputation score.
4. `langgraph/libs/langgraph/langgraph/pregel/_runner.py`: ownership of concurrent tasks, retry, and commit processing. Its persistence documentation distinguishes graph checkpoints from application memory. [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence).
5. `mini-swe-agent/src/minisweagent/agents/default.py`: the inspected file is 190 lines, with separate `run`, `step`, `query`, `execute_actions`, and serialization functions. Use it to challenge whether orchestration complexity is buying outcome quality. Its headline benchmark scores must not be attributed to a 4B local model. [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent).

BOINC's adaptive replication reduces checking for host/application combinations with a record of validated work and permits suspicious results to trigger another computation. This is useful as a pattern for task-scoped evidence and selective verification. Two language models agreeing on an incorrect answer do not become correct merely because the same scheduling idea worked for a scientific computation. [BOINC adaptive replication](https://github.com/BOINC/boinc/wiki/Adaptive-Replication).

Petals' default branch snapshot was dated August 2024 and Hivemind's January 2026. Neither was marked archived, but those dates matter when considering modern-model support. A current-looking README or a large audience is insufficient evidence of maintained compatibility. Pin a commit and test the exact integration before adoption.

“Scraping” should mean targeted source reuse with provenance: choose a behavior, inspect its implementation and tests at a fixed commit, record the license, and build the smallest compatible adaptation. Copying a complete framework would import another project's assumptions about authentication, execution, durability, and deployment. A three-function design lesson may be more valuable than another dependency.

## Novelty and positioning

The README and MASTER_PLAN lean on SwarmHarness to argue that an open lane has been academically validated. The paper proposes decentralized discovery, routing, and spendable compute credits; Mycelium currently uses a coordinator and non-spendable contribution points. Its proposal is related, but its existence does not prove product demand, safety, incentive compatibility, or implementation superiority. [Jose, *SwarmHarness*, May 2026](https://arxiv.org/html/2605.28764v1).

The paper's broad portrayal of BOINC as lacking a credit incentive is also poor support for novelty: BOINC has longstanding credit machinery, visible directly in its validator and credit source. Spendable exchange credits differ from contribution recognition, but that distinction should be explicit. Mycelium should make a narrow, demonstrable promise rather than depend on a claim that nobody else combines these ideas.

A credible current description is: **“Run auditable local-AI jobs across computers your group trusts, with explicit checks and a record of who produced each result.”** The potential advantage is reliable intake, bounded work, useful validation, and transparent outcomes on heterogeneous local hardware. Token-free participation is a design choice that can simplify the experience; it is not by itself a reason for a requester to use the system.

The largest product risk is recruiting supply before finding repeat demand. Contributors need a reason to keep their machines available, but requesters need something worth waiting for. A chart that generates correctly demonstrates the pipeline; it does not establish why someone needs a network to make the chart. A useful alpha task should recur, be divisible, and have an inexpensive correctness check.

## The best near-term workload

My first candidate would be **batch extraction or transformation over public or explicitly shareable documents, returning schema-constrained records with source spans**. Work units can remain self-contained. Records can be checked for schema, required fields, exact identifiers, source-span validity, arithmetic, and known-answer accuracy on a gold subset. Some semantic claims still need human review; a valid schema alone is insufficient.

A second candidate is **small code transformations or test generation against existing deterministic fixtures**, where the requesting operator provides an isolated execution environment. Workers continue returning text. This is closer to the project's current artifacts but depends on repairing execution policy and developing a real behavioral verifier. Never convert contributor nodes into arbitrary code executors as a shortcut.

A third is **batch model evaluation or comparison** for a group that already owns mixed hardware. The result is a measured report, not a claim that a swarm is smarter. This could turn the project's unusually detailed provenance into immediate utility, but it needs supported experiment routing and cost capture first.

I would defer broad web research, arbitrary application generation, autonomous repository modification, and a general marketplace. Their truth conditions, tool permissions, data movement, or execution requirements are much wider. The existing API and strategy abstractions can support later expansion if a narrow workload earns it.

## A bounded plan that can decide whether to continue

**Stage 1 — repair safety and measurement, roughly one focused engineering cycle.** Fix A1–A4 first, then review/cancellation/contract propagation, then study identity/completeness and grading. Preserve the current architecture. Add actual token/timing capture and record unavailable measurements as unknown. The deliverable is a trustworthy small experiment, not another public feature.

**Stage 2 — a development pilot.** Choose 12–20 development items from one narrow workload, with correct and incorrect fixtures for each important check. Compare three arms: direct generation; direct generation plus one verifier-informed repair within the same total budget; and the existing DAG. If independent complete candidates look promising, add a separately budgeted ensemble arm. Test on already owner-approved hardware; no autonomous enrollment or model downloads are part of this audit.

Freeze model digest, runtime version, quantization, context, task/checker/fixture identity, sampling parameters, total output-token budget, and retry policy. Interleave arms and record warm versus cold behavior. Measure the actual selected result, all candidate costs, and human acceptance after a blinded review. Pilot outcomes select a direction; they do not justify a broad significance claim.

**Stage 3 — separate quality scaling from hardware scaling.** Hold the winning strategy and task batch fixed, then compare one worker with two and three. Record p50/p95 latency, accepted results per hour, total worker-seconds, verifier time, retries, interrupted work, and requester corrections. Include one controlled worker loss and coordinator restart in the owner-controlled test environment. Report loss/recovery behavior plainly.

**Stage 4 — a runtime/cache comparison.** Only after measuring the memory bottleneck, compare documented FP16/q8/q4 cache settings on supported hardware. A TurboQuant fork can be an additional pinned arm. A 4B-versus-9B comparison is separate because it changes model capacity. Test identical prompts at 8K and a workload-relevant longer context; do not fill a 128K context merely to manufacture a cache-saving result.

**Stage 5 — held-out confirmation and five actual requesters.** Repair the study summarizer first, preregister the endpoint and full manifest, and choose the confirmatory sample size using the pilot's uncertainty and a practical effect threshold. The current 36 held-out items should not be spent repeatedly during tuning. If they cannot resolve the claimed effect, report uncertainty or narrow the claim instead of silently changing the test.

The following are suggested product gates, not measured achievements or universal standards:

| Gate | Proposed decision rule |
|---|---|
| Safety | Execution refusal, project confinement, credential-log hygiene, and private listener tests pass |
| Quality | At least 90% human-accepted output on the chosen low-impact workload; report the interval and false accepts |
| Added value | At least 1.5× accepted batch throughput on two workers, or a meaningful quality improvement over direct-with-repair at comparable measured cost |
| Verification | No known broken sentinel artifact is accepted; blind review separately estimates misses |
| Operations | Work loss/cancellation/restart behavior is measured; operator repair time is recorded |
| Demand | Five requesters try real work and at least three choose to return without prompting |
| Supply | Three independent consenting owners remain willing to contribute after several weeks of actual load |

If direct-with-repair wins on coupled code, use it. If distribution improves batch throughput but not answer quality, position the product around throughput. If verification consumes the saved effort, narrow the task. If requesters do not return after one deliberate repositioning, preserve the research and reduce maintenance rather than expanding the architecture to avoid the result.

## What I would do next

I would keep the project and sharply constrain the next month. First repair the identified boundary and measurement defects. Then demonstrate one recurring task where two ordinary computers produce more **accepted useful work** than one, with every retry and rejected result counted. Publish the experiment and an honest demo together, using the existing private-alpha posture.

I would postpone more reputation machinery, active capability ranking, decentralized discovery, general workflow replay, marketplace features, and a framework migration. The current protocol already provides enough structure to answer the central question. The most valuable next contribution is evidence that a user would miss the system if it disappeared.

The optimistic case is a small group pooling otherwise underused machines for tasks that are cheap to check and expensive to perform repeatedly. The pessimistic case is a sophisticated coordinator that generates extra tokens and bookkeeping around an answer one local model could have produced more simply. The existing code is strong enough to run the experiment that distinguishes those cases. That is why continuing is justified—and why the next milestone should be a result rather than a larger architecture.

## Source notes

Primary external sources used are linked beside the claims they support: the TurboQuant paper and Google Research post; Qwen's model configuration; Ollama's FAQ and feature issue; upstream llama.cpp proposals; Kim et al.'s controlled agent study; Belcak et al.'s position paper; Jose's SwarmHarness proposal; official LocalAI and LangGraph documentation; BOINC documentation and source; and the eleven repositories in the source index. Publication dates are distinguished from repository retrieval dates. Repository default branches can move; the accompanying JSON records the inspected immutable commits.

Internal sources include current protocol/architecture/threat-model documentation, application source and tests, the historical architecture audit, all five committed broad-suite result logs, ensemble candidate results, the expanded corpus, and sampling/evaluation methodology. Fresh model capability, real-node speedup, electricity use, live deployment exposure, and repeat-user demand remain unmeasured in this audit. None of the calculated memory, ensemble, speedup, or gate examples should be quoted as an observed Mycelium result.
