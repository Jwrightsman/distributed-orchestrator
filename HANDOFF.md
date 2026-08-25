# Handoff — Mycelium Durable Node Enrollment

_Updated August 25, 2026._

## Read these first

1. `SPRINT_DURABLE_NODE_ENROLLMENT.md` — current Theme 2A scope, evidence, and residuals
2. `MASTER_PLAN.md` — current direction and freeze boundary
3. `docs/PROTOCOL.md` — normative execution/client/worker contract
4. `docs/ACCESS_CONTROL.md` and `docs/ARTIFACTS.md` — authority and delivery APIs
5. `docs/THREAT_MODEL.md` — defended and undefended boundaries
6. `docs/DEPLOY.md`, `docs/TRUSTED_ALPHA_RUNBOOK.md`, and
   `docs/OPERATIONS.md` — deployment and recovery procedure
7. `docs/adr/0010-durable-enrollment-identity.md` — Theme 2A identity decision
8. `docs/audits/2026-08-23-comparative-architecture-audit.md` — historical,
   non-normative architecture research
9. `CLAUDE.md` and `AGENTS.md` — repository and consent rules

`SPRINT_DURABLE_EXECUTION_TRUTH.md`, `SPRINT_TRUSTED_ALPHA_RC1.md`,
`SPRINT_STRATEGY_PROTOCOL.md`, `SPRINT_TRUSTED_ALPHA_INTEGRITY.md`, and
`SPRINT_PHASE2.md` are historical records. Do not copy their old test counts,
status lists, or interface assumptions into a current claim.

## Human and scope context

Jett has no programming background. Make technical calls, explain the choices
he must act on, and warn before anything network-facing. Never install, join, or
run Mycelium on a machine without that machine owner's explicit informed
consent.

Theme 2A is a bounded durable-enrollment change on the trusted-alpha backend. It
does not authorize typed resource descriptors (Theme 2B), permissionless
admission, public-key infrastructure, attestation, Sybil resistance, workflow
resumption, coordinator HA, new strategies, accounts, marketplace/token
features, federation, model sharding, generated-code sandbox, or unrelated UI
redesign. Frontend work may consume the contract below without changing its
meanings.

## Branch and review

- Branch: `codex/theme-2a-durable-node-enrollment`
- Base: `origin/master` at `9979f681369fa69cfb35133f17e07ce3aac54abf`
- Merge: review explicitly; do not auto-merge
- Current sprint record: `SPRINT_DURABLE_NODE_ENROLLMENT.md`

The final integrator must record the current commit range and current full-suite,
Ruff, import, preflight, and deployment checks. Historical counts in sprint
records are evidence for those exact revisions, not a substitute for the final
branch run.

## Theme 2A architecture

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
  and restore cover durable state with online SQLite snapshotting, a versioned
  checksum manifest, validate-before-mutate restore, and rollback.

### SQLite, attempt authority, and contribution

- Production `events.db` access uses the common WAL, foreign-key, 10-second busy
  timeout, `synchronous=NORMAL`, bounded retry, and migration-lock policy.
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
| Assurance level | `assurance_level` plus validation summary/evidence. Describe the exact checks; never collapse it to a generic verified badge. |
| Post-hoc verification | `posthoc_verification_status`, timestamps, agreement, and reason. RC1 trusted-alpha is disabled; show that plainly without changing original assurance. |
| Canonical submission replay | `Idempotency-Replayed` appears only on keyed canonical POST responses. `false` means created; `true` means an existing execution was returned. Never label it exactly-once execution or workflow resumption. |
| Share metadata | Create returns plaintext token once. List shows share ID, create/expiry/revocation/last-access times and artifact/node/candidate flags without token. Support revoke-one/revoke-all and explain that copied content cannot be recalled. |

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
attestation, exactly-once external side effects, open-mode peer identity, or
proof that arbitrary generated output is correct.

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

The intended release state is a **small private trusted alpha**: auditable work
across computers whose operators are known and trusted, with explicit evidence
and honest recovery boundaries—not an anonymous compute network.
