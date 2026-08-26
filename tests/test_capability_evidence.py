"""Standalone durable capability evidence and shadow-policy tests."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, replace

import pytest

from capability_evidence import (
    BinaryAggregate,
    CapabilityEvidenceStore,
    CapabilityShadowDecisionStore,
    EligibleShadowCandidate,
    EvidenceConflict,
    EvidenceScope,
    ScopeAggregate,
    ShadowDecisionConflict,
    evaluate_shadow_preference,
)
from node_capabilities import NodeCapabilityDescriptorV1, NodeCapabilitySnapshotStore
from node_enrollments import NodeEnrollmentStore, new_enrollment_credential


MODEL_DIGEST = "sha256:" + "a" * 64
SECOND_MODEL_DIGEST = "sha256:" + "b" * 64


def _descriptor(*, executor_version: str = "0.11.4") -> NodeCapabilityDescriptorV1:
    return NodeCapabilityDescriptorV1.model_validate(
        {
            "descriptor_version": "1",
            "executor": {
                "kind": "ollama",
                "version": executor_version,
                "worker_protocol_version": "1",
            },
            "models": [
                {
                    "provider": "ollama",
                    "name": "qwen3.5:4b",
                    "digest": MODEL_DIGEST,
                    "context_tokens": 16_384,
                    "variant": "Q4_K_M",
                },
                {
                    "provider": "ollama",
                    "name": "alternate:4b",
                    "digest": SECOND_MODEL_DIGEST,
                    "context_tokens": 16_384,
                    "variant": "Q5_K_M",
                },
            ],
            "hardware": {
                "architecture": "x86_64",
                "logical_cpu_count": 8,
                "total_memory_bytes": 16 * 1024**3,
                "gpus": [],
            },
            "features": ["code"],
            "limits": {
                "max_concurrent_execution_units": 1,
                "max_output_bytes": 1_048_576,
                "max_context_tokens": 16_384,
            },
            "isolation": {"kind": "none"},
        }
    )


def _register(database, node_id: str, *, descriptor=None):
    enrollment = NodeEnrollmentStore(database).bootstrap(
        node_id, new_enrollment_credential(), now=1
    ).record
    snapshot = NodeCapabilitySnapshotStore(database).remember(
        enrollment.enrollment_id, descriptor or _descriptor(), now=2
    )
    return enrollment, snapshot


def _attempt(
    enrollment,
    snapshot,
    *,
    suffix: str,
    role: str = "production",
    unit_kind: str = "candidate",
    model_name: str = "qwen3.5:4b",
    model_digest: str | None = MODEL_DIGEST,
    state: str = "settled",
    terminal_cause: str | None = "settled_output",
    issued_at: float = 100,
    lease_expires_at: float = 200,
    settled_at: float | None = 110,
    execution_id: str = "execution-1",
    comparison_primary_attempt_id: str | None = None,
):
    return {
        "attempt_id": f"attempt-{suffix}",
        "execution_id": execution_id,
        "execution_unit_id": f"unit-{suffix}",
        "execution_unit_kind": unit_kind,
        "assigned_node_id": enrollment.node_id,
        "assigned_enrollment_id": enrollment.enrollment_id,
        "assigned_descriptor_version": snapshot.descriptor_version,
        "assigned_descriptor_hash": snapshot.descriptor_hash,
        "assigned_model_provider": "ollama",
        "assigned_model_name": model_name,
        "assigned_model_digest": model_digest,
        "evidence_role": role,
        "comparison_primary_attempt_id": comparison_primary_attempt_id,
        "contract_version": "1",
        "state": state,
        "terminal_cause": terminal_cause,
        "issued_at": issued_at,
        "lease_expires_at": lease_expires_at,
        "settled_at": settled_at,
        # These deliberately sensitive fields are not part of the evidence API.
        "prompt": "TOP SECRET PROMPT",
        "output": "TOP SECRET OUTPUT",
        "reason": "TOP SECRET REASON",
    }


def _resolved_scope(store: CapabilityEvidenceStore, attempt) -> EvidenceScope:
    resolution = store.resolve_scope(attempt)
    assert resolution.context is not None
    return resolution.context.scope


def test_scope_requires_every_authoritative_binding_and_validated_snapshot(tmp_path):
    database = tmp_path / "evidence.db"
    enrollment, snapshot = _register(database, "worker-a")
    store = CapabilityEvidenceStore(database)
    attempt = _attempt(enrollment, snapshot, suffix="complete")

    resolution = store.resolve_scope(attempt)

    assert resolution.usable is True
    assert resolution.context is not None
    assert resolution.context.scope == EvidenceScope(
        enrollment_id=enrollment.enrollment_id,
        descriptor_version="1",
        descriptor_hash=snapshot.descriptor_hash,
        executor_kind="ollama",
        executor_version="0.11.4",
        worker_protocol_version="1",
        model_provider="ollama",
        model_name="qwen3.5:4b",
        model_digest=MODEL_DIGEST,
        model_variant="Q4_K_M",
        task_class="candidate",
        evidence_role="production",
    )

    for field in (
        "assigned_enrollment_id",
        "assigned_descriptor_version",
        "assigned_descriptor_hash",
        "assigned_model_provider",
        "assigned_model_name",
        "execution_id",
        "execution_unit_id",
        "evidence_role",
    ):
        incomplete = dict(attempt)
        incomplete[field] = None
        excluded = store.resolve_scope(incomplete)
        assert excluded.usable is False, field

    unsupported = dict(attempt, execution_unit_kind="reviewer")
    assert store.resolve_scope(unsupported).excluded_reason_code == (
        "unsupported_task_class"
    )
    mismatched_model = dict(attempt, assigned_model_digest=SECOND_MODEL_DIGEST)
    assert store.resolve_scope(mismatched_model).excluded_reason_code == (
        "selected_model_binding_mismatch"
    )


def test_historical_or_incomplete_attempt_is_excluded_without_a_row(tmp_path):
    database = tmp_path / "evidence.db"
    enrollment, snapshot = _register(database, "worker-a")
    store = CapabilityEvidenceStore(database)
    historical = _attempt(enrollment, snapshot, suffix="legacy")
    historical["assigned_enrollment_id"] = None
    historical["assigned_descriptor_hash"] = None
    historical["assigned_model_name"] = None

    result = store.record_settlement(
        historical, accepted_at=110, output_bytes=12, recorded_at=120
    )

    assert result.recorded is False
    assert result.excluded_reason_code == "attempt_binding_incomplete"
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM node_capability_observations"
        ).fetchone()[0] == 0


def test_settlement_is_append_only_deterministic_replay_safe_and_secret_free(tmp_path):
    database = tmp_path / "evidence.db"
    enrollment, snapshot = _register(database, "worker-a")
    store = CapabilityEvidenceStore(database)
    attempt = _attempt(enrollment, snapshot, suffix="settlement")

    first = store.record_settlement(
        attempt, accepted_at=110, output_bytes=100, recorded_at=120
    )
    replay = store.record_settlement(
        attempt, accepted_at=110, output_bytes=100, recorded_at=999
    )

    assert len(first.observations) == 5
    assert [item.observation_id for item in replay.observations] == [
        item.observation_id for item in first.observations
    ]
    assert {item.recorded_at for item in replay.observations} == {120}
    assert {item.observation_type for item in first.observations} == {
        "settlement_outcome",
        "deadline_completion",
        "coordinator_wall_seconds",
        "output_bytes",
        "effective_output_bytes_per_second",
    }
    with pytest.raises(EvidenceConflict):
        store.record_settlement(
            attempt, accepted_at=110, output_bytes=101, recorded_at=121
        )

    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM node_capability_observations").fetchall()
        serialized = json.dumps([dict(row) for row in rows])
        columns = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info(node_capability_observations)"
            ).fetchall()
        }
        assert len(rows) == 5
        assert not {"prompt", "output", "error", "reason", "secret"} & columns
        assert "TOP SECRET" not in serialized
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute(
                "UPDATE node_capability_observations SET outcome = 'fail'"
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("DELETE FROM node_capability_observations")


@pytest.mark.parametrize(
    ("cause", "state", "expected_outcome"),
    [
        ("lease_expired", "expired", "lease_expired"),
        ("node_stale", "reclaimed", "node_stale"),
    ],
)
def test_only_typed_lease_and_disconnect_causes_become_terminal_evidence(
    tmp_path, cause, state, expected_outcome
):
    database = tmp_path / f"{cause}.db"
    enrollment, snapshot = _register(database, "worker-a")
    store = CapabilityEvidenceStore(database)
    attempt = _attempt(
        enrollment,
        snapshot,
        suffix=cause,
        state=state,
        terminal_cause=cause,
        settled_at=170,
    )

    result = store.record_terminal(attempt, terminal_at=170, recorded_at=180)

    assert [item.outcome for item in result.observations] == [
        expected_outcome,
        "fail",
    ]


@pytest.mark.parametrize(
    "cause",
    [
        "output_payload_limit",
        "error_payload_limit",
        "stream_output_limit",
        "stream_batch_limit",
        "stream_rate_limit",
        "execution_cancelled",
        "execution_deadline",
        "receipt_binding_failure",
        "enrollment_reclaimed",
        "session_replaced",
        "coordinator_restart",
        "superseded",
    ],
)
def test_non_attributable_terminal_causes_are_excluded_without_reason_parsing(
    tmp_path, cause
):
    database = tmp_path / f"excluded-{cause}.db"
    enrollment, snapshot = _register(database, "worker-a")
    store = CapabilityEvidenceStore(database)
    attempt = _attempt(
        enrollment,
        snapshot,
        suffix=cause,
        state="cancelled",
        terminal_cause=cause,
        settled_at=170,
    )
    attempt["reason"] = "node_stale lease_expired TOP SECRET"

    result = store.record_terminal(attempt, terminal_at=170, recorded_at=180)

    assert result.recorded is False
    assert result.excluded_reason_code == "terminal_cause_not_worker_attributable"
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM node_capability_observations WHERE attempt_id = ?",
            (attempt["attempt_id"],),
        ).fetchone()[0] == 0


def test_contract_floor_is_post_terminal_bounded_and_conflict_checked(tmp_path):
    database = tmp_path / "evidence.db"
    enrollment, snapshot = _register(database, "worker-a")
    store = CapabilityEvidenceStore(database)
    attempt = _attempt(enrollment, snapshot, suffix="floor")

    first = store.record_contract_floor(
        attempt, passed=True, method_version="validator-v1", recorded_at=120
    )
    replay = store.record_contract_floor(
        attempt, passed=True, method_version="validator-v1", recorded_at=999
    )

    assert first.observations[0].metadata == {
        "contract_version": "1",
        "method_version": "validator-v1",
    }
    assert replay.observations[0].observation_id == first.observations[0].observation_id
    with pytest.raises(EvidenceConflict):
        store.record_contract_floor(
            attempt, passed=False, method_version="validator-v1", recorded_at=121
        )
    with pytest.raises(ValueError, match="1-64"):
        different = dict(attempt, attempt_id="attempt-floor-long-method")
        store.record_contract_floor(
            different, passed=True, method_version="x" * 65, recorded_at=122
        )

    active = dict(
        attempt,
        attempt_id="attempt-active-floor",
        execution_unit_id="unit-active-floor",
        state="active",
        terminal_cause=None,
    )
    excluded = store.record_contract_floor(active, passed=False, recorded_at=122)
    assert excluded.excluded_reason_code == "contract_floor_requires_settled_attempt"


def test_sampled_agreement_is_atomic_paired_separate_and_replay_safe(tmp_path):
    database = tmp_path / "evidence.db"
    primary_enrollment, primary_snapshot = _register(database, "worker-a")
    sampled_enrollment, sampled_snapshot = _register(database, "worker-b")
    store = CapabilityEvidenceStore(database)
    primary = _attempt(primary_enrollment, primary_snapshot, suffix="primary")
    sampled = _attempt(
        sampled_enrollment,
        sampled_snapshot,
        suffix="sample",
        role="sampled_comparison",
        settled_at=112,
        comparison_primary_attempt_id=primary["attempt_id"],
    )

    first = store.record_sampled_agreement(
        primary,
        sampled,
        agreed=True,
        method_version="shape-v1",
        recorded_at=120,
    )
    replay = store.record_sampled_agreement(
        primary,
        sampled,
        agreed=True,
        method_version="shape-v1",
        recorded_at=999,
    )

    assert len(first.observations) == 2
    assert {item.scope.evidence_role for item in first.observations} == {
        "production",
        "sampled_comparison",
    }
    assert len({item.metadata["pair_id"] for item in first.observations}) == 1
    assert [item.observation_id for item in replay.observations] == [
        item.observation_id for item in first.observations
    ]
    with pytest.raises(EvidenceConflict):
        store.record_sampled_agreement(
            primary,
            sampled,
            agreed=False,
            method_version="shape-v1",
            recorded_at=121,
        )

    wrong_role = dict(sampled, attempt_id="attempt-wrong-role", evidence_role="production")
    excluded = store.record_sampled_agreement(
        primary,
        wrong_role,
        agreed=True,
        method_version="shape-v1",
        recorded_at=121,
    )
    assert excluded.excluded_reason_code == "comparison_sample_role_invalid"

    unrelated = dict(
        sampled,
        attempt_id="attempt-unrelated-sample",
        execution_unit_id="unit-unrelated-sample",
        comparison_primary_attempt_id="attempt-another-primary",
    )
    excluded = store.record_sampled_agreement(
        primary,
        unrelated,
        agreed=True,
        method_version="shape-v1",
        recorded_at=121,
    )
    assert excluded.excluded_reason_code == "comparison_primary_binding_mismatch"


def test_exact_scope_aggregation_reports_counts_intervals_and_bounded_medians(tmp_path):
    database = tmp_path / "evidence.db"
    enrollment, snapshot = _register(database, "worker-a")
    store = CapabilityEvidenceStore(database)
    attempts = []
    for index, wall in enumerate((10, 20, 30), start=1):
        attempt = _attempt(
            enrollment,
            snapshot,
            suffix=f"success-{index}",
            settled_at=100 + wall,
        )
        attempts.append(attempt)
        store.record_settlement(
            attempt,
            accepted_at=100 + wall,
            output_bytes=wall * 10,
            recorded_at=200 + index,
        )
        store.record_contract_floor(
            attempt,
            passed=index != 3,
            method_version="validator-v1",
            recorded_at=210 + index,
        )
    for index, cause in enumerate(("lease_expired", "node_stale"), start=4):
        failed = _attempt(
            enrollment,
            snapshot,
            suffix=f"failure-{index}",
            state="expired" if cause == "lease_expired" else "reclaimed",
            terminal_cause=cause,
            settled_at=180,
        )
        store.record_terminal(failed, terminal_at=180, recorded_at=220 + index)

    scope = _resolved_scope(store, attempts[0])
    aggregate = store.aggregate(scope, minimum_samples=5, recent_limit=2)

    assert aggregate.observation_count == 22
    assert aggregate.settlement_count == 3
    assert aggregate.deadline_completion.sample_count == 5
    assert aggregate.deadline_completion.positive_count == 3
    assert aggregate.deadline_completion.negative_count == 2
    assert aggregate.deadline_completion.rate == pytest.approx(0.6)
    assert 0 < aggregate.deadline_completion.wilson_low < 0.6
    assert 0.6 < aggregate.deadline_completion.wilson_high < 1
    assert aggregate.contract_floor.sample_count == 3
    assert aggregate.contract_floor.positive_count == 2
    assert aggregate.lease_expiration_count == 1
    assert aggregate.worker_disconnect_count == 1
    assert aggregate.latency_sample_count == 3
    assert aggregate.recent_median_latency_seconds == 25
    assert aggregate.throughput_sample_count == 3
    assert aggregate.recent_median_output_bytes_per_second == 10
    assert aggregate.insufficient_evidence is False


def test_aggregation_never_crosses_model_role_task_or_recording_cutoff(tmp_path):
    database = tmp_path / "evidence.db"
    enrollment, snapshot = _register(database, "worker-a")
    store = CapabilityEvidenceStore(database)
    base = _attempt(enrollment, snapshot, suffix="base")
    store.record_settlement(base, accepted_at=110, output_bytes=10, recorded_at=120)

    alternate = _attempt(
        enrollment,
        snapshot,
        suffix="alternate",
        model_name="alternate:4b",
        model_digest=SECOND_MODEL_DIGEST,
    )
    sampled = _attempt(
        enrollment,
        snapshot,
        suffix="sampled",
        role="sampled_comparison",
    )
    dag = _attempt(
        enrollment, snapshot, suffix="dag", unit_kind="dag_subtask"
    )
    for attempt in (alternate, sampled, dag):
        store.record_settlement(
            attempt, accepted_at=110, output_bytes=10, recorded_at=130
        )

    base_scope = _resolved_scope(store, base)
    assert store.aggregate(base_scope, minimum_samples=2).deadline_completion.sample_count == 1
    before = store.aggregate(
        base_scope, minimum_samples=1, recorded_before=119.999
    )
    after = store.aggregate(base_scope, minimum_samples=1, recorded_before=120)
    assert before.observation_count == 0
    assert before.insufficient_evidence is True
    assert after.observation_count == 5
    assert after.insufficient_evidence is False


def test_operator_scope_summaries_are_bounded_filtered_and_privacy_safe(tmp_path):
    database = tmp_path / "operator-evidence.db"
    enrollment_a, snapshot_a = _register(database, "worker-a")
    enrollment_b, snapshot_b = _register(database, "worker-b")
    store = CapabilityEvidenceStore(database)
    production = _attempt(enrollment_a, snapshot_a, suffix="production")
    dag = _attempt(
        enrollment_a, snapshot_a, suffix="production-dag", unit_kind="dag_subtask"
    )
    sampled = _attempt(
        enrollment_b,
        snapshot_b,
        suffix="sampled",
        role="sampled_comparison",
    )
    for index, attempt in enumerate((production, dag, sampled)):
        accepted_at = 110 + index
        attempt["settled_at"] = accepted_at
        store.record_settlement(
            attempt,
            accepted_at=accepted_at,
            output_bytes=10,
            recorded_at=120 + index,
        )

    default_summaries = store.list_scope_aggregates(minimum_samples=1)
    candidate_only = store.list_scope_aggregates(
        enrollment_id=enrollment_a.enrollment_id,
        descriptor_hash=snapshot_a.descriptor_hash,
        task_class="candidate",
        limit=1,
        minimum_samples=1,
    )
    sampled_only = store.list_scope_aggregates(
        role="sampled_comparison", minimum_samples=1
    )
    every_role = store.list_scope_aggregates(role=None, minimum_samples=1)

    assert len(default_summaries) == 2
    assert {item.scope.evidence_role for item in default_summaries} == {
        "production"
    }
    assert len(candidate_only) == 1
    assert candidate_only[0].node_id == "worker-a"
    assert candidate_only[0].scope.task_class == "candidate"
    assert candidate_only[0].last_observed_at == 110
    assert candidate_only[0].aggregate.deadline_completion.sample_count == 1
    assert len(sampled_only) == 1
    assert sampled_only[0].node_id == "worker-b"
    assert len(every_role) == 3
    assert set(candidate_only[0].__dataclass_fields__) == {
        "node_id",
        "scope",
        "aggregate",
        "last_observed_at",
    }
    assert "TOP SECRET" not in json.dumps(asdict(candidate_only[0]))

    for invalid_limit in (0, 201):
        with pytest.raises(ValueError, match="1 and 200"):
            store.list_scope_aggregates(limit=invalid_limit)
    with pytest.raises(ValueError, match="task_class"):
        store.list_scope_aggregates(task_class="reviewer")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="role"):
        store.list_scope_aggregates(role="ranked")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="SHA-256"):
        store.list_scope_aggregates(descriptor_hash="not-a-digest")


def test_best_effort_wrapper_contains_optional_evidence_failure():
    def failure():
        raise sqlite3.OperationalError("evidence database unavailable")

    result = CapabilityEvidenceStore.best_effort(failure)

    assert result.succeeded is False
    assert result.value is None
    assert result.error_code == "evidence_write_failed"


def test_best_effort_transaction_rolls_back_only_optional_evidence_work(tmp_path):
    database = tmp_path / "best-effort.db"
    with sqlite3.connect(database) as con:
        con.execute("CREATE TABLE production_work (value TEXT NOT NULL)")
        con.execute("CREATE TABLE optional_evidence (value TEXT NOT NULL)")
        con.execute("BEGIN")
        con.execute("INSERT INTO production_work VALUES ('kept')")

        def partial_failure():
            con.execute("INSERT INTO optional_evidence VALUES ('rolled-back')")
            raise sqlite3.OperationalError("optional evidence failed")

        result = CapabilityEvidenceStore.best_effort_in_transaction(
            con, partial_failure
        )
        con.commit()

    assert result.succeeded is False
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT value FROM production_work").fetchall() == [
            ("kept",)
        ]
        assert con.execute("SELECT value FROM optional_evidence").fetchall() == []


def _binary(samples: int, positives: int, low: float) -> BinaryAggregate:
    return BinaryAggregate(
        sample_count=samples,
        positive_count=positives,
        negative_count=samples - positives,
        rate=(positives / samples if samples else None),
        wilson_low=(low if samples else None),
        wilson_high=(min(1.0, low + 0.3) if samples else None),
    )


def _shadow_aggregate(
    scope: EvidenceScope,
    *,
    deadline_samples: int,
    deadline_low: float,
    contract_samples: int = 0,
    contract_low: float = 0,
    latency_samples: int = 0,
    latency: float | None = None,
    throughput_samples: int = 0,
    throughput: float | None = None,
) -> ScopeAggregate:
    return ScopeAggregate(
        scope=scope,
        observation_count=deadline_samples + contract_samples + latency_samples,
        settlement_count=deadline_samples,
        settled_output_count=deadline_samples,
        settled_worker_error_count=0,
        settled_empty_output_count=0,
        deadline_completion=_binary(
            deadline_samples, deadline_samples, deadline_low
        ),
        contract_floor=_binary(contract_samples, contract_samples, contract_low),
        sampled_agreement=_binary(0, 0, 0),
        lease_expiration_count=0,
        worker_disconnect_count=0,
        latency_sample_count=latency_samples,
        recent_median_latency_seconds=latency,
        throughput_sample_count=throughput_samples,
        recent_median_output_bytes_per_second=throughput,
        minimum_samples=5,
        insufficient_evidence=deadline_samples < 5,
    )


def test_shadow_evaluator_is_conservative_lexicographic_and_tie_retains_actual(tmp_path):
    database = tmp_path / "evidence.db"
    enrollment, snapshot = _register(database, "worker-a")
    scope_a = _resolved_scope(
        CapabilityEvidenceStore(database),
        _attempt(enrollment, snapshot, suffix="scope"),
    )
    scope_b = replace(scope_a, enrollment_id="enrollment-shadow-b")

    insufficient = evaluate_shadow_preference(
        actual_attempt_id="attempt-issued",
        actual_candidate_id="node-a",
        candidates=(
            EligibleShadowCandidate(
                "node-a",
                _shadow_aggregate(
                    scope_a, deadline_samples=5, deadline_low=0.5
                ),
            ),
            EligibleShadowCandidate(
                "node-b",
                _shadow_aggregate(
                    scope_b, deadline_samples=4, deadline_low=0.7
                ),
            ),
        ),
        minimum_samples=5,
        decision_at=200,
    )
    assert insufficient.outcome == "no_preference"
    assert insufficient.preferred_candidate_id is None
    assert insufficient.rationale_code == "insufficient_deadline_evidence"

    different = evaluate_shadow_preference(
        actual_attempt_id="attempt-issued",
        actual_candidate_id="node-a",
        candidates=(
            EligibleShadowCandidate(
                "node-a",
                _shadow_aggregate(
                    scope_a, deadline_samples=5, deadline_low=0.5
                ),
            ),
            EligibleShadowCandidate(
                "node-b",
                _shadow_aggregate(
                    scope_b, deadline_samples=5, deadline_low=0.7
                ),
            ),
        ),
        minimum_samples=5,
        decision_at=200,
    )
    assert different.outcome == "different"
    assert different.preferred_candidate_id == "node-b"

    tie = evaluate_shadow_preference(
        actual_attempt_id="attempt-tie",
        actual_candidate_id="node-a",
        candidates=(
            EligibleShadowCandidate(
                "node-a",
                _shadow_aggregate(
                    scope_a, deadline_samples=5, deadline_low=0.7
                ),
            ),
            EligibleShadowCandidate(
                "node-b",
                _shadow_aggregate(
                    scope_b, deadline_samples=5, deadline_low=0.7
                ),
            ),
        ),
        minimum_samples=5,
        decision_at=200,
    )
    assert tie.outcome == "same"
    assert tie.preferred_candidate_id == "node-a"
    assert tie.rationale_code == "tie_retained_actual"
    assert different.decision_id == insufficient.decision_id
    assert different.candidate_set_digest == insufficient.candidate_set_digest


def test_shadow_evaluator_rejects_ineligible_candidates_and_uneven_dimensions(tmp_path):
    database = tmp_path / "evidence.db"
    enrollment, snapshot = _register(database, "worker-a")
    scope = _resolved_scope(
        CapabilityEvidenceStore(database),
        _attempt(enrollment, snapshot, suffix="scope"),
    )
    aggregate = _shadow_aggregate(scope, deadline_samples=5, deadline_low=0.5)

    with pytest.raises(ValueError, match="hard eligible"):
        EligibleShadowCandidate("node-a", aggregate, hard_eligible=False)  # type: ignore[arg-type]

    uneven_contract = evaluate_shadow_preference(
        actual_attempt_id="attempt-issued",
        actual_candidate_id="node-a",
        candidates=(
            EligibleShadowCandidate(
                "node-a",
                _shadow_aggregate(
                    scope,
                    deadline_samples=5,
                    deadline_low=0.5,
                    contract_samples=5,
                    contract_low=0.4,
                ),
            ),
            EligibleShadowCandidate(
                "node-b",
                _shadow_aggregate(
                    replace(scope, enrollment_id="enrollment-b"),
                    deadline_samples=5,
                    deadline_low=0.6,
                ),
            ),
        ),
        minimum_samples=5,
        decision_at=200,
    )
    assert uneven_contract.outcome == "no_preference"
    assert uneven_contract.rationale_code == "insufficient_contract_evidence"


def test_shadow_decisions_have_a_separate_append_only_replay_safe_store(tmp_path):
    database = tmp_path / "shadow.db"
    enrollment, snapshot = _register(database, "worker-a")
    scope = _resolved_scope(
        CapabilityEvidenceStore(database),
        _attempt(enrollment, snapshot, suffix="scope"),
    )
    evaluation = evaluate_shadow_preference(
        actual_attempt_id="attempt-issued",
        actual_candidate_id="node-a",
        candidates=(
            EligibleShadowCandidate(
                "node-a",
                _shadow_aggregate(
                    scope, deadline_samples=5, deadline_low=0.5
                ),
            ),
        ),
        minimum_samples=5,
        decision_at=200,
    )
    store = CapabilityShadowDecisionStore(database)

    first = store.record(evaluation, recorded_at=210)
    replay = store.record(evaluation, recorded_at=999)

    assert replay == first
    assert store.get(evaluation.decision_id) == first
    conflicting = replace(
        evaluation,
        preferred_candidate_id="node-a",
        outcome="same",
        rationale_code="evidence_preferred_actual",
    )
    with pytest.raises(ShadowDecisionConflict):
        store.record(conflicting, recorded_at=211)
    invalid = replace(evaluation, decision_id="0" * 64)
    with pytest.raises(ValueError, match="not canonical"):
        store.record(invalid, recorded_at=211)
    with sqlite3.connect(database) as con:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute(
                "UPDATE capability_shadow_decisions SET outcome = 'same'"
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("DELETE FROM capability_shadow_decisions")


def test_shadow_operator_counts_group_by_actual_candidate_and_exact_scope(tmp_path):
    database = tmp_path / "shadow-counts.db"
    enrollment, snapshot = _register(database, "worker-a")
    scope_a = _resolved_scope(
        CapabilityEvidenceStore(database),
        _attempt(enrollment, snapshot, suffix="scope"),
    )
    scope_b = replace(scope_a, enrollment_id="enrollment-shadow-b")
    store = CapabilityShadowDecisionStore(database)

    no_preference = evaluate_shadow_preference(
        actual_attempt_id="attempt-shadow-1",
        actual_candidate_id="node-a",
        candidates=(
            EligibleShadowCandidate(
                "node-a",
                _shadow_aggregate(
                    scope_a, deadline_samples=4, deadline_low=0.5
                ),
            ),
            EligibleShadowCandidate(
                "node-b",
                _shadow_aggregate(
                    scope_b, deadline_samples=5, deadline_low=0.6
                ),
            ),
        ),
        minimum_samples=5,
        decision_at=200,
    )
    same = evaluate_shadow_preference(
        actual_attempt_id="attempt-shadow-2",
        actual_candidate_id="node-a",
        candidates=(
            EligibleShadowCandidate(
                "node-a",
                _shadow_aggregate(
                    scope_a, deadline_samples=5, deadline_low=0.7
                ),
            ),
            EligibleShadowCandidate(
                "node-b",
                _shadow_aggregate(
                    scope_b, deadline_samples=5, deadline_low=0.6
                ),
            ),
        ),
        minimum_samples=5,
        decision_at=201,
    )
    different = evaluate_shadow_preference(
        actual_attempt_id="attempt-shadow-3",
        actual_candidate_id="node-a",
        candidates=(
            EligibleShadowCandidate(
                "node-a",
                _shadow_aggregate(
                    scope_a, deadline_samples=5, deadline_low=0.5
                ),
            ),
            EligibleShadowCandidate(
                "node-b",
                _shadow_aggregate(
                    scope_b, deadline_samples=5, deadline_low=0.7
                ),
            ),
        ),
        minimum_samples=5,
        decision_at=202,
    )
    for evaluation in (no_preference, same, different):
        store.record(evaluation, recorded_at=evaluation.decision_at + 1)
    newer_other_scope = evaluate_shadow_preference(
        actual_attempt_id="attempt-shadow-other-scope",
        actual_candidate_id="node-b",
        candidates=(
            EligibleShadowCandidate(
                "node-b",
                _shadow_aggregate(
                    scope_b, deadline_samples=5, deadline_low=0.6
                ),
            ),
        ),
        minimum_samples=5,
        decision_at=300,
    )
    store.record(newer_other_scope, recorded_at=301)

    summaries = store.aggregate_counts(
        actual_candidate_id="node-a", actual_scope_key=scope_a.scope_key
    )

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.actual_candidate_id == "node-a"
    assert summary.actual_scope_key == scope_a.scope_key
    assert summary.decision_count == 3
    assert summary.same_count == 1
    assert summary.different_count == 1
    assert summary.no_preference_count == 1
    assert summary.last_decision_at == 202
    assert store.aggregate_counts(limit=1)[0].actual_scope_key == scope_b.scope_key
    assert store.aggregate_counts_for_scope_keys([scope_a.scope_key]) == summaries
    assert set(summary.__dataclass_fields__) == {
        "actual_candidate_id",
        "actual_scope_key",
        "decision_count",
        "same_count",
        "different_count",
        "no_preference_count",
        "last_decision_at",
    }
    assert store.aggregate_counts(actual_candidate_id="node-missing") == ()
    with pytest.raises(ValueError, match="1 and 200"):
        store.aggregate_counts(limit=201)
    with pytest.raises(ValueError, match="SHA-256"):
        store.aggregate_counts(actual_scope_key="not-a-scope")


def test_aggregate_surface_has_no_global_score_or_routing_label():
    forbidden = {
        "score",
        "reputation",
        "routing_weight",
        "trusted_for_routing",
        "rank",
    }
    fields = set(ScopeAggregate.__dataclass_fields__)
    assert fields.isdisjoint(forbidden)
    assert math.isclose(_binary(10, 7, 0.4).rate or 0, 0.7)
