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
`e4c78f84c67ea64a1ccf5ae2390c6bd15cc0423c5455978bcb2a53e1e7418ce5`
for the reviewed CRLF Windows checkout and
`1833a5834b15fc2242255615dcd3e1752082035297fb580fb69f8eba0b78ab38`
for the byte-distinct LF checkout used by Linux/macOS CI. The identity binds
task text, expectations, and the actual referenced schema/fixture bytes, so
these values must not be collapsed into one cross-platform label. Recompute and
freeze the value from the owner-approved activation checkout; any other value
requires a new reviewed plan, not silent continuation. Also record the actual
checker source SHA-256 and every acceptance fixture hash.

Write `manifest.json` before the first call: version `1`, the exact item/arm
lists above, replicates `[0]`, aggregation `single_replicate`, study ID,
measurement/checker/model identity, and declared
`homogeneous_hardware_seconds_v1` budget policy with the measured hardware ID
and relative tolerance 0.25. Preserve this full manifest after interruption.
Existing historical records are not migrated into it or reclassified.

## Required adapter and instrumentation

The controlled adapter uses `ExecutionRequestV1` and the canonical service for
all three arms, with local placement/local-only confidentiality and no
enrollment. It is implemented by the September 14 preflight addendum below;
activation remains blocked on the unresolved identities and acceptance
prerequisites. Direct-with-repair is a bounded composition of existing direct
executions, not a new registered strategy. It must carry
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
runtime counter is unknown, never zero or a text-length estimate. The legacy
evaluation runner does not provide this complete collector. The experiment-only
call capture described below instruments the canonical model wrapper without
changing production strategy behavior.

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

## September 14 preflight implementation and explicit resolutions

The experiment-only adapter is `evals/canonical_pilot.py`; production strategy
registration is unchanged. The command below only prepares unresolved identities:

```powershell
python scripts/canonical_pilot_preflight.py --output C:\TEMP\pilot-prepared.json
```

Offline controls, with authored fixture responses and mocked HTTP transport:

```powershell
python scripts/canonical_pilot_preflight.py --dry-run --output C:\TEMP\pilot-controls-new
python -m pytest tests/test_canonical_pilot.py tests/test_call_capture.py -q
```

Use a new output location. The controls exercise all 36 planned cells and 84
mock calls through the existing canonical direct and DAG implementations. The
fixture checker is an exact-text control, **not the corpus acceptance checker**;
no generated code is executed. Synthetic identities and cost counters are
labelled offline controls and must never be used to activate a real study. The
same dry run injects a checker-private sentinel and proves it is absent from
direct, repair, planner, builder, and reviewer payloads. It also performs
negative admission probes for every shared budget, rejects oversized/private
repair inputs, and verifies that an omitted duration counter remains unknown.

These implementation decisions resolve ambiguities without raising a ceiling:

- DAG's named allowances sum to 3,584 tokens. Its remaining 512 tokens stay
  unused: automatic planner/builder/schema retries and revision are disabled.
  They are not permission for another call. The global ceiling remains 147,456.
- Runtime calls are serialized on the one approved hardware profile, including
  independent DAG builders. The DAG planner/dependencies/reviewer still use the
  existing path. This avoids unreserved overlapping runtime work; serialization
  is frozen as an experiment setting and changes no production default.
- Per-call runtime `total_duration` is the reported inference hardware-duration
  metric. Admission conservatively charges the larger of this and monotonic
  request wall time. The latter includes transport/queue overhead and is recorded
  separately; it never replaces the actual runtime counter in comparisons.
- The 1,000-character canonical task bound also applies to the entire repair
  request. A 2,048-token candidate is **not guaranteed to fit**. Unsupported
  repair records an ungraded operational/preflight outcome and stops the pilot;
  no truncation, schema transport, replacement cell, or enlarged request is used.
  The full manifest remains intact. A design guaranteeing repair for longer
  candidates would require a separately reviewed plan/protocol change.
- There is no separate warm-up entry point. Record actual warm/cold state;
  do not run uncounted calibration generation. Adding an explicit warm-up would
  require extending the frozen accounting plan inside these same ceilings.
- Checker time includes static canonical validation and the explicitly supplied
  acceptance endpoint. Nested parser work is counted once. Unknown cleanup or
  termination stops dispatch; cancellation does not imply zero runtime cost.
  Run records and journal events preserve observed checker seconds separately
  from the complete total, which remains null when termination is unknown.
- Frozen identities include current commit, tracked diff, actual source/fixture
  file hashes (including new adapter files), active prompt bytes/version,
  strategy versions, measurement identity, and externally observed runtime,
  model, hardware, settings, approval, rubric, and blinding identities. Before
  every model dispatch the adapter compares them again. A source edit invalidates
  an in-progress run. Fields named as digests must be 64 hexadecimal SHA-256
  values (the model digest may use Ollama's `sha256:` prefix); tags and labels
  are rejected. Seed honoring remains unknown.

## Future explicitly authorized activation

There is intentionally no live CLI flag. The remaining prerequisites are an
owner-approved runtime with observed immutable identities, reviewed positive
and broken fixtures for **each actual acceptance check**, an explicitly approved
isolated evaluator, and a frozen rubric/blinding map with locked human judgments
before unblinding. The repository-only preflight does not establish any of them.

After explicit authorization, on that approved isolated machine:

1. Finalize and freeze the reviewed checkout; rerun the offline controls first.
   Obtain the real model digest, quantization, runtime version/binary digest,
   hardware/OS identity and provider/settings digest without substituting tags.
2. Supply a read-only `observed_identity()` function returning those observed
   values and every `REQUIRED_IDENTITIES` field. It must re-observe drift, not
   return guessed constants. Supply an approved bounded async checker accepting
   `(item_id, candidate, deadline_monotonic=...)` and returning a measured boolean
   plus only `syntax`, `artifact`, or `requirement`. Private checker data stays
   inside that endpoint; it must enforce process cleanup on its deadline.
3. Use `prepare_manifest(study_id, observed_identity())`, inspect it, and call
   `validate_activation(manifest, observed_identity())`. Construct a private,
   disposable `ExecutionService` with confined SQLite/artifact roots; no network
   listener, enrollment, or production state is needed. Construct
   `CanonicalPilot(service, manifest, observed_identity, checker, new_directory,
   warm_cold="warm" or "cold")`. It writes the manifest before dispatch.
4. In one async invocation, iterate `manifest["schedule"]` in order and await
   `pilot.run_cell(cell)`. Stop on any exception or `pilot.ledger.stopped`.
   Never resume an existing journal or rerun a cell. Preserve `manifest.json`,
   `pilot-events.jsonl`, `runs.jsonl`, canonical artifacts, and unknown costs.
5. Have the acceptance reviewer judge the blinded artifacts using the frozen
   rubric. Keep human acceptance judgments distinct from the mechanical checker
   booleans in `runs.jsonl`; the adapter does not infer human acceptance.
   Lock judgments before unblinding. Only then report the planned denominators,
   missing cells, endpoint distinctions and separate costs. The existing summary
   rejects missing/ungraded cells; it must not be used to conceal an incomplete
   pilot or relabel the fixture controls as benchmark evidence.
