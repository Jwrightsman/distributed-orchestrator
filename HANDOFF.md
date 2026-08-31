# Handoff — Mycelium Theme 3A Bounded Validator Process Isolation

_Updated August 31, 2026._

## Read these first

1. `docs/adr/0013-parser-heavy-validators-bounded-process-boundary.md` — current
   validator classification, protocol, staging, containment, and security limits
2. `docs/adr/0011-node-capabilities-versioned-claims.md` — typed claim and hard
   matching boundary
3. `docs/adr/0012-observed-capability-evidence-shadow-only.md` — scoped evidence,
   shadow-only policy, and operational-health boundary
4. `docs/experiments/capability-evidence-shadow.md` — live thresholds and the
   future-active no-go gates
5. `docs/ARCHITECTURE.md` and `docs/PROTOCOL.md` — current system and normative
   execution/client/worker contracts
6. `docs/TRUSTED_ALPHA_RUNBOOK.md` and `docs/OPERATIONS.md` — protected reporting,
   deployment, backup, and recovery procedure
7. `docs/adr/0010-durable-enrollment-identity.md` — retained enrollment identity
   foundation
8. `MASTER_PLAN.md` — current direction and freeze boundary
9. `docs/audits/2026-08-23-comparative-architecture-audit.md` — historical,
   non-normative architecture research; do not rewrite it as current policy
10. `CLAUDE.md` and `AGENTS.md` — repository and consent rules

`SPRINT_DURABLE_NODE_ENROLLMENT.md`, `SPRINT_DURABLE_EXECUTION_TRUTH.md`,
`SPRINT_TRUSTED_ALPHA_RC1.md`, `SPRINT_STRATEGY_PROTOCOL.md`,
`SPRINT_TRUSTED_ALPHA_INTEGRITY.md`, and `SPRINT_PHASE2.md` are historical
records. Do not copy their old test counts, status lists, or interface
assumptions into a current claim.

## Human and scope context

Jett has no programming background. Make technical calls, explain the choices
he must act on, and warn before anything network-facing. Never install, join, or
run Mycelium on a machine without that machine owner's explicit informed
consent.

Theme 3A puts parser-heavy, trusted built-in validators behind a bounded child-
process boundary. It contains parser failures, stages only selected candidate
files, clamps work to the remaining execution deadline, records fail-closed
validation evidence, and adds parent-authored execution metadata and content-
free counters. It does not
execute generated code, establish same-user filesystem confidentiality,
guarantee network denial, or add containers, VMs, WASM, plugins, behavioral
validation, workflow resumption, or coordinator HA. Theme 2.1 capability and
evidence behavior remains a retained prerequisite below.

## Branch and review

- Branch: `codex/theme-3a-validator-process-isolation`
- Base: latest default branch after Theme 2.1, starting at
  `79986be455c3f35ee9671f09d8a25f70550b040c`
- Merge: review explicitly; do not auto-merge
- Current decision/handoff records: `HANDOFF.md` and
  `docs/adr/0013-parser-heavy-validators-bounded-process-boundary.md`

The final integrator must record the current commit range and current full-suite,
Ruff, import, preflight, and deployment checks. Historical counts in sprint
records are evidence for those exact revisions, not a substitute for the final
branch run. The configured GitHub Actions jobs are Ubuntu-only. Theme 3A's
operating-system split is nevertheless material: report POSIX CI and Windows
manual results separately where containment expectations differ; Windows
verification remains pending until such a run is recorded.

## Theme 3A architecture

- `auto` is the default. It classifies `code_parse`, `structured_json`, and
  `json_schema` as `subprocess_isolated`; `nonempty`,
  `artifact_extraction`, `artifact_contract`, and `file_manifest` are
  `inline_trusted`.
- Forced `subprocess` supports every current built-in. Explicit `inline` is a
  weaker local-development/debug mode, and trusted-alpha preflight rejects it.
  Evidence records an overridden isolated parser as `inline_compatibility`;
  required isolated checks never silently fall back inline after a runner
  failure.
- Runner defaults are 10 seconds, 256 MiB, 2 MiB request, and 32 KiB response.
  Strict inclusive config bounds are respectively 1–120 seconds, 128–1,024
  MiB, 16 KiB–16 MiB, and 1–256 KiB. Booleans and non-integer values are
  invalid; trusted loading raises while local loading warns and defaults.
- The registry remains a closed built-in allowlist. The strict version-1
  request/response protocol cannot name an import, executable, shell command,
  arbitrary callable, plugin, credential, database, or unrelated execution.
  Task content, generated output, schemas, and filenames are not process
  arguments.
- Parent and child use bounded JSON over stdin/stdout. Unknown fields,
  identity/version mismatch, malformed or excessive values, recognized bare or
  delimiter-prefixed absolute POSIX/Windows/UNC host-path patterns, or any
  `file://` pattern in response keys/values, and
  over-limit bytes fail closed. The protocol has no host-path field. The parent owns assurance,
  behavioral-correctness, required/optional/source, execution-mode,
  containment, and termination metadata.
- File-consuming child checks receive only regular-file copies selected from
  the authoritative candidate subtree. Staging rejects traversal, symlinks,
  reparse points, special files, another candidate, and size/hash/identity
  drift; enforces count, per-file, aggregate, and path bounds; uses no hard
  links. Process-tree termination and reaping are attempted before stage removal.
- The runner uses the current interpreter without a shell, a sanitized
  environment whose temp variables point into its controlled work directory, a
  fresh working directory/process group, bounded I/O, and a timeout clamped to
  the canonical repair/registry validation's remaining deadline.
  Cancellation and timeout request process-tree termination and reaping;
  failure to confirm process-tree cleanup is separately counted as a containment
  incident. Failure to delete the temporary workspace fails closed with
  `validator_stage_cleanup_failed` evidence and increments the distinct
  `staging_cleanup_failures` counter; the leftover directory still requires
  operator cleanup.
- POSIX applies available CPU, address-space, file-size, descriptor, and child-
  process limits. Windows retains wall-clock, bounded-pipe, staging, and
  best-effort cleanup controls but does not claim those POSIX guarantees;
  Windows path-race resistance is best effort within standard-library support,
  and stage privacy relies on the operator-secured temporary root's inherited
  ACL rather than POSIX mode bits.
  A process group is not parent-death enforcement: POSIX has an early hard
  child alarm. Successful Windows Job Object assignment adds best-effort
  kill-on-close behavior, but a pre-assignment escape or unassigned runner can
  survive coordinator crash; there is no child-side alarm, durable PID registry,
  or restart orphan discovery.
- Spawn, timeout, crash, protocol, oversize, staging, and stage-cleanup failures become bounded
  error evidence. An unconfirmed process-tree cleanup is reflected in error
  evidence or the content-free cleanup counter, depending on the original
  outcome. Parent-authored metadata and content-free
  process counters omit prompts, output, schemas, source contents, credentials,
  raw stderr, host paths, and arbitrary exceptions.
- `code_parse` parses generated files as data. It never imports a generated
  module or runs top-level statements, shells, build scripts, tests, browsers,
  package installers, or generated networking. Structural success is not
  behavioral correctness.
- The same-user subprocess is containment, not mandatory access control or a
  hostile-code sandbox. `network_policy` remains recorded intent. Containers,
  VMs, WASM, gVisor, Firecracker, plugins, and executable behavioral validation
  remain deferred.

Decision: [ADR 0013](docs/adr/0013-parser-heavy-validators-bounded-process-boundary.md).

## Theme 2.1 architecture retained

- `limits.max_output_bytes` is a claimed hard placement limit for typed nodes.
  The coordinator passes the canonical execution's authoritative
  `max_output_bytes` into the one shared matcher. Equality is eligible; a lower
  claim is ineligible with `insufficient_output_capacity`.
- The execution output budget is not duplicated in
  `NodeResourceRequirementsV1`, does not change canonical request hashing, and
  is checked consistently during scheduler qualification, eligible-set
  construction, long polling, the under-lock handout recheck, protected
  diagnostics, and shadow candidate capture.
- Explicit descriptorless compatibility retains legacy matching because no
  typed output claim is fabricated. Once work is assigned, the durable
  attempt's exact server-issued output limit remains authoritative for stream
  and settlement enforcement; a descriptor or result cannot raise it.
- `max_concurrent_execution_units` is an informational upper-bound claim. The
  coordinator does not maintain or enforce per-node slot counts. The stock
  worker remains sequential and conservatively stays within the claim; values
  above one do not create server concurrency slots, parallel polls, or
  capacity-weighted scheduling.
- Shadow admission freezes a bounded set of non-secret, assignment-time node
  claim inputs. Canonical rematching and evidence-scope construction run from
  that frozen snapshot in bounded background work, after the real attempt is
  durable, so they neither delay handout nor hold the production queue lock.
- Shadow operational health uses bounded admission outcomes (`disabled`,
  `not_applicable`, `queue_saturated`, `scope_capture_failed`, `scheduled`) and
  evaluation outcomes (`completed`, `evaluator_failed`,
  `decision_write_failed`, `cancelled_on_shutdown`). Successful minimal events
  are durable and deduplicated in sibling `capability-shadow-health.db`, whose
  writer locks are isolated from authoritative `events.db`; health-store write,
  containment, and callback failures are process-lifetime counters with a reset
  timestamp.
- Operational accounting is best effort and non-authoritative. Its records and
  protected report omit prompts, output bodies, worker error text, arbitrary
  exceptions, credentials, session tokens, attempt nonces, and artifact
  contents. Failure cannot change eligibility, selection, handout, settlement,
  execution state, or attempt count.
- Graceful shutdown closes new shadow admission and uses a finite drain.
  Timed-out capture is classified with the bounded reason
  `coordinator_shutdown_during_scope_capture`; an in-flight decision write
  retains its truthful eventual completed/write-failed classification instead
  of being mislabeled as cancellation.
- Protected evidence aggregates and deterministic evaluation reports expose
  `eligible_for_future_active_experiment` with bounded `blocking_reasons`.
  The reasons are `legacy_descriptor_identity`,
  `descriptor_identity_unreconstructable`, `immutable_model_identity_missing`,
  and `model_identity_unreconstructable`. The diagnostic does not change current
  hard eligibility, assignment, shadow preference, or valid shadow collection;
  digestless typed scopes continue collecting when otherwise reconstructable.
- A future active experiment requires immutable model and descriptor identity,
  every live volume/safety/predictive/fairness threshold, a separate accepted
  ADR, and a separately reviewed implementation PR. Active routing and worker
  concurrency remain unimplemented.

Decisions: [ADR 0011](docs/adr/0011-node-capabilities-versioned-claims.md) and
[ADR 0012](docs/adr/0012-observed-capability-evidence-shadow-only.md).

## Durable enrollment foundation retained

- `enrollment_id` is immutable durable contributor attribution; `node_id` is a
  display label; `session_id` is one process incarnation; `attempt_id` is one
  lease authority.
- Bootstrap requires shared admission plus a worker-generated high-entropy
  credential. Returning registration uses only the per-node credential.
- SQLite stores a domain-separated enrollment credential digest, status,
  timestamps, and credential version. It never stores plaintext.
- Sessions remain process-local and bind enrollment, label, token digest, and
  credential version. Every worker operation checks durable enrollment status.
- Revocation/rotation affects one enrollment, reclaims its active leases, and
  preserves attempts, receipts, and contribution history.
- New attempts, accepted receipts, quarantine audit rows, and contributions
  carry nullable enrollment attribution. Historical label-only rows stay
  explicitly unattributed.
- New enrolled contribution and verification identity uses enrollment ID;
  legacy compatibility identity is session-scoped so a label takeover cannot
  inherit trust/history.
- The stock worker owns a private atomic coordinator-scoped identity file. A
  coordinator backup preserves enrollment digests/attribution but not worker
  plaintext identity files.
- Trusted alpha requires durable enrollment plus TLS or a private authenticated
  overlay. Local `compat` mode remains explicit and cannot claim an enrolled or
  revoked label.

Decision: [ADR 0010](docs/adr/0010-durable-enrollment-identity.md).

## Theme 1 prerequisite retained

### Durable execution publication

- SQLite is authoritative for queued, running, and terminal canonical
  snapshots. Required snapshots commit with finite retry before a deep live
  copy, normal lifecycle event, callback, compatibility mirror, response, or
  terminal artifact/share publication.
- Permanent required-persistence failure raises `ExecutionPersistenceError`
  (terminal failures use `TerminalPersistenceError`). Active HTTP operations
  return `503` with `detail.code=execution_persistence_unavailable` and do not
  claim the transition.
- Asynchronous terminal-persistence failure leaves the last durable snapshot
  visible, suppresses normal terminal events and completion callbacks, and
  cannot be reclassified recursively as another unpersisted terminal result.
- Diagnostic events are non-authoritative and secret-safe. Once terminal state
  commits, later event, callback, or metadata failure does not undo it.
- Persisted events retain allowlisted structural telemetry only. Startup
  idempotently redacts historical payloads before HTTP/WebSocket replay;
  generated token text remains live-stream-only.
- Terminal artifact delivery, including through a share, requires a durable
  terminal snapshot. Current sealed roots must match the manifest hash bound
  into that snapshot; historical `legacy_live` roots remain an explicitly
  labeled, freshly rescanned compatibility path.
- Legacy run/history/gallery/status/try/CLI/download/demo publication resolves
  artifact-root ownership before mutable log fields and applies the same
  terminal/hash boundary. Restart-reconciled staged output remains hidden.
- DAG project-memory iterations publish after the normal terminal event and
  before the completion callback. Failed terminal persistence leaves memory at
  its prior committed iteration.
- Redundant terminal request/result snapshots are evicted after terminal
  observers finish. `GET` and replay reload the authoritative SQLite row;
  queued/running and failed-persistence boundaries remain cached.

Decision: [ADR 0009](docs/adr/0009-durable-terminal-commit-before-publication.md).

### Idempotent canonical submission

- Optional `Idempotency-Key` on `POST /v1/executions` validates 1–128 printable
  ASCII characters and is never stored or logged in plaintext.
- Validated requests, keys, and requester scopes use deterministic/digest-only
  identity. A configured pitch credential defines the trusted-alpha scope;
  open mode uses the direct peer host as best-effort development scoping.
- One immediate transaction creates the queued execution and mapping. Matching
  retries return the existing execution and `Idempotency-Replayed: true` without
  scheduling; changed requests return `409 idempotency_conflict`.
- Mappings persist through restart and backup. Replaying an interrupted
  execution returns it; it does not resume lost work.
- Compatibility HTTP, CLI, and MCP idempotency remain deferred.

Decision: [ADR 0008](docs/adr/0008-idempotent-canonical-execution-submission.md).

## Trusted-alpha foundation retained from RC1

### Deployment and single ownership

- `deployment_mode=local` preserves developer compatibility.
- `deployment_mode=trusted_alpha` fails preflight/startup unless viewer, pitch,
  and node secrets are pairwise distinct and at least 32 characters; state and
  config paths are coherent; secure-cookie/TLS intent is coherent; and enabled
  public pitching has an explicit acknowledgement.
- One operating-system lock is acquired before state migrations or background
  work. A second coordinator for the same state directory fails closed.
- `/health` publishes a safe protection bit and warnings. Viewer-authorized
  `/v1/operator/health` publishes instance ID, deployment mode, lock state, and
  preflight warnings.
- Docker remains one Uvicorn worker and includes the preflight scripts. Backup
  format v2 covers both `events.db` and `capability-shadow-health.db` with
  independent online SQLite snapshots, a versioned checksum manifest,
  validate-before-mutate restore, and rollback. Restore remains compatible with
  legacy format-v1 archives that predate the health database.

### SQLite, attempt authority, and contribution

- Production `events.db` access uses the common WAL, foreign-key, 10-second busy
  timeout, `synchronous=NORMAL`, bounded retry, and migration-lock policy.
- Best-effort shadow decisions and operational-health writes use sibling
  `capability-shadow-health.db`; its separate writer-lock domain prevents them
  from contending with authoritative attempt, assignment, or settlement writes.
  Pre-isolation decisions in `events.db` copy forward idempotently at startup.
- The coordinator persists an active attempt before handing out worker work.
  Authority binds task, execution, unit, kind, node, node session, contract,
  nonce digest, lease, state, and output cap.
- Result settlement atomically records the terminal attempt, immutable accepted
  receipt, exact-replay response/hash, and unique compute contribution.
- Exact replay state survives database reopen without paying twice; changed
  replay fails. Coordinator restart separately invalidates process-local node
  sessions, so it does not authorize reuse of an old session. Dispatch consumes
  only a receipt matching its expected execution and unit.
- Unknown, unleased, expired, reclaimed, cancelled, interrupted, wrong-bound,
  oversized, and changed-replay output remains outside operational results in a
  bounded diagnostic quarantine.
- Compute points mean accepted, bound compute. They do not mean candidate
  selection, validated correctness, money, a token, or future value.

### Enrollment, sessions, and bounded worker traffic

- `X-Node-Secret` authorizes initial bootstrap only. The stock worker proposes a
  high-entropy per-node credential; SQLite stores only its domain-separated
  digest under an immutable enrollment ID.
- Returning registration uses the label and enrollment credential without the
  shared secret. Registration returns a non-secret session ID, one-time
  plaintext session token, and start/expiry timestamps; the server retains only
  the session digest.
- Poll, heartbeat, drain, result, stream, and token-stream routes require the
  current `X-Node-Session`. Enrolled operations recover durable identity
  server-side and attempts bind enrollment, node, session, and credential
  version.
- Coordinator restart invalidates sessions while preserving enrollment.
  Revocation/rotation is enforced at the next authenticated operation and
  safely reclaims active leases.
- Sessions are process-local, last at most 24 hours, and enrollment remains
  bearer attribution rather than public-key/physical-machine identity or Sybil
  resistance.
- Node/registration/error fields, output bytes, cumulative stream bytes, batch
  count/rate, event fanout, and quarantine preview/count are bounded. Crossing a
  worker stream/output limit cannot be bypassed through reconnect or final
  settlement.
- Private node APIs distinguish immutable enrollment identity, current-session
  counters, and durable enrollment-keyed lifetime totals. They never return an
  enrollment credential/digest or session token.

### Lifecycle, validation, and post-hoc state

- One total deadline covers queueing, planning, generation, worker waits,
  validation, review/revision, and artifact finalization.
- Cancellation removes queued work, cancels active attempts, persists terminal
  cancellation, and rejects late results.
- Startup truthfully marks non-resumable queued/running executions, legacy jobs,
  and active attempts `interrupted`/retryable.
- Lifecycle, validation outcome, and assurance are separate. Compatibility
  `status` must not drive trust UI.
- Output contracts impose validator floors; JSON Schema and supported parsers
  produce explicitly scoped evidence. Structural evidence is not behavioral
  correctness.
- Post-hoc duplicate-verification fields exist, but trusted-alpha RC1 reports
  `posthoc_verification_status=disabled`. Do not imply that duplicate execution
  is running or that this field upgrades original validation.

### Role-scoped, sealed artifacts

- Entries are classified as `deliverable`, `provenance`, `log`,
  `candidate_source`, or `internal`. Private manifest/download defaults to
  deliverables; audit material has explicit manifest/download access.
- Terminal finalization applies winner scope and seals immutable entry rows plus
  a canonical manifest hash. Active roots remain protected through final scan.
- Every file/ZIP retrieval still confines, resolves, and re-hashes live bytes
  against the sealed baseline. Drift, missing content, symlinks, and traversal
  fail closed. Historical `legacy_live` roots remain honestly labeled/rescanned.
- `sealed_manifest_hash` is local integrity evidence, not a signature,
  independent timestamp, malware verdict, provenance attestation, or defense
  against a host able to change SQLite and files together.

### Explicit shares and public profile

- Share tokens are returned once and stored only by digest. Viewer APIs create,
  list metadata, revoke one, or revoke all. Invalid/expired/revoked capabilities
  deliberately have one public `404` shape.
- Public execution JSON is allowlist-based. It excludes private IDs/telemetry,
  filesystem paths, attempt/session credentials, credit detail, and unbounded
  diagnostics.
- Share artifacts are deliverables by default. Candidate source needs explicit
  candidate-detail scope; logs, provenance, and internal roles are never shared;
  no-winner candidate entries are excluded.
- Public share responses are no-store/no-referrer/nosniff. Application
  unhandled-error logging redacts token path segments. Uvicorn and reverse-proxy
  access-log redaction is still the operator's responsibility.
- Public pitch remains off by default. Trusted-alpha mode requires explicit
  acknowledgement and forces the bounded local/direct/no-project profile.

## Durability boundary

| Durable | Process-local or operator-owned |
| --- | --- |
| canonical execution and legacy-job snapshots | worker queue and dispatcher waits |
| scoped canonical submission mappings and request digests | submission scheduling and resumption |
| attempts, accepted receipts, replay responses | node sessions and connected-node registry |
| contribution records and compatibility projection | running coroutines and model-call process state |
| share metadata/token hashes | live WebSocket connections and event fanout |
| artifact registrations, sealed rows/hashes, retained files | breaker/waiting-node state |
| typed descriptor snapshots and scoped capability observations | current-session descriptor claim and background evaluator tasks |
| shadow decisions and successful operational-health events in `capability-shadow-health.db` | health-store write, containment, and callback counters plus their process reset timestamp |
| SQLite and file state captured by an explicit backup | archive scheduling, off-site copies, TLS/proxy configuration |

Restart reconciliation makes process-local loss truthful; it does not resume
work. Restore recovers captured durable state; it cannot recover node sessions,
in-flight calls, or queue ownership. Matching idempotency replay returns the
same captured execution; it does not recreate or resume it.

Durable snapshot commit precedes its live copy, normal event, callback, mirror,
response, and terminal artifact/share access. Diagnostic failure telemetry is
not another lifecycle authority.

## Claude Code frontend/API handoff

The backend intentionally does not prescribe visual layout. A frontend must
represent these fields directly instead of inferring them from legacy status,
filenames, or generic “verified” badges:

| Frontend-visible concept | Backend source and required presentation |
| --- | --- |
| Enrollment/session state | Private `/v1/operator/node-enrollments` and node fields: enrollment ID/label/status/timestamps, live session ID/start/expiry, drain/current task, and session/lifetime counters. Never request or display credential material or a session token. |
| Deployment protection status | Public `/health.private_routes_protected`, `/health.node_enrollment_required`, and warnings; private `/v1/operator/health` mode, instance ID, coordinator-lock state, and preflight warnings. Make fail-open/compat local mode visibly unsafe for reachable deployment. |
| Artifact role | `ArtifactEntryV1.role`: Deliverable, Provenance, Log, Candidate source, or Internal. Default requester download is deliverable-only. |
| Manifest integrity mode | `artifact_integrity_mode` / manifest `integrity_mode`: None, Active, Sealed, Legacy live, or Invalid. Do not show legacy live as sealed. |
| Sealed manifest hash | `sealed_manifest_hash` / `manifest_hash`. Label it local sealed-baseline integrity, not signature or external attestation. |
| Deliverable vs audit download | `artifact_manifest_url` and `/download` for deliverables; `audit_manifest_url`, `role=audit`, and `/audit-download` for non-deliverable audit material. Keep the separation explicit. |
| Lifecycle status | `lifecycle_status`: queued, running, completed, failed, cancelled, interrupted. Use it for control flow. |
| Validation outcome | `validation_outcome`: passed, failed, partial, not run. Present separately from lifecycle. |
| Validator execution boundary | Parent-owned validation evidence exposes execution mode, runner protocol version, platform containment level, applicable termination reason, and duration around bounded non-authoritative child detail. Render `subprocess_isolated` as an isolated process check, `inline_compatibility` as a weaker same-process compatibility check, and `inline_trusted` as a bounded inline check; never call any of them a sandbox or render raw child output, stderr, schemas, files, or host paths. |
| Validator runner health | `validator_process` object on private `/v1/operator/health`: configured mode, registered validator policies, runner protocol/containment level, process-local counter reset time and content-free outcome totals. Label it operational process health, not correctness or durable lifecycle truth. |
| Assurance level | `assurance_level` plus validation summary/evidence. Describe the exact checks; never collapse it to a generic verified badge. |
| Post-hoc verification | `posthoc_verification_status`, timestamps, agreement, and reason. RC1 trusted-alpha is disabled; show that plainly without changing original assurance. |
| Canonical submission replay | `Idempotency-Replayed` appears only on keyed canonical POST responses. `false` means created; `true` means an existing execution was returned. Never label it exactly-once execution or workflow resumption. |
| Share metadata | Create returns plaintext token once. List shows share ID, create/expiry/revocation/last-access times and artifact/node/candidate flags without token. Support revoke-one/revoke-all and explain that copied content cannot be recalled. |
| Claimed output capacity | Private node enrollment diagnostics report hard eligibility and stable reason codes. Present `limits.max_output_bytes` as a node claim, not attestation. Make clear that the durable attempt's task limit remains authoritative and that descriptorless compatibility has no fabricated typed capacity. |
| Shadow operational health | Protected evidence reporting exposes durable phase/outcome counts, `orphan_evaluation_total`, offered/scheduled/completed/skipped/failed/pending totals, the drop/failure numerator and denominator, latest event time, optional bounded windows, and process-local fallback counters with reset time. An orphan terminal row is visibly counted as one inferred scheduled/offered observation so partial health-store failure cannot silently shrink the denominator. Label it experiment health, not node reputation. |
| Future active-experiment identity | Per-scope `eligible_for_future_active_experiment` and `blocking_reasons`. Missing immutable model identity is a promotion blocker only; it does not change current routing or itself suppress otherwise valid shadow collection. Do not present it as trust or correctness. |

The client must handle `401` private HTTP, `4401` event WebSocket, `409`
artifact-integrity/session conflicts or `idempotency_conflict`, `413`
worker/artifact limits, `422 invalid_idempotency_key`, `429` rate limits,
`503 execution_persistence_unavailable` or `idempotency_consistency_error`, and
uniform public-share `404`. Events are flat; `/health.nodes_online` is an
integer; private node/session detail is not public status.

Use these assurance labels only, mapped to actual evidence:

| UI label | Meaning |
| --- | --- |
| Not checked | no applicable check ran or no evidence passed |
| Structure checked | bounded extraction/manifest/parser-level evidence only |
| Contract validated | deterministic contract/schema conformance for the stated contract |
| Behavior tested | an explicit behavioral validator actually ran and passed |
| AI reviewed | model-judged review evidence, not deterministic proof |

## Approved and prohibited product language

Recommended primary statement:

> Run auditable local-AI jobs across computers you trust.

Recommended supporting statement:

> Break work into coordinated components or generate multiple complete attempts.
> Mycelium dispatches work to local models, applies explicit checks, and records
> how each result was produced.

Revise or remove these claims wherever they appear:

- “Every request is split into pieces.” Direct and ensemble generate complete
  attempts; only DAG decomposes work.
- “Every result is working code.” Measured outputs are fallible and unstable.
- “Every result is tested to see whether it runs.” Checks depend on contract,
  format, and configured validator support.
- Absolute “no cloud.” Local Ollama is the default, but optional external
  OpenAI-compatible model providers exist.
- “Volunteer” or “anonymous” network as the current security model. The trusted alpha is an
  invited group of computers/operators you trust.
- A generic “verified” badge. Show lifecycle, validation, assurance, integrity,
  and post-hoc dimensions separately with evidence-scoped labels.

Also prohibited: trustless/permissionless readiness, public-key or physical
worker identity, attestation, confidential compute, enforced no-network execution, sandboxed
generated code, Sybil resistance, durable queue resume, multi-coordinator
operation, multi-user authorization, monetary credits, host-independent artifact
attestation, exactly-once external side effects, open-mode peer identity, active
evidence-based routing, node reputation, parallel worker handouts, or proof that
arbitrary generated output is correct.

Approved Theme 3A wording is “bounded process containment for trusted built-in
parsers.” Do not shorten it to “sandboxed validation,” “safe hostile-code
execution,” “filesystem isolation,” “network isolation,” or “behavior tested.”

## Release/operator checklist

1. Run `python scripts/preflight.py` against the intended config/state paths.
2. Use trusted-alpha mode, three independent strong secrets, TLS or a private
   overlay, secure cookies, and exactly one coordinator process.
3. Confirm public `/health` is `ok`, `node_enrollment_required` is true, and
   private routes are protected; confirm viewer-less private HTTP and WebSocket
   access is rejected.
4. Configure Uvicorn/reverse-proxy logs not to retain share capability paths,
   idempotency keys, or static/session/attempt credentials.
5. Exercise bootstrap/returning register, idempotence/conflict, one-enrollment
   revoke/rotate/reclaim/restart, label-takeover rejection, session-bound
   poll/result/stream, cross-enrollment settlement rejection, and output/stream
   limits.
6. Exercise lifecycle timeout/cancel/restart, artifact seal/drift/role downloads,
   share scope/expiry/revocation, exact/changed attempt replay, transient and
   permanent terminal persistence failure, and callback/event failure after a
   committed terminal result.
7. Exercise canonical keyed create/replay/conflict, concurrent duplicate
   submission, requester-scope separation, restart replay, and invalid-key
   rejection.
8. Create and verify a backup; rehearse restore into an empty staging state
   directory before relying on it.
9. Run the bounded live multi-node harness, including `terminal_publication` and
   `submission_idempotency`, and the current focused/full test suite; record
   exact revision and results.
10. Keep public pitch disabled unless the operator explicitly accepts the bounded
   demo endpoint's abuse and compute risk.
11. Review generated artifacts before execution. Validation and sealing are not
    a sandbox or content-safety verdict.
12. Exercise greater-than, equal, and lower typed output-capacity matching across
    scheduler qualification, polling, the under-lock recheck, protected
    diagnostics, and shadow candidate capture. Verify the durable attempt still
    stores and enforces the exact server task limit.
13. Exercise every shadow admission/evaluation outcome, deterministic replay,
    restart persistence, health-store failure fallback, and shutdown
    cancellation. Reproduce the documented drop/failure rate from its reported
    numerator and denominator, including an orphan evaluation whose admission
    row was not persisted, and inspect the response for forbidden data.
14. Verify exact digest, missing digest, legacy descriptor, and unreconstructable
    identity diagnostics without changing actual assignment or shadow
    preference. Confirm there is no active evidence mode and no worker
    concurrency.

15. Exercise `auto`, forced `subprocess`, and local `inline`; confirm trusted-
    alpha preflight rejects `inline`, every built-in supports forced subprocess,
    and an isolated failure never falls back inline.
16. Exercise protocol malformed/oversized/identity failures; parser timeout,
    crash, spawn, stdout, memory, environment, descriptor, cancellation, and
    process-tree cleanup cases; and POSIX/Windows-specific expectations.
17. Exercise staging traversal, symlink/reparse, special-file, count/size,
    cleanup, and candidate-isolation cases. Confirm generated Python side
    effects and exceptions are parsed without import or execution.

## Theme 3A branch verification

Final branch gates are pending. Do not convert focused slice results
or historical Theme 2.1 counts into a full-branch claim. Before review, record
the exact ending SHA and results for the focused validator/protocol/staging,
validation-policy, artifact, lifecycle, persistence, and cancellation suites;
the full test suite; Ruff; server import; trusted-alpha harness; restart
recovery; Compose configuration; diff check; and every configured Ubuntu CI
job. Record a separate manual Windows run before making a cross-platform claim.
Report commands not run and every platform-specific skip.

Focused slice evidence available before integration, all from the current
Windows host unless stated otherwise:

- configuration and preflight tests: 72 passed;
- deployment configuration tests: 11 passed, 1 skipped;
- targeted Ruff for configuration/preflight files: passed; and
- protocol tests: 26 passed; staging/artifact/validation focus: 74 passed,
  2 skipped (reported by their respective implementation slices).

The skipped cases have not established the POSIX-only resource and process-group
paths. Configured Ubuntu CI and a complete documented Windows platform run are
both still pending as final branch evidence.

These counts come from different intermediate working-tree states and are not a
release gate.

### Offline checkpoint (August 31, 2026)

The branch was intentionally paused for the operator after the final relative-
artifact-root regression fix. Current checkpoint evidence on Windows:

- validator protocol/staging/process focus before that last path-normalization
  fix: 94 passed, 5 platform-specific skips;
- the new relative-root regression plus the unrelated worker-identity failure
  rerun: 2 passed;
- full-suite attempt before the last path-normalization fix: 1,292 passed,
  8 skipped, 1 failed in the pre-existing concurrent Windows worker-identity
  lock test; its isolated rerun passed;
- an earlier full-suite attempt on the branch passed 1,292 with 8 skips, but it
  predates the last cleanup and relative-root changes and is not the final gate;
- `python -m ruff check .`: passed after the last fix; and
- `git diff --check`: passed after the last fix, with only Git's CRLF conversion
  notices.

Ubuntu/POSIX containment remains unrun locally: the Docker client was present,
but the Linux engine/service was unavailable and could not be started with this
session's permissions. Resume by rerunning the complete suite from the checkpoint
commit, then run server import, trusted-alpha harness, restart recovery, Compose
configuration, and the configured Ubuntu CI jobs before publishing the PR as
ready for review.

## Historical Theme 2.1 branch verification

The working tree based on
`d6d12da176741962611a13f2130097bc880959c5` completed these gates on August 26,
2026:

- `python -m pytest -q`: 1,153 passed, 3 skipped;
- focused capability, evidence, integration, evaluation, attempt-authority, and
  execution-interface suites: 166 passed in their six requested commands
  (27, 40, 31, 11, 42, and 15 respectively);
- `python -m ruff check .`: passed;
- `python -c "import server"`: passed;
- `python scripts/trusted_alpha_harness.py`: passed in bounded profile,
  including 63 focused tests across 43 selectors;
- `python scripts/restart_recovery.py`: 17/17 checks passed;
- `python scripts/capability_evidence_eval.py`: passed with report version 2,
  20 identity-eligible fixture scopes, zero blocked fixture scopes, and zero
  invariant failures;
- `docker compose config`: passed; and
- `git diff --check`: passed, with only Git's platform line-ending notices.

Use the final branch `HEAD` as the ending revision; do not substitute these
results for a later changed tree without rerunning the affected gates.

The intended release state is a **small private trusted alpha**: auditable work
across computers whose operators are known and trusted, with explicit evidence
and honest recovery boundaries—not an anonymous compute network.
