"""Durable, scoped, append-only post-hoc verification evidence.

Post-hoc verification happens *after* an execution is terminal. ADR 0009 makes
terminal state monotonic and never reclassified, so a verification result cannot
live on the execution row: it is a separate append-only record that references an
execution, attempt, and accepted receipt without mutating any of them. Nothing in
this module can change a lifecycle state, a validation outcome, an assurance
level, an artifact seal, a settlement, or a contribution.

Four categories stay separate here, as they do for capability evidence:

    contract-floor validation  structural checks at terminal time (elsewhere)
    post-hoc verification      what this module records
    agreement                  two outputs matched in shape - NOT correctness
    assurance                  what a task class's evidence supports (Theme 3B-2)

Reputation is none of these and is not implemented. There is no score, no
ranking, and no aggregate that reads as correctness. See ADR 0014.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, TypeVar, get_args

from capability_evidence import TaskClass
from node_capabilities import LEGACY_DESCRIPTOR_HASH
from sqlite_store import connection, migration_lock


VERIFICATION_EVIDENCE_SCHEMA_VERSION = "1"
MAX_IDENTIFIER_LENGTH = 256
MAX_METADATA_JSON_BYTES = 1024
MAX_METADATA_VALUE_LENGTH = 64

_T = TypeVar("_T")

VerifierKind = Literal["deterministic_check", "sampled_reexecution"]

VerificationOutcome = Literal[
    "passed",      # deterministic check ran and the subject's output satisfied it
    "failed",      # deterministic check ran and the subject's output did not
    "agreed",      # a comparison run produced a matching output *shape*
    "disagreed",   # a comparison run produced a differing output *shape*
    "not_run",     # no evidence about the subject; see fault_attribution
]

# Every record says who a non-result is owed to. Only `subject_output` may carry
# an outcome that says anything about the subject's work; everything else is a
# statement about the coordinator, the requester, or the verifier, and is
# recorded so that a missing result is not silently read as a failure.
FaultAttribution = Literal[
    "subject_output",
    "requester_cancelled",
    "coordinator_shutdown",
    "coordinator_persistence_failure",
    "pre_assignment_deadline",
    "verifier_unavailable",
    "unattributed",
]

IdentityClass = Literal["enrolled", "legacy"]

_VERIFIER_KINDS = frozenset(get_args(VerifierKind))
_OUTCOMES = frozenset(get_args(VerificationOutcome))
_ATTRIBUTIONS = frozenset(get_args(FaultAttribution))
_IDENTITY_CLASSES = frozenset(get_args(IdentityClass))
_TASK_CLASSES = frozenset(get_args(TaskClass))

# Which outcomes each verifier kind may produce. A deterministic check cannot
# "agree" with anything, and a comparison run cannot "pass".
_KIND_OUTCOMES: dict[str, frozenset[str]] = {
    "deterministic_check": frozenset({"passed", "failed", "not_run"}),
    "sampled_reexecution": frozenset({"agreed", "disagreed", "not_run"}),
}

# Only the subject's own output can be evidence about the subject. Every other
# attribution means the run did not happen, so its only legal outcome is
# `not_run` - a coordinator restart is not a node failing a check.
_SUBJECT_OUTCOMES = frozenset({"passed", "failed", "agreed", "disagreed"})

# A malformed or mismatched authority credential is a security event handled by
# attempt authority, not a verification outcome about anyone's work. Recording it
# here would turn an authentication rejection into evidence against a node.
_REJECTED_ATTRIBUTIONS = frozenset(
    {"malformed_authority_credentials", "authority_mismatch", "unauthenticated"}
)

# Structural contract-floor validators are checked at terminal time and are
# already recorded as structural evidence. Re-recording one here would let a
# structural failure be read as a statement about semantic correctness, which is
# exactly the conflation ADR 0004 separates.
STRUCTURAL_VALIDATOR_NAMES = frozenset(
    {"nonempty", "artifact_extraction", "artifact_contract", "file_manifest"}
)

# Bounded, content-free metadata. No prompts, outputs, artifact contents,
# schemas, credentials, tokens, nonces, or worker error text can be expressed.
_METADATA_KEYS = frozenset(
    {"method_version", "comparison_pair_id", "check_name", "sample_index"}
)

VerificationCounterName = Literal[
    "record_failed",
    "scope_unresolved",
    "read_failed",
]
_COUNTER_NAMES = frozenset(get_args(VerificationCounterName))


class VerificationEvidenceConflict(RuntimeError):
    """A deterministic evidence ID already names different immutable content."""


def _domain_digest(domain: str, payload: str) -> str:
    return hashlib.sha256(
        domain.encode("ascii") + b"\0" + payload.encode("utf-8")
    ).hexdigest()


def _bounded_text(
    value: object, *, field_name: str, maximum: int = MAX_IDENTIFIER_LENGTH
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError(
            f"{field_name} must be 1-{maximum} characters without outer whitespace"
        )
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} must contain printable non-whitespace characters")
    return value


def _optional_bounded_text(
    value: object, *, field_name: str, maximum: int
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name=field_name, maximum=maximum)


def _timestamp(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite timestamp")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{field_name} must be a finite non-negative timestamp")
    return parsed


def _canonical_metadata(metadata: Mapping[str, object] | None) -> str:
    """Serialize an allowlisted, bounded, content-free metadata mapping."""

    if metadata is None:
        return "{}"
    if not isinstance(metadata, Mapping):
        raise ValueError("verification evidence metadata must be a mapping")
    cleaned: dict[str, str] = {}
    for key, value in metadata.items():
        if key not in _METADATA_KEYS:
            raise ValueError(f"verification evidence metadata key is not allowed: {key!r}")
        cleaned[key] = _bounded_text(
            value, field_name=f"metadata[{key}]", maximum=MAX_METADATA_VALUE_LENGTH
        )
    encoded = json.dumps(cleaned, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_METADATA_JSON_BYTES:
        raise ValueError("verification evidence metadata exceeds its byte bound")
    return encoded


@dataclass(frozen=True)
class VerificationScope:
    """The exact aggregation boundary. No field may be inferred across scopes.

    A descriptor change, a model change, or a verifier version change starts a
    cold scope, exactly as it does for capability evidence: history earned under
    one configuration is never inherited by another.
    """

    subject_enrollment_id: str | None
    identity_class: IdentityClass
    descriptor_version: str | None
    descriptor_hash: str
    executor_kind: str | None
    executor_version: str | None
    worker_protocol_version: str | None
    model_provider: str | None
    model_name: str | None
    model_digest: str | None
    model_variant: str | None
    task_class: TaskClass
    verifier_kind: VerifierKind
    verifier_name: str
    verifier_version: str

    @property
    def scope_key(self) -> str:
        payload = json.dumps(
            {
                "descriptor_hash": self.descriptor_hash,
                "descriptor_version": self.descriptor_version,
                "executor_kind": self.executor_kind,
                "executor_version": self.executor_version,
                "identity_class": self.identity_class,
                "model_digest": self.model_digest,
                "model_name": self.model_name,
                "model_provider": self.model_provider,
                "model_variant": self.model_variant,
                "subject_enrollment_id": self.subject_enrollment_id,
                "task_class": self.task_class,
                "verifier_kind": self.verifier_kind,
                "verifier_name": self.verifier_name,
                "verifier_version": self.verifier_version,
                "worker_protocol_version": self.worker_protocol_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return _domain_digest("mycelium.verification-evidence-scope.v1", payload)


@dataclass(frozen=True)
class VerificationEvidenceRecord:
    evidence_id: str
    execution_id: str
    unit_id: str | None
    attempt_id: str | None
    receipt_id: str | None
    subject_node_id: str | None
    scope: VerificationScope
    outcome: VerificationOutcome
    fault_attribution: FaultAttribution
    numeric_value: float | None
    metadata: Mapping[str, str]
    occurred_at: float
    recorded_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> VerificationEvidenceRecord:
        metadata = json.loads(str(row["metadata_json"]))
        if not isinstance(metadata, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        ):
            raise RuntimeError("stored verification evidence metadata is invalid")
        if any(key not in _METADATA_KEYS for key in metadata):
            raise RuntimeError("stored verification evidence metadata is not allowlisted")
        outcome = str(row["outcome"])
        attribution = str(row["fault_attribution"])
        if outcome not in _OUTCOMES or attribution not in _ATTRIBUTIONS:
            raise RuntimeError("stored verification evidence enum is invalid")
        return cls(
            evidence_id=str(row["evidence_id"]),
            execution_id=str(row["execution_id"]),
            unit_id=_none_or_str(row["unit_id"]),
            attempt_id=_none_or_str(row["attempt_id"]),
            receipt_id=_none_or_str(row["receipt_id"]),
            subject_node_id=_none_or_str(row["subject_node_id"]),
            scope=_scope_from_row(row),
            outcome=outcome,  # type: ignore[arg-type]
            fault_attribution=attribution,  # type: ignore[arg-type]
            numeric_value=(
                float(row["numeric_value"]) if row["numeric_value"] is not None else None
            ),
            metadata=metadata,
            occurred_at=float(row["occurred_at"]),
            recorded_at=float(row["recorded_at"]),
        )


def _none_or_str(value: object) -> str | None:
    return None if value is None else str(value)


def _scope_from_row(row: sqlite3.Row) -> VerificationScope:
    return VerificationScope(
        subject_enrollment_id=_none_or_str(row["subject_enrollment_id"]),
        identity_class=str(row["identity_class"]),  # type: ignore[arg-type]
        descriptor_version=_none_or_str(row["descriptor_version"]),
        descriptor_hash=str(row["descriptor_hash"]),
        executor_kind=_none_or_str(row["executor_kind"]),
        executor_version=_none_or_str(row["executor_version"]),
        worker_protocol_version=_none_or_str(row["worker_protocol_version"]),
        model_provider=_none_or_str(row["model_provider"]),
        model_name=_none_or_str(row["model_name"]),
        model_digest=_none_or_str(row["model_digest"]),
        model_variant=_none_or_str(row["model_variant"]),
        task_class=str(row["task_class"]),  # type: ignore[arg-type]
        verifier_kind=str(row["verifier_kind"]),  # type: ignore[arg-type]
        verifier_name=str(row["verifier_name"]),
        verifier_version=str(row["verifier_version"]),
    )


def evidence_id_for(
    *,
    execution_id: str,
    unit_id: str | None,
    attempt_id: str | None,
    receipt_id: str | None,
    subject_enrollment_id: str | None,
    verifier_kind: str,
    verifier_name: str,
    verifier_version: str,
    subject_key: str,
) -> str:
    """Derive the deterministic identity of one verification observation.

    Replay safety is a property of this function, not of any in-memory set. The
    same verifier at the same version, against the same subject work, produces
    the same ID however many times settlement replays, the coordinator restarts,
    or a callback is redelivered. A *different* verifier version produces a
    different ID, so a re-run adds a record instead of overwriting one.
    """

    payload = json.dumps(
        {
            "attempt_id": attempt_id,
            "execution_id": execution_id,
            "receipt_id": receipt_id,
            "subject_enrollment_id": subject_enrollment_id,
            "subject_key": subject_key,
            "unit_id": unit_id,
            "verifier_kind": verifier_kind,
            "verifier_name": verifier_name,
            "verifier_version": verifier_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _domain_digest("mycelium.verification-evidence.v1", payload)


@dataclass(frozen=True)
class BestEffortResult(Generic[_T]):
    succeeded: bool
    value: _T | None
    error_code: str | None = None


@dataclass(frozen=True)
class OutcomeCounts:
    """Counts only. There is no score here and no ranking across operators."""

    passed: int = 0
    failed: int = 0
    agreed: int = 0
    disagreed: int = 0
    not_run: int = 0

    @property
    def attributable_sample_count(self) -> int:
        """Records that say something about the subject's own output."""
        return self.passed + self.failed + self.agreed + self.disagreed

    def as_dict(self) -> dict[str, int]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "agreed": self.agreed,
            "disagreed": self.disagreed,
            "not_run": self.not_run,
        }


@dataclass(frozen=True)
class BinaryAggregate:
    sample_count: int
    positive_count: int
    negative_count: int
    rate: float | None
    wilson_low: float | None
    wilson_high: float | None


@dataclass(frozen=True)
class VerificationScopeSummary:
    """Privacy-bounded operator projection. Never individual observations."""

    scope: VerificationScope
    subject_node_id: str | None
    outcome_counts: OutcomeCounts
    attribution_counts: Mapping[str, int]
    observed: BinaryAggregate
    minimum_samples: int
    insufficient_evidence: bool
    last_observed_at: float


def _binary_aggregate(sample_count: int, positive_count: int) -> BinaryAggregate:
    negative_count = sample_count - positive_count
    if sample_count == 0:
        return BinaryAggregate(0, 0, 0, None, None, None)
    rate = positive_count / sample_count
    z = 1.959963984540054
    denominator = 1 + z * z / sample_count
    center = (rate + z * z / (2 * sample_count)) / denominator
    margin = (
        z
        * math.sqrt(
            rate * (1 - rate) / sample_count + z * z / (4 * sample_count * sample_count)
        )
        / denominator
    )
    return BinaryAggregate(
        sample_count=sample_count,
        positive_count=positive_count,
        negative_count=negative_count,
        rate=rate,
        wilson_low=max(0.0, center - margin),
        wilson_high=min(1.0, center + margin),
    )


class VerificationEvidenceProcessCounters:
    """Non-durable counters for failures of the recording path itself.

    Theme 2.1's pattern: durable accounting for what succeeded, process-local
    counters for what could not be written, and no recursive attempt to record
    the failure of failure-recording.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counts: dict[str, int] = {name: 0 for name in _COUNTER_NAMES}

    def increment(self, name: str) -> int:
        if name not in _COUNTER_NAMES:
            raise ValueError("unsupported verification evidence counter")
        with self._lock:
            self._counts[name] += 1
            return self._counts[name]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def reset(self) -> None:
        with self._lock:
            self._counts = {name: 0 for name in _COUNTER_NAMES}


def ensure_verification_evidence_schema(con: sqlite3.Connection) -> None:
    """Install the additive append-only verification evidence schema.

    Safe to call repeatedly, on a fresh database or an existing one. There is
    deliberately no foreign key to the execution or attempt tables: evidence
    references them but must never be able to block, cascade into, or otherwise
    reach back into terminal state.
    """

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_evidence (
            evidence_id       TEXT PRIMARY KEY CHECK(length(evidence_id) = 64),
            schema_version    TEXT NOT NULL CHECK(schema_version = '1'),
            execution_id      TEXT NOT NULL CHECK(length(execution_id) BETWEEN 1 AND 256),
            unit_id           TEXT CHECK(unit_id IS NULL OR length(unit_id) BETWEEN 1 AND 256),
            attempt_id        TEXT CHECK(attempt_id IS NULL OR length(attempt_id) BETWEEN 1 AND 256),
            receipt_id        TEXT CHECK(receipt_id IS NULL OR length(receipt_id) BETWEEN 1 AND 256),
            subject_node_id   TEXT CHECK(subject_node_id IS NULL OR length(subject_node_id) BETWEEN 1 AND 256),
            subject_enrollment_id TEXT,
            identity_class    TEXT NOT NULL CHECK(identity_class IN ('enrolled', 'legacy')),
            descriptor_version TEXT CHECK(descriptor_version IS NULL OR length(descriptor_version) BETWEEN 1 AND 16),
            descriptor_hash   TEXT NOT NULL CHECK(length(descriptor_hash) = 64),
            executor_kind     TEXT CHECK(executor_kind IS NULL OR length(executor_kind) BETWEEN 1 AND 64),
            executor_version  TEXT CHECK(executor_version IS NULL OR length(executor_version) BETWEEN 1 AND 64),
            worker_protocol_version TEXT CHECK(worker_protocol_version IS NULL OR length(worker_protocol_version) BETWEEN 1 AND 32),
            model_provider    TEXT CHECK(model_provider IS NULL OR length(model_provider) BETWEEN 1 AND 64),
            model_name        TEXT CHECK(model_name IS NULL OR length(model_name) BETWEEN 1 AND 128),
            model_digest      TEXT CHECK(model_digest IS NULL OR length(model_digest) = 71),
            model_variant     TEXT CHECK(model_variant IS NULL OR length(model_variant) BETWEEN 1 AND 64),
            task_class        TEXT NOT NULL CHECK(task_class IN ('dag_subtask', 'candidate')),
            verifier_kind     TEXT NOT NULL CHECK(verifier_kind IN ('deterministic_check', 'sampled_reexecution')),
            verifier_name     TEXT NOT NULL CHECK(length(verifier_name) BETWEEN 1 AND 64),
            verifier_version  TEXT NOT NULL CHECK(length(verifier_version) BETWEEN 1 AND 32),
            outcome           TEXT NOT NULL CHECK(outcome IN (
                'passed', 'failed', 'agreed', 'disagreed', 'not_run'
            )),
            fault_attribution TEXT NOT NULL CHECK(fault_attribution IN (
                'subject_output', 'requester_cancelled', 'coordinator_shutdown',
                'coordinator_persistence_failure', 'pre_assignment_deadline',
                'verifier_unavailable', 'unattributed'
            )),
            numeric_value     REAL,
            metadata_json     TEXT NOT NULL CHECK(length(metadata_json) <= 1024),
            occurred_at       REAL NOT NULL,
            recorded_at       REAL NOT NULL,
            -- Table-level constraints follow every column, as SQLite requires.
            --
            -- Only the subject's own output may carry an outcome about the
            -- subject. Enforced in the schema as well as in Python, so a future
            -- writer cannot bypass it.
            CHECK(
                (fault_attribution = 'subject_output')
                = (outcome IN ('passed', 'failed', 'agreed', 'disagreed'))
            ),
            -- Kinds do not share an outcome vocabulary.
            CHECK(
                (verifier_kind = 'deterministic_check'
                 AND outcome IN ('passed', 'failed', 'not_run'))
                OR (verifier_kind = 'sampled_reexecution'
                    AND outcome IN ('agreed', 'disagreed', 'not_run'))
            ),
            CHECK(
                (identity_class = 'enrolled') = (subject_enrollment_id IS NOT NULL)
            )
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_verification_evidence_scope "
        "ON verification_evidence("
        "subject_enrollment_id, descriptor_hash, executor_kind, executor_version, "
        "worker_protocol_version, model_provider, model_name, model_digest, "
        "model_variant, task_class, verifier_kind, verifier_name, verifier_version, "
        "occurred_at)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_verification_evidence_execution "
        "ON verification_evidence(execution_id, attempt_id)"
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_verification_evidence_no_update
        BEFORE UPDATE ON verification_evidence
        BEGIN
            SELECT RAISE(ABORT, 'verification evidence is append-only');
        END
        """
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_verification_evidence_no_delete
        BEFORE DELETE ON verification_evidence
        BEGIN
            SELECT RAISE(ABORT, 'verification evidence is append-only');
        END
        """
    )


class VerificationEvidenceStore:
    """Append-only durable verification evidence and exact-scope aggregation.

    This store has no API that can change eligibility, queue order, leases,
    assignment, settlement, or contribution. That is a property of what is
    absent, and ADR 0014 records it.
    """

    def __init__(self, path: str | Path = "events.db"):
        self.path = Path(path)
        self._lock = threading.RLock()

    def migrate(self) -> None:
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            ensure_verification_evidence_schema(con)
            con.commit()

    # ── containment ──────────────────────────────────────────────────

    @staticmethod
    def best_effort(
        operation: Callable[..., _T], *args: object, **kwargs: object
    ) -> BestEffortResult[_T]:
        """Contain evidence work that owns its transaction.

        Recording evidence is never allowed to fail an execution, alter a
        settlement, change eligibility, or delay a handout.
        """

        try:
            return BestEffortResult(succeeded=True, value=operation(*args, **kwargs))
        except Exception:
            return BestEffortResult(
                succeeded=False, value=None, error_code="verification_evidence_write_failed"
            )

    @staticmethod
    def best_effort_in_transaction(
        con: sqlite3.Connection,
        operation: Callable[..., _T],
        *args: object,
        **kwargs: object,
    ) -> BestEffortResult[_T]:
        """Contain optional evidence work without poisoning its caller's transaction."""

        con.execute("SAVEPOINT verification_evidence_best_effort")
        try:
            value = operation(*args, **kwargs)
        except Exception:
            con.execute("ROLLBACK TO verification_evidence_best_effort")
            con.execute("RELEASE verification_evidence_best_effort")
            return BestEffortResult(
                succeeded=False,
                value=None,
                error_code="verification_evidence_write_failed",
            )
        con.execute("RELEASE verification_evidence_best_effort")
        return BestEffortResult(succeeded=True, value=value)

    # ── writing ──────────────────────────────────────────────────────

    @staticmethod
    def validate(
        *,
        verifier_kind: str,
        verifier_name: str,
        outcome: str,
        fault_attribution: str,
    ) -> None:
        """Reject anything the evidence model does not permit, with a reason.

        Kept separate from the insert so callers and tests can ask the model what
        it allows without touching a database.
        """

        if fault_attribution in _REJECTED_ATTRIBUTIONS:
            raise ValueError(
                "authority failures are security events handled by attempt "
                "authority, not verification outcomes about a node's work"
            )
        if verifier_kind not in _VERIFIER_KINDS:
            raise ValueError("unsupported verification verifier kind")
        if outcome not in _OUTCOMES:
            raise ValueError("unsupported verification outcome")
        if fault_attribution not in _ATTRIBUTIONS:
            raise ValueError("unsupported verification fault attribution")
        if outcome not in _KIND_OUTCOMES[verifier_kind]:
            raise ValueError(
                f"outcome {outcome!r} is not in the {verifier_kind!r} vocabulary"
            )
        if (fault_attribution == "subject_output") != (outcome in _SUBJECT_OUTCOMES):
            raise ValueError(
                "only subject_output may carry an outcome about the subject; every "
                "other attribution means the run did not happen"
            )
        if (
            verifier_kind == "deterministic_check"
            and verifier_name in STRUCTURAL_VALIDATOR_NAMES
        ):
            raise ValueError(
                "structural contract-floor validators are recorded as structural "
                "evidence at terminal time; recording one here would read a "
                "structural failure as semantic incorrectness"
            )

    def record(
        self,
        *,
        execution_id: str,
        task_class: str,
        verifier_kind: str,
        verifier_name: str,
        verifier_version: str,
        outcome: str,
        fault_attribution: str,
        unit_id: str | None = None,
        attempt_id: str | None = None,
        receipt_id: str | None = None,
        subject_node_id: str | None = None,
        subject_enrollment_id: str | None = None,
        descriptor_version: str | None = None,
        descriptor_hash: str | None = None,
        executor_kind: str | None = None,
        executor_version: str | None = None,
        worker_protocol_version: str | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        model_digest: str | None = None,
        model_variant: str | None = None,
        subject_key: str = "default",
        numeric_value: float | None = None,
        metadata: Mapping[str, object] | None = None,
        occurred_at: float | None = None,
        recorded_at: float | None = None,
    ) -> VerificationEvidenceRecord:
        """Append one verification observation, idempotently."""

        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                record = self.record_in_transaction(
                    con,
                    execution_id=execution_id,
                    task_class=task_class,
                    verifier_kind=verifier_kind,
                    verifier_name=verifier_name,
                    verifier_version=verifier_version,
                    outcome=outcome,
                    fault_attribution=fault_attribution,
                    unit_id=unit_id,
                    attempt_id=attempt_id,
                    receipt_id=receipt_id,
                    subject_node_id=subject_node_id,
                    subject_enrollment_id=subject_enrollment_id,
                    descriptor_version=descriptor_version,
                    descriptor_hash=descriptor_hash,
                    executor_kind=executor_kind,
                    executor_version=executor_version,
                    worker_protocol_version=worker_protocol_version,
                    model_provider=model_provider,
                    model_name=model_name,
                    model_digest=model_digest,
                    model_variant=model_variant,
                    subject_key=subject_key,
                    numeric_value=numeric_value,
                    metadata=metadata,
                    occurred_at=occurred_at,
                    recorded_at=recorded_at,
                )
                con.commit()
                return record
            except Exception:
                if con.in_transaction:
                    con.rollback()
                raise

    def record_in_transaction(
        self,
        con: sqlite3.Connection,
        *,
        execution_id: str,
        task_class: str,
        verifier_kind: str,
        verifier_name: str,
        verifier_version: str,
        outcome: str,
        fault_attribution: str,
        unit_id: str | None = None,
        attempt_id: str | None = None,
        receipt_id: str | None = None,
        subject_node_id: str | None = None,
        subject_enrollment_id: str | None = None,
        descriptor_version: str | None = None,
        descriptor_hash: str | None = None,
        executor_kind: str | None = None,
        executor_version: str | None = None,
        worker_protocol_version: str | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        model_digest: str | None = None,
        model_variant: str | None = None,
        subject_key: str = "default",
        numeric_value: float | None = None,
        metadata: Mapping[str, object] | None = None,
        occurred_at: float | None = None,
        recorded_at: float | None = None,
    ) -> VerificationEvidenceRecord:
        ensure_verification_evidence_schema(con)

        execution_id = _bounded_text(execution_id, field_name="execution_id")
        verifier_name = _bounded_text(verifier_name, field_name="verifier_name", maximum=64)
        verifier_version = _bounded_text(
            verifier_version, field_name="verifier_version", maximum=32
        )
        subject_key = _bounded_text(subject_key, field_name="subject_key", maximum=128)
        if task_class not in _TASK_CLASSES:
            raise ValueError("task_class must be dag_subtask or candidate")
        self.validate(
            verifier_kind=verifier_kind,
            verifier_name=verifier_name,
            outcome=outcome,
            fault_attribution=fault_attribution,
        )

        unit_id = _optional_bounded_text(unit_id, field_name="unit_id", maximum=256)
        attempt_id = _optional_bounded_text(attempt_id, field_name="attempt_id", maximum=256)
        receipt_id = _optional_bounded_text(receipt_id, field_name="receipt_id", maximum=256)
        subject_node_id = _optional_bounded_text(
            subject_node_id, field_name="subject_node_id", maximum=256
        )
        subject_enrollment_id = _optional_bounded_text(
            subject_enrollment_id, field_name="subject_enrollment_id", maximum=64
        )
        # A row without enrolled identity is legacy. It is never guessed at from a
        # reusable node label, and it aggregates separately.
        identity_class: IdentityClass = (
            "enrolled" if subject_enrollment_id is not None else "legacy"
        )
        if descriptor_hash is None:
            descriptor_hash = LEGACY_DESCRIPTOR_HASH
        descriptor_hash = _bounded_text(
            descriptor_hash, field_name="descriptor_hash", maximum=64
        )
        if len(descriptor_hash) != 64 or any(
            character not in "0123456789abcdef" for character in descriptor_hash
        ):
            raise ValueError("descriptor_hash must be a lowercase SHA-256 digest")

        if numeric_value is not None:
            if isinstance(numeric_value, bool) or not isinstance(
                numeric_value, (int, float)
            ):
                raise ValueError("numeric evidence must be a finite number")
            numeric_value = float(numeric_value)
            if not math.isfinite(numeric_value) or numeric_value < 0:
                raise ValueError("numeric evidence must be finite and non-negative")

        now = time.time()
        occurred_at = _timestamp(
            now if occurred_at is None else occurred_at, field_name="occurred_at"
        )
        recorded_at = _timestamp(
            now if recorded_at is None else recorded_at, field_name="recorded_at"
        )
        if recorded_at < occurred_at:
            raise ValueError("recorded_at cannot precede occurred_at")
        metadata_json = _canonical_metadata(metadata)

        evidence_id = evidence_id_for(
            execution_id=execution_id,
            unit_id=unit_id,
            attempt_id=attempt_id,
            receipt_id=receipt_id,
            subject_enrollment_id=subject_enrollment_id,
            verifier_kind=verifier_kind,
            verifier_name=verifier_name,
            verifier_version=verifier_version,
            subject_key=subject_key,
        )
        scope = VerificationScope(
            subject_enrollment_id=subject_enrollment_id,
            identity_class=identity_class,
            descriptor_version=_optional_bounded_text(
                descriptor_version, field_name="descriptor_version", maximum=16
            ),
            descriptor_hash=descriptor_hash,
            executor_kind=_optional_bounded_text(
                executor_kind, field_name="executor_kind", maximum=64
            ),
            executor_version=_optional_bounded_text(
                executor_version, field_name="executor_version", maximum=64
            ),
            worker_protocol_version=_optional_bounded_text(
                worker_protocol_version, field_name="worker_protocol_version", maximum=32
            ),
            model_provider=_optional_bounded_text(
                model_provider, field_name="model_provider", maximum=64
            ),
            model_name=_optional_bounded_text(
                model_name, field_name="model_name", maximum=128
            ),
            model_digest=_optional_bounded_text(
                model_digest, field_name="model_digest", maximum=71
            ),
            model_variant=_optional_bounded_text(
                model_variant, field_name="model_variant", maximum=64
            ),
            task_class=task_class,  # type: ignore[arg-type]
            verifier_kind=verifier_kind,  # type: ignore[arg-type]
            verifier_name=verifier_name,
            verifier_version=verifier_version,
        )

        con.execute(
            """
            INSERT OR IGNORE INTO verification_evidence (
                evidence_id, schema_version, execution_id, unit_id, attempt_id,
                receipt_id, subject_node_id, subject_enrollment_id, identity_class,
                descriptor_version, descriptor_hash, executor_kind, executor_version,
                worker_protocol_version, model_provider, model_name, model_digest,
                model_variant, task_class, verifier_kind, verifier_name,
                verifier_version, outcome, fault_attribution, numeric_value,
                metadata_json, occurred_at, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                VERIFICATION_EVIDENCE_SCHEMA_VERSION,
                execution_id,
                unit_id,
                attempt_id,
                receipt_id,
                subject_node_id,
                subject_enrollment_id,
                identity_class,
                scope.descriptor_version,
                scope.descriptor_hash,
                scope.executor_kind,
                scope.executor_version,
                scope.worker_protocol_version,
                scope.model_provider,
                scope.model_name,
                scope.model_digest,
                scope.model_variant,
                task_class,
                verifier_kind,
                verifier_name,
                verifier_version,
                outcome,
                fault_attribution,
                numeric_value,
                metadata_json,
                occurred_at,
                recorded_at,
            ),
        )
        row = con.execute(
            "SELECT * FROM verification_evidence WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        if row is None:  # pragma: no cover - SQLite insert contract
            raise RuntimeError("verification evidence disappeared after insertion")
        record = VerificationEvidenceRecord.from_row(row)
        # A deterministic ID that already names different content is a modelling
        # error, not something to overwrite. Append-only means the first write
        # wins and the conflict is reported.
        if (
            record.scope != scope
            or record.outcome != outcome
            or record.fault_attribution != fault_attribution
            or record.execution_id != execution_id
            or record.attempt_id != attempt_id
            or record.receipt_id != receipt_id
            or record.unit_id != unit_id
        ):
            raise VerificationEvidenceConflict(
                "verification evidence ID conflicts with immutable stored content"
            )
        return record

    # ── reading ──────────────────────────────────────────────────────

    def get(self, evidence_id: str) -> VerificationEvidenceRecord | None:
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute(
                "SELECT * FROM verification_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        return None if row is None else VerificationEvidenceRecord.from_row(row)

    def count(self) -> int:
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute("SELECT COUNT(*) AS n FROM verification_evidence").fetchone()
        return int(row["n"]) if row else 0

    def legacy_count(self) -> int:
        """Rows without enrolled identity, held separately and never merged."""
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute(
                "SELECT COUNT(*) AS n FROM verification_evidence "
                "WHERE identity_class = 'legacy'"
            ).fetchone()
        return int(row["n"]) if row else 0

    def list_scope_summaries(
        self,
        *,
        subject_enrollment_id: str | None = None,
        descriptor_hash: str | None = None,
        task_class: str | None = None,
        verifier_kind: str | None = None,
        limit: int = 100,
        minimum_samples: int = 5,
    ) -> tuple[VerificationScopeSummary, ...]:
        """Bounded exact-scope summaries for a protected operator surface.

        Deterministic-check and agreement evidence never share a scope, because
        `verifier_kind` is part of the scope key. There is therefore no aggregate
        in which an agreement contributes to a pass rate.
        """

        if isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if isinstance(minimum_samples, bool) or not 1 <= minimum_samples <= 10_000:
            raise ValueError("minimum_samples must be between 1 and 10000")
        if subject_enrollment_id is not None:
            subject_enrollment_id = _bounded_text(
                subject_enrollment_id, field_name="subject_enrollment_id", maximum=64
            )
        if descriptor_hash is not None:
            descriptor_hash = _bounded_text(
                descriptor_hash, field_name="descriptor_hash", maximum=64
            )
            if len(descriptor_hash) != 64 or any(
                character not in "0123456789abcdef" for character in descriptor_hash
            ):
                raise ValueError("descriptor_hash must be a lowercase SHA-256 digest")
        if task_class is not None and task_class not in _TASK_CLASSES:
            raise ValueError("task_class must be dag_subtask or candidate")
        if verifier_kind is not None and verifier_kind not in _VERIFIER_KINDS:
            raise ValueError("unsupported verification verifier kind")

        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in (
            ("subject_enrollment_id", subject_enrollment_id),
            ("descriptor_hash", descriptor_hash),
            ("task_class", task_class),
            ("verifier_kind", verifier_kind),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = " AND ".join(clauses) if clauses else "1 = 1"
        group_columns = (
            "subject_node_id, subject_enrollment_id, identity_class, "
            "descriptor_version, descriptor_hash, executor_kind, executor_version, "
            "worker_protocol_version, model_provider, model_name, model_digest, "
            "model_variant, task_class, verifier_kind, verifier_name, verifier_version"
        )

        self.migrate()
        summaries: list[VerificationScopeSummary] = []
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            rows = con.execute(
                f"SELECT {group_columns}, MAX(occurred_at) AS last_observed_at "
                f"FROM verification_evidence WHERE {where} "
                f"GROUP BY {group_columns} "
                "ORDER BY last_observed_at DESC, verifier_name "
                "LIMIT ?",
                [*parameters, limit],
            ).fetchall()
            for row in rows:
                scope = _scope_from_row(row)
                summaries.append(
                    self._summarize_scope(
                        con,
                        scope,
                        subject_node_id=_none_or_str(row["subject_node_id"]),
                        last_observed_at=float(row["last_observed_at"]),
                        minimum_samples=minimum_samples,
                    )
                )
        return tuple(summaries)

    def _summarize_scope(
        self,
        con: sqlite3.Connection,
        scope: VerificationScope,
        *,
        subject_node_id: str | None,
        last_observed_at: float,
        minimum_samples: int,
    ) -> VerificationScopeSummary:
        where, parameters = _scope_clause(scope)
        outcome_rows = con.execute(
            f"SELECT outcome, COUNT(*) AS n FROM verification_evidence "
            f"WHERE {where} GROUP BY outcome",
            parameters,
        ).fetchall()
        counts = {str(row["outcome"]): int(row["n"]) for row in outcome_rows}
        outcomes = OutcomeCounts(
            passed=counts.get("passed", 0),
            failed=counts.get("failed", 0),
            agreed=counts.get("agreed", 0),
            disagreed=counts.get("disagreed", 0),
            not_run=counts.get("not_run", 0),
        )
        attribution_rows = con.execute(
            f"SELECT fault_attribution, COUNT(*) AS n FROM verification_evidence "
            f"WHERE {where} GROUP BY fault_attribution",
            parameters,
        ).fetchall()
        attributions = {
            str(row["fault_attribution"]): int(row["n"]) for row in attribution_rows
        }

        # `observed` is the proportion of attributable records whose outcome was
        # the affirmative one for this verifier kind. For a deterministic check
        # that is "passed"; for a comparison it is "agreed", which describes
        # output shape and is deliberately not called a pass rate.
        sample_count = outcomes.attributable_sample_count
        positive = (
            outcomes.passed
            if scope.verifier_kind == "deterministic_check"
            else outcomes.agreed
        )
        return VerificationScopeSummary(
            scope=scope,
            subject_node_id=subject_node_id,
            outcome_counts=outcomes,
            attribution_counts=attributions,
            observed=_binary_aggregate(sample_count, positive),
            minimum_samples=minimum_samples,
            insufficient_evidence=sample_count < minimum_samples,
            last_observed_at=last_observed_at,
        )


def _scope_clause(scope: VerificationScope) -> tuple[str, list[object]]:
    columns = (
        ("subject_enrollment_id", scope.subject_enrollment_id),
        ("identity_class", scope.identity_class),
        ("descriptor_version", scope.descriptor_version),
        ("descriptor_hash", scope.descriptor_hash),
        ("executor_kind", scope.executor_kind),
        ("executor_version", scope.executor_version),
        ("worker_protocol_version", scope.worker_protocol_version),
        ("model_provider", scope.model_provider),
        ("model_name", scope.model_name),
        ("model_digest", scope.model_digest),
        ("model_variant", scope.model_variant),
        ("task_class", scope.task_class),
        ("verifier_kind", scope.verifier_kind),
        ("verifier_name", scope.verifier_name),
        ("verifier_version", scope.verifier_version),
    )
    clauses: list[str] = []
    parameters: list[object] = []
    for column, value in columns:
        if value is None:
            clauses.append(f"{column} IS NULL")
        else:
            clauses.append(f"{column} = ?")
            parameters.append(value)
    return " AND ".join(clauses), parameters
