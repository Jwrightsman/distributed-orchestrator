# September 12 audit remediation

Base: `418a44ddbfcf28d042ce7dc1add8f2493ed28dd7`. Started with only the
September 12 audit files untracked; those original evidence files were
preserved. Work is an uncommitted reviewable repository diff. No production
coordinator/contributor was run or enrolled, no model was downloaded or used,
no untrusted generated artifact was executed, and no deployment or external
message was made. Tests use mocks, authored fixtures, disposable SQLite/files,
and isolated loopback services.

## Reproductions and repairs

The original offline audit script reproduced all six probe groups at the
unchanged base before edits. New tests assert the correct boundary rather than
the old defective outputs. The historical script is intentionally retained;
it now stops at rejected traversal and is not a regression-suite entry point.

| Findings | Confirmed behavior and repair | Focused coverage |
|---|---|---|
| A1 | The authored sentinel executed under `--no-exec`. Both graders now share execution policy; Python/stdout/browser checks are explicitly ungraded when disabled, while static checks still run. | `test_eval_execution_policy.py` spies on all execution entry points and checks static failures. |
| A2 | `../outside` read memory and modified metadata. Canonical, legacy, CLI/MCP/internal callers now share portable ID validation; every project-memory path is confined, including linked metadata, iteration directories/files, and post-summarization writes. | `test_project_confinement.py`: Windows/POSIX path strings, real Windows junctions, symlinks/hardlinks, preserved valid IDs, read/write and HTTP/MCP paths. |
| A3/A4 | Both original Caddy templates emitted synthetic share and custom-header credentials; private adaptation listened on `:443`. Both templates now omit complete URIs and all request/response headers in access/runtime logs. The private template explicitly binds literal tailnet addresses. | `test_caddy_security.py`: actual emitted JSON logs for ordinary, denied, proxy-error, encoded-share and artifact-share requests; header forwarding preserved; IPv4-only/dual-stack TCP/TLS and off-address denial with correct Host/SNI. |
| A5 | Missing ratings defaulted PASS and unchanged bad revisions cleared FAIL. Ratings now fail closed as UNKNOWN; attempts are distinct from repair success. Changed output needs a separate review, recorded with its hash, before review success can change; parser changes invalidate a prior review PASS. Canonical assurance stays independent. | `test_runtime_audit_regressions.py`, updated parsing/run-record/resilience tests. |
| A6 | A controlled sibling completed after its builder wave failed. Waves now cancel and await children; service failure/normalization/finalization/persistence paths revoke dispatcher authority while preserving settled receipts/contributions. Permanent revocation failure suppresses terminal publication. | Controlled wave cancellation and service/attempt tests, including late-result rejection, durable accepted contributions, finalization failure, and bounded revocation failure. |
| A7 | A contract-only filename was absent from captured DAG planner/builder/reviewer prompts. DAG and ensemble now share a bounded public contract brief; private evaluator data is not an input. | Captured actual strategy/pipeline stage prompts plus public projection checks. |
| A8/A9 | Observed prefixes were accepted as complete, and opposite replicates changed the endpoint when reordered. A preregistered manifest now defines all cells; missing cells or frozen-identity mismatches reject summaries. Multi-replicate summaries are explicitly unsupported; replicate identity is never coerced. | Missing-all-arms, order invariance, exact-key supersession, malformed manifest, replicate rejection, frozen-plan and runner preflight tests. |
| A10 | Editing expectations preserved the old corpus digest. Historical corpus identity v1 is deliberately unchanged; new measurement identity v2 hashes task/expectations/referenced schema and fixture bytes/grader identity. New runner records explicitly carry both versions. | Changed expectations, schema bytes, fixture bytes, checker version and missing-reference regressions. |
| A11 | All six legacy interactive items had zero behavior checks. Corpus v3/grader 3 start `legacy-interactive-v3` with explicit DOM/canvas smoke probes and positive/inert fixtures. Stored historical results and bands are unchanged. | Each behavior probe accepts its authored positive fixture and rejects an inert counterpart that still passes the old browser-load check. |
| A12 | Independent resampling returned 0.968 best-of-five against the closed form 0.96875; perfectly correlated real groups would remain 0.5. The claim now identifies an independence-based illustration, with separate actual grouped/selected outcomes. | Perfect correlation, order invariance, oracle-vs-selected distinction, missing selection. |
| A13 | Two equal-latency synthetic arms with no unit costs were labeled equal compute. Summaries now separate latency, aggregate inference hardware-seconds, and tokens; comparable-cost claims require complete per-call capture and a declared hardware policy. | A five-call parallel arm has the same latency but five times the aggregate work; missing timing/tokens/hardware/capture/latency suppress comparisons. |

## Verification

Windows, Python 3.14.3. The isolated Caddy tests used **Caddy v2.11.4** from the
official release, extracted to a temporary directory as a test dependency. No
system service, production proxy, certificate store, or firewall was changed.

Focused commands completed:

```text
python -m pytest tests/test_eval_execution_policy.py tests/test_eval_grading.py tests/test_evals.py tests/test_eval_exec_classification.py -q
74 passed in 32.95s

python -m pytest tests/test_eval_execution_policy.py tests/test_eval_runner_manifest.py -q
9 passed in 0.98s

python -m pytest tests/test_project_confinement.py tests/test_caddy_security.py tests/test_runtime_audit_regressions.py tests/test_eval_runner_manifest.py tests/test_eval_run_records.py tests/test_eval_corpus.py -q
132 passed in 16.39s

python -m pytest tests/test_measurement_audit_regressions.py tests/test_runtime_audit_regressions.py -q
47 passed in 45.10s
```

For Caddy tests, set `MYCELIUM_TEST_CADDY` to an existing executable (the tests
never download it). Without one, the emitted-log/listener tests explicitly skip.
The [runtime focused log](2026-09-13-runtime-focused.log) additionally records
141 passing runtime tests from integration.

The [first integrated full run](2026-09-13-remediation-pytest.log) returned
2,597 passed, one failed, 41 skipped, and two expected failures in 1,021.07s.
The failure caught disabled certificate verification in the new isolated Caddy
client. The client now explicitly trusts its disposable certificate with hostname
verification enabled. The harness restricts protocols to TCP HTTP/1.1 and HTTP/2;
its probes do not cover QUIC, and Windows rejected an unrelated ephemeral UDP
listener during the focused rerun. Production templates were not changed for this.

`python -m pytest tests/test_caddy_security.py tests/test_worker_transport.py -q`
then returned **66 passed in 26.84s**, with Caddy enabled; see the
[focused log](2026-09-14-caddy-focused.log).

Final full-suite command: `python -m pytest -q -ra`, with Caddy enabled:
**2,598 passed, 41 skipped, two expected failures in 1,369.21s (22m 49s)**;
see [the final full log](2026-09-14-remediation-pytest.log). The skips cover
POSIX/macOS-only behavior, unavailable OpenSSL certificate tooling, and the
existing absent `requires-python` declaration. The two existing expected failures
cover inline settlement accounting and legacy worker-supplied hostname storage
(ADR 0016); neither is claimed repaired here.

`python -m ruff check .` and `git -c core.safecrlf=false diff --check` passed
after the final report update. Changes remain uncommitted for review.

## Limits and next step

- Caddy tests establish adapted-template behavior on disposable TCP/TLS
  IPv4/IPv6 loopback listeners, not the deployed tailnet, UDP/HTTP3, host
  firewall, routing, or real certificate renewal. Old logs/backups are untouched.
- Windows junction and link paths were exercised; no POSIX runtime was available
  in this session. Storage checks are not race-proof against a hostile local
  process swapping paths after checks. The host filesystem remains trusted.
- A separate model review establishes a review claim, not deterministic repair
  correctness. Canonical structural/deterministic assurance does not inherit it.
- The interactive checks are declared smoke probes. They are not proof of full
  game/app functionality; all historical quality numbers remain legacy evidence.
- Complete runtime token/timing collection and a canonical experiment adapter
  remain prerequisites for a cost-matched model study. The current legacy DAG
  runner records unavailable cost as unknown and cannot establish comparable
  compute. Repeated-item statistical aggregation remains unsupported.

The [canonical pilot plan](../experiments/2026-09-13-canonical-pilot-plan.md) is
prepared, **not activated**: 12 development items × three arms, at most 147,456
generated tokens, six inference hardware-hours plus one checker hardware-hour,
frozen identities before activation, complete actual cost capture, and blinded
acceptance. Unavailable model/runtime identities are explicit activation
blockers. No new inference campaign was started.
