"""Durable worker enrollment protocol and revocation invariants."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import access_control
from execution.attempts import AcceptedResultBroker, AttemptStore
from node_enrollments import (
    EnrollmentAuthenticationFailed,
    EnrollmentCredentialConflict,
    EnrollmentCredentialRotated,
    EnrollmentLabelConflict,
    EnrollmentRevoked,
    EnrollmentRotationConflict,
    NodeEnrollmentStore,
)
import routes_nodes
from server import app
import server_state as state


ADMISSION_SECRET = "bootstrap-admission-secret-that-is-long-enough"
CREDENTIAL_A = "enrollment-credential-A-0123456789abcdef"
CREDENTIAL_B = "enrollment-credential-B-0123456789abcdef"
CREDENTIAL_C = "enrollment-credential-C-0123456789abcdef"


def _registration(node_id: str, **extra) -> dict:
    return {
        "node_id": node_id,
        "model": "qwen3.5:4b",
        "platform": "Linux",
        "machine": "x86_64",
        "hostname": node_id,
        **extra,
    }


def _bootstrap(client: TestClient, node_id: str, credential: str):
    return client.post(
        "/nodes/register",
        json=_registration(
            node_id,
            enrollment_action="bootstrap",
            enrollment_credential=credential,
        ),
        headers={"X-Node-Secret": ADMISSION_SECRET},
    )


def _returning(client: TestClient, node_id: str, credential: str):
    return client.post(
        "/nodes/register",
        json=_registration(
            node_id,
            enrollment_action="returning",
            enrollment_credential=credential,
        ),
    )


def _session_headers(response) -> dict[str, str]:
    return {"X-Node-Session": response.json()["session_token"]}


def _queue_task(task_id: str) -> None:
    state.task_queue.append(
        {
            "task_id": task_id,
            "title": task_id,
            "prompt": "complete it",
            "system": "system",
            "contract_version": "1",
            "execution_id": "e" * 32,
            "execution_unit_id": f"candidate-{task_id}",
            "execution_unit_kind": "candidate",
            "max_output_bytes": 1024,
        }
    )


def _result_body(task: dict, node_id: str) -> dict:
    return {
        "node_id": node_id,
        "output": "done",
        "elapsed_seconds": 1,
        "contract_version": task["contract_version"],
        "attempt_id": task["attempt_id"],
        "nonce": task["nonce"],
        "execution_id": task["execution_id"],
        "execution_unit_id": task["execution_unit_id"],
        "execution_unit_kind": task["execution_unit_kind"],
    }


@pytest.fixture
def enrolled_server(tmp_path, monkeypatch):
    db_path = tmp_path / "events.db"
    attempt_store = AttemptStore(db_path)
    enrollment_store = NodeEnrollmentStore(db_path)
    broker = AcceptedResultBroker(attempt_store)
    monkeypatch.setattr(state, "_DB_PATH", db_path)
    monkeypatch.setattr(state, "attempt_store", attempt_store)
    monkeypatch.setattr(state, "enrollment_store", enrollment_store)
    monkeypatch.setattr(state, "accepted_result_broker", broker)
    monkeypatch.setattr(state, "_LONG_POLL_TIMEOUT", 0.01)
    monkeypatch.setattr(routes_nodes, "sync_compatibility_ledger", lambda: None)
    for value in (
        state.nodes,
        state.task_queue,
        state.task_inflight,
        state.task_results,
        state.node_failure_count,
        state.node_blacklist,
        state.waiting_nodes,
        state.pipeline_events,
    ):
        value.clear()
    state.node_sessions.reset()

    settings = {
        "node_secret": ADMISSION_SECRET,
        "node_enrollment_mode": "required",
        "viewer_key": "",
    }
    with TestClient(app) as client:
        monkeypatch.setattr(state, "get_config", lambda: settings)
        monkeypatch.setattr(access_control, "get_config", lambda: settings)
        yield client, settings, db_path


def test_store_is_idempotent_secret_free_and_conflict_stable(tmp_path):
    path = tmp_path / "enrollments.db"
    store = NodeEnrollmentStore(path)
    store.migrate()
    store.migrate()

    first = store.bootstrap("worker-a", CREDENTIAL_A, now=10)
    retry = store.bootstrap("worker-a", CREDENTIAL_A, now=11)
    assert first.created is True
    assert retry.idempotent is True
    assert retry.record.enrollment_id == first.record.enrollment_id
    assert store.count() == 1
    assert CREDENTIAL_A not in repr(first)
    assert CREDENTIAL_A.encode() not in path.read_bytes()
    assert "credential" not in first.record.public_metadata()

    with pytest.raises(EnrollmentLabelConflict):
        store.bootstrap("worker-a", CREDENTIAL_B)
    with pytest.raises(EnrollmentCredentialConflict):
        store.bootstrap("worker-b", CREDENTIAL_A)

    revoked_once = store.revoke(first.record.enrollment_id, "operator request", now=12)
    revoked_twice = store.revoke(first.record.enrollment_id, "ignored retry", now=13)
    assert revoked_once.status == revoked_twice.status == "revoked"
    assert revoked_once.revoked_at == revoked_twice.revoked_at == 12
    with pytest.raises(EnrollmentRevoked):
        store.authenticate("worker-a", CREDENTIAL_A)


def test_rotation_is_retry_safe_and_preserves_enrollment_id(tmp_path):
    store = NodeEnrollmentStore(tmp_path / "rotate.db")
    original = store.bootstrap("worker", CREDENTIAL_A).record

    rotated = store.rotate(
        original.enrollment_id,
        CREDENTIAL_B,
        expected_credential_version=1,
        now=20,
    )
    ambiguous_commit_retry = store.rotate(
        original.enrollment_id,
        CREDENTIAL_B,
        expected_credential_version=1,
        now=21,
    )

    assert rotated.record.enrollment_id == original.enrollment_id
    assert rotated.record.credential_version == 2
    assert ambiguous_commit_retry.idempotent is True
    assert ambiguous_commit_retry.record.credential_version == 2
    with pytest.raises(EnrollmentRotationConflict):
        store.rotate(
            original.enrollment_id,
            CREDENTIAL_C,
            expected_credential_version=1,
        )
    with pytest.raises(EnrollmentAuthenticationFailed):
        store.authenticate("worker", CREDENTIAL_A)
    assert store.authenticate("worker", CREDENTIAL_B).enrollment_id == original.enrollment_id
    with pytest.raises(EnrollmentCredentialRotated):
        store.validate_session(original.enrollment_id, "worker", 1)


def test_bootstrap_conflicts_and_shared_secret_cannot_claim_enrollment(enrolled_server):
    client, _settings, _path = enrolled_server
    first = _bootstrap(client, "worker-a", CREDENTIAL_A)
    assert first.status_code == 200
    enrollment_id = first.json()["enrollment_id"]
    assert first.json()["enrolled"] is True
    session = state.node_sessions.current("worker-a")
    assert session is not None
    assert session.node_id == "worker-a"
    assert session.enrollment_id == enrollment_id
    assert session.credential_version == 1
    assert session.session_id == first.json()["session_id"]
    assert len(state.node_sessions) == 1

    retry = _bootstrap(client, "worker-a", CREDENTIAL_A)
    assert retry.status_code == 200
    assert retry.json()["enrollment_id"] == enrollment_id
    assert retry.json()["enrollment_idempotent"] is True
    assert state.enrollment_store.count() == 1

    label_conflict = _bootstrap(client, "worker-a", CREDENTIAL_B)
    credential_conflict = _bootstrap(client, "worker-b", CREDENTIAL_A)
    assert label_conflict.status_code == 409
    assert label_conflict.json()["detail"]["code"] == "node_enrollment_label_conflict"
    assert credential_conflict.status_code == 409
    assert credential_conflict.json()["detail"]["code"] == "node_enrollment_credential_conflict"

    shared_secret_only = client.post(
        "/nodes/register",
        json=_registration("worker-a"),
        headers={"X-Node-Secret": ADMISSION_SECRET},
    )
    assert shared_secret_only.status_code == 426
    assert shared_secret_only.json()["detail"]["code"] == "durable_node_enrollment_required"
    assert state.enrollment_store.count() == 1


def test_restart_invalidates_session_but_returning_needs_no_shared_secret(enrolled_server):
    client, _settings, _path = enrolled_server
    first = _bootstrap(client, "worker", CREDENTIAL_A)
    old_headers = _session_headers(first)
    enrollment_id = first.json()["enrollment_id"]

    state._init_db()
    assert state.enrollment_store.get(enrollment_id) is not None
    assert client.post("/nodes/worker/heartbeat", headers=old_headers).status_code == 401

    returned = _returning(client, "worker", CREDENTIAL_A)
    assert returned.status_code == 200
    assert returned.json()["enrollment_id"] == enrollment_id
    assert returned.json()["session_id"] != first.json()["session_id"]
    assert client.post(
        "/nodes/worker/heartbeat", headers=_session_headers(returned)
    ).status_code == 200


def test_enrolled_normal_operations_need_only_the_session(enrolled_server):
    client, _settings, path = enrolled_server
    registration = _bootstrap(client, "worker", CREDENTIAL_A)
    headers = _session_headers(registration)
    assert client.post("/nodes/worker/heartbeat", headers=headers).status_code == 200

    _queue_task("task-1")
    task_response = client.get(
        "/tasks/next", params={"node_id": "worker"}, headers=headers
    )
    assert task_response.status_code == 200
    task = task_response.json()
    stream = _result_body(task, "worker")
    stream.pop("output")
    stream.pop("elapsed_seconds")
    stream["tokens"] = "partial"
    assert client.post("/tasks/task-1/stream", json=stream, headers=headers).status_code == 200
    assert client.post(
        "/tasks/task-1/result",
        json=_result_body(task, "worker"),
        headers=headers,
    ).status_code == 200

    with sqlite3.connect(path) as con:
        attempt = con.execute(
            "SELECT assigned_enrollment_id FROM attempts WHERE task_id = 'task-1'"
        ).fetchone()
        contribution = con.execute(
            "SELECT enrollment_id FROM contributions WHERE attempt_id = ?",
            (task["attempt_id"],),
        ).fetchone()
    expected = registration.json()["enrollment_id"]
    assert attempt == (expected,)
    assert contribution == (expected,)
    listed = client.get("/nodes").json()["nodes"][0]
    assert listed["enrollment_id"] == expected


def test_session_replacement_between_poll_check_and_handoff_cannot_strand_attempt(
    enrolled_server, monkeypatch
):
    client, _settings, _path = enrolled_server
    registration = _bootstrap(client, "worker-a", CREDENTIAL_A)
    enrollment_id = registration.json()["enrollment_id"]
    headers = _session_headers(registration)
    _queue_task("task-session-race")
    original_lock = state._task_queue_lock

    class ReplacingLock:
        replaced = False

        def __enter__(self):
            original_lock.acquire()
            if not self.replaced:
                self.replaced = True
                state.node_sessions.register(
                    "worker-a",
                    enrollment_id=enrollment_id,
                    credential_version=1,
                )
            return self

        def __exit__(self, *_exc):
            original_lock.release()

    monkeypatch.setattr(state, "_task_queue_lock", ReplacingLock())
    response = client.get(
        "/tasks/next", params={"node_id": "worker-a"}, headers=headers
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "node_session_rejected"
    assert not state.task_inflight
    assert [task["task_id"] for task in state.task_queue] == ["task-session-race"]


def test_invalid_enrollment_credential_is_not_reflected_by_validation(
    enrolled_server, caplog
):
    client, _settings, _path = enrolled_server
    sentinel = "INVALID ENROLLMENT CREDENTIAL SENTINEL"

    response = client.post(
        "/nodes/register",
        json=_registration(
            "worker-a",
            enrollment_action="bootstrap",
            enrollment_credential=sentinel,
        ),
        headers={"X-Node-Secret": ADMISSION_SECRET},
    )

    assert response.status_code == 422
    assert sentinel not in response.text
    assert sentinel not in caplog.text


def test_concurrent_node_removal_after_settlement_does_not_change_accepted_reply(
    enrolled_server, monkeypatch
):
    client, _settings, path = enrolled_server
    registration = _bootstrap(client, "worker-a", CREDENTIAL_A)
    headers = _session_headers(registration)
    _queue_task("task-post-commit-race")
    task = client.get(
        "/tasks/next", params={"node_id": "worker-a"}, headers=headers
    ).json()
    real_settle = routes_nodes._settle_and_publish

    def settle_then_remove(*args, **kwargs):
        settled = real_settle(*args, **kwargs)
        state.nodes.pop("worker-a", None)
        state.node_sessions.invalidate_node("worker-a")
        return settled

    monkeypatch.setattr(routes_nodes, "_settle_and_publish", settle_then_remove)
    response = client.post(
        "/tasks/task-post-commit-race/result",
        json=_result_body(task, "worker-a"),
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    with sqlite3.connect(path) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM contributions WHERE attempt_id = ?",
            (task["attempt_id"],),
        ).fetchone() == (1,)


def test_post_settlement_mirror_failure_does_not_change_accepted_reply(
    enrolled_server, monkeypatch
):
    client, _settings, _path = enrolled_server
    registration = _bootstrap(client, "worker-a", CREDENTIAL_A)
    headers = _session_headers(registration)
    _queue_task("task-mirror-failure")
    task = client.get(
        "/tasks/next", params={"node_id": "worker-a"}, headers=headers
    ).json()

    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("summary unavailable")

    monkeypatch.setattr(routes_nodes, "_lifetime_summary", unavailable)
    response = client.post(
        "/tasks/task-mirror-failure/result",
        json=_result_body(task, "worker-a"),
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert any(
        event["type"] == "post_settlement_mirror_failed"
        and event["enrollment_id"] == registration.json()["enrollment_id"]
        for event in state.pipeline_events
    )


def test_enrollment_revalidation_error_is_diagnosed_without_stopping_cleanup(
    enrolled_server, monkeypatch
):
    client, _settings, _path = enrolled_server
    registration = _bootstrap(client, "worker-a", CREDENTIAL_A)
    enrollment_id = registration.json()["enrollment_id"]
    state.nodes["stale-legacy"] = {
        "node_id": "stale-legacy",
        "last_seen": 0,
        "session_id": None,
        "enrollment_id": None,
    }

    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("database temporarily unavailable")

    monkeypatch.setattr(state.enrollment_store, "validate_session", unavailable)
    state._cleanup_pass()

    assert "worker-a" in state.nodes
    assert "stale-legacy" not in state.nodes
    diagnostic = next(
        event
        for event in state.pipeline_events
        if event["type"] == "enrollment_revalidation_failed"
    )
    assert diagnostic["node_id"] == "worker-a"
    assert diagnostic["enrollment_id"] == enrollment_id
    assert diagnostic["error_type"] == "OperationalError"


def test_revocation_is_isolated_rejects_operations_and_reclaims_work(enrolled_server):
    client, _settings, _path = enrolled_server
    a = _bootstrap(client, "worker-a", CREDENTIAL_A)
    b = _bootstrap(client, "worker-b", CREDENTIAL_B)
    _queue_task("task-a")
    task = client.get(
        "/tasks/next", params={"node_id": "worker-a"}, headers=_session_headers(a)
    ).json()

    state.enrollment_store.revoke(a.json()["enrollment_id"], "compromised")
    rejected = client.post("/nodes/worker-a/heartbeat", headers=_session_headers(a))
    assert rejected.status_code == 403
    assert rejected.json()["detail"]["code"] == "node_enrollment_revoked"
    assert client.get(
        "/tasks/next", params={"node_id": "worker-a"}, headers=_session_headers(a)
    ).status_code == 401
    assert client.post("/nodes/worker-a/drain", headers=_session_headers(a)).status_code == 401
    assert _returning(client, "worker-a", CREDENTIAL_A).status_code == 403
    assert client.post(
        "/tasks/task-a/result",
        json=_result_body(task, "worker-a"),
        headers=_session_headers(a),
    ).status_code == 401

    attempt = state.attempt_store.get(task["attempt_id"])
    assert attempt is not None and attempt.state == "reclaimed"
    assert state.task_queue[0]["task_id"] == "task-a"
    assert "assigned_enrollment_id" not in state.task_queue[0]
    assert client.post(
        "/nodes/worker-b/heartbeat", headers=_session_headers(b)
    ).status_code == 200

    stream_worker = _bootstrap(client, "worker-stream", CREDENTIAL_C)
    _queue_task("task-stream")
    stream_task = client.get(
        "/tasks/next",
        params={"node_id": "worker-stream"},
        headers=_session_headers(stream_worker),
    ).json()
    state.enrollment_store.revoke(
        stream_worker.json()["enrollment_id"], "stream credential compromised"
    )
    stream_body = _result_body(stream_task, "worker-stream")
    stream_body.pop("output")
    stream_body.pop("elapsed_seconds")
    stream_body["tokens"] = "must not be accepted"
    assert client.post(
        "/tasks/task-stream/stream",
        json=stream_body,
        headers=_session_headers(stream_worker),
    ).status_code == 403
    assert state.attempt_store.get(stream_task["attempt_id"]).state == "reclaimed"


@pytest.mark.parametrize("operation", ["heartbeat", "poll", "drain", "stream", "result"])
def test_each_worker_operation_observes_revocation_before_session_eviction(
    enrolled_server, operation
):
    client, _settings, _path = enrolled_server
    registration = _bootstrap(client, "worker", CREDENTIAL_A)
    headers = _session_headers(registration)
    task = None
    if operation in {"stream", "result"}:
        _queue_task(f"task-{operation}")
        task = client.get(
            "/tasks/next", params={"node_id": "worker"}, headers=headers
        ).json()

    state.enrollment_store.revoke(
        registration.json()["enrollment_id"], f"reject {operation}"
    )

    if operation == "heartbeat":
        response = client.post("/nodes/worker/heartbeat", headers=headers)
    elif operation == "poll":
        response = client.get(
            "/tasks/next", params={"node_id": "worker"}, headers=headers
        )
    elif operation == "drain":
        response = client.post("/nodes/worker/drain", headers=headers)
    elif operation == "stream":
        body = _result_body(task, "worker")
        body.pop("output")
        body.pop("elapsed_seconds")
        body["tokens"] = "must not stream"
        response = client.post(
            f"/tasks/{task['task_id']}/stream", json=body, headers=headers
        )
    else:
        response = client.post(
            f"/tasks/{task['task_id']}/result",
            json=_result_body(task, "worker"),
            headers=headers,
        )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "node_enrollment_revoked"


def test_rotation_invalidates_live_session_and_old_registration_credential(
    enrolled_server,
):
    client, _settings, _path = enrolled_server
    registration = _bootstrap(client, "worker", CREDENTIAL_A)
    enrollment_id = registration.json()["enrollment_id"]

    rotated = state.enrollment_store.rotate(enrollment_id, CREDENTIAL_B)

    assert rotated.record.enrollment_id == enrollment_id
    rejected = client.post(
        "/nodes/worker/heartbeat", headers=_session_headers(registration)
    )
    assert rejected.status_code == 401
    assert rejected.json()["detail"]["code"] == "node_enrollment_credential_rotated"
    assert _returning(client, "worker", CREDENTIAL_A).status_code == 401
    returned = _returning(client, "worker", CREDENTIAL_B)
    assert returned.status_code == 200
    assert returned.json()["enrollment_id"] == enrollment_id


def test_enrollment_b_cannot_settle_enrollment_a_attempt(enrolled_server):
    client, _settings, _path = enrolled_server
    a = _bootstrap(client, "worker-a", CREDENTIAL_A)
    b = _bootstrap(client, "worker-b", CREDENTIAL_B)
    _queue_task("task-cross")
    task = client.get(
        "/tasks/next", params={"node_id": "worker-a"}, headers=_session_headers(a)
    ).json()

    stolen = client.post(
        "/tasks/task-cross/result",
        json=_result_body(task, "worker-b"),
        headers=_session_headers(b),
    )
    assert stolen.status_code == 403
    assert state.attempt_store.get(task["attempt_id"]).state == "active"
    assert client.post(
        "/tasks/task-cross/result",
        json=_result_body(task, "worker-a"),
        headers=_session_headers(a),
    ).status_code == 200


def test_enrolled_exact_replay_survives_fresh_session_and_pays_once(
    enrolled_server,
):
    client, _settings, path = enrolled_server
    first = _bootstrap(client, "worker-a", CREDENTIAL_A)
    other = _bootstrap(client, "worker-b", CREDENTIAL_B)
    _queue_task("task-replay")
    task = client.get(
        "/tasks/next", params={"node_id": "worker-a"}, headers=_session_headers(first)
    ).json()
    result = _result_body(task, "worker-a")

    accepted = client.post(
        "/tasks/task-replay/result", json=result, headers=_session_headers(first)
    )
    returned = _returning(client, "worker-a", CREDENTIAL_A)
    replayed = client.post(
        "/tasks/task-replay/result", json=result, headers=_session_headers(returned)
    )
    stolen = client.post(
        "/tasks/task-replay/result",
        json={**result, "node_id": "worker-b"},
        headers=_session_headers(other),
    )

    assert accepted.status_code == replayed.status_code == 200
    assert accepted.json() == replayed.json()
    assert returned.json()["session_id"] != first.json()["session_id"]
    assert stolen.status_code == 403
    with sqlite3.connect(path) as con:
        receipts = con.execute(
            "SELECT COUNT(*) FROM accepted_result_receipts WHERE attempt_id = ?",
            (task["attempt_id"],),
        ).fetchone()[0]
        contributions = con.execute(
            "SELECT COUNT(*) FROM contributions WHERE attempt_id = ?",
            (task["attempt_id"],),
        ).fetchone()[0]
    assert receipts == contributions == 1


def test_compat_is_explicit_and_cannot_masquerade_as_enrollment(enrolled_server):
    client, settings, _path = enrolled_server
    settings["node_enrollment_mode"] = "compat"
    durable = _bootstrap(client, "durable", CREDENTIAL_A)
    legacy = client.post(
        "/nodes/register",
        json=_registration("legacy"),
        headers={"X-Node-Secret": ADMISSION_SECRET},
    )
    assert legacy.status_code == 200
    assert legacy.json()["enrolled"] is False
    assert legacy.json()["enrollment_id"] is None
    assert legacy.json()["enrollment_action"] == "legacy_compat"
    assert state.node_sessions.current("legacy").enrollment_id is None

    takeover = client.post(
        "/nodes/register",
        json=_registration("durable"),
        headers={"X-Node-Secret": ADMISSION_SECRET},
    )
    assert takeover.status_code == 409
    assert takeover.json()["detail"]["code"] == "node_enrollment_label_conflict"
    assert state.enrollment_store.get(durable.json()["enrollment_id"]).status == "active"


def test_operator_listing_is_protected_and_contains_no_credential_material(
    enrolled_server, caplog
):
    client, settings, path = enrolled_server
    registration = _bootstrap(client, "worker", CREDENTIAL_C)
    settings["viewer_key"] = "viewer-secret-that-is-long-enough-for-tests"

    assert client.get("/v1/operator/node-enrollments").status_code == 401
    response = client.get(
        "/v1/operator/node-enrollments",
        headers={"Authorization": f"Bearer {settings['viewer_key']}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["enrollments"][0]["enrollment_id"] == registration.json()["enrollment_id"]
    assert body["enrollments"][0]["live_session_present"] is True
    serialized = response.text.lower()
    assert CREDENTIAL_C.lower() not in serialized
    assert "credential_digest" not in serialized
    assert "enrollment_credential" not in serialized
    assert CREDENTIAL_C not in caplog.text
    assert CREDENTIAL_C not in str(state.pipeline_events)
    assert CREDENTIAL_C.encode() not in path.read_bytes()

    conflict = _bootstrap(client, "worker", CREDENTIAL_B)
    assert conflict.status_code == 409
    assert CREDENTIAL_B not in conflict.text
    assert CREDENTIAL_B not in caplog.text
    assert CREDENTIAL_B not in str(state.pipeline_events)
    with sqlite3.connect(path) as con:
        sql_export = "\n".join(con.iterdump())
    assert CREDENTIAL_C not in sql_export
    assert CREDENTIAL_B not in sql_export
