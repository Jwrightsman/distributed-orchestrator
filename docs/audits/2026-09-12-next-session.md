# Next-session prompt

```text
Work in C:\Users\wrigh\distributed-orchestrator on Mycelium. Implement a bounded remediation of the September 12 audit. I authorize reversible repository edits and safe local tests for the issues below. Do not install/join/run a real contributor or coordinator, download models, execute untrusted generated artifacts, deploy, or contact other people. Keep production inference and enrollment out of this session.

Read AGENTS.md, MASTER_PLAN.md, current protocol/threat-model documents, and:
- docs/audits/2026-09-12-code-audit.md
- docs/audits/2026-09-12-feasibility-and-research.md
- docs/audits/2026-09-12-reproduce.py
- docs/audits/2026-09-12-reproductions.json

The audited revision was 418a44d. Recheck current code and git status first; preserve unrelated changes. The full suite then passed 2,490 tests with 41 skips and 2 xfails, so green tests alone do not refute the audit. Reproduce each issue against the current revision before changing it. The audit script deliberately records defective behavior; convert its cases into proper regression tests instead of treating its current output as desired behavior.

Prioritize these safety boundaries:
1. A1: Make --no-exec suppress all execution in legacy and primary grading, including Python/stdout/browser checks. Keep those checks explicitly ungraded; static checks can still run.
2. A2: Constrain project IDs and all project-memory reads/writes to the intended root, covering canonical, legacy, CLI/internal callers, Windows/POSIX paths, traversal, absolute paths, and symlink/junction escapes. Preserve valid existing project IDs.
3. A3: Make both Caddy templates remove every supported credential header and capability-bearing share path from access logs. Test synthetic emitted log records or the real isolated log pipeline when available; matching config text alone does not establish redaction.
4. A4: Make the private Tailscale template explicitly enforce its intended listener interface. Correct the hostname-versus-bind explanation and cover IPv4/IPv6/off-tailnet behavior as far as an isolated local environment permits. Do not change any deployed proxy or firewall.

Then repair runtime truth:
5. A5: A missing/malformed review rating must not default to PASS. An unchanged revision without an Issues Found heading must not clear a failed rating. Separate revision attempted from independently established repair success and preserve canonical assurance semantics.
6. A6: Own, cancel, and await sibling builder work when one builder fails. Ensure all terminal failure paths revoke outstanding dispatcher work without undoing already durable accepted contribution records. Add controlled async and service/attempt tests for no work remaining actionable after failure.
7. A7: Communicate public structured output-contract requirements to DAG planning/building/review consistently with ensemble. Preserve bounded prompts and keep private evaluator answers out of model context.

Then repair measurement before new model runs:
8. A8/A9: Add an explicit study manifest for expected items, arms, and replicates; reject missing planned cells. Preserve replicate identity with a preregistered aggregation method, or explicitly reject unsupported multi-replicate summaries. Results must be invariant to record order apart from documented same-key supersession.
9. A10: Version a measurement digest that includes expectations and referenced schema/fixture hashes as well as task text and grader identity. Preserve historical identity semantics explicitly.
10. A11: Give legacy interactive items appropriate behavior checks and positive/broken fixtures. Start a new measurement series when the rubric changes; never upgrade old scores without actual regrading.
11. A12: Correct the ensemble resampling claim. Independent resampling cannot establish independence of real candidates. Add a correlated-group control and report actual grouped/selected outcomes when such data exists.
12. A13: Stop calling equal elapsed time equal compute. Require complete per-unit cost capture and a declared comparison policy; report latency and aggregate hardware work separately. Cover parallel-arm and missing-timing cases.

Use independent subagents for bounded non-overlapping security, runtime, and evaluation work if available. Coordinate shared files and integrate carefully. Keep changes within these repairs; do not migrate frameworks, add a strategy, activate reputation routing, build marketplace features, or refactor broadly.

Run focused regression tests after each repair and the full suite plus Ruff once the integrated changes are ready. Report any platform/tooling limitations precisely. Update contradictory docs touched by these changes, especially --no-exec, review success, listener privacy, and the scope of historical evaluation numbers.

Finish with a concise account of confirmed issues, fixes, exact tests, remaining limitations, and a reviewable diff. Prepare—but do not execute—a small canonical direct versus direct-with-repair versus DAG benchmark plan with an explicit total budget, frozen model/runtime/task/checker identity, actual token/timing capture, and blinded acceptance. The objective is trustworthy safety and measurement before another expensive inference campaign.
```

The full prompt addresses all audited defects. For a shorter session, stop after the seven safety/runtime items with passing focused checks and an explicit handoff; do not begin a model benchmark while the measurement issues remain.
