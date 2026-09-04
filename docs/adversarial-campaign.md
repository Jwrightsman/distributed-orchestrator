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
| `tests/protocol_harness.py` | The driving surface: real routes, real stores, the seams, and the coverage counters. |
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
| `max_examples` | 15 | 120 |
| `stateful_step_count` | 75 | 80 |
| `derandomize` | yes | no |
| Example database | disabled | disabled |
| Measured wall clock | see below | 6 m 18 s, plus up to 5 min of shrinking per failure |

Few examples, many steps. An example costs about two seconds to set up (an
isolated coordinator, its stores, its lifespan) and about fifty milliseconds per
step, so steps are where a budget buys coverage — and coverage here means
dependent chains completing: poll, settle, retry, expire, sweep. The original
15x14 profile bought almost none of that; see F4.

The CI profile is derandomized with no example database, so a red build is
reproducible from the source tree alone — there is no `.hypothesis` directory
whose absence changes the answer, and the coverage floor below is deterministic
rather than probabilistic for a given Hypothesis version. The extended profile is for local hunting; it
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
| `submit_correct_result` | the honest worker; its own rule, because everything downstream needs an acceptance |
| `submit_replayed_result` | the honest retry after a lost response; also its own rule |
| `record_verification_evidence` | append post-hoc verification evidence for an accepted attempt |
| `submit_result` | nine mutations: wrong nonce, wrong attempt id, wrong node label, wrong session, wrong execution id, missing contract version, oversized output, error over the character cap, error over the byte cap |
| `stream_tokens` | attempt-bound token batches against the cumulative budget |
| `supersede_attempt` | a durable active→superseded transition |
| `advance_clock` / `rewind_clock` | coordinator time forward and backward, capped at ±3000 s |
| `run_janitor` | one real `server_state._cleanup_pass()` sweep |
| `time_passes_and_the_janitor_wakes` | the background sweep after time has passed, as the 30-second task does |
| `submit_execution` | `POST /v1/executions`, keyed and unkeyed, two requester scopes, two canonical requests |
| `cancel_execution` / `read_execution` | cancel (idempotent) and durable read |
| `arm_persistence_fault` | arm a SQLite failure at operation index *n*, in `io`, `commit`, or `busy` mode |
| `restart_coordinator` | a new coordinator epoch over the same durable state |

Restarts and drains are bounded per sequence (2 and 1). Both are absorbing
enough that an unbounded number of them makes every sequence a sequence about
nothing — no session survives long enough to reach settlement. Both get direct
coverage in `tests/test_adversarial_scenarios.py` instead.

Session *abandonment* — a laptop closing mid-task, the most ordinary failure on
a volunteer network — is not a generated rule, because since F7's fix it is no
longer expressible as a clock movement at all: silence is elapsed time on the
monotonic source, which the campaign does not move. It has a direct test
instead:
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

**No invariant was weakened, relaxed, or deleted to make a test pass.** One
production defect was fixed: F7, in Theme 1.5, and the fix is deliberately narrow
— see its entry. Nothing else in the coordinator was redesigned. F8 strengthened
a probe rather than relaxing it: the scan now reads more, not less.

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

**And it was not fully fixed.** Theme 1.5 added the standing guard below, and the
guard's first run showed the campaign was *still* reaching zero accepted
settlements. `open_the_network` had removed the setup half of the problem, but
the poll-then-settle chain still needed more steps than the 14-step CI profile
gave it, and Hypothesis's early examples are small. Three things changed:

* **Shape.** The honest submission and the honest retry became their own rules
  instead of two of ten adversarial mutations - every other property depends on
  an acceptance, so the normal case must not be a 1-in-N draw against the
  attacks. `initialize` now takes one handout of each lease length, so the
  settlement path is live from step 0. And a rule models the background janitor
  waking after time has passed: without it, every lease reached a terminal state
  through a submission before a sweep ever saw one, and the janitor's reclaim
  path was never exercised.
* **Budget.** 15 examples x 60 steps, not x14. Measured: ~125 s for the module,
  up from ~35 s. The old 35 s was largely buying nothing.
* **Correctness.** Reaching settlement immediately exposed that the campaign's
  oversized-error mutation expected 413 where the request model returns 422 -
  F6's two bounds, which this document already described and the campaign had
  never executed. Both are now generated.

### The coverage floor

`tests/test_protocol_state_machine.py::test_the_campaign_holds_and_reaches_the_code_it_asserts_on`
runs the state machine itself and then fails - not warns - if a whole run did not
reach each class below. It runs in the default CI profile. Counters live on
`CoordinatorHarness` and record outcome classes and counts only: no identifiers,
no payloads, no timings, so a failure is safe to paste into a CI log.

The assertion is at run level, not per example: a single generated sequence need
not hit everything. And the test runs the machine itself rather than relying on
Hypothesis's generated `TestCase`, so it cannot depend on another test having run
first in the same session - and the campaign still executes exactly once per CI
run.

| Class | Floor | Theme 3C | Theme 4B (re-measured) |
| --- | --- | --- | --- |
| `settlement_accepted` | 1 | 2 | **30** |
| `settlement_rejected_stale_or_misbound` | 1 | 24 | 36 |
| `settlement_replayed` | 1 | 2 | 17 |
| `idempotency_replayed` | 1 | 4 | 4 |
| `idempotency_conflict` | 1 | 6 | 7 |
| `persistence_fault_fired_in_submission` | 1 | 2 | **1** |
| `restart_with_outstanding_handout` | 1 | 6 | 5 |
| `lease_reclaimed_by_janitor` | 1 | 2 | 6 |
| `capability_observation_recorded` | 1 | **1** | 23 |
| `verification_evidence_recorded` | 1 | 2 | 9 |
| `cross_enrollment_submission_rejected` | 1 | **1** | 5 |
| `ledger_entry_chained` | 1 | 2 | 30 |
| `execution_cancelled_while_running` | 1 | *unreachable* | 4 |
| `provenance_envelope_created` | 1 | *unreachable* | 4 |

Both previously-unfloored classes are now floored. `persistence_fault_fired_in_submission`
is the thin one at exactly 1, where `capability_observation_recorded` and
`cross_enrollment_submission_rejected` used to be.

`MYCELIUM_CAMPAIGN_COUNTS=<path>` writes the observed counts to a JSON file, so
this table is re-measured rather than transcribed from a CI log. Two consecutive
runs whose wall times differed by 19 seconds produced byte-identical counts.

Every floor is 1 deliberately. A floor tuned up to the edge of what a profile
reliably produces becomes flaky, and a flaky guard gets deleted by the next
person, which is how the guard dies. `capability_observation_recorded` and
`cross_enrollment_submission_rejected` are the thin ones at exactly 1 - if a
future change drops one to 0, that is the guard working, and the failure message
names the class and prints everything the run *did* reach.

Counts change whenever a rule is added or removed, **and whenever the coordinator
changes how much SQLite work a settlement does**, because the fault injector
counts operations and its armed index then lands somewhere else. Theme 3C is the
clearest example: adding one `SELECT` to the settlement transaction for the
ledger chain dropped `idempotency_replayed` and `verification_evidence_recorded`
to zero at the then-current budget. The budget was raised from 60 to 75 steps
rather than the floor lowered, and every count here was re-measured rather than
carried over. Raising a budget to keep a guard honest is the trade the guard is
for; lowering the floor to keep a build green would not have been.

### The two "structurally unreachable" classes were not structural

Theme 3B-1 and Theme 3C each recorded a class as impossible to reach here, and
each blamed `TestClient` for cancelling background work. Both explanations were
wrong in the same way, and Theme 4B found out by testing the premise instead of
the conclusion.

The harness constructed its two *requester* clients without entering them as
context managers. An un-entered `TestClient` starts a fresh `anyio` blocking
portal per request and closes it on the way out, and closing a portal cancels
its task group - which is the background execution task. The worker client had
been entered all along; the requester clients had not. Measured before anything
was changed: an execution whose strategy awaits lands `interrupted` with the
clients unentered and `completed` with them entered.

So `execution_cancelled_while_running` and `provenance_envelope_created` are
both floored now, at 4 and 4. The earlier measurements were real - making the
strategy park *did* produce `interrupted` every time - but they measured the
harness, and the conclusion drawn from them was about the framework.

A background task that outlives its request has to be waited for deliberately.
The harness gained condition waits rather than timed ones
(`await_execution_running`, `await_execution_terminal`, `quiesce`): they block
until a durable fact is true, so a slower machine takes longer to arrive and
observes the same thing. `quiesce` also runs before a persistence fault is
armed, because the injector counts SQLite operations process-wide and leftover
background work would otherwise consume the armed index - a machine-speed
dependency in disguise, of exactly the kind Theme 1.5 removed from node
staleness.

### What the two new rules cost, and what was done about it

Adding two rules regenerates every sequence, and the thin classes reshuffled
just as Theme 3C's one extra `SELECT` reshuffled them. `lease_reclaimed_by_janitor`
went to zero. Three responses were measured before one was chosen:

| attempt | result |
| --- | --- |
| leave spare units queued for a later poll | janitor class restored, `settlement_accepted` starved to 1 |
| balance the two lease lengths of those spares | still 1 |
| raise the budget to 24 examples (292 s) | still 1 |

Which pointed at something worth finding. Finding F4's fix made the settlement
chain *available* at the start of a sequence and never again: once the initial
handouts have been settled, expired, superseded, or reclaimed, an honest
submission needs a generated enqueue and a generated poll to line up before
anything else destroys the result. `settlement_accepted` had been swinging
between 1 and 6 across whole runs on nothing but a change to `max_examples`, so
every property downstream of an acceptance - replay, credit, the ledger chain,
the capability observation, verification evidence - was riding on the draw.
Instrumenting the janitor said the same thing from the other end: across a run
it swept 128 times, found an active attempt in 4 of those, and found nothing in
flight at all in 111.

So the honest-worker rule and the janitor-wake rule now take a unit when the
sequence is holding none, the same way `open_the_network` establishes the
sequence's prerequisites. What is generated stays generated: whether the rule
runs, against which handout, with which output, and in what order relative to
every adversarial rule. What stops being generated is only whether the worker
had a lease to be honest with.

**Neither the budget was raised nor a floor lowered.** `max_examples` is still
15 and `stateful_step_count` is still 75.

No class was deferred to the extended profile. All eight are reached by the
default CI profile. Because that profile is derandomized with no example
database, these counts are a property of the code and the Hypothesis version,
not of the machine — they reproduce identically on CI. A Hypothesis upgrade can
change generation and drop a thin class to zero; that surfaces as this test
failing and naming the class, which is the correct outcome, and is why the
dependency is pinned below 7.

This is the standing guard against F4 recurring.
`test_campaign_reads_the_tables_it_asserts_on` covers the other half of the same
failure mode: an invariant querying a mistyped table name would pass vacuously
forever.

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

*Classification: real defect, fixed in Theme 1.5.*

`server_state._cleanup_pass` decides worker-lease expiry **and** node staleness
(`_NODE_TIMEOUT`, 90 s) from the same coordinator clock reading. A forward jump
larger than 90 s — an NTP correction, a resumed laptop — therefore reclaims every
node's in-flight work as stale, not only the leases that actually expired.

Nothing unsafe followed — reclaimed work is requeued, no attempt settles twice,
no credit is created — but it is wrong, and on a home server an NTP step or a
resumed suspended host is not exotic.

*Resolution (Theme 1.5):* the two decisions were separated. A lease deadline is
an absolute point in time, issued at handout and durable across a restart, so it
stays on `coordinator_now()`. Heartbeat recency is an elapsed *duration* held in
process-local session state that does not survive a restart anyway, so it moved
to `server_state.coordinator_monotonic()`, which a wall-clock correction cannot
move. `last_seen` remains a wall-clock timestamp in node and session views,
because that is what operators read; the monotonic reading sits beside it and is
what staleness is actually decided with.

Deliberately unchanged: lease issue, lease-expiry evaluation, `_NODE_TIMEOUT`'s
value, reclaim policy, the durable deadline representation, and the
attempt-authority model. A node record carrying no monotonic reading falls back
to the previous wall-clock behaviour exactly. A backward jump still cannot revive
a durably expired lease —
`test_a_backward_coordinator_clock_cannot_revive_a_durably_expired_lease`
asserts it, unchanged.

Covered by `test_a_forward_clock_jump_expires_only_leases_that_actually_ran_out`,
`test_a_forward_clock_jump_does_not_reclaim_a_healthy_nodes_work`, and
`test_a_node_that_stops_heartbeating_still_goes_stale_with_no_clock_jump` — the
last one because a fix that quietly turned staleness detection off would pass the
first two.

One consequence for the tests: several existing tests simulated a silent node by
backdating `last_seen` on the wall clock. What they assert is unchanged, but how
they express "this node stopped talking" had to change, because that is no longer
a wall-clock fact. They now use one shared `age_node_record` / `age_node_session`
helper.

With staleness no longer following it, the generated campaign's clock cap rose
from ±80 s to ±3000 s — bounded now by `_RESULT_TTL` (3600 s), the one remaining
wall-clock consumer in the sweep, which prunes a compatibility mirror the model
does not track.

What that actually reaches, measured over a CI-profile run (724 clock readings):
offsets from **−179 s to +296 s**, with **29 %** of readings past `_NODE_TIMEOUT`.
The cap is not the binding constraint — `rewind_clock` pulls the offset back, so
it random-walks rather than climbing — but the region past 90 s, which the old
±80 s cap made unreachable by construction, is now routine.

### F8 — the secret scan failed CI at random

*Classification: test defect. Found by CI, not locally.*

The campaign's teardown scan reported
`sqlite:events.db contains identity material` on a **docs-only** commit, having
passed on the commit before it and on the pull-request run five minutes later.
Hypothesis labelled it `FlakyFailure` — it did not reproduce on replay — because
the values involved are generated by the server at run time, not by the
generator, so `derandomize` does not make them repeat.

The probe was the problem. `scan_for_secrets` searched a few megabytes of SQLite
for every secret-class value, and the idempotency keys were `"k0"` and `"k1"`.
A two-character needle cannot distinguish a leak from arithmetic: it turns up in
binary data often enough to fail a build at random. This is ROADMAP §2's warning
running backwards — not a measurement missing a real finding, but a measurement
asserting one that is not there.

Everything else was eliminated before concluding that. Across a full CI-profile
run — 17 databases, 4.3 MB — the counts were: admission secret 0, enrollment
credentials 0, server-minted session tokens and attempt nonces 0 (182 distinct
values checked), `"k0"` 0, `"k1"` 0. The long needles are not present, and only
the two-character ones can appear by chance at all.

*Resolution*, three parts:

* Idempotency keys became 44-character synthetic values. Still opaque, still
  content-free, now long enough that a match is evidence.
* The scan reports which *class* of value matched — admission secret, enrollment
  credential, idempotency key, or server-minted token/nonce — and never the
  value. The original failure said only "identity material", which is why it took
  a reconstruction rather than a reading to classify. A recurrence will say
  which.
* The scan now reads `events.db-wal` as well as `events.db`. A write that has not
  been checkpointed lives only in the WAL, so the old probe could have let a real
  leak hide behind WAL timing. (This immediately produced a second, smaller false
  positive — the WAL legitimately mirrors the canonical store's request text and
  accepted output — so the prompt/output exemption now covers `events.db` *and*
  its WAL, and nothing else.)

`test_every_secret_probe_is_long_enough_to_mean_something` is the standing guard,
in the same spirit as the coverage floor: a probe shorter than 32 characters
fails the build rather than producing random red.

Eight consecutive CI-profile runs passed after the change, against roughly one
failure in ten before it. That is not proof — the honest statement is that the
only needle capable of colliding by chance is gone, and any recurrence now names
its class instead of leaving the next person where this entry started.

### Nothing deferred under protocol case 4

No finding required a design change, so there are no `xfail` or skipped
reproducers in this PR.

---

## Seams introduced

Four, each the narrowest thing that makes one scenario reachable.

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

**`server_state.coordinator_monotonic()`** — one function returning
`time.monotonic()`, used for heartbeat recency and node staleness and nothing
else. This one is a production fix rather than a test seam (F7): reading a
duration from the wall clock was the bug. Like `coordinator_now`, it is a plain
function - no injectable clock object, no interface, no configuration.

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
* **Clock offsets beyond ±3000 s in generation.** The cap is now set by
  `_RESULT_TTL`, not by node staleness; see F7. Larger jumps, and the monotonic
  isolation itself, are covered deterministically in the scenarios module.
* **WebSocket event delivery and the dashboard.** The event *stream* is asserted
  against durable state; its transport is not exercised.

---

## CI runtime cost

Measured on this branch: Windows 11, Python 3.14.3, 8 GB, CPU-only. **CI runs
Linux on a GitHub-hosted runner, so the numbers there will differ.**

Every observation taken on this branch, not a selected one:

| Command | Observations |
| --- | --- |
| `pytest -q tests/test_protocol_state_machine.py` (at 60 steps) | 51.1 s, 55.0 s, 61.9 s, 104.5 s, 111.9 s, 124.9 s, 128.6 s |
| `pytest -q tests/test_protocol_state_machine.py` (at 75 steps, Theme 3C) | 268.9 s, 317.8 s |
| `pytest -q tests/test_protocol_state_machine.py` (at 75 steps, Theme 4B) | 286.7 s, 305.6 s, 326.7 s |
| `pytest -q tests/test_adversarial_scenarios.py` | 36.6 s, 40.2 s, 43.5 s, 49.7 s, 55.4 s, 56.9 s, 62.8 s, 146.0 s, 160.2 s |
| both modules in one invocation | 90.8 s, 91.4 s |
| `pytest -q` (whole suite, with both) | 466.4 s |
| extended profile (not in `pytest -q`) | 378.3 s |

The same command varies by 2.5x on this machine, and the two combined runs were
*faster* than the sum of the modules run alone. So the honest statement is a
range — **the campaign adds roughly 90–190 s to `pytest -q` here** — and not a
point estimate. Re-measure on the runner that matters before quoting a figure.

Theme 3B-1 added a rule and three floor classes to the campaign and re-measured
rather than carrying the old figures forward. The range did not move — which,
given how wide it is, is weak evidence that the additions were cheap rather than
proof of it. Theme 4A changed nothing in the campaign and re-measured anyway; the
observations landed inside the existing range.

**Theme 3C moved it, and not because of measurement noise.** Raising the step
budget from 60 to 75 - to restore the coverage headroom the ledger chain's extra
`SELECT` consumed - took the campaign module from a 51-129 s range to 269-318 s,
and the scenarios module to 146-160 s. That is a real increase of roughly three
minutes on `pytest -q`, paid to keep the coverage floor honest rather than to
lower it. Whether that stays affordable is a fair question for the next person
who touches this; the alternative on the table was a guard that no longer
guarded.

**Theme 4B did not move it.** Executions now run to completion instead of being
cancelled by a collapsing event loop, which is more work per submission, and two
rules were added - and the module landed at 286.7 s, 305.6 s and 326.7 s, inside
Theme 3C's range. The likeliest reason the extra work is invisible is that the
cancellation it replaced was not free either: the portal teardown that killed
each background task ran inside the request that started it, so the cost was
already being paid, just as a cancellation rather than as a result. The step
budget was unchanged, which is the other half of the answer.

Two of those three runs are also the determinism evidence: 305.6 s and 286.7 s,
19 seconds apart in wall time, produced byte-identical coverage counts.

What is not in doubt is the direction and the reason. The campaign module got
slower in Theme 1.5, because the coverage floor showed that at the previous
15x14 budget it reached no accepted settlements at all: the cheaper number was
buying an assertion that every invariant held about a run in which almost nothing
happened. Steps are the cheap axis — an example costs ~2 s to set up and ~50 ms
per step — so the budget went into steps rather than examples.

Most of the scenario module is still the two parametrized fault sweeps (26
injected disk failures across the submission path, 9 commit-time failures at the
settlement boundary), each building an isolated coordinator per index. They are
also the two highest-value tests here, so they were not trimmed to make this
table look better.
