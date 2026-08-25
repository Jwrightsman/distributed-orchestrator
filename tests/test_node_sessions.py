"""Trusted-alpha worker registration sessions and node statistics."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import access_control
import server_state as state
from server import app


@pytest.fixture
def client(monkeypatch):
    for value in (
        state.nodes,
        state.task_queue,
        state.task_inflight,
        state.task_results,
        state.node_failure_count,
        state.node_blacklist,
    ):
        value.clear()
    state.node_sessions.reset()
    monkeypatch.setattr(state, "_LONG_POLL_TIMEOUT", 0.01)
    with TestClient(app) as test_client:
        yield test_client


def _registration(node_id: str = "worker", **overrides) -> dict:
    return {
        "node_id": node_id,
        "model": "qwen3.5:4b",
        "platform": "Linux",
        "machine": "x86_64",
        "hostname": node_id,
        "cpu_count": 4,
        "ram_gb": 8,
        "capabilities": ["code"],
        **overrides,
    }


def _register(client, node_id: str = "worker", token: str | None = None, **overrides):
    headers = {"X-Node-Session": token} if token else {}
    return client.post(
        "/nodes/register",
        json=_registration(node_id, **overrides),
        headers=headers,
    )


def _headers(registration_response) -> dict[str, str]:
    return {"X-Node-Session": registration_response.json()["session_token"]}


def _queue_v1(task_id: str = "task-1", *, max_output_bytes: int = 1024):
    state.task_queue.append({
        "task_id": task_id,
        "title": "candidate",
        "prompt": "complete it",
        "system": "system",
        "contract_version": "1",
        "execution_id": "e" * 32,
        "execution_unit_id": "candidate-1",
        "execution_unit_kind": "candidate",
        "max_output_bytes": max_output_bytes,
    })


def _bound_body(task: dict, *, node_id: str = "worker", output: str = "done") -> dict:
    return {
        "node_id": node_id,
        "output": output,
        "elapsed_seconds": 1,
        "contract_version": task["contract_version"],
        "attempt_id": task["attempt_id"],
        "nonce": task["nonce"],
        "execution_id": task["execution_id"],
        "execution_unit_id": task["execution_unit_id"],
        "execution_unit_kind": task["execution_unit_kind"],
    }


def test_registration_issues_digest_only_server_session(client):
    response = _register(client, " Worker-A ")

    assert response.status_code == 200
    body = response.json()
    assert body["node_id"] == "worker-a"
    assert body["session_id"]
    assert len(body["session_token"]) >= 32
    assert body["session_expires_at"]
    record = state.node_sessions.current("worker-a")
    assert record is not None
    assert record.token_digest != body["session_token"]
    assert "session_token" not in state.nodes["worker-a"]
    assert body["session_token"] not in repr(record)


def test_polling_requires_valid_session_and_creates_no_placeholder(client):
    missing = client.get("/tasks/next", params={"node_id": "made-up"})
    assert missing.status_code == 401
    assert missing.json()["detail"]["action"] == "register_again"
    assert "made-up" not in state.nodes

    registration = _register(client)
    wrong = client.get(
        "/tasks/next",
        params={"node_id": "worker"},
        headers={"X-Node-Session": "wrong"},
    )
    assert wrong.status_code == 401
    assert client.get(
        "/tasks/next", params={"node_id": "worker"}, headers=_headers(registration)
    ).status_code == 204


def test_expired_session_is_rejected(client):
    registration = _register(client)
    state.node_sessions.current("worker").expires_at = time.time() - 1

    response = client.get(
        "/tasks/next", params={"node_id": "worker"}, headers=_headers(registration)
    )

    assert response.status_code == 401
    assert "expired" in response.json()["detail"]["message"]


def test_duplicate_active_id_conflicts_but_same_session_is_idempotent(client):
    first = _register(client)
    duplicate = _register(client)
    same = _register(client, token=first.json()["session_token"])

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "node_id_in_use"
    assert same.status_code == 200
    assert same.json()["idempotent"] is True
    assert same.json()["session_id"] == first.json()["session_id"]
    assert same.json()["session_token"] == first.json()["session_token"]


def test_stale_id_can_be_reclaimed_and_old_token_stops_working(client):
    first = _register(client)
    old_record = state.node_sessions.current("worker")
    old_record.last_seen = time.time() - state._NODE_TIMEOUT - 1

    replacement = _register(client)

    assert replacement.status_code == 200
    assert replacement.json()["session_id"] != first.json()["session_id"]
    assert replacement.json()["session_token"] != first.json()["session_token"]
    old_poll = client.get(
        "/tasks/next", params={"node_id": "worker"}, headers=_headers(first)
    )
    assert old_poll.status_code == 401
    assert client.get(
        "/tasks/next", params={"node_id": "worker"}, headers=_headers(replacement)
    ).status_code == 204


def test_result_and_stream_require_session_and_attempt_binding(client):
    registration = _register(client)
    _queue_v1()
    task = client.get(
        "/tasks/next", params={"node_id": "worker"}, headers=_headers(registration)
    ).json()
    body = _bound_body(task)
    stream = {**body, "tokens": "partial"}
    stream.pop("output")
    stream.pop("elapsed_seconds")

    assert client.post("/tasks/task-1/stream", json=stream).status_code == 401
    assert client.post(
        "/tasks/task-1/stream", json=stream, headers=_headers(registration)
    ).status_code == 200
    assert client.post("/tasks/task-1/result", json=body).status_code == 401
    assert client.post(
        "/tasks/task-1/result", json=body, headers=_headers(registration)
    ).status_code == 200


def test_replacement_session_cannot_stream_or_settle_old_attempt(client):
    original = _register(client)
    _queue_v1()
    task = client.get(
        "/tasks/next", params={"node_id": "worker"}, headers=_headers(original)
    ).json()
    state.node_sessions.current("worker").last_seen = time.time() - state._NODE_TIMEOUT - 1
    replacement = _register(client)
    assert replacement.status_code == 200

    body = _bound_body(task)
    stream = {**body, "tokens": "late"}
    stream.pop("output")
    stream.pop("elapsed_seconds")
    assert client.post(
        "/tasks/task-1/stream", json=stream, headers=_headers(original)
    ).status_code == 401
    replacement_stream = client.post(
        "/tasks/task-1/stream", json=stream, headers=_headers(replacement)
    )
    assert replacement_stream.status_code == 403
    assert client.post(
        "/tasks/task-1/result", json=body, headers=_headers(replacement)
    ).status_code == 403
    assert state.attempt_store.get(task["attempt_id"]).state == "reclaimed"


def test_unenrolled_statistics_do_not_cross_session_identity(client):
    first = _register(client)
    _queue_v1()
    task = client.get(
        "/tasks/next", params={"node_id": "worker"}, headers=_headers(first)
    ).json()
    assert client.post(
        "/tasks/task-1/result",
        json=_bound_body(task),
        headers=_headers(first),
    ).status_code == 200
    node = state.nodes["worker"]
    assert node["session_tasks_completed"] == 1
    assert node["session_contribution_points"] == 5
    assert node["lifetime_tasks_completed"] == 1
    assert node["lifetime_contribution_points"] == 5

    state.node_sessions.current("worker").last_seen = time.time() - state._NODE_TIMEOUT - 1
    second = _register(client)
    assert second.status_code == 200
    node = state.nodes["worker"]
    assert node["session_tasks_completed"] == 0
    assert node["session_contribution_points"] == 0
    # A new legacy session is not durable identity. Reusing its human-readable
    # label must not inherit the prior process incarnation's contribution.
    assert node["lifetime_tasks_completed"] == 0
    assert node["lifetime_contribution_points"] == 0


def test_heartbeat_and_drain_require_current_session(client):
    registration = _register(client)
    assert client.post("/nodes/worker/heartbeat").status_code == 401
    heartbeat = client.post(
        "/nodes/worker/heartbeat", headers=_headers(registration)
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["session_id"] == registration.json()["session_id"]

    drained = client.post("/nodes/worker/drain", headers=_headers(registration))
    assert drained.status_code == 200
    assert drained.json()["draining"] is True
    _queue_v1()
    assert client.get(
        "/tasks/next", params={"node_id": "worker"}, headers=_headers(registration)
    ).status_code == 204
    assert len(state.task_queue) == 1


def test_worker_control_routes_bypass_viewer_gate_but_retain_node_auth(
    client, monkeypatch
):
    config = {"viewer_key": "viewer-secret", "node_secret": "node-secret"}
    monkeypatch.setattr(access_control, "get_config", lambda: config)
    monkeypatch.setattr(state, "get_config", lambda: config)

    registration = client.post(
        "/nodes/register",
        json=_registration(),
        headers={"X-Node-Secret": "node-secret"},
    )
    assert registration.status_code == 200
    session_headers = _headers(registration)

    # Reaching the route without node admission proves viewer middleware did
    # not accept the worker session as a viewer credential.
    missing_admission = client.post(
        "/nodes/worker/heartbeat", headers=session_headers
    )
    assert missing_admission.status_code == 401
    assert "X-Node-Secret" in missing_admission.json()["detail"]

    worker_headers = {"X-Node-Secret": "node-secret", **session_headers}
    assert client.post(
        "/nodes/worker/heartbeat", headers=worker_headers
    ).status_code == 200
    assert client.post(
        "/nodes/worker/drain", headers=worker_headers
    ).status_code == 200


@pytest.mark.parametrize(
    "overrides",
    [
        {"node_id": "x" * 65},
        {"node_id": "not a safe id"},
        {"model": "m" * 97},
        {"hostname": "h" * 254},
        {"capabilities": [f"cap-{index}" for index in range(32)]},
        {"capabilities": ["x" * 65]},
    ],
)
def test_registration_fields_and_capabilities_are_bounded(client, overrides):
    response = client.post(
        "/nodes/register", json=_registration(**overrides)
    )
    assert response.status_code == 422


def test_coordinator_restart_invalidates_process_local_session(client):
    registration = _register(client)
    state._init_db()

    response = client.get(
        "/tasks/next", params={"node_id": "worker"}, headers=_headers(registration)
    )

    assert response.status_code == 401
    assert response.json()["detail"]["action"] == "register_again"
