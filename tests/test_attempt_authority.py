"""Durable attempt authority, receipt broker, and quarantine invariants."""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from execution.attempts import (
    AcceptedResultBroker,
    AttemptConflict,
    AttemptRejected,
    AttemptStore,
    ReceiptBindingError,
)
from node_enrollments import ensure_node_enrollment_schema


EXECUTION_ID = "e" * 32


def _task(task_id: str = "task-1") -> dict:
    return {
        "task_id": task_id,
        "contract_version": "1",
        "execution_id": EXECUTION_ID,
        "execution_unit_id": "candidate-1",
        "execution_unit_kind": "candidate",
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
        attempt_id=attempt_id,
        nonce=nonce,
        issued_at=now,
        lease_expires_at=now + 60,
    )
    return task, attempt_id, nonce


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
    assert receipt is not None
    assert receipt.assigned_enrollment_id is None
    assert receipt.as_legacy_result()["enrollment_id"] is None
    with sqlite3.connect(path) as con:
        quarantine_columns = {
            row[1] for row in con.execute("PRAGMA table_info(result_quarantine)")
        }
    assert "claimed_enrollment_id" in quarantine_columns


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
    with sqlite3.connect(path) as con:
        receipt = con.execute(
            "SELECT assigned_enrollment_id FROM accepted_result_receipts"
        ).fetchone()
        contribution = con.execute(
            "SELECT enrollment_id, node_id, session_id FROM contributions"
        ).fetchone()
    assert receipt == ("enrollment-a",)
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
            "SELECT task_id, state FROM attempts ORDER BY task_id"
        ).fetchall()
    assert rows == [
        ("task-a-1", "reclaimed"),
        ("task-a-2", "reclaimed"),
        ("task-b", "active"),
    ]


def test_changed_payload_is_not_an_exact_replay(tmp_path):
    store = AttemptStore(tmp_path / "attempts.db")
    task, attempt_id, nonce = _issue(store)
    submitted = _submission(task, attempt_id, nonce)
    store.settle(**submitted)

    with pytest.raises(AttemptRejected, match="replay payload does not match"):
        store.settle(**{**submitted, "output": "different output"})


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
