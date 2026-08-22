# Handoff — Mycelium trusted-alpha integrity

_Updated August 22, 2026._

## Read these first

1. `MASTER_PLAN.md` — current direction and scope boundaries
2. `SPRINT_TRUSTED_ALPHA_INTEGRITY.md` — sprint delivery map and verification
   requirements
3. `docs/PROTOCOL.md` — normative client and worker contract
4. `docs/ARCHITECTURE.md`, `docs/ACCESS_CONTROL.md`, and
   `docs/ARTIFACTS.md` — backend boundaries and APIs
5. `docs/THREAT_MODEL.md` — what is and is not defended
6. `docs/adr/0003-attempt-authority.md` and
   `docs/adr/0004-lifecycle-vs-assurance.md` — integrity decisions
7. `CLAUDE.md` — repository working rules
8. `ROADMAP.md` — reference only, not a work queue

`SPRINT_STRATEGY_PROTOCOL.md` and `SPRINT_PHASE2.md` are historical records.
Do not copy their old test counts or priority lists into a current claim.

## Human context

Jett has no programming background. Make technical calls, explain the decisions
he must act on, and warn before anything network-facing. Do not install or join
Mycelium on a machine without that machine owner's explicit informed consent.

The trusted-alpha sprint is a bounded backend exception to the feature freeze.
It does not authorize more strategies, visual UI work, marketplace or token
features, federation, model sharding, accounts, or a generated-code sandbox.

## Branch

- Branch: `codex/trusted-alpha-integrity-v1`
- Base: `origin/master` at `e6d6888`
- Merge: review explicitly; do not auto-merge

The final commit list, test results, Ruff result, server import, Compose result,
and conditional Docker result belong in the parent task's final report after all
concurrent implementation commits are integrated. Do not freeze a transient
test count in this handoff.

## Delivered architecture

### Worker result authority

- The server persists an active SQLite attempt before handing a unit to a
  worker.
- Attempt authority binds task, execution, unit, kind, assigned node, contract,
  nonce digest, state, and lease. Strictness never comes from the worker's
  submitted version.
- V1 omissions and mismatches reject; there is no downgrade to a legacy path.
- Settlement atomically records the terminal attempt, immutable accepted
  receipt, replay response/hash, and unique compute contribution.
- Exact replay survives restart without paying twice. Changed replay fails.
- The dispatcher consumes only a receipt matching its execution and unit.
- Unknown, unleased, expired, reclaimed, cancelled, interrupted, and wrong-bound
  output goes only to bounded diagnostic quarantine, never operational results.

### Lifecycle and assurance

- `timeout_seconds` is one total deadline beginning at canonical queueing.
- Cancellation removes queued work, cancels active attempts, signals local
  execution, persists terminal cancellation, and rejects late results.
- Startup makes non-resumable canonical executions, legacy jobs, and active
  attempts truthfully interrupted/retryable.
- Lifecycle, validation outcome, and assurance are separate fields. The old
  `status` is a compatibility projection.
- Validation summaries name checks run, passed, failed, and not run. Structural
  checks are not marketed as behavioral proof.

### Validation and ensemble

- Output contracts impose validator floors explicit policy cannot remove.
- `require_all` has defined semantics for explicit required validators.
- JSON Schema draft 2020-12 is validated at request time.
- Manifest paths, exact counts, supported formats, and parser coverage are
  checked honestly.
- Candidate generation, materialization, extraction, and validation failures are
  isolated. Winner ordering does not use output length.

### Privacy, artifacts, and sharing

- Canonical defaults are local, local-only, and no remote consent. Remote-
  capable canonical calls require explicit recorded consent.
- Ensemble/direct reject `project_id`; DAG remains the supported memory path.
- `viewer_key` is separate from `pitch_key` and `node_secret`, and works as a
  header, Bearer token, or signed expiring HttpOnly session.
- Middleware is deny-by-default when configured. Empty viewer auth is a warned
  fail-open local-development mode.
- Complete artifacts use authenticated relative-path manifest/file/ZIP APIs with
  root confinement, symlink rejection, SHA-256, quotas, streaming, and
  active-aware retention across both storage roots.
- Explicit shares use durable token hashes, expiry, revocation, node redaction,
  candidate-detail scope, and independent artifact permission.
- The public share representation is allowlist-based and contains no server
  path, project/job id, attempt secret, credit detail, or private telemetry.

### Public profile, telemetry, contribution, and interfaces

- Keyless public pitch is off by default and accepts only task text. The server
  forces one local direct candidate, short deadline/output cap, no project, and
  compute-aware admission limits.
- Results record requested, planned, and observed placement, unit counts,
  fallback, attempts, retries, reassignments, and consent.
- SQLite contribution records are concurrent-safe and idempotent. Compute
  points mean accepted compute, not selected output, correctness, or money.
- `/health` uses `nodes_online: integer`; `/events` is flat; jobs pass through
  running; worker rejection is reported as failure; error-report failure does
  not hide the original exception.

## Durability boundary

| Durable | Process-local |
| --- | --- |
| canonical execution snapshots | worker queue |
| legacy job records | connected-node registry |
| active/terminal attempts | breaker and waiting-node state |
| accepted receipts and replay responses | running coroutines and dispatcher waits |
| contribution records | in-memory receipt cache |
| share records/token hashes | live WebSocket connections |
| artifact root/manifest metadata plus retained files | active model call process state |

Restart reconciliation makes process-local loss truthful. It does not resume
lost work or provide failover.

## Claude visual handoff

This branch intentionally does not modify templates, CSS, page layout,
dashboard components, animations, or visual marketing design. Claude may safely
build against:

- `ExecutionRequestV1` and `ExecutionResultV1`;
- `lifecycle_status`, `validation_outcome`, `assurance_level`, and
  `validation_summary`;
- `ArtifactManifestV1` and the canonical artifact routes;
- `CreateExecutionShareV1`, `PublicExecutionShareV1`, and the share routes;
- `POST/DELETE /v1/viewer/session`;
- public `/health.private_routes_protected` and `warnings`;
- requested/planned/observed placement and remote-consent fields.

Safe UI language:

- “Accepted from an active server-issued attempt,” not “worker identity
  cryptographically verified.”
- “Lifecycle completed” separately from “validation passed.”
- “Structural checks passed” or “JSON Schema conformance passed,” not “working
  code” without actual behavioral evidence.
- “Private when viewer auth is configured,” and show the health warning when it
  is not.
- “Shared with an explicit expiring/revocable link,” not an old public run id.
- “Artifact available through authenticated download,” never a filesystem path.
- “Compute contribution points,” never payment or proof of correctness.

The visual client must handle `401` on old dashboard/history/run routes and
`4401` on the event WebSocket. It must not assume `/nodes` is part of public
health; public health carries only the node count.

## Operational checklist before a reachable deployment

1. Configure independent `node_secret`, `pitch_key`, and `viewer_key`.
2. Use Tailscale or TLS; the application does not provide HTTPS.
3. Set `viewer_cookie_secure=true` behind HTTPS.
4. Verify `/health` reports `private_routes_protected: true`.
5. Verify private HTTP returns `401` without a viewer credential and
   `/ws/events` closes with `4401`.
6. Exercise canonical local submission, explicit remote-consent rejection and
   acceptance, cancellation, restart reconciliation, artifact download, share
   expiry/revocation, and rejected worker settlement.
7. Keep public pitch disabled unless its fixed local profile and resource budget
   are intentionally accepted.
8. Review generated artifacts before execution. No validator is a sandbox.
9. Verify the deployed build fingerprint rather than assuming a rebuild landed.

## Claims that remain prohibited

Do not claim public-network readiness, trustlessness, permissionless nodes,
cryptographic worker identity, confidential compute, enforced no-network
execution, sandboxed generated code, Sybil resistance, durable queue resume,
multi-user authorization, monetary credits, or proof that arbitrary generated
output is correct.

The intended handoff state is a **small private trusted alpha** with honest
integrity and access boundaries. Historical operational detail removed from this
file remains available in Git history if needed; it must not override the
current protocol documents.
