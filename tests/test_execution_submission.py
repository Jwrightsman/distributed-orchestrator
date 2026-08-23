"""Retry-safe canonical execution submission and digest-only persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
import string
from asyncio import sleep
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import execution.service as service_module
import routes_executions
import server_state as state
from execution.contracts import ExecutionRequestV1
from execution.idempotency import (
    InvalidIdempotencyKey,
    SubmissionIdentity,
    canonical_request_digest,
    canonical_request_json,
    submission_identity,
    validate_idempotency_key,
)
from execution.persistence import ExecutionStore, IdempotencyConflictError
from execution.service import ExecutionService, ServiceExecution


def _counts(database) -> tuple[int, int]:
    with sqlite3.connect(database) as con:
        executions = con.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        submissions = con.execute(
            "SELECT COUNT(*) FROM execution_submissions"
        ).fetchone()[0]
    return executions, submissions


def _stub_activation(monkeypatch, service: ExecutionService, activated: list[str], lock=None):
    """Count the one post-commit scheduling boundary without starting model work."""

    def activate(request, queued, **_kwargs):
        if lock is None:
            activated.append(queued.execution_id)
        else:
            with lock:
                activated.append(queued.execution_id)
        service._remember(request, queued)
        return queued

    monkeypatch.setattr(service, "_activate_committed_submission", activate)


def _api(
    monkeypatch,
    service: ExecutionService,
    *,
    pitch_key: str = "",
    peer_host: str = "testclient",
    rate_max: int = 100,
) -> TestClient:
    config = {
        "pitch_key": pitch_key,
        "pitch_rate_max": rate_max,
        "pitch_rate_window": 60,
    }
    monkeypatch.setattr(routes_executions, "get_config", lambda: config)
    monkeypatch.setattr(state, "get_config", lambda: config)
    monkeypatch.setattr(routes_executions, "get_execution_service", lambda: service)
    state._pitch_timestamps.clear()
    app = FastAPI()
    app.include_router(routes_executions.router)
    return TestClient(app, client=(peer_host, 50000))


def test_canonical_request_digest_ignores_json_object_key_order():
    first = ExecutionRequestV1.model_validate(
        {
            "task": "Return one JSON object",
            "strategy": "direct",
            "output_contract": {
                "kind": "structured_json",
                "json_schema": {
                    "type": "object",
                    "properties": {
                        "alpha": {"type": "string"},
                        "beta": {"type": "integer"},
                    },
                },
            },
        }
    )
    reordered = ExecutionRequestV1.model_validate(
        {
            "output_contract": {
                "json_schema": {
                    "properties": {
                        "beta": {"type": "integer"},
                        "alpha": {"type": "string"},
                    },
                    "type": "object",
                },
                "kind": "structured_json",
            },
            "strategy": "direct",
            "task": "Return one JSON object",
        }
    )

    assert canonical_request_json(first) == canonical_request_json(reordered)
    assert canonical_request_digest(first) == canonical_request_digest(reordered)
    assert '"timeout_seconds":1800' in canonical_request_json(first)


def test_canonical_request_digest_changes_for_material_request_change():
    first = ExecutionRequestV1(task="Build alpha", strategy="direct")
    changed = ExecutionRequestV1(task="Build beta", strategy="direct")

    assert canonical_request_digest(first) != canonical_request_digest(changed)


@pytest.mark.parametrize("value", ["", "   ", "x" * 129, "control\x1fcharacter", "snowman-☃"])
def test_idempotency_key_validation_rejects_values_outside_printable_ascii(value):
    with pytest.raises(InvalidIdempotencyKey):
        validate_idempotency_key(value)


def test_submission_identity_rejects_plaintext_before_persistence(tmp_path):
    database = tmp_path / "events.db"
    ExecutionStore(database).migrate()

    with pytest.raises(ValueError, match="requester_scope_hash"):
        SubmissionIdentity(
            requester_scope_hash="plaintext-requester-credential",
            idempotency_key_hash="1" * 64,
            request_hash="2" * 64,
        )

    assert _counts(database) == (0, 0)


def test_idempotent_submission_detaches_caller_owned_request(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    captured_requests = []

    def activate(request, queued, **_kwargs):
        captured_requests.append(request)
        service._remember(request, queued)
        return queued

    monkeypatch.setattr(service, "_activate_committed_submission", activate)
    original = ExecutionRequestV1(
        task="Original immutable submission",
        strategy="direct",
    )
    identity = submission_identity(
        original,
        idempotency_key="detached-request-key",
        requester_scope_kind="pitch-key",
        requester_scope_value="detached-requester",
    )

    created = service.submit_idempotent(original, identity)
    original.task = "Mutated after atomic submission"

    pristine = ExecutionRequestV1(
        task="Original immutable submission",
        strategy="direct",
    )
    replayed = service.submit_idempotent(pristine, identity)
    execution_id = created.result.execution_id
    assert captured_requests[0] is not original
    assert captured_requests[0].task == "Original immutable submission"
    assert service._requests[execution_id].task == "Original immutable submission"
    assert service.store.get_request(execution_id).task == "Original immutable submission"
    assert replayed.replayed is True
    assert replayed.result.execution_id == execution_id


def test_fresh_schema_has_scoped_primary_key_index_and_foreign_key(tmp_path):
    database = tmp_path / "events.db"
    ExecutionStore(database).migrate()

    with sqlite3.connect(database) as con:
        columns = con.execute("PRAGMA table_info(execution_submissions)").fetchall()
        indexes = {
            row[1] for row in con.execute("PRAGMA index_list(execution_submissions)")
        }
        foreign_keys = con.execute(
            "PRAGMA foreign_key_list(execution_submissions)"
        ).fetchall()

    by_name = {row[1]: row for row in columns}
    assert set(by_name) == {
        "requester_scope_hash",
        "idempotency_key_hash",
        "request_hash",
        "execution_id",
        "created_at",
    }
    assert by_name["requester_scope_hash"][5] == 1
    assert by_name["idempotency_key_hash"][5] == 2
    assert "idx_execution_submissions_execution_id" in indexes
    assert any(
        row[2] == "executions"
        and row[3] == "execution_id"
        and row[4] == "execution_id"
        for row in foreign_keys
    )


def test_schema_upgrade_preserves_existing_data_and_is_idempotent(tmp_path):
    database = tmp_path / "events.db"
    store = ExecutionStore(database)
    request = ExecutionRequestV1(task="Preserve this execution", strategy="direct")
    service = ExecutionService(store=store)
    existing = service._new_result(request, "e" * 32, "legacy-job", "queued")

    store.migrate()
    store.create(request, existing)
    with sqlite3.connect(database) as con:
        con.execute("DROP TABLE execution_submissions")
        con.execute("CREATE TABLE legacy_probe (value TEXT NOT NULL)")
        con.execute("INSERT INTO legacy_probe VALUES ('preserved')")
        con.commit()

    for _ in range(4):
        ExecutionStore(database).migrate()

    with sqlite3.connect(database) as con:
        submission_tables = con.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='execution_submissions'"
        ).fetchone()[0]
        execution = con.execute(
            "SELECT job_id, status FROM executions WHERE execution_id = ?",
            (existing.execution_id,),
        ).fetchone()
        probe = con.execute("SELECT value FROM legacy_probe").fetchone()[0]

    assert submission_tables == 1
    assert execution == ("legacy-job", "queued")
    assert probe == "preserved"


def test_same_scoped_key_replays_one_execution_and_conflicts_on_change(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    pitch_credential = "trusted-pitch-credential"
    client = _api(monkeypatch, service, pitch_key=pitch_credential)
    headers = {
        "X-Pitch-Key": pitch_credential,
        "Idempotency-Key": "logical-submission-1",
    }

    with client:
        created = client.post(
            "/v1/executions",
            json={"task": "Build once", "strategy": "direct"},
            headers=headers,
        )
        replayed = client.post(
            "/v1/executions",
            json={
                "strategy": "direct",
                "placement": "local",
                "protocol_version": "1",
                "task": "Build once",
            },
            headers=headers,
        )
        conflicted = client.post(
            "/v1/executions",
            json={"task": "Build something different", "strategy": "direct"},
            headers=headers,
        )

    execution_id = created.json()["execution_id"]
    assert created.status_code == 202
    assert created.headers["Idempotency-Replayed"] == "false"
    assert replayed.status_code == 202
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert replayed.json()["execution_id"] == execution_id
    assert conflicted.status_code == 409
    assert conflicted.json()["detail"] == {
        "code": "idempotency_conflict",
        "message": "Idempotency-Key is already bound to a different request.",
        "execution_id": execution_id,
    }
    assert activated == [execution_id]
    assert _counts(database) == (1, 1)
    assert service.store.get_request(execution_id).task == "Build once"


def test_replay_route_runs_auth_rate_limit_and_cross_validation_in_order(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    credential = "ordered-pitch-credential"
    client = _api(monkeypatch, service, pitch_key=credential, rate_max=2)
    headers = {
        "X-Pitch-Key": credential,
        "Idempotency-Key": "ordered-replay",
    }

    with client:
        created = client.post(
            "/v1/executions",
            json={"task": "Ordered replay", "strategy": "direct"},
            headers=headers,
        )
        missing_auth = client.post(
            "/v1/executions",
            json={"task": "Ordered replay", "strategy": "direct"},
            headers={"Idempotency-Key": "ordered-replay"},
        )
        cross_invalid = client.post(
            "/v1/executions",
            json={
                "task": "Ordered replay",
                "strategy": "direct",
                "project_id": "unsupported-direct-project",
            },
            headers=headers,
        )
        rate_limited = client.post(
            "/v1/executions",
            json={"task": "Ordered replay", "strategy": "direct"},
            headers=headers,
        )

    assert created.status_code == 202
    assert missing_auth.status_code == 401
    # Authentication failure occurs before the limiter and therefore does not
    # consume the second permitted request. Cross-component validation then
    # runs before the idempotency lookup, producing 422 rather than 409.
    assert cross_invalid.status_code == 422
    assert "project_id is not supported" in cross_invalid.text
    # The invalid-but-authenticated attempt did consume the second rate slot,
    # so the next otherwise valid replay is rejected before service lookup.
    assert rate_limited.status_code == 429
    assert _counts(database) == (1, 1)
    assert activated == [created.json()["execution_id"]]


def test_configured_pitch_credential_scopes_across_peer_addresses(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    credential = "shared-configured-pitch-key"
    first_peer = _api(
        monkeypatch,
        service,
        pitch_key=credential,
        peer_host="192.0.2.20",
    )
    second_peer = _api(
        monkeypatch,
        service,
        pitch_key=credential,
        peer_host="192.0.2.21",
    )
    headers = {
        "X-Pitch-Key": credential,
        "Idempotency-Key": "credential-scoped-key",
    }

    with first_peer, second_peer:
        created = first_peer.post(
            "/v1/executions",
            json={"task": "Credential scoped", "strategy": "direct"},
            headers=headers,
        )
        replayed = second_peer.post(
            "/v1/executions",
            json={"task": "Credential scoped", "strategy": "direct"},
            headers=headers,
        )

    assert created.headers["Idempotency-Replayed"] == "false"
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert replayed.json()["execution_id"] == created.json()["execution_id"]
    assert activated == [created.json()["execution_id"]]
    assert _counts(database) == (1, 1)


def test_open_mode_scopes_to_direct_peer_and_ignores_forwarding_headers(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    first_peer = _api(monkeypatch, service, peer_host="192.0.2.30")
    second_peer = _api(monkeypatch, service, peer_host="192.0.2.31")
    base_headers = {"Idempotency-Key": "peer-scoped-key"}

    with first_peer, second_peer:
        created = first_peer.post(
            "/v1/executions",
            json={"task": "Peer scoped", "strategy": "direct"},
            headers={**base_headers, "X-Forwarded-For": "198.51.100.1"},
        )
        same_peer_replay = first_peer.post(
            "/v1/executions",
            json={"task": "Peer scoped", "strategy": "direct"},
            headers={**base_headers, "X-Forwarded-For": "203.0.113.250"},
        )
        other_peer_create = second_peer.post(
            "/v1/executions",
            json={"task": "Peer scoped", "strategy": "direct"},
            headers={**base_headers, "X-Forwarded-For": "192.0.2.30"},
        )

    assert same_peer_replay.headers["Idempotency-Replayed"] == "true"
    assert same_peer_replay.json()["execution_id"] == created.json()["execution_id"]
    assert other_peer_create.headers["Idempotency-Replayed"] == "false"
    assert other_peer_create.json()["execution_id"] != created.json()["execution_id"]
    assert activated == [
        created.json()["execution_id"],
        other_peer_create.json()["execution_id"],
    ]
    assert _counts(database) == (2, 2)


def test_same_key_under_different_requester_scopes_creates_distinct_executions(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    request = ExecutionRequestV1(task="Scoped work", strategy="direct")
    first_identity = submission_identity(
        request,
        idempotency_key="same-key",
        requester_scope_kind="peer-host",
        requester_scope_value="192.0.2.10",
    )
    second_identity = submission_identity(
        request,
        idempotency_key="same-key",
        requester_scope_kind="peer-host",
        requester_scope_value="192.0.2.11",
    )

    first = service.submit_idempotent(request, first_identity)
    second = service.submit_idempotent(request, second_identity)

    assert first.replayed is False
    assert second.replayed is False
    assert first.result.execution_id != second.result.execution_id
    assert activated == [first.result.execution_id, second.result.execution_id]
    assert _counts(database) == (2, 2)


def test_submission_without_key_keeps_distinct_legacy_behavior(tmp_path, monkeypatch):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    client = _api(monkeypatch, service)

    with client:
        first = client.post(
            "/v1/executions",
            json={"task": "No retry key", "strategy": "direct"},
        )
        second = client.post(
            "/v1/executions",
            json={"task": "No retry key", "strategy": "direct"},
        )

    assert first.status_code == second.status_code == 202
    assert first.json()["execution_id"] != second.json()["execution_id"]
    assert "Idempotency-Replayed" not in first.headers
    assert "Idempotency-Replayed" not in second.headers
    assert activated == [first.json()["execution_id"], second.json()["execution_id"]]
    assert _counts(database) == (2, 0)


@pytest.mark.parametrize("invalid_key", ["", "   ", "x" * 129])
def test_invalid_http_key_creates_no_execution_or_mapping(
    tmp_path,
    monkeypatch,
    invalid_key,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    service.store.migrate()
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    client = _api(monkeypatch, service)

    with client:
        response = client.post(
            "/v1/executions",
            json={"task": "Must not exist", "strategy": "direct"},
            headers={"Idempotency-Key": invalid_key},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_idempotency_key"
    assert activated == []
    assert _counts(database) == (0, 0)


def test_missing_execution_mapping_fails_closed_for_changed_request(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    client = _api(monkeypatch, service)
    headers = {"Idempotency-Key": "consistency-key"}

    with client:
        created = client.post(
            "/v1/executions",
            json={"task": "Consistency check", "strategy": "direct"},
            headers=headers,
        )
        # Raw sqlite3 connections do not enable foreign keys by default. This
        # deliberately simulates external corruption or a legacy tool that
        # removed the execution without its durable key mapping.
        with sqlite3.connect(database) as con:
            con.execute(
                "DELETE FROM executions WHERE execution_id = ?",
                (created.json()["execution_id"],),
            )
            con.commit()
        replay = client.post(
            "/v1/executions",
            json={"task": "Changed request must not mask corruption", "strategy": "direct"},
            headers=headers,
        )

    assert created.status_code == 202
    assert replay.status_code == 503
    assert replay.json()["detail"] == {
        "code": "idempotency_consistency_error",
        "message": "The existing submission mapping is temporarily unavailable.",
    }
    assert activated == [created.json()["execution_id"]]
    assert _counts(database) == (0, 1)


@pytest.mark.parametrize("corruption", ["result_identity", "request_digest"])
def test_corrupt_submission_target_fails_closed_with_stable_503(
    tmp_path,
    monkeypatch,
    corruption,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    client = _api(monkeypatch, service)
    headers = {"Idempotency-Key": "corrupt-target-key"}

    with client:
        created = client.post(
            "/v1/executions",
            json={"task": "Corruption check", "strategy": "direct"},
            headers=headers,
        )
        execution_id = created.json()["execution_id"]
        with sqlite3.connect(database) as con:
            request_json, result_json = con.execute(
                "SELECT request_json, result_json FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if corruption == "result_identity":
                payload = json.loads(result_json)
                payload["execution_id"] = "m" * 32
                con.execute(
                    "UPDATE executions SET result_json = ? WHERE execution_id = ?",
                    (json.dumps(payload), execution_id),
                )
            else:
                payload = json.loads(request_json)
                payload["task"] = "A different persisted request"
                con.execute(
                    "UPDATE executions SET request_json = ? WHERE execution_id = ?",
                    (json.dumps(payload), execution_id),
                )
            con.commit()
        replay = client.post(
            "/v1/executions",
            json={"task": "Corruption check", "strategy": "direct"},
            headers=headers,
        )

    assert created.status_code == 202
    assert replay.status_code == 503
    assert replay.json()["detail"]["code"] == "idempotency_consistency_error"
    assert activated == [execution_id]
    assert _counts(database) == (1, 1)


def test_submission_persistence_failure_has_stable_503_envelope(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    service.store.migrate()
    attempts = 0

    def unavailable(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("simulated durable-store outage")

    monkeypatch.setattr(service.store, "create_or_replay_submission", unavailable)
    client = _api(monkeypatch, service)
    with client:
        response = client.post(
            "/v1/executions",
            json={"task": "Unavailable persistence", "strategy": "direct"},
            headers={"Idempotency-Key": "persistence-outage-key"},
        )

    assert attempts == 3
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "execution_persistence_unavailable",
        "message": (
            "Required execution state could not be committed. "
            "Verify durable state before retrying."
        ),
    }
    assert _counts(database) == (0, 0)


def test_submission_retry_reuses_one_factory_result_and_uuid(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    request = ExecutionRequestV1(task="Stable candidate identity", strategy="direct")
    identity = submission_identity(
        request,
        idempotency_key="stable-candidate-key",
        requester_scope_kind="pitch-key",
        requester_scope_value="stable-candidate-requester",
    )
    original_create_or_replay = service.store.create_or_replay_submission
    persistence_attempts = 0
    candidate_ids: list[str] = []
    uuid_calls = 0

    class FixedUuid:
        hex = "d" * 32

    class CountingUuidModule:
        @staticmethod
        def uuid4():
            nonlocal uuid_calls
            uuid_calls += 1
            return FixedUuid()

    def fail_once_after_factory(request_arg, identity_arg, result_factory):
        nonlocal persistence_attempts
        persistence_attempts += 1
        candidate = result_factory()
        candidate_ids.append(candidate.execution_id)
        if persistence_attempts == 1:
            raise sqlite3.OperationalError("transient failure after candidate allocation")
        return original_create_or_replay(request_arg, identity_arg, result_factory)

    monkeypatch.setattr(service_module, "uuid", CountingUuidModule())
    monkeypatch.setattr(
        service.store,
        "create_or_replay_submission",
        fail_once_after_factory,
    )

    submitted = service.submit_idempotent(request, identity)

    assert persistence_attempts == 2
    assert uuid_calls == 1
    assert candidate_ids == ["d" * 32, "d" * 32]
    assert submitted.replayed is False
    assert submitted.result.execution_id == "d" * 32
    assert activated == ["d" * 32]
    assert _counts(database) == (1, 1)


def test_concurrent_same_key_creates_one_execution_and_activation(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    ExecutionStore(database).migrate()
    services = [
        ExecutionService(store=ExecutionStore(database)),
        ExecutionService(store=ExecutionStore(database)),
    ]
    request = ExecutionRequestV1(task="Concurrent retry", strategy="direct")
    identity = submission_identity(
        request,
        idempotency_key="concurrent-key",
        requester_scope_kind="pitch-key",
        requester_scope_value="shared-requester-credential",
    )
    start = Barrier(2)
    activation_lock = Lock()
    activated: list[str] = []
    for service in services:
        _stub_activation(monkeypatch, service, activated, activation_lock)

    def submit(index):
        start.wait(timeout=5)
        return services[index].submit_idempotent(request, identity)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, index) for index in range(2)]
        submitted = [future.result(timeout=15) for future in futures]

    assert sorted(item.replayed for item in submitted) == [False, True]
    assert len({item.result.execution_id for item in submitted}) == 1
    assert activated == [submitted[0].result.execution_id]
    assert _counts(database) == (1, 1)


def test_concurrent_http_retries_schedule_and_emit_creation_exactly_once(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    observation_lock = Lock()
    emitted: list[str] = []
    execution_calls: list[str] = []
    execution_started = Event()

    def emit(event_type, _data):
        with observation_lock:
            emitted.append(event_type)

    async def fake_execute(request, *, execution_id, control, **_kwargs):
        with observation_lock:
            execution_calls.append(execution_id)
        execution_started.set()
        await sleep(0)
        return ServiceExecution(result=control.result, legacy_payload={})

    monkeypatch.setattr(service, "_emit", emit)
    monkeypatch.setattr(service, "execute", fake_execute)
    client = _api(monkeypatch, service)
    start = Barrier(2)
    headers = {"Idempotency-Key": "concurrent-http-key"}

    def post_once():
        start.wait(timeout=5)
        return client.post(
            "/v1/executions",
            json={"task": "Concurrent HTTP retry", "strategy": "direct"},
            headers=headers,
        )

    with client, ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(post_once) for _ in range(2)]
        responses = [future.result(timeout=15) for future in futures]
        assert execution_started.wait(timeout=5)

    assert all(response.status_code == 202 for response in responses)
    assert sorted(
        response.headers["Idempotency-Replayed"] for response in responses
    ) == ["false", "true"]
    assert len({response.json()["execution_id"] for response in responses}) == 1
    assert execution_calls == [responses[0].json()["execution_id"]]
    assert emitted.count("execution_created") == 1
    assert emitted.count("strategy_selected") == 1
    assert _counts(database) == (1, 1)


def test_only_digests_reach_storage_or_logs(tmp_path, monkeypatch, caplog):
    database = tmp_path / "events.db"
    raw_key = "plaintext-idempotency-key"
    requester_credential = "plaintext-requester-credential"
    request = ExecutionRequestV1(task="Digest-only storage test", strategy="direct")
    changed = ExecutionRequestV1(task="Conflicting digest-only test", strategy="direct")
    identity = submission_identity(
        request,
        idempotency_key=raw_key,
        requester_scope_kind="pitch-key",
        requester_scope_value=requester_credential,
    )
    conflicting_identity = submission_identity(
        changed,
        idempotency_key=raw_key,
        requester_scope_kind="pitch-key",
        requester_scope_value=requester_credential,
    )
    service = ExecutionService(store=ExecutionStore(database))
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)

    with caplog.at_level(logging.ERROR):
        created = service.submit_idempotent(request, identity)
        with pytest.raises(IdempotencyConflictError):
            service.submit_idempotent(changed, conflicting_identity)

    row = service.store.raw_submission(
        identity.requester_scope_hash,
        identity.idempotency_key_hash,
    )
    assert row is not None
    assert row["execution_id"] == created.result.execution_id
    for field in ("requester_scope_hash", "idempotency_key_hash", "request_hash"):
        value = row[field]
        assert len(value) == 64
        assert set(value) <= set(string.hexdigits.lower())
    serialized_row = repr(row)
    database_bytes = b"".join(
        path.read_bytes()
        for path in database.parent.glob(f"{database.name}*")
        if path.is_file()
    )
    for secret in (raw_key, requester_credential):
        assert secret not in serialized_row
        assert secret not in caplog.text
        assert secret.encode("utf-8") not in database_bytes


def test_restart_after_atomic_commit_replays_same_interrupted_execution(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    request = ExecutionRequestV1(task="Commit, crash, and retry", strategy="direct")
    identity = submission_identity(
        request,
        idempotency_key="restart-stable-key",
        requester_scope_kind="pitch-key",
        requester_scope_value="restart-requester-credential",
    )
    first_service = ExecutionService(store=ExecutionStore(database))

    committed = first_service.store.create_or_replay_submission(
        request,
        identity,
        lambda: first_service._new_result(
            request,
            "c" * 32,
            None,
            "queued",
        ),
    )
    assert committed.replayed is False
    assert first_service._controls == {}
    assert first_service._background == {}

    restarted = ExecutionService(store=ExecutionStore(database))
    monkeypatch.setattr(restarted, "_emit", lambda *_args, **_kwargs: None)
    assert restarted.reconcile_after_restart("restart-after-commit") == [
        committed.result.execution_id
    ]
    monkeypatch.setattr(
        restarted,
        "_activate_committed_submission",
        lambda *_args, **_kwargs: pytest.fail("a replay scheduled replacement work"),
    )

    replayed = restarted.submit_idempotent(request, identity)

    assert replayed.replayed is True
    assert replayed.result.execution_id == committed.result.execution_id
    assert replayed.result.lifecycle_status == "interrupted"
    assert replayed.result.coordinator_restart_marker == "restart-after-commit"
    assert restarted._controls == {}
    assert restarted._background == {}
    assert _counts(database) == (1, 1)
