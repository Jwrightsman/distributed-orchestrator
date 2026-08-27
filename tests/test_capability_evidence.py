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
    CapabilityShadowOperationalStore,
    DEADLINE_COMPLETION_SUBJECT,
    EligibleShadowCandidate,
    EvidenceConflict,
    EvidenceScope,
    ScopeAggregate,
    ShadowDecisionConflict,
    ShadowOperationalEventConflict,
    ShadowOperationalProcessCounters,
    evaluate_shadow_preference,
    future_active_experiment_eligibility,
)
from node_capabilities import (
    LEGACY_DESCRIPTOR_HASH,
    NodeCapabilityDescriptorV1,
    NodeCapabilitySnapshotStore,
)
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


def _future_experiment_scope(**changes) -> EvidenceScope:
    scope = EvidenceScope(
        enrollment_id="enrollment-identity",
        descriptor_version="1",
        descriptor_hash="c" * 64,
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
    return replace(scope, **changes)


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


def test_only_timely_settled_output_passes_deadline_completion(tmp_path):
    database = tmp_path / "settlement-deadline-outcomes.db"
    enrollment, snapshot = _register(database, "worker-a")
    store = CapabilityEvidenceStore(database)
    attempts = []

    for index, (terminal_cause, accepted_at, expected_deadline) in enumerate(
        (
            ("settled_output", 110, "pass"),
            ("settled_output", 201, "fail"),
            ("settled_worker_error", 110, "fail"),
            ("settled_empty_output", 110, "fail"),
        ),
        start=1,
    ):
        attempt = _attempt(
            enrollment,
            snapshot,
            suffix=f"deadline-{index}-{terminal_cause}",
            terminal_cause=terminal_cause,
            settled_at=accepted_at,
        )
        attempts.append(attempt)
        result = store.record_settlement(
            attempt,
            accepted_at=accepted_at,
            output_bytes=10 if terminal_cause == "settled_output" else 0,
            recorded_at=accepted_at + 10,
        )

        deadline = next(
            observation
            for observation in result.observations
            if observation.observation_type == "deadline_completion"
        )
        assert deadline.subject_key == DEADLINE_COMPLETION_SUBJECT
        assert deadline.outcome == expected_deadline

    aggregate = store.aggregate(
        _resolved_scope(store, attempts[0]), minimum_samples=4
    )
    assert aggregate.deadline_completion.sample_count == 4
    assert aggregate.deadline_completion.positive_count == 1
    assert aggregate.deadline_completion.negative_count == 3


def test_read_only_aggregate_uses_initialized_schema_without_migration(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "read-only-aggregate.db"
    enrollment, snapshot = _register(database, "worker-a")
    store = CapabilityEvidenceStore(database)
    attempt = _attempt(
        enrollment,
        snapshot,
        suffix="read-only-aggregate",
        terminal_cause="settled_output",
        settled_at=110,
    )
    store.record_settlement(
        attempt,
        accepted_at=110,
        output_bytes=10,
        recorded_at=120,
    )
    scope = _resolved_scope(store, attempt)
    expected = store.aggregate(scope, minimum_samples=1, recorded_before=120)

    def forbidden_migration():
        raise AssertionError("read-only aggregation must not migrate")

    monkeypatch.setattr(store, "migrate", forbidden_migration)

    assert store.aggregate_read_only(
        scope,
        minimum_samples=1,
        recorded_before=120,
    ) == expected


def test_legacy_deadline_row_is_backfilled_by_v2_without_conflict_or_double_count(
    tmp_path,
):
    database = tmp_path / "deadline-subject-upgrade.db"
    enrollment, snapshot = _register(database, "worker-a")
    store = CapabilityEvidenceStore(database)
    attempt = _attempt(
        enrollment,
        snapshot,
        suffix="deadline-upgrade",
        terminal_cause="settled_worker_error",
        settled_at=110,
    )

    store.migrate()
    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        resolution = store.resolve_scope_in_transaction(con, attempt)
        assert resolution.context is not None
        legacy = store._append_one(
            con,
            resolution.context,
            observation_type="deadline_completion",
            subject_key="lifecycle",
            outcome="pass",
            numeric_value=None,
            metadata=None,
            observed_at=110,
            recorded_at=120,
        )
        con.commit()

    backfill = store.record_settlement(
        attempt,
        accepted_at=110,
        output_bytes=0,
        recorded_at=121,
    )
    replay = store.record_settlement(
        attempt,
        accepted_at=110,
        output_bytes=0,
        recorded_at=999,
    )
    v2 = next(
        observation
        for observation in backfill.observations
        if observation.observation_type == "deadline_completion"
    )

    assert legacy.subject_key == "lifecycle"
    assert legacy.outcome == "pass"
    assert v2.subject_key == DEADLINE_COMPLETION_SUBJECT
    assert v2.outcome == "fail"
    assert v2.observation_id != legacy.observation_id
    assert [item.observation_id for item in replay.observations] == [
        item.observation_id for item in backfill.observations
    ]

    with sqlite3.connect(database) as con:
        deadline_rows = con.execute(
            "SELECT subject_key, outcome FROM node_capability_observations "
            "WHERE attempt_id = ? AND observation_type = 'deadline_completion' "
            "ORDER BY subject_key",
            (attempt["attempt_id"],),
        ).fetchall()
    assert deadline_rows == [
        ("lifecycle", "pass"),
        (DEADLINE_COMPLETION_SUBJECT, "fail"),
    ]

    aggregate = store.aggregate(_resolved_scope(store, attempt), minimum_samples=1)
    assert aggregate.deadline_completion.sample_count == 1
    assert aggregate.deadline_completion.positive_count == 0
    assert aggregate.deadline_completion.negative_count == 1
    assert aggregate.observation_count == len(backfill.observations)


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


def test_descriptor_snapshot_change_starts_a_new_durable_scope(tmp_path):
    database = tmp_path / "descriptor-scope-reset.db"
    enrollment, first_snapshot = _register(database, "worker-a")
    second_snapshot = NodeCapabilitySnapshotStore(database).remember(
        enrollment.enrollment_id,
        _descriptor(executor_version="0.12.0"),
        now=3,
    )
    store = CapabilityEvidenceStore(database)
    first_attempt = _attempt(
        enrollment,
        first_snapshot,
        suffix="descriptor-first",
    )
    second_attempt = _attempt(
        enrollment,
        second_snapshot,
        suffix="descriptor-second",
    )

    store.record_settlement(
        first_attempt, accepted_at=110, output_bytes=10, recorded_at=120
    )
    store.record_settlement(
        second_attempt, accepted_at=110, output_bytes=10, recorded_at=121
    )

    first_scope = _resolved_scope(store, first_attempt)
    second_scope = _resolved_scope(store, second_attempt)
    assert first_scope.enrollment_id == second_scope.enrollment_id
    assert first_scope.descriptor_hash != second_scope.descriptor_hash
    assert first_scope.scope_key != second_scope.scope_key
    assert store.aggregate(
        first_scope, minimum_samples=2
    ).deadline_completion.sample_count == 1
    assert store.aggregate(
        second_scope, minimum_samples=2
    ).deadline_completion.sample_count == 1

    reopened = CapabilityEvidenceStore(database)
    summaries = reopened.list_scope_aggregates(
        enrollment_id=enrollment.enrollment_id,
        minimum_samples=2,
    )
    assert len(summaries) == 2
    assert {item.scope.descriptor_hash for item in summaries} == {
        first_snapshot.descriptor_hash,
        second_snapshot.descriptor_hash,
    }
    assert {item.aggregate.settlement_count for item in summaries} == {1}
    assert all(item.aggregate.insufficient_evidence for item in summaries)
    with sqlite3.connect(database) as con:
        grouped = con.execute(
            "SELECT descriptor_hash, COUNT(*) "
            "FROM node_capability_observations GROUP BY descriptor_hash"
        ).fetchall()
    assert set(grouped) == {
        (first_snapshot.descriptor_hash, 5),
        (second_snapshot.descriptor_hash, 5),
    }


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


def test_shadow_operational_store_is_replay_safe_append_only_and_restart_safe(
    tmp_path,
):
    database = tmp_path / "shadow-operational.db"
    store = CapabilityShadowOperationalStore(database)

    first = store.record(
        attempt_id="attempt-operational",
        phase="admission",
        outcome="scheduled",
        reason_code="evaluation_scheduled",
        occurred_at=10,
    )
    replay = store.record(
        attempt_id="attempt-operational",
        phase="admission",
        outcome="scheduled",
        reason_code="evaluation_scheduled",
        occurred_at=999,
    )

    assert replay == first
    assert replay.occurred_at == 10
    with pytest.raises(ShadowOperationalEventConflict):
        store.record(
            attempt_id="attempt-operational",
            phase="admission",
            outcome="queue_saturated",
            reason_code="background_queue_limit_reached",
            occurred_at=11,
        )

    private_exception = "PRIVATE WORKER ERROR MUST NOT PERSIST"
    with pytest.raises(ValueError, match="reason_code"):
        store.record(
            attempt_id="attempt-private",
            phase="evaluation",
            outcome="evaluator_failed",
            reason_code=private_exception,
            occurred_at=12,
        )

    restarted = CapabilityShadowOperationalStore(database)
    assert restarted.get(first.event_id) == first
    assert restarted.get_for_attempt_phase("attempt-operational", "admission") == first
    report = restarted.report()
    assert report.admission_counts["scheduled"] == 1
    assert report.assignment_observation_total == 1
    assert report.offered_total == report.drop_failure_denominator == 1
    assert report.drop_failure_numerator == report.failed_total == 0
    assert report.drop_failure_rate == 0.0
    assert report.pending_total == 1

    expected_columns = {
        "event_id",
        "attempt_id",
        "phase",
        "outcome",
        "reason_code",
        "occurred_at",
    }
    assert set(asdict(first)) == expected_columns
    with sqlite3.connect(database) as con:
        columns = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info(capability_shadow_operational_events)"
            )
        }
        row_count = con.execute(
            "SELECT COUNT(*) FROM capability_shadow_operational_events"
        ).fetchone()[0]
        assert columns == expected_columns
        assert row_count == 1
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute(
                "UPDATE capability_shadow_operational_events "
                "SET occurred_at = 20"
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("DELETE FROM capability_shadow_operational_events")
    assert private_exception.encode("utf-8") not in database.read_bytes()


def test_shadow_operational_report_uses_admission_cohort_and_reproducible_math(
    tmp_path,
):
    store = CapabilityShadowOperationalStore(tmp_path / "shadow-report.db")

    def record(attempt_id, phase, outcome, reason_code, occurred_at):
        store.record(
            attempt_id=attempt_id,
            phase=phase,
            outcome=outcome,
            reason_code=reason_code,
            occurred_at=occurred_at,
        )

    record(
        "attempt-before",
        "admission",
        "queue_saturated",
        "background_queue_limit_reached",
        9,
    )
    record("attempt-disabled", "admission", "disabled", "mode_disabled", 10)
    record(
        "attempt-not-applicable",
        "admission",
        "not_applicable",
        "nonproduction_attempt",
        11,
    )
    record(
        "attempt-queue",
        "admission",
        "queue_saturated",
        "background_queue_limit_reached",
        12,
    )
    record(
        "attempt-scope",
        "admission",
        "scope_capture_failed",
        "scope_capture_failed",
        13,
    )
    for attempt_id, admitted_at in (
        ("attempt-completed", 14),
        ("attempt-evaluator", 15),
        ("attempt-write", 16),
        ("attempt-cancelled", 17),
        ("attempt-pending", 20),
    ):
        record(
            attempt_id,
            "admission",
            "scheduled",
            "evaluation_scheduled",
            admitted_at,
        )
    record(
        "attempt-completed",
        "evaluation",
        "completed",
        "decision_persisted",
        100,
    )
    record(
        "attempt-evaluator",
        "evaluation",
        "evaluator_failed",
        "evaluator_failed",
        101,
    )
    record(
        "attempt-write",
        "evaluation",
        "decision_write_failed",
        "decision_write_failed",
        102,
    )
    record(
        "attempt-cancelled",
        "evaluation",
        "cancelled_on_shutdown",
        "coordinator_shutdown",
        103,
    )
    record(
        "attempt-after",
        "admission",
        "scheduled",
        "evaluation_scheduled",
        21,
    )
    record(
        "attempt-after",
        "evaluation",
        "completed",
        "decision_persisted",
        200,
    )

    report = store.report(window_started_at=10, window_ended_at=20)

    assert report.admission_counts == {
        "disabled": 1,
        "not_applicable": 1,
        "queue_saturated": 1,
        "scope_capture_failed": 1,
        "scheduled": 5,
    }
    assert report.evaluation_counts == {
        "completed": 1,
        "evaluator_failed": 1,
        "decision_write_failed": 1,
        "cancelled_on_shutdown": 1,
    }
    assert report.orphan_evaluation_total == 0
    assert report.assignment_observation_total == 9
    assert report.offered_total == report.drop_failure_denominator == 7
    assert report.scheduled_total == 5
    assert report.completed_total == 1
    assert report.skipped_total == 2
    assert report.failed_total == report.drop_failure_numerator == 5
    assert report.pending_total == 1
    assert report.drop_failure_rate == pytest.approx(5 / 7)
    assert report.latest_event_at == 103
    assert report.window_started_at == 10
    assert report.window_ended_at == 20


def test_shadow_operational_report_includes_evaluation_when_admission_write_is_missing(
    tmp_path,
):
    store = CapabilityShadowOperationalStore(tmp_path / "shadow-orphan-report.db")
    store.record(
        attempt_id="attempt-orphan-evaluation",
        phase="evaluation",
        outcome="evaluator_failed",
        reason_code="evaluator_failed",
        occurred_at=15,
    )

    report = store.report(window_started_at=10, window_ended_at=20)

    assert report.admission_counts == {
        "disabled": 0,
        "not_applicable": 0,
        "queue_saturated": 0,
        "scope_capture_failed": 0,
        "scheduled": 0,
    }
    assert report.evaluation_counts == {
        "completed": 0,
        "evaluator_failed": 1,
        "decision_write_failed": 0,
        "cancelled_on_shutdown": 0,
    }
    assert report.orphan_evaluation_total == 1
    assert report.assignment_observation_total == 1
    assert report.scheduled_total == 1
    assert report.offered_total == report.drop_failure_denominator == 1
    assert report.failed_total == report.drop_failure_numerator == 1
    assert report.pending_total == 0
    assert report.drop_failure_rate == 1.0
    assert report.latest_event_at == 15


def test_shadow_operational_process_counters_increment_and_reset():
    counters = ShadowOperationalProcessCounters(reset_at=10)

    assert counters.increment("durable_health_record_write_failure") == 1
    assert counters.increment("durable_health_record_write_failure") == 2
    assert counters.increment("unexpected_containment_failure") == 1
    assert counters.increment("background_task_callback_failure") == 1
    assert asdict(counters.snapshot()) == {
        "reset_at": 10.0,
        "durable_health_record_write_failure": 2,
        "unexpected_containment_failure": 1,
        "background_task_callback_failure": 1,
    }
    with pytest.raises(ValueError, match="unsupported"):
        counters.increment("not-a-counter")

    counters.reset(reset_at=20)
    assert asdict(counters.snapshot()) == {
        "reset_at": 20.0,
        "durable_health_record_write_failure": 0,
        "unexpected_containment_failure": 0,
        "background_task_callback_failure": 0,
    }


def test_future_active_experiment_identity_accepts_digest_and_blocks_missing_digest():
    exact = future_active_experiment_eligibility(_future_experiment_scope())
    missing = future_active_experiment_eligibility(
        _future_experiment_scope(model_digest=None)
    )

    assert exact.eligible_for_future_active_experiment is True
    assert exact.blocking_reasons == ()
    assert exact.as_dict() == {
        "eligible_for_future_active_experiment": True,
        "blocking_reasons": [],
        "meaning": (
            "identity_prerequisites_only_not_correctness_reputation_trust_"
            "or_active_routing"
        ),
    }
    assert missing.eligible_for_future_active_experiment is False
    assert missing.blocking_reasons == ("immutable_model_identity_missing",)


def test_future_active_experiment_identity_blocks_legacy_and_unreconstructable_flags():
    legacy = future_active_experiment_eligibility(
        _future_experiment_scope(descriptor_hash=LEGACY_DESCRIPTOR_HASH)
    )
    unreconstructable = future_active_experiment_eligibility(
        _future_experiment_scope(),
        descriptor_identity_reconstructable=False,
        model_identity_reconstructable=False,
    )

    assert legacy.eligible_for_future_active_experiment is False
    assert legacy.blocking_reasons == ("legacy_descriptor_identity",)
    assert unreconstructable.eligible_for_future_active_experiment is False
    assert unreconstructable.blocking_reasons == (
        "descriptor_identity_unreconstructable",
        "model_identity_unreconstructable",
    )


def test_schema_migration_is_idempotent_and_preserves_all_evidence_state(tmp_path):
    database = tmp_path / "evidence-migration.db"
    enrollment, snapshot = _register(database, "worker-a")
    evidence_store = CapabilityEvidenceStore(database)
    shadow_store = CapabilityShadowDecisionStore(database)

    for _ in range(2):
        evidence_store.migrate()
        shadow_store.migrate()

    attempt = _attempt(enrollment, snapshot, suffix="migration")
    settlement = evidence_store.record_settlement(
        attempt, accepted_at=110, output_bytes=10, recorded_at=120
    )
    projection = evidence_store.record_contract_floor_projection(
        execution_id=attempt["execution_id"],
        projections=((attempt, True),),
        method_version="validator-v1",
        recorded_at=121,
    )
    scope = _resolved_scope(evidence_store, attempt)
    evaluation = evaluate_shadow_preference(
        actual_attempt_id=attempt["attempt_id"],
        actual_candidate_id="worker-a",
        candidates=(
            EligibleShadowCandidate(
                "worker-a",
                _shadow_aggregate(
                    scope, deadline_samples=5, deadline_low=0.5
                ),
            ),
        ),
        minimum_samples=5,
        decision_at=122,
    )
    shadow = shadow_store.record(evaluation, recorded_at=123)

    table_names = {
        "node_capability_observations",
        "capability_evidence_projection_receipts",
        "capability_shadow_decisions",
    }
    trigger_names = {
        "trg_capability_observations_no_update",
        "trg_capability_observations_no_delete",
        "trg_capability_evidence_projections_no_update",
        "trg_capability_evidence_projections_no_delete",
        "trg_capability_shadow_decisions_no_update",
        "trg_capability_shadow_decisions_no_delete",
    }

    def persisted_state():
        with sqlite3.connect(database) as con:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            triggers = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }
            observations = con.execute(
                "SELECT observation_id, observation_type, outcome "
                "FROM node_capability_observations ORDER BY observation_id"
            ).fetchall()
            projections = con.execute(
                "SELECT execution_id, projection_version, source_digest "
                "FROM capability_evidence_projection_receipts"
            ).fetchall()
            decisions = con.execute(
                "SELECT decision_id, outcome, actual_scope_key "
                "FROM capability_shadow_decisions"
            ).fetchall()
        return tables, triggers, observations, projections, decisions

    before = persisted_state()
    assert table_names <= before[0]
    assert trigger_names <= before[1]
    assert len(before[2]) == len(settlement.observations) + len(
        projection.observations
    )
    assert before[3] == [
        (attempt["execution_id"], "contract-floor-v1", projection.source_digest)
    ]
    assert before[4] == [
        (shadow.evaluation.decision_id, "no_preference", scope.scope_key)
    ]

    for _ in range(3):
        CapabilityEvidenceStore(database).migrate()
        CapabilityShadowDecisionStore(database).migrate()

    assert persisted_state() == before


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
