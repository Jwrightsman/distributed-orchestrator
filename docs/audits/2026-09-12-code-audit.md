# Mycelium code audit

The reviewed application revision is `418a44d` (September 12, 2026). This is an evidence-backed review of the current private-alpha implementation. It supersedes neither the protocol nor accepted ADRs, and it does not change application behavior.

The full existing suite completed on Windows, Python 3.14.3: **2,490 passed, 41 skipped, 2 xfailed in 974.81 seconds**. Ruff passed before audit files were added. The [test log](2026-09-12-pytest.log), [offline reproduction script](2026-09-12-reproduce.py), and [probe results](2026-09-12-reproductions.json) preserve the evidence. A passing suite does not cover the defects below; the probes exercise cases outside its existing assertions.

The audit inventoried all 412 tracked files, including 221 Python files. Critical runtime, trust, deployment, model integration, and evaluation paths received focused source review. Existing tests cover a much larger surface than the probes. Archived design assets were treated as historical material, and frontend coverage relied primarily on source/contracts and existing tests. This was not a manual inspection of every line, a production penetration test, a fresh model benchmark, or a browser UX review. No contributor was enrolled, no model was downloaded, and no real inference was requested. The only artifact executed by the added probes was a fixed, authored sentinel inside a temporary directory.

## Findings

P1 means fix before broader use of the affected path. P2 means a material correctness, reliability, or measurement defect. Deployment findings describe the shipped configuration and its consequences under stated conditions; they do not claim that the live installation has been exploited.

| ID | Priority | Finding | Evidence |
|---|---|---|---|
| A1 | P1 | Evaluation `--no-exec` still executes generated artifacts | Reproduced with an authored sentinel |
| A2 | P1 | Project IDs escape the project-memory root | Reproduced read and write outside the root |
| A3 | P1 | Caddy access logs retain supported credential headers and path share capabilities | Shipped config plus official Caddy behavior |
| A4 | P1, conditional exposure | Tailscale Caddy template does not restrict the listener to the tailnet | Shipped config plus official Caddy binding semantics |
| A5 | P2 | A failed deliverable can become PASS without another review | Reproduced with unchanged revised text |
| A6 | P2 | One builder failure leaves sibling builders running | Reproduced with controlled async tasks |
| A7 | P2 | DAG generation never receives the structured output contract | Direct comparison of DAG and ensemble adapters |
| A8 | P2 | Study completeness is inferred from observed items | Reproduced acceptance of a one-item prefix |
| A9 | P2 | Study summaries discard replicate identity | Reversing record order changes reported success |
| A10 | P2 | Corpus identity omits expected checks | Changed checks retain the same digest |
| A11 | P2 | Original HTML items do not receive the advertised behavior checks | Corpus and grading dispatch inspection |
| A12 | P2 | Ensemble resampling cannot test candidate independence | Sampling algorithm explicitly constructs independent draws |
| A13 | P2 | Equal wall-clock time is labeled equal compute | Study comparison uses elapsed seconds rather than aggregate work |

### A1. `--no-exec` is bypassed by primary grading

At `evals/run_evals.py:235`, `args.no_exec` skips the legacy `execute_artifacts` call. At line 249, the same function always calls `grading.grade`. `evals/grading.py:580` always includes `check_runs`, and applicable stdout/browser checks can execute artifacts too. The flag is advertised as skipping generated-code execution.

The offline probe calls the real `run_one` with `no_exec=True`, a mocked pipeline, and an authored Python file whose only action is to write a sentinel. The returned legacy outcome is `skipped`; the sentinel nevertheless exists. This requires use of the evaluation harness, not a production worker, and does not contradict the production worker's text-only boundary. It does defeat the evaluator's explicit execution choice. The grader's temporary directory and scrubbed environment are not a host filesystem or network sandbox.

**Repair:** carry a single execution policy through both graders. With execution disabled, leave execution-dependent checks explicitly ungraded; do not convert them into success. Add regression coverage for Python execution, stdout checks, and browser behavior. Static parsing should remain available. The relevant regression must assert that execution entry points are never called, not merely that the output contains `skipped`.

### A2. Canonical project IDs are not confined to `projects/`

`execution/contracts.py:320` checks only whether `project_id` is blank. `memory.py:109`, `:140`, and `:157` directly join caller-supplied IDs to `PROJECTS_DIR`. Canonical DAG execution forwards the ID at `execution/strategies.py:194`; `orchestrator.py:747` onward loads its memory.

The probe constructs `ExecutionRequestV1(project_id='../outside', strategy='dag', ...)`, reads an existing sibling directory's `memory.md`, and modifies its `meta.json` through `add_iteration`. All files in the reproduction are disposable. The exploit requires submission access in a protected deployment and suitable target files readable/writable by the service account. Targets are constrained by fixed filenames and expected metadata structure: this is not arbitrary-file RCE. It is still an unintended read/write outside the project root, and content read as memory can enter model prompts.

**Repair:** enforce a single safe project identifier contract at every entry point and a resolved-root confinement check in the memory storage layer. Reject separators, absolute paths, traversal, and platform-specific path forms; account for symlinks/junctions. Validate in the storage layer even when an API already validates, because CLI and internal callers use it too. Test canonical submissions and memory read/write helpers on Windows and POSIX.

### A3. Proxy logging preserves credentials the application tries to protect

`deploy/Caddyfile.public:70` and `deploy/Caddyfile.tailscale:73` filter only a query parameter named `token`. Share capabilities are path components under `/v1/shares/{token}`, so removing a query parameter does not remove the actual capability. Supported authentication also uses `X-Node-Secret`, `X-Node-Session`, `X-Pitch-Key`, and `X-Viewer-Key`; see `node.py:506` and `access_control.py:153` among other consumers.

Caddy's documented automatic header redactions cover Cookie, Set-Cookie, Authorization, and Proxy-Authorization. The templates do not explicitly redact Mycelium's custom headers. An attacker needs access to logs, log backups, or a logging collector to obtain this exposure; the finding is not unauthenticated access to a log endpoint. The application can store only credential digests while its reverse proxy stores the original incoming credentials. [Caddy logging documentation](https://caddyserver.com/docs/caddyfile/directives/log).

**Repair:** explicitly remove all supported credential headers from access logs and suppress or normalize capability-bearing paths. Verify actual emitted logs with synthetic marker credentials and share URLs for both templates. Configuration-text assertions alone are insufficient. Consider existing log retention and credential/share rotation if an operator confirms the affected configuration has been used.

### A4. A `.ts.net` hostname does not establish a private listener

`deploy/Caddyfile.tailscale:16` says the site name makes Caddy bind only to the tailnet interface. The site block includes neither `bind` nor an equivalent global restriction. Caddy normally binds a wildcard listener; virtual-host matching is a separate mechanism. A client that can reach the listener can present the expected hostname/SNI. [Caddy bind documentation](https://caddyserver.com/docs/caddyfile/directives/bind).

Actual reachability depends on the host firewall, interfaces, and network routing, which this audit did not inspect. Application authentication remains present. The confirmed defect is that the template does not enforce the private exposure boundary it claims.

**Repair:** bind explicitly to the intended tailnet address, document address management, and check IPv4/IPv6 listeners and off-tailnet denial in an isolated deployment test. Preserve the loopback-only application listener. Correct the explanation so operators do not mistake hostname selection for network isolation.

### A5. Review ratings fail open

`orchestrator.py:217` returns `PASS` when it cannot parse a rating. In the revision loop, lines 885–893 extract review-style issue headings from a revised deliverable and promote it to PASS if none exist. The reviser normally returns a deliverable, so absence of an issue heading does not establish that the prior issue was fixed.

The probe supplies a reviewer FAIL, a known defect, and a reviser that returns exactly the same defective text. The pipeline prefix changes the rating to PASS. A reply with no rating also becomes PASS. The production canonical validation/assurance fields are separate, so this does not automatically mean canonical deterministic validation is bypassed. It corrupts the review/revision story, including compatibility metadata and user-visible repair claims.

**Repair:** retain the prior rating until a new review or relevant independent check provides evidence. Represent missing or malformed ratings as unknown. Record attempted revision separately from successful repair. Regression fixtures should include unchanged output, missing rating markers, and a genuinely corrected artifact.

### A6. Builder failure does not clean up parallel siblings

`orchestrator.py:836` uses `asyncio.gather` without sibling cleanup. When one coroutine raises, ordinary `gather` propagation does not cancel its siblings. `execution/service.py:952` marks generic exceptions failed but lacks the dispatcher cancellation present in timeout/cancel branches.

The probe uses two authored async builders: one waits until its sibling starts and then raises; the other blocks on a controlled event. After the pipeline has raised, one sibling remains pending and later completes. This demonstrates the pipeline ownership defect. Continued remote leasing or inference after canonical failure is a consequence to test at the service/dispatcher boundary, not a live-system observation made here.

**Repair:** own and drain all tasks in a builder wave on failure, and terminate outstanding execution units for every terminal failure path. Preserve already accepted contribution truth rather than retroactively erasing valid settlement. A regression should assert no queued/leased sibling remains actionable and no child completion changes the failed execution's publication state.

### A7. Output contracts reach DAG validation but not DAG generation

`execution/strategies.py:189` calls the DAG runner with `request.task` and options, but never supplies `request.output_contract`. The builder path likewise receives the subtask and dependency context. In contrast, ensemble generation explicitly serializes the contract into its prompt at lines 361–365.

A caller can state a required filename, schema, or other generation-relevant constraint only in the structured contract. DAG planning/building/review then has no opportunity to obey it, although later validation can reject the output. The system creates avoidable failure by checking a requirement it never communicated.

**Repair:** compile a bounded, consistent generation brief from the task and output contract for both strategies, carrying relevant constraints through planning and review. Keep secret/reference answers out of model context: public output requirements and private evaluator fixtures are different data. Test a requirement that appears solely in the structured contract.

### A8. A partial study can be called complete

`scripts/eval_study_summary.py:57` builds the expected item set from records already present. It detects an item missing from one observed arm, but cannot detect an item missing from every arm. The function has no planned-item/replicate manifest argument. Two completed records for one item are accepted even when the intended study has many more items.

**Repair:** persist a study manifest before execution: selected item IDs, arms, replicate IDs, corpus/checker identities, and budget policy. Require equality between planned and completed cells. Treat intentional aborts and operational failures explicitly. A regression should truncate every arm at the same point and verify that no confirmatory summary is emitted.

### A9. Replicate outcomes depend on log order

`runrecord.latest_per_key` retains `(item, arm, replicate)`, but `scripts/eval_study_summary.py:83` returns a dictionary keyed only by item ID. Multiple replicates overwrite one another. Reversing two opposite-outcome records changes the reported outcome from false to true in the probe. Cost reporting counts the replicate rows, so outcome and cost summaries can describe different units.

**Repair:** either reject multi-replicate studies until supported or implement their preregistered endpoint with explicit within-item aggregation and appropriate inference. Do not treat all repeats as independent new task draws. Preserve order invariance except for documented supersession of the same logical run key.

### A10. A grading change can preserve corpus identity

`evals/corpus.py:128` hashes only item ID and task text. Expected checks, schemas, and fixture contents affect the measurement but are omitted. The probe changes expected checks and observes the same digest. A global grader version does not detect edited per-item expectations.

**Repair:** define a versioned measurement identity covering prompt text, expectations, referenced fixture/schema content hashes, and grader version. Keep split membership identity separate. Historical records need an explicit older identity version, not silent reinterpretation.

### A11. Strict HTML behavior checks are not applied to all legacy items

All 28 original entries in `evals/prompts.json` lack `expect.checks`. In particular, `web-snake` asks only for HTML and keywords `canvas`, `addeventlistener`, and `score`. `evals/grading.py:583` invokes `html_behaviour` only when explicitly listed. The primary endpoint's base `check_runs` remains distinct from playable behavior.

The documentation presents the stricter HTML checker as the primary endpoint's distinguishing feature, but the original corpus does not uniformly use it. A static or noninteractive artifact can satisfy a weaker check without fulfilling the task. Merely naming the field `primary_pass` does not strengthen its rubric.

**Repair:** give each relevant item explicit behavior assertions, plus positive and deliberately broken fixtures that establish what the checker detects. Change the measurement identity and start a new comparable series. Do not retroactively relabel historical success as behavior-verified success without the artifacts and actual regrading.

### A12. The empirical ensemble estimate assumes the independence it claims to test

`scripts/ensemble_experiment.py:57` estimates best-of-N success by independently sampling Boolean trial outcomes with replacement. Its docstring says disagreement with the closed form can reveal non-independence. Each `rng.choice` at line 70 creates an independent draw from the empirical marginal distribution, so the simulated probability converges to `1 - (1 - p_hat)^N`. Any gap is Monte Carlo noise, not evidence about original within-group dependence.

**Repair:** label this as an independence-based illustration. To measure real ensemble benefit, retain actual candidate groups, grouping by task and execution, record selected-winner correctness, and measure correlated errors within those groups. Bootstrap intact task/group units when that matches the planned estimand. Include a perfectly correlated synthetic control: all candidates in a group succeed or fail together, so best-of-N has no gain.

### A13. Wall-clock equality is presented as compute equality

`scripts/eval_study_summary.py` compares the two arms' `seconds_total` values and prints that this is the equal-compute comparison when the ratio is within 25%. Those values sum elapsed run times, not aggregate worker inference time. A five-worker ensemble and a one-worker direct run can have the same latency with very different compute consumption. `cost_for` also sums whatever times are present even when `seconds_missing` is nonzero, so partial cost capture can still feed the positive claim.

**Repair:** label the current metric elapsed generation time. Require complete per-unit cost records and a declared hardware/cost policy before asserting comparable compute. Report wall-clock latency, aggregate worker-seconds, tokens, and optional energy separately; token count alone is not hardware-independent compute either. Add synthetic controls for parallel arms and missing timing records. This matters especially when the runner becomes strategy-aware.

## Important limitations and engineering priorities

The canonical architecture is considerably stronger than the introductory AGENTS description. Strategy and placement are distinct; direct and ensemble already exist; durable execution and attempt settlement, enrollment revocation, artifact sealing, privacy defaults, and validator subprocess boundaries are implemented. Recommendations to rebuild these from scratch would be stale. The August architecture audit is historical, and much of its early hardening agenda has shipped.

The queue remains process-local, and interrupted work is reconciled rather than resumed. This is an explicit alpha limitation, not hidden data durability. Durable records, durable scheduling, and exactly-once external side effects are three different properties. A future durable queue should begin with independent direct/ensemble units if measured restart loss justifies it; general DAG replay is a larger commitment.

Capability descriptors are claims. Shadow evidence remains separate from active routing and correctness, which is appropriate. Contribution points record accepted compute, not useful answers or unforgeable machine identity. Making points spendable would change the abuse and accounting problem substantially.

The model wrapper returns text and discards useful inference timing/token counters (`ollama_client.py:217`). The eval run records currently write `tokens=None` (`evals/run_evals.py:530`). The generic runner calls the legacy DAG directly and records `strategy='dag'`; changing `--arm` names does not implement an architecture experiment. A strategy-aware experiment adapter and actual inference-cost capture are necessary before claiming improvement at equal compute.

`auto_detect_model` ranks by family substring and input order, not measured task performance or memory fit. A machine with several Qwen3.5 variants may select a model unsuitable for its capacity. Treat the ladder as a convenience fallback, not a benchmark-backed best-model policy. Pin the default profile during experiments.

Large modules concentrate change risk: `capability_evidence.py`, `server_state.py`, `execution/attempts.py`, and `execution/service.py` carry substantial protocol state. Separate well-defined pure policy from persistence and publication as defects or feature work touch those boundaries; a blanket module-splitting rewrite would add little immediate user value. Dependency requirements use broad lower bounds, and CI tests Linux plus selected macOS worker paths; a supported Windows CI job and reproducible dependency resolution would reduce environment drift.

Documentation is internally inconsistent in consequential places. README line 525 says the legacy judge gate is at least 3/5; current code/docs use at least 4. AGENTS says no cloud inference and browser execution as if universal, while current production supports optional providers and parse-focused validation. MASTER_PLAN contains an HTTP Tailscale join example despite the non-loopback HTTPS requirement. Its claim that distribution is the only bottleneck should be revised in light of the unresolved outcome-quality evidence. Preserve historical numbers, but label their dates, endpoint, and scope.

## Recommended repair sequence

1. Fix A1–A4, with tests exercising real boundaries rather than matching configuration text. These affect explicit execution choice, filesystem confinement, credentials, and network exposure.
2. Fix A5–A7 before interpreting repair rates or collecting a strategy comparison.
3. Fix A8–A13 and capture full experimental identity and cost before spending days on fresh inference.
4. Run a small real end-to-end slice through canonical strategies on owner-approved hardware, then the planned comparison described in the accompanying research report.
5. Investigate durable scheduling, advanced routing, and additional isolation only when a named alpha workload produces evidence that the missing property is the limiting factor.

These are proposed changes, not implemented fixes. The audit added reports, source references, and offline evidence only.
