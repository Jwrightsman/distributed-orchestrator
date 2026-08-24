# Durable Execution Truth Sprint

_Theme 1 implementation record opened August 23, 2026._

## Objective

Establish two bounded invariants for the canonical execution service:

1. authoritative execution state is committed durably before live-cache,
   lifecycle-event, callback, legacy-mirror, response, artifact, or share
   publication; and
2. retries of keyed `POST /v1/executions` submissions converge on one
   requester-scoped durable execution identity.

This sprint does not add workflow resumption, a durable scheduler, coordinator
high availability, a broker, a general outbox, a workflow engine, a marketplace,
DHT discovery, or permissionless networking.

## Branch and starting point

- Branch: `codex/theme-1-durable-execution-truth`
- Base: `origin/master` at `8a9a09a2920dc376c60b7e63064df61e5c0602d3`
- Audit archive commit: `274c3c5`
- Review: do not auto-merge or push directly to the default branch

The comparative audit is archived at
[`docs/audits/2026-08-23-comparative-architecture-audit.md`](docs/audits/2026-08-23-comparative-architecture-audit.md).
It is historical and non-normative; implementation changes in this sprint do
not rewrite its findings.

## Implementation map

### Durable publication

- Required queued, running, terminal, cancellation, and metadata snapshots use
  one typed, finite-retry commit boundary.
- Live snapshots, normal lifecycle events, start/completion callbacks, and
  compatibility mirrors follow durable commitment.
- Permanent required-persistence failure leaves the last durable snapshot
  authoritative and suppresses normal terminal publication.
- Active HTTP boundaries return a sanitized `503` rather than claiming an
  uncommitted transition.
- Terminal artifact delivery, including through a share, requires a committed
  terminal execution. Current sealed roots additionally require an exact
  manifest-hash binding; historical `legacy_live` compatibility stays labeled
  and freshly rescanned.
- Legacy run/history/gallery/status/try/CLI/download/demo readers resolve root
  ownership before mutable log fields and keep staged or restart-reconciled
  terminal files hidden.
- DAG project-memory iterations publish after the normal terminal event and
  before completion callbacks; terminal persistence failure leaves memory
  unchanged.
- Redundant terminal request/result cache entries are evicted only after their
  post-commit observers finish; later reads reload the authoritative SQLite row.
- Pipeline contribution records use fixed labels only. Startup idempotently
  redacts historical free-form task/details fields in SQLite and regenerates
  the JSON compatibility projection without changing contribution identity or
  points.
- Pipeline events pass through a central structural allowlist before memory,
  SQLite, broadcast, or replay. Startup idempotently redacts historical
  payloads; generated token text remains live-stream-only.

Decision: [ADR 0009](docs/adr/0009-durable-terminal-commit-before-publication.md).

### Idempotent canonical submission

- `POST /v1/executions` accepts an optional validated `Idempotency-Key`.
- The validated request is hashed canonically with defaults included.
- Requester scope and key values are stored only as domain-separated SHA-256
  digests.
- One immediate SQLite transaction creates both the queued execution and its
  mapping, or returns replay/conflict without scheduling duplicate work.
- Open-mode peer scoping is best-effort development behavior, not user identity.
- Mappings are retained indefinitely for the trusted alpha.

Decision: [ADR 0008](docs/adr/0008-idempotent-canonical-execution-submission.md).

## Persistence and compatibility

The additive `execution_submissions` table is created idempotently alongside
the existing `executions` table. It stores `requester_scope_hash`,
`idempotency_key_hash`, `request_hash`, `execution_id`, and `created_at`; its
composite primary key is `(requester_scope_hash, idempotency_key_hash)`, its
execution ID has an `ON DELETE RESTRICT` foreign key to `executions`, and an
execution-ID index supports reverse lookup. Existing databases keep all current
rows and receive the new empty mapping table on migration. Existing unkeyed
submissions remain create-every-time. The execution protocol version remains
`1`; the HTTP header does not change the protocol model.

Legacy HTTP, CLI, and MCP submission idempotency are intentionally deferred.
Their service and persistence dependencies remain compatible, and later work
may adopt the reusable primitives through an explicit interface design.

## Verification record

These results were run against the final working tree before the Theme 1
commits. They are current evidence, not copied release counts.

| Check | Result |
| --- | --- |
| `python -m pytest -q tests/test_durable_execution_truth.py` | 22 passed in 1.95s |
| `python -m pytest -q tests/test_execution_submission.py` | 29 passed in 1.38s |
| `python -m pytest -q tests/test_execution_lifecycle.py` | 16 passed in 8.14s |
| `python -m pytest -q tests/test_execution_persistence.py` | 7 passed in 0.40s |
| `python -m pytest -q tests/test_execution_interfaces.py` | 15 passed in 2.64s |
| `python -m pytest -q tests/test_legacy_publication.py` | 11 passed in 2.53s |
| `python -m pytest -q tests/test_event_privacy.py tests/test_ledger.py` | 11 passed in 2.00s |
| `python -m pytest -q` | 882 passed, 2 skipped in 159.14s |
| `python -m ruff check .` | Passed |
| `python -c "import server"` | Passed |
| `python -m py_compile execution/service.py execution/persistence.py execution/idempotency.py execution/publication.py server_state.py routes_executions.py routes_events.py scripts/soak_test.py` | Passed |
| `python scripts/trusted_alpha_harness.py` | 55 passed; harness passed one iteration with 35 selectors in 16.2s |
| `python scripts/restart_recovery.py` | 17/17 checks passed |
| `python scripts/soak_test.py` | Passed: 20/20 measured pitches plus warmup; 0.36 MB/pitch post-GC; no retained execution maps or orphaned tasks |
| `docker compose config` | Passed |
| `node --check templates/_dashboard.js` | Passed |
| `git diff --check` and `git diff --check 274c3c5..HEAD` | Passed for the Theme 1 working tree/commits; Git reported only expected LF-to-CRLF working-copy notices |
| Modified-Markdown relative-link and fence check | Passed for 13 files |

### Diagnostic iterations

- Early restart-harness attempts exposed a client timeout shorter than the
  `/health` Ollama probe, then an optional blank GPU field; the harness now
  waits through the bounded probe and submits `null`, and the final run is
  17/17.
- The first soak attempt hit the same readiness mismatch and left its verified
  child process listening on port 8078; that exact process was terminated and
  startup now fails with bounded cleanup. Subsequent runs exposed cyclic-GC
  noise plus genuinely unbounded terminal request/result maps. The maps now
  evict after observers, the 0.5 MB/pitch threshold is unchanged, and two final
  runs passed at 0.33 and 0.36 MB/pitch.
- `git diff --check 8a9a09a2920dc376c60b7e63064df61e5c0602d3..HEAD`
  reports only the archived audit's original two-space Markdown hard breaks.
  They are deliberate source formatting from the attachment and remain intact
  under the preservation requirement. Both Theme 1 commits pass `--check`.

## Residual limitations

- Scheduler queues, dispatcher waits, background tasks, and node sessions are
  still process-local.
- Restart marks non-resumable queued/running executions `interrupted`; it does
  not restart them.
- Idempotency prevents duplicate canonical submission; it does not guarantee
  exactly-once external model, worker, callback, or artifact side effects.
- All configured pitch-key holders share one requester scope. Open-mode peer
  addresses are not durable user identity.
- There is no coordinator failover, distributed transaction, external event
  delivery guarantee, or automatic replay of model side effects.
- Submission mappings have no TTL during trusted alpha and therefore grow with
  keyed submissions.
- Legacy jobs retain their documented seven-day in-memory window. The final
  default Windows soak passed, but post-fix 120-pitch and Linux reruns remain
  outstanding before making a long-run cross-platform memory claim.

## Completion rule

This sprint is complete only when the runtime, persistence, API, deterministic
fault/concurrency tests, harness labels, ADRs, and current documentation agree;
the final verification table contains results actually run on the final branch;
and a complete diff review finds no secret logging, duplicate scheduling,
publication-before-commit path, or destructive migration.
