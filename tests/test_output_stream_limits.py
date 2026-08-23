"""Server-issued output budgets, cumulative streams, and bounded fan-out."""

from __future__ import annotations

import hashlib
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import execution.attempts as attempt_module
import server_state as state
from execution.attempts import AttemptStore, WorkerPayloadLimitExceeded
from server import app


EXECUTION_ID = "e" * 32


def _task(*, task_id: str = "task-1", budget: int = 8) -> dict:
    return {
        "task_id": task_id,
        "contract_version": "1",
        "execution_id": EXECUTION_ID,
        "execution_unit_id": "candidate-1",
        "execution_unit_kind": "candidate",
        "max_output_bytes": budget,
    }


def _issue(store: AttemptStore, *, budget: int = 8, session_id: str | None = None):
    task = _task(budget=budget)
    now = time.time()
    store.issue(
        task,
        assigned_node_id="worker",
        assigned_session_id=session_id,
        attempt_id="attempt-1",
        nonce="unguessable-nonce",
        issued_at=now,
        lease_expires_at=now + 60,
    )
    return task


def _submission(task: dict, *, output: str | None, error: str | None = None) -> dict:
    return {
        "task_id": task["task_id"],
        "node_id": "worker",
        "output": output,
        "error": error,
        "elapsed_seconds": 1,
        "contract_version": "1",
        "attempt_id": "attempt-1",
        "nonce": "unguessable-nonce",
        "execution_id": task["execution_id"],
        "execution_unit_id": task["execution_unit_id"],
        "execution_unit_kind": task["execution_unit_kind"],
    }


@pytest.mark.parametrize(
    ("budget", "output"),
    [
        (4, "abcd"),
        (4, "éé"),
    ],
)
def test_exact_ascii_and_multibyte_utf8_boundaries_settle(tmp_path, budget, output):
    store = AttemptStore(tmp_path / "attempts.db")
    task = _issue(store, budget=budget)

    outcome = store.settle(**_submission(task, output=output))

    assert outcome.receipt.output == output
    assert store.get("attempt-1").max_output_bytes == budget


def test_multibyte_oversize_is_terminal_before_receipt_or_contribution(tmp_path):
    store = AttemptStore(tmp_path / "attempts.db")
    task = _issue(store, budget=3)

    with pytest.raises(WorkerPayloadLimitExceeded) as rejected:
        store.settle(**_submission(task, output="éé"))

    assert rejected.value.observed == 4
    assert store.get("attempt-1").state == "cancelled"
    assert store.get_receipt_for_task(task["task_id"]) is None
    assert store.lifetime_contribution_summary("worker") == {
        "lifetime_tasks_completed": 0,
        "lifetime_contribution_points": 0.0,
    }


@pytest.fixture
def client(monkeypatch):
    for value in (
        state.nodes,
        state.task_queue,
        state.task_inflight,
        state.task_results,
        state.node_failure_count,
        state.node_blacklist,
        state.pipeline_events,
    ):
        value.clear()
    state.node_sessions.reset()
    monkeypatch.setattr(state, "_LONG_POLL_TIMEOUT", 0.01)
    with TestClient(app) as test_client:
        yield test_client


def _register(client):
    response = client.post("/nodes/register", json={
        "node_id": "worker",
        "model": "model",
        "platform": "Linux",
        "machine": "x86_64",
        "hostname": "worker",
    })
    assert response.status_code == 200
    return response


def _headers(registration) -> dict[str, str]:
    return {"X-Node-Session": registration.json()["session_token"]}


def _claim(client, registration, *, budget: int = 8) -> dict:
    state.task_queue.append(_task(budget=budget))
    response = client.get(
        "/tasks/next", params={"node_id": "worker"}, headers=_headers(registration)
    )
    assert response.status_code == 200
    return response.json()


def _result_body(task: dict, *, output: str | None, error: str | None = None) -> dict:
    return {
        "node_id": "worker",
        "output": output,
        "error": error,
        "elapsed_seconds": 1,
        "contract_version": "1",
        "attempt_id": task["attempt_id"],
        "nonce": task["nonce"],
        "execution_id": task["execution_id"],
        "execution_unit_id": task["execution_unit_id"],
        "execution_unit_kind": task["execution_unit_kind"],
    }


def _stream_body(task: dict, tokens: str) -> dict:
    body = _result_body(task, output=None)
    body.pop("output")
    body.pop("error")
    body.pop("elapsed_seconds")
    body["tokens"] = tokens
    return body


def test_oversized_result_is_quarantined_but_never_published_or_paid(client):
    registration = _register(client)
    task = _claim(client, registration, budget=4)
    oversized = "😀" * 2000

    response = client.post(
        "/tasks/task-1/result",
        json=_result_body(task, output=oversized),
        headers=_headers(registration),
    )

    assert response.status_code == 413
    assert response.json()["error"] == "output_limit_exceeded"
    assert response.json()["observed_bytes"] == len(oversized.encode("utf-8"))
    assert state.attempt_store.get(task["attempt_id"]).state == "cancelled"
    assert state.attempt_store.get_receipt_for_task("task-1") is None
    assert state.accepted_result_broker.get_matching(
        task_id="task-1",
        execution_id=EXECUTION_ID,
        execution_unit_id="candidate-1",
        execution_unit_kind="candidate",
    ) is None
    assert state.attempt_store.lifetime_contribution_summary("worker")[
        "lifetime_tasks_completed"
    ] == 0
    with sqlite3.connect(state.attempt_store.path) as connection:
        output_hash, preview = connection.execute(
            "SELECT output_sha256, output_preview FROM result_quarantine "
            "ORDER BY received_at DESC LIMIT 1"
        ).fetchone()
    assert output_hash == hashlib.sha256(oversized.encode("utf-8")).hexdigest()
    assert len(preview.encode("utf-8")) <= 4096


def test_error_text_has_its_own_utf8_byte_limit(client):
    registration = _register(client)
    task = _claim(client, registration, budget=100)
    response = client.post(
        "/tasks/task-1/result",
        json=_result_body(task, output=None, error="😀" * 513),
        headers=_headers(registration),
    )
    assert response.status_code == 413
    assert response.json()["error"] == "error_limit_exceeded"
    assert state.attempt_store.get_receipt_for_task("task-1") is None


def test_stream_budget_is_cumulative_and_emits_one_terminal_event(client):
    registration = _register(client)
    task = _claim(client, registration, budget=5)
    headers = _headers(registration)

    assert client.post(
        "/tasks/task-1/tokens", json=_stream_body(task, "é"), headers=headers
    ).status_code == 200
    assert client.post(
        "/tasks/task-1/tokens", json=_stream_body(task, "abc"), headers=headers
    ).status_code == 200
    limited = client.post(
        "/tasks/task-1/tokens", json=_stream_body(task, "x"), headers=headers
    )
    assert limited.status_code == 413
    assert limited.json()["error"] == "output_limit_exceeded"
    assert limited.json()["streamed_bytes"] == 5
    record = state.attempt_store.get(task["attempt_id"])
    assert record.streamed_bytes == 5
    assert record.stream_batch_count == 2
    assert record.first_stream_at is not None
    assert record.last_stream_at is not None
    assert record.stream_closed == 1
    assert record.state == "cancelled"

    again = client.post(
        "/tasks/task-1/tokens", json=_stream_body(task, "again"), headers=headers
    )
    assert again.status_code == 403
    events = [
        event
        for event in state.pipeline_events
        if event["type"] == "stream_limit_exceeded"
    ]
    assert len(events) == 1


def test_stream_batch_count_and_rate_are_bounded(client, monkeypatch):
    registration = _register(client)
    task = _claim(client, registration, budget=100)
    headers = _headers(registration)
    monkeypatch.setattr(attempt_module, "MAX_STREAM_BATCHES", 2)
    assert client.post(
        "/tasks/task-1/tokens", json=_stream_body(task, "a"), headers=headers
    ).status_code == 200
    assert client.post(
        "/tasks/task-1/tokens", json=_stream_body(task, "b"), headers=headers
    ).status_code == 200
    limited = client.post(
        "/tasks/task-1/tokens", json=_stream_body(task, "c"), headers=headers
    )
    assert limited.status_code == 413
    assert limited.json()["error"] == "stream_batch_limit_exceeded"

    # A fresh attempt exercises the independent per-second rate budget.
    state.task_queue.clear()
    state.task_inflight.clear()
    second_task = _task(task_id="task-2", budget=100)
    second_task["execution_unit_id"] = "candidate-2"
    state.task_queue.append(second_task)
    task2 = client.get(
        "/tasks/next", params={"node_id": "worker"}, headers=headers
    ).json()
    monkeypatch.setattr(attempt_module, "MAX_STREAM_BATCHES", 100)
    monkeypatch.setattr(attempt_module, "MAX_STREAM_BATCHES_PER_WINDOW", 2)
    assert client.post(
        "/tasks/task-2/tokens", json=_stream_body(task2, "a"), headers=headers
    ).status_code == 200
    assert client.post(
        "/tasks/task-2/tokens", json=_stream_body(task2, "b"), headers=headers
    ).status_code == 200
    rate_limited = client.post(
        "/tasks/task-2/tokens", json=_stream_body(task2, "c"), headers=headers
    )
    assert rate_limited.status_code == 429
    assert rate_limited.json()["error"] == "stream_rate_limit_exceeded"


def test_stream_after_settlement_is_rejected(client):
    registration = _register(client)
    task = _claim(client, registration, budget=20)
    headers = _headers(registration)
    assert client.post(
        "/tasks/task-1/result",
        json=_result_body(task, output="done"),
        headers=headers,
    ).status_code == 200
    response = client.post(
        "/tasks/task-1/tokens", json=_stream_body(task, "late"), headers=headers
    )
    assert response.status_code == 403
    assert "settled" in response.json()["detail"]


@pytest.mark.asyncio
async def test_slow_websocket_viewer_has_bounded_fanout_queue():
    manager = state._WSManager()

    class SlowWebSocket:
        def __init__(self):
            import asyncio

            self.release = asyncio.Event()

        async def accept(self):
            return None

        async def send_json(self, _data):
            await self.release.wait()

    websocket = SlowWebSocket()
    await manager.connect(websocket)
    for index in range(state._WS_QUEUE_MAX * 4):
        manager.publish({"type": "token", "token": str(index)})

    connection = manager._connections[websocket]
    assert manager.queued_event_count <= state._WS_QUEUE_MAX
    assert connection["truncation_notified"] is True
    queued = list(connection["queue"]._queue)
    assert sum(event["type"] == "token_fanout_truncated" for event in queued) <= 1
    manager.disconnect(websocket)
