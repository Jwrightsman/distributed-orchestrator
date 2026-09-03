# ADR 0014 — Verification evidence is durable, scoped, and never authoritative over terminal state

**Status:** Accepted (2026-09-03, Theme 3B-1)

**Context:** ROADMAP §6 lists "persist reputation and verifier evidence instead
of losing it with the process" as open. Post-hoc verification is disabled in
trusted alpha because its status and evidence had no durable semantics: the
sampled comparison's verdict lived in a process-local dictionary and vanished
with the process.

## Decision

Post-hoc verification evidence is a **separate, append-only, exactly-scoped**
durable record. It references an execution, unit, attempt, and accepted receipt.
It never mutates any of them.

### Why it cannot live on the execution row

ADR 0009 makes terminal execution state monotonic and never reclassified: once a
terminal snapshot is committed, later telemetry, callbacks, or event-delivery
failures cannot reopen or reclassify it. Post-hoc verification happens *after*
terminal, by definition. If a verification result could write back to the
execution, then either terminal state stops being monotonic or verification stops
being post-hoc. Neither is acceptable, so the evidence lives beside the execution
instead of inside it.

This is not only a convention. `verification_evidence` has **no foreign key**
into `executions` or `attempts`, so it cannot block, cascade into, or restrict
terminal state; and `BEFORE UPDATE` / `BEFORE DELETE` triggers make the table
append-only in the database, not merely by agreement. The adversarial campaign's
`execution_lifecycle_is_monotonic` and `terminal_attempts_never_reopen`
invariants continue to hold, and a new invariant asserts that every evidence row
names an attempt that is `settled` and holds an accepted receipt.

### Categories that stay separate

| | what it is | where it lives |
| --- | --- | --- |
| contract-floor validation | structural checks at terminal time | the execution's validation summary |
| post-hoc verification | evidence produced after terminal | `verification_evidence` |
| agreement | two outputs matched in *shape* | `verification_evidence`, a distinct `verifier_kind` |
| assurance | what a task class's evidence supports | **not built** — Theme 3B-2 |
| reputation | — | **not built, and not planned here** |

Collapsing these into one number is the failure mode this repository has avoided
since ADR 0004, and it is avoided here structurally rather than by discipline:

* The outcome vocabulary contains **no word for correctness**. A deterministic
  check is `passed` or `failed`; a comparison is `agreed` or `disagreed`.
* The two vocabularies do not overlap, and `verifier_kind` is part of the scope
  key. There is therefore no aggregate in which an agreement contributes to a
  pass rate — not because a reader would be careful, but because the rows do not
  meet.
* Recording a structural contract-floor validator (`nonempty`,
  `artifact_extraction`, `artifact_contract`, `file_manifest`) as post-hoc
  verification is **refused**. A structural failure is not a statement about
  semantic correctness, and the store will not let one be written as though it
  were.
* The protected read surface says in its own response body that it is not
  reputation, not correctness, not assurance, and influences nothing.

### Scoping

The scope is the same discipline as ADR 0012: subject enrollment ID, identity
class, descriptor version and hash, executor kind/version, worker protocol
version, model provider/name/digest/variant, task class, and verifier
kind/name/version. Changing any of them starts a cold scope. History earned
under one descriptor, one model, or one verifier version is never inherited by
another.

Task class reuses Theme 2C's vocabulary (`dag_subtask`, `candidate`) rather than
inventing a second one. The two subsystems answer different questions but they
partition work the same way, and a second vocabulary would guarantee they
eventually disagree.

Rows without enrolled identity are `identity_class = 'legacy'`. They are never
merged with enrolled scopes and an enrollment is never inferred from a reusable
node label. The operator surface reports the legacy count separately.

### Deterministic identity and replay safety

`evidence_id` is a domain-separated SHA-256 over execution, unit, attempt,
receipt, subject enrollment, verifier kind/name/version, and a subject key. It is
a pure function of authoritative identity, so replay safety does not depend on
any in-memory de-duplication:

* accepted-result exact replay, restart reconciliation, repeated callbacks, and
  event redelivery all converge on **one** row;
* a re-run of the same verifier at the same version against the same subject is
  the same row;
* a re-run at a **different version** is a new row, and the earlier record is not
  overwritten — a deterministic ID that already names different content raises
  `VerificationEvidenceConflict` rather than silently replacing anything.

### Containment

Evidence writes are best-effort with respect to the execution path. A failure to
record must never fail an execution, alter settlement, change eligibility, or
delay a handout. Following Theme 2.1: durable accounting for what succeeded,
process-local counters for failures of the recording path itself, and no
recursive attempt to record the failure of failure-recording.

### Fault attribution

Every record carries an explicit attribution, and only outcomes supportable by
authoritative coordinator state are recorded. **Only `subject_output` may carry
an outcome about the subject.** This is enforced in Python and again as a table
CHECK constraint, so a future writer cannot bypass it.

| attribution | may say | why |
| --- | --- | --- |
| `subject_output` | passed / failed / agreed / disagreed | the subject's own output was examined |
| `requester_cancelled` | `not_run` only | the requester withdrew the work |
| `coordinator_shutdown` | `not_run` only | the coordinator stopped; that is ours |
| `coordinator_persistence_failure` | `not_run` only | our storage failed, not their work |
| `pre_assignment_deadline` | `not_run` only | the deadline had passed before anyone was asked |
| `verifier_unavailable` | `not_run` only | the verifier crashed, timed out, or was absent |
| `unattributed` | `not_run` only | the policy declined to attribute |

Malformed or mismatched authority credentials are **not recordable at all**.
Those are security events handled by attempt authority; recording one here would
turn an authentication rejection into evidence against a node.

`not_run` records are excluded from the attributable sample count, so a
cancellation or a coordinator restart can never depress a scope's observed rate.

## What this ADR does *not* decide

**Nothing is re-enabled.** `verify_rate` remains `0.0` by default and
trusted-alpha mode still disables sampled verification. The point of this change
is that durability now exists, not that the feature is switched on. Whether to
re-enable it in trusted alpha is a separate decision requiring its own evidence.
Before that decision could reasonably be made:

1. sampled verification's *own* terminal state must be durable, not just its
   evidence — a sampled attempt that disappears mid-flight is still untracked;
2. the cost must be measured, not assumed: each sample is a whole extra
   inference on hardware where a task takes minutes;
3. there must be a documented answer to what a disagreement *means*, given that
   the coordinator cannot tell which of two outputs is wrong (ROADMAP §5,
   "Layered verification");
4. the operator-facing wording must be settled, so that "agreement" is never read
   as "correct" by whoever looks at the dashboard.

**Task-class assurance ladders are deferred to Theme 3B-2.** They need this
substrate to exist first, and they need a decision about what evidence justifies
what claim — which is an assurance question, not an evidence question. Building
the ladder in the same change as the substrate would have meant designing the
storage around one consumer before knowing whether it was the right one.

**This is not reputation.** There is no score, no ranking, no leaderboard, and no
comparison across operators. Contribution points are untouched by every outcome
here; they remain what they have always been — a record that a nonempty,
attempt-bound worker result was accepted.

## Consequences

* A later PR can build assurance on a substrate that is already durable, scoped,
  replay-safe, and attribution-aware.
* Evidence accumulates from now on even though nothing consumes it yet, which is
  the point: a decision about re-enabling sampled verification should be made
  against real data rather than an argument.
* One more table to migrate and reason about, in a database that already carries
  attempts, receipts, contributions, capability observations, and shadow
  decisions. It is additive and append-only, and it initializes idempotently on
  fresh and existing databases.
* The append-only triggers mean evidence cannot be pruned by the ordinary path.
  If retention ever becomes a problem, that is a deliberate future decision with
  its own ADR, not something a maintenance script should do quietly.
