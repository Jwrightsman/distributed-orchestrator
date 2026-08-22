# Trusted-Alpha Integrity and Delivery Sprint

_Bounded backend/protocol hardening authorized August 21, 2026. Documentation
snapshot updated August 22, 2026._

## Objective

Move Mycelium from an address-readable, in-memory-attempt prototype to a small
private trusted alpha with durable result authority, truthful lifecycle and
assurance, private reads, explicit sharing, and safe artifact delivery.

The intended claim is narrow:

> Mycelium is suitable for a small private trusted alpha, while its
> shared-secret identity, process-local queue, lack of generated-code
> sandboxing, and lack of permissionless-network defenses remain explicit.

Do not describe this sprint as public-network readiness.

## Branch and base

- Branch: `codex/trusted-alpha-integrity-v1`
- Base: `origin/master` at `e6d6888`
- Previous completed baseline: execution strategy protocol v1
- Merge policy: review and merge explicitly; do not auto-merge this branch

The sprint prompt required a baseline suite, Ruff, server import, Compose
configuration, and conditional Docker build. A trustworthy exact baseline was
not present in this docs agent's handoff context, so this file does not invent
one from an older sprint count. The final branch report must record the actual
commands and outputs it observed.

## Scope boundaries

Delivered work is limited to integrity, lifecycle, assurance, validation,
candidate isolation, artifacts, access, sharing, privacy defaults, project
honesty, telemetry, contribution semantics, interfaces, tests, and
documentation.

Explicitly not delivered:

- map, research, consensus, debate, or additional personas;
- blockchain, tokens, wallets, transfers, or real-money settlement;
- marketplace, federation, peer-to-peer networking, or model sharding;
- per-node public-key identity;
- full accounts or multi-tenancy;
- generated-code sandboxing;
- major visual UI, layout, CSS, components, or animations.

Claude Code owns visual follow-up. Backend route availability necessarily
changes when viewer auth is configured, but this sprint does not build a visual
login, artifact browser, share page, or redesigned run page.

## Delivered integrity model

### 1. Server-authoritative attempts

Protocol strictness comes from the durable server-issued attempt. A v1 worker
cannot omit `contract_version`, attempt id, nonce, execution id, unit id, or
unit kind to reach a legacy path. URL task, assigned node, lease, state, and all
binding fields must match.

Assignment persists one active attempt before handout. Raw nonces are not
stored. Result settlement uses one SQLite write transaction to validate,
conditionally settle, persist the canonical result hash and replay response,
insert the immutable accepted receipt, and insert a unique compute contribution
when earned.

The dispatcher consumes only an accepted receipt matching task, execution,
unit, kind, and contract. An exact replay returns the original response after
database reopen without paying twice. A changed replay fails.

ADR: [docs/adr/0003-attempt-authority.md](docs/adr/0003-attempt-authority.md).

### 2. Rejection and quarantine

Unknown, queued-but-unleased, expired, reclaimed, cancelled, superseded,
interrupted, and wrong-bound worker submissions do not enter operational
execution. They emit rejection and may enter a separate 500-row quarantine with
a reason, output hash, and at most 4 KiB preview.

Quarantine never wakes a dispatcher, becomes final output, updates normal node
success state, earns points, or emits normal attempt completion. The historical
result dictionary is a post-settlement compatibility mirror, not authority.

### 3. Truthful lifecycle

`timeout_seconds` is one total deadline beginning at canonical queueing. The
strategy, local calls, worker waits, validation, review/revision, and artifact
finalization consume the same remaining budget. Worker leases cannot outlive
the execution deadline.

`POST /v1/executions/{id}/cancel` records cancellation, signals local work,
removes queued worker units, cancels active attempts, rejects late results,
persists terminal cancellation, and emits an event.

Coordinator startup moves non-resumable canonical executions and legacy jobs
from queued/running to retryable `interrupted`, with reason, restart marker, and
timestamp. Active worker attempts are interrupted. Reconciliation is
idempotent. This is truthful loss reporting, not queue resumption.

### 4. Lifecycle versus assurance

Canonical results separate:

- `lifecycle_status`: queued, running, completed, failed, cancelled,
  interrupted;
- `validation_outcome`: passed, failed, partial, not_run;
- `assurance_level`: unverified, structural, deterministic, model_judged.

The compatibility `status` field remains additive; lifecycle-completed work
whose validation outcome did not pass projects as `unverified`. Every result
names what ran, passed, failed, and was not checked. Polling uses lifecycle;
correctness claims use evidence.

ADR: [docs/adr/0004-lifecycle-vs-assurance.md](docs/adr/0004-lifecycle-vs-assurance.md).

### 5. Validator semantics

- Output contracts impose an immutable AND floor.
- Explicit validators may add but not remove floor checks.
- `require_all` controls explicit required validators: all when true, any when
  false.
- JSON Schema draft 2020-12 is itself validated at request time.
- File manifests compare exact normalized relative paths, not basenames.
- Absolute, drive, backslash, traversal, dot/empty, and duplicate normalized
  paths are rejected.
- Exact artifact count, single-artifact exactness, maximum count, and supported
  file-extension format checks are enforced.
- Nonempty, extraction, manifest, artifact contract, valid JSON, and supported
  code parsing are structural; JSON Schema is deterministic contract
  conformance. These do not prove general behavioral correctness.
- Auto selection does not treat extraction or parsing as correctness proof.

### 6. Ensemble isolation and selection

Candidate generation, directory creation, materialization, extraction, and
validation have per-candidate failure boundaries. Remaining candidates
continue. `validated_score` orders by required-policy acceptance, assurance,
meaningful score, lower latency, and stable candidate id. `first_valid` means
first acceptable completion. Output length is not a quality tie-breaker.

### 7. Artifacts

Canonical `ArtifactManifestV1` and `ArtifactEntryV1` provide relative path,
media type, size, SHA-256, optional source candidate/unit, and timestamp without
publishing a root.

Authenticated APIs:

```text
GET /v1/executions/{id}/artifacts
GET /v1/executions/{id}/artifacts/{relative_path:path}
GET /v1/executions/{id}/download
```

The shared registry covers DAG `output/` and ensemble
`execution_artifacts/`, rejects traversal and symlinks after resolution,
enforces file/per-file/aggregate quotas, refreshes and rehashes on access,
streams files, prepares ZIPs on temporary disk, protects active roots, and
prunes terminal registered roots by age/space. Details:
[docs/ARTIFACTS.md](docs/ARTIFACTS.md).

### 8. Private reads and explicit shares

`viewer_key` is separate from `node_secret` and `pitch_key`. It works as
`X-Viewer-Key`, exact Bearer token, or a signed expiring HttpOnly cookie. Static
key and signature checks use constant-time comparison. Middleware protects
everything not deliberately public or separately protocol-authenticated.

When the viewer key is empty, local-development compatibility fails open but
startup and `/health` warn that private routes are exposed.

Share APIs:

```text
POST /v1/executions/{id}/shares
GET /v1/shares/{token}
DELETE /v1/executions/{id}/shares/{share_id}
```

Tokens are random bearer capabilities whose hashes, expiry, revocation,
artifact permission, node-redaction, and candidate-detail flags are durable.
Public execution projection is allowlist-based. Invalid, expired, and revoked
tokens share a `404` response. Tokens do not authorize another execution or a
private route. Details: [docs/ACCESS_CONTROL.md](docs/ACCESS_CONTROL.md).

### 9. Privacy-safe defaults and public profile

New canonical calls default to local placement, local-only confidentiality, and
no remote consent. Remote-capable requests must set a non-local confidentiality
class and `remote_dispatch_consent=true`. Legacy adapters record adapter-owned
consent only to preserve documented historical behavior.

The optional keyless endpoint accepts only task text. Caller execution knobs are
rejected. Its server profile is direct/one candidate/concurrency one/local/
local-only/no project/120 seconds/64 KiB, with two requests per source per hour,
one active request per source, three active public jobs globally, and one global
inference slot. It returns a one-hour redacted share and cannot use worker
nodes.

### 10. Project memory and telemetry

DAG retains project-memory support. Ensemble/direct reject `project_id` rather
than silently ignoring it until selected-result-only updates exist.

Results add requested, planned, and observed placement; local/distributed unit
counts; fallback, attempt, retry, and reassignment counts; and recorded remote
consent. Compatibility placement fields remain for older clients.

### 11. Contribution semantics

SQLite is authoritative for concurrent-safe, idempotent contribution records;
`ledger.json` and `credits` are compatibility projections/names. Accepted bound
worker compute can earn `basis=compute_contribution` even when its candidate is
not selected or the final result is not validated. Records explicitly state
`points_are_monetary=false`.

No token, money, wallet, transfer, or payment system was added.

### 12. Interface alignment

- `/health` publishes `nodes_online` as an integer; detailed nodes are private.
- `/events` is a flat schema whose metadata and event fields are peers.
- Legacy async jobs transition through `running`.
- The worker prints `DONE` only after the result POST is accepted.
- Error-report failure does not hide the original worker exception.
- CLI and MCP can send `VIEWER_KEY` for private reads and `PITCH_KEY` for
  submission.

## SQLite migrations and durable records

Idempotent additive schemas include:

| Table | Purpose |
| --- | --- |
| `executions` | canonical request/result, lifecycle, assurance, consent, interruption metadata |
| `attempts` | active/terminal server-owned attempt authority and replay response |
| `accepted_result_receipts` | immutable dispatcher-consumable bound results |
| `result_quarantine` | bounded diagnostic-only rejected output |
| `contributions` | unique non-monetary contribution records |
| `artifact_roots` / `artifact_entries` | internal roots and public-safe manifest metadata |
| `execution_shares` | token hashes, expiry, revocation, and capability flags |
| `jobs` additions | running/interrupted/restart/retryability metadata |

The process-local worker queue, node registry, circuit breaker, and execution
coroutines are intentionally not represented as durable scheduling.

## Compatibility

- `/pitch`, `/pitch/async`, and `/pitch/distributed` remain adapters over the
  canonical service.
- A task-only legacy body remains valid.
- `/pitch` remains local by default; async and distributed adapters preserve
  their documented placement behavior with recorded consent.
- `direct` remains one-candidate ensemble and no new strategy was added.
- Legacy `status`, `placement_selected`, `credits`, `ledger.json`, job shapes,
  events, and artifact path fields remain only where safe compatibility needs
  them; canonical and public surfaces use the new authoritative fields.

## Documentation and Claude inputs

Normative/operational files:

- `docs/PROTOCOL.md`
- `docs/ARCHITECTURE.md`
- `docs/THREAT_MODEL.md`
- `docs/ACCESS_CONTROL.md`
- `docs/ARTIFACTS.md`
- `docs/adr/0003-attempt-authority.md`
- `docs/adr/0004-lifecycle-vs-assurance.md`
- `README.md`, `HANDOFF.md`, and `MASTER_PLAN.md`

Claude may build visual controls against `ExecutionRequestV1`,
`ExecutionResultV1`, `ValidationSummaryV1`, `ArtifactManifestV1`,
`PublicExecutionShareV1`, the viewer-session API, artifact APIs, share APIs, and
the `/health` protection flag. It must not call structural output “working,” use
old run ids as public permalinks, reveal server paths, call shared-secret nodes
cryptographically verified, or describe contribution points as payment.

## Verification record

This documentation commit is intentionally isolated from concurrent backend
integration. Documentation checks run for this commit:

- all 11 assigned documentation files exist;
- every relative Markdown link in those files resolves locally;
- all 14 required trusted-alpha claim topics are present;
- the targeted stale-claim scan passed;
- no trailing whitespace or accidental absolute Windows path was found;
- `git diff --check` passed for the tracked documentation diff.

No Markdown linter or `codespell` module was installed, so neither is claimed.
The final sprint owner must report exact integrated results for:

```text
python -m pytest -q
python -m ruff check .
python -c "import server"
docker compose config
```

Run Docker build checks only with a working daemon. Do not convert an unavailable
daemon into a passing build claim. Do not copy a test count from
`SPRINT_STRATEGY_PROTOCOL.md` or the old README.

## Remaining known limitations

- One shared node secret; no per-node public-key identity or individual
  cryptographic revocation.
- Viewer and pitch auth are instance-wide shared secrets, not accounts or roles.
- No built-in HTTPS.
- Process-local queue, nodes, breaker state, and running coroutines; restart
  interrupts rather than resumes.
- One coordinator and no failover.
- No generated-code sandbox, content scanning, or enforced network policy.
- Structural and deterministic contract checks do not prove arbitrary behavior.
- No Sybil resistance, hardware attestation, confidential computing, or
  permissionless settlement.
- Share holders can copy data before revocation; retention is not secure erasure.

## Completion rule

The sprint is not complete merely because the documentation exists. Completion
requires the integrated branch to pass the final checks the task owner actually
runs, to contain no visual/CSS work, to preserve the active-attempt invariant,
and to report anything incomplete without softening it into a future promise.
