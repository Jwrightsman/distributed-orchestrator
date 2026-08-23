# Trusted-Alpha Release Candidate 1 Sprint

_Release-engineering and operational-integrity sprint completed August 23,
2026._

## Objective and release claim

Prepare Mycelium for one correctly protected, single-process coordinator and an
invited group of trusted worker-node operators. RC1 makes deployment mistakes
visible, prevents live node-label/session collisions, standardizes concurrent
storage, enforces server-owned output budgets before settlement, and gives
terminal artifacts a role-scoped immutable local baseline.

The release claim is deliberately narrow:

> Mycelium is prepared for a small private trusted alpha using one protected
> coordinator and invited worker nodes. Its queue remains process-local, node
> admission still uses a shared secret, generated code is not sandboxed, and
> permissionless participation remains out of scope.

This sprint did not add an execution strategy, public-key node identity,
multi-process scaling, accounts, payments/tokens, marketplace/federation,
peer-to-peer transport, model sharding, generated-code sandboxing, or broad
visual UI work.

## Branch, base, and evidence rule

- Branch: `codex/trusted-alpha-rc1`
- Base: `origin/master` at `fd6fa29`
- Merge policy: explicit review; do not auto-merge
- Integrated code/test documentation checkpoint before this sprint record:
  `dd93669`

Test counts below belong to the stated integrated revision and commands. They
must not be copied into a later release report without rerunning that revision.
The docs-only closing commit does not change runtime behavior.

## Recorded verification

### Baseline

The recorded pre-RC1 suite on `fd6fa29` passed 697 tests with 1 skipped in
103.38 seconds. Bare `ruff` was not on `PATH`, but
`python -m ruff check .`, server import, and Compose configuration passed. WSL
Bash and the Docker daemon were unavailable; shell syntax and image-build checks
were not reported as passed at baseline.

### Final integrated branch

| Check | Result |
| --- | --- |
| `python -m pytest -q` | 806 passed, 2 skipped in 177.53 seconds |
| `python -m ruff check .` | passed |
| `python -c "import server"` | passed |
| `docker compose config` | passed |
| Git Bash `bash -n deploy.sh install.sh` | passed |
| Docker image build/start | not run; Docker daemon unavailable |
| bounded live multi-node harness plus 44 focused tests | passed in 18.3 seconds |
| nightly harness, one iteration, plus 53 focused tests | passed in 22.7 seconds |

The bounded harness is committed in `ef5a567`. CI runs the no-Ollama bounded
path. A scheduled/manual workflow exercises higher concurrency/restarts and
related focused coverage. Neither result is evidence of live Ollama model
quality or WAN performance.

## Delivered implementation map

### 1. Three-authority deployment and preflight

- `viewer_key`, `pitch_key`, and `node_secret` are independent instance-wide
  authorities. Deployment generates all three, preserves valid values on rerun,
  upgrades historical two-key config without replacing it, writes atomically,
  keeps restrictive permissions, and never prints values.
- `deployment_mode` is `local` or `trusted_alpha`; local is the compatibility
  default. Trusted-alpha startup/preflight fails closed on missing, weak, or
  repeated secrets; malformed config; incoherent config/state/output/artifact
  paths; non-writable storage; incoherent secure-cookie/HTTPS intent; enabled
  public pitch without acknowledgement; or unavailable coordinator ownership.
- `scripts/preflight.py` provides human and secret-safe JSON output. Deployment
  uses it and requires both `status=ok` and
  `private_routes_protected=true`.
- Docker runs one Uvicorn worker, includes operational scripts, and persists the
  state directory. Documentation distinguishes loopback development, private
  overlay trusted alpha, and TLS reverse-proxy deployment.

### 2. Explicit distributed CLI consent

- Local remains the default. Placement, remote consent, confidentiality, and
  approved node IDs are explicit flags.
- Remote-capable distributed/auto requests fail before inference without
  `--allow-remote`; approved-node confidentiality requires an allowlist; and
  incoherent local-only/distributed combinations fail concisely.
- Strategy and placement remain independent across DAG, ensemble, and direct.
  The CLI warns that assigned worker operators can read their unit prompts.
- Existing local CLI calls and legacy adapters retain documented compatibility.

### 3. Server-issued node sessions and statistics

- `node_secret` remains shared admission. Registration returns normalized
  `node_id`, non-secret `session_id`, one-time plaintext `session_token`, and
  start/expiry state; only a SHA-256 digest remains server-side.
- Poll, result, stream/token, heartbeat, and drain calls require the current
  `X-Node-Session` as well as configured node admission. Handed-out attempts bind
  `assigned_session_id`; attempt ID, nonce, lease, node, execution, unit, kind,
  and contract checks remain mandatory.
- Exact-token registration is idempotent. Different live claimants receive
  `409`; stale/expired IDs can be reclaimed; old sessions/work reject; sessions
  invalidate on restart; and the stock worker re-registers automatically.
- Registration fields/capabilities are bounded and arbitrary polling no longer
  creates an admitted placeholder.
- Private state separates session task/point counters from lifetime contribution
  totals derived from durable records. Neither is called correctness or payment.

Decision: [ADR 0005](docs/adr/0005-node-registration-sessions.md).

### 4. One coordinator and one SQLite policy

- A cross-platform nonblocking OS lock is acquired for the state directory
  before migrations, reconciliation, or background work and held for process
  lifetime. A second owner fails clearly. Docker/multi-worker configuration is
  kept at one worker; private health exposes a safe instance/mode/lock view.
- Production `events.db` users use one connection/transaction utility with WAL
  where supported, foreign keys on, a 10-second busy timeout,
  `synchronous=NORMAL`, bounded retry for transient lock/busy errors, serialized
  migrations, row handling, and explicit immediate transactions at integrity
  boundaries.
- Concurrency tests cover store initialization and overlapping attempt, event,
  execution, artifact, share, and contribution writes without unexplained lock
  failures or partial integrity state.

Decision: [ADR 0006](docs/adr/0006-single-coordinator-invariant.md).

### 5. Attempt-owned output and stream budgets

- Each durable attempt stores the execution-unit `max_output_bytes`; worker
  submission cannot choose it. Canonical requests allow 1 KiB through 10 MiB.
- Settlement calculates UTF-8 bytes before receipt/contribution/publication.
  Oversized ASCII or multibyte output rejects without an accepted receipt,
  broker publication, contribution, or ambiguous settled state. Quarantine keeps
  only a hash and at most a 4 KiB preview; worker error text is capped at 2 KiB.
- Stream state tracks cumulative bytes, batch count/times, and closed status on
  the authoritative attempt. The same output cap, 250,000-batch ceiling, and
  120-batches-per-second limit survive endpoint switching/reconnect. Session
  replacement and terminal/inactive attempts reject further streams. Limit
  closure emits at most one terminal limit event.
- The reference worker stops accumulation/sending at its cap rather than silently
  truncating into a normal result. WebSocket fanout is bounded for slow viewers.

### 6. Sealed, role-scoped artifacts

- Manifest states are `active`, `sealed`, `legacy_live`, and `invalid`; entry
  roles are `deliverable`, `provenance`, `log`, `candidate_source`, and
  `internal`.
- Terminal finalization holds the root active through one bounded scan, applies
  ensemble/direct winner prefix, persists immutable entry metadata and a
  canonical manifest hash transactionally, records `sealed_at`, then clears
  active state. Ordinary reads never rewrite a sealed baseline.
- Every file/ZIP read still confines the path, rejects symlinks/traversal, and
  hashes live bytes against the stored entry. Mutation fails with an integrity
  error. ZIP creation loads/selects one manifest and streams a temporary archive
  with cleanup.
- Private manifest/download defaults to deliverables. Explicit audit routes
  expose non-deliverable operator records; deprecated `role=all` preserves the
  prior complete private view. Results expose primary deliverables, manifest
  URLs, integrity mode, and sealed hash.
- Public shares default to deliverables; candidate source requires candidate
  detail; provenance/log/internal roles never share; no-winner candidate-scoped
  entries are excluded.

Decision: [ADR 0007](docs/adr/0007-sealed-artifact-manifests.md).

### 7. Share operations and post-hoc truthfulness

- Viewer routes create a share, list active metadata without tokens, revoke one,
  or revoke all. Raw tokens are returned only once and SQLite stores only their
  digest.
- Public capability routes use no-store, no-referrer, and nosniff headers.
  Invalid, expired, revoked, or privately missing shares use one public `404`
  shape. Application unhandled-error logs redact token path segments; Uvicorn
  and reverse-proxy access-log redaction remains operator responsibility.
- Post-hoc verification has explicit status/timestamp/agreement/reason fields.
  Trusted-alpha sets status `disabled` until duplicate-execution evidence has
  durable semantics. Detached work cannot silently change final assurance.

### 8. Backup, restore, operations, and harness

- Backup uses SQLite's live backup API and captures config without printing
  values, projects, both artifact stores, compatibility ledger, and build/version
  metadata in a versioned checksum-indexed archive with restrictive permissions.
- Restore stages and validates the full layout, entry types, traversal/symlinks,
  duplicate/case/Unicode collisions, JSON, SQLite, expected entries, and hashes
  before mutation. It refuses existing state without explicit overwrite, removes
  stale SQLite sidecars, installs by same-filesystem rename with rollback, and
  prints the post-restore preflight command.
- `docs/TRUSTED_ALPHA_RUNBOOK.md` covers preflight, overlays/TLS, credential
  distribution/rotation, viewer/pitch/worker workflows, drain, backup/restore,
  update, health/logs, share revocation, stuck/interrupted work, diagnostics, and
  uninstall. `docs/OPERATIONS.md` records the one-process/durability and evidence
  boundaries.
- The no-Ollama harness covers protected coordinator startup, two sessions,
  distributed strategies, consent, attempt settlement, streaming/limits,
  cancellation/late rejection, restart reconciliation, artifact seal/read/drift,
  share/revocation, database concurrency, and clean shutdown.

## Acceptance criteria disposition

| # | Criterion | RC1 disposition |
| --- | --- | --- |
| 1 | Recommended deployment configures three credentials | Delivered |
| 2 | Two-key deployments upgrade without replacement | Delivered and tested |
| 3 | Unsafe trusted-alpha preflight fails | Delivered and tested |
| 4 | Distributed CLI has explicit consent and works | Delivered and tested |
| 5 | Worker protocol requires node session | Delivered and tested |
| 6 | Active node ID cannot be silently replaced | Delivered; conflict is `409` |
| 7 | Session does not weaken attempt binding | Delivered; binding is additive |
| 8 | Session/lifetime stats distinct | Delivered |
| 9 | One state directory has one coordinator | Delivered and tested cross-platform |
| 10 | SQLite subsystems use shared policy | Delivered |
| 11 | Concurrent alpha writes avoid unexplained locks | Delivered and tested |
| 12 | Attempt stores server-issued output limit | Delivered |
| 13 | Oversized result cannot settle/publish/earn | Delivered and tested, including UTF-8 |
| 14 | Stream has cumulative byte/event budget | Delivered and tested |
| 15 | Terminal manifest gets sealed baseline | Delivered |
| 16 | Reads never rewrite sealed baseline | Delivered and tested |
| 17 | Artifact mutation is an integrity failure | Delivered and harnessed |
| 18 | ZIP uses one manifest snapshot | Delivered and tested |
| 19 | Deliverable and audit records differ | Delivered in model and routes |
| 20 | Share list/revoke does not expose tokens | Delivered and tested |
| 21 | Post-hoc work cannot alter final assurance | Delivered by trusted-alpha disablement |
| 22 | Backup/restore tooling tested | Delivered |
| 23 | Bounded multi-node harness runs in CI | Delivered in `ef5a567` |
| 24 | Trusted-alpha runbook complete | Delivered |
| 25 | Existing safe behavior remains compatible | Covered by 806-test final suite |
| 26 | No new execution strategy | Preserved |
| 27 | No marketplace/token/federation/public identity | Preserved |
| 28 | No broad visual UI work | Preserved; frontend contract only |
| 29 | Full suite passes | 806 passed, 2 skipped |
| 30 | Ruff passes | Passed |
| 31 | Server import passes | Passed |
| 32 | Compose validation passes | Passed |
| 33 | Docker result reported honestly | Daemon unavailable; no build claim |
| 34 | Residual limits documented | Documented here, handoff, protocol, operations, and threat model |

## Commit map relative to base

The integrated functional/test/doc commits before this closing record are:

```text
b8fe1bc fix: make CLI remote dispatch explicit
69f9983 feat: add verified backup and restore tooling
620da94 refactor: unify core SQLite connection policy
1b644a6 feat: seal artifact baselines and administer shares
15cabfd feat: add trusted-alpha deployment preflight
055766e feat: enforce single coordinator ownership
52db7ac feat: bind workers to server-issued sessions
d5cf0e8 test: cover node sessions and output limits
e308051 docs: record node registration session protocol
73e6f6f fix: harden public artifact capability responses
59be46d fix: keep trusted-alpha status output secret-safe
d47b556 docs: add trusted-alpha deployment runbooks
8506a5a fix: exempt authenticated worker protocol routes
77fd9a5 fix: publish terminal executions after finalization
f6af03a test: exercise trusted-alpha storage and assurance
17595a9 docs: align trusted-alpha operator examples
ef5a567 test: add trusted-alpha operational harness
dd93669 fix: redact share capabilities from application logs
```

Use `git log --oneline fd6fa29..HEAD` for the authoritative list after the final
docs commit.

## Claude/API and marketing handoff

`HANDOFF.md` is the concise frontend contract for node session state, deployment
protection, artifact role/integrity/hash, deliverable versus audit download,
lifecycle, validation, assurance, post-hoc state, and share metadata. It also
records the approved product statements and claims requiring revision. RC1 does
not implement the visual components or broadly edit public visual pages.

## Compatibility notes

- Local deployment remains the default and warns rather than inventing secrets.
- Local CLI calls keep their default; remote behavior now requires explicit
  consent at the canonical CLI boundary.
- `X-Node-Secret` remains configured admission. Current workers add sessions and
  re-register; session-less protocol callers receive actionable `401`.
- `role=all` preserves the old complete authenticated manifest with deprecation
  headers. Historical roots are `legacy_live`, not silently sealed.
- Legacy jobs/adapters remain canonical-backed; restart still interrupts rather
  than resumes process-local work.
- `status`, `tasks_completed`, `credits_earned`, `output_reference`, and
  `ledger.json` remain explicitly described compatibility projections.

## Residual limitations and incomplete external checks

- One coordinator and one state directory are supported; there is no high
  availability, horizontal scheduler scaling, durable queue, or automatic resume.
- Node admission is one shared secret. Sessions prevent current label collision
  but are process-local bearer credentials, not public-key identity, attestation,
  individual revocation, or Sybil resistance.
- Workers read assigned prompts. TLS/private-overlay deployment and credential
  distribution/rotation remain operator responsibilities.
- Generated output is not sandboxed, moderated, or malware-scanned.
  `network_policy` records intent but is not enforcement.
- Validation evidence is scoped; neither model review nor manifest sealing proves
  arbitrary behavioral correctness.
- Sealed hashes are local SQLite/file baselines, not signatures or defense
  against a compromised host. Restore preserves that same trust boundary.
- Share links are bearer credentials. Revocation cannot recall copied content;
  Uvicorn/reverse-proxy access logs require operator redaction.
- Backup creation is explicit; scheduling, off-site copies, encryption at rest,
  and disaster-recovery objectives are not supplied.
- Trusted-alpha post-hoc duplicate verification is disabled, not silently
  detached. There is no post-hoc correctness upgrade.
- A Docker build/start was not executed because the daemon was unavailable.
  Live Ollama quality, WAN behavior, external TLS/proxy configuration, and actual
  invited-node operations remain deployment-time checks, not claims from CI.

No known RC1 acceptance criterion is intentionally deferred inside the supported
small-private-alpha boundary. The unavailable Docker-daemon check and external
operator/environment exercises are reported above rather than represented as
passed.
