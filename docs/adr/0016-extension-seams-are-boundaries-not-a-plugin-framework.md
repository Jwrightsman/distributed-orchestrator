# ADR 0016 — Extension seams are documented boundaries with contract tests, not a plugin framework

**Status:** Accepted (2026-09-03, Theme 4A)

**Context:** The August architecture audit classified several parts of Mycelium as
places a second implementation might one day plug in, and warned in the same
breath against building a generalized plugin framework to host one
implementation. That warning is the whole design constraint here.

A registry, an entry-point mechanism, or an abstract base class with exactly one
subclass would add indirection today, in exchange for flexibility nobody has
asked for, at a stage where the project's stated bottleneck is reliability rather
than capability. It would also be the kind of change that looks like architecture
and reads like progress while making every call path one hop longer to follow.

## Decision

For each seam: **a described boundary and a contract test**, and nothing else.

* No registry, no plugin discovery, no entry points, no dynamic import.
* No configuration key that selects an implementation.
* No abstract base class with a single subclass.
* No new parameter threaded through production code to satisfy a boundary. Where
  honouring a seam would require that, the coupling is recorded as a finding
  instead.

`tests/test_seam_contracts.py` is the executable half. A second implementation
would have to satisfy the same assertions; that is what "extension point" means
here.

No contract below assumes a DHT, a marketplace, or a blockchain. A boundary that
only made sense given one of those would be a boundary drawn around a fantasy,
and is called out where the temptation exists.

---

### 1. Scheduler backend

**Crosses:** a typed execution request, the live node registry, a capability
match result. **Out:** a placement decision and an ordered eligible set.

**Never crosses:** observed evidence of any kind, a reputation or score, a
storage handle, or a node label used as anything but a key into the registry.

**A second implementation** would need to decide placement from the request and
the claims alone. That is a real constraint: it rules out a scheduler that
consults history, which is exactly the property ADR 0012 protects.

**Honoured.** `match_node_requirements` is a pure function of its arguments and
takes no store. `qualifying_nodes` and `select_placement` reach no storage and
name no evidence store. `dispatch` *records* evidence and never reads an
aggregate back — recording is not deciding, and the contract test distinguishes
them.

### 2. Enrollment and identity provider

**Crosses:** a bootstrap admission secret, a worker-proposed credential, a
normalized display label. **Out:** an opaque immutable enrollment ID and a
credential version.

**Never crosses:** a plaintext credential into storage; a node label used as a
trust key.

**A second implementation** — a keypair scheme, an external IdP — would mint
opaque stable IDs and expose only digests. It would not need the label to mean
anything, which is the test of whether the boundary is drawn in the right place.

**Honoured.** Storage holds `credential_digest` and no plaintext column; the
credential does not appear in the database bytes or in `public_metadata()`.
Standings group by enrollment first, explicit legacy session second, and a label
only for historical rows that predate both. Settlement authority requires
session *and* enrollment, not a label.

### 3. Discovery and transport

**Crosses:** inbound worker-initiated HTTP carrying a session bearer. **Out:**
task handouts on the worker's own poll.

**Never crosses:** a coordinator-initiated connection to a worker; a worker
address used for transport.

**A second implementation** — a queue, a relay, a different protocol — must be
substitutable without changing attempt or settlement semantics. That is only true
while the coordinator never dials out, which is also what makes a node safe to
run behind NAT with no inbound ports.

This is the seam where a DHT would be tempting. It is deliberately not assumed:
the boundary says "the coordinator does not dial workers", not "peers discover
each other", and a coordinator-vs-P2P decision is ROADMAP §5's open question, not
a thing to bake into a contract test.

**Honoured, with a noted coupling.** No coordinator module opens an outbound
client to a worker, and no durable record carries an address-shaped column. See
finding F2 for the legacy `hostname` field.

### 4. Validator executor

**Crosses:** a bounded control message naming a built-in validator, its version,
a validated logical filename or output reference, and parent-clamped limits.

**Never crosses:** coordinator configuration, a credential, a database path, a
module or executable name, or a callable.

**A second implementation** — a container, a microVM, WASM — would receive the
same control message and nothing more. ADR 0013 already made this a process
boundary; this states what may traverse it.

**Honoured.** `ValidatorRunnerRequestV2` exposes no config, credential, path, or
callable field. The child reads no coordinator configuration and names no config
key: configuration informs *parent-side* limits, which are clamped and then sent
as values, and that is a different thing from config crossing the boundary.

### 5. Artifact provenance signer and publisher

**Crosses:** a sealed manifest hash committed with terminal execution state, plus
artifact-root ownership. **Out:** an authenticated download or an explicit share.

**Never crosses:** publication without the committed seal.

**A second implementation** — a provenance signer — would add a signature *over
the same manifest*. It would not become the thing that decides whether
publication may happen, because that decision belongs to the durable terminal
commit (ADR 0009) and must stay there.

**Honoured, and deliberately empty.** There is no signing key, no signature
field, and no configuration for one. The contract test asserts that absence, so
the seam description cannot quietly drift into implying a signer exists.

### 6. Reputation, accounting, and future payment policy

**Crosses:** an accepted receipt's output and error, and its enrollment
attribution. **Out:** a fixed non-monetary point value.

**Never crosses:** evidence, reputation, or any signal about past behaviour into
what work is worth; and no monetary meaning, ever.

**A second implementation** — a different accounting basis, or one day a payment
policy — would replace `compute_contribution_points`. It would not read history
inside it. Note what this boundary refuses to assume: there is no marketplace,
no price, and no settlement currency, and a contract that assumed any of them
would be describing a system that does not exist.

**Partially honoured.** The policy is now a named pure function owned by
`ledger.py` rather than a bare literal inside a settlement transaction. But the
contribution INSERT still executes inside `AttemptStore.settle`'s
`BEGIN IMMEDIATE`. See finding F1.

---

## Findings

### F1 — accounting executes inside the settlement transaction

*Classification: real coupling. Partially fixed; the rest deferred.*

`points = 5 if output and not error else 0` sat inline inside
`AttemptStore.settle`, putting a policy decision inside an integrity boundary and
making "what is a point worth" a question you answered by reading the settlement
transaction.

**Fixed, trivially and safely:** the policy moved to
`ledger.compute_contribution_points`, a pure function of the settled result,
which settlement now applies rather than defines. No new parameter, no new
import, identical values.

**Not fixed:** the contribution INSERT still runs inside settlement's
transaction. That atomicity is deliberate and load-bearing — the campaign's
credit/receipt invariant depends on it — so substituting a different accounting
policy would mean threading one through settlement, which is exactly the
indirection this ADR forbids. Reproducer:
`test_settlement_does_not_execute_accounting_inline`, xfail and strict.

**Trigger to reopen:** a second accounting basis is actually wanted — for
example if the guild economics in ROADMAP §8 stop being hypothetical. Until
then, one basis executed atomically is the right trade.

### F2 — the legacy registration accepts a worker-supplied hostname

*Classification: noted coupling. Deferred.*

`NodeRegistration.hostname` is worker-supplied, stored on the process-local node
record, and displayed on a protected operator view. It is never dialled — the
transport boundary holds — but it is transport-shaped data crossing a boundary
that says addresses are not needed, and the typed capability descriptor
deliberately excludes hostnames for exactly that reason (see `docs/THREAT_MODEL.md`).

Removing the field is a protocol change with a version bump behind it, not a
Theme 4A cleanup. Reproducer:
`test_registration_does_not_accept_a_worker_supplied_hostname`, xfail and strict.

**Trigger to reopen:** the next breaking protocol version, where dropping the
field costs nothing extra.

### F3 — SQLite specifics in routes

*Classification: real violation. Fixed in this PR.*

`routes_events.py` imported `sqlite3` and ran `SELECT` against the events table
in two places, which meant the event surface could not be backed by anything else
without editing HTTP handlers. Extracted to `server_state.read_persisted_events`,
bounded and validated. `test_routes_do_not_speak_sql` now asserts no
`routes_*.py` module imports `sqlite3` or contains raw SQL.

---

## Deferred: A2A, an edge adapter mapping only

Nothing is implemented. MCP is the shipped and measured interface (10/10
end-to-end), and adding a second agent-facing protocol before the first one has
external users would be building for an audience that does not exist yet.

If it were built, it would be an **edge adapter** and the mapping would be:

| A2A concept | Mycelium concept |
| --- | --- |
| Agent Card | a static capability document: strategies, output contracts, confidentiality classes, the worker-protocol window |
| Task | one canonical `ExecutionRequestV1` and its `execution_id` |
| Task status | `lifecycle_status`, unchanged and monotonic |
| Artifact | a sealed artifact manifest entry, delivered through the existing authenticated download or explicit share |
| Streaming | the existing token-stream telemetry, which is progress and never an accepted result |
| Cancellation | `POST /v1/executions/{id}/cancel`, already idempotent and terminal |

**What stays internal, and why.** Attempts, leases, nonces, receipts, and
settlement do not map outward and must not. They are the coordinator's integrity
model for untrusted workers; an external agent protocol has no business naming a
lease or observing which node settled a unit, and exposing them would turn an
internal invariant into a compatibility obligation. An A2A task maps to an
*execution*, never to an attempt.

**Trigger to reopen:** an actual external agent runtime asks for it, and MCP has
been shown insufficient for that case.

## Deferred: durable ensemble-candidate queue

Ensemble candidates live on the process-local queue. A coordinator restart loses
in-flight candidates; restart reconciliation makes that loss *truthful*
(ADR 0009) but does not resume the work.

**Cost of building it:** a durable queue is the same class of change as the
durable scheduler ROADMAP §5 describes — persisted queue state, a claim protocol,
and reconciliation that can distinguish "leased" from "merely queued" across
restart. It is not a small addition, and a half-durable queue is worse than an
honest process-local one because it invites trust it has not earned.

**Risk of not building it:** a restart during an ensemble loses candidate work
that must be redone. On the current single-coordinator, small-workload deployment
that costs minutes, is visible, and is already reported truthfully.

**Trigger to reopen:** evidence that restart loss on ensemble candidates is
actually expensive at real workload sizes — restarts frequent enough, or
ensembles large enough, that redoing the work is a material cost rather than a
theoretical one. Measure before building.

## Consequences

* Six boundaries are written down and asserted, so a future second implementation
  has a specification rather than an archaeology exercise.
* Two of them are not fully honoured, and both say so out loud with a strict
  xfail reproducer rather than a comment nobody runs.
* No indirection was added. Every call path is exactly as long as it was.
* The contract tests are partly source-shaped assertions, which are more brittle
  than behavioural ones. That is the honest cost of asserting "X never reaches Y"
  without building the abstraction that would make it structurally impossible —
  and building that abstraction is the thing this ADR declines to do.
