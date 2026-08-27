"""Durable, exactly-scoped node capability evidence and shadow evaluation.

This module deliberately has no scheduler dependency.  It records bounded facts
about already-issued attempts and can compute a hypothetical preference among
callers' already-hard-eligible candidates, but it has no API capable of changing
eligibility, queue order, leases, or assignment.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, TypeVar

from node_capabilities import (
    LEGACY_DESCRIPTOR_HASH,
    NodeCapabilitySnapshotRecord,
    ensure_node_capability_snapshot_schema,
    normalize_model_digest,
)
from sqlite_store import connection, migration_lock


EVIDENCE_SCHEMA_VERSION = "1"
SHADOW_POLICY_VERSION = "1"
CONTRACT_FLOOR_PROJECTION_VERSION = "contract-floor-v1"
DEADLINE_COMPLETION_SUBJECT = "nonempty_output_before_lease_v2"
MAX_IDENTIFIER_LENGTH = 256
MAX_METADATA_JSON_BYTES = 1024
MAX_RECENT_SAMPLES = 1000
MAX_SHADOW_CANDIDATES = 64
MAX_OBSERVED_OUTPUT_BYTES = 10_485_760
MAX_COORDINATOR_WALL_SECONDS = 30 * 24 * 60 * 60

TaskClass = Literal["dag_subtask", "candidate"]
EvidenceRole = Literal["production", "sampled_comparison"]
ObservationType = Literal[
    "settlement_outcome",
    "deadline_completion",
    "coordinator_wall_seconds",
    "output_bytes",
    "effective_output_bytes_per_second",
    "terminal_outcome",
    "contract_floor",
    "sampled_agreement",
]
ShadowOutcome = Literal["same", "different", "no_preference"]
ShadowOperationalPhase = Literal["admission", "evaluation"]
ShadowAdmissionOutcome = Literal[
    "disabled",
    "not_applicable",
    "queue_saturated",
    "scope_capture_failed",
    "scheduled",
]
ShadowEvaluationOutcome = Literal[
    "completed",
    "evaluator_failed",
    "decision_write_failed",
    "cancelled_on_shutdown",
]
ShadowOperationalOutcome = ShadowAdmissionOutcome | ShadowEvaluationOutcome
ShadowOperationalCounterName = Literal[
    "durable_health_record_write_failure",
    "unexpected_containment_failure",
    "background_task_callback_failure",
]
FutureActiveExperimentBlockingReason = Literal[
    "legacy_descriptor_identity",
    "descriptor_identity_unreconstructable",
    "immutable_model_identity_missing",
    "model_identity_unreconstructable",
]

FUTURE_ACTIVE_EXPERIMENT_BLOCKING_REASON_ORDER: tuple[
    FutureActiveExperimentBlockingReason, ...
] = (
    "legacy_descriptor_identity",
    "descriptor_identity_unreconstructable",
    "immutable_model_identity_missing",
    "model_identity_unreconstructable",
)

SHADOW_ADMISSION_OUTCOMES: tuple[ShadowAdmissionOutcome, ...] = (
    "disabled",
    "not_applicable",
    "queue_saturated",
    "scope_capture_failed",
    "scheduled",
)
SHADOW_EVALUATION_OUTCOMES: tuple[ShadowEvaluationOutcome, ...] = (
    "completed",
    "evaluator_failed",
    "decision_write_failed",
    "cancelled_on_shutdown",
)
SHADOW_OPERATION_COUNTER_NAMES: tuple[ShadowOperationalCounterName, ...] = (
    "durable_health_record_write_failure",
    "unexpected_containment_failure",
    "background_task_callback_failure",
)

_SHADOW_OPERATION_REASON_CODES: dict[
    tuple[ShadowOperationalPhase, ShadowOperationalOutcome], frozenset[str]
] = {
    ("admission", "disabled"): frozenset({"mode_disabled"}),
    ("admission", "not_applicable"): frozenset(
        {
            "legacy_descriptor_identity",
            "nonproduction_attempt",
            "unsupported_task_class",
        }
    ),
    ("admission", "queue_saturated"): frozenset(
        {"background_queue_limit_reached"}
    ),
    ("admission", "scope_capture_failed"): frozenset(
        {
            "scope_capture_failed",
            "coordinator_shutdown_during_scope_capture",
        }
    ),
    ("admission", "scheduled"): frozenset({"evaluation_scheduled"}),
    ("evaluation", "completed"): frozenset({"decision_persisted"}),
    ("evaluation", "evaluator_failed"): frozenset({"evaluator_failed"}),
    ("evaluation", "decision_write_failed"): frozenset(
        {"decision_write_failed"}
    ),
    ("evaluation", "cancelled_on_shutdown"): frozenset(
        {"coordinator_shutdown"}
    ),
}

_OBSERVATION_TYPES: frozenset[str] = frozenset(
    {
        "settlement_outcome",
        "deadline_completion",
        "coordinator_wall_seconds",
        "output_bytes",
        "effective_output_bytes_per_second",
        "terminal_outcome",
        "contract_floor",
        "sampled_agreement",
    }
)
_OBSERVATION_OUTCOMES: dict[str, frozenset[str]] = {
    "settlement_outcome": frozenset(
        {"settled_output", "settled_worker_error", "settled_empty_output"}
    ),
    "deadline_completion": frozenset({"pass", "fail"}),
    "coordinator_wall_seconds": frozenset({"measured"}),
    "output_bytes": frozenset({"measured"}),
    "effective_output_bytes_per_second": frozenset({"measured"}),
    "terminal_outcome": frozenset({"lease_expired", "node_stale"}),
    "contract_floor": frozenset({"pass", "fail"}),
    "sampled_agreement": frozenset({"agree", "disagree"}),
}
_NUMERIC_TYPES = frozenset(
    {
        "coordinator_wall_seconds",
        "output_bytes",
        "effective_output_bytes_per_second",
    }
)
_METADATA_KEYS: dict[str, frozenset[str]] = {
    "contract_floor": frozenset({"contract_version", "method_version"}),
    "sampled_agreement": frozenset({"pair_id", "method_version"}),
}
_SETTLEMENT_CAUSES = frozenset(
    {"settled_output", "settled_worker_error", "settled_empty_output"}
)
_WORKER_TERMINAL_CAUSES = frozenset({"lease_expired", "node_stale"})
_TERMINAL_STATES = frozenset(
    {"settled", "expired", "reclaimed", "cancelled", "superseded", "interrupted"}
)
_SHADOW_RATIONALES = frozenset(
    {
        "single_candidate",
        "insufficient_deadline_evidence",
        "insufficient_contract_evidence",
        "insufficient_latency_evidence",
        "insufficient_throughput_evidence",
        "ambiguous_best",
        "tie_retained_actual",
        "evidence_preferred_actual",
        "evidence_preferred_alternative",
    }
)


class EvidenceConflict(RuntimeError):
    """A deterministic event ID was reused for different immutable content."""


class ShadowDecisionConflict(RuntimeError):
    """A deterministic shadow-decision ID was reused for different content."""


class ShadowOperationalEventConflict(RuntimeError):
    """One attempt/phase operational identity was reused for another outcome."""


@dataclass(frozen=True)
class EvidenceScope:
    """The exact aggregation boundary; no field may be inferred across scopes."""

    enrollment_id: str
    descriptor_version: str
    descriptor_hash: str
    executor_kind: str
    executor_version: str | None
    worker_protocol_version: str
    model_provider: str
    model_name: str
    model_digest: str | None
    model_variant: str | None
    task_class: TaskClass
    evidence_role: EvidenceRole

    @property
    def scope_key(self) -> str:
        payload = json.dumps(
            {
                "descriptor_hash": self.descriptor_hash,
                "descriptor_version": self.descriptor_version,
                "enrollment_id": self.enrollment_id,
                "evidence_role": self.evidence_role,
                "executor_kind": self.executor_kind,
                "executor_version": self.executor_version,
                "model_digest": self.model_digest,
                "model_name": self.model_name,
                "model_provider": self.model_provider,
                "model_variant": self.model_variant,
                "task_class": self.task_class,
                "worker_protocol_version": self.worker_protocol_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return _domain_digest("mycelium.capability-evidence-scope.v1", payload)


@dataclass(frozen=True)
class FutureActiveExperimentEligibility:
    """Identity prerequisites only; never a promotion or routing decision."""

    eligible_for_future_active_experiment: bool
    blocking_reasons: tuple[FutureActiveExperimentBlockingReason, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "eligible_for_future_active_experiment": (
                self.eligible_for_future_active_experiment
            ),
            "blocking_reasons": list(self.blocking_reasons),
            "meaning": (
                "identity_prerequisites_only_not_correctness_reputation_trust_"
                "or_active_routing"
            ),
        }


def future_active_experiment_eligibility(
    scope: EvidenceScope,
    *,
    descriptor_identity_reconstructable: bool = True,
    model_identity_reconstructable: bool = True,
) -> FutureActiveExperimentEligibility:
    """Diagnose immutable identity needed by a separately approved experiment.

    This presentation-only diagnostic is intentionally absent from hard
    matching, shadow candidate selection, evidence aggregation, and shadow
    preference. A positive result is necessary but not sufficient for any
    future active experiment.
    """

    blockers: set[FutureActiveExperimentBlockingReason] = set()
    legacy_descriptor = scope.descriptor_hash == LEGACY_DESCRIPTOR_HASH
    if legacy_descriptor:
        blockers.add("legacy_descriptor_identity")
    elif (
        not descriptor_identity_reconstructable
        or scope.descriptor_version != "1"
        or len(scope.descriptor_hash) != 64
        or any(
            character not in "0123456789abcdef"
            for character in scope.descriptor_hash
        )
    ):
        blockers.add("descriptor_identity_unreconstructable")

    if scope.model_digest is None:
        blockers.add("immutable_model_identity_missing")
    else:
        try:
            canonical_digest = normalize_model_digest(scope.model_digest)
            _bounded_text(
                scope.model_provider,
                field_name="model_provider",
                maximum=64,
            )
            _bounded_text(scope.model_name, field_name="model_name", maximum=128)
        except ValueError:
            blockers.add("model_identity_unreconstructable")
        else:
            if (
                not model_identity_reconstructable
                or canonical_digest != scope.model_digest
            ):
                blockers.add("model_identity_unreconstructable")

    ordered = tuple(
        reason
        for reason in FUTURE_ACTIVE_EXPERIMENT_BLOCKING_REASON_ORDER
        if reason in blockers
    )
    return FutureActiveExperimentEligibility(
        eligible_for_future_active_experiment=not ordered,
        blocking_reasons=ordered,
    )


@dataclass(frozen=True)
class ResolvedEvidenceContext:
    scope: EvidenceScope
    attempt_id: str
    execution_id: str
    execution_unit_id: str
    node_id: str


@dataclass(frozen=True)
class ScopeResolution:
    context: ResolvedEvidenceContext | None
    excluded_reason_code: str | None = None

    @property
    def usable(self) -> bool:
        return self.context is not None


@dataclass(frozen=True)
class ObservationRecord:
    observation_id: str
    attempt_id: str
    execution_id: str
    execution_unit_id: str
    node_id: str
    scope: EvidenceScope
    observation_type: ObservationType
    subject_key: str
    outcome: str
    numeric_value: float | None
    metadata: Mapping[str, str]
    observed_at: float
    recorded_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ObservationRecord:
        metadata = json.loads(str(row["metadata_json"]))
        if not isinstance(metadata, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        ):
            raise RuntimeError("stored capability evidence metadata is invalid")
        observation_type = str(row["observation_type"])
        if observation_type not in _OBSERVATION_TYPES:
            raise RuntimeError("stored capability evidence type is invalid")
        return cls(
            observation_id=str(row["observation_id"]),
            attempt_id=str(row["attempt_id"]),
            execution_id=str(row["execution_id"]),
            execution_unit_id=str(row["execution_unit_id"]),
            node_id=str(row["node_id"]),
            scope=_scope_from_row(row),
            observation_type=observation_type,  # type: ignore[arg-type]
            subject_key=str(row["subject_key"]),
            outcome=str(row["outcome"]),
            numeric_value=(
                float(row["numeric_value"])
                if row["numeric_value"] is not None
                else None
            ),
            metadata=metadata,
            observed_at=float(row["observed_at"]),
            recorded_at=float(row["recorded_at"]),
        )


@dataclass(frozen=True)
class EvidenceWriteResult:
    observations: tuple[ObservationRecord, ...]
    excluded_reason_code: str | None = None

    @property
    def recorded(self) -> bool:
        return bool(self.observations)


@dataclass(frozen=True)
class ContractFloorProjectionResult:
    """Atomic post-terminal assurance projection and durable completion receipt."""

    source_digest: str
    observations: tuple[ObservationRecord, ...]
    excluded_reason_codes: tuple[str, ...]
    replayed: bool


_T = TypeVar("_T")


@dataclass(frozen=True)
class BestEffortResult(Generic[_T]):
    succeeded: bool
    value: _T | None
    error_code: str | None = None


@dataclass(frozen=True)
class BinaryAggregate:
    sample_count: int
    positive_count: int
    negative_count: int
    rate: float | None
    wilson_low: float | None
    wilson_high: float | None


@dataclass(frozen=True)
class ScopeAggregate:
    scope: EvidenceScope
    observation_count: int
    settlement_count: int
    settled_output_count: int
    settled_worker_error_count: int
    settled_empty_output_count: int
    deadline_completion: BinaryAggregate
    contract_floor: BinaryAggregate
    sampled_agreement: BinaryAggregate
    lease_expiration_count: int
    worker_disconnect_count: int
    latency_sample_count: int
    recent_median_latency_seconds: float | None
    throughput_sample_count: int
    recent_median_output_bytes_per_second: float | None
    minimum_samples: int
    insufficient_evidence: bool


@dataclass(frozen=True)
class ScopeAggregateSummary:
    """Privacy-bounded operator projection; never contains individual observations."""

    node_id: str
    scope: EvidenceScope
    aggregate: ScopeAggregate
    last_observed_at: float


@dataclass(frozen=True)
class EligibleShadowCandidate:
    """Evidence for a candidate already accepted by the hard matcher."""

    candidate_id: str
    aggregate: ScopeAggregate
    hard_eligible: Literal[True] = True

    def __post_init__(self) -> None:
        _bounded_text(self.candidate_id, field_name="candidate_id")
        if self.hard_eligible is not True:
            raise ValueError("shadow candidates must already be hard eligible")


@dataclass(frozen=True)
class ShadowEvaluation:
    decision_id: str
    actual_attempt_id: str
    policy_version: str
    decision_at: float
    actual_candidate_id: str
    actual_scope_key: str
    preferred_candidate_id: str | None
    outcome: ShadowOutcome
    rationale_code: str
    candidate_count: int
    candidate_set_digest: str


@dataclass(frozen=True)
class ShadowDecisionRecord:
    evaluation: ShadowEvaluation
    recorded_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ShadowDecisionRecord:
        return cls(
            evaluation=ShadowEvaluation(
                decision_id=str(row["decision_id"]),
                actual_attempt_id=str(row["actual_attempt_id"]),
                policy_version=str(row["policy_version"]),
                decision_at=float(row["decision_at"]),
                actual_candidate_id=str(row["actual_candidate_id"]),
                actual_scope_key=str(row["actual_scope_key"]),
                preferred_candidate_id=(
                    str(row["preferred_candidate_id"])
                    if row["preferred_candidate_id"] is not None
                    else None
                ),
                outcome=str(row["outcome"]),  # type: ignore[arg-type]
                rationale_code=str(row["rationale_code"]),
                candidate_count=int(row["candidate_count"]),
                candidate_set_digest=str(row["candidate_set_digest"]),
            ),
            recorded_at=float(row["recorded_at"]),
        )


@dataclass(frozen=True)
class ShadowDecisionAggregate:
    """Bounded operator counts grouped by the actual candidate and exact scope."""

    actual_candidate_id: str
    actual_scope_key: str
    decision_count: int
    same_count: int
    different_count: int
    no_preference_count: int
    last_decision_at: float


@dataclass(frozen=True)
class ShadowOperationalRecord:
    """Content-free operational health for one shadow-pipeline phase."""

    event_id: str
    attempt_id: str
    phase: ShadowOperationalPhase
    outcome: ShadowOperationalOutcome
    reason_code: str
    occurred_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ShadowOperationalRecord:
        event_id = str(row["event_id"])
        attempt_id = _bounded_text(row["attempt_id"], field_name="attempt_id")
        phase = str(row["phase"])
        outcome = str(row["outcome"])
        reason_code = str(row["reason_code"])
        _validate_shadow_operational_classification(
            phase=phase,
            outcome=outcome,
            reason_code=reason_code,
        )
        expected_event_id = _shadow_operational_event_id(
            attempt_id=attempt_id,
            phase=phase,  # type: ignore[arg-type]
        )
        if event_id != expected_event_id:
            raise RuntimeError("stored shadow operational event ID is not canonical")
        return cls(
            event_id=event_id,
            attempt_id=attempt_id,
            phase=phase,  # type: ignore[arg-type]
            outcome=outcome,  # type: ignore[arg-type]
            reason_code=reason_code,
            occurred_at=_timestamp(row["occurred_at"], field_name="occurred_at"),
        )


@dataclass(frozen=True)
class ShadowOperationalReport:
    """Fixed-shape health counts for an assignment-time admission cohort."""

    admission_counts: dict[ShadowAdmissionOutcome, int]
    evaluation_counts: dict[ShadowEvaluationOutcome, int]
    orphan_evaluation_total: int
    assignment_observation_total: int
    offered_total: int
    scheduled_total: int
    completed_total: int
    skipped_total: int
    failed_total: int
    pending_total: int
    drop_failure_numerator: int
    drop_failure_denominator: int
    drop_failure_rate: float | None
    latest_event_at: float | None
    window_started_at: float | None
    window_ended_at: float | None


@dataclass(frozen=True)
class ShadowOperationalProcessSnapshot:
    """Process-lifetime fallback counters that a failed durable store cannot own."""

    reset_at: float
    durable_health_record_write_failure: int
    unexpected_containment_failure: int
    background_task_callback_failure: int


def _domain_digest(domain: str, payload: str) -> str:
    return hashlib.sha256(
        domain.encode("ascii") + b"\0" + payload.encode("utf-8")
    ).hexdigest()


def _bounded_text(
    value: object,
    *,
    field_name: str,
    maximum: int = MAX_IDENTIFIER_LENGTH,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} must be 1-{maximum} characters without outer whitespace")
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} must contain printable non-whitespace characters")
    return value


def _optional_bounded_text(
    value: object,
    *,
    field_name: str,
    maximum: int,
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


def _validate_shadow_operational_classification(
    *, phase: str, outcome: str, reason_code: object
) -> str:
    if phase not in {"admission", "evaluation"}:
        raise ValueError("shadow operational phase must be admission or evaluation")
    allowed_reasons = _SHADOW_OPERATION_REASON_CODES.get(
        (phase, outcome)  # type: ignore[arg-type]
    )
    if allowed_reasons is None:
        raise ValueError("shadow operational outcome is invalid for its phase")
    parsed_reason = _bounded_text(
        reason_code,
        field_name="reason_code",
        maximum=64,
    )
    if parsed_reason not in allowed_reasons:
        raise ValueError("shadow operational reason code is invalid for its outcome")
    return parsed_reason


def _shadow_operational_event_id(
    *, attempt_id: str, phase: ShadowOperationalPhase
) -> str:
    payload = json.dumps(
        {"attempt_id": attempt_id, "phase": phase},
        sort_keys=True,
        separators=(",", ":"),
    )
    return _domain_digest("mycelium.capability-shadow-operation.v1", payload)


class ShadowOperationalProcessCounters:
    """Thread-safe non-durable counters for failures the health store cannot log."""

    def __init__(self, *, reset_at: float | None = None):
        self._lock = threading.RLock()
        self._reset_at = _timestamp(
            time.time() if reset_at is None else reset_at,
            field_name="reset_at",
        )
        self._counts: dict[ShadowOperationalCounterName, int] = {
            name: 0 for name in SHADOW_OPERATION_COUNTER_NAMES
        }

    def increment(self, name: ShadowOperationalCounterName) -> int:
        if name not in SHADOW_OPERATION_COUNTER_NAMES:
            raise ValueError("unsupported shadow operational process counter")
        with self._lock:
            self._counts[name] += 1
            return self._counts[name]

    def reset(self, *, reset_at: float | None = None) -> None:
        parsed = _timestamp(
            time.time() if reset_at is None else reset_at,
            field_name="reset_at",
        )
        with self._lock:
            self._reset_at = parsed
            for name in SHADOW_OPERATION_COUNTER_NAMES:
                self._counts[name] = 0

    def snapshot(self) -> ShadowOperationalProcessSnapshot:
        with self._lock:
            return ShadowOperationalProcessSnapshot(
                reset_at=self._reset_at,
                durable_health_record_write_failure=self._counts[
                    "durable_health_record_write_failure"
                ],
                unexpected_containment_failure=self._counts[
                    "unexpected_containment_failure"
                ],
                background_task_callback_failure=self._counts[
                    "background_task_callback_failure"
                ],
            )


def _attempt_value(attempt: object, name: str) -> object:
    if isinstance(attempt, Mapping):
        return attempt.get(name)
    return getattr(attempt, name, None)


def _attempt_text(
    attempt: object,
    name: str,
    *,
    maximum: int = MAX_IDENTIFIER_LENGTH,
) -> str:
    return _bounded_text(
        _attempt_value(attempt, name), field_name=name, maximum=maximum
    )


def _canonical_metadata(
    observation_type: str, metadata: Mapping[str, object] | None
) -> str:
    supplied = dict(metadata or {})
    allowed = _METADATA_KEYS.get(observation_type, frozenset())
    if set(supplied) - allowed:
        raise ValueError(
            f"metadata keys are not allowed for observation type {observation_type}"
        )
    canonical: dict[str, str] = {}
    for key, value in supplied.items():
        maximum = 64
        parsed = _bounded_text(value, field_name=f"metadata.{key}", maximum=maximum)
        if key == "pair_id" and (
            len(parsed) != 64
            or any(character not in "0123456789abcdef" for character in parsed)
        ):
            raise ValueError("metadata.pair_id must be a lowercase SHA-256 digest")
        canonical[key] = parsed
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_METADATA_JSON_BYTES:
        raise ValueError("capability evidence metadata exceeds its storage limit")
    return encoded


def _scope_from_row(row: sqlite3.Row) -> EvidenceScope:
    return EvidenceScope(
        enrollment_id=str(row["enrollment_id"]),
        descriptor_version=str(row["descriptor_version"]),
        descriptor_hash=str(row["descriptor_hash"]),
        executor_kind=str(row["executor_kind"]),
        executor_version=(
            str(row["executor_version"])
            if row["executor_version"] is not None
            else None
        ),
        worker_protocol_version=str(row["worker_protocol_version"]),
        model_provider=str(row["model_provider"]),
        model_name=str(row["model_name"]),
        model_digest=(
            str(row["model_digest"]) if row["model_digest"] is not None else None
        ),
        model_variant=(
            str(row["model_variant"]) if row["model_variant"] is not None else None
        ),
        task_class=str(row["task_class"]),  # type: ignore[arg-type]
        evidence_role=str(row["evidence_role"]),  # type: ignore[arg-type]
    )


def _observation_id(
    *, attempt_id: str, observation_type: str, subject_key: str
) -> str:
    payload = json.dumps(
        {
            "attempt_id": attempt_id,
            "observation_type": observation_type,
            "subject_key": subject_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _domain_digest("mycelium.node-capability-observation.v1", payload)


def _comparison_pair_id(primary_attempt_id: str, sampled_attempt_id: str) -> str:
    payload = json.dumps(
        sorted((primary_attempt_id, sampled_attempt_id)), separators=(",", ":")
    )
    return _domain_digest("mycelium.sampled-comparison-pair.v1", payload)


def _contract_floor_projection_digest(
    *,
    execution_id: str,
    method_version: str,
    projections: Sequence[tuple[str, bool]],
) -> str:
    payload = json.dumps(
        {
            "execution_id": execution_id,
            "method_version": method_version,
            "projections": [
                {"attempt_id": attempt_id, "passed": passed}
                for attempt_id, passed in projections
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _domain_digest("mycelium.contract-floor-projection.v1", payload)


def ensure_capability_evidence_schema(con: sqlite3.Connection) -> None:
    """Install the additive append-only evidence and shadow-decision schema."""

    ensure_node_capability_snapshot_schema(con)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS node_capability_observations (
            observation_id       TEXT PRIMARY KEY CHECK(length(observation_id) = 64),
            schema_version       TEXT NOT NULL CHECK(schema_version = '1'),
            attempt_id           TEXT NOT NULL CHECK(length(attempt_id) BETWEEN 1 AND 256),
            execution_id         TEXT NOT NULL CHECK(length(execution_id) BETWEEN 1 AND 256),
            execution_unit_id    TEXT NOT NULL CHECK(length(execution_unit_id) BETWEEN 1 AND 256),
            node_id              TEXT NOT NULL CHECK(length(node_id) BETWEEN 1 AND 256),
            enrollment_id        TEXT NOT NULL,
            descriptor_version   TEXT NOT NULL CHECK(length(descriptor_version) BETWEEN 1 AND 16),
            descriptor_hash      TEXT NOT NULL CHECK(length(descriptor_hash) = 64),
            executor_kind        TEXT NOT NULL CHECK(length(executor_kind) BETWEEN 1 AND 64),
            executor_version     TEXT CHECK(executor_version IS NULL OR length(executor_version) BETWEEN 1 AND 64),
            worker_protocol_version TEXT NOT NULL CHECK(length(worker_protocol_version) BETWEEN 1 AND 32),
            model_provider       TEXT NOT NULL CHECK(length(model_provider) BETWEEN 1 AND 64),
            model_name           TEXT NOT NULL CHECK(length(model_name) BETWEEN 1 AND 128),
            model_digest         TEXT CHECK(model_digest IS NULL OR length(model_digest) = 71),
            model_variant        TEXT CHECK(model_variant IS NULL OR length(model_variant) BETWEEN 1 AND 64),
            task_class           TEXT NOT NULL CHECK(task_class IN ('dag_subtask', 'candidate')),
            evidence_role        TEXT NOT NULL CHECK(evidence_role IN ('production', 'sampled_comparison')),
            observation_type     TEXT NOT NULL CHECK(observation_type IN (
                'settlement_outcome', 'deadline_completion',
                'coordinator_wall_seconds', 'output_bytes',
                'effective_output_bytes_per_second', 'terminal_outcome',
                'contract_floor', 'sampled_agreement'
            )),
            subject_key          TEXT NOT NULL CHECK(length(subject_key) BETWEEN 1 AND 128),
            outcome              TEXT NOT NULL CHECK(length(outcome) BETWEEN 1 AND 64),
            numeric_value        REAL,
            metadata_json        TEXT NOT NULL CHECK(length(metadata_json) <= 1024),
            observed_at          REAL NOT NULL,
            recorded_at          REAL NOT NULL,
            FOREIGN KEY (enrollment_id, descriptor_hash)
                REFERENCES node_capability_snapshots(enrollment_id, descriptor_hash)
                ON DELETE RESTRICT
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_capability_observations_scope "
        "ON node_capability_observations("
        "enrollment_id, descriptor_hash, executor_kind, executor_version, "
        "worker_protocol_version, model_provider, model_name, model_digest, "
        "model_variant, task_class, evidence_role, observation_type, observed_at)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_capability_observations_attempt "
        "ON node_capability_observations(attempt_id, observation_type)"
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_capability_observations_no_update
        BEFORE UPDATE ON node_capability_observations
        BEGIN
            SELECT RAISE(ABORT, 'node capability observations are append-only');
        END
        """
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_capability_observations_no_delete
        BEFORE DELETE ON node_capability_observations
        BEGIN
            SELECT RAISE(ABORT, 'node capability observations are append-only');
        END
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS capability_evidence_projection_receipts (
            execution_id       TEXT NOT NULL CHECK(length(execution_id) BETWEEN 1 AND 256),
            projection_version TEXT NOT NULL CHECK(length(projection_version) BETWEEN 1 AND 64),
            source_digest      TEXT NOT NULL CHECK(length(source_digest) = 64),
            candidate_count    INTEGER NOT NULL CHECK(candidate_count BETWEEN 0 AND 16),
            observation_count  INTEGER NOT NULL CHECK(observation_count BETWEEN 0 AND 16),
            excluded_count     INTEGER NOT NULL CHECK(excluded_count BETWEEN 0 AND 16),
            projected_at       REAL NOT NULL,
            PRIMARY KEY (execution_id, projection_version)
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_capability_evidence_projection_time "
        "ON capability_evidence_projection_receipts(projected_at, execution_id)"
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_capability_evidence_projections_no_update
        BEFORE UPDATE ON capability_evidence_projection_receipts
        BEGIN
            SELECT RAISE(ABORT, 'capability evidence projection receipts are append-only');
        END
        """
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_capability_evidence_projections_no_delete
        BEFORE DELETE ON capability_evidence_projection_receipts
        BEGIN
            SELECT RAISE(ABORT, 'capability evidence projection receipts are append-only');
        END
        """
    )
    ensure_capability_shadow_decision_schema(con)


def ensure_capability_shadow_decision_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS capability_shadow_decisions (
            decision_id            TEXT PRIMARY KEY CHECK(length(decision_id) = 64),
            schema_version         TEXT NOT NULL CHECK(schema_version = '1'),
            actual_attempt_id      TEXT NOT NULL CHECK(length(actual_attempt_id) BETWEEN 1 AND 256),
            policy_version         TEXT NOT NULL CHECK(length(policy_version) BETWEEN 1 AND 64),
            decision_at            REAL NOT NULL,
            actual_candidate_id    TEXT NOT NULL CHECK(length(actual_candidate_id) BETWEEN 1 AND 256),
            actual_scope_key       TEXT NOT NULL CHECK(length(actual_scope_key) = 64),
            preferred_candidate_id TEXT CHECK(
                preferred_candidate_id IS NULL OR length(preferred_candidate_id) BETWEEN 1 AND 256
            ),
            outcome                TEXT NOT NULL CHECK(outcome IN ('same', 'different', 'no_preference')),
            rationale_code         TEXT NOT NULL CHECK(length(rationale_code) BETWEEN 1 AND 64),
            candidate_count        INTEGER NOT NULL CHECK(candidate_count BETWEEN 1 AND 64),
            candidate_set_digest   TEXT NOT NULL CHECK(length(candidate_set_digest) = 64),
            recorded_at            REAL NOT NULL
        )
        """
    )
    columns = {
        str(row[1])
        for row in con.execute(
            "PRAGMA table_info(capability_shadow_decisions)"
        ).fetchall()
    }
    if "actual_scope_key" not in columns:
        con.execute(
            "ALTER TABLE capability_shadow_decisions ADD COLUMN actual_scope_key TEXT"
        )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_capability_shadow_decisions_time "
        "ON capability_shadow_decisions(decision_at, decision_id)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_capability_shadow_decisions_actual_scope "
        "ON capability_shadow_decisions(actual_candidate_id, actual_scope_key, decision_at)"
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_capability_shadow_decisions_no_update
        BEFORE UPDATE ON capability_shadow_decisions
        BEGIN
            SELECT RAISE(ABORT, 'capability shadow decisions are append-only');
        END
        """
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_capability_shadow_decisions_no_delete
        BEFORE DELETE ON capability_shadow_decisions
        BEGIN
            SELECT RAISE(ABORT, 'capability shadow decisions are append-only');
        END
        """
    )


def ensure_capability_shadow_operational_schema(con: sqlite3.Connection) -> None:
    """Install the bounded append-only shadow-pipeline health schema."""

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS capability_shadow_operational_events (
            event_id     TEXT PRIMARY KEY CHECK(
                length(event_id) = 64
                AND event_id NOT GLOB '*[^0-9a-f]*'
            ),
            attempt_id   TEXT NOT NULL CHECK(length(attempt_id) BETWEEN 1 AND 256),
            phase        TEXT NOT NULL CHECK(phase IN ('admission', 'evaluation')),
            outcome      TEXT NOT NULL CHECK(
                (phase = 'admission' AND outcome IN (
                    'disabled', 'not_applicable', 'queue_saturated',
                    'scope_capture_failed', 'scheduled'
                ))
                OR
                (phase = 'evaluation' AND outcome IN (
                    'completed', 'evaluator_failed', 'decision_write_failed',
                    'cancelled_on_shutdown'
                ))
            ),
            reason_code  TEXT NOT NULL CHECK(
                reason_code IN (
                    'mode_disabled', 'legacy_descriptor_identity',
                    'nonproduction_attempt', 'unsupported_task_class',
                    'background_queue_limit_reached', 'scope_capture_failed',
                    'coordinator_shutdown_during_scope_capture',
                    'evaluation_scheduled', 'decision_persisted',
                    'evaluator_failed', 'decision_write_failed',
                    'coordinator_shutdown'
                )
            ),
            occurred_at  REAL NOT NULL CHECK(occurred_at >= 0),
            CHECK(
                (outcome = 'disabled' AND reason_code = 'mode_disabled')
                OR (outcome = 'not_applicable' AND reason_code IN (
                    'legacy_descriptor_identity', 'nonproduction_attempt',
                    'unsupported_task_class'
                ))
                OR (outcome = 'queue_saturated'
                    AND reason_code = 'background_queue_limit_reached')
                OR (outcome = 'scope_capture_failed'
                    AND reason_code IN (
                        'scope_capture_failed',
                        'coordinator_shutdown_during_scope_capture'
                    ))
                OR (outcome = 'scheduled'
                    AND reason_code = 'evaluation_scheduled')
                OR (outcome = 'completed'
                    AND reason_code = 'decision_persisted')
                OR (outcome = 'evaluator_failed'
                    AND reason_code = 'evaluator_failed')
                OR (outcome = 'decision_write_failed'
                    AND reason_code = 'decision_write_failed')
                OR (outcome = 'cancelled_on_shutdown'
                    AND reason_code = 'coordinator_shutdown')
            ),
            UNIQUE(attempt_id, phase)
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_capability_shadow_operations_phase_time "
        "ON capability_shadow_operational_events(phase, occurred_at, attempt_id)"
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_capability_shadow_operations_no_update
        BEFORE UPDATE ON capability_shadow_operational_events
        BEGIN
            SELECT RAISE(ABORT, 'capability shadow operational events are append-only');
        END
        """
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_capability_shadow_operations_no_delete
        BEFORE DELETE ON capability_shadow_operational_events
        BEGIN
            SELECT RAISE(ABORT, 'capability shadow operational events are append-only');
        END
        """
    )


class CapabilityEvidenceStore:
    """Append-only durable observations and exact-scope aggregation."""

    def __init__(self, path: str | Path = "events.db"):
        self.path = Path(path)
        self._lock = threading.RLock()

    def migrate(self) -> None:
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            ensure_capability_evidence_schema(con)
            con.commit()

    @staticmethod
    def best_effort(
        operation: Callable[..., _T], *args: object, **kwargs: object
    ) -> BestEffortResult[_T]:
        """Contain evidence work that owns its transaction."""

        try:
            return BestEffortResult(succeeded=True, value=operation(*args, **kwargs))
        except Exception:
            return BestEffortResult(
                succeeded=False, value=None, error_code="evidence_write_failed"
            )

    @staticmethod
    def best_effort_in_transaction(
        con: sqlite3.Connection,
        operation: Callable[..., _T],
        *args: object,
        **kwargs: object,
    ) -> BestEffortResult[_T]:
        """Contain optional evidence work without poisoning its caller's transaction."""

        con.execute("SAVEPOINT capability_evidence_best_effort")
        try:
            value = operation(*args, **kwargs)
        except Exception:
            con.execute("ROLLBACK TO capability_evidence_best_effort")
            con.execute("RELEASE capability_evidence_best_effort")
            return BestEffortResult(
                succeeded=False, value=None, error_code="evidence_write_failed"
            )
        con.execute("RELEASE capability_evidence_best_effort")
        return BestEffortResult(succeeded=True, value=value)

    @staticmethod
    def resolve_scope_in_transaction(
        con: sqlite3.Connection, attempt: object
    ) -> ScopeResolution:
        """Resolve an attempt only through its validated immutable snapshot."""

        try:
            attempt_id = _attempt_text(attempt, "attempt_id")
            execution_id = _attempt_text(attempt, "execution_id")
            execution_unit_id = _attempt_text(attempt, "execution_unit_id")
            unit_kind = _attempt_text(attempt, "execution_unit_kind", maximum=32)
            if unit_kind not in {"dag_subtask", "candidate"}:
                return ScopeResolution(None, "unsupported_task_class")
            role = _attempt_text(attempt, "evidence_role", maximum=32)
            if role not in {"production", "sampled_comparison"}:
                return ScopeResolution(None, "evidence_role_missing_or_invalid")
            enrollment_id = _attempt_text(
                attempt, "assigned_enrollment_id", maximum=64
            )
            descriptor_version = _attempt_text(
                attempt, "assigned_descriptor_version", maximum=16
            )
            descriptor_hash = _attempt_text(
                attempt, "assigned_descriptor_hash", maximum=64
            )
            if len(descriptor_hash) != 64 or any(
                character not in "0123456789abcdef"
                for character in descriptor_hash
            ):
                return ScopeResolution(None, "descriptor_binding_invalid")
            node_id = _attempt_text(attempt, "assigned_node_id")
            model_provider = _attempt_text(
                attempt, "assigned_model_provider", maximum=64
            )
            model_name = _attempt_text(attempt, "assigned_model_name", maximum=128)
            supplied_digest = _attempt_value(attempt, "assigned_model_digest")
            if supplied_digest is not None and not isinstance(supplied_digest, str):
                return ScopeResolution(None, "selected_model_binding_mismatch")
            model_digest = (
                normalize_model_digest(supplied_digest)
                if isinstance(supplied_digest, str)
                else None
            )
        except ValueError:
            return ScopeResolution(None, "attempt_binding_incomplete")

        row = con.execute(
            """
            SELECT snapshots.*, enrollments.node_id AS enrolled_node_id
            FROM node_capability_snapshots AS snapshots
            JOIN node_enrollments AS enrollments
              ON enrollments.enrollment_id = snapshots.enrollment_id
            WHERE snapshots.enrollment_id = ? AND snapshots.descriptor_hash = ?
            """,
            (enrollment_id, descriptor_hash),
        ).fetchone()
        if row is None:
            return ScopeResolution(None, "immutable_snapshot_missing")
        snapshot = NodeCapabilitySnapshotRecord.from_row(row)
        if snapshot.descriptor_version != descriptor_version:
            return ScopeResolution(None, "descriptor_binding_mismatch")
        if str(row["enrolled_node_id"]) != node_id:
            return ScopeResolution(None, "enrollment_node_mismatch")

        selected = [
            model
            for model in snapshot.descriptor.models
            if model.provider == model_provider
            and model.name == model_name
            and model.digest == model_digest
        ]
        if len(selected) != 1:
            return ScopeResolution(None, "selected_model_binding_mismatch")
        model = selected[0]
        executor = snapshot.descriptor.executor
        scope = EvidenceScope(
            enrollment_id=enrollment_id,
            descriptor_version=descriptor_version,
            descriptor_hash=descriptor_hash,
            executor_kind=executor.kind,
            executor_version=executor.version,
            worker_protocol_version=executor.worker_protocol_version,
            model_provider=model.provider,
            model_name=model.name,
            model_digest=model.digest,
            model_variant=model.variant,
            task_class=unit_kind,  # type: ignore[arg-type]
            evidence_role=role,  # type: ignore[arg-type]
        )
        return ScopeResolution(
            ResolvedEvidenceContext(
                scope=scope,
                attempt_id=attempt_id,
                execution_id=execution_id,
                execution_unit_id=execution_unit_id,
                node_id=node_id,
            )
        )

    def resolve_scope(self, attempt: object) -> ScopeResolution:
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            return self.resolve_scope_in_transaction(con, attempt)

    @staticmethod
    def _append_one(
        con: sqlite3.Connection,
        context: ResolvedEvidenceContext,
        *,
        observation_type: ObservationType,
        subject_key: str,
        outcome: str,
        numeric_value: float | None,
        metadata: Mapping[str, object] | None,
        observed_at: float,
        recorded_at: float,
    ) -> ObservationRecord:
        if observation_type not in _OBSERVATION_TYPES:
            raise ValueError("unsupported capability evidence observation type")
        if outcome not in _OBSERVATION_OUTCOMES[observation_type]:
            raise ValueError("outcome is invalid for the observation type")
        subject_key = _bounded_text(
            subject_key, field_name="subject_key", maximum=128
        )
        observed_at = _timestamp(observed_at, field_name="observed_at")
        recorded_at = _timestamp(recorded_at, field_name="recorded_at")
        if recorded_at < observed_at:
            raise ValueError("recorded_at cannot precede observed_at")
        if observation_type in _NUMERIC_TYPES:
            if isinstance(numeric_value, bool) or not isinstance(
                numeric_value, (int, float)
            ):
                raise ValueError("numeric evidence requires a finite numeric value")
            numeric_value = float(numeric_value)
            if not math.isfinite(numeric_value) or numeric_value < 0:
                raise ValueError("numeric evidence must be finite and non-negative")
        elif numeric_value is not None:
            raise ValueError("non-numeric evidence cannot have a numeric value")
        if observation_type == "output_bytes" and (
            numeric_value is None or numeric_value > MAX_OBSERVED_OUTPUT_BYTES
        ):
            raise ValueError("observed output bytes exceed the protocol limit")
        if observation_type == "coordinator_wall_seconds" and (
            numeric_value is None or numeric_value > MAX_COORDINATOR_WALL_SECONDS
        ):
            raise ValueError("coordinator wall duration exceeds the evidence limit")
        metadata_json = _canonical_metadata(observation_type, metadata)
        observation_id = _observation_id(
            attempt_id=context.attempt_id,
            observation_type=observation_type,
            subject_key=subject_key,
        )
        scope = context.scope
        values = (
            observation_id,
            EVIDENCE_SCHEMA_VERSION,
            context.attempt_id,
            context.execution_id,
            context.execution_unit_id,
            context.node_id,
            scope.enrollment_id,
            scope.descriptor_version,
            scope.descriptor_hash,
            scope.executor_kind,
            scope.executor_version,
            scope.worker_protocol_version,
            scope.model_provider,
            scope.model_name,
            scope.model_digest,
            scope.model_variant,
            scope.task_class,
            scope.evidence_role,
            observation_type,
            subject_key,
            outcome,
            numeric_value,
            metadata_json,
            observed_at,
            recorded_at,
        )
        con.execute(
            """
            INSERT OR IGNORE INTO node_capability_observations (
                observation_id, schema_version, attempt_id, execution_id,
                execution_unit_id, node_id, enrollment_id, descriptor_version,
                descriptor_hash, executor_kind, executor_version,
                worker_protocol_version, model_provider, model_name, model_digest,
                model_variant, task_class, evidence_role, observation_type,
                subject_key, outcome, numeric_value, metadata_json, observed_at,
                recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        row = con.execute(
            "SELECT * FROM node_capability_observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - SQLite insert contract
            raise RuntimeError("capability observation disappeared after insertion")
        record = ObservationRecord.from_row(row)
        expected = (
            context.attempt_id,
            context.execution_id,
            context.execution_unit_id,
            context.node_id,
            scope,
            observation_type,
            subject_key,
            outcome,
            numeric_value,
            json.loads(metadata_json),
            observed_at,
        )
        actual = (
            record.attempt_id,
            record.execution_id,
            record.execution_unit_id,
            record.node_id,
            record.scope,
            record.observation_type,
            record.subject_key,
            record.outcome,
            record.numeric_value,
            dict(record.metadata),
            record.observed_at,
        )
        if actual != expected:
            raise EvidenceConflict(
                "capability observation ID conflicts with immutable stored content"
            )
        return record

    @classmethod
    def record_settlement_in_transaction(
        cls,
        con: sqlite3.Connection,
        attempt: object,
        *,
        accepted_at: float,
        output_bytes: int,
        recorded_at: float | None = None,
    ) -> EvidenceWriteResult:
        ensure_capability_evidence_schema(con)
        resolution = cls.resolve_scope_in_transaction(con, attempt)
        if resolution.context is None:
            return EvidenceWriteResult((), resolution.excluded_reason_code)
        state = _attempt_value(attempt, "state")
        cause = _attempt_value(attempt, "terminal_cause")
        if state != "settled" or cause not in _SETTLEMENT_CAUSES:
            return EvidenceWriteResult((), "not_an_accepted_settlement")
        accepted = _timestamp(accepted_at, field_name="accepted_at")
        issued = _timestamp(_attempt_value(attempt, "issued_at"), field_name="issued_at")
        deadline = _timestamp(
            _attempt_value(attempt, "lease_expires_at"),
            field_name="lease_expires_at",
        )
        if accepted < issued or deadline <= issued:
            raise ValueError("settlement timestamps are inconsistent")
        settled_at = _attempt_value(attempt, "settled_at")
        if settled_at is None or _timestamp(
            settled_at, field_name="settled_at"
        ) != accepted:
            return EvidenceWriteResult((), "settlement_timestamp_binding_mismatch")
        if isinstance(output_bytes, bool) or not isinstance(output_bytes, int):
            raise ValueError("output_bytes must be an integer")
        if output_bytes < 0 or output_bytes > MAX_OBSERVED_OUTPUT_BYTES:
            raise ValueError("output_bytes is outside the evidence limit")
        written_at = time.time() if recorded_at is None else recorded_at
        wall_seconds = accepted - issued
        completed_before_deadline = (
            cause == "settled_output" and accepted <= deadline
        )
        context = resolution.context
        observations = [
            cls._append_one(
                con,
                context,
                observation_type="settlement_outcome",
                subject_key="lifecycle",
                outcome=str(cause),
                numeric_value=None,
                metadata=None,
                observed_at=accepted,
                recorded_at=written_at,
            ),
            cls._append_one(
                con,
                context,
                observation_type="deadline_completion",
                subject_key=DEADLINE_COMPLETION_SUBJECT,
                outcome="pass" if completed_before_deadline else "fail",
                numeric_value=None,
                metadata=None,
                observed_at=accepted,
                recorded_at=written_at,
            ),
            cls._append_one(
                con,
                context,
                observation_type="coordinator_wall_seconds",
                subject_key="settlement",
                outcome="measured",
                numeric_value=wall_seconds,
                metadata=None,
                observed_at=accepted,
                recorded_at=written_at,
            ),
            cls._append_one(
                con,
                context,
                observation_type="output_bytes",
                subject_key="settlement",
                outcome="measured",
                numeric_value=float(output_bytes),
                metadata=None,
                observed_at=accepted,
                recorded_at=written_at,
            ),
        ]
        if wall_seconds > 0 and output_bytes > 0:
            observations.append(
                cls._append_one(
                    con,
                    context,
                    observation_type="effective_output_bytes_per_second",
                    subject_key="settlement",
                    outcome="measured",
                    numeric_value=output_bytes / wall_seconds,
                    metadata=None,
                    observed_at=accepted,
                    recorded_at=written_at,
                )
            )
        return EvidenceWriteResult(tuple(observations))

    def record_settlement(
        self,
        attempt: object,
        *,
        accepted_at: float,
        output_bytes: int,
        recorded_at: float | None = None,
    ) -> EvidenceWriteResult:
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            con.execute("BEGIN IMMEDIATE")
            result = self.record_settlement_in_transaction(
                con,
                attempt,
                accepted_at=accepted_at,
                output_bytes=output_bytes,
                recorded_at=recorded_at,
            )
            con.commit()
            return result

    @classmethod
    def record_terminal_in_transaction(
        cls,
        con: sqlite3.Connection,
        attempt: object,
        *,
        terminal_at: float,
        recorded_at: float | None = None,
    ) -> EvidenceWriteResult:
        ensure_capability_evidence_schema(con)
        resolution = cls.resolve_scope_in_transaction(con, attempt)
        if resolution.context is None:
            return EvidenceWriteResult((), resolution.excluded_reason_code)
        state = _attempt_value(attempt, "state")
        cause = _attempt_value(attempt, "terminal_cause")
        if state not in _TERMINAL_STATES:
            return EvidenceWriteResult((), "attempt_is_not_terminal")
        if cause not in _WORKER_TERMINAL_CAUSES:
            return EvidenceWriteResult((), "terminal_cause_not_worker_attributable")
        terminal = _timestamp(terminal_at, field_name="terminal_at")
        issued = _timestamp(_attempt_value(attempt, "issued_at"), field_name="issued_at")
        if terminal < issued:
            raise ValueError("terminal timestamp precedes issuance")
        written_at = time.time() if recorded_at is None else recorded_at
        context = resolution.context
        observations = (
            cls._append_one(
                con,
                context,
                observation_type="terminal_outcome",
                subject_key="lifecycle",
                outcome=str(cause),
                numeric_value=None,
                metadata=None,
                observed_at=terminal,
                recorded_at=written_at,
            ),
            cls._append_one(
                con,
                context,
                observation_type="deadline_completion",
                subject_key=DEADLINE_COMPLETION_SUBJECT,
                outcome="fail",
                numeric_value=None,
                metadata=None,
                observed_at=terminal,
                recorded_at=written_at,
            ),
        )
        return EvidenceWriteResult(observations)

    def record_terminal(
        self,
        attempt: object,
        *,
        terminal_at: float,
        recorded_at: float | None = None,
    ) -> EvidenceWriteResult:
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            con.execute("BEGIN IMMEDIATE")
            result = self.record_terminal_in_transaction(
                con, attempt, terminal_at=terminal_at, recorded_at=recorded_at
            )
            con.commit()
            return result

    @classmethod
    def record_contract_floor_in_transaction(
        cls,
        con: sqlite3.Connection,
        attempt: object,
        *,
        passed: bool,
        method_version: str | None = None,
        observed_at: float | None = None,
        recorded_at: float | None = None,
    ) -> EvidenceWriteResult:
        ensure_capability_evidence_schema(con)
        resolution = cls.resolve_scope_in_transaction(con, attempt)
        if resolution.context is None:
            return EvidenceWriteResult((), resolution.excluded_reason_code)
        state = _attempt_value(attempt, "state")
        cause = _attempt_value(attempt, "terminal_cause")
        if state != "settled" or cause not in _SETTLEMENT_CAUSES:
            return EvidenceWriteResult((), "contract_floor_requires_settled_attempt")
        if not isinstance(passed, bool):
            raise ValueError("passed must be a boolean")
        terminal_time = (
            _attempt_value(attempt, "settled_at")
            if observed_at is None
            else observed_at
        )
        observed = _timestamp(terminal_time, field_name="observed_at")
        settled_at = _timestamp(
            _attempt_value(attempt, "settled_at"), field_name="settled_at"
        )
        if observed < settled_at:
            raise ValueError("contract-floor evidence cannot predate settlement")
        metadata: dict[str, object] = {}
        contract_version = _attempt_value(attempt, "contract_version")
        if contract_version is not None:
            metadata["contract_version"] = contract_version
        if method_version is not None:
            metadata["method_version"] = method_version
        record = cls._append_one(
            con,
            resolution.context,
            observation_type="contract_floor",
            subject_key="post_terminal_contract_floor",
            outcome="pass" if passed else "fail",
            numeric_value=None,
            metadata=metadata,
            observed_at=observed,
            recorded_at=time.time() if recorded_at is None else recorded_at,
        )
        return EvidenceWriteResult((record,))

    def record_contract_floor(
        self,
        attempt: object,
        *,
        passed: bool,
        method_version: str | None = None,
        observed_at: float | None = None,
        recorded_at: float | None = None,
    ) -> EvidenceWriteResult:
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            con.execute("BEGIN IMMEDIATE")
            result = self.record_contract_floor_in_transaction(
                con,
                attempt,
                passed=passed,
                method_version=method_version,
                observed_at=observed_at,
                recorded_at=recorded_at,
            )
            con.commit()
            return result

    def record_contract_floor_projection(
        self,
        *,
        execution_id: str,
        projections: Sequence[tuple[object, bool]],
        method_version: str,
        recorded_at: float | None = None,
    ) -> ContractFloorProjectionResult:
        """Atomically project candidate assurance and mark one terminal result done.

        The content-free receipt is an outbox acknowledgement: if this optional
        transaction fails after terminal execution persistence, startup can
        select only executions without a receipt and safely retry the batch.
        """

        execution_id = _bounded_text(execution_id, field_name="execution_id")
        method_version = _bounded_text(
            method_version, field_name="method_version", maximum=64
        )
        if len(projections) > 16:
            raise ValueError("contract-floor projection exceeds 16 candidates")
        normalized: list[tuple[str, bool, object]] = []
        for attempt, passed in projections:
            if not isinstance(passed, bool):
                raise ValueError("contract-floor projection outcomes must be boolean")
            attempt_id = _attempt_text(attempt, "attempt_id")
            normalized.append((attempt_id, passed, attempt))
        normalized.sort(key=lambda item: item[0])
        if len({item[0] for item in normalized}) != len(normalized):
            raise ValueError("contract-floor projection attempt IDs must be unique")
        digest_inputs = [(attempt_id, passed) for attempt_id, passed, _ in normalized]
        source_digest = _contract_floor_projection_digest(
            execution_id=execution_id,
            method_version=method_version,
            projections=digest_inputs,
        )
        written_at = time.time() if recorded_at is None else _timestamp(
            recorded_at, field_name="recorded_at"
        )

        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            ensure_capability_evidence_schema(con)
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT source_digest FROM capability_evidence_projection_receipts "
                "WHERE execution_id = ? AND projection_version = ?",
                (execution_id, CONTRACT_FLOOR_PROJECTION_VERSION),
            ).fetchone()
            if existing is not None:
                if str(existing["source_digest"]) != source_digest:
                    raise EvidenceConflict(
                        "contract-floor projection receipt conflicts with terminal result"
                    )
                con.commit()
                return ContractFloorProjectionResult(
                    source_digest=source_digest,
                    observations=(),
                    excluded_reason_codes=(),
                    replayed=True,
                )

            observations: list[ObservationRecord] = []
            excluded: list[str] = []
            for _attempt_id, passed, attempt in normalized:
                result = self.record_contract_floor_in_transaction(
                    con,
                    attempt,
                    passed=passed,
                    method_version=method_version,
                    recorded_at=written_at,
                )
                observations.extend(result.observations)
                if result.excluded_reason_code is not None:
                    excluded.append(result.excluded_reason_code)
            con.execute(
                """
                INSERT INTO capability_evidence_projection_receipts (
                    execution_id, projection_version, source_digest,
                    candidate_count, observation_count, excluded_count,
                    projected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    CONTRACT_FLOOR_PROJECTION_VERSION,
                    source_digest,
                    len(normalized),
                    len(observations),
                    len(excluded),
                    written_at,
                ),
            )
            con.commit()
            return ContractFloorProjectionResult(
                source_digest=source_digest,
                observations=tuple(observations),
                excluded_reason_codes=tuple(excluded),
                replayed=False,
            )

    @classmethod
    def record_sampled_agreement_in_transaction(
        cls,
        con: sqlite3.Connection,
        primary_attempt: object,
        sampled_attempt: object,
        *,
        agreed: bool,
        method_version: str,
        observed_at: float | None = None,
        recorded_at: float | None = None,
    ) -> EvidenceWriteResult:
        ensure_capability_evidence_schema(con)
        primary = cls.resolve_scope_in_transaction(con, primary_attempt)
        sampled = cls.resolve_scope_in_transaction(con, sampled_attempt)
        if primary.context is None:
            return EvidenceWriteResult((), primary.excluded_reason_code)
        if sampled.context is None:
            return EvidenceWriteResult((), sampled.excluded_reason_code)
        if primary.context.scope.evidence_role != "production":
            return EvidenceWriteResult((), "comparison_primary_role_invalid")
        if sampled.context.scope.evidence_role != "sampled_comparison":
            return EvidenceWriteResult((), "comparison_sample_role_invalid")
        if (
            _attempt_value(primary_attempt, "comparison_primary_attempt_id")
            is not None
            or _attempt_value(sampled_attempt, "comparison_primary_attempt_id")
            != primary.context.attempt_id
        ):
            return EvidenceWriteResult((), "comparison_primary_binding_mismatch")
        if any(
            _attempt_value(attempt, "state") != "settled"
            or _attempt_value(attempt, "terminal_cause") not in _SETTLEMENT_CAUSES
            for attempt in (primary_attempt, sampled_attempt)
        ):
            return EvidenceWriteResult((), "comparison_requires_settled_attempts")
        if (
            primary.context.execution_id != sampled.context.execution_id
            or primary.context.scope.task_class != sampled.context.scope.task_class
        ):
            return EvidenceWriteResult((), "comparison_work_binding_mismatch")
        if not isinstance(agreed, bool):
            raise ValueError("agreed must be a boolean")
        method_version = _bounded_text(
            method_version, field_name="method_version", maximum=64
        )
        pair_id = _comparison_pair_id(
            primary.context.attempt_id, sampled.context.attempt_id
        )
        if observed_at is None:
            terminal_times = (
                _attempt_value(primary_attempt, "settled_at"),
                _attempt_value(sampled_attempt, "settled_at"),
            )
            if any(value is None for value in terminal_times):
                return EvidenceWriteResult((), "comparison_terminal_time_missing")
            observed_at = max(float(value) for value in terminal_times if value is not None)
        observed = _timestamp(observed_at, field_name="observed_at")
        for attempt in (primary_attempt, sampled_attempt):
            settled_at = _timestamp(
                _attempt_value(attempt, "settled_at"), field_name="settled_at"
            )
            if observed < settled_at:
                raise ValueError("sampled agreement cannot predate either settlement")
        written_at = time.time() if recorded_at is None else recorded_at
        metadata = {"method_version": method_version, "pair_id": pair_id}
        outcome = "agree" if agreed else "disagree"
        observations = tuple(
            cls._append_one(
                con,
                context,
                observation_type="sampled_agreement",
                subject_key=pair_id,
                outcome=outcome,
                numeric_value=None,
                metadata=metadata,
                observed_at=observed,
                recorded_at=written_at,
            )
            for context in (primary.context, sampled.context)
        )
        return EvidenceWriteResult(observations)

    def record_sampled_agreement(
        self,
        primary_attempt: object,
        sampled_attempt: object,
        *,
        agreed: bool,
        method_version: str,
        observed_at: float | None = None,
        recorded_at: float | None = None,
    ) -> EvidenceWriteResult:
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            con.execute("BEGIN IMMEDIATE")
            result = self.record_sampled_agreement_in_transaction(
                con,
                primary_attempt,
                sampled_attempt,
                agreed=agreed,
                method_version=method_version,
                observed_at=observed_at,
                recorded_at=recorded_at,
            )
            con.commit()
            return result

    @staticmethod
    def _aggregate_in_connection(
        con: sqlite3.Connection,
        scope: EvidenceScope,
        *,
        minimum_samples: int,
        recent_limit: int,
        cutoff: float | None,
    ) -> ScopeAggregate:
        where, parameters = _scope_where(scope)
        if cutoff is not None:
            where += " AND recorded_at <= ?"
            parameters.append(cutoff)
        where += (
            " AND (observation_type != 'deadline_completion' "
            "OR subject_key = ?)"
        )
        parameters.append(DEADLINE_COMPLETION_SUBJECT)
        count_rows = con.execute(
            f"SELECT observation_type, outcome, COUNT(*) AS sample_count "
            f"FROM node_capability_observations WHERE {where} "
            "GROUP BY observation_type, outcome",
            parameters,
        ).fetchall()
        count_map = {
            (str(row["observation_type"]), str(row["outcome"])): int(
                row["sample_count"]
            )
            for row in count_rows
        }

        def count(observation_type: str, outcome: str | None = None) -> int:
            if outcome is not None:
                return count_map.get((observation_type, outcome), 0)
            return sum(
                sample_count
                for (stored_type, _), sample_count in count_map.items()
                if stored_type == observation_type
            )

        def binary(observation_type: str, positive: str) -> BinaryAggregate:
            samples = count(observation_type)
            positives = count(observation_type, positive)
            return _binary_aggregate(samples, positives)

        def recent_median(observation_type: str) -> tuple[int, float | None]:
            numeric_where = where + " AND observation_type = ? AND numeric_value IS NOT NULL"
            numeric_parameters = [*parameters, observation_type]
            sample_row = con.execute(
                f"SELECT COUNT(*) AS sample_count "
                f"FROM node_capability_observations WHERE {numeric_where}",
                numeric_parameters,
            ).fetchone()
            bounded_rows = con.execute(
                f"SELECT numeric_value FROM node_capability_observations "
                f"WHERE {numeric_where} "
                "ORDER BY observed_at DESC, observation_id DESC LIMIT ?",
                [*numeric_parameters, recent_limit],
            ).fetchall()
            sample_count = int(sample_row["sample_count"]) if sample_row else 0
            bounded = [float(row["numeric_value"]) for row in bounded_rows]
            return sample_count, statistics.median(bounded) if bounded else None

        deadline = binary("deadline_completion", "pass")
        latency_count, latency_median = recent_median("coordinator_wall_seconds")
        throughput_count, throughput_median = recent_median(
            "effective_output_bytes_per_second"
        )
        return ScopeAggregate(
            scope=scope,
            observation_count=sum(count_map.values()),
            settlement_count=count("settlement_outcome"),
            settled_output_count=count("settlement_outcome", "settled_output"),
            settled_worker_error_count=count(
                "settlement_outcome", "settled_worker_error"
            ),
            settled_empty_output_count=count(
                "settlement_outcome", "settled_empty_output"
            ),
            deadline_completion=deadline,
            contract_floor=binary("contract_floor", "pass"),
            sampled_agreement=binary("sampled_agreement", "agree"),
            lease_expiration_count=count("terminal_outcome", "lease_expired"),
            worker_disconnect_count=count("terminal_outcome", "node_stale"),
            latency_sample_count=latency_count,
            recent_median_latency_seconds=latency_median,
            throughput_sample_count=throughput_count,
            recent_median_output_bytes_per_second=throughput_median,
            minimum_samples=minimum_samples,
            insufficient_evidence=deadline.sample_count < minimum_samples,
        )

    def aggregate(
        self,
        scope: EvidenceScope,
        *,
        minimum_samples: int = 5,
        recent_limit: int = 100,
        recorded_before: float | None = None,
    ) -> ScopeAggregate:
        if isinstance(minimum_samples, bool) or not 1 <= minimum_samples <= 10_000:
            raise ValueError("minimum_samples must be between 1 and 10000")
        if isinstance(recent_limit, bool) or not 1 <= recent_limit <= MAX_RECENT_SAMPLES:
            raise ValueError(f"recent_limit must be between 1 and {MAX_RECENT_SAMPLES}")
        cutoff = (
            None
            if recorded_before is None
            else _timestamp(recorded_before, field_name="recorded_before")
        )
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            return self._aggregate_in_connection(
                con,
                scope,
                minimum_samples=minimum_samples,
                recent_limit=recent_limit,
                cutoff=cutoff,
            )

    def aggregate_read_only(
        self,
        scope: EvidenceScope,
        *,
        minimum_samples: int = 5,
        recent_limit: int = 100,
        recorded_before: float | None = None,
    ) -> ScopeAggregate:
        """Aggregate from an initialized database without schema/write access."""

        if isinstance(minimum_samples, bool) or not 1 <= minimum_samples <= 10_000:
            raise ValueError("minimum_samples must be between 1 and 10000")
        if isinstance(recent_limit, bool) or not 1 <= recent_limit <= MAX_RECENT_SAMPLES:
            raise ValueError(f"recent_limit must be between 1 and {MAX_RECENT_SAMPLES}")
        cutoff = (
            None
            if recorded_before is None
            else _timestamp(recorded_before, field_name="recorded_before")
        )
        with self._lock, connection(
            self.path,
            row_factory=sqlite3.Row,
            read_only=True,
        ) as con:
            return self._aggregate_in_connection(
                con,
                scope,
                minimum_samples=minimum_samples,
                recent_limit=recent_limit,
                cutoff=cutoff,
            )

    def list_scope_aggregates(
        self,
        *,
        enrollment_id: str | None = None,
        descriptor_hash: str | None = None,
        task_class: TaskClass | None = None,
        role: EvidenceRole | None = "production",
        limit: int = 100,
        minimum_samples: int = 5,
        recent_limit: int = 100,
        recorded_before: float | None = None,
    ) -> tuple[ScopeAggregateSummary, ...]:
        """List bounded exact-scope summaries for a protected operator surface."""

        if isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if enrollment_id is not None:
            enrollment_id = _bounded_text(
                enrollment_id, field_name="enrollment_id", maximum=64
            )
        if descriptor_hash is not None:
            descriptor_hash = _bounded_text(
                descriptor_hash, field_name="descriptor_hash", maximum=64
            )
            if len(descriptor_hash) != 64 or any(
                character not in "0123456789abcdef"
                for character in descriptor_hash
            ):
                raise ValueError("descriptor_hash must be a lowercase SHA-256 digest")
        if task_class is not None and task_class not in {"dag_subtask", "candidate"}:
            raise ValueError("task_class must be dag_subtask or candidate")
        if role is not None and role not in {"production", "sampled_comparison"}:
            raise ValueError("role must be production or sampled_comparison")
        cutoff = (
            None
            if recorded_before is None
            else _timestamp(recorded_before, field_name="recorded_before")
        )
        # Validate aggregation bounds before running the distinct-scope query.
        if isinstance(minimum_samples, bool) or not 1 <= minimum_samples <= 10_000:
            raise ValueError("minimum_samples must be between 1 and 10000")
        if isinstance(recent_limit, bool) or not 1 <= recent_limit <= MAX_RECENT_SAMPLES:
            raise ValueError(f"recent_limit must be between 1 and {MAX_RECENT_SAMPLES}")

        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in (
            ("enrollment_id", enrollment_id),
            ("descriptor_hash", descriptor_hash),
            ("task_class", task_class),
            ("evidence_role", role),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        if cutoff is not None:
            clauses.append("recorded_at <= ?")
            parameters.append(cutoff)
        where = " AND ".join(clauses) if clauses else "1 = 1"
        group_columns = (
            "node_id, enrollment_id, descriptor_version, descriptor_hash, "
            "executor_kind, executor_version, worker_protocol_version, "
            "model_provider, model_name, model_digest, model_variant, "
            "task_class, evidence_role"
        )
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            rows = con.execute(
                f"SELECT {group_columns}, MAX(observed_at) AS last_observed_at "
                f"FROM node_capability_observations WHERE {where} "
                f"GROUP BY {group_columns} "
                "ORDER BY last_observed_at DESC, enrollment_id, descriptor_hash "
                "LIMIT ?",
                [*parameters, limit],
            ).fetchall()
            summaries = []
            for row in rows:
                scope = _scope_from_row(row)
                summaries.append(
                    ScopeAggregateSummary(
                        node_id=str(row["node_id"]),
                        scope=scope,
                        aggregate=self._aggregate_in_connection(
                            con,
                            scope,
                            minimum_samples=minimum_samples,
                            recent_limit=recent_limit,
                            cutoff=cutoff,
                        ),
                        last_observed_at=float(row["last_observed_at"]),
                    )
                )
        return tuple(summaries)


class CapabilityShadowDecisionStore:
    """Separate append-only store for counterfactual, non-routing decisions."""

    def __init__(self, path: str | Path = "events.db"):
        self.path = Path(path)
        self._lock = threading.RLock()

    def migrate(self) -> None:
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            ensure_capability_shadow_decision_schema(con)
            con.commit()

    def import_legacy_decisions(
        self,
        source_path: str | Path,
        *,
        batch_size: int = 500,
    ) -> int:
        """Idempotently copy pre-isolation decisions from another database."""

        if isinstance(batch_size, bool) or not 1 <= batch_size <= 500:
            raise ValueError("batch_size must be between 1 and 500")
        source = Path(source_path)
        if not source.is_file() or source.resolve() == self.path.resolve():
            return 0

        columns = (
            "decision_id",
            "schema_version",
            "actual_attempt_id",
            "policy_version",
            "decision_at",
            "actual_candidate_id",
            "actual_scope_key",
            "preferred_candidate_id",
            "outcome",
            "rationale_code",
            "candidate_count",
            "candidate_set_digest",
            "recorded_at",
        )
        column_list = ", ".join(columns)
        placeholders = ", ".join("?" for _column in columns)
        self.migrate()
        imported = 0
        with migration_lock(source), connection(
            source,
            row_factory=sqlite3.Row,
        ) as source_con:
            table = source_con.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'capability_shadow_decisions'"
            ).fetchone()
            if table is None:
                return 0
            available_columns = {
                str(row[1])
                for row in source_con.execute(
                    "PRAGMA table_info(capability_shadow_decisions)"
                ).fetchall()
            }
            if not set(columns) <= available_columns:
                raise ValueError("legacy shadow decision schema is incomplete")
            cursor = source_con.execute(
                f"SELECT {column_list} FROM capability_shadow_decisions "
                "WHERE actual_scope_key IS NOT NULL ORDER BY decision_id"
            )
            while rows := cursor.fetchmany(batch_size):
                values = [tuple(row[column] for column in columns) for row in rows]
                with self._lock, migration_lock(self.path), connection(
                    self.path,
                    row_factory=sqlite3.Row,
                ) as destination:
                    ensure_capability_shadow_decision_schema(destination)
                    destination.execute("BEGIN IMMEDIATE")
                    before = destination.total_changes
                    destination.executemany(
                        "INSERT OR IGNORE INTO capability_shadow_decisions "
                        f"({column_list}) VALUES ({placeholders})",
                        values,
                    )
                    imported += destination.total_changes - before
                    decision_ids = [str(row["decision_id"]) for row in rows]
                    id_placeholders = ", ".join("?" for _item in decision_ids)
                    stored_rows = destination.execute(
                        f"SELECT {column_list} FROM capability_shadow_decisions "
                        f"WHERE decision_id IN ({id_placeholders})",
                        decision_ids,
                    ).fetchall()
                    stored = {
                        str(row["decision_id"]): tuple(
                            row[column] for column in columns
                        )
                        for row in stored_rows
                    }
                    for row, expected in zip(rows, values, strict=True):
                        if stored.get(str(row["decision_id"])) != expected:
                            raise ShadowDecisionConflict(
                                "legacy shadow decision conflicts with isolated store"
                            )
                    destination.commit()
        return imported

    def record(
        self, evaluation: ShadowEvaluation, *, recorded_at: float | None = None
    ) -> ShadowDecisionRecord:
        if evaluation.outcome not in {"same", "different", "no_preference"}:
            raise ValueError("invalid shadow outcome")
        if evaluation.rationale_code not in _SHADOW_RATIONALES:
            raise ValueError("invalid shadow rationale code")
        actual_attempt_id = _bounded_text(
            evaluation.actual_attempt_id, field_name="actual_attempt_id"
        )
        policy_version = _bounded_text(
            evaluation.policy_version, field_name="policy_version", maximum=64
        )
        actual_candidate_id = _bounded_text(
            evaluation.actual_candidate_id, field_name="actual_candidate_id"
        )
        if evaluation.preferred_candidate_id is not None:
            _bounded_text(
                evaluation.preferred_candidate_id,
                field_name="preferred_candidate_id",
            )
        if len(evaluation.actual_scope_key) != 64 or any(
            character not in "0123456789abcdef"
            for character in evaluation.actual_scope_key
        ):
            raise ValueError("actual_scope_key must be a lowercase SHA-256 digest")
        decision_at = _timestamp(evaluation.decision_at, field_name="decision_at")
        if not 1 <= evaluation.candidate_count <= MAX_SHADOW_CANDIDATES:
            raise ValueError("shadow candidate_count is outside the protocol limit")
        if len(evaluation.candidate_set_digest) != 64 or any(
            character not in "0123456789abcdef"
            for character in evaluation.candidate_set_digest
        ):
            raise ValueError("candidate_set_digest must be a lowercase SHA-256 digest")
        expected_decision_id = _domain_digest(
            "mycelium.capability-shadow-decision.v1",
            json.dumps(
                {
                    "actual_attempt_id": actual_attempt_id,
                    "policy_version": policy_version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        if evaluation.decision_id != expected_decision_id:
            raise ValueError("shadow decision ID is not canonical")
        if evaluation.outcome == "no_preference":
            if evaluation.preferred_candidate_id is not None:
                raise ValueError("no-preference decisions cannot name a preferred candidate")
        elif evaluation.outcome == "same":
            if evaluation.preferred_candidate_id != actual_candidate_id:
                raise ValueError("same decisions must retain the actual candidate")
        elif evaluation.preferred_candidate_id in {None, actual_candidate_id}:
            raise ValueError("different decisions must name a different candidate")
        written_at = time.time() if recorded_at is None else _timestamp(
            recorded_at, field_name="recorded_at"
        )
        if written_at < decision_at:
            raise ValueError("shadow decision cannot be recorded before it is evaluated")
        values = (
            evaluation.decision_id,
            EVIDENCE_SCHEMA_VERSION,
            evaluation.actual_attempt_id,
            evaluation.policy_version,
            evaluation.decision_at,
            evaluation.actual_candidate_id,
            evaluation.actual_scope_key,
            evaluation.preferred_candidate_id,
            evaluation.outcome,
            evaluation.rationale_code,
            evaluation.candidate_count,
            evaluation.candidate_set_digest,
            written_at,
        )
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            ensure_capability_shadow_decision_schema(con)
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """
                INSERT OR IGNORE INTO capability_shadow_decisions (
                    decision_id, schema_version, actual_attempt_id,
                    policy_version, decision_at, actual_candidate_id,
                    actual_scope_key, preferred_candidate_id, outcome, rationale_code,
                    candidate_count, candidate_set_digest, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = con.execute(
                "SELECT * FROM capability_shadow_decisions WHERE decision_id = ?",
                (evaluation.decision_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - SQLite insert contract
                raise RuntimeError("shadow decision disappeared after insertion")
            record = ShadowDecisionRecord.from_row(row)
            if record.evaluation != evaluation:
                raise ShadowDecisionConflict(
                    "shadow decision ID conflicts with immutable stored content"
                )
            con.commit()
            return record

    def get(self, decision_id: str) -> ShadowDecisionRecord | None:
        decision_id = _bounded_text(
            decision_id, field_name="decision_id", maximum=64
        )
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute(
                "SELECT * FROM capability_shadow_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        return ShadowDecisionRecord.from_row(row) if row is not None else None

    def aggregate_counts(
        self,
        *,
        actual_candidate_id: str | None = None,
        actual_scope_key: str | None = None,
        limit: int = 100,
    ) -> tuple[ShadowDecisionAggregate, ...]:
        """Return bounded shadow outcomes without exposing individual decisions."""

        if isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        clauses = ["actual_scope_key IS NOT NULL"]
        parameters: list[object] = []
        if actual_candidate_id is not None:
            actual_candidate_id = _bounded_text(
                actual_candidate_id,
                field_name="actual_candidate_id",
            )
            clauses.append("actual_candidate_id = ?")
            parameters.append(actual_candidate_id)
        if actual_scope_key is not None:
            actual_scope_key = _bounded_text(
                actual_scope_key, field_name="actual_scope_key", maximum=64
            )
            if len(actual_scope_key) != 64 or any(
                character not in "0123456789abcdef"
                for character in actual_scope_key
            ):
                raise ValueError("actual_scope_key must be a lowercase SHA-256 digest")
            clauses.append("actual_scope_key = ?")
            parameters.append(actual_scope_key)
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            rows = con.execute(
                """
                SELECT actual_candidate_id, actual_scope_key,
                       COUNT(*) AS decision_count,
                       SUM(CASE WHEN outcome = 'same' THEN 1 ELSE 0 END) AS same_count,
                       SUM(CASE WHEN outcome = 'different' THEN 1 ELSE 0 END) AS different_count,
                       SUM(CASE WHEN outcome = 'no_preference' THEN 1 ELSE 0 END) AS no_preference_count,
                       MAX(decision_at) AS last_decision_at
                FROM capability_shadow_decisions
                WHERE """
                + " AND ".join(clauses)
                + """
                GROUP BY actual_candidate_id, actual_scope_key
                ORDER BY last_decision_at DESC, actual_candidate_id, actual_scope_key
                LIMIT ?
                """,
                [*parameters, limit],
            ).fetchall()
        return tuple(
            ShadowDecisionAggregate(
                actual_candidate_id=str(row["actual_candidate_id"]),
                actual_scope_key=str(row["actual_scope_key"]),
                decision_count=int(row["decision_count"]),
                same_count=int(row["same_count"]),
                different_count=int(row["different_count"]),
                no_preference_count=int(row["no_preference_count"]),
                last_decision_at=float(row["last_decision_at"]),
            )
            for row in rows
        )

    def aggregate_counts_for_scope_keys(
        self,
        scope_keys: Sequence[str],
    ) -> tuple[ShadowDecisionAggregate, ...]:
        """Return exact counts for a bounded operator-selected scope set."""

        if len(scope_keys) > 200:
            raise ValueError("at most 200 shadow scope keys may be queried")
        normalized: list[str] = []
        for scope_key in scope_keys:
            parsed = _bounded_text(
                scope_key, field_name="actual_scope_key", maximum=64
            )
            if len(parsed) != 64 or any(
                character not in "0123456789abcdef" for character in parsed
            ):
                raise ValueError(
                    "actual_scope_key must be a lowercase SHA-256 digest"
                )
            normalized.append(parsed)
        normalized = sorted(set(normalized))
        if not normalized:
            return ()
        placeholders = ", ".join("?" for _item in normalized)
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            rows = con.execute(
                f"""
                SELECT actual_candidate_id, actual_scope_key,
                       COUNT(*) AS decision_count,
                       SUM(CASE WHEN outcome = 'same' THEN 1 ELSE 0 END) AS same_count,
                       SUM(CASE WHEN outcome = 'different' THEN 1 ELSE 0 END) AS different_count,
                       SUM(CASE WHEN outcome = 'no_preference' THEN 1 ELSE 0 END) AS no_preference_count,
                       MAX(decision_at) AS last_decision_at
                FROM capability_shadow_decisions
                WHERE actual_scope_key IN ({placeholders})
                GROUP BY actual_candidate_id, actual_scope_key
                ORDER BY last_decision_at DESC, actual_candidate_id, actual_scope_key
                """,
                normalized,
            ).fetchall()
        return tuple(
            ShadowDecisionAggregate(
                actual_candidate_id=str(row["actual_candidate_id"]),
                actual_scope_key=str(row["actual_scope_key"]),
                decision_count=int(row["decision_count"]),
                same_count=int(row["same_count"]),
                different_count=int(row["different_count"]),
                no_preference_count=int(row["no_preference_count"]),
                last_decision_at=float(row["last_decision_at"]),
            )
            for row in rows
        )


class CapabilityShadowOperationalStore:
    """Append-only operational health, separate from evidence and node outcomes."""

    def __init__(self, path: str | Path = "capability-shadow-health.db"):
        self.path = Path(path)
        self._lock = threading.RLock()

    def migrate(self) -> None:
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            ensure_capability_shadow_operational_schema(con)
            con.commit()

    def record(
        self,
        *,
        attempt_id: str,
        phase: ShadowOperationalPhase,
        outcome: ShadowOperationalOutcome,
        reason_code: str,
        occurred_at: float | None = None,
    ) -> ShadowOperationalRecord:
        """Record one terminal phase outcome, or replay the identical classification.

        The event identity is the attempt/phase pair. A same-classification replay
        returns the first row (including its original timestamp); changing either
        outcome or reason under that identity raises a conflict.
        """

        parsed_attempt_id = _bounded_text(
            attempt_id,
            field_name="attempt_id",
        )
        if phase not in {"admission", "evaluation"}:
            raise ValueError("shadow operational phase must be admission or evaluation")
        parsed_reason = _validate_shadow_operational_classification(
            phase=phase,
            outcome=outcome,
            reason_code=reason_code,
        )
        event_time = _timestamp(
            time.time() if occurred_at is None else occurred_at,
            field_name="occurred_at",
        )
        event_id = _shadow_operational_event_id(
            attempt_id=parsed_attempt_id,
            phase=phase,
        )
        values = (
            event_id,
            parsed_attempt_id,
            phase,
            outcome,
            parsed_reason,
            event_time,
        )
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            ensure_capability_shadow_operational_schema(con)
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """
                INSERT OR IGNORE INTO capability_shadow_operational_events (
                    event_id, attempt_id, phase, outcome, reason_code, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = con.execute(
                "SELECT * FROM capability_shadow_operational_events "
                "WHERE attempt_id = ? AND phase = ?",
                (parsed_attempt_id, phase),
            ).fetchone()
            if row is None:  # pragma: no cover - SQLite insert contract
                raise RuntimeError(
                    "shadow operational event disappeared after insertion"
                )
            record = ShadowOperationalRecord.from_row(row)
            immutable_identity = (
                record.event_id,
                record.attempt_id,
                record.phase,
                record.outcome,
                record.reason_code,
            )
            if immutable_identity != values[:5]:
                raise ShadowOperationalEventConflict(
                    "shadow operational event conflicts with immutable stored content"
                )
            con.commit()
            return record

    def get(self, event_id: str) -> ShadowOperationalRecord | None:
        parsed_event_id = _bounded_text(
            event_id,
            field_name="event_id",
            maximum=64,
        )
        if len(parsed_event_id) != 64 or any(
            character not in "0123456789abcdef" for character in parsed_event_id
        ):
            raise ValueError("event_id must be a lowercase SHA-256 digest")
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute(
                "SELECT * FROM capability_shadow_operational_events "
                "WHERE event_id = ?",
                (parsed_event_id,),
            ).fetchone()
        return ShadowOperationalRecord.from_row(row) if row is not None else None

    def get_for_attempt_phase(
        self,
        attempt_id: str,
        phase: ShadowOperationalPhase,
    ) -> ShadowOperationalRecord | None:
        parsed_attempt_id = _bounded_text(
            attempt_id,
            field_name="attempt_id",
        )
        if phase not in {"admission", "evaluation"}:
            raise ValueError("shadow operational phase must be admission or evaluation")
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute(
                "SELECT * FROM capability_shadow_operational_events "
                "WHERE attempt_id = ? AND phase = ?",
                (parsed_attempt_id, phase),
            ).fetchone()
        return ShadowOperationalRecord.from_row(row) if row is not None else None

    def report(
        self,
        *,
        window_started_at: float | None = None,
        window_ended_at: float | None = None,
    ) -> ShadowOperationalReport:
        """Aggregate a cohort selected by its admission timestamp.

        Evaluation events for selected attempts remain in the cohort even if
        they complete after ``window_ended_at``. This keeps the failure numerator
        and offered denominator about the same assignments. A durable evaluation
        whose admission write is missing is selected by its own timestamp and
        counted as one inferred scheduled/offered assignment, so a persisted
        terminal outcome cannot disappear from the report.
        """

        started = (
            None
            if window_started_at is None
            else _timestamp(window_started_at, field_name="window_started_at")
        )
        ended = (
            None
            if window_ended_at is None
            else _timestamp(window_ended_at, field_name="window_ended_at")
        )
        if started is not None and ended is not None and started > ended:
            raise ValueError("window_started_at must not exceed window_ended_at")

        cohort_clauses = ["phase = 'admission'"]
        parameters: list[object] = []
        if started is not None:
            cohort_clauses.append("occurred_at >= ?")
            parameters.append(started)
        if ended is not None:
            cohort_clauses.append("occurred_at <= ?")
            parameters.append(ended)

        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            rows = con.execute(
                """
                WITH cohort AS (
                    SELECT attempt_id
                    FROM capability_shadow_operational_events
                    WHERE """
                + " AND ".join(cohort_clauses)
                + """
                )
                SELECT events.phase, events.outcome, COUNT(*) AS event_count,
                       MAX(events.occurred_at) AS latest_event_at
                FROM capability_shadow_operational_events AS events
                JOIN cohort ON cohort.attempt_id = events.attempt_id
                GROUP BY events.phase, events.outcome
                ORDER BY events.phase, events.outcome
                """,
                parameters,
            ).fetchall()
            orphan_clauses = [
                "evaluation.phase = 'evaluation'",
                "admission.attempt_id IS NULL",
            ]
            orphan_parameters: list[object] = []
            if started is not None:
                orphan_clauses.append("evaluation.occurred_at >= ?")
                orphan_parameters.append(started)
            if ended is not None:
                orphan_clauses.append("evaluation.occurred_at <= ?")
                orphan_parameters.append(ended)
            orphan_rows = con.execute(
                """
                SELECT evaluation.outcome, COUNT(*) AS event_count,
                       MAX(evaluation.occurred_at) AS latest_event_at
                FROM capability_shadow_operational_events AS evaluation
                LEFT JOIN capability_shadow_operational_events AS admission
                  ON admission.attempt_id = evaluation.attempt_id
                 AND admission.phase = 'admission'
                WHERE """
                + " AND ".join(orphan_clauses)
                + """
                GROUP BY evaluation.outcome
                ORDER BY evaluation.outcome
                """,
                orphan_parameters,
            ).fetchall()

        admission_counts: dict[ShadowAdmissionOutcome, int] = {
            outcome: 0 for outcome in SHADOW_ADMISSION_OUTCOMES
        }
        evaluation_counts: dict[ShadowEvaluationOutcome, int] = {
            outcome: 0 for outcome in SHADOW_EVALUATION_OUTCOMES
        }
        latest_event_at: float | None = None
        for row in rows:
            phase = str(row["phase"])
            outcome = str(row["outcome"])
            count = int(row["event_count"])
            if phase == "admission":
                admission_counts[outcome] = count  # type: ignore[index]
            elif phase == "evaluation":
                evaluation_counts[outcome] = count  # type: ignore[index]
            else:  # pragma: no cover - guarded by the schema
                raise RuntimeError("stored shadow operational phase is invalid")
            row_latest = _timestamp(
                row["latest_event_at"],
                field_name="latest_event_at",
            )
            latest_event_at = (
                row_latest
                if latest_event_at is None
                else max(latest_event_at, row_latest)
            )

        orphan_evaluation_total = 0
        for row in orphan_rows:
            outcome = str(row["outcome"])
            count = int(row["event_count"])
            evaluation_counts[outcome] += count  # type: ignore[index]
            orphan_evaluation_total += count
            row_latest = _timestamp(
                row["latest_event_at"],
                field_name="latest_event_at",
            )
            latest_event_at = (
                row_latest
                if latest_event_at is None
                else max(latest_event_at, row_latest)
            )

        assignment_observation_total = (
            sum(admission_counts.values()) + orphan_evaluation_total
        )
        scheduled_total = (
            admission_counts["scheduled"] + orphan_evaluation_total
        )
        completed_total = evaluation_counts["completed"]
        skipped_total = (
            admission_counts["disabled"] + admission_counts["not_applicable"]
        )
        offered_total = (
            scheduled_total
            + admission_counts["queue_saturated"]
            + admission_counts["scope_capture_failed"]
        )
        evaluation_terminal_total = sum(evaluation_counts.values())
        failed_total = (
            admission_counts["queue_saturated"]
            + admission_counts["scope_capture_failed"]
            + evaluation_counts["evaluator_failed"]
            + evaluation_counts["decision_write_failed"]
            + evaluation_counts["cancelled_on_shutdown"]
        )
        return ShadowOperationalReport(
            admission_counts=admission_counts,
            evaluation_counts=evaluation_counts,
            orphan_evaluation_total=orphan_evaluation_total,
            assignment_observation_total=assignment_observation_total,
            offered_total=offered_total,
            scheduled_total=scheduled_total,
            completed_total=completed_total,
            skipped_total=skipped_total,
            failed_total=failed_total,
            pending_total=max(0, scheduled_total - evaluation_terminal_total),
            drop_failure_numerator=failed_total,
            drop_failure_denominator=offered_total,
            drop_failure_rate=(
                failed_total / offered_total if offered_total else None
            ),
            latest_event_at=latest_event_at,
            window_started_at=started,
            window_ended_at=ended,
        )


def _scope_where(scope: EvidenceScope) -> tuple[str, list[object]]:
    where = (
        "enrollment_id = ? AND descriptor_version = ? AND descriptor_hash = ? "
        "AND executor_kind = ? AND executor_version IS ? "
        "AND worker_protocol_version = ? AND model_provider = ? "
        "AND model_name = ? AND model_digest IS ? AND model_variant IS ? "
        "AND task_class = ? AND evidence_role = ?"
    )
    parameters: list[object] = [
        scope.enrollment_id,
        scope.descriptor_version,
        scope.descriptor_hash,
        scope.executor_kind,
        scope.executor_version,
        scope.worker_protocol_version,
        scope.model_provider,
        scope.model_name,
        scope.model_digest,
        scope.model_variant,
        scope.task_class,
        scope.evidence_role,
    ]
    return where, parameters


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
            rate * (1 - rate) / sample_count
            + z * z / (4 * sample_count * sample_count)
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


def evaluate_shadow_preference(
    *,
    actual_attempt_id: str,
    actual_candidate_id: str,
    candidates: Sequence[EligibleShadowCandidate],
    minimum_samples: int,
    decision_at: float,
    policy_version: str = SHADOW_POLICY_VERSION,
) -> ShadowEvaluation:
    """Pure conservative counterfactual over already-hard-eligible candidates."""

    actual_attempt_id = _bounded_text(
        actual_attempt_id, field_name="actual_attempt_id"
    )
    actual_candidate_id = _bounded_text(
        actual_candidate_id, field_name="actual_candidate_id"
    )
    policy_version = _bounded_text(
        policy_version, field_name="policy_version", maximum=64
    )
    decision_at = _timestamp(decision_at, field_name="decision_at")
    if isinstance(minimum_samples, bool) or not 1 <= minimum_samples <= 10_000:
        raise ValueError("minimum_samples must be between 1 and 10000")
    if not candidates or len(candidates) > MAX_SHADOW_CANDIDATES:
        raise ValueError(
            f"shadow evaluation requires 1-{MAX_SHADOW_CANDIDATES} candidates"
        )
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("shadow candidate IDs must be unique")
    if actual_candidate_id not in by_id:
        raise ValueError("actual candidate must be in the eligible candidate set")
    candidate_set_payload = json.dumps(
        sorted(
            (candidate.candidate_id, candidate.aggregate.scope.scope_key)
            for candidate in candidates
        ),
        separators=(",", ":"),
    )
    candidate_set_digest = _domain_digest(
        "mycelium.capability-shadow-candidate-set.v1", candidate_set_payload
    )
    decision_payload = json.dumps(
        {
            "actual_attempt_id": actual_attempt_id,
            "policy_version": policy_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    decision_id = _domain_digest(
        "mycelium.capability-shadow-decision.v1", decision_payload
    )

    def result(
        preferred: str | None, outcome: ShadowOutcome, rationale: str
    ) -> ShadowEvaluation:
        return ShadowEvaluation(
            decision_id=decision_id,
            actual_attempt_id=actual_attempt_id,
            policy_version=policy_version,
            decision_at=decision_at,
            actual_candidate_id=actual_candidate_id,
            actual_scope_key=by_id[actual_candidate_id].aggregate.scope.scope_key,
            preferred_candidate_id=preferred,
            outcome=outcome,
            rationale_code=rationale,
            candidate_count=len(candidates),
            candidate_set_digest=candidate_set_digest,
        )

    if len(candidates) == 1:
        return result(None, "no_preference", "single_candidate")
    if any(
        candidate.aggregate.deadline_completion.sample_count < minimum_samples
        for candidate in candidates
    ):
        return result(None, "no_preference", "insufficient_deadline_evidence")

    contract_counts = [
        candidate.aggregate.contract_floor.sample_count for candidate in candidates
    ]
    use_contract = any(contract_counts)
    if use_contract and any(count < minimum_samples for count in contract_counts):
        return result(None, "no_preference", "insufficient_contract_evidence")

    latency_counts = [candidate.aggregate.latency_sample_count for candidate in candidates]
    use_latency = any(latency_counts)
    if use_latency and any(count < minimum_samples for count in latency_counts):
        return result(None, "no_preference", "insufficient_latency_evidence")

    throughput_counts = [
        candidate.aggregate.throughput_sample_count for candidate in candidates
    ]
    use_throughput = any(throughput_counts)
    if use_throughput and any(count < minimum_samples for count in throughput_counts):
        return result(None, "no_preference", "insufficient_throughput_evidence")

    def dimensions(candidate: EligibleShadowCandidate) -> tuple[float, ...]:
        aggregate = candidate.aggregate
        deadline = aggregate.deadline_completion.wilson_low
        if deadline is None:  # guarded by minimum samples
            raise AssertionError("deadline evidence unexpectedly absent")
        values = [deadline]
        if use_contract:
            contract = aggregate.contract_floor.wilson_low
            if contract is None:  # guarded by minimum samples
                raise AssertionError("contract evidence unexpectedly absent")
            values.append(contract)
        if use_latency:
            latency = aggregate.recent_median_latency_seconds
            if latency is None:  # guarded by minimum samples
                raise AssertionError("latency evidence unexpectedly absent")
            values.append(-latency)
        if use_throughput:
            throughput = aggregate.recent_median_output_bytes_per_second
            if throughput is None:  # guarded by minimum samples
                raise AssertionError("throughput evidence unexpectedly absent")
            values.append(throughput)
        return tuple(values)

    vectors = {candidate.candidate_id: dimensions(candidate) for candidate in candidates}
    best_vector = max(vectors.values())
    best_ids = sorted(
        candidate_id
        for candidate_id, vector in vectors.items()
        if vector == best_vector
    )
    if len(best_ids) > 1:
        if actual_candidate_id in best_ids:
            return result(actual_candidate_id, "same", "tie_retained_actual")
        return result(None, "no_preference", "ambiguous_best")
    preferred = best_ids[0]
    if preferred == actual_candidate_id:
        return result(preferred, "same", "evidence_preferred_actual")
    return result(preferred, "different", "evidence_preferred_alternative")
