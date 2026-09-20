# Remediation review and canonical pilot preflight

The benchmark remains **unexecuted**. Work used authored fixtures, mocked HTTP
model responses, static parsers, disposable SQLite/files and isolated local tests.
No real contributor/coordinator, model download, production inference, deployment,
external message, or untrusted generated-artifact execution was performed.

## Checkout reconciliation

The requested path initially held a clean `master` at audited revision
`418a44ddbfcf28d042ce7dc1add8f2493ed28dd7`, with no tracked/untracked changes.
The September 13 documents were absent there. Local branch inspection found
the remediation already committed as `214b466` on
`codex/september-audit-remediation`; the clean checkout was switched to it.
The review compared that complete remediation with the audited base. Changes
from this session remain uncommitted on the remediation branch. Historical
audit artifacts, their original claims, logs, measurement identities and scores
were preserved; this addendum records the changed repository state.

Three bounded runtime, call-capture and measurement reviews were delegated.
The primary agent inspected and integrated the initial edits. Final read-only
passes then checked the integrated review parser, budget boundaries, identity
validation, private-data exclusion and cost semantics before verification.

## Confirmed fixes and reviewed boundaries

- Cost summaries previously discarded the cost of superseded attempts by using
  the quality endpoint's latest-row projection. They now include all appended
  attempt costs; an earlier missing cost still prevents a complete comparison.
- A rating heading inside a fenced or assembled deliverable could supply a
  missing review verdict. Verdict parsing now requires the initial protocol
  heading and excludes assembled content.
- Revision verification reused an assembly-capable reviewer, which could pass
  its own replacement while the pipeline retained different revised bytes.
  Verification now requests assessment of the exact candidate and refuses a
  PASS containing any assembled-output section, including compact and indented
  heading variants understood by the general parser.
- Reviewed project path confinement, linked files/junctions, shared public
  contract projection, builder-wave cleanup, required dispatcher revocation,
  manifest completeness and measurement digest changes. No additional confirmed
  defect was changed in those areas. Trusted-host race limits and cooperative
  external-runtime cancellation limits remain as documented.

## Experiment infrastructure

`evals/canonical_pilot.py` composes the existing canonical direct and DAG paths.
Direct-with-repair is at most two canonical direct requests, never a registered
strategy. The complete repair request must fit the existing 1,000-character
task field; unsupported repair stops the pilot without truncation.

`execution/call_capture.py` observes actual HTTP generation attempts and terminal
stream counters. It preserves unknown counters, cancellation and abandoned work,
role/execution/unit/candidate identities, per-call IDs, timing and warm/cold
observations. Local worker-attempt IDs remain null where none exists. Complete
cost requires every started call to reconcile with actual counters and confirmed
termination, plus an observed warm/cold value. Input/output counts are runtime
counters, not text estimates.

The adapter reserves output tokens and serializes runtime calls under the
unchanged 36-cell, 147,456-token, 21,600-inference-second and
3,600-checker-second ceilings. Each cell shares a 600-second deadline and
600-second inference budget. Conservative admission charges are separate from
the reported runtime inference-duration metric. Unknown runtime cost/termination,
budget exhaustion and failed preflight stop dispatch. Append-only events and run
records retain operational failures; interrupted journals cannot be resumed.
Checker records preserve observed elapsed time separately from a complete total;
the latter remains null after an exception or unconfirmed cleanup.

The freeze binds repository commit/diff and actual source/fixture hashes,
active prompt bytes/version, strategy versions, corpus/checker identity and
externally observed model/runtime/hardware/settings identities. Unavailable
identities remain null in preparation. Digest fields require actual SHA-256
values rather than tags or labels. Historical corpus identity v1 and the plan's
measurement identity v2 are unchanged.

The plan addendum explicitly resolves the unused DAG 512-token reserve,
serialization, optional warm-up, repair-size incompatibility, admission versus
measurement time, and mechanical versus blinded human acceptance endpoints.

## Verification

- Focused remediation regressions: **54 passed in 84.36s**.
- Adapter, capture, Ollama and sampling integration: **68 passed in 161.89s**;
  [log](2026-09-14-pilot-focused.log).
- Final integrated focused suite: **106 passed in 104.63s**;
  [log](2026-09-19-pilot-focused.log). It includes the complete offline control,
  call capture, budget boundaries, review parsing and measurement regressions.
- The standalone offline control exercised **36 scheduled fixture cells and 84
  mocked runtime calls**, including 12 deliberately rejected first repair
  candidates. It proved the frozen schedule, all shared budget rejections,
  bounded/public repair inputs, absence of an injected checker-private sentinel
  from every model-call class, reconciled normal calls, and an honestly unknown
  missing duration counter. These are plumbing controls, not corpus acceptance
  results or real hardware-cost measurements. The standalone CLI completed with
  the same assertions; [report](2026-09-19-pilot-offline-control.log).
- Initial control attempts exposed source drift during an edit, Windows cleanup
  of unreferenced SQLite connections, and a token-field integration mismatch.
  The stable successful run above followed those corrections. Original audit
  verification logs were not overwritten.
- The first integrated full suite returned **2 failed, 2,628 passed, 41 skipped,
  2 expected failures in 4,441.24s**; [log](2026-09-14-pilot-full-pytest.log).
  Both failures were old run-record test stubs returning no response for the new
  assessment-only reviewer prompt. The stub now supplies a verdict for that
  reviewer role without an unrelated assembled replacement. The affected
  run-record/runtime suite then passed **41 tests in 34.24s**;
  [log](2026-09-14-pilot-review-focused.log).
- The September 14 rerun was interrupted at 56% and establishes no suite result;
  its partial [log](2026-09-14-pilot-full-pytest-rerun.log) is retained rather
  than rewritten.
- The fresh final full suite, with isolated Caddy enabled, returned **2,643
  passed, 41 skipped, 2 expected failures in 888.23s**;
  [log](2026-09-19-pilot-full-pytest.log). The 41 skipped tests are not claimed
  as exercised coverage. The two expected failures remain the documented
  seam-contract findings; neither is claimed as passing.
- Ruff and `git -c core.safecrlf=false diff --check` pass on the final integrated
  diff.

## Activation blockers and future procedure

Real immutable model/runtime/quantization, hardware/OS, provider settings,
owner/isolation approval, acceptance fixture, rubric and blinding-map identities
are unavailable. Real positive/broken acceptance controls and an approved isolated
checker must be supplied and reviewed. The adapter's checker booleans do not
stand in for blinded human acceptance. Seed honoring remains unknown.

The offline controls use synthetic identities and must never be represented as
a real study. Cancellation cannot prove a separate model runtime has stopped;
that loss remains unknown and stops the pilot. Windows static-validator coverage
does not establish a generated-code sandbox or POSIX containment.

The [pilot plan's activation procedure](../experiments/2026-09-13-canonical-pilot-plan.md#future-explicitly-authorized-activation)
gives preparation/control commands and the exact adapter API sequence for a
future explicitly authorized run. There is no live CLI flag, no automatic
enrollment, and no benchmark has been activated by this work.
