# Canonical direct / repair / DAG development pilot — not activated

Prepared September 13, 2026. **No benchmark has been run.** This is a bounded
development pilot, not a confirmatory claim or permission to enroll hardware.
The earlier equal-elapsed-time study proposal is withdrawn. The current legacy
eval runner always runs DAG; changing `--arm` does not implement this plan.

## Fixed design and budget

Use these 12 development items, once per arm (36 planned cells, replicate 0):

- extract-log-errors; extract-markdown-headings; extract-jsonl-fields; extract-table-from-text
- transform-csv-to-jsonl; transform-dedupe-orders; transform-word-frequencies; transform-merge-two-files
- classify-orders-by-size; classify-sentiment-keywords; classify-readings-anomalies; classify-sales-performance

No confirmatory items, replacements, optional stopping on observed quality, or
additional replicates. Interleave the three arms within each item using a
frozen permutation with scheduling seed 20260913. Run one cell at a time on one
already owner-approved hardware profile. Record warm/cold status. Any separate
warm-up consumes the same total budget; no uncounted calibration generation.

| Arm | Canonical requests and selection | Output-token ceiling per cell |
|---|---|---|
| direct | One `strategy=direct` request; retain its only candidate | 4,096 |
| direct-with-repair | One direct request (2,048 tokens), plus at most one direct repair request (2,048 tokens) when the checker rejects it; retain the last candidate | 4,096 combined |
| dag | `strategy=dag`, maximum_subtasks=2, review=true, revision=false; planner 512, each builder 768, reviewer 1,536 tokens, with remaining 512 reserved inside the same ceiling | 4,096 combined |

The **entire pilot stops at the first of 36 terminal cells, 147,456 generated
tokens, or 21,600 aggregate inference hardware-seconds (6 hours)**. Each cell
has at most 600 aggregate inference hardware-seconds and a 600-second elapsed
deadline shared across its requests/calls. Retries, failed/cancelled calls,
planner/reviewer calls, and discarded candidates all consume the budget.
Disable automatic planner/builder retries and unbudgeted repair. Checker work
has a separate total cap of 3,600 hardware-seconds (1 hour), making the overall
hardware-work ceiling **7 hours**. Hard budget loss stops dispatch and records
unknown cost if termination cannot be measured. Missing planned cells make the
pilot incomplete; do not shrink the manifest to match what ran.

## Identity freeze before activation

Candidate model name: `qwen3.5:4b`; its actual immutable model digest, runtime
version/binary digest, quantization, hardware identifier, and OS remain
**unresolved** in this repository-only session. Do not substitute a tag for a
digest or fill missing values with guessed defaults. Record the actual runtime
and model available on the owner-approved machine before freezing the plan.

Pin context to 8,192 tokens, generation temperature to 0, and generation seed
to 20260913; record seed honoring as unknown unless independently demonstrated.
Freeze the finalized repository commit/diff hash, strategy/prompt versions,
runtime/provider settings, all retry/token/deadline settings, and hardware/OS
identity in the activation manifest. No runtime/cache/model changes mid-pilot.

The prepared corpus has historical corpus identity version 1
`b4fc182b0391ecd51eb52c0bfe4c52f007efc6059a26ae1ba067e65f9ef04f00`.
With grader 3, measurement identity version 2 is
`e4c78f84c67ea64a1ccf5ae2390c6bd15cc0423c5455978bcb2a53e1e7418ce5`.
That digest binds task text, expectations, and referenced schema/fixture bytes.
Recompute and verify it against the reviewed final checkout before activation;
a difference requires a new frozen plan, not silent continuation. Also record
the actual checker source SHA-256 and every acceptance fixture hash.

Write `manifest.json` before the first call: version `1`, the exact item/arm
lists above, replicates `[0]`, aggregation `single_replicate`, study ID,
measurement/checker/model identity, and declared
`homogeneous_hardware_seconds_v1` budget policy with the measured hardware ID
and relative tolerance 0.25. Preserve this full manifest after interruption.
Existing historical records are not migrated into it or reclassified.

## Required adapter and instrumentation

Activation is blocked until a controlled adapter uses `ExecutionRequestV1` and
the canonical service for all three arms, with local placement/local-only
confidentiality and no enrollment. Direct-with-repair is a bounded composition
of existing direct executions, not a new registered strategy. It must carry
the prior candidate and public checker diagnostics within the existing request
bounds. If they cannot fit, record unsupported repair and stop preflight;
never silently truncate a repair input or hide it in an output schema.

Public output requirements reach generation through the shared contract brief.
Private reference answers, expected stdout, evaluator fixtures, and blinded
acceptance results never enter model context. A repair diagnostic identifies
the failed requirement or parser error without revealing the reference answer.

Capture actual per-call input/output token counts and inference durations from
the runtime response, with call ID, role, execution/candidate/attempt identity,
hardware ID, retry/cancellation outcome, monotonic start/end, and warm/cold
status. Record every call, including rejected and abandoned work. Record
elapsed cell latency, total per-call inference hardware-seconds, checker time,
and optional energy separately. `unit_costs` plus `cost_capture_complete=true`
is permitted only after the collector reconciles all started calls. A missing
runtime counter is unknown, never zero or a text-length estimate. The current
text-only model wrapper does not yet provide this complete collector.

## Acceptance and reporting

Before generation, review authored positive and broken fixtures for each
acceptance check. Any generated-code behavior check requires an explicitly
approved isolated evaluator environment in the future; production validators
remain static/structural and no generated artifact is executed in this session.

Give the human acceptance reviewer randomly assigned artifact IDs and task
requirements without arm, model rating, latency, or repair history. Freeze and
hash the rubric and blinding map before review; reveal arm labels only after
all judgments are locked. Record false accepts, correctness failures, missing
checks, and operational failures separately. Select no oracle winner.

Report per-arm accepted counts out of the planned 12, uncertainty, complete
paired item tables, latency median/p95, aggregate inference and checker work,
actual tokens, and missing measurements. The single-replicate endpoint is
supported; repeated-item summaries are deliberately rejected. Cost matching
requires complete cost records under the declared hardware policy and is
separate from quality. This small development pilot chooses a next question;
it does not establish general model superiority, candidate independence,
hardware-independent compute equality, or current production readiness.
