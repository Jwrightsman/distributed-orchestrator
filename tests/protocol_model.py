"""A reference model of Mycelium's intended protocol semantics.

This file is deliberately the *specification*, not a second implementation. It
imports nothing from the coordinator, holds no SQLite, and knows nothing about
HTTP. It answers one question per operation — "what should happen?" — coarsely
enough that a reviewer can read it end to end and decide whether it agrees with
`docs/PROTOCOL.md`.

Where the real system distinguishes fifteen rejection reasons, the model says
`REJECT`. That is on purpose: an oracle that reproduces the implementation's
branching is an oracle that reproduces the implementation's bugs.

The vocabulary here matches `docs/adversarial-campaign.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ── Outcomes the model predicts ──────────────────────────────────────


class Settlement(str, Enum):
    """What a result submission should do to durable state."""

    ACCEPT = "accept"          # settles, creates exactly one receipt and one credit
    REPLAY = "replay"          # returns the stored response, creates nothing
    REJECT = "reject"          # never settles; may land in bounded quarantine
    PAYLOAD_LIMIT = "payload_limit"   # bounds enforced before the atomic transition


class Bootstrap(str, Enum):
    CREATED = "created"
    IDEMPOTENT = "idempotent"
    LABEL_CONFLICT = "label_conflict"
    CREDENTIAL_CONFLICT = "credential_conflict"
    REVOKED = "revoked"


class Submission(str, Enum):
    CREATED = "created"
    REPLAYED = "replayed"
    CONFLICT = "conflict"


TERMINAL_LIFECYCLES = frozenset({"completed", "failed", "cancelled", "interrupted"})
TERMINAL_ATTEMPT_STATES = frozenset(
    {"settled", "expired", "reclaimed", "cancelled", "superseded", "interrupted"}
)

# The protocol's own limits, restated so a silent production change fails here.
# Worker error text is bounded twice: the request model caps characters, and
# settlement caps UTF-8 bytes. Which one binds depends on the encoding, and a
# submission refused by the first never reaches the second.
MAX_ERROR_BYTES = 2048
MAX_ERROR_CHARACTERS = 2048


# ── Modelled entities ────────────────────────────────────────────────


@dataclass
class ModelEnrollment:
    enrollment_id: str
    label: str
    credential: str
    credential_version: int = 1
    status: str = "active"

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass
class ModelSession:
    session_id: str
    label: str
    enrollment_id: str | None
    credential_version: int | None
    valid: bool = True


@dataclass
class ModelAttempt:
    """One leased execution authority."""

    attempt_id: str
    task_id: str
    execution_id: str | None
    unit_id: str | None
    unit_kind: str | None
    contract_version: str | None
    label: str
    session_id: str | None
    enrollment_id: str | None
    credential_version: int | None
    nonce: str
    lease_expires_at: float
    max_output_bytes: int
    state: str = "active"

    @property
    def active(self) -> bool:
        return self.state == "active"


@dataclass
class ModelExecution:
    execution_id: str
    lifecycle: str = "queued"
    terminal_classification: str | None = None


@dataclass
class ModelSubmissionMapping:
    request_hash: str
    execution_id: str


@dataclass
class ProtocolModel:
    """Intended coordinator behaviour, as a plain state machine."""

    enrollments: dict[str, ModelEnrollment] = field(default_factory=dict)
    sessions: dict[str, ModelSession] = field(default_factory=dict)
    attempts: dict[str, ModelAttempt] = field(default_factory=dict)
    executions: dict[str, ModelExecution] = field(default_factory=dict)

    # attempt_id -> the canonical payload digest that settled it.
    receipts: dict[str, str] = field(default_factory=dict)
    # attempt_id -> enrollment_id (or None for legacy compatibility work).
    credits: dict[str, str | None] = field(default_factory=dict)
    # (requester_scope, idempotency_key) -> mapping
    submissions: dict[tuple[str, str], ModelSubmissionMapping] = field(default_factory=dict)

    # Everything a caller has already been shown, so "durable before public"
    # has something concrete to compare a post-restart durable read against.
    observed_lifecycle: dict[str, set[str]] = field(default_factory=dict)

    # ── enrollment ───────────────────────────────────────────────────

    def enrollment_for_label(self, label: str) -> ModelEnrollment | None:
        for enrollment in self.enrollments.values():
            if enrollment.label == label:
                return enrollment
        return None

    def enrollment_for_credential(self, credential: str) -> ModelEnrollment | None:
        for enrollment in self.enrollments.values():
            if enrollment.credential == credential:
                return enrollment
        return None

    def expect_bootstrap(self, label: str, credential: str) -> Bootstrap:
        """Bootstrap admits one enrollment per label and per credential.

        The label is checked first, and revocation beats everything: a revoked
        enrollment cannot be re-bootstrapped, not even by presenting the exact
        original pair, because that would make revocation undoable by the party
        it was used against. Only then does a credential mismatch become a label
        conflict, and only a label nobody holds falls through to the credential
        check.

        This is the whole of the duplicate-identity-registration guarantee, and
        it is deliberately not a Sybil defence: N distinct labels with N distinct
        credentials produce N enrollments, and the model says so.
        """
        by_label = self.enrollment_for_label(label)
        if by_label is not None:
            if not by_label.active:
                return Bootstrap.REVOKED
            if by_label.credential != credential:
                return Bootstrap.LABEL_CONFLICT
            return Bootstrap.IDEMPOTENT
        if self.enrollment_for_credential(credential) is not None:
            return Bootstrap.CREDENTIAL_CONFLICT
        return Bootstrap.CREATED

    def apply_bootstrap(self, enrollment_id: str, label: str, credential: str) -> ModelEnrollment:
        enrollment = ModelEnrollment(enrollment_id, label, credential)
        self.enrollments[enrollment_id] = enrollment
        return enrollment

    def apply_revoke(self, enrollment_id: str) -> None:
        enrollment = self.enrollments.get(enrollment_id)
        if enrollment is None:
            return
        enrollment.status = "revoked"
        self._invalidate_enrollment(enrollment_id, "reclaimed")

    def apply_rotate(self, enrollment_id: str, credential: str) -> None:
        enrollment = self.enrollments.get(enrollment_id)
        if enrollment is None:
            return
        enrollment.credential = credential
        enrollment.credential_version += 1
        self._invalidate_enrollment(enrollment_id, "reclaimed")

    def _invalidate_enrollment(self, enrollment_id: str, attempt_state: str) -> None:
        for session in self.sessions.values():
            if session.enrollment_id == enrollment_id:
                session.valid = False
        for attempt in self.attempts.values():
            if attempt.active and attempt.enrollment_id == enrollment_id:
                attempt.state = attempt_state

    # ── sessions ─────────────────────────────────────────────────────

    def apply_session(
        self,
        session_id: str,
        label: str,
        enrollment_id: str | None,
        credential_version: int | None,
    ) -> ModelSession:
        """A new incarnation replaces the label's previous one.

        Sessions are process-local by design (ADR 0005). The replaced session
        stops authorising work immediately, and any lease it still held is
        reclaimed rather than left for a late submission to settle.
        """
        for existing in self.sessions.values():
            if existing.label == label and existing.valid:
                existing.valid = False
                for attempt in self.attempts.values():
                    if attempt.active and attempt.session_id == existing.session_id:
                        attempt.state = "reclaimed"
        session = ModelSession(session_id, label, enrollment_id, credential_version)
        self.sessions[session_id] = session
        return session

    def restart(self) -> None:
        """A coordinator restart is a new epoch.

        Every session becomes invalid and every live lease is durably
        interrupted, so a late result fails closed instead of settling into a
        process that has no dispatcher waiting for it. Nothing already terminal
        moves, and nothing already accepted is un-accepted.
        """
        for session in self.sessions.values():
            session.valid = False
        for attempt in self.attempts.values():
            if attempt.active:
                attempt.state = "interrupted"
        for execution in self.executions.values():
            if execution.lifecycle in ("queued", "running"):
                self._set_lifecycle(execution, "interrupted")

    # ── attempts ─────────────────────────────────────────────────────

    def apply_issue(self, attempt: ModelAttempt) -> None:
        for existing in self.attempts.values():
            if existing.task_id == attempt.task_id and existing.active:
                existing.state = "superseded"
        self.attempts[attempt.attempt_id] = attempt

    def active_attempt_for_task(self, task_id: str) -> ModelAttempt | None:
        for attempt in self.attempts.values():
            if attempt.task_id == task_id and attempt.active:
                return attempt
        return None

    def apply_terminal(self, attempt_id: str, state: str) -> None:
        attempt = self.attempts.get(attempt_id)
        if attempt is not None and attempt.active:
            attempt.state = state

    def expire_leases(self, now: float) -> list[str]:
        expired = [
            attempt.attempt_id
            for attempt in self.attempts.values()
            if attempt.active and attempt.lease_expires_at < now
        ]
        for attempt_id in expired:
            self.attempts[attempt_id].state = "expired"
        return expired

    # ── settlement ───────────────────────────────────────────────────

    def expect_settlement(
        self,
        *,
        task_id: str,
        claimed_attempt_id: str | None,
        claimed_nonce: str | None,
        claimed_label: str,
        claimed_execution_id: str | None,
        claimed_unit_id: str | None,
        claimed_unit_kind: str | None,
        claimed_contract_version: str | None,
        session_id: str | None,
        payload_digest: str,
        output_bytes: int,
        error_bytes: int,
        error_characters: int,
        now: float,
    ) -> Settlement:
        """Decide what a submission does, in the order the protocol states.

        The active server-issued attempt is authoritative. A submitter cannot
        redirect settlement by naming a different attempt, a different node
        label, or a different session; it can only settle the attempt the
        coordinator currently holds open for this task, and only if every bound
        field it echoes matches.
        """
        if error_characters > MAX_ERROR_CHARACTERS:
            # Refused by the request model before any attempt is consulted.
            return Settlement.REJECT

        # The presented session must itself be live and must own the label the
        # submission claims. That is the coordinator's front door; nothing
        # below it can be reached by a caller holding someone else's token.
        session = self.sessions.get(session_id) if session_id else None
        if session is None or not session.valid or session.label != claimed_label:
            return Settlement.REJECT

        attempt = self.active_attempt_for_task(task_id)
        if attempt is None and claimed_attempt_id:
            attempt = self.attempts.get(claimed_attempt_id)
        if attempt is None:
            return Settlement.REJECT

        if claimed_label != attempt.label:
            return Settlement.REJECT

        if attempt.enrollment_id is not None:
            if session.enrollment_id != attempt.enrollment_id:
                return Settlement.REJECT
            enrollment = self.enrollments.get(attempt.enrollment_id)
            if enrollment is None or not enrollment.active:
                return Settlement.REJECT
            if session.credential_version != attempt.credential_version:
                # An exact replay of an already-settled attempt is recoverable
                # by a later incarnation of the same still-active enrollment; a
                # live settlement by a newer credential version is not.
                if attempt.state != "settled":
                    return Settlement.REJECT
        elif session.enrollment_id is not None:
            # An enrolled session may not claim legacy compatibility work.
            return Settlement.REJECT
        if not claimed_attempt_id or claimed_attempt_id != attempt.attempt_id:
            return Settlement.REJECT
        if not claimed_nonce or claimed_nonce != attempt.nonce:
            return Settlement.REJECT
        if attempt.contract_version == "1":
            if claimed_contract_version != "1":
                return Settlement.REJECT
            if claimed_execution_id != attempt.execution_id:
                return Settlement.REJECT
            if claimed_unit_id != attempt.unit_id:
                return Settlement.REJECT
            if claimed_unit_kind != attempt.unit_kind:
                return Settlement.REJECT
        if attempt.session_id is not None and attempt.state != "settled":
            if session_id != attempt.session_id:
                return Settlement.REJECT

        if attempt.state == "settled":
            stored = self.receipts.get(attempt.attempt_id)
            return Settlement.REPLAY if stored == payload_digest else Settlement.REJECT
        if attempt.state != "active":
            return Settlement.REJECT
        if now > attempt.lease_expires_at:
            return Settlement.REJECT
        if output_bytes > attempt.max_output_bytes or error_bytes > MAX_ERROR_BYTES:
            return Settlement.PAYLOAD_LIMIT
        return Settlement.ACCEPT

    def apply_settlement(self, attempt_id: str, payload_digest: str) -> None:
        """Acceptance is exactly one receipt and exactly one credit."""
        attempt = self.attempts[attempt_id]
        attempt.state = "settled"
        self.receipts[attempt_id] = payload_digest
        self.credits[attempt_id] = attempt.enrollment_id

    def apply_payload_limit(self, attempt_id: str) -> None:
        attempt = self.attempts.get(attempt_id)
        if attempt is not None and attempt.active:
            attempt.state = "cancelled"

    def apply_lease_expiry(self, attempt_id: str) -> None:
        attempt = self.attempts.get(attempt_id)
        if attempt is not None and attempt.active:
            attempt.state = "expired"

    # ── canonical execution submission ───────────────────────────────

    def expect_submission(self, scope: str, key: str | None, request_hash: str) -> Submission:
        """Keyed submission converges; an unkeyed one always creates."""
        if key is None:
            return Submission.CREATED
        mapping = self.submissions.get((scope, key))
        if mapping is None:
            return Submission.CREATED
        if mapping.request_hash == request_hash:
            return Submission.REPLAYED
        return Submission.CONFLICT

    def apply_submission(
        self,
        scope: str,
        key: str | None,
        request_hash: str,
        execution_id: str,
    ) -> None:
        self.executions[execution_id] = ModelExecution(execution_id)
        if key is not None:
            self.submissions[(scope, key)] = ModelSubmissionMapping(request_hash, execution_id)

    def _set_lifecycle(self, execution: ModelExecution, lifecycle: str) -> None:
        if execution.lifecycle in TERMINAL_LIFECYCLES:
            return
        execution.lifecycle = lifecycle
        if lifecycle in TERMINAL_LIFECYCLES:
            execution.terminal_classification = lifecycle

    def apply_lifecycle(self, execution_id: str, lifecycle: str) -> None:
        execution = self.executions.get(execution_id)
        if execution is not None:
            self._set_lifecycle(execution, lifecycle)

    def observe(self, execution_id: str, lifecycle: str) -> None:
        """Record that a caller was shown this lifecycle for this execution."""
        self.observed_lifecycle.setdefault(execution_id, set()).add(lifecycle)

    # ── the invariants, stated once ──────────────────────────────────

    def credited_attempts(self) -> set[str]:
        return set(self.credits)

    def accepted_attempts(self) -> set[str]:
        return set(self.receipts)

    def settled_attempts_per_task(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for attempt in self.attempts.values():
            if attempt.state == "settled":
                counts[attempt.task_id] = counts.get(attempt.task_id, 0) + 1
        return counts
