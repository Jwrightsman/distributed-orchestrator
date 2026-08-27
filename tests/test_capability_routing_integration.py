"""Server integration for typed capability claims and assignment snapshots."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import access_control
from execution.attempts import AcceptedResultBroker, AttemptStore
from execution.contracts import ExecutionRequestV1
from execution.dispatch import qualifying_nodes
import node_capabilities
from node_capabilities import (
    ExecutorDescriptorV1,
    HardwareDescriptorV1,
    IsolationDescriptorV1,
    ModelDescriptorV1,
    NodeCapabilityDescriptorV1,
    NodeCapabilitySnapshotStore,
    NodeLimitDescriptorV1,
    NodeResourceRequirementsV1,
)
from node_enrollments import NodeEnrollmentStore
import routes_nodes
from server import app
import server_state as state


ADMISSION_SECRET = "capability-bootstrap-admission-secret"
ENROLLMENT_CREDENTIAL = "capability-enrollment-credential-0123456789"
VIEWER_KEY = "capability-viewer-secret"


def _descriptor(
    *,
    model: str = "qwen3.5:4b",
    memory_bytes: int = 16 * 1024**3,
    max_output_bytes: int = 1_048_576,
) -> NodeCapabilityDescriptorV1:
    return NodeCapabilityDescriptorV1(
        executor=ExecutorDescriptorV1(
            kind="ollama",
            version="0.11.0",
            worker_protocol_version="1",
        ),
        models=[
            ModelDescriptorV1(
                provider="ollama",
                name=model,
                context_tokens=32_768,
            )
        ],
        hardware=HardwareDescriptorV1(
            architecture="x86_64",
            logical_cpu_count=8,
            total_memory_bytes=memory_bytes,
            gpus=None,
        ),
        features=["code"],
        limits=NodeLimitDescriptorV1(
            max_concurrent_execution_units=1,
            max_output_bytes=max_output_bytes,
            max_context_tokens=32_768,
        ),
        isolation=IsolationDescriptorV1(kind="none"),
    )


def _registration(descriptor: NodeCapabilityDescriptorV1, *, action: str) -> dict:
    return {
        "node_id": "worker",
        "enrollment_action": action,
        "enrollment_credential": ENROLLMENT_CREDENTIAL,
        "model": descriptor.models[0].name,
        "platform": "Linux",
        "machine": "x86_64",
        "hostname": "worker-host",
        "capabilities": ["legacy-code"],
        "capability_descriptor": descriptor.model_dump(mode="json"),
    }


def _bootstrap(client: TestClient, descriptor: NodeCapabilityDescriptorV1):
    return client.post(
        "/nodes/register",
        json=_registration(descriptor, action="bootstrap"),
        headers={"X-Node-Secret": ADMISSION_SECRET},
    )


def _returning(
    client: TestClient,
    descriptor: NodeCapabilityDescriptorV1,
    *,
    session_token: str | None = None,
):
    headers = {"X-Node-Session": session_token} if session_token else {}
    return client.post(
        "/nodes/register",
        json=_registration(descriptor, action="returning"),
        headers=headers,
    )


def _queue_v1(
    task_id: str,
    *,
    requirements=None,
    max_output_bytes: int = 1024,
) -> None:
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
            "max_output_bytes": max_output_bytes,
            "requires": ["legacy-code"],
            "resource_requirements": (
                requirements.model_dump(mode="json")
                if requirements is not None
                else None
            ),
            "eligible_nodes": ["worker"],
        }
    )


@pytest.fixture
def capability_server(tmp_path, monkeypatch):
    db_path = tmp_path / "events.db"
    attempt_store = AttemptStore(db_path)
    enrollment_store = NodeEnrollmentStore(db_path)
    snapshot_store = NodeCapabilitySnapshotStore(db_path)
    broker = AcceptedResultBroker(attempt_store)
    monkeypatch.setattr(state, "_DB_PATH", db_path)
    monkeypatch.setattr(state, "attempt_store", attempt_store)
    monkeypatch.setattr(state, "enrollment_store", enrollment_store)
    monkeypatch.setattr(state, "capability_snapshot_store", snapshot_store)
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
        "viewer_key": VIEWER_KEY,
    }
    monkeypatch.setattr(state, "get_config", lambda: settings)
    monkeypatch.setattr(access_control, "get_config", lambda: settings)
    with TestClient(app) as client:
        yield client, db_path


def test_descriptor_is_immutable_in_session_but_new_session_gets_new_snapshot(
    capability_server,
):
    client, _path = capability_server
    descriptor_a = _descriptor()
    descriptor_b = _descriptor(memory_bytes=32 * 1024**3)
    first = _bootstrap(client, descriptor_a)
    assert first.status_code == 200
    first_body = first.json()
    snapshots_before = state.capability_snapshot_store.list_for_enrollment(
        first_body["enrollment_id"]
    )
    assert len(snapshots_before) == 1

    same_session_change = _returning(
        client,
        descriptor_b,
        session_token=first_body["session_token"],
    )
    assert same_session_change.status_code == 409
    assert (
        same_session_change.json()["detail"]["code"]
        == "node_capability_descriptor_conflict"
    )
    assert state.node_sessions.current("worker").session_id == first_body["session_id"]
    snapshots_after_rejection = state.capability_snapshot_store.list_for_enrollment(
        first_body["enrollment_id"]
    )
    assert len(snapshots_after_rejection) == 1
    assert snapshots_after_rejection[0].descriptor_hash == snapshots_before[0].descriptor_hash
    assert snapshots_after_rejection[0].last_seen_at == snapshots_before[0].last_seen_at

    replacement = _returning(client, descriptor_b)
    assert replacement.status_code == 200
    assert replacement.json()["session_id"] != first_body["session_id"]
    assert replacement.json()["capability_descriptor_hash"] != first_body[
        "capability_descriptor_hash"
    ]
    snapshots = state.capability_snapshot_store.list_for_enrollment(
        first_body["enrollment_id"]
    )
    assert len(snapshots) == 2


def test_enrolled_registration_requires_a_typed_descriptor_without_side_effects(
    capability_server,
):
    client, _path = capability_server
    descriptor = _descriptor()
    bootstrap_payload = _registration(descriptor, action="bootstrap")
    bootstrap_payload.pop("capability_descriptor")

    bootstrap = client.post(
        "/nodes/register",
        json=bootstrap_payload,
        headers={"X-Node-Secret": ADMISSION_SECRET},
    )

    assert bootstrap.status_code == 426
    assert bootstrap.json()["detail"] == {
        "code": "node_capability_descriptor_required",
        "message": (
            "Durably enrolled workers must submit a typed capability descriptor. "
            "Upgrade the worker and register again."
        ),
        "action": "upgrade_worker",
    }
    assert state.enrollment_store.get_by_node("worker") is None

    registered = _bootstrap(client, descriptor)
    assert registered.status_code == 200
    returning_payload = _registration(descriptor, action="returning")
    returning_payload.pop("capability_descriptor")
    returning = client.post("/nodes/register", json=returning_payload)
    assert returning.status_code == 426
    assert returning.json()["detail"]["code"] == (
        "node_capability_descriptor_required"
    )


def test_registration_rejects_a_contradictory_legacy_model_projection(
    capability_server,
):
    client, _path = capability_server
    descriptor = _descriptor(model="descriptor-model:4b")
    payload = _registration(descriptor, action="bootstrap")
    payload["model"] = "different-legacy-model:4b"

    response = client.post(
        "/nodes/register",
        json=payload,
        headers={"X-Node-Secret": ADMISSION_SECRET},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == (
        "node_capability_descriptor_model_mismatch"
    )


def test_scheduler_and_polling_both_use_the_shared_matcher(
    capability_server, monkeypatch
):
    client, _path = capability_server
    registration = _bootstrap(client, _descriptor())
    assert registration.status_code == 200
    headers = {"X-Node-Session": registration.json()["session_token"]}
    mismatching = NodeResourceRequirementsV1(
        acceptable_models=[{"provider": "ollama", "name": "other:4b"}]
    )
    request = ExecutionRequestV1(
        task="build it",
        placement="distributed",
        confidentiality="trusted_guild",
        remote_dispatch_consent=True,
        requirements={"resource_requirements": mismatching.model_dump(mode="json")},
    )

    original_matcher = node_capabilities.match_node_requirements
    calls: list[str] = []

    def recording_matcher(*args, **kwargs):
        calls.append("match")
        return original_matcher(*args, **kwargs)

    monkeypatch.setattr(node_capabilities, "match_node_requirements", recording_matcher)
    assert qualifying_nodes(request) == []

    _queue_v1("typed-mismatch", requirements=mismatching)
    response = client.get(
        "/tasks/next",
        params={"node_id": "worker"},
        headers=headers,
    )
    assert response.status_code == 204
    assert [task["task_id"] for task in state.task_queue] == ["typed-mismatch"]
    assert len(calls) >= 2


def test_output_capacity_is_shared_by_scheduler_poll_and_operator_diagnostics(
    capability_server,
):
    client, _path = capability_server
    descriptor = _descriptor(max_output_bytes=2048)
    registration = _bootstrap(client, descriptor)
    assert registration.status_code == 200
    headers = {"X-Node-Session": registration.json()["session_token"]}
    request = ExecutionRequestV1(
        task="build it",
        placement="distributed",
        confidentiality="trusted_guild",
        remote_dispatch_consent=True,
        max_output_bytes=4096,
    )

    assert qualifying_nodes(request) == []

    _queue_v1("too-large", max_output_bytes=4096)
    response = client.get(
        "/tasks/next",
        params={"node_id": "worker"},
        headers=headers,
    )
    assert response.status_code == 204
    assert [task["task_id"] for task in state.task_queue] == ["too-large"]
    assert state.attempt_store.active_for_task("too-large") is None

    operator = client.get(
        "/v1/operator/node-enrollments",
        params={"required_output_capacity_bytes": 4096},
        headers={"Authorization": f"Bearer {VIEWER_KEY}"},
    )
    assert operator.status_code == 200
    diagnostic = operator.json()["enrollments"][0][
        "hard_requirement_eligibility"
    ]
    assert diagnostic == {
        "eligible": False,
        "reason_codes": ["insufficient_output_capacity"],
        "matched_descriptor_hash": registration.json()[
            "capability_descriptor_hash"
        ],
        "selected_model": None,
    }


def test_under_lock_handout_recheck_rejects_stale_output_capacity_precheck(
    capability_server, monkeypatch
):
    client, _path = capability_server
    descriptor = _descriptor(max_output_bytes=2048)
    registration = _bootstrap(client, descriptor)
    assert registration.status_code == 200
    _queue_v1("capacity-race", max_output_bytes=1024)
    monkeypatch.setattr(state, "_LONG_POLL_TIMEOUT", 0)
    original_matcher = node_capabilities.match_node_requirements
    calls = 0

    def mutate_after_precheck(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_matcher(*args, **kwargs)
        if calls == 1:
            state.task_queue[0]["max_output_bytes"] = 4096
        return result

    monkeypatch.setattr(
        node_capabilities,
        "match_node_requirements",
        mutate_after_precheck,
    )

    response = client.get(
        "/tasks/next",
        params={"node_id": "worker"},
        headers={"X-Node-Session": registration.json()["session_token"]},
    )

    assert response.status_code == 204
    assert calls >= 2
    assert state.task_queue[0]["max_output_bytes"] == 4096
    assert state.attempt_store.active_for_task("capacity-race") is None


def test_larger_worker_claim_cannot_raise_server_attempt_output_limit(
    capability_server,
):
    client, _path = capability_server
    descriptor = _descriptor(max_output_bytes=10_485_760)
    registration = _bootstrap(client, descriptor)
    assert registration.status_code == 200
    _queue_v1("authoritative-budget", max_output_bytes=4097)

    handout = client.get(
        "/tasks/next",
        params={"node_id": "worker"},
        headers={"X-Node-Session": registration.json()["session_token"]},
    )

    assert handout.status_code == 200
    assert handout.json()["max_output_bytes"] == 4097
    attempt = state.attempt_store.get(handout.json()["attempt_id"])
    assert attempt is not None
    assert attempt.max_output_bytes == 4097

    attempted_raise = client.post(
        "/tasks/authoritative-budget/result",
        json={
            "node_id": "worker",
            "output": "x" * 4098,
            "error": None,
            "elapsed_seconds": 1,
            "contract_version": handout.json()["contract_version"],
            "attempt_id": handout.json()["attempt_id"],
            "nonce": handout.json()["nonce"],
            "execution_id": handout.json()["execution_id"],
            "execution_unit_id": handout.json()["execution_unit_id"],
            "execution_unit_kind": handout.json()["execution_unit_kind"],
            "max_output_bytes": 10_485_760,
        },
        headers={"X-Node-Session": registration.json()["session_token"]},
    )
    assert attempted_raise.status_code == 413
    assert attempted_raise.json()["max_bytes"] == 4097
    assert state.attempt_store.get(handout.json()["attempt_id"]).max_output_bytes == (
        4097
    )


def test_handout_binds_the_exact_matching_model_from_a_multi_model_descriptor(
    capability_server,
):
    client, _path = capability_server
    configured_digest = "sha256:" + "a" * 64
    selected_digest = "sha256:" + "b" * 64
    base = _descriptor()
    descriptor = NodeCapabilityDescriptorV1.model_validate(
        {
            **base.model_dump(mode="json"),
            "models": [
                {
                    "provider": "ollama",
                    "name": "configured:latest",
                    "digest": configured_digest,
                    "context_tokens": 16_384,
                },
                {
                    "provider": "ollama",
                    "name": "selected:latest",
                    "digest": selected_digest,
                    "context_tokens": 32_768,
                },
            ],
        }
    )
    payload = _registration(descriptor, action="bootstrap")
    payload["model"] = "configured:latest"
    registration = client.post(
        "/nodes/register",
        json=payload,
        headers={"X-Node-Secret": ADMISSION_SECRET},
    )
    assert registration.status_code == 200
    requirements = NodeResourceRequirementsV1(
        acceptable_models=[
            {"provider": "ollama", "name": "configured:latest"},
            {"provider": "ollama", "name": "selected:latest"},
        ],
        exact_model_digest=selected_digest,
        minimum_context_tokens=20_000,
    )
    _queue_v1("multi-model", requirements=requirements)

    handout = client.get(
        "/tasks/next",
        params={"node_id": "worker"},
        headers={"X-Node-Session": registration.json()["session_token"]},
    )

    assert handout.status_code == 200
    assert handout.json()["selected_model"] == {
        "provider": "ollama",
        "name": "selected:latest",
        "digest": selected_digest,
    }


def test_attempt_receipt_and_operator_diagnostics_bind_descriptor_and_requirements(
    capability_server,
):
    client, _path = capability_server
    descriptor = _descriptor()
    registration = _bootstrap(client, descriptor)
    assert registration.status_code == 200
    registered = registration.json()
    session_headers = {"X-Node-Session": registered["session_token"]}
    requirements = NodeResourceRequirementsV1(minimum_logical_cpus=4)
    _queue_v1("bound-task", requirements=requirements)

    handout = client.get(
        "/tasks/next",
        params={"node_id": "worker"},
        headers=session_headers,
    )
    assert handout.status_code == 200
    task = handout.json()
    expected_requirement_digest = node_capabilities.canonical_requirement_digest(
        requirements, ["legacy-code"]
    )
    attempt = state.attempt_store.get(task["attempt_id"])
    assert attempt is not None
    assert attempt.assigned_descriptor_version == "1"
    assert attempt.assigned_descriptor_hash == registered[
        "capability_descriptor_hash"
    ]
    assert attempt.requirement_digest == expected_requirement_digest

    result = {
        "node_id": "worker",
        "output": "done",
        "elapsed_seconds": 1,
        "contract_version": task["contract_version"],
        "attempt_id": task["attempt_id"],
        "nonce": task["nonce"],
        "execution_id": task["execution_id"],
        "execution_unit_id": task["execution_unit_id"],
        "execution_unit_kind": task["execution_unit_kind"],
    }
    assert client.post(
        "/tasks/bound-task/result", json=result, headers=session_headers
    ).status_code == 200
    receipt = state.attempt_store.get_receipt_for_task("bound-task")
    assert receipt is not None
    assert receipt.assigned_descriptor_hash == attempt.assigned_descriptor_hash
    assert receipt.requirement_digest == expected_requirement_digest
    assert receipt.as_legacy_result()["capability_descriptor_hash"] == (
        attempt.assigned_descriptor_hash
    )

    viewer_headers = {"Authorization": f"Bearer {VIEWER_KEY}"}
    listing = client.get("/nodes", headers=viewer_headers)
    assert listing.status_code == 200
    assert "capability_descriptor" not in listing.json()["nodes"][0]
    assert listing.json()["nodes"][0]["capability_descriptor_hash"] == (
        attempt.assigned_descriptor_hash
    )

    diagnostic_requirements = NodeResourceRequirementsV1(
        minimum_memory_bytes=64 * 1024**3
    )
    operator = client.get(
        "/v1/operator/node-enrollments",
        params={
            "resource_requirements": json.dumps(
                diagnostic_requirements.model_dump(mode="json")
            )
        },
        headers=viewer_headers,
    )
    assert operator.status_code == 200
    node = operator.json()["enrollments"][0]
    assert node["capability_descriptor"] == descriptor.model_dump(mode="json")
    assert node["hard_requirement_eligibility"]["eligible"] is False
    assert node["hard_requirement_eligibility"]["reason_codes"] == [
        "insufficient_memory"
    ]
    serialized = operator.text.lower()
    assert ENROLLMENT_CREDENTIAL.lower() not in serialized
    assert "session_token" not in serialized
    assert "credential_digest" not in serialized


def test_handout_rejects_a_prepopulated_noncanonical_requirement_digest(
    capability_server,
):
    client, _path = capability_server
    registration = _bootstrap(client, _descriptor())
    assert registration.status_code == 200
    _queue_v1("injected-requirement")
    state.task_queue[-1]["requirement_digest"] = "f" * 64

    response = client.get(
        "/tasks/next",
        params={"node_id": "worker"},
        headers={"X-Node-Session": registration.json()["session_token"]},
    )

    assert response.status_code == 503
    assert state.attempt_store.active_for_task("injected-requirement") is None
    assert [task["task_id"] for task in state.task_queue] == [
        "injected-requirement"
    ]


def test_later_session_cannot_rewrite_an_earlier_attempt_snapshot(
    capability_server,
):
    client, _path = capability_server
    descriptor_a = _descriptor()
    descriptor_b = _descriptor(memory_bytes=32 * 1024**3)
    first = _bootstrap(client, descriptor_a).json()
    first_headers = {"X-Node-Session": first["session_token"]}
    _queue_v1("reassigned")
    first_handout = client.get(
        "/tasks/next", params={"node_id": "worker"}, headers=first_headers
    ).json()

    replacement = _returning(client, descriptor_b)
    assert replacement.status_code == 200
    second = replacement.json()
    second_handout = client.get(
        "/tasks/next",
        params={"node_id": "worker"},
        headers={"X-Node-Session": second["session_token"]},
    ).json()

    old_attempt = state.attempt_store.get(first_handout["attempt_id"])
    new_attempt = state.attempt_store.get(second_handout["attempt_id"])
    assert old_attempt is not None and new_attempt is not None
    assert old_attempt.state == "reclaimed"
    assert old_attempt.assigned_descriptor_hash == first[
        "capability_descriptor_hash"
    ]
    assert new_attempt.assigned_descriptor_hash == second[
        "capability_descriptor_hash"
    ]
    assert old_attempt.assigned_descriptor_hash != new_attempt.assigned_descriptor_hash
