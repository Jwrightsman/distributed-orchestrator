"""Durable attempt authority, receipt broker, and quarantine invariants."""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from capability_evidence import CapabilityEvidenceStore
from execution.attempts import (
    AcceptedResultBroker,
    AttemptConflict,
    AttemptRejected,
    AttemptStore,
    ReceiptBindingError,
    WorkerPayloadLimitExceeded,
)
from node_capabilities import (
    NodeCapabilityDescriptorV1,
    NodeCapabilitySnapshotStore,
    capability_descriptor_digest,
)
from node_enrollments import ensure_node_enrollment_schema


EXECUTION_ID = "e" * 32
TEST_DESCRIPTOR = NodeCapabilityDescriptorV1.model_validate(
    {
        "executor": {"kind": "ollama", "worker_protocol_version": "1"},
        "models": [{"provider": "ollama", "name": "qwen3.5:4b"}],
        "hardware": {
            "architecture": "x86_64",
            "logical_cpu_count": 4,
            "total_memory_bytes": 8 * 1024**3,
        },
        "features": ["code"],
        "limits": {
            "max_concurrent_execution_units": 1,
            "max_output_bytes": 1_048_576,
        },
        "isolation": {"kind": "none"},
    }
)
TEST_DESCRIPTOR_HASH = capability_descriptor_digest(TEST_DESCRIPTOR)
MODEL_DIGEST_A = f"sha256:{'a' * 64}"
MODEL_DIGEST_B = f"sha256:{'b' * 64}"


def _multi_model_descriptor() -> NodeCapabilityDescriptorV1:
    return NodeCapabilityDescriptorV1.model_validate(
        {
            "executor": {"kind": "ollama", "worker_protocol_version": "1"},
            "models": [
                {
                    "provider": "ollama",
                    "name": "model-a:latest",
                    "digest": MODEL_DIGEST_A,
                },
                {
                    "provider": "ollama",
                    "name": "model-b:latest",
                    "digest": MODEL_DIGEST_B,
                },
            ],
            "hardware": {
                "architecture": "x86_64",
                "logical_cpu_count": 4,
                "total_memory_bytes": 8 * 1024**3,
            },
            "features": ["code"],
            "limits": {
                "max_concurrent_execution_units": 1,
                "max_output_bytes": 1_048_576,
            },
            "isolation": {"kind": "none"},
        }
    )


def _task(task_id: str = "task-1") -> dict:
    return {
        "task_id": task_id,
        "contract_version": "1",
        "execution_id": EXECUTION_ID,
        "execution_unit_id": "candidate-1",
        "execution_unit_kind": "candidate",
        "selected_model": {
            "provider": "ollama",
            "name": "qwen3.5:4b",
            "digest": None,
        },
        "evidence_role": "production",
    }


def _issue(store: AttemptStore, task_id: str = "task-1") -> tuple[dict, str, str]:
    task = _task(task_id)
    attempt_id = f"attempt-{task_id}"
    nonce = f"nonce-{task_id}-unguessable"
    now = time.time()
    store.issue(
        task,
        assigned_node_id="worker",
        attempt_id=attempt_id,
        nonce=nonce,
        issued_at=now,
        lease_expires_at=now + 60,
    )
    return task, attempt_id, nonce


def _submission(task: dict, attempt_id: str, nonce: str, **overrides) -> dict:
    value = {
        "task_id": task["task_id"],
        "node_id": "worker",
        "output": "complete output",
        "error": None,
        "elapsed_seconds": 1.0,
        "contract_version": "1",
        "attempt_id": attempt_id,
        "nonce": nonce,
        "execution_id": task["execution_id"],
        "execution_unit_id": task["execution_unit_id"],
        "execution_unit_kind": task["execution_unit_kind"],
    }
    value.update(overrides)
    return value


def _enroll(
    path,
    enrollment_id: str,
    node_id: str,
    *,
    credential_version: int = 1,
    status: str = "active",
) -> None:
    with sqlite3.connect(path) as con:
        ensure_node_enrollment_schema(con)
        con.execute(
            """
            INSERT INTO node_enrollments (
                enrollment_id, node_id, credential_digest, status,
                credential_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                enrollment_id,
                node_id,
                f"digest:{enrollment_id}",
                status,
                credential_version,
                time.time(),
            ),
        )
        con.commit()
    NodeCapabilitySnapshotStore(path).remember(enrollment_id, TEST_DESCRIPTOR)


def _issue_enrolled(
    store: AttemptStore,
    *,
    enrollment_id: str = "enrollment-a",
    node_id: str = "worker",
    session_id: str = "session-a",
    credential_version: int = 1,
    task_id: str = "task-enrolled",
) -> tuple[dict, str, str]:
    task = _task(task_id)
    attempt_id = f"attempt-{task_id}"
    nonce = f"nonce-{task_id}-unguessable"
    now = time.time()
    store.issue(
        task,
        assigned_node_id=node_id,
        assigned_enrollment_id=enrollment_id,
        assigned_credential_version=credential_version,
        assigned_session_id=session_id,
        assigned_descriptor_version=TEST_DESCRIPTOR.descriptor_version,
        assigned_descriptor_hash=TEST_DESCRIPTOR_HASH,
        attempt_id=attempt_id,
        nonce=nonce,
        issued_at=now,
        lease_expires_at=now + 60,
    )
    return task, attempt_id, nonce


def test_enrolled_attempt_requires_a_valid_descriptor_snapshot_binding(tmp_path):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker")
    store = AttemptStore(path)
    now = time.time()

    with pytest.raises(ValueError, match="descriptor snapshot binding"):
        store.issue(
            _task("missing-descriptor"),
            assigned_node_id="worker",
            assigned_enrollment_id="enrollment-a",
            assigned_credential_version=1,
            assigned_session_id="session-a",
            attempt_id="attempt-missing-descriptor",
            nonce="nonce-missing-descriptor",
            issued_at=now,
            lease_expires_at=now + 60,
        )

    with pytest.raises(AttemptConflict, match="snapshot is missing"):
        store.issue(
            _task("unknown-descriptor"),
            assigned_node_id="worker",
            assigned_enrollment_id="enrollment-a",
            assigned_credential_version=1,
            assigned_session_id="session-a",
            assigned_descriptor_version="1",
            assigned_descriptor_hash="f" * 64,
            attempt_id="attempt-unknown-descriptor",
            nonce="nonce-unknown-descriptor",
            issued_at=now,
            lease_expires_at=now + 60,
        )


def test_enrolled_attempt_binds_exact_advertised_model_and_evidence_role(tmp_path):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker")
    descriptor = _multi_model_descriptor()
    descriptor_hash = capability_descriptor_digest(descriptor)
    NodeCapabilitySnapshotStore(path).remember("enrollment-a", descriptor)
    store = AttemptStore(path)
    now = time.time()
    primary_task = _task("comparison-primary")
    primary_task["selected_model"] = {
        "provider": "ollama",
        "name": "model-a:latest",
        "digest": MODEL_DIGEST_A,
    }
    primary = store.issue(
        primary_task,
        assigned_node_id="worker",
        assigned_enrollment_id="enrollment-a",
        assigned_credential_version=1,
        assigned_session_id="session-a",
        assigned_descriptor_version=descriptor.descriptor_version,
        assigned_descriptor_hash=descriptor_hash,
        attempt_id="attempt-comparison-primary",
        nonce="nonce-comparison-primary-unguessable",
        issued_at=now,
        lease_expires_at=now + 60,
    )
    task = _task("exact-model")
    task["selected_model"] = {
        "provider": "ollama",
        "name": "model-b:latest",
        "digest": MODEL_DIGEST_B,
    }
    task["evidence_role"] = "sampled_comparison"
    task["comparison_primary_attempt_id"] = primary.attempt_id

    record = store.issue(
        task,
        assigned_node_id="worker",
        assigned_enrollment_id="enrollment-a",
        assigned_credential_version=1,
        assigned_session_id="session-a",
        assigned_descriptor_version=descriptor.descriptor_version,
        assigned_descriptor_hash=descriptor_hash,
        attempt_id="attempt-exact-model",
        nonce="nonce-exact-model-unguessable",
        issued_at=now,
        lease_expires_at=now + 60,
    )

    assert record.assigned_model_provider == "ollama"
    assert record.assigned_model_name == "model-b:latest"
    assert record.assigned_model_digest == MODEL_DIGEST_B
    assert record.evidence_role == "sampled_comparison"
    assert record.comparison_primary_attempt_id == primary.attempt_id
    assert record.terminal_cause is None

    receipt = store.settle(
        **_submission(task, record.attempt_id, "nonce-exact-model-unguessable"),
        session_id="session-a",
        enrollment_id="enrollment-a",
        credential_version=1,
    ).receipt
    assert receipt.assigned_model_provider == record.assigned_model_provider
    assert receipt.assigned_model_name == record.assigned_model_name
    assert receipt.assigned_model_digest == record.assigned_model_digest
    assert receipt.evidence_role == record.evidence_role


@pytest.mark.parametrize(
    "selected_model, error_type, message",
    [
        (None, ValueError, "requires a selected_model"),
        (
            {
                "provider": "ollama",
                "name": "model-b:latest",
                "digest": MODEL_DIGEST_A,
            },
            AttemptConflict,
            "not uniquely advertised",
        ),
        (
            {
                "provider": "ollama",
                "name": "model-b:latest",
                "digest": MODEL_DIGEST_B,
                "context_tokens": 4096,
            },
            ValueError,
            "exactly provider, name, and digest",
        ),
    ],
)
def test_enrolled_attempt_rejects_unbound_or_malformed_selected_model(
    tmp_path, selected_model, error_type, message
):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker")
    descriptor = _multi_model_descriptor()
    descriptor_hash = capability_descriptor_digest(descriptor)
    NodeCapabilitySnapshotStore(path).remember("enrollment-a", descriptor)
    task = _task("invalid-selected-model")
    if selected_model is None:
        task.pop("selected_model")
    else:
        task["selected_model"] = selected_model
    now = time.time()

    with pytest.raises(error_type, match=message):
        AttemptStore(path).issue(
            task,
            assigned_node_id="worker",
            assigned_enrollment_id="enrollment-a",
            assigned_credential_version=1,
            assigned_session_id="session-a",
            assigned_descriptor_version=descriptor.descriptor_version,
            assigned_descriptor_hash=descriptor_hash,
            attempt_id="attempt-invalid-selected-model",
            nonce="nonce-invalid-selected-model",
            issued_at=now,
            lease_expires_at=now + 60,
        )

    assert AttemptStore(path).get("attempt-invalid-selected-model") is None


def test_enrolled_attempt_rejects_unknown_evidence_role(tmp_path):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker")
    task = _task("invalid-evidence-role")
    task["evidence_role"] = "benchmark"
    now = time.time()

    with pytest.raises(ValueError, match="evidence_role"):
        AttemptStore(path).issue(
            task,
            assigned_node_id="worker",
            assigned_enrollment_id="enrollment-a",
            assigned_credential_version=1,
            assigned_session_id="session-a",
            assigned_descriptor_version=TEST_DESCRIPTOR.descriptor_version,
            assigned_descriptor_hash=TEST_DESCRIPTOR_HASH,
            attempt_id="attempt-invalid-evidence-role",
            nonce="nonce-invalid-evidence-role",
            issued_at=now,
            lease_expires_at=now + 60,
        )


def test_attempt_issue_rejects_injected_requirement_identity(tmp_path):
    store = AttemptStore(tmp_path / "attempts.db")
    now = time.time()
    task = _task("injected-requirements")
    task["requires"] = ["code"]
    task["requirement_digest"] = "f" * 64

    with pytest.raises(ValueError, match="canonical task requirements"):
        store.issue(
            task,
            assigned_node_id="legacy-worker",
            attempt_id="attempt-injected-requirements",
            nonce="nonce-injected-requirements",
            issued_at=now,
            lease_expires_at=now + 60,
        )

    assert store.get("attempt-injected-requirements") is None


def test_migration_stores_nonce_digest_not_raw_secret(tmp_path):
    path = tmp_path / "attempts.db"
    store = AttemptStore(path)
    _task_value, attempt_id, nonce = _issue(store)

    with sqlite3.connect(path) as con:
        row = con.execute(
            "SELECT nonce_digest FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()

    assert row and row[0] != nonce
    assert nonce not in path.read_bytes().decode("utf-8", errors="ignore")
    attempt = store.get(attempt_id)
    assert attempt.assigned_model_provider is None
    assert attempt.assigned_model_name is None
    assert attempt.assigned_model_digest is None
    assert attempt.evidence_role is None
    assert attempt.comparison_primary_attempt_id is None


def test_additive_migration_keeps_historical_attempt_and_receipt_unattributed(tmp_path):
    path = tmp_path / "historical.db"
    with sqlite3.connect(path) as con:
        con.executescript(
            """
            CREATE TABLE attempts (
                attempt_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                execution_id TEXT,
                execution_unit_id TEXT,
                execution_unit_kind TEXT,
                assigned_node_id TEXT NOT NULL,
                assigned_session_id TEXT,
                contract_version TEXT,
                nonce_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                issued_at REAL NOT NULL,
                lease_expires_at REAL NOT NULL,
                max_output_bytes INTEGER NOT NULL DEFAULT 1048576,
                streamed_bytes INTEGER NOT NULL DEFAULT 0,
                stream_batch_count INTEGER NOT NULL DEFAULT 0,
                first_stream_at REAL,
                last_stream_at REAL,
                stream_closed INTEGER NOT NULL DEFAULT 0,
                stream_limit_event_emitted INTEGER NOT NULL DEFAULT 0,
                stream_rate_window_started_at REAL,
                stream_rate_window_batch_count INTEGER NOT NULL DEFAULT 0,
                settled_at REAL,
                result_hash TEXT,
                response_json TEXT,
                reason TEXT
            );
            CREATE TABLE accepted_result_receipts (
                attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
                task_id TEXT NOT NULL UNIQUE,
                execution_id TEXT,
                execution_unit_id TEXT,
                execution_unit_kind TEXT,
                assigned_node_id TEXT NOT NULL,
                contract_version TEXT,
                result_hash TEXT NOT NULL,
                accepted_at REAL NOT NULL,
                output TEXT,
                error TEXT,
                elapsed_seconds REAL NOT NULL
            );
            CREATE TABLE result_quarantine (
                quarantine_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                claimed_attempt_id TEXT,
                claimed_node_id TEXT NOT NULL,
                claimed_execution_id TEXT,
                claimed_unit_id TEXT,
                claimed_unit_kind TEXT,
                claimed_contract_version TEXT,
                reason TEXT NOT NULL,
                output_sha256 TEXT,
                output_preview TEXT,
                error TEXT,
                received_at REAL NOT NULL
            );
            """
        )
        con.execute(
            """
            INSERT INTO attempts (
                attempt_id, task_id, assigned_node_id, assigned_session_id,
                nonce_digest, state, issued_at, lease_expires_at, settled_at,
                result_hash, response_json
            ) VALUES ('historical-attempt', 'historical-task', 'old-label',
                      'old-session', 'digest', 'settled', 1, 2, 1.5,
                      'result-hash', '{"status":"accepted","credits_earned":5}')
            """
        )
        con.execute(
            """
            INSERT INTO accepted_result_receipts (
                attempt_id, task_id, assigned_node_id, result_hash,
                accepted_at, output, elapsed_seconds
            ) VALUES ('historical-attempt', 'historical-task', 'old-label',
                      'result-hash', 1.5, 'old output', 1)
            """
        )
        con.commit()

    store = AttemptStore(path)
    store.migrate()
    store.migrate()

    attempt = store.get("historical-attempt")
    receipt = store.get_receipt_for_task("historical-task")
    assert attempt is not None
    assert attempt.assigned_enrollment_id is None
    assert attempt.assigned_credential_version is None
    assert attempt.assigned_descriptor_version is None
    assert attempt.assigned_descriptor_hash is None
    assert attempt.assigned_model_provider is None
    assert attempt.assigned_model_name is None
    assert attempt.assigned_model_digest is None
    assert attempt.evidence_role is None
    assert attempt.terminal_cause is None
    assert attempt.requirement_version is None
    assert attempt.requirement_digest is None
    assert receipt is not None
    assert receipt.assigned_enrollment_id is None
    assert receipt.assigned_descriptor_version is None
    assert receipt.assigned_descriptor_hash is None
    assert receipt.assigned_model_provider is None
    assert receipt.assigned_model_name is None
    assert receipt.assigned_model_digest is None
    assert receipt.evidence_role is None
    assert receipt.terminal_cause is None
    assert receipt.requirement_version is None
    assert receipt.requirement_digest is None
    assert receipt.as_legacy_result()["enrollment_id"] is None
    assert receipt.as_legacy_result()["capability_descriptor_hash"] is None
    assert store.list_evidence_reconciliation_candidates() == []
    with sqlite3.connect(path) as con:
        quarantine_columns = {
            row[1] for row in con.execute("PRAGMA table_info(result_quarantine)")
        }
        attempt_columns = {
            row[1] for row in con.execute("PRAGMA table_info(attempts)")
        }
        receipt_columns = {
            row[1]
            for row in con.execute("PRAGMA table_info(accepted_result_receipts)")
        }
    assert "claimed_enrollment_id" in quarantine_columns
    assert {
        "assigned_descriptor_version",
        "assigned_descriptor_hash",
        "assigned_model_provider",
        "assigned_model_name",
        "assigned_model_digest",
        "evidence_role",
        "comparison_primary_attempt_id",
        "terminal_cause",
        "requirement_version",
        "requirement_digest",
    }.issubset(attempt_columns)
    assert {
        "assigned_descriptor_version",
        "assigned_descriptor_hash",
        "assigned_model_provider",
        "assigned_model_name",
        "assigned_model_digest",
        "evidence_role",
        "terminal_cause",
        "requirement_version",
        "requirement_digest",
    }.issubset(receipt_columns)


@pytest.mark.parametrize("limit", [0, 1001, True, 1.5, "1"])
def test_evidence_reconciliation_candidate_limit_is_bounded(tmp_path, limit):
    store = AttemptStore(tmp_path / "attempts.db")
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_evidence_reconciliation_candidates(limit=limit)


def test_evidence_reconciliation_candidates_are_scoped_bounded_and_deterministic(
    tmp_path, monkeypatch
):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker")
    store = AttemptStore(path)
    task_z, attempt_z, nonce_z = _issue_enrolled(store, task_id="task-z")
    _task_a, attempt_a, _nonce_a = _issue_enrolled(store, task_id="task-a")
    terminal_at = time.time()
    with monkeypatch.context() as evidence_failure:
        evidence_failure.setattr(
            CapabilityEvidenceStore,
            "best_effort_in_transaction",
            staticmethod(lambda *_args, **_kwargs: None),
        )
        store.settle(
            **_submission(task_z, attempt_z, nonce_z),
            session_id="session-a",
            enrollment_id="enrollment-a",
            credential_version=1,
            now=terminal_at,
        )
        assert store.transition_active(
            attempt_id=attempt_a,
            state="expired",
            reason="diagnostic wording",
            terminal_cause="lease_expired",
            now=terminal_at,
        )

    candidates = store.list_evidence_reconciliation_candidates(limit=1000)
    assert [candidate.attempt_id for candidate in candidates] == [
        attempt_a,
        attempt_z,
    ]
    assert [
        candidate.terminal_cause for candidate in candidates
    ] == ["lease_expired", "settled_output"]
    assert store.list_evidence_reconciliation_candidates(limit=1) == [
        candidates[0]
    ]


def test_evidence_reconciliation_excludes_complete_and_nonattributable_attempts(
    tmp_path, monkeypatch
):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker")
    store = AttemptStore(path)

    complete_task, complete_id, complete_nonce = _issue_enrolled(
        store, task_id="complete"
    )
    store.settle(
        **_submission(complete_task, complete_id, complete_nonce),
        session_id="session-a",
        enrollment_id="enrollment-a",
        credential_version=1,
    )

    missing_task, missing_id, missing_nonce = _issue_enrolled(
        store, task_id="missing"
    )
    with monkeypatch.context() as evidence_failure:
        evidence_failure.setattr(
            CapabilityEvidenceStore,
            "best_effort_in_transaction",
            staticmethod(lambda *_args, **_kwargs: None),
        )
        store.settle(
            **_submission(missing_task, missing_id, missing_nonce),
            session_id="session-a",
            enrollment_id="enrollment-a",
            credential_version=1,
        )

    _task_value, nonattributable_id, _nonce = _issue_enrolled(
        store, task_id="caller-cancelled"
    )
    assert store.transition_active(
        attempt_id=nonattributable_id,
        state="cancelled",
        reason="caller cancelled",
        terminal_cause="execution_cancelled",
    )

    candidates = store.list_evidence_reconciliation_candidates(limit=1000)

    assert [candidate.attempt_id for candidate in candidates] == [missing_id]


def test_evidence_reconciliation_limit_cannot_be_starved_by_older_complete_rows(
    tmp_path, monkeypatch
):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker")
    store = AttemptStore(path)

    for index in range(3):
        task, attempt_id, nonce = _issue_enrolled(
            store, task_id=f"older-complete-{index}"
        )
        store.settle(
            **_submission(task, attempt_id, nonce),
            session_id="session-a",
            enrollment_id="enrollment-a",
            credential_version=1,
        )

    missing_ids: list[str] = []
    with monkeypatch.context() as evidence_failure:
        evidence_failure.setattr(
            CapabilityEvidenceStore,
            "best_effort_in_transaction",
            staticmethod(lambda *_args, **_kwargs: None),
        )
        for index in range(2):
            task, attempt_id, nonce = _issue_enrolled(
                store, task_id=f"later-missing-{index}"
            )
            store.settle(
                **_submission(task, attempt_id, nonce),
                session_id="session-a",
                enrollment_id="enrollment-a",
                credential_version=1,
            )
            missing_ids.append(attempt_id)

    candidates = store.list_evidence_reconciliation_candidates(limit=2)

    assert len(candidates) == 2
    assert {candidate.attempt_id for candidate in candidates} == set(missing_ids)


def test_exact_replay_survives_database_reopen(tmp_path):
    path = tmp_path / "attempts.db"
    first_store = AttemptStore(path)
    task, attempt_id, nonce = _issue(first_store)
    submitted = _submission(task, attempt_id, nonce)

    first = first_store.settle(**submitted)
    replay = AttemptStore(path).settle(**submitted)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.response == first.response
    assert replay.receipt == first.receipt


def test_enrolled_settlement_persists_attribution_atomically(tmp_path):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker")
    store = AttemptStore(path)
    task, attempt_id, nonce = _issue_enrolled(store)

    outcome = store.settle(
        **_submission(task, attempt_id, nonce),
        session_id="session-a",
        enrollment_id="enrollment-a",
        credential_version=1,
    )

    assert outcome.receipt.assigned_enrollment_id == "enrollment-a"
    assert outcome.receipt.assigned_model_provider == "ollama"
    assert outcome.receipt.assigned_model_name == "qwen3.5:4b"
    assert outcome.receipt.assigned_model_digest is None
    assert outcome.receipt.evidence_role == "production"
    assert outcome.receipt.terminal_cause == "settled_output"
    assert outcome.receipt.as_legacy_result()["selected_model_name"] == "qwen3.5:4b"
    with sqlite3.connect(path) as con:
        receipt = con.execute(
            "SELECT assigned_enrollment_id, assigned_model_provider, "
            "assigned_model_name, assigned_model_digest, evidence_role, "
            "terminal_cause FROM accepted_result_receipts"
        ).fetchone()
        contribution = con.execute(
            "SELECT enrollment_id, node_id, session_id FROM contributions"
        ).fetchone()
    assert receipt == (
        "enrollment-a",
        "ollama",
        "qwen3.5:4b",
        None,
        "production",
        "settled_output",
    )
    assert contribution == ("enrollment-a", "worker", "session-a")
    assert store.lifetime_contribution_summary(
        "worker", enrollment_id="enrollment-a"
    ) == {
        "lifetime_tasks_completed": 1,
        "lifetime_contribution_points": 5.0,
    }
    assert store.lifetime_contribution_summary("worker") == {
        "lifetime_tasks_completed": 0,
        "lifetime_contribution_points": 0.0,
    }


def test_exact_replay_accepts_fresh_session_for_same_current_enrollment(tmp_path):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker")
    store = AttemptStore(path)
    task, attempt_id, nonce = _issue_enrolled(store)
    submitted = _submission(task, attempt_id, nonce)

    first = store.settle(
        **submitted,
        session_id="session-a",
        enrollment_id="enrollment-a",
        credential_version=1,
    )
    replay = AttemptStore(path).settle(
        **submitted,
        session_id="fresh-session-a",
        enrollment_id="enrollment-a",
        credential_version=1,
    )

    assert replay.replayed is True
    assert replay.response == first.response
    assert replay.receipt == first.receipt


def test_other_enrollment_cannot_settle_even_with_assigned_label(tmp_path):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker")
    _enroll(path, "enrollment-b", "other-worker")
    store = AttemptStore(path)
    task, attempt_id, nonce = _issue_enrolled(store)

    with pytest.raises(AttemptRejected, match="enrollment"):
        store.settle(
            **_submission(task, attempt_id, nonce, node_id="worker"),
            session_id="session-a",
            enrollment_id="enrollment-b",
            credential_version=1,
        )
    assert store.get(attempt_id).state == "active"


def test_enrolled_stream_checks_current_enrollment_and_attempt_version(tmp_path):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker")
    store = AttemptStore(path)
    task, attempt_id, nonce = _issue_enrolled(store)
    binding = {
        "task_id": task["task_id"],
        "node_id": "worker",
        "tokens": "batch",
        "contract_version": "1",
        "attempt_id": attempt_id,
        "nonce": nonce,
        "execution_id": task["execution_id"],
        "execution_unit_id": task["execution_unit_id"],
        "execution_unit_kind": task["execution_unit_kind"],
        "session_id": "session-a",
        "enrollment_id": "enrollment-a",
        "credential_version": 1,
    }

    assert store.record_stream_batch(**binding).accepted is True
    with sqlite3.connect(path) as con:
        con.execute(
            "UPDATE node_enrollments SET credential_version=2 "
            "WHERE enrollment_id='enrollment-a'"
        )
        con.commit()
    with pytest.raises(AttemptRejected, match="no longer current"):
        store.record_stream_batch(**binding)


def test_revoked_enrollment_cannot_settle_or_replay(tmp_path):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker")
    store = AttemptStore(path)
    task, attempt_id, nonce = _issue_enrolled(store)
    submitted = _submission(task, attempt_id, nonce)

    with sqlite3.connect(path) as con:
        con.execute(
            "UPDATE node_enrollments SET status='revoked', revoked_at=? "
            "WHERE enrollment_id='enrollment-a'",
            (time.time(),),
        )
        con.commit()
    with pytest.raises(AttemptRejected, match="revoked"):
        store.settle(
            **submitted,
            session_id="session-a",
            enrollment_id="enrollment-a",
            credential_version=1,
        )

    with sqlite3.connect(path) as con:
        con.execute(
            "UPDATE node_enrollments SET status='active', revoked_at=NULL "
            "WHERE enrollment_id='enrollment-a'"
        )
        con.commit()
    store.settle(
        **submitted,
        session_id="session-a",
        enrollment_id="enrollment-a",
        credential_version=1,
    )
    with sqlite3.connect(path) as con:
        con.execute(
            "UPDATE node_enrollments SET status='revoked', revoked_at=? "
            "WHERE enrollment_id='enrollment-a'",
            (time.time(),),
        )
        con.commit()
    with pytest.raises(AttemptRejected, match="revoked"):
        store.settle(
            **submitted,
            session_id="fresh-session",
            enrollment_id="enrollment-a",
            credential_version=1,
        )


def test_revoked_enrollment_cannot_receive_a_new_attempt(tmp_path):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker", status="revoked")
    store = AttemptStore(path)
    with pytest.raises(AttemptConflict, match="revoked"):
        _issue_enrolled(store)
    assert store.count_attempts("task-enrolled") == 0


def test_rotation_invalidates_old_authority_and_new_attempt_uses_new_version(tmp_path):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker")
    store = AttemptStore(path)
    task, attempt_id, nonce = _issue_enrolled(store)
    submitted = _submission(task, attempt_id, nonce)

    with sqlite3.connect(path) as con:
        con.execute(
            "UPDATE node_enrollments SET credential_version=2, rotated_at=? "
            "WHERE enrollment_id='enrollment-a'",
            (time.time(),),
        )
        con.commit()
    with pytest.raises(AttemptRejected, match="no longer current"):
        store.settle(
            **submitted,
            session_id="session-a",
            enrollment_id="enrollment-a",
            credential_version=1,
        )
    with pytest.raises(AttemptRejected, match="credential version"):
        store.settle(
            **submitted,
            session_id="fresh-session",
            enrollment_id="enrollment-a",
            credential_version=2,
        )

    assert store.transition_active(
        attempt_id=attempt_id,
        state="reclaimed",
        reason="credential rotated",
    )
    next_task, next_attempt, next_nonce = _issue_enrolled(
        store,
        session_id="fresh-session",
        credential_version=2,
        task_id="task-after-rotation",
    )
    accepted = store.settle(
        **_submission(next_task, next_attempt, next_nonce),
        session_id="fresh-session",
        enrollment_id="enrollment-a",
        credential_version=2,
    )
    assert accepted.replayed is False


def test_reclaim_enrollment_only_transitions_its_active_attempts(tmp_path):
    path = tmp_path / "attempts.db"
    _enroll(path, "enrollment-a", "worker-a")
    _enroll(path, "enrollment-b", "worker-b")
    store = AttemptStore(path)
    _issue_enrolled(
        store,
        enrollment_id="enrollment-a",
        node_id="worker-a",
        session_id="session-a",
        task_id="task-a-1",
    )
    _issue_enrolled(
        store,
        enrollment_id="enrollment-a",
        node_id="worker-a",
        session_id="session-a",
        task_id="task-a-2",
    )
    _issue_enrolled(
        store,
        enrollment_id="enrollment-b",
        node_id="worker-b",
        session_id="session-b",
        task_id="task-b",
    )

    assert store.reclaim_enrollment("enrollment-a", "operator revoked") == [
        "task-a-1",
        "task-a-2",
    ]
    assert store.reclaim_enrollment("enrollment-a", "operator revoked") == []
    with sqlite3.connect(path) as con:
        rows = con.execute(
            "SELECT task_id, state, terminal_cause FROM attempts ORDER BY task_id"
        ).fetchall()
    assert rows == [
        ("task-a-1", "reclaimed", "enrollment_reclaimed"),
        ("task-a-2", "reclaimed", "enrollment_reclaimed"),
        ("task-b", "active", None),
    ]


def test_changed_payload_is_not_an_exact_replay(tmp_path):
    store = AttemptStore(tmp_path / "attempts.db")
    task, attempt_id, nonce = _issue(store)
    submitted = _submission(task, attempt_id, nonce)
    store.settle(**submitted)

    with pytest.raises(AttemptRejected, match="replay payload does not match"):
        store.settle(**{**submitted, "output": "different output"})


@pytest.mark.parametrize(
    "output, error, expected_cause",
    [
        ("complete output", None, "settled_output"),
        (None, "worker failed", "settled_worker_error"),
        (None, None, "settled_empty_output"),
    ],
)
def test_settlement_persists_bounded_terminal_cause_on_attempt_and_receipt(
    tmp_path, output, error, expected_cause
):
    store = AttemptStore(tmp_path / f"{expected_cause}.db")
    task, attempt_id, nonce = _issue(store)

    outcome = store.settle(
        **_submission(task, attempt_id, nonce, output=output, error=error)
    )

    assert store.get(attempt_id).terminal_cause == expected_cause
    assert outcome.receipt.terminal_cause == expected_cause


def test_known_attempt_closures_persist_typed_causes_without_parsing_reason(tmp_path):
    store = AttemptStore(tmp_path / "attempts.db")
    _task_value, attempt_id, _nonce = _issue(store, "deadline")

    with pytest.raises(ValueError, match="terminal_cause"):
        store.transition_active(
            attempt_id=attempt_id,
            state="cancelled",
            reason="arbitrary diagnostic text",
            terminal_cause="invented_cause",
        )
    assert store.get(attempt_id).state == "active"

    assert store.transition_active(
        attempt_id=attempt_id,
        state="cancelled",
        reason="this wording is not interpreted",
        terminal_cause="execution_deadline",
    )
    assert store.get(attempt_id).terminal_cause == "execution_deadline"

    _task_value, expiring_attempt, _nonce = _issue(store, "expiring")
    assert store.expire_due(now=time.time() + 120) == 1
    assert store.get(expiring_attempt).terminal_cause == "lease_expired"

    _task_value, interrupted_attempt, _nonce = _issue(store, "restart")
    assert store.interrupt_active("coordinator restarted") == 1
    assert store.get(interrupted_attempt).terminal_cause == "coordinator_restart"


def test_cancel_execution_persists_only_an_explicit_bounded_cause(tmp_path):
    store = AttemptStore(tmp_path / "attempts.db")
    _task_value, first_attempt, _nonce = _issue(store, "cancel-first")
    _task_value, second_attempt, _nonce = _issue(store, "cancel-second")

    assert set(
        store.cancel_execution(
            EXECUTION_ID,
            "wording is diagnostic only",
            terminal_cause="execution_cancelled",
        )
    ) == {"cancel-first", "cancel-second"}
    assert store.get(first_attempt).terminal_cause == "execution_cancelled"
    assert store.get(second_attempt).terminal_cause == "execution_cancelled"


def test_payload_and_stream_limits_persist_distinct_typed_causes(tmp_path):
    store = AttemptStore(tmp_path / "attempts.db")
    payload_task = _task("payload-limit")
    payload_task["max_output_bytes"] = 3
    now = time.time()
    store.issue(
        payload_task,
        assigned_node_id="worker",
        attempt_id="attempt-payload-limit",
        nonce="nonce-payload-limit",
        issued_at=now,
        lease_expires_at=now + 60,
    )
    with pytest.raises(WorkerPayloadLimitExceeded):
        store.settle(
            **_submission(
                payload_task,
                "attempt-payload-limit",
                "nonce-payload-limit",
                output="four",
            )
        )
    assert (
        store.get("attempt-payload-limit").terminal_cause
        == "output_payload_limit"
    )

    stream_task = _task("stream-limit")
    stream_task["max_output_bytes"] = 3
    store.issue(
        stream_task,
        assigned_node_id="worker",
        attempt_id="attempt-stream-limit",
        nonce="nonce-stream-limit",
        issued_at=now,
        lease_expires_at=now + 60,
    )
    outcome = store.record_stream_batch(
        task_id=stream_task["task_id"],
        node_id="worker",
        tokens="four",
        contract_version="1",
        attempt_id="attempt-stream-limit",
        nonce="nonce-stream-limit",
        execution_id=stream_task["execution_id"],
        execution_unit_id=stream_task["execution_unit_id"],
        execution_unit_kind=stream_task["execution_unit_kind"],
    )
    assert outcome.error_code == "output_limit_exceeded"
    assert store.get("attempt-stream-limit").terminal_cause == "stream_output_limit"


def test_two_concurrent_submissions_settle_exactly_once(tmp_path):
    path = tmp_path / "attempts.db"
    task, attempt_id, nonce = _issue(AttemptStore(path))
    submitted = _submission(task, attempt_id, nonce)

    def settle_once(_index: int):
        return AttemptStore(path).settle(**submitted)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(settle_once, range(2)))

    assert sorted(outcome.replayed for outcome in outcomes) == [False, True]
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM accepted_result_receipts").fetchone()[0] == 1
        contribution = con.execute(
            "SELECT basis, points_are_monetary FROM contributions"
        ).fetchone()
    assert contribution == ("compute_contribution", 0)


@pytest.mark.parametrize(
    "terminal_state",
    ["expired", "reclaimed", "cancelled", "superseded", "interrupted"],
)
def test_inactive_attempt_cannot_publish_a_receipt(tmp_path, terminal_state):
    store = AttemptStore(tmp_path / f"{terminal_state}.db")
    task, attempt_id, nonce = _issue(store)
    assert store.transition_active(
        attempt_id=attempt_id,
        state=terminal_state,
        reason=f"test {terminal_state}",
    )
    assert store.get(attempt_id).terminal_cause is None

    message = "lease expired" if terminal_state == "expired" else f"attempt is {terminal_state}"
    with pytest.raises(AttemptRejected, match=message):
        store.settle(**_submission(task, attempt_id, nonce))
    assert store.get_receipt_for_task(task["task_id"]) is None


def test_unknown_task_cannot_publish_a_receipt(tmp_path):
    store = AttemptStore(tmp_path / "attempts.db")
    with pytest.raises(AttemptRejected, match="no active server-issued attempt"):
        store.settle(
            **_submission(_task("unknown"), "made-up-attempt", "made-up-nonce")
        )
    assert store.get_receipt_for_task("unknown") is None


def test_broker_checks_execution_and_unit_binding(tmp_path):
    store = AttemptStore(tmp_path / "attempts.db")
    task, attempt_id, nonce = _issue(store)
    receipt = store.settle(**_submission(task, attempt_id, nonce)).receipt
    broker = AcceptedResultBroker(store)
    broker.publish(receipt)

    assert broker.get_matching(
        task_id=task["task_id"],
        execution_id=task["execution_id"],
        execution_unit_id=task["execution_unit_id"],
        execution_unit_kind=task["execution_unit_kind"],
    ) == receipt
    with pytest.raises(ReceiptBindingError, match="execution_unit_id"):
        broker.get_matching(
            task_id=task["task_id"],
            execution_id=task["execution_id"],
            execution_unit_id="another-unit",
            execution_unit_kind=task["execution_unit_kind"],
        )


def test_quarantine_never_becomes_an_accepted_receipt(tmp_path):
    store = AttemptStore(tmp_path / "attempts.db")
    broker = AcceptedResultBroker(store)
    store.quarantine(
        task_id="queued-task",
        claimed_attempt_id=None,
        claimed_node_id="claimant",
        claimed_execution_id=EXECUTION_ID,
        claimed_unit_id="candidate-1",
        claimed_unit_kind="candidate",
        claimed_contract_version=None,
        output="plausible but unbound output",
        error=None,
        reason="no active server-issued attempt",
    )

    assert store.quarantine_count() == 1
    assert broker.get_matching(
        task_id="queued-task",
        execution_id=EXECUTION_ID,
        execution_unit_id="candidate-1",
        execution_unit_kind="candidate",
    ) is None
