# Adversarial protocol campaign

A model-based, property-driven test campaign over the attempt, settlement,
enrollment, and execution-lifecycle state machines. Generated operation
sequences drive the real coordinator; a small reference model says what should
have happened; a fixed set of global invariants must hold no matter what.

It needs no Ollama, no network, and no external nodes.

```bash
python -m pytest -q tests/test_protocol_state_machine.py   # the generated campaign
python -m pytest -q tests/test_adversarial_scenarios.py    # the named scenarios
MYCELIUM_CAMPAIGN_PROFILE=extended python -m pytest -q tests/test_protocol_state_machine.py
```

| File | What it is |
| --- | --- |
| `tests/protocol_model.py` | The reference model. No production imports, no SQLite, no HTTP. |
| `tests/protocol_harness.py` | The driving surface: real routes, real stores, plus the three seams. |
| `tests/test_protocol_state_machine.py` | The Hypothesis `RuleBasedStateMachine` and the invariants. |
| `tests/test_adversarial_scenarios.py` | The named ROADMAP §5 scenarios, deterministically. |

---

## Framework and profiles

Hypothesis `RuleBasedStateMachine`, pinned in `requirements-dev.txt` as
`hypothesis>=6.100,<7`. It is test-only: nothing under the runtime imports it,
and `pip install -r requirements.txt` alone still starts a coordinator.

| | CI (default) | Extended |
| --- | --- | --- |
| Selected by | nothing | `MYCELIUM_CAMPAIGN_PROFILE=extended` |
| `max_examples` | 15 | 250 |
| `stateful_step_count` | 14 | 40 |
| `derandomize` | yes | no |
| Example database | disabled | disabled |
| Measured wall clock | ~32 s | ~8–9 min, plus up to 5 min of shrinking on a failure |

The CI profile is derandomized with no example database, so a red build is
reproducible from the source tree alone — there is no `.hypothesis` directory
whose absence changes the answer. The extended profile is for local hunting; it
is where two of the findings below came from.

Counterexample output is content-free. Every value the generator can choose is
a fixed synthetic constant in `tests/protocol_harness.py` — labels `n0…n3`,
credentials `campaign-credential-NN-000…`, keys `k0…k2`, two synthetic task
strings, two synthetic output strings. No real credential, key, token, nonce,
prompt, output, or artifact byte can appear in a shrink report or a CI log,
because none is ever generated.

---

## The reference model

`tests/protocol_model.py` is the specification, not a second implementation. It
tracks enrollments, process-local sessions, attempts and their lease deadlines,
executions and their lifecycle, accepted receipts, contribution records,
idempotency mappings, and what a caller has already been shown.

It is deliberately coarse. Where the coordinator distinguishes fifteen rejection
reasons, the model answers `REJECT`. An oracle that reproduces the
implementation's branching is an oracle that reproduces the implementation's
bugs — and it stops being reviewable by reading, which is the only property that
makes a model worth having.

---

## Operation vocabulary

Every sequence starts from `open_the_network()`: two bootstrap-enrolled nodes,
one canonical execution, two queued units. Reaching settlement otherwise takes a
four-step prerequisite chain that uniform rule selection almost never assembles
inside a bounded step budget — see finding **F4**.

On top of that, in any order, including invalid ones:

| Rule | What it drives |
| --- | --- |
| `bootstrap_enrollment` | `POST /nodes/register` bootstrap: new label, repeat label, duplicate credential, revoked label |
| `returning_registration` | returning enrollment; a second incarnation for the same enrollment |
| `revoke_enrollment` / `rotate_credential` | the durable store plus reclaim, exactly as the operator CLI path does |
| `drain_node` | `POST /nodes/{id}/drain` |
| `enqueue_unit` / `poll_for_work` | queue a canonical unit; long-poll and take a server-issued attempt |
| `submit_result` | ten mutations: correct, replay, wrong nonce, wrong attempt id, wrong node label, wrong session, wrong execution id, missing contract version, oversized output, oversized error |
| `stream_tokens` | attempt-bound token batches against the cumulative budget |
| `supersede_attempt` | a durable active→superseded transition |
| `advance_clock` / `rewind_clock` | coordinator time forward and backward, capped at ±80 s |
| `run_janitor` | one real `server_state._cleanup_pass()` sweep |
| `submit_execution` | `POST /v1/executions`, keyed and unkeyed, two requester scopes, two canonical requests |
| `cancel_execution` / `read_execution` | cancel (idempotent) and durable read |
| `arm_persistence_fault` | arm a SQLite failure at operation index *n*, in `io`, `commit`, or `busy` mode |
| `restart_coordinator` | a new coordinator epoch over the same durable state |

Restarts and drains are bounded per sequence (2 and 1). Both are absorbing
enough that an unbounded number of them makes every sequence a sequence about
nothing — no session survives long enough to reach settlement. Both get direct
coverage in `tests/test_adversarial_scenarios.py` instead.

Session *abandonment* — a laptop closing mid-task, the most ordinary failure on
a volunteer network — is not a generated rule, because it needs a clock movement
past `_NODE_TIMEOUT` that the generated campaign caps (see F7). It has a direct
test instead:
`test_an_abandoned_session_has_its_lease_reclaimed_and_its_work_requeued`
asserts that the janitor reclaims the lease, requeues the unit, refuses the
abandoned worker if it later wakes up holding the original handout, and lets a
different node settle the requeued unit on a new attempt.

Handouts are deliberately **kept** across a generated restart. A worker holding
a lease when the coordinator died still holds it, and its late submission is the
point: it must fail closed, while an exact replay of an already-settled attempt
must still be recoverable by a fresh session of the same enrollment.

### Faults, and what the model stops claiming

When an injected fault fires, the model stops *predicting* that operation's
outcome — it describes fault-free semantics — but it still *learns* what the
coordinator actually did, from the response and from durable state. It only ever
forgets or copies; it never invents an outcome the coordinator did not produce.
Every global invariant below is still asserted after every step, faulted or not.
That asymmetry is where findings **F1** and **F2** came from, and getting it
wrong is how a fault-injection campaign quietly stops testing anything.

---

## The invariants

Each is an independently reportable named property.

| Invariant | Property | What a break would mean |
| --- | --- | --- |
| Settlement | `settlement_is_at_most_once` | Two workers were paid for one unit, or one unit produced two conflicting results. The dispatcher's authority model is broken. |
| Credit | `credit_matches_acceptance_exactly` | The ledger no longer describes work done. Either someone earned credit for a result that was refused, or someone donated CPU and was not recorded. |
| Exact replay | `check_exact_replay_creates_nothing` | A retrying worker — the normal case on a flaky home connection — is paid twice, or gets a different answer each time. |
| Stale authority | `check_stale_authority_never_settles`, `only_settled_attempts_hold_receipts` | An expired, superseded, revoked, or misbound submission entered operational state. This is the hole PR #45 closed; it must stay closed. |
| Monotonic lifecycle | `terminal_attempts_never_reopen`, `execution_lifecycle_is_monotonic` | A finished run changed its mind. Every permalink, share, and ledger row that quoted it is now wrong. |
| Durable before public | `durable_state_is_never_behind_public_state` | A caller was told something the database does not know, so a restart silently retracts it. ADR 0009's whole subject. |
| Idempotency | asserted in `submit_execution` | A retried submission ran the work twice, or a different request silently reused someone's key. |
| Enrollment isolation | `enrollment_isolation_holds` | One contributor can take another's credit — the finding `tests/test_result_binding.py` exists for. |
| Evidence integrity | `capability_observations_are_not_double_counted`, `shadow_evidence_never_reaches_routing` | Shadow evidence is influencing real placement, which ADR 0012 says it must not, or a replay is inflating a scope's sample count. |
| Secret hygiene | `_assert_no_secret_in`, `quarantine_is_bounded_and_carries_no_identity`, the `teardown()` scan | A credential, session token, or nonce is readable in the database, the logs, the event stream, the ledger projection, quarantine, or an error body. |

### One place the invariant list needed narrowing

The campaign brief asked that no *prompt or output* appear "in any database
row". That is not what Mycelium promises, and asserting it would have failed
immediately against correct behaviour: an accepted result's output is stored in
`accepted_result_receipts.output`, and a rejected one's bounded 4 KiB preview is
stored in `result_quarantine.output_preview`. Both are documented in
`docs/PROTOCOL.md` under "Settlement, replay, rejection, and quarantine", and a
durable result you cannot read back is not a durable result.

So the property is scoped, in `CoordinatorHarness.scan_for_secrets`:

* **Identity material** — enrollment credentials, session tokens, attempt
  nonces, idempotency keys — may never be readable in `events.db`, the shadow
  health database, `ledger.json`, the event stream, the logs, or any error body.
  Credentials may not be echoed even in a success body.
* **Prompts and outputs** may never reach the event stream, the logs, or the
  ledger projection. They legitimately live in `executions.request_json` and
  `accepted_result_receipts.output`.

Session tokens and nonces *are* delivered over the wire to the worker that owns
them — that is the protocol, not a leak. Finding **F3** is what happens when you
forget that.

---

## ROADMAP §5 scenario coverage

| # | Scenario | Status | Where |
| --- | --- | --- | --- |
| 1 | Result submitted under another node's id | example-based **and** generated | `test_result_binding.py`, `submit_result` mutation `wrong_label`, `test_one_enrollment_cannot_settle_another_enrollments_attempt` |
| 2 | Replay | example-based **and** generated | `test_result_binding.py`, mutation `replay`, `check_exact_replay_creates_nothing` |
| 3 | Expired lease | example-based **and** generated | `test_result_binding.py`, `advance_clock` + `run_janitor`, `test_worker_reported_elapsed_time_cannot_extend_an_expired_lease` |
| 4 | Duplicate after retry | example-based **and** generated | `test_result_binding.py`, `settlement_is_at_most_once` |
| 5 | Restart mid-submission | **now covered** | `test_a_lease_held_across_a_restart_can_never_settle`, `test_an_accepted_settlement_survives_a_restart_and_replays_exactly`, `test_a_fault_anywhere_in_the_submission_path_leaves_a_truthful_state` (26 indices), `test_a_real_lifespan_restart_agrees_with_the_campaign_restart`, plus `restart_coordinator` |
| 6 | Clock skew | **now covered** | `test_the_worker_protocol_carries_no_timestamp_that_could_move_a_lease`, the three coordinator-clock tests, plus `advance_clock` / `rewind_clock` |
| 7 | Duplicate identity registration | **now covered** | `test_a_label_or_credential_collision_is_a_stable_conflict`, `test_a_concurrent_bootstrap_race_on_one_label_admits_exactly_one`, `test_a_freed_label_does_not_inherit_the_previous_enrollments_history`, plus `bootstrap_enrollment` |
| 8 | Oversized or malformed payload | **now covered** | five tests in §4 of the scenarios module, plus mutations `oversized_output` / `oversized_error` and `stream_tokens` |
| 9 | Sybil registration | **now covered, as documented behaviour** | `test_n_bootstraps_under_the_shared_secret_produce_n_independent_enrollments` |
| 10 | Colluding verifiers | **now covered, as documented behaviour** | `test_sampled_agreement_is_off_by_default_and_never_routes`, `test_total_agreement_between_two_nodes_changes_no_placement_decision` |
| 11 | Disk-full and IO faults | **now covered** | `PersistenceFaultInjector`, the 26-index submission sweep, `test_a_persistence_failure_during_handout_never_publishes_a_lease`, `test_a_failed_ledger_projection_cannot_alter_an_accepted_settlement`, plus `arm_persistence_fault` |
| 12 | Crash between verification and settlement | **now covered** | `test_a_crash_at_the_settlement_boundary_never_pays_without_accepting` (9 commit-fault indices) |

Two of these assert what Mycelium **documents**, not what it prevents.
Scenario 9: the project does not claim Sybil resistance — a holder of
`node_secret` can enrol under as many labels as it likes. What is asserted is
the narrower thing that is actually true: each identity is separately
attributed, separately revocable, and earns exactly its own credit. Scenario 10:
`verify_rate` is off by default and local-mode only, sampled agreement describes
output *shape*, and the test asserts that total agreement between two nodes
moves no eligibility, ordering, settlement, or credit decision — and that
agreement is never reported under a field name that reads as correctness.
Writing a test that asserted Sybil prevention would assert a guarantee this
project does not make.

---

## Findings

Every failure the campaign produced, in the order it produced them. Each was
reduced to a deterministic reproducer and confirmed by hand before being
classified — ROADMAP §2 requires verifying a negative result by running the
artifact, and this campaign is exactly the kind of artifact that would otherwise
produce a confident wrong answer.

**No invariant was weakened, relaxed, or deleted to make a test pass. No
production behaviour was redesigned.**

### F1 — a first keyed submission reported `Idempotency-Replayed: true`

*Classification: test defect.*

The generated sequence armed a disk I/O fault at operation index 12, submitted a
keyed execution, and then submitted the same one again. The second call reported
a replay where the model expected a creation.

Reproducer (confirmed by hand, `tests/protocol_harness.py` only):

```python
harness.faults.arm(target_index=12, mode="io")
first = harness.submit_execution(host="10.0.0.2", task="synthetic-task-alpha", idempotency_key="k1")
harness.faults.disarm()
# fault fired: True | first: 202, Idempotency-Replayed: false | durable mappings: 1
second = harness.submit_execution(host="10.0.0.2", task="synthetic-task-alpha", idempotency_key="k1")
# second: 202, Idempotency-Replayed: true
```

The coordinator was right. `ExecutionService._commit_submission`'s bounded
persistence retry loop absorbed the injected failure and committed the mapping;
both responses were correct. The campaign was wrong: it treated "a fault fired"
as "the outcome is unknowable" and discarded the model update, and
`_resync_from_durable` cannot reconstruct a `(scope, key)` mapping because the
durable table stores only digests.

*Resolution:* faulted operations now update the model from the observed response
rather than being skipped. See "Faults, and what the model stops claiming".
This finding is a positive result about the retry loop, recorded as such.

### F2 — a keyed submission conflicted where the model expected convergence

*Classification: test defect. Same root cause as F1, mirrored.*

Once a faulted-but-committed mapping is missing from the model, the next
submission under that key conflicts (409) instead of replaying. Fixed by the
same change, plus an `ambiguous_keys` set: after a fault or a 503, a key's
durable mapping is marked unknowable and prediction is suspended for it until a
later response reveals the truth.

### F3 — the secret scan flagged a registration success body

*Classification: test defect.*

`POST /nodes/register` returns `session_token` in its 200 body. It has to: that
is how the worker gets its bearer token. The campaign's blanket "no minted
secret may appear in any response" check flagged the protocol working correctly.

*Resolution:* the check was scoped, not weakened. Enrollment credentials must
not be echoed in *any* body, success included; minted session tokens and nonces
must not appear in any body with status ≥ 400, and must not appear in storage,
logs, events, the ledger projection, or quarantine at all. Those are still
asserted, on every step and again at teardown.

### F4 — the campaign was not reaching settlement at all

*Classification: test defect. The most important one here.*

Instrumenting `CoordinatorHarness` showed 33 successful bootstraps, 82 restarts,
25 rotations — and **zero** polls and **zero** result submissions across 25
examples of 30 steps. Every invariant about settlement, credit, and replay was
passing vacuously.

Settlement needs an enrolled node, a queued execution, a queued unit, and a
handout. With ~18 rules selected roughly uniformly, that four-step chain almost
never assembles inside a bounded step budget — and unbounded `restart_coordinator`
was clearing sessions faster than they could be established.

*Resolution:* `@initialize()` opens a working network before generation starts;
restarts and drains are bounded per sequence. `test_campaign_reads_the_tables_it_asserts_on`
now guards the other half of the same failure mode by asserting that the tables
the invariants query actually exist.

This is the finding worth reading twice. A property-based campaign that reports
green while never reaching the code it claims to cover is worse than no campaign,
because it is quoted as evidence.

### F5 — a revoked label was expected to re-bootstrap idempotently

*Classification: documented behaviour; the model was wrong.*

Found by the extended profile. Sequence: `open_the_network`,
`revoke_enrollment(n0)`, `bootstrap_enrollment(n0, its original credential)`.
The model predicted `idempotent`; the coordinator returned `403
node_enrollment_revoked`.

The coordinator is right, and the ordering matters: `NodeEnrollmentStore.bootstrap`
checks the label row first, and revocation beats a matching credential. If a
revoked enrollment could re-bootstrap by presenting its original pair,
revocation would be undoable by the party it was used against.

*Resolution:* `ProtocolModel.expect_bootstrap` now mirrors the real precedence —
label first, revocation before credential match — with a `Bootstrap.REVOKED`
outcome. `docs/PROTOCOL.md` gained one sentence, because the precedence was true
but unstated. Covered deterministically by
`test_a_freed_label_does_not_inherit_the_previous_enrollments_history`.

### F6 — an oversized `error` field returned 422, not 413

*Classification: documented behaviour, stated imprecisely.*

`docs/PROTOCOL.md` said "worker error text is capped at 2 KiB". There are two
caps, and which one binds depends on the encoding: `TaskResult.error` is capped
at 2048 **characters** by the transport model, and `AttemptStore.settle` caps it
at 2048 **bytes**. An ASCII overflow is refused at the transport with 422 —
earlier, and therefore stronger, than the campaign expected.

That raised the interesting case: text that passes the character cap and busts
the byte cap. 1500 `é` characters is 1500 characters and 3000 bytes. It is
correctly refused with 413 at the byte cap, settles nothing, and earns no
credit. Had only the character cap existed, `error` would silently have been a
8 KiB field.

*Resolution:* both bounds are now asserted in
`test_the_error_field_is_bounded_in_characters_and_again_in_bytes`, and
`docs/PROTOCOL.md` states them separately.

### F7 — a forward clock jump past 90 s also reclaims every node

*Classification: correct behaviour. Recorded because it is not obvious.*

`server_state._cleanup_pass` decides worker-lease expiry **and** node staleness
(`_NODE_TIMEOUT`, 90 s) from the same coordinator clock reading. A forward jump
larger than 90 s — an NTP correction, a resumed laptop — therefore reclaims every
node's in-flight work as stale, not only the leases that actually expired.

Nothing unsafe follows: reclaimed work is requeued, no attempt settles twice, and
no credit is created. It predates this branch; `coordinator_now()` did not change
it, because that function returns `time.time()` in production exactly as before.

*Resolution:* recorded, not changed — a design change to node staleness is out of
scope for a test PR. The generated campaign caps its clock offset at ±80 s so the
model stays simple, and the behaviour is covered explicitly in
`test_a_backward_coordinator_clock_cannot_revive_a_durably_expired_lease`, which
re-registers after the sweep and then asserts the lease-based refusal.

### Nothing deferred under protocol case 4

No finding required a design change, so there are no `xfail` or skipped
reproducers in this PR.

---

## Seams introduced

Three, each the narrowest thing that makes one scenario reachable.

**`server_state.coordinator_now()`** — one function returning `time.time()`,
used at the two places that decide lease authority: lease issue in
`routes_nodes.next_task`, and lease-expiry evaluation in
`routes_nodes._settle_and_publish` and `server_state._cleanup_pass`. Production
behaviour is byte-for-byte what it was. The alternative was patching
`time.time` process-wide, which would also move SQLite, logging, and asyncio.
This is not a clock-abstraction layer: there is no injectable clock object, no
interface, and no configuration.

**`tests/protocol_harness.PersistenceFaultInjector`** — test-only. It swaps
`sqlite_store.RetryConnection` for a subclass that fails a chosen operation
index. There is no production fault-injection endpoint, no framework, and no
plugin point. (The subclass reimplements its parent's methods against
`sqlite3.Connection` under the same `retry_busy` policy rather than calling
`super()`: `RetryConnection` resolves `super(RetryConnection, self)` through the
module global that the swap replaces, so delegating upward recurses.)

**`CoordinatorHarness.restart()`** — no production change at all. It calls
`server_state._init_db()`, the same epoch boundary the FastAPI lifespan calls,
then rebuilds every store on new connections.
`test_a_real_lifespan_restart_agrees_with_the_campaign_restart` runs the actual
lifespan and asserts the same outcome, so the shortcut cannot quietly diverge
from the thing it stands in for.

---

## What this campaign does not cover

* **Multiple coordinators.** ADR 0006 makes one coordinator per state directory
  an invariant, enforced by an OS lock. Nothing here tests two.
* **Real network partitions.** Every request is in-process through
  `TestClient`. There are no dropped packets, no half-open sockets, no
  TCP-level reordering, and no proxy.
* **Live models.** `CampaignStrategy` returns immediately. Nothing here says
  anything about planner, builder, reviewer, or reviser quality — that is what
  `evals/` measures, and its error bars are in `AGENTS.md`.
* **Hostile native code.** Validator containment is ADR 0013's subject and is
  tested in `tests/test_validator_protocol.py` and `tests/test_validator_staging.py`.
  This campaign checks the runner's control-message bounds and nothing more.
* **Real filesystem exhaustion.** Faults are injected at the SQLite API, not at
  the filesystem. A genuinely full disk on every platform behaves in ways this
  cannot reproduce — Windows, in particular, fails differently from Linux, and
  CI runs Linux.
* **The dispatcher's unit→execution feedback loop.** Executions and worker units
  share durable storage and identity here, and units carry real execution
  bindings, but `Dispatcher._distributed`'s await-a-receipt path is not driven by
  the generator. `tests/test_attempt_dispatch.py` covers it directly.
* **Clock offsets beyond ±80 s in generation.** Larger jumps are covered
  deterministically in the scenarios module; see F7 for why they are not
  generated.
* **WebSocket event delivery and the dashboard.** The event *stream* is asserted
  against durable state; its transport is not exercised.

---

## CI runtime cost

Measured on this branch, Windows, Python 3.14.3, 8 GB, CPU-only:

| Module | Tests | Wall clock |
| --- | --- | --- |
| `tests/test_protocol_state_machine.py` | 5 | ~32 s |
| `tests/test_adversarial_scenarios.py` | 63 | ~73 s |
| **Added to `pytest -q`** | **68** | **~105 s** |

The bulk of the scenario cost is the two parametrized fault sweeps (26 + 9
indices), each of which builds an isolated coordinator, and each of which is the
highest-value test in the set. The campaign profile is tuned so the generated
half stays under the deterministic half.
