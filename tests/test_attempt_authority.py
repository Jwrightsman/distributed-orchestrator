"""Durable attempt authority, receipt broker, and quarantine invariants."""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from execution.attempts import (
    AcceptedResultBroker,
    AttemptRejected,
    AttemptStore,
    ReceiptBindingError,
)


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
