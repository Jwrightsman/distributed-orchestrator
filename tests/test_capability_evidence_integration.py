"""Cross-component guarantees for scoped capability evidence in shadow mode."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import access_control
from capability_evidence import (
    DEADLINE_COMPLETION_SUBJECT,
    CapabilityEvidenceStore,
    CapabilityShadowDecisionStore,
    CapabilityShadowOperationalStore,
    ShadowOperationalProcessCounters,
)
from execution.attempts import AcceptedResultBroker, AttemptStore
from execution.contracts import (
    CandidateSummaryV1,
    ExecutionRequestV1,
    ExecutionResultV1,
    ValidationEvidenceV1,
)
from execution.persistence import ExecutionStore
from execution.service import ExecutionService
from node_capabilities import (
    NodeCapabilityDescriptorV1,
    NodeCapabilitySnapshotStore,
    NodeResourceRequirementsV1,
)
from node_enrollments import NodeEnrollmentStore
import routes_nodes
from server import app
import server_state as state


ADMISSION_SECRET = "evidence-bootstrap-admission-secret"
VIEWER_KEY = "evidence-viewer-secret-that-is-private"
MODEL_DIGEST = f"sha256:{'a' * 64}"


def test_shadow_daemon_executor_does_not_hold_process_exit():
    repository = Path(__file__).resolve().parents[1]
    script = """
import threading
import server_state as state

entered = threading.Event()
never_release = threading.Event()

def block_forever():
    entered.set()
    never_release.wait()

state.begin_capability_shadow_runtime()
state._capability_shadow_work_executor.submit(block_forever)
assert entered.wait(2)
state._capability_shadow_work_executor.close()
print("daemon-exit-ready")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "daemon-exit-ready"


def _descriptor(
    *,
    memory_bytes: int = 16 * 1024**3,
    model_name: str = "qwen3.5:4b",
    model_digest: str | None = MODEL_DIGEST,
    max_output_bytes: int = 1_048_576,
) -> NodeCapabilityDescriptorV1:
    return NodeCapabilityDescriptorV1.model_validate(
        {
            "descriptor_version": "1",
            "executor": {
                "kind": "ollama",
                "version": "0.11.4",
                "worker_protocol_version": "1",
            },
            "models": [
                {
                    "provider": "ollama",
                    "name": model_name,
                    "digest": model_digest,
                    "context_tokens": 16_384,
                    "variant": "Q4_K_M",
                }
            ],
            "hardware": {
                "architecture": "x86_64",
                "logical_cpu_count": 8,
                "total_memory_bytes": memory_bytes,
                "gpus": [],
            },
            "features": ["code"],
            "limits": {
                "max_concurrent_execution_units": 1,
                "max_output_bytes": max_output_bytes,
                "max_context_tokens": 16_384,
            },
            "isolation": {"kind": "none"},
        }
    )


def _register(
    client: TestClient,
    node_id: str,
    descriptor: NodeCapabilityDescriptorV1,
) -> dict:
    credential = f"enrollment-credential-{node_id}-0123456789"
    response = client.post(
        "/nodes/register",
        json={
            "node_id": node_id,
            "enrollment_action": "bootstrap",
            "enrollment_credential": credential,
            "model": descriptor.models[0].name,
            "platform": "Linux",
            "machine": "x86_64",
            "hostname": f"{node_id}-host",
            "capabilities": ["code"],
            "capability_descriptor": descriptor.model_dump(mode="json"),
        },
        headers={"X-Node-Secret": ADMISSION_SECRET},
    )
    assert response.status_code == 200, response.text
    return {**response.json(), "enrollment_credential": credential}


def _queue_task(
    task_id: str,
    *,
    eligible_nodes: list[str],
    requirements: NodeResourceRequirementsV1 | None = None,
    prompt: str = "PRIVATE PROMPT MUST NOT APPEAR",
    max_output_bytes: int = 1024,
) -> None:
    state.task_queue.append(
        {
            "task_id": task_id,
            "title": task_id,
            "prompt": prompt,
            "system": "private system instructions",
            "contract_version": "1",
            "execution_id": f"execution-{task_id}-0123456789",
            "execution_unit_id": f"candidate-{task_id}",
            "execution_unit_kind": "candidate",
            "evidence_role": "production",
            "max_output_bytes": max_output_bytes,
            "requires": ["code"],
            "resource_requirements": (
                requirements.model_dump(mode="json")
                if requirements is not None
                else None
            ),
            "eligible_nodes": eligible_nodes,
            "lease_seconds": 60,
        }
    )


def _claim(client: TestClient, node_id: str, registration: dict) -> dict:
    response = client.get(
        "/tasks/next",
        params={"node_id": node_id},
        headers={"X-Node-Session": registration["session_token"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _result_body(
    task: dict,
    node_id: str,
    *,
    output: str | None = "PRIVATE OUTPUT MUST NOT APPEAR",
    error: str | None = None,
) -> dict:
    return {
        "node_id": node_id,
        "output": output,
        "error": error,
        "elapsed_seconds": 1.0,
        "contract_version": task["contract_version"],
        "attempt_id": task["attempt_id"],
        "nonce": task["nonce"],
        "execution_id": task["execution_id"],
        "execution_unit_id": task["execution_unit_id"],
        "execution_unit_kind": task["execution_unit_kind"],
    }


def _observations(database, *, attempt_id: str | None = None) -> list[sqlite3.Row]:
    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        if attempt_id is None:
            return con.execute(
                "SELECT * FROM node_capability_observations "
                "ORDER BY observation_id"
            ).fetchall()
        return con.execute(
            "SELECT * FROM node_capability_observations "
            "WHERE attempt_id = ? ORDER BY observation_id",
            (attempt_id,),
        ).fetchall()


@pytest.fixture
def evidence_server(tmp_path, monkeypatch):
    database = tmp_path / "events.db"
    attempt_store = AttemptStore(database)
    enrollment_store = NodeEnrollmentStore(database)
    snapshot_store = NodeCapabilitySnapshotStore(database)
    evidence_store = CapabilityEvidenceStore(database)
    shadow_database = tmp_path / "capability-shadow-health.db"
    shadow_store = CapabilityShadowDecisionStore(shadow_database)
    shadow_operational_store = CapabilityShadowOperationalStore(
        shadow_database
    )
    broker = AcceptedResultBroker(attempt_store)
    monkeypatch.setattr(state, "_DB_PATH", database)
    monkeypatch.setattr(state, "attempt_store", attempt_store)
    monkeypatch.setattr(state, "enrollment_store", enrollment_store)
    monkeypatch.setattr(state, "capability_snapshot_store", snapshot_store)
    monkeypatch.setattr(state, "capability_evidence_store", evidence_store)
    monkeypatch.setattr(state, "capability_shadow_decision_store", shadow_store)
    monkeypatch.setattr(
        state,
        "capability_shadow_operational_store",
        shadow_operational_store,
    )
    monkeypatch.setattr(
        state,
        "capability_shadow_process_counters",
        ShadowOperationalProcessCounters(reset_at=10),
    )
    monkeypatch.setattr(state, "accepted_result_broker", broker)
    monkeypatch.setattr(state, "_LONG_POLL_TIMEOUT", 0.01)
    monkeypatch.setattr(routes_nodes, "sync_compatibility_ledger", lambda: None)
    for value in (
        state.nodes,
        state.task_queue,
        state.task_inflight,
        state.task_results,
        state.settled_attempts,
        state.node_failure_count,
        state.node_blacklist,
        state.waiting_nodes,
        state.pipeline_events,
    ):
        value.clear()
    state._capability_shadow_tasks.clear()
    state._capability_shadow_health_tasks.clear()
    state._capability_shadow_task_attempt_ids.clear()
    state._capability_shadow_task_decision_times.clear()
    state._capability_shadow_terminal_claims.clear()
    state._capability_shadow_decision_writes.clear()
    state._capability_shadow_decision_futures.clear()
    state._capability_shadow_admissions.clear()
    state.node_sessions.reset()
    settings = {
        "node_secret": ADMISSION_SECRET,
        "node_enrollment_mode": "required",
        "viewer_key": VIEWER_KEY,
        "capability_evidence_mode": "off",
        "capability_evidence_min_samples": 2,
    }
    monkeypatch.setattr(state, "get_config", lambda: settings)
    monkeypatch.setattr(access_control, "get_config", lambda: settings)
    with TestClient(app) as client:
        yield client, database, settings


def _issue_scoped_attempt(
    registration: dict,
    descriptor: NodeCapabilityDescriptorV1,
    *,
    task_id: str,
    role: str = "production",
    execution_id: str | None = None,
    issued_at: float = 100,
    lease_expires_at: float = 200,
    execution_deadline_at: float | None = None,
    comparison_primary_attempt_id: str | None = None,
):
    enrollment = state.enrollment_store.get(registration["enrollment_id"])
    assert enrollment is not None
    task = {
        "task_id": task_id,
        "contract_version": "1",
        "execution_id": execution_id or f"execution-{task_id}",
        "execution_unit_id": f"candidate-{task_id}",
        "execution_unit_kind": "candidate",
        "evidence_role": role,
        "comparison_primary_attempt_id": comparison_primary_attempt_id,
        "selected_model": {
            "provider": descriptor.models[0].provider,
            "name": descriptor.models[0].name,
            "digest": descriptor.models[0].digest,
        },
    }
    if execution_deadline_at is not None:
        task["execution_deadline_at"] = execution_deadline_at
    nonce = f"nonce-{task_id}-unguessable"
    record = state.attempt_store.issue(
        task,
        assigned_node_id=enrollment.node_id,
        assigned_enrollment_id=enrollment.enrollment_id,
        assigned_credential_version=enrollment.credential_version,
        assigned_session_id=registration["session_id"],
        assigned_descriptor_version=registration["capability_descriptor_version"],
        assigned_descriptor_hash=registration["capability_descriptor_hash"],
        attempt_id=f"attempt-{task_id}",
        nonce=nonce,
        issued_at=issued_at,
        lease_expires_at=lease_expires_at,
    )
    return task, record, nonce, enrollment


def _settle_scoped_attempt(
    task: dict,
    record,
    nonce: str,
    enrollment,
    registration: dict,
    *,
    output: str | None = "complete output",
    error: str | None = None,
    settled_at: float = 110,
):
    return state.attempt_store.settle(
        task_id=task["task_id"],
        node_id=enrollment.node_id,
        output=output,
        error=error,
        elapsed_seconds=1.0,
        contract_version="1",
        attempt_id=record.attempt_id,
        nonce=nonce,
        execution_id=task["execution_id"],
        execution_unit_id=task["execution_unit_id"],
        execution_unit_kind=task["execution_unit_kind"],
        session_id=registration["session_id"],
        enrollment_id=enrollment.enrollment_id,
        credential_version=enrollment.credential_version,
        now=settled_at,
    )


def _seed_contrasting_evidence(
    registration: dict,
    descriptor: NodeCapabilityDescriptorV1,
    *,
    node_id: str,
    contract_passed: bool,
    elapsed_seconds: float,
) -> None:
    recorded_at = time.time() - 1
    for index in range(2):
        settled_at = 100 + elapsed_seconds
        attempt = {
            "attempt_id": f"seed-{node_id}-{index}",
            "execution_id": f"seed-execution-{node_id}-{index}",
            "execution_unit_id": f"seed-unit-{node_id}-{index}",
            "execution_unit_kind": "candidate",
            "assigned_node_id": node_id,
            "assigned_enrollment_id": registration["enrollment_id"],
            "assigned_descriptor_version": registration[
                "capability_descriptor_version"
            ],
            "assigned_descriptor_hash": registration["capability_descriptor_hash"],
            "assigned_model_provider": descriptor.models[0].provider,
            "assigned_model_name": descriptor.models[0].name,
            "assigned_model_digest": descriptor.models[0].digest,
            "evidence_role": "production",
            "contract_version": "1",
            "state": "settled",
            "terminal_cause": "settled_output",
            "issued_at": 100,
            "lease_expires_at": 250,
            "settled_at": settled_at,
        }
        state.capability_evidence_store.record_settlement(
            attempt,
            accepted_at=settled_at,
            output_bytes=100,
            recorded_at=recorded_at,
        )
        state.capability_evidence_store.record_contract_floor(
            attempt,
            passed=contract_passed,
            method_version="seed-contract-v1",
            recorded_at=recorded_at,
        )


async def _await_new_shadow_task(previous_tasks: set[asyncio.Task]) -> None:
    """Await one explicitly scheduled evaluator and detach test-owned tracking."""

    created = state._capability_shadow_tasks - previous_tasks
    assert len(created) == 1
    task = created.pop()
    try:
        await task
    finally:
        state._capability_shadow_tasks.discard(task)
        state._capability_shadow_task_attempt_ids.pop(task, None)


def test_authoritative_settlement_writes_evidence_replay_is_idempotent_and_rejection_does_not(
    evidence_server,
):
    client, database, _settings = evidence_server
    descriptor = _descriptor()
    registration = _register(client, "worker", descriptor)
    session_headers = {"X-Node-Session": registration["session_token"]}
    _queue_task("accepted", eligible_nodes=["worker"])
    task = _claim(client, "worker", registration)
    body = _result_body(task, "worker")

    accepted = client.post(
        "/tasks/accepted/result", json=body, headers=session_headers
    )
    assert accepted.status_code == 200
    first_rows = _observations(database, attempt_id=task["attempt_id"])
    assert {row["observation_type"] for row in first_rows} == {
        "settlement_outcome",
        "deadline_completion",
        "coordinator_wall_seconds",
        "output_bytes",
        "effective_output_bytes_per_second",
    }

    replay = client.post(
        "/tasks/accepted/result", json=body, headers=session_headers
    )
    assert replay.status_code == 200
    assert replay.json() == accepted.json()
    assert [row["observation_id"] for row in _observations(
        database, attempt_id=task["attempt_id"]
    )] == [row["observation_id"] for row in first_rows]

    _queue_task("rejected", eligible_nodes=["worker"])
    rejected_task = _claim(client, "worker", registration)
    rejected_body = _result_body(rejected_task, "worker")
    rejected_body["nonce"] = "wrong-nonce"
    rejected = client.post(
        "/tasks/rejected/result", json=rejected_body, headers=session_headers
    )
    assert rejected.status_code == 403
    assert _observations(database, attempt_id=rejected_task["attempt_id"]) == []


def test_evidence_savepoint_failure_cannot_rollback_settlement_or_contribution(
    evidence_server, monkeypatch
):
    client, database, _settings = evidence_server
    descriptor = _descriptor()
    registration = _register(client, "worker", descriptor)
    _queue_task("savepoint", eligible_nodes=["worker"])
    task = _claim(client, "worker", registration)

    def fail_after_partial_write(con, *_args, **_kwargs):
        con.execute("CREATE TABLE evidence_partial_write(value TEXT)")
        con.execute("INSERT INTO evidence_partial_write VALUES ('rollback me')")
        raise RuntimeError("synthetic evidence failure")

    monkeypatch.setattr(
        CapabilityEvidenceStore,
        "record_settlement_in_transaction",
        staticmethod(fail_after_partial_write),
    )
    response = client.post(
        "/tasks/savepoint/result",
        json=_result_body(task, "worker"),
        headers={"X-Node-Session": registration["session_token"]},
    )

    assert response.status_code == 200
    assert state.attempt_store.get_receipt_for_task("savepoint") is not None
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM contributions WHERE attempt_id = ?",
            (task["attempt_id"],),
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'evidence_partial_write'"
        ).fetchone()[0] == 0
    assert _observations(database, attempt_id=task["attempt_id"]) == []


def test_typed_fault_attribution_excludes_caller_coordinator_and_bounded_execution_deadline(
    evidence_server,
):
    client, database, _settings = evidence_server
    descriptor = _descriptor()
    registration = _register(client, "worker", descriptor)
    cases = [
        ("caller", "cancelled", "execution_cancelled"),
        ("restart", "interrupted", "coordinator_restart"),
        ("lease", "expired", "lease_expired"),
        ("stale", "reclaimed", "node_stale"),
    ]
    records = {}
    for suffix, terminal_state, cause in cases:
        _task, record, _nonce, _enrollment = _issue_scoped_attempt(
            registration,
            descriptor,
            task_id=suffix,
            lease_expires_at=500,
        )
        assert state.attempt_store.transition_active(
            attempt_id=record.attempt_id,
            state=terminal_state,
            reason="free-form text is not interpreted",
            terminal_cause=cause,
            now=170,
        )
        records[suffix] = state.attempt_store.get(record.attempt_id)

    for suffix in ("caller", "restart"):
        assert _observations(
            database, attempt_id=records[suffix].attempt_id
        ) == []
    for suffix, expected in (("lease", "lease_expired"), ("stale", "node_stale")):
        rows = _observations(database, attempt_id=records[suffix].attempt_id)
        assert {
            (row["observation_type"], row["outcome"]) for row in rows
        } == {
            ("deadline_completion", "fail"),
            ("terminal_outcome", expected),
        }

    _task, deadline_record, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="bounded-deadline",
        issued_at=200,
        lease_expires_at=250,
        execution_deadline_at=250,
    )
    assert deadline_record.lease_deadline_kind == "execution_deadline"
    assert state.attempt_store.expire_due(now=251) == 1
    expired = state.attempt_store.get(deadline_record.attempt_id)
    assert expired.terminal_cause == "execution_deadline"
    assert _observations(database, attempt_id=deadline_record.attempt_id) == []


def test_coordinator_restart_persistence_failure_never_projects_node_failure(
    evidence_server,
):
    client, database, _settings = evidence_server
    descriptor = _descriptor()
    registration = _register(client, "worker", descriptor)
    _task, record, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="coordinator-persistence-failure",
    )
    diagnostic_reason = (
        "lease_expired node_stale worker missed deadline; text is not attribution"
    )

    with sqlite3.connect(database) as con:
        con.execute(
            """
            CREATE TRIGGER fail_coordinator_attempt_persistence
            BEFORE UPDATE OF state ON attempts
            WHEN OLD.attempt_id = 'attempt-coordinator-persistence-failure'
            BEGIN
                SELECT RAISE(ABORT, 'synthetic coordinator persistence failure');
            END
            """
        )
        con.commit()

    with pytest.raises(
        sqlite3.DatabaseError,
        match="synthetic coordinator persistence failure",
    ):
        state.attempt_store.interrupt_active(diagnostic_reason)

    unchanged = state.attempt_store.get(record.attempt_id)
    assert unchanged is not None
    assert unchanged.state == "active"
    assert unchanged.terminal_cause is None
    assert _observations(database, attempt_id=record.attempt_id) == []

    with sqlite3.connect(database) as con:
        con.execute("DROP TRIGGER fail_coordinator_attempt_persistence")
        con.commit()

    assert state.attempt_store.interrupt_active(diagnostic_reason) == 1
    interrupted = state.attempt_store.get(record.attempt_id)
    assert interrupted is not None
    assert interrupted.state == "interrupted"
    assert interrupted.terminal_cause == "coordinator_restart"

    state._reconcile_capability_evidence()

    rows = _observations(database, attempt_id=record.attempt_id)
    assert rows == []
    assert not {
        "terminal_outcome",
        "deadline_completion",
    }.intersection(row["observation_type"] for row in rows)


def test_reopen_preserves_observations_and_startup_reconciliation_repairs_missing_row(
    evidence_server, monkeypatch
):
    client, database, _settings = evidence_server
    descriptor = _descriptor()
    registration = _register(client, "worker", descriptor)
    _queue_task("reconcile", eligible_nodes=["worker"])
    task = _claim(client, "worker", registration)
    response = client.post(
        "/tasks/reconcile/result",
        json=_result_body(task, "worker", output="reconciliation output"),
        headers={"X-Node-Session": registration["session_token"]},
    )
    assert response.status_code == 200
    original_ids = {
        row["observation_id"]
        for row in _observations(database, attempt_id=task["attempt_id"])
    }
    assert len(original_ids) == 5

    with sqlite3.connect(database) as con:
        con.execute("DROP TRIGGER trg_capability_observations_no_delete")
        con.execute(
            "DELETE FROM node_capability_observations "
            "WHERE attempt_id = ? AND observation_type = 'output_bytes'",
            (task["attempt_id"],),
        )
        con.commit()

    reopened = CapabilityEvidenceStore(database)
    reopened.migrate()
    assert len(_observations(database, attempt_id=task["attempt_id"])) == 4
    monkeypatch.setattr(state, "capability_evidence_store", reopened)
    state._reconcile_capability_evidence()

    repaired_ids = {
        row["observation_id"]
        for row in _observations(database, attempt_id=task["attempt_id"])
    }
    assert repaired_ids == original_ids


def test_startup_reconciliation_upgrades_legacy_deadline_success_semantics(
    evidence_server,
):
    _client, database, _settings = evidence_server
    descriptor = _descriptor()
    registration = _register(_client, "worker", descriptor)
    task, attempt, nonce, enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="legacy-deadline-upgrade",
    )
    _settle_scoped_attempt(
        task,
        attempt,
        nonce,
        enrollment,
        registration,
        output=None,
        error="worker-reported failure",
    )
    settled = state.attempt_store.get(attempt.attempt_id)
    assert settled is not None
    assert settled.terminal_cause == "settled_worker_error"

    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        current = con.execute(
            "SELECT observed_at, recorded_at "
            "FROM node_capability_observations "
            "WHERE attempt_id = ? AND observation_type = 'deadline_completion' "
            "AND subject_key = ?",
            (attempt.attempt_id, DEADLINE_COMPLETION_SUBJECT),
        ).fetchone()
        assert current is not None
        con.execute("DROP TRIGGER trg_capability_observations_no_delete")
        con.execute(
            "DELETE FROM node_capability_observations "
            "WHERE attempt_id = ? AND observation_type = 'deadline_completion'",
            (attempt.attempt_id,),
        )
        resolution = CapabilityEvidenceStore.resolve_scope_in_transaction(
            con, settled
        )
        assert resolution.context is not None
        CapabilityEvidenceStore._append_one(
            con,
            resolution.context,
            observation_type="deadline_completion",
            subject_key="lifecycle",
            outcome="pass",
            numeric_value=None,
            metadata=None,
            observed_at=float(current["observed_at"]),
            recorded_at=float(current["recorded_at"]),
        )
        con.commit()
    state.capability_evidence_store.migrate()

    before = _observations(database, attempt_id=attempt.attempt_id)
    legacy_before = [
        row
        for row in before
        if row["observation_type"] == "deadline_completion"
    ]
    assert [(row["subject_key"], row["outcome"]) for row in legacy_before] == [
        ("lifecycle", "pass")
    ]
    legacy_fingerprint = tuple(legacy_before[0])
    assert attempt.attempt_id in {
        item.attempt_id
        for item in state.attempt_store.list_evidence_reconciliation_candidates()
    }

    state._reconcile_capability_evidence()

    after = _observations(database, attempt_id=attempt.attempt_id)
    deadline_rows = [
        row
        for row in after
        if row["observation_type"] == "deadline_completion"
    ]
    assert {
        (row["subject_key"], row["outcome"]) for row in deadline_rows
    } == {
        ("lifecycle", "pass"),
        (DEADLINE_COMPLETION_SUBJECT, "fail"),
    }
    assert tuple(
        next(row for row in deadline_rows if row["subject_key"] == "lifecycle")
    ) == legacy_fingerprint
    resolution = state.capability_evidence_store.resolve_scope(settled)
    assert resolution.context is not None
    aggregate = state.capability_evidence_store.aggregate(
        resolution.context.scope,
        minimum_samples=1,
    )
    assert aggregate.deadline_completion.sample_count == 1
    assert aggregate.deadline_completion.positive_count == 0
    assert aggregate.deadline_completion.negative_count == 1
    assert attempt.attempt_id not in {
        item.attempt_id
        for item in state.attempt_store.list_evidence_reconciliation_candidates()
    }

    state._reconcile_capability_evidence()

    assert _observations(database, attempt_id=attempt.attempt_id) == after


def test_sampled_agreement_persists_for_real_attempts_and_replays_without_duplicates(
    evidence_server,
):
    client, database, _settings = evidence_server
    descriptor = _descriptor()
    primary_registration = _register(client, "primary", descriptor)
    sampled_registration = _register(client, "sampled", descriptor)
    execution_id = "shared-sampled-execution"
    primary_task, primary, primary_nonce, primary_enrollment = (
        _issue_scoped_attempt(
            primary_registration,
            descriptor,
            task_id="primary",
            execution_id=execution_id,
        )
    )
    sampled_task, sampled, sampled_nonce, sampled_enrollment = (
        _issue_scoped_attempt(
            sampled_registration,
            descriptor,
            task_id="sampled",
            role="sampled_comparison",
            execution_id=execution_id,
            comparison_primary_attempt_id=primary.attempt_id,
        )
    )
    _settle_scoped_attempt(
        primary_task,
        primary,
        primary_nonce,
        primary_enrollment,
        primary_registration,
        settled_at=110,
    )
    _settle_scoped_attempt(
        sampled_task,
        sampled,
        sampled_nonce,
        sampled_enrollment,
        sampled_registration,
        settled_at=112,
    )
    primary = state.attempt_store.get(primary.attempt_id)
    sampled = state.attempt_store.get(sampled.attempt_id)

    first = state.capability_evidence_store.record_sampled_agreement(
        primary,
        sampled,
        agreed=True,
        method_version="shape-v1",
        recorded_at=120,
    )
    replay = CapabilityEvidenceStore(database).record_sampled_agreement(
        primary,
        sampled,
        agreed=True,
        method_version="shape-v1",
        recorded_at=999,
    )

    assert len(first.observations) == 2
    assert [item.observation_id for item in replay.observations] == [
        item.observation_id for item in first.observations
    ]
    with sqlite3.connect(database) as con:
        rows = con.execute(
            "SELECT evidence_role, outcome FROM node_capability_observations "
            "WHERE observation_type = 'sampled_agreement' "
            "ORDER BY evidence_role"
        ).fetchall()
    assert rows == [
        ("production", "agree"),
        ("sampled_comparison", "agree"),
    ]


def test_execution_service_projects_only_candidate_local_contract_floor(
    evidence_server,
):
    client, database, _settings = evidence_server
    descriptor = _descriptor()
    registration = _register(client, "worker", descriptor)
    settled_attempts = []
    for suffix in ("pass", "fail"):
        task, record, nonce, enrollment = _issue_scoped_attempt(
            registration, descriptor, task_id=f"floor-{suffix}"
        )
        _settle_scoped_attempt(task, record, nonce, enrollment, registration)
        settled_attempts.append(state.attempt_store.get(record.attempt_id))

    def validation(status: str, *, validator_name: str) -> ValidationEvidenceV1:
        return ValidationEvidenceV1(
            validator_name=validator_name,
            validator_version="1",
            status=status,
            assurance_level="structural",
            proves_behavioral_correctness=False,
            requirement_source="contract_floor",
            required=True,
        )

    candidates = [
        CandidateSummaryV1(
            candidate_id="candidate-pass",
            status="completed",
            placement="distributed",
            attempt_id=settled_attempts[0].attempt_id,
            validation=[validation("passed", validator_name="candidate-floor")],
        ),
        CandidateSummaryV1(
            candidate_id="candidate-fail",
            status="rejected",
            placement="distributed",
            attempt_id=settled_attempts[1].attempt_id,
            validation=[validation("failed", validator_name="candidate-floor")],
        ),
    ]
    global_validation = validation("failed", validator_name="global-final-floor")
    result = SimpleNamespace(
        execution_id="execution-contract-floor",
        candidates=candidates,
        validation_evidence=[global_validation],
    )

    ExecutionService._record_contract_floor_evidence(result)

    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT attempt_id, outcome, metadata_json "
            "FROM node_capability_observations "
            "WHERE observation_type = 'contract_floor' ORDER BY attempt_id"
        ).fetchall()
    assert [(row["attempt_id"], row["outcome"]) for row in rows] == [
        (settled_attempts[1].attempt_id, "fail"),
        (settled_attempts[0].attempt_id, "pass"),
    ]
    assert all(
        json.loads(row["metadata_json"])["method_version"]
        == "candidate-validation-v1"
        for row in rows
    )
    assert "global-final-floor" not in json.dumps(
        [dict(row) for row in rows]
    )


def test_startup_reconstructs_missing_contract_floor_from_durable_terminal_result(
    evidence_server,
):
    client, database, _settings = evidence_server
    descriptor = _descriptor()
    registration = _register(client, "worker", descriptor)
    task, record, nonce, enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="floor-startup-recovery",
    )
    _settle_scoped_attempt(task, record, nonce, enrollment, registration)
    settled = state.attempt_store.get(record.attempt_id)
    assert settled is not None

    validation = ValidationEvidenceV1(
        validator_name="candidate-floor",
        validator_version="1",
        status="passed",
        assurance_level="structural",
        proves_behavioral_correctness=False,
        requirement_source="contract_floor",
        required=True,
    )
    request = ExecutionRequestV1(task="recover durable contract-floor evidence")
    result = ExecutionResultV1(
        execution_id=task["execution_id"],
        status="completed",
        lifecycle_status="completed",
        task=request.task,
        strategy_requested="auto",
        strategy_selected="ensemble",
        strategy_version="1",
        selector_reason="test fixture",
        selector_version="test-v1",
        placement_requested="local",
        created_at="2026-08-26T00:00:00+00:00",
        completed_at="2026-08-26T00:01:00+00:00",
        candidates=[
            CandidateSummaryV1(
                candidate_id="candidate-floor-startup-recovery",
                status="completed",
                placement="distributed",
                attempt_id=settled.attempt_id,
                validation=[validation],
            )
        ],
    )
    ExecutionStore(database).save(request, result)
    assert not any(
        row["observation_type"] == "contract_floor"
        for row in _observations(database, attempt_id=settled.attempt_id)
    )

    restarted = ExecutionService(store=ExecutionStore(database))
    restarted.reconcile_contract_floor_evidence(limit=1)

    repaired = [
        row
        for row in _observations(database, attempt_id=settled.attempt_id)
        if row["observation_type"] == "contract_floor"
    ]
    assert [(row["outcome"], json.loads(row["metadata_json"])) for row in repaired] == [
        (
            "pass",
            {
                "contract_version": "1",
                "method_version": "candidate-validation-v1",
            },
        )
    ]

    restarted.reconcile_contract_floor_evidence(limit=1)
    assert [
        row["observation_id"]
        for row in _observations(database, attempt_id=settled.attempt_id)
        if row["observation_type"] == "contract_floor"
    ] == [repaired[0]["observation_id"]]


def test_operator_evidence_requires_viewer_and_exposes_only_safe_aggregates(
    evidence_server,
):
    client, _database, settings = evidence_server
    descriptor = _descriptor()
    registration = _register(client, "worker", descriptor)
    private_prompt = "PROMPT-PRIVATE-7f9d"
    private_output = "OUTPUT-PRIVATE-5b2c"
    _queue_task(
        "operator-safe",
        eligible_nodes=["worker"],
        prompt=private_prompt,
    )
    task = _claim(client, "worker", registration)
    assert client.post(
        "/tasks/operator-safe/result",
        json=_result_body(task, "worker", output=private_output),
        headers={"X-Node-Session": registration["session_token"]},
    ).status_code == 200

    assert client.get("/v1/operator/capability-evidence").status_code == 401
    response = client.get(
        "/v1/operator/capability-evidence",
        headers={"Authorization": f"Bearer {VIEWER_KEY}"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["affects_routing"] is False
    assert payload["categories"]["reputation"] == "not_implemented"
    assert payload["scopes"][0]["contract_floor"]["meaning"] == (
        "structural_contract_assurance_not_semantic_correctness"
    )
    assert payload["scopes"][0]["deadline_success"]["meaning"] == (
        "nonempty_output_settled_before_lease_deadline"
    )
    serialized = response.text
    for secret in (
        private_prompt,
        private_output,
        task["nonce"],
        registration["session_token"],
        registration["enrollment_credential"],
        settings["viewer_key"],
    ):
        assert secret not in serialized
    assert all(
        forbidden not in serialized
        for forbidden in (
            '"prompt"',
            '"output"',
            '"nonce"',
            '"session_token"',
            '"credential"',
            '"global_score"',
            '"reputation_score"',
            '"trust_score"',
        )
    )


def test_off_and_shadow_modes_keep_actual_handout_invariant_with_contrasting_evidence(
    evidence_server,
    monkeypatch,
):
    client, database, settings = evidence_server
    descriptor = _descriptor()
    registration_a = _register(client, "node-a", descriptor)
    registration_b = _register(client, "node-b", descriptor)
    _seed_contrasting_evidence(
        registration_a,
        descriptor,
        node_id="node-a",
        contract_passed=False,
        elapsed_seconds=90,
    )
    _seed_contrasting_evidence(
        registration_b,
        descriptor,
        node_id="node-b",
        contract_passed=True,
        elapsed_seconds=10,
    )
    terminal_recorded = threading.Event()
    operational = state.capability_shadow_operational_store
    original_record = operational.record

    def record_and_signal(**kwargs):
        record = original_record(**kwargs)
        if kwargs["phase"] == "evaluation":
            terminal_recorded.set()
        return record

    monkeypatch.setattr(operational, "record", record_and_signal)
    monkeypatch.setattr(
        state,
        "_schedule_capability_shadow_operation",
        state._record_capability_shadow_operation,
    )

    settings["capability_evidence_mode"] = "off"
    _queue_task("mode-off", eligible_nodes=["node-a", "node-b"])
    off_handout = _claim(client, "node-a", registration_a)
    assert state.attempt_store.get(off_handout["attempt_id"]).assigned_node_id == (
        "node-a"
    )

    settings["capability_evidence_mode"] = "shadow"
    _queue_task("mode-shadow", eligible_nodes=["node-a", "node-b"])
    shadow_handout = _claim(client, "node-a", registration_a)
    assert state.attempt_store.get(shadow_handout["attempt_id"]).assigned_node_id == (
        "node-a"
    )

    assert terminal_recorded.wait(timeout=5)
    with sqlite3.connect(state.capability_shadow_decision_store.path) as con:
        decision = con.execute(
            "SELECT outcome, preferred_candidate_id, actual_candidate_id "
            "FROM capability_shadow_decisions WHERE actual_attempt_id = ?",
            (shadow_handout["attempt_id"],),
        ).fetchone()
    assert decision is not None
    assert decision[0] == "different"
    assert decision[1] != decision[2]


def test_shadow_candidate_set_reapplies_hard_matcher_and_excludes_ineligible_node(
    evidence_server,
):
    client, database, settings = evidence_server
    eligible_descriptor = _descriptor(memory_bytes=16 * 1024**3)
    ineligible_descriptor = _descriptor(memory_bytes=2 * 1024**3)
    registration_a = _register(client, "eligible", eligible_descriptor)
    _register(client, "ineligible", ineligible_descriptor)
    requirements = NodeResourceRequirementsV1(
        minimum_memory_bytes=8 * 1024**3
    )
    settings["capability_evidence_mode"] = "off"
    _queue_task(
        "hard-filter",
        eligible_nodes=["eligible", "ineligible"],
        requirements=requirements,
    )
    handout = _claim(client, "eligible", registration_a)
    attempt = state.attempt_store.get(handout["attempt_id"])
    decision_at = time.time()
    scopes = state._capture_capability_shadow_scopes(
        actual_attempt=attempt,
        actual_descriptor=eligible_descriptor,
        resource_requirements=requirements.model_dump(mode="json"),
        required_capabilities=("code",),
        eligible_node_ids=("eligible", "ineligible"),
        captured_at=decision_at,
    )
    assert len(scopes) == 1

    state._evaluate_capability_shadow(
        actual_attempt_id=attempt.attempt_id,
        actual_scope_key=scopes[0].scope_key,
        candidate_scopes=scopes,
        minimum_samples=1,
        decision_at=decision_at,
    )

    with sqlite3.connect(state.capability_shadow_decision_store.path) as con:
        decision = con.execute(
            "SELECT candidate_count, outcome, rationale_code "
            "FROM capability_shadow_decisions WHERE actual_attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()
    assert decision == (1, "no_preference", "single_candidate")


def test_shadow_candidate_capture_rechecks_actual_output_capacity(evidence_server):
    client, _database, _settings = evidence_server
    descriptor = _descriptor(max_output_bytes=1024)
    registration = _register(client, "stale-actual-capacity", descriptor)
    _task, attempt, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="stale-actual-capacity",
    )

    with pytest.raises(
        ValueError,
        match="actual shadow node no longer satisfies task requirements",
    ):
        state._capture_capability_shadow_scopes(
            actual_attempt=attempt,
            actual_descriptor=descriptor,
            resource_requirements=None,
            required_capabilities=("code",),
            eligible_node_ids=("stale-actual-capacity",),
            captured_at=time.time(),
        )


def test_blocked_shadow_scope_capture_cannot_delay_worker_handout(
    evidence_server,
    monkeypatch,
):
    client, _database, settings = evidence_server
    settings["capability_evidence_mode"] = "shadow"
    descriptor = _descriptor()
    registration = _register(client, "nonblocking-capture-worker", descriptor)
    _queue_task(
        "nonblocking-shadow-capture",
        eligible_nodes=["nonblocking-capture-worker"],
    )
    original_capture = state._capture_capability_shadow_scopes
    capture_entered = threading.Event()
    release_capture = threading.Event()
    handout_finished = threading.Event()
    handouts: list[dict] = []
    failures: list[BaseException] = []

    def blocking_capture(**kwargs):
        capture_entered.set()
        release_capture.wait()
        return original_capture(**kwargs)

    def claim_work():
        try:
            handouts.append(
                _claim(client, "nonblocking-capture-worker", registration)
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            handout_finished.set()

    monkeypatch.setattr(
        state,
        "_capture_capability_shadow_scopes",
        blocking_capture,
    )
    claim_thread = threading.Thread(target=claim_work)
    claim_thread.start()
    try:
        assert capture_entered.wait(timeout=5)
        assert handout_finished.wait(timeout=5)
    finally:
        release_capture.set()
        claim_thread.join(timeout=5)

    assert not claim_thread.is_alive()
    assert failures == []
    assert len(handouts) == 1
    assert handouts[0]["task_id"] == "nonblocking-shadow-capture"


def test_shadow_evaluation_uses_assignment_time_candidate_snapshot(evidence_server):
    client, database, settings = evidence_server
    descriptor = _descriptor()
    registration_a = _register(client, "snapshot-a", descriptor)
    _register(client, "snapshot-b", descriptor)
    settings["capability_evidence_mode"] = "off"
    _queue_task(
        "snapshot-candidates",
        eligible_nodes=["snapshot-a", "snapshot-b"],
    )
    handout = _claim(client, "snapshot-a", registration_a)
    attempt = state.attempt_store.get(handout["attempt_id"])
    decision_at = time.time()
    scopes = state._capture_capability_shadow_scopes(
        actual_attempt=attempt,
        actual_descriptor=descriptor,
        resource_requirements=None,
        required_capabilities=("code",),
        eligible_node_ids=("snapshot-a", "snapshot-b"),
        captured_at=decision_at,
    )
    assert len(scopes) == 2

    state.nodes.pop("snapshot-b")
    actual_scope = next(
        scope for scope in scopes if scope.enrollment_id == registration_a["enrollment_id"]
    )
    state._evaluate_capability_shadow(
        actual_attempt_id=attempt.attempt_id,
        actual_scope_key=actual_scope.scope_key,
        candidate_scopes=scopes,
        minimum_samples=1,
        decision_at=decision_at,
    )

    with sqlite3.connect(state.capability_shadow_decision_store.path) as con:
        decision = con.execute(
            "SELECT candidate_count, outcome, rationale_code "
            "FROM capability_shadow_decisions WHERE actual_attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()
    assert decision == (2, "no_preference", "insufficient_deadline_evidence")


@pytest.mark.asyncio
async def test_shadow_operational_admission_outcomes_are_bounded_and_counted_once(
    evidence_server,
    monkeypatch,
):
    client, _database, settings = evidence_server
    descriptor = _descriptor()
    registration = _register(client, "admission-worker", descriptor)
    monkeypatch.setattr(
        state,
        "_schedule_capability_shadow_operation",
        state._record_capability_shadow_operation,
    )

    def schedule(record) -> bool:
        return state.schedule_capability_shadow_evaluation(
            record,
            actual_descriptor=descriptor,
            resource_requirements=None,
            required_capabilities=("code",),
            eligible_node_ids=("admission-worker",),
            decision_at=time.time(),
        )

    _task, disabled, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="shadow-disabled",
    )
    assert schedule(disabled) is False

    settings["capability_evidence_mode"] = "shadow"
    _task, production, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="shadow-nonproduction",
    )
    nonproduction = replace(production, evidence_role="sampled_comparison")
    assert schedule(nonproduction) is False

    _task, saturated, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="shadow-saturated",
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(state, "_CAPABILITY_SHADOW_TASK_LIMIT", 0)
        assert schedule(saturated) is False

    _task, capture_failed, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="shadow-capture-failed",
    )

    def fail_scope_capture(**_kwargs):
        raise RuntimeError("PRIVATE SCOPE FAILURE MUST NOT APPEAR")

    with monkeypatch.context() as scoped:
        scoped.setattr(state, "_capture_capability_shadow_scopes", fail_scope_capture)
        previous_tasks = set(state._capability_shadow_tasks)
        assert schedule(capture_failed) is True
        await _await_new_shadow_task(previous_tasks)

    _task, scheduled, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="shadow-scheduled",
    )
    previous_tasks = set(state._capability_shadow_tasks)
    assert schedule(scheduled) is True
    await _await_new_shadow_task(previous_tasks)

    store = state.capability_shadow_operational_store
    expected_admissions = {
        disabled.attempt_id: ("disabled", "mode_disabled"),
        nonproduction.attempt_id: ("not_applicable", "nonproduction_attempt"),
        saturated.attempt_id: (
            "queue_saturated",
            "background_queue_limit_reached",
        ),
        capture_failed.attempt_id: (
            "scope_capture_failed",
            "scope_capture_failed",
        ),
        scheduled.attempt_id: ("scheduled", "evaluation_scheduled"),
    }
    for attempt_id, expected in expected_admissions.items():
        record = store.get_for_attempt_phase(attempt_id, "admission")
        assert record is not None
        assert (record.outcome, record.reason_code) == expected
    terminal = store.get_for_attempt_phase(scheduled.attempt_id, "evaluation")
    assert terminal is not None
    assert (terminal.outcome, terminal.reason_code) == (
        "completed",
        "decision_persisted",
    )

    scheduled_admission = store.get_for_attempt_phase(
        scheduled.attempt_id,
        "admission",
    )
    assert scheduled_admission is not None
    state._record_capability_shadow_operation(
        attempt_id=scheduled.attempt_id,
        phase="admission",
        outcome="scheduled",
        reason_code="evaluation_scheduled",
        occurred_at=scheduled_admission.occurred_at + 1,
    )
    report = store.report()
    assert report.admission_counts == {
        "disabled": 1,
        "not_applicable": 1,
        "queue_saturated": 1,
        "scope_capture_failed": 1,
        "scheduled": 1,
    }
    assert report.evaluation_counts == {
        "completed": 1,
        "evaluator_failed": 0,
        "decision_write_failed": 0,
        "cancelled_on_shutdown": 0,
    }
    assert (
        report.assignment_observation_total,
        report.offered_total,
        report.scheduled_total,
        report.completed_total,
        report.skipped_total,
        report.failed_total,
        report.pending_total,
    ) == (5, 3, 1, 1, 2, 2, 0)
    assert (
        report.drop_failure_numerator,
        report.drop_failure_denominator,
        report.drop_failure_rate,
    ) == (2, 3, pytest.approx(2 / 3))


@pytest.mark.asyncio
async def test_shadow_evaluator_and_decision_write_failures_are_distinct(
    evidence_server,
    monkeypatch,
):
    client, _database, settings = evidence_server
    settings["capability_evidence_mode"] = "shadow"
    descriptor = _descriptor()
    registration = _register(client, "failure-worker", descriptor)
    monkeypatch.setattr(
        state,
        "_schedule_capability_shadow_operation",
        state._record_capability_shadow_operation,
    )

    def schedule(record) -> bool:
        return state.schedule_capability_shadow_evaluation(
            record,
            actual_descriptor=descriptor,
            resource_requirements=None,
            required_capabilities=("code",),
            eligible_node_ids=("failure-worker",),
            decision_at=time.time(),
        )

    _task, evaluator_attempt, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="shadow-evaluator-failure",
    )

    def fail_evaluator(**_kwargs):
        raise RuntimeError("PRIVATE EVALUATOR FAILURE MUST NOT APPEAR")

    previous_tasks = set(state._capability_shadow_tasks)
    with monkeypatch.context() as scoped:
        scoped.setattr(state, "_build_capability_shadow_evaluation", fail_evaluator)
        assert schedule(evaluator_attempt) is True
        await _await_new_shadow_task(previous_tasks)

    _task, write_attempt, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="shadow-decision-write-failure",
    )

    def fail_decision_write(*_args, **_kwargs):
        raise RuntimeError("PRIVATE DECISION FAILURE MUST NOT APPEAR")

    previous_tasks = set(state._capability_shadow_tasks)
    with monkeypatch.context() as scoped:
        scoped.setattr(
            state.capability_shadow_decision_store,
            "record",
            fail_decision_write,
        )
        assert schedule(write_attempt) is True
        await _await_new_shadow_task(previous_tasks)

    store = state.capability_shadow_operational_store
    evaluator_record = store.get_for_attempt_phase(
        evaluator_attempt.attempt_id,
        "evaluation",
    )
    write_record = store.get_for_attempt_phase(
        write_attempt.attempt_id,
        "evaluation",
    )
    assert evaluator_record is not None
    assert write_record is not None
    assert (evaluator_record.outcome, evaluator_record.reason_code) == (
        "evaluator_failed",
        "evaluator_failed",
    )
    assert (write_record.outcome, write_record.reason_code) == (
        "decision_write_failed",
        "decision_write_failed",
    )
    report = store.report()
    assert report.scheduled_total == 2
    assert report.completed_total == 0
    assert report.evaluation_counts["evaluator_failed"] == 1
    assert report.evaluation_counts["decision_write_failed"] == 1
    assert report.failed_total == 2


@pytest.mark.asyncio
async def test_shadow_shutdown_cancellation_is_operational_not_node_fault(
    evidence_server,
    monkeypatch,
):
    client, database, settings = evidence_server
    settings["capability_evidence_mode"] = "shadow"
    descriptor = _descriptor()
    registration = _register(client, "shutdown-worker", descriptor)
    monkeypatch.setattr(
        state,
        "_schedule_capability_shadow_operation",
        state._record_capability_shadow_operation,
    )
    evaluator_entered = threading.Event()
    release_evaluator = threading.Event()
    evaluator_exited = threading.Event()

    def block_evaluator(**_kwargs):
        evaluator_entered.set()
        release_evaluator.wait()
        evaluator_exited.set()
        raise RuntimeError("released after coordinator shutdown")

    monkeypatch.setattr(state, "_build_capability_shadow_evaluation", block_evaluator)
    _task, attempt, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="shadow-shutdown",
    )
    assert state.schedule_capability_shadow_evaluation(
        attempt,
        actual_descriptor=descriptor,
        resource_requirements=None,
        required_capabilities=("code",),
        eligible_node_ids=("shutdown-worker",),
        decision_at=time.time(),
    ) is True
    assert await asyncio.wait_for(
        asyncio.to_thread(evaluator_entered.wait),
        timeout=5,
    )
    try:
        await state.shutdown_capability_shadow_evaluations()
    finally:
        release_evaluator.set()
    assert await asyncio.wait_for(
        asyncio.to_thread(evaluator_exited.wait),
        timeout=5,
    )

    record = state.capability_shadow_operational_store.get_for_attempt_phase(
        attempt.attempt_id,
        "evaluation",
    )
    assert record is not None
    assert (record.outcome, record.reason_code) == (
        "cancelled_on_shutdown",
        "coordinator_shutdown",
    )
    assert state.node_failure_count.get("shutdown-worker", 0) == 0
    with sqlite3.connect(state.capability_shadow_operational_store.path) as con:
        columns = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info(capability_shadow_operational_events)"
            )
        }
    assert columns == {
        "event_id",
        "attempt_id",
        "phase",
        "outcome",
        "reason_code",
        "occurred_at",
    }


@pytest.mark.asyncio
async def test_shutdown_waits_for_inflight_decision_write_before_classifying(
    evidence_server,
    monkeypatch,
):
    client, database, settings = evidence_server
    settings["capability_evidence_mode"] = "shadow"
    descriptor = _descriptor()
    registration = _register(client, "shutdown-decision-worker", descriptor)
    monkeypatch.setattr(
        state,
        "_schedule_capability_shadow_operation",
        state._record_capability_shadow_operation,
    )
    original_record = state.capability_shadow_decision_store.record
    write_entered = threading.Event()
    release_write = threading.Event()

    def blocking_record(evaluation):
        write_entered.set()
        release_write.wait()
        return original_record(evaluation)

    monkeypatch.setattr(
        state.capability_shadow_decision_store,
        "record",
        blocking_record,
    )
    _task, attempt, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="shadow-shutdown-decision",
    )
    assert state.schedule_capability_shadow_evaluation(
        attempt,
        actual_descriptor=descriptor,
        resource_requirements=None,
        required_capabilities=("code",),
        eligible_node_ids=("shutdown-decision-worker",),
        decision_at=time.time(),
    ) is True
    assert await asyncio.wait_for(
        asyncio.to_thread(write_entered.wait),
        timeout=5,
    )

    shutdown_checked_write = asyncio.Event()

    class ObservedDecisionWrites(set):
        def __contains__(self, item):
            shutdown_checked_write.set()
            return super().__contains__(item)

    monkeypatch.setattr(
        state,
        "_capability_shadow_decision_writes",
        ObservedDecisionWrites(state._capability_shadow_decision_writes),
    )
    shutdown_task = asyncio.create_task(
        state.shutdown_capability_shadow_evaluations()
    )
    await asyncio.wait_for(shutdown_checked_write.wait(), timeout=5)
    assert not shutdown_task.done()
    assert attempt.attempt_id not in state._capability_shadow_terminal_claims
    release_write.set()
    await asyncio.wait_for(shutdown_task, timeout=5)

    with sqlite3.connect(state.capability_shadow_decision_store.path) as con:
        assert con.execute(
            "SELECT outcome FROM capability_shadow_decisions "
            "WHERE actual_attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone() == ("no_preference",)
    terminal = state.capability_shadow_operational_store.get_for_attempt_phase(
        attempt.attempt_id,
        "evaluation",
    )
    assert terminal is not None
    assert (terminal.outcome, terminal.reason_code) == (
        "completed",
        "decision_persisted",
    )


@pytest.mark.asyncio
async def test_shutdown_bounds_inflight_scope_capture_and_classifies_it(
    evidence_server,
    monkeypatch,
):
    client, _database, settings = evidence_server
    settings["capability_evidence_mode"] = "shadow"
    descriptor = _descriptor()
    registration = _register(client, "shutdown-capture-worker", descriptor)
    monkeypatch.setattr(
        state,
        "_schedule_capability_shadow_operation",
        state._record_capability_shadow_operation,
    )
    monkeypatch.setattr(state, "_CAPABILITY_SHADOW_SHUTDOWN_DRAIN_SECONDS", 0)
    capture_entered = threading.Event()
    release_capture = threading.Event()
    capture_exited = threading.Event()

    def blocking_capture(**_kwargs):
        capture_entered.set()
        release_capture.wait()
        capture_exited.set()
        return ()

    monkeypatch.setattr(state, "_capture_capability_shadow_scopes", blocking_capture)
    _task, attempt, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="shadow-shutdown-capture-timeout",
    )
    decision_at = time.time()
    assert state.schedule_capability_shadow_evaluation(
        attempt,
        actual_descriptor=descriptor,
        resource_requirements=None,
        required_capabilities=("code",),
        eligible_node_ids=("shutdown-capture-worker",),
        decision_at=decision_at,
    ) is True
    assert await asyncio.wait_for(
        asyncio.to_thread(capture_entered.wait),
        timeout=5,
    )

    try:
        await asyncio.wait_for(
            state.shutdown_capability_shadow_evaluations(),
            timeout=2,
        )
    finally:
        release_capture.set()
    assert await asyncio.wait_for(
        asyncio.to_thread(capture_exited.wait),
        timeout=5,
    )

    admission = state.capability_shadow_operational_store.get_for_attempt_phase(
        attempt.attempt_id,
        "admission",
    )
    assert admission is not None
    assert (admission.outcome, admission.reason_code) == (
        "scope_capture_failed",
        "coordinator_shutdown_during_scope_capture",
    )
    assert admission.occurred_at == decision_at
    assert state.capability_shadow_operational_store.get_for_attempt_phase(
        attempt.attempt_id,
        "evaluation",
    ) is None
    assert state.node_failure_count.get("shutdown-capture-worker", 0) == 0


@pytest.mark.asyncio
async def test_shutdown_bounds_inflight_decision_write_without_false_cancellation(
    evidence_server,
    monkeypatch,
):
    client, database, settings = evidence_server
    settings["capability_evidence_mode"] = "shadow"
    descriptor = _descriptor()
    registration = _register(client, "shutdown-write-timeout-worker", descriptor)
    monkeypatch.setattr(state, "_CAPABILITY_SHADOW_SHUTDOWN_DRAIN_SECONDS", 0)
    original_operation = state._record_capability_shadow_operation
    terminal_recorded = threading.Event()

    def observed_operation(**kwargs):
        original_operation(**kwargs)
        if kwargs["phase"] == "evaluation":
            terminal_recorded.set()

    monkeypatch.setattr(state, "_record_capability_shadow_operation", observed_operation)
    monkeypatch.setattr(
        state,
        "_schedule_capability_shadow_operation",
        observed_operation,
    )
    original_decision_record = state.capability_shadow_decision_store.record
    write_entered = threading.Event()
    release_write = threading.Event()

    def blocking_record(evaluation):
        write_entered.set()
        release_write.wait()
        return original_decision_record(evaluation)

    monkeypatch.setattr(
        state.capability_shadow_decision_store,
        "record",
        blocking_record,
    )
    _task, attempt, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="shadow-shutdown-write-timeout",
    )
    assert state.schedule_capability_shadow_evaluation(
        attempt,
        actual_descriptor=descriptor,
        resource_requirements=None,
        required_capabilities=("code",),
        eligible_node_ids=("shutdown-write-timeout-worker",),
        decision_at=time.time(),
    ) is True
    assert await asyncio.wait_for(
        asyncio.to_thread(write_entered.wait),
        timeout=5,
    )
    counter_before = (
        state.capability_shadow_process_counters.snapshot().unexpected_containment_failure
    )

    await asyncio.wait_for(
        state.shutdown_capability_shadow_evaluations(),
        timeout=2,
    )
    assert state.capability_shadow_operational_store.get_for_attempt_phase(
        attempt.attempt_id,
        "evaluation",
    ) is None
    assert (
        state.capability_shadow_process_counters.snapshot().unexpected_containment_failure
        == counter_before + 1
    )

    release_write.set()
    assert await asyncio.wait_for(
        asyncio.to_thread(terminal_recorded.wait),
        timeout=5,
    )
    terminal = state.capability_shadow_operational_store.get_for_attempt_phase(
        attempt.attempt_id,
        "evaluation",
    )
    assert terminal is not None
    assert (terminal.outcome, terminal.reason_code) == (
        "completed",
        "decision_persisted",
    )
    with sqlite3.connect(state.capability_shadow_decision_store.path) as con:
        assert con.execute(
            "SELECT outcome FROM capability_shadow_decisions "
            "WHERE actual_attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone() == ("no_preference",)
    assert state.node_failure_count.get("shutdown-write-timeout-worker", 0) == 0


def test_operational_store_failure_is_local_and_cannot_change_assignment_or_settlement(
    evidence_server,
    monkeypatch,
    caplog,
):
    client, _database, settings = evidence_server
    settings["capability_evidence_mode"] = "shadow"
    descriptor = _descriptor()
    registration = _register(client, "store-failure-worker", descriptor)
    monkeypatch.setattr(
        state,
        "_schedule_capability_shadow_operation",
        state._record_capability_shadow_operation,
    )

    def fail_scope_capture(**_kwargs):
        raise RuntimeError("PRIVATE SCOPE CAPTURE FAILURE")

    def fail_operational_write(**_kwargs):
        raise RuntimeError("PRIVATE OPERATIONAL STORE FAILURE")

    monkeypatch.setattr(state, "_capture_capability_shadow_scopes", fail_scope_capture)
    monkeypatch.setattr(
        state.capability_shadow_operational_store,
        "record",
        fail_operational_write,
    )
    _queue_task("store-failure-routing", eligible_nodes=["store-failure-worker"])
    handout = _claim(client, "store-failure-worker", registration)
    assert handout["task_id"] == "store-failure-routing"
    assert state.attempt_store.count_attempts("store-failure-routing") == 1
    issued = state.attempt_store.get(handout["attempt_id"])
    assert issued is not None
    assert issued.assigned_node_id == "store-failure-worker"

    response = client.post(
        "/tasks/store-failure-routing/result",
        json=_result_body(handout, "store-failure-worker"),
        headers={"X-Node-Session": registration["session_token"]},
    )
    assert response.status_code == 200
    settled = state.attempt_store.get(handout["attempt_id"])
    assert settled is not None
    assert settled.state == "settled"
    assert state.attempt_store.count_attempts("store-failure-routing") == 1
    counters = state.capability_shadow_process_counters.snapshot()
    assert counters.durable_health_record_write_failure == 1
    assert counters.unexpected_containment_failure == 0
    assert counters.background_task_callback_failure == 0
    assert "PRIVATE OPERATIONAL STORE FAILURE" not in caplog.text
    assert "PRIVATE SCOPE CAPTURE FAILURE" not in caplog.text


def test_shadow_stores_are_isolated_from_authoritative_attempt_database(
    evidence_server,
):
    _client, database, _settings = evidence_server
    operational_path = state.capability_shadow_operational_store.path
    decision_path = state.capability_shadow_decision_store.path

    assert operational_path != database
    assert decision_path == operational_path
    assert operational_path.exists()
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = "
            "'capability_shadow_operational_events'"
        ).fetchone() is None
        # PR #61 databases retain this legacy table for copy-forward, but the
        # isolated runtime store is the only destination for new decisions.
        assert con.execute(
            "SELECT COUNT(*) FROM capability_shadow_decisions"
        ).fetchone() == (0,)


def test_legacy_shadow_decisions_copy_forward_idempotently(
    evidence_server,
):
    client, database, _settings = evidence_server
    descriptor = _descriptor()
    registration = _register(client, "legacy-decision-worker", descriptor)
    _task, attempt, _nonce, _enrollment = _issue_scoped_attempt(
        registration,
        descriptor,
        task_id="legacy-shadow-decision-copy",
    )
    candidate_scopes = state._capture_capability_shadow_scopes(
        actual_attempt=attempt,
        actual_descriptor=descriptor,
        resource_requirements=None,
        required_capabilities=("code",),
        eligible_node_ids=("legacy-decision-worker",),
        captured_at=150,
        candidate_claims=state._snapshot_capability_shadow_candidate_claims(
            actual_node_id="legacy-decision-worker",
            eligible_node_ids=("legacy-decision-worker",),
        ),
    )
    actual_scope = candidate_scopes[0]
    evaluation = state._build_capability_shadow_evaluation(
        actual_attempt_id=attempt.attempt_id,
        actual_scope_key=actual_scope.scope_key,
        candidate_scopes=candidate_scopes,
        minimum_samples=2,
        decision_at=150,
    )
    legacy_store = CapabilityShadowDecisionStore(database)
    legacy_record = legacy_store.record(evaluation, recorded_at=151)

    state._migrate_capability_shadow_decision_store()
    state._migrate_capability_shadow_decision_store()

    copied = state.capability_shadow_decision_store.get(
        legacy_record.evaluation.decision_id
    )
    assert copied == legacy_record
    with sqlite3.connect(state.capability_shadow_decision_store.path) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM capability_shadow_decisions "
            "WHERE decision_id = ?",
            (legacy_record.evaluation.decision_id,),
        ).fetchone() == (1,)


def test_operational_store_migration_failure_does_not_gain_startup_authority(
    evidence_server,
    monkeypatch,
    caplog,
):
    _client, _database, _settings = evidence_server
    private_error = "PRIVATE OPERATIONAL MIGRATION FAILURE 1c2d"

    def fail_migration():
        raise RuntimeError(private_error)

    monkeypatch.setattr(
        state.capability_shadow_operational_store,
        "migrate",
        fail_migration,
    )
    state._migrate_capability_shadow_operational_store()

    counters = state.capability_shadow_process_counters.snapshot()
    assert counters.durable_health_record_write_failure == 1
    assert counters.unexpected_containment_failure == 0
    assert private_error not in caplog.text


def test_protected_report_contains_operational_read_failure_and_exposes_counters(
    evidence_server,
    monkeypatch,
    caplog,
):
    client, _database, _settings = evidence_server
    private_error = "PRIVATE OPERATIONAL READ FAILURE 8f7e"

    def fail_report(**_kwargs):
        raise RuntimeError(private_error)

    monkeypatch.setattr(
        state.capability_shadow_operational_store,
        "report",
        fail_report,
    )
    response = client.get(
        "/v1/operator/capability-evidence",
        headers={"Authorization": f"Bearer {VIEWER_KEY}"},
    )

    assert response.status_code == 200
    health = response.json()["shadow_operational_health"]
    assert health["durable"] == {
        "available": False,
        "counts_by_phase": None,
        "orphan_evaluation_total": None,
        "assignment_observation_total": None,
        "offered_total": None,
        "scheduled_total": None,
        "completed_total": None,
        "skipped_total": None,
        "failed_total": None,
        "pending_total": None,
        "drop_failure_numerator": None,
        "drop_failure_denominator": None,
        "drop_failure_rate": None,
        "window": {
            "started_at": None,
            "ended_at": None,
            "cohort_basis": "inclusive_admission_occurred_at",
        },
        "latest_event_at": None,
    }
    assert health["process_local"]["unexpected_containment_failure"] == 1
    assert private_error not in response.text
    assert private_error not in caplog.text


def test_protected_report_contains_shadow_decision_read_failure(
    evidence_server,
    monkeypatch,
    caplog,
):
    client, _database, _settings = evidence_server
    private_error = "PRIVATE SHADOW DECISION READ FAILURE 6a71"

    def fail_report(_scope_keys):
        raise RuntimeError(private_error)

    monkeypatch.setattr(
        state.capability_shadow_decision_store,
        "aggregate_counts_for_scope_keys",
        fail_report,
    )
    response = client.get(
        "/v1/operator/capability-evidence",
        headers={"Authorization": f"Bearer {VIEWER_KEY}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["shadow_decision_aggregates_available"] is False
    assert payload["shadow_operational_health"]["durable"]["available"] is True
    assert payload["shadow_operational_health"]["process_local"][
        "unexpected_containment_failure"
    ] == 1
    assert private_error not in caplog.text


def test_protected_shadow_health_report_has_reproducible_totals_and_no_secrets(
    evidence_server,
    monkeypatch,
    caplog,
):
    client, database, settings = evidence_server
    descriptor = _descriptor()
    registration = _register(client, "report-worker", descriptor)
    monkeypatch.setattr(
        state,
        "_schedule_capability_shadow_operation",
        state._record_capability_shadow_operation,
    )
    private_prompt = "REPORT-PROMPT-PRIVATE-46d0"
    private_output = "REPORT-OUTPUT-PRIVATE-a572"
    private_error = "REPORT-WORKER-ERROR-PRIVATE-c194"

    _queue_task(
        "report-output",
        eligible_nodes=["report-worker"],
        prompt=private_prompt,
    )
    output_task = _claim(client, "report-worker", registration)
    assert client.post(
        "/tasks/report-output/result",
        json=_result_body(output_task, "report-worker", output=private_output),
        headers={"X-Node-Session": registration["session_token"]},
    ).status_code == 200
    _queue_task(
        "report-error",
        eligible_nodes=["report-worker"],
        prompt=private_prompt,
    )
    error_task = _claim(client, "report-worker", registration)
    assert client.post(
        "/tasks/report-error/result",
        json=_result_body(
            error_task,
            "report-worker",
            output=None,
            error=private_error,
        ),
        headers={"X-Node-Session": registration["session_token"]},
    ).status_code == 200

    operational = state.capability_shadow_operational_store
    admissions = (
        ("report-not-applicable", "not_applicable", "nonproduction_attempt"),
        (
            "report-queue-saturated",
            "queue_saturated",
            "background_queue_limit_reached",
        ),
        (
            "report-scope-failed",
            "scope_capture_failed",
            "scope_capture_failed",
        ),
    )
    for index, (attempt_id, outcome, reason_code) in enumerate(admissions):
        operational.record(
            attempt_id=attempt_id,
            phase="admission",
            outcome=outcome,
            reason_code=reason_code,
            occurred_at=10 + index,
        )
    evaluations = (
        ("report-completed", "completed", "decision_persisted"),
        ("report-evaluator-failed", "evaluator_failed", "evaluator_failed"),
        (
            "report-write-failed",
            "decision_write_failed",
            "decision_write_failed",
        ),
        (
            "report-cancelled",
            "cancelled_on_shutdown",
            "coordinator_shutdown",
        ),
    )
    for index, (attempt_id, outcome, reason_code) in enumerate(evaluations):
        operational.record(
            attempt_id=attempt_id,
            phase="admission",
            outcome="scheduled",
            reason_code="evaluation_scheduled",
            occurred_at=20 + index,
        )
        operational.record(
            attempt_id=attempt_id,
            phase="evaluation",
            outcome=outcome,
            reason_code=reason_code,
            occurred_at=30 + index,
        )

    assert client.get("/v1/operator/capability-evidence").status_code == 401
    response = client.get(
        "/v1/operator/capability-evidence",
        headers={"Authorization": f"Bearer {VIEWER_KEY}"},
    )
    assert response.status_code == 200
    payload = response.json()
    metrics_response = client.get(
        "/metrics",
        headers={"Authorization": f"Bearer {VIEWER_KEY}"},
    )
    assert metrics_response.status_code == 200
    health = payload["shadow_operational_health"]
    assert health["meaning"] == "operational_experiment_health_not_node_reputation"
    assert health["authoritative"] is False
    assert health["affects_routing"] is False
    durable = health["durable"]
    assert durable["available"] is True
    assert durable["orphan_evaluation_total"] == 0
    assert durable["counts_by_phase"] == {
        "admission": {
            "disabled": 2,
            "not_applicable": 1,
            "queue_saturated": 1,
            "scope_capture_failed": 1,
            "scheduled": 4,
        },
        "evaluation": {
            "completed": 1,
            "evaluator_failed": 1,
            "decision_write_failed": 1,
            "cancelled_on_shutdown": 1,
        },
    }
    assert {
        key: durable[key]
        for key in (
            "assignment_observation_total",
            "offered_total",
            "scheduled_total",
            "completed_total",
            "skipped_total",
            "failed_total",
            "pending_total",
            "drop_failure_numerator",
            "drop_failure_denominator",
        )
    } == {
        "assignment_observation_total": 9,
        "offered_total": 6,
        "scheduled_total": 4,
        "completed_total": 1,
        "skipped_total": 3,
        "failed_total": 5,
        "pending_total": 0,
        "drop_failure_numerator": 5,
        "drop_failure_denominator": 6,
    }
    assert durable["drop_failure_rate"] == pytest.approx(5 / 6)
    assert durable["latest_event_at"] is not None
    assert health["process_local"] == {
        "reset_at": 10.0,
        "durable_health_record_write_failure": 0,
        "unexpected_containment_failure": 0,
        "background_task_callback_failure": 0,
        "durable": False,
    }

    with sqlite3.connect(state.capability_shadow_operational_store.path) as con:
        con.row_factory = sqlite3.Row
        operational_rows = [
            dict(row)
            for row in con.execute(
                "SELECT * FROM capability_shadow_operational_events"
            ).fetchall()
        ]
    serialized = "\n".join(
        (
            response.text,
            metrics_response.text,
            json.dumps(operational_rows, sort_keys=True),
            json.dumps(state.pipeline_events, sort_keys=True),
            caplog.text,
        )
    )
    for secret in (
        private_prompt,
        private_output,
        private_error,
        output_task["nonce"],
        error_task["nonce"],
        registration["session_token"],
        registration["enrollment_credential"],
        settings["viewer_key"],
    ):
        assert secret not in serialized
    assert all(
        forbidden not in json.dumps(operational_rows, sort_keys=True)
        for forbidden in (
            '"prompt"',
            '"output"',
            '"error"',
            '"nonce"',
            '"session_token"',
            '"credential"',
        )
    )


def test_shadow_scope_capture_excludes_insufficient_output_capacity(
    evidence_server,
):
    client, _database, settings = evidence_server
    settings["capability_evidence_mode"] = "off"
    eligible_descriptor = _descriptor(max_output_bytes=8192)
    low_descriptor = _descriptor(max_output_bytes=1024)
    eligible_registration = _register(client, "output-eligible", eligible_descriptor)
    _register(client, "output-too-small", low_descriptor)
    _queue_task(
        "output-shadow-filter",
        eligible_nodes=["output-eligible", "output-too-small"],
        max_output_bytes=4096,
    )
    handout = _claim(client, "output-eligible", eligible_registration)
    attempt = state.attempt_store.get(handout["attempt_id"])
    assert attempt is not None
    assert attempt.max_output_bytes == 4096
    scopes = state._capture_capability_shadow_scopes(
        actual_attempt=attempt,
        actual_descriptor=eligible_descriptor,
        resource_requirements=None,
        required_capabilities=("code",),
        eligible_node_ids=("output-eligible", "output-too-small"),
        captured_at=time.time(),
    )
    assert [scope.enrollment_id for scope in scopes] == [
        eligible_registration["enrollment_id"]
    ]


def test_digestless_typed_scope_remains_assignable_observed_and_shadowed(
    evidence_server,
    monkeypatch,
):
    client, database, settings = evidence_server
    settings["capability_evidence_mode"] = "shadow"
    descriptor = _descriptor(model_digest=None)
    registration = _register(client, "digestless-worker", descriptor)
    monkeypatch.setattr(
        state,
        "_schedule_capability_shadow_operation",
        state._record_capability_shadow_operation,
    )
    terminal_recorded = threading.Event()
    operational = state.capability_shadow_operational_store
    original_record = operational.record

    def record_and_signal(**kwargs):
        record = original_record(**kwargs)
        if kwargs["phase"] == "evaluation":
            terminal_recorded.set()
        return record

    monkeypatch.setattr(operational, "record", record_and_signal)
    _queue_task("digestless-shadow", eligible_nodes=["digestless-worker"])
    handout = _claim(client, "digestless-worker", registration)
    assert handout["task_id"] == "digestless-shadow"
    attempt = state.attempt_store.get(handout["attempt_id"])
    assert attempt is not None
    assert attempt.assigned_node_id == "digestless-worker"
    assert attempt.assigned_model_digest is None
    assert terminal_recorded.wait(timeout=5)
    assert client.post(
        "/tasks/digestless-shadow/result",
        json=_result_body(handout, "digestless-worker"),
        headers={"X-Node-Session": registration["session_token"]},
    ).status_code == 200

    assert _observations(
        database,
        attempt_id=handout["attempt_id"],
    )
    admission = operational.get_for_attempt_phase(handout["attempt_id"], "admission")
    terminal = operational.get_for_attempt_phase(handout["attempt_id"], "evaluation")
    assert admission is not None and admission.outcome == "scheduled"
    assert terminal is not None and terminal.outcome == "completed"

    response = client.get(
        "/v1/operator/capability-evidence",
        headers={"Authorization": f"Bearer {VIEWER_KEY}"},
    )
    assert response.status_code == 200
    payload = response.json()
    scope = next(
        item
        for item in payload["scopes"]
        if item["enrollment_id"] == registration["enrollment_id"]
    )
    assert scope["model"]["digest"] is None
    assert scope["future_active_experiment_eligibility"] == {
        "eligible_for_future_active_experiment": False,
        "blocking_reasons": ["immutable_model_identity_missing"],
        "meaning": (
            "identity_prerequisites_only_not_correctness_reputation_trust_"
            "or_active_routing"
        ),
    }
    assert scope["shadow_policy"]["decision_count"] == 1
    assert scope["shadow_policy"]["no_preference_count"] == 1
    assert payload["future_active_experiment_eligibility"][
        "blocking_reason_counts"
    ]["immutable_model_identity_missing"] == 1
    assert payload["future_active_experiment_eligibility"][
        "active_routing_implemented"
    ] is False
    assert payload["affects_routing"] is False
