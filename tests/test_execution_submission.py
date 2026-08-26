"""Retry-safe canonical execution submission and digest-only persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import string
from asyncio import sleep
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock, RLock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import execution.service as service_module
import routes_executions
import routes_pitch
import server_state as state
from execution.contracts import ExecutionRequestV1
from execution.dispatch import Dispatcher
from execution.idempotency import (
    InvalidIdempotencyKey,
    REQUEST_HASH_VERSION_V1,
    REQUEST_HASH_VERSION_V2,
    RequestHashVersionIncompatible,
    SubmissionIdentity,
    UnsupportedRequestHashVersion,
    canonical_request_digest,
    canonical_request_json,
    request_hash_version,
    submission_identity,
    validate_idempotency_key,
)
from execution.persistence import (
    ExecutionStore,
    IdempotencyConflictError,
    SubmissionConsistencyError,
    SubmissionDisposition,
)
from execution.service import (
    ExecutionPersistenceError,
    ExecutionService,
    ServiceExecution,
    SubmissionActivationError,
)


_PRE_THEME_2B_TASK = "Pre-Theme-2 replay fixture"
_PRE_THEME_2B_CANONICAL_JSON = (
    '{"confidentiality":"local_only","max_output_bytes":1048576,'
    '"network_policy":"disabled","output_contract":null,"placement":"local",'
    '"project_id":null,"protocol_version":"1","remote_dispatch_consent":false,'
    '"requirements":{"allow_local_fallback":true,"approved_node_ids":[],'
    '"required_capabilities":[]},"strategy":"direct","strategy_options":null,'
    '"task":"Pre-Theme-2 replay fixture","timeout_seconds":1800,'
    '"verification":{"allow_unverified_fallback":true,"require_all":true,'
    '"validators":[]}}'
)
_PRE_THEME_2B_REQUEST_JSON = (
    '{"protocol_version":"1","task":"Pre-Theme-2 replay fixture",'
    '"project_id":null,"strategy":"direct","strategy_options":null,'
    '"placement":"local","remote_dispatch_consent":false,'
    '"requirements":{"required_capabilities":[],"approved_node_ids":[],'
    '"allow_local_fallback":true},"output_contract":null,'
    '"verification":{"validators":[],"allow_unverified_fallback":true,'
    '"require_all":true},"confidentiality":"local_only",'
    '"timeout_seconds":1800,"max_output_bytes":1048576,'
    '"network_policy":"disabled"}'
)
_PRE_THEME_2B_REQUEST_HASH = (
    "c806e1c1eff81f8de2c9d30c84268e44a2d60a57e8e245875d139f7b02998b31"
)


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


@pytest.fixture
def pre_theme_2b_submission_database(tmp_path):
    """A populated execution/mapping pair using the pre-Theme-2 schema and bytes."""

    database = tmp_path / "pre-theme-2b.db"
    request = ExecutionRequestV1(task=_PRE_THEME_2B_TASK, strategy="direct")
    identity = submission_identity(
        request,
        idempotency_key="pre-theme-2b-key",
        requester_scope_kind="pitch-key",
        requester_scope_value="pre-theme-2b-requester",
    )
    assert identity.request_hash == _PRE_THEME_2B_REQUEST_HASH
    service = ExecutionService(store=ExecutionStore(database))
    queued = service._new_result(request, "f" * 32, None, "queued")
    service.store.create_or_replay_submission(request, identity, lambda: queued)

    with sqlite3.connect(database) as con:
        mapping = con.execute(
            """
            SELECT requester_scope_hash, idempotency_key_hash, request_hash,
                   execution_id, created_at
            FROM execution_submissions
            """
        ).fetchone()
        con.execute(
            "UPDATE executions SET request_json = ? WHERE execution_id = ?",
            (_PRE_THEME_2B_REQUEST_JSON, queued.execution_id),
        )
        con.execute("DROP TABLE execution_submissions")
        con.execute(
            """
            CREATE TABLE execution_submissions (
                requester_scope_hash TEXT NOT NULL,
                idempotency_key_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (requester_scope_hash, idempotency_key_hash),
                FOREIGN KEY (execution_id) REFERENCES executions(execution_id)
                    ON DELETE RESTRICT
            )
            """
        )
        con.execute(
            """
            INSERT INTO execution_submissions (
                requester_scope_hash, idempotency_key_hash, request_hash,
                execution_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            mapping,
        )
        con.execute(
            "CREATE INDEX idx_execution_submissions_execution_id "
            "ON execution_submissions(execution_id)"
        )
        con.commit()

    return database, request, identity, queued.execution_id


class CommitThenRaiseStore(ExecutionStore):
    """Commit one initial submission, then hide that outcome from its caller."""

    def __init__(
        self,
        path,
        *,
        keyed: bool = False,
        unkeyed: bool = False,
        committed: Event | None = None,
        release: Event | None = None,
    ):
        super().__init__(path)
        self._fault_lock = Lock()
        self._raise_keyed = keyed
        self._raise_unkeyed = unkeyed
        self.committed = committed
        self.release = release
        self.keyed_dispositions: list[SubmissionDisposition] = []
        self.keyed_candidate_ids: list[str] = []
        self.unkeyed_candidate_ids: list[str] = []

    def _raise_once_after_commit(self, kind: str) -> None:
        with self._fault_lock:
            attribute = f"_raise_{kind}"
            should_raise = bool(getattr(self, attribute))
            if should_raise:
                setattr(self, attribute, False)
        if not should_raise:
            return
        if self.committed is not None:
            self.committed.set()
        if self.release is not None and not self.release.wait(timeout=10):
            raise AssertionError("test did not release ambiguous commit")
        raise sqlite3.OperationalError("simulated exception after durable commit")

    def create_or_replay_submission(self, request, identity, result_factory):
        def record_candidate():
            candidate = result_factory()
            self.keyed_candidate_ids.append(candidate.execution_id)
            return candidate

        record = super().create_or_replay_submission(
            request,
            identity,
            record_candidate,
        )
        self.keyed_dispositions.append(record.disposition)
        self._raise_once_after_commit("keyed")
        return record

    def create(self, request, result):
        self.unkeyed_candidate_ids.append(result.execution_id)
        super().create(request, result)
        self._raise_once_after_commit("unkeyed")


class ConflictingInitialStore(ExecutionStore):
    """Commit another initial row under the candidate ID, then raise."""

    def __init__(self, path):
        super().__init__(path)
        self.injected = False
        self.candidate_ids: list[str] = []

    def create(self, request, result):
        self.candidate_ids.append(result.execution_id)
        if not self.injected:
            self.injected = True
            conflicting_request = request.model_copy(
                update={"task": "Conflicting durable request"}
            )
            conflicting_result = result.model_copy(deep=True)
            conflicting_result.task = conflicting_request.task
            super().create(conflicting_request, conflicting_result)
            raise sqlite3.OperationalError(
                "simulated unknown commit with conflicting row"
            )
        return super().create(request, result)


class RaiseAfterTaskRegistration(dict):
    """Expose the boundary after create_task returned its one task handle."""

    def __init__(self):
        super().__init__()
        self.created_tasks: list[asyncio.Task] = []
        self.raised = False

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.created_tasks.append(value)
        if not self.raised:
            self.raised = True
            raise RuntimeError("simulated failure after task registration")


class FailActivationPreflightOnceStore(ExecutionStore):
    """Fail the first durable read after initial creation has committed."""

    def __init__(self, path):
        super().__init__(path)
        self.failed = False
        self.get_calls = 0

    def get(self, execution_id):
        self.get_calls += 1
        if not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("simulated activation preflight outage")
        return super().get(execution_id)


class RejectInitialRunningStore(ExecutionStore):
    """Keep the durable candidate queued while running publication fails."""

    def __init__(self, path):
        super().__init__(path)
        self.running_attempts = 0

    def save(self, request, result):
        if result.lifecycle_status == "running":
            self.running_attempts += 1
            raise sqlite3.OperationalError("simulated running publication outage")
        return super().save(request, result)


class FailCrashInspectionOnceStore(ExecutionStore):
    """Fail the crash handler's read after activation preflight succeeded."""

    def __init__(self, path):
        super().__init__(path)
        self.get_calls = 0

    def get(self, execution_id):
        self.get_calls += 1
        if self.get_calls == 2:
            raise sqlite3.OperationalError("simulated crash-state read outage")
        return super().get(execution_id)


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


def test_pre_theme_2b_request_hash_v1_bytes_are_frozen():
    request = ExecutionRequestV1(task=_PRE_THEME_2B_TASK, strategy="direct")

    assert request_hash_version(request) == REQUEST_HASH_VERSION_V1
    assert canonical_request_json(request) == _PRE_THEME_2B_CANONICAL_JSON
    assert canonical_request_digest(request) == _PRE_THEME_2B_REQUEST_HASH


def test_empty_typed_requirement_block_preserves_request_hash_v1():
    legacy = ExecutionRequestV1(task="No effective typed requirement", strategy="direct")
    explicit_empty = ExecutionRequestV1(
        task="No effective typed requirement",
        strategy="direct",
        requirements={"resource_requirements": {}},
    )

    assert request_hash_version(explicit_empty) == REQUEST_HASH_VERSION_V1
    assert canonical_request_json(explicit_empty) == canonical_request_json(legacy)
    assert canonical_request_digest(explicit_empty) == canonical_request_digest(legacy)


def test_material_typed_requirements_use_v2_and_conflict_when_changed():
    first = ExecutionRequestV1(
        task="Typed resources",
        strategy="direct",
        requirements={
            "resource_requirements": {
                "minimum_memory_bytes": 8_589_934_592,
                "required_features": ["json", "code"],
            }
        },
    )
    reordered = ExecutionRequestV1(
        requirements={
            "resource_requirements": {
                "required_features": ["code", "json"],
                "minimum_memory_bytes": 8_589_934_592,
            }
        },
        strategy="direct",
        task="Typed resources",
    )
    changed = ExecutionRequestV1(
        task="Typed resources",
        strategy="direct",
        requirements={
            "resource_requirements": {
                "minimum_memory_bytes": 17_179_869_184,
                "required_features": ["code", "json"],
            }
        },
    )

    assert request_hash_version(first) == REQUEST_HASH_VERSION_V2
    assert canonical_request_json(first) == canonical_request_json(reordered)
    assert canonical_request_digest(first) == canonical_request_digest(reordered)
    assert canonical_request_digest(first) != canonical_request_digest(changed)
    with pytest.raises(RequestHashVersionIncompatible):
        canonical_request_digest(first, hash_version=REQUEST_HASH_VERSION_V1)
    with pytest.raises(UnsupportedRequestHashVersion):
        canonical_request_digest(first, hash_version="")


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


def test_submission_identity_legacy_constructor_defaults_to_hash_v1():
    identity = SubmissionIdentity(
        requester_scope_hash="0" * 64,
        idempotency_key_hash="1" * 64,
        request_hash="2" * 64,
    )

    assert identity.request_hash_version == REQUEST_HASH_VERSION_V1


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
        "request_hash_version",
        "execution_id",
        "created_at",
    }
    assert by_name["requester_scope_hash"][5] == 1
    assert by_name["idempotency_key_hash"][5] == 2
    assert by_name["request_hash_version"][3] == 1
    assert by_name["request_hash_version"][4] == "'1'"
    assert "idx_execution_submissions_execution_id" in indexes
    assert any(
        row[2] == "executions"
        and row[3] == "execution_id"
        and row[4] == "execution_id"
        for row in foreign_keys
    )


def test_pre_theme_2b_mapping_migrates_and_replays_with_stored_hash_version(
    pre_theme_2b_submission_database,
    monkeypatch,
):
    database, request, identity, execution_id = pre_theme_2b_submission_database
    with sqlite3.connect(database) as con:
        before = {
            row[1] for row in con.execute("PRAGMA table_info(execution_submissions)")
        }
    assert "request_hash_version" not in before

    store = ExecutionStore(database)
    store.migrate()
    store.migrate()
    row = store.raw_submission(
        identity.requester_scope_hash,
        identity.idempotency_key_hash,
    )
    assert row is not None
    assert row["request_hash"] == _PRE_THEME_2B_REQUEST_HASH
    assert row["request_hash_version"] == REQUEST_HASH_VERSION_V1

    service = ExecutionService(store=store)
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    replayed = service.submit_idempotent(request, identity)

    assert replayed.replayed is True
    assert replayed.result.execution_id == execution_id
    assert activated == []
    assert _counts(database) == (1, 1)


def test_typed_requirements_conflict_with_pre_theme_2b_mapping(
    pre_theme_2b_submission_database,
):
    database, request, _identity, execution_id = pre_theme_2b_submission_database
    typed_request = ExecutionRequestV1.model_validate(
        {
            **request.model_dump(mode="json"),
            "requirements": {
                **request.requirements.model_dump(mode="json"),
                "resource_requirements": {"minimum_logical_cpus": 8},
            },
        }
    )
    typed_identity = submission_identity(
        typed_request,
        idempotency_key="pre-theme-2b-key",
        requester_scope_kind="pitch-key",
        requester_scope_value="pre-theme-2b-requester",
    )

    assert typed_identity.request_hash_version == REQUEST_HASH_VERSION_V2
    service = ExecutionService(store=ExecutionStore(database))
    with pytest.raises(IdempotencyConflictError) as raised:
        service.submit_idempotent(typed_request, typed_identity)
    assert raised.value.execution_id == execution_id


def test_new_typed_mapping_replays_and_material_requirement_change_conflicts(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    request = ExecutionRequestV1(
        task="Versioned typed mapping",
        strategy="direct",
        requirements={"resource_requirements": {"minimum_logical_cpus": 4}},
    )
    changed = ExecutionRequestV1(
        task="Versioned typed mapping",
        strategy="direct",
        requirements={"resource_requirements": {"minimum_logical_cpus": 8}},
    )
    identity = submission_identity(
        request,
        idempotency_key="typed-version-key",
        requester_scope_kind="pitch-key",
        requester_scope_value="typed-version-requester",
    )
    changed_identity = submission_identity(
        changed,
        idempotency_key="typed-version-key",
        requester_scope_kind="pitch-key",
        requester_scope_value="typed-version-requester",
    )
    service = ExecutionService(store=ExecutionStore(database))
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)

    created = service.submit_idempotent(request, identity)
    replayed = service.submit_idempotent(request, identity)
    with pytest.raises(IdempotencyConflictError):
        service.submit_idempotent(changed, changed_identity)

    row = service.store.raw_submission(
        identity.requester_scope_hash,
        identity.idempotency_key_hash,
    )
    assert created.replayed is False
    assert replayed.replayed is True
    assert replayed.result.execution_id == created.result.execution_id
    assert row is not None
    assert row["request_hash_version"] == REQUEST_HASH_VERSION_V2
    assert activated == [created.result.execution_id]


def test_unknown_stored_request_hash_version_fails_closed(tmp_path, monkeypatch):
    database = tmp_path / "events.db"
    request = ExecutionRequestV1(task="Unknown hash version", strategy="direct")
    identity = submission_identity(
        request,
        idempotency_key="unknown-version-key",
        requester_scope_kind="pitch-key",
        requester_scope_value="unknown-version-requester",
    )
    service = ExecutionService(store=ExecutionStore(database))
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    created = service.submit_idempotent(request, identity)
    with sqlite3.connect(database) as con:
        con.execute(
            "UPDATE execution_submissions SET request_hash_version = '99' "
            "WHERE execution_id = ?",
            (created.result.execution_id,),
        )
        con.commit()

    with pytest.raises(SubmissionConsistencyError):
        service.submit_idempotent(request, identity)
    assert activated == [created.result.execution_id]


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


def test_exact_historical_candidate_match_on_first_attempt_remains_replay(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    store = ExecutionStore(database)
    request = ExecutionRequestV1(
        task="An exact historical row is still not this call",
        strategy="direct",
    )
    identity = submission_identity(
        request,
        idempotency_key="historical-exact-candidate-key",
        requester_scope_kind="pitch-key",
        requester_scope_value="historical-exact-candidate-requester",
    )
    execution_id = "a" * 32
    created_at = "2026-08-24T12:00:00+00:00"
    seed_service = ExecutionService(store=store)
    queued = seed_service._new_result(
        request,
        execution_id,
        None,
        "queued",
        created_at,
    )
    queued.lifecycle_status = "queued"
    seeded = store.create_or_replay_submission(request, identity, lambda: queued)
    assert seeded.disposition is SubmissionDisposition.CREATED

    class FixedUuid:
        hex = execution_id

    service = ExecutionService(store=store)
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    monkeypatch.setattr(service_module, "_now", lambda: created_at)
    monkeypatch.setattr(service_module.uuid, "uuid4", lambda: FixedUuid())

    submitted = service.submit_idempotent(request, identity)

    assert submitted.replayed is True
    assert submitted.result.execution_id == execution_id
    assert activated == []
    assert service._controls == {}
    assert service._background == {}
    assert _counts(database) == (1, 1)


def test_historical_candidate_identity_collision_replays_current_lifecycle(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    store = ExecutionStore(database)
    request = ExecutionRequestV1(
        task="Replay a progressed historical identity collision",
        strategy="direct",
    )
    identity = submission_identity(
        request,
        idempotency_key="historical-progressed-candidate-key",
        requester_scope_kind="pitch-key",
        requester_scope_value="historical-progressed-candidate-requester",
    )
    execution_id = "c" * 32
    created_at = "2026-08-24T12:30:00+00:00"
    seed_service = ExecutionService(store=store)
    queued = seed_service._new_result(
        request,
        execution_id,
        None,
        "queued",
        created_at,
    )
    queued.lifecycle_status = "queued"
    store.create_or_replay_submission(request, identity, lambda: queued)
    interrupted = queued.model_copy(deep=True)
    interrupted.status = "failed"
    interrupted.lifecycle_status = "interrupted"
    interrupted.interruption_reason = "historical interruption"
    interrupted.interrupted_at = "2026-08-24T12:31:00+00:00"
    interrupted.completed_at = interrupted.interrupted_at
    interrupted.retryable = True
    store.save(request, interrupted)

    class FixedUuid:
        hex = execution_id

    service = ExecutionService(store=store)
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    monkeypatch.setattr(service_module, "_now", lambda: created_at)
    monkeypatch.setattr(service_module.uuid, "uuid4", lambda: FixedUuid())

    submitted = service.submit_idempotent(request, identity)

    assert submitted.replayed is True
    assert submitted.result.lifecycle_status == "interrupted"
    assert submitted.result.execution_id == execution_id
    assert activated == []
    assert _counts(database) == (1, 1)


def test_keyed_commit_then_raise_recovers_owned_creation_and_activates_once(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    store = CommitThenRaiseStore(database, keyed=True)
    service = ExecutionService(store=store)
    emitted: list[str] = []
    execution_calls: list[str] = []
    remembered: list[tuple[str, str]] = []
    started = Event()
    release = Event()
    finished = Event()
    original_remember = service._remember

    def remember_once(request, result):
        remembered.append((result.execution_id, result.lifecycle_status))
        original_remember(request, result)

    async def fake_execute(_request, *, execution_id, control, **_kwargs):
        execution_calls.append(execution_id)
        started.set()
        try:
            assert await asyncio.to_thread(release.wait, 10)
        finally:
            finished.set()
        return ServiceExecution(result=control.result, legacy_payload={})

    monkeypatch.setattr(service, "execute", fake_execute)
    monkeypatch.setattr(service, "_emit", lambda name, _data: emitted.append(name))
    monkeypatch.setattr(service, "_remember", remember_once)
    client = _api(monkeypatch, service)
    headers = {"Idempotency-Key": "commit-then-raise-key"}

    with client:
        response = client.post(
            "/v1/executions",
            json={"task": "Recover this committed submission", "strategy": "direct"},
            headers=headers,
        )
        execution_id = response.json()["execution_id"]
        try:
            assert started.wait(timeout=5)
            assert list(service._controls) == [execution_id]
            assert list(service._background) == [execution_id]
            assert len(service._background) == 1
        finally:
            release.set()
        assert finished.wait(timeout=5)

    assert response.status_code == 202
    assert response.headers["Idempotency-Replayed"] == "false"
    assert execution_calls == [execution_id]
    assert remembered == [(execution_id, "queued")]
    assert emitted.count("execution_created") == 1
    assert emitted.count("strategy_selected") == 1
    assert store.keyed_dispositions == [
        SubmissionDisposition.CREATED,
        SubmissionDisposition.RECOVERED_CREATION,
    ]
    assert store.keyed_candidate_ids == [execution_id, execution_id]
    assert _counts(database) == (1, 1)


def test_concurrent_replay_during_ambiguous_commit_is_inert(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    committed = Event()
    release = Event()
    first = ExecutionService(
        store=CommitThenRaiseStore(
            database,
            keyed=True,
            committed=committed,
            release=release,
        )
    )
    second = ExecutionService(store=ExecutionStore(database))
    request = ExecutionRequestV1(
        task="Observe an in-flight ambiguous commit",
        strategy="direct",
    )
    identity = submission_identity(
        request,
        idempotency_key="concurrent-ambiguous-key",
        requester_scope_kind="pitch-key",
        requester_scope_value="concurrent-ambiguous-requester",
    )
    activated: list[str] = []
    activation_lock = Lock()
    _stub_activation(monkeypatch, first, activated, activation_lock)
    _stub_activation(monkeypatch, second, activated, activation_lock)

    with ThreadPoolExecutor(max_workers=1) as pool:
        originating_future = pool.submit(first.submit_idempotent, request, identity)
        assert committed.wait(timeout=5)

        replay = second.submit_idempotent(request, identity)
        assert replay.replayed is True
        assert activated == []
        assert second._controls == {}
        assert second._background == {}

        release.set()
        originating = originating_future.result(timeout=10)

    assert originating.replayed is False
    assert replay.result.execution_id == originating.result.execution_id
    assert activated == [originating.result.execution_id]
    assert _counts(database) == (1, 1)


@pytest.mark.asyncio
async def test_repeated_activation_is_idempotent_and_inconsistency_fails_closed(
    tmp_path,
    monkeypatch,
):
    service = ExecutionService(store=ExecutionStore(tmp_path / "events.db"))
    request = ExecutionRequestV1(task="Activate exactly once", strategy="direct")
    queued = service._new_result(request, "r" * 32, None, "queued")
    queued.lifecycle_status = "queued"
    service.store.create(request, queued)
    emitted: list[str] = []
    execute_calls: list[str] = []
    completion_callbacks: list[ServiceExecution] = []
    release = asyncio.Event()

    async def fake_execute(_request, *, execution_id, control, **_kwargs):
        execute_calls.append(execution_id)
        await release.wait()
        return ServiceExecution(result=control.result, legacy_payload={})

    monkeypatch.setattr(service, "execute", fake_execute)
    monkeypatch.setattr(service, "_emit", lambda name, _data: emitted.append(name))

    service._activate_committed_submission(
        request,
        queued,
        on_complete=completion_callbacks.append,
    )
    first_task = service._background[queued.execution_id]
    first_control = service._controls[queued.execution_id]
    repeated = service._activate_committed_submission(
        request,
        queued,
        on_complete=lambda _run: pytest.fail("repeat replaced completion callback"),
    )

    assert repeated.execution_id == queued.execution_id
    assert service._background[queued.execution_id] is first_task
    assert service._controls[queued.execution_id] is first_control
    assert emitted.count("execution_created") == 1
    assert emitted.count("strategy_selected") == 1

    inconsistent = queued.model_copy(update={"task": "Conflicting activation"})
    with pytest.raises(SubmissionConsistencyError):
        service._activate_committed_submission(request, inconsistent)

    await asyncio.sleep(0)
    assert execute_calls == [queued.execution_id]
    release.set()
    await first_task
    await asyncio.sleep(0)
    assert len(completion_callbacks) == 1


def test_cancellation_serializes_with_activation_cache_publication(
    tmp_path,
    monkeypatch,
):
    service = ExecutionService(store=ExecutionStore(tmp_path / "events.db"))
    request = ExecutionRequestV1(
        task="Cancellation must not leave a stale queued cache",
        strategy="direct",
    )
    cache_entered = Event()
    release_cache = Event()
    cancellation_started = Event()
    cancellation_waiting = Event()
    cancellation_finished = Event()
    execution_ids: list[str] = []
    original_remember = service._remember

    class ObservedActivationLock:
        def __init__(self):
            self.lock = RLock()

        def __enter__(self):
            if cancellation_started.is_set() and not release_cache.is_set():
                cancellation_waiting.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_args):
            self.lock.release()

    def blocking_remember(request_arg, result):
        if result.lifecycle_status == "queued" and not cache_entered.is_set():
            execution_ids.append(result.execution_id)
            cache_entered.set()
            assert release_cache.wait(timeout=10)
        original_remember(request_arg, result)

    async def wait_until_cancelled(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def submit_and_hold_loop():
        queued = service.submit(request)
        assert await asyncio.to_thread(cancellation_finished.wait, 10)
        return queued

    def run_cancellation():
        cancellation_started.set()
        try:
            return asyncio.run(service.cancel(execution_ids[0], "race cancellation"))
        finally:
            cancellation_finished.set()

    service._activation_lock = ObservedActivationLock()
    monkeypatch.setattr(service, "_remember", blocking_remember)
    monkeypatch.setattr(service, "execute", wait_until_cancelled)
    monkeypatch.setattr(Dispatcher, "cancel_execution", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_emit", lambda *_args, **_kwargs: None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        submitted_future = pool.submit(lambda: asyncio.run(submit_and_hold_loop()))
        assert cache_entered.wait(timeout=5)
        cancelled_future = pool.submit(run_cancellation)
        assert cancellation_waiting.wait(timeout=5)
        release_cache.set()
        cancelled = cancelled_future.result(timeout=10)
        queued = submitted_future.result(timeout=10)

    durable = service.store.get(queued.execution_id)
    visible = service.get(queued.execution_id)
    assert cancelled.lifecycle_status == "cancelled"
    assert durable.lifecycle_status == "cancelled"
    assert visible.lifecycle_status == "cancelled"
    assert service._live_results.get(queued.execution_id) is None
    assert service._controls == {}
    assert service._background == {}
    assert service._activating == set()


def test_unkeyed_commit_then_raise_recovers_stable_creation_once(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    store = CommitThenRaiseStore(database, unkeyed=True)
    service = ExecutionService(store=store)
    emitted: list[str] = []
    execution_calls: list[str] = []
    remembered: list[tuple[str, str]] = []
    started = Event()
    release = Event()
    finished = Event()
    original_remember = service._remember

    def remember_once(request, result):
        remembered.append((result.execution_id, result.lifecycle_status))
        original_remember(request, result)

    async def fake_execute(_request, *, execution_id, control, **_kwargs):
        execution_calls.append(execution_id)
        started.set()
        try:
            assert await asyncio.to_thread(release.wait, 10)
        finally:
            finished.set()
        return ServiceExecution(result=control.result, legacy_payload={})

    monkeypatch.setattr(service, "execute", fake_execute)
    monkeypatch.setattr(service, "_emit", lambda name, _data: emitted.append(name))
    monkeypatch.setattr(service, "_remember", remember_once)
    client = _api(monkeypatch, service)

    with client:
        response = client.post(
            "/v1/executions",
            json={"task": "Recover unkeyed initial commit", "strategy": "direct"},
        )
        execution_id = response.json()["execution_id"]
        try:
            assert started.wait(timeout=5)
            assert list(service._controls) == [execution_id]
            assert list(service._background) == [execution_id]
        finally:
            release.set()
        assert finished.wait(timeout=5)

    assert response.status_code == 202
    assert "Idempotency-Replayed" not in response.headers
    assert store.unkeyed_candidate_ids == [execution_id, execution_id]
    assert execution_calls == [execution_id]
    assert remembered == [(execution_id, "queued")]
    assert emitted.count("execution_created") == 1
    assert _counts(database) == (1, 0)


def test_unkeyed_exact_historical_candidate_on_first_attempt_fails_closed(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    store = ExecutionStore(database)
    request = ExecutionRequestV1(
        task="Do not adopt an unkeyed historical execution",
        strategy="direct",
    )
    execution_id = "b" * 32
    created_at = "2026-08-24T13:00:00+00:00"
    seed_service = ExecutionService(store=store)
    queued = seed_service._new_result(
        request,
        execution_id,
        None,
        "queued",
        created_at,
    )
    queued.lifecycle_status = "queued"
    store.create(request, queued)

    class FixedUuid:
        hex = execution_id

    service = ExecutionService(store=store)
    activated: list[str] = []
    _stub_activation(monkeypatch, service, activated)
    monkeypatch.setattr(service_module, "_now", lambda: created_at)
    monkeypatch.setattr(service_module.uuid, "uuid4", lambda: FixedUuid())

    with pytest.raises(SubmissionConsistencyError):
        service.submit(request)

    assert activated == []
    assert service._controls == {}
    assert service._background == {}
    assert _counts(database) == (1, 0)


def test_unkeyed_ambiguous_commit_with_conflicting_row_fails_closed(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    store = ConflictingInitialStore(database)
    service = ExecutionService(store=store)
    emitted: list[str] = []
    monkeypatch.setattr(service, "_emit", lambda name, _data: emitted.append(name))

    with pytest.raises(ExecutionPersistenceError) as raised:
        service.submit(
            ExecutionRequestV1(
                task="Candidate must not replace conflicting durable state",
                strategy="direct",
            )
        )

    assert raised.value.phase == "queued_submission"
    assert len(store.candidate_ids) == 3
    assert len(set(store.candidate_ids)) == 1
    assert store.get_request(store.candidate_ids[0]).task == "Conflicting durable request"
    assert _counts(database) == (1, 0)
    assert emitted == []
    assert service._controls == {}
    assert service._background == {}


def test_activation_setup_failure_is_durably_interrupted_and_replays_inert(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    original_control = service_module.ExecutionControl
    emitted: list[str] = []
    observed_interruption_state: list[str] = []

    def capture_event(name, data):
        emitted.append(name)
        if name == "execution_interrupted":
            observed_interruption_state.append(
                service.store.get(data["execution_id"]).lifecycle_status
            )

    def fail_control_setup(*_args, **_kwargs):
        raise RuntimeError("simulated control construction failure")

    monkeypatch.setattr(service, "_emit", capture_event)
    monkeypatch.setattr(service_module, "ExecutionControl", fail_control_setup)
    client = _api(monkeypatch, service)
    headers = {"Idempotency-Key": "activation-setup-failure-key"}

    with client:
        failed = client.post(
            "/v1/executions",
            json={"task": "Contain activation failure", "strategy": "direct"},
            headers=headers,
        )
        monkeypatch.setattr(service_module, "ExecutionControl", original_control)
        replayed = client.post(
            "/v1/executions",
            json={"task": "Contain activation failure", "strategy": "direct"},
            headers=headers,
        )

    execution_id = failed.json()["detail"]["execution_id"]
    durable = service.store.get(execution_id)
    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "submission_activation_failed"
    assert replayed.status_code == 202
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert replayed.json()["execution_id"] == execution_id
    assert durable.lifecycle_status == "interrupted"
    assert durable.status == "failed"
    assert durable.interruption_reason == "submission_activation_failed"
    assert durable.retryable is True
    assert durable.interrupted_at and durable.completed_at
    assert [error.code for error in durable.errors] == ["submission_activation_failed"]
    assert observed_interruption_state == ["interrupted"]
    assert "execution_running" not in emitted
    assert service._controls == {}
    assert service._background == {}
    assert service._activating == set()
    assert _counts(database) == (1, 1)


def test_activation_preflight_read_failure_is_durably_contained(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    store = FailActivationPreflightOnceStore(database)
    service = ExecutionService(store=store)
    emitted: list[str] = []
    monkeypatch.setattr(service, "_emit", lambda name, _data: emitted.append(name))
    client = _api(monkeypatch, service)
    headers = {"Idempotency-Key": "activation-preflight-failure-key"}

    with client:
        failed = client.post(
            "/v1/executions",
            json={"task": "Contain a failed activation read", "strategy": "direct"},
            headers=headers,
        )
        replayed = client.post(
            "/v1/executions",
            json={"task": "Contain a failed activation read", "strategy": "direct"},
            headers=headers,
        )

    execution_id = failed.json()["detail"]["execution_id"]
    durable = store.get(execution_id)
    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "submission_activation_failed"
    assert replayed.status_code == 202
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert replayed.json()["execution_id"] == execution_id
    assert durable.lifecycle_status == "interrupted"
    assert durable.interruption_reason == "submission_activation_failed"
    assert emitted == ["execution_interrupted"]
    assert store.get_calls >= 3
    assert service._controls == {}
    assert service._background == {}
    assert service._activating == set()
    assert _counts(database) == (1, 1)


def test_activation_cache_publication_failure_is_durably_contained(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    emitted: list[str] = []
    observed_interruption_state: list[str] = []
    original_remember = service._remember

    def partially_publish_then_fail(request, result):
        original_remember(request, result)
        raise RuntimeError("simulated live-cache publication failure")

    def capture_event(name, data):
        emitted.append(name)
        if name == "execution_interrupted":
            observed_interruption_state.append(
                service.get(data["execution_id"]).lifecycle_status
            )

    monkeypatch.setattr(service, "_remember", partially_publish_then_fail)
    monkeypatch.setattr(service, "_emit", capture_event)
    client = _api(monkeypatch, service)

    with client:
        failed = client.post(
            "/v1/executions",
            json={"task": "Contain a failed cache publication", "strategy": "direct"},
        )

    execution_id = failed.json()["detail"]["execution_id"]
    durable = service.store.get(execution_id)
    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "submission_activation_failed"
    assert durable.lifecycle_status == "interrupted"
    assert durable.interruption_reason == "submission_activation_failed"
    assert emitted == ["execution_interrupted"]
    assert observed_interruption_state == ["interrupted"]
    assert service._controls == {}
    assert service._background == {}
    assert service._activating == set()
    assert _counts(database) == (1, 0)


@pytest.mark.asyncio
async def test_legacy_job_binding_reflects_contained_activation_failure(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "events.db"
    service = ExecutionService(store=ExecutionStore(database))
    request = ExecutionRequestV1(task="Keep legacy mirror truthful", strategy="direct")
    job_id = "job_activation_failure_mirror"
    monkeypatch.setitem(
        routes_pitch.jobs,
        job_id,
        {
            "job_id": job_id,
            "task": request.task,
            "project_id": None,
            "status": "queued",
            "submitted_at": "2026-08-24T00:00:00+00:00",
            "result": None,
            "error": None,
            "trace_id": "trace-activation-failure",
            "execution_request": request.model_dump(mode="json"),
        },
    )

    def fail_control_setup(*_args, **_kwargs):
        raise RuntimeError("simulated legacy activation setup failure")

    durable_jobs: list[dict] = []
    monkeypatch.setattr(routes_pitch, "get_execution_service", lambda: service)
    monkeypatch.setattr(
        routes_pitch,
        "_db_write_job",
        lambda job: durable_jobs.append(dict(job)),
    )
    monkeypatch.setattr(service_module, "ExecutionControl", fail_control_setup)
    monkeypatch.setattr(service, "_emit", lambda *_args, **_kwargs: None)

    with pytest.raises(SubmissionActivationError) as raised:
        await routes_pitch._run_job(
            job_id,
            request.task,
            trace_id="trace-activation-failure",
            canonical=request,
        )

    job = routes_pitch.jobs[job_id]
    durable = service.store.get(raised.value.execution_id)
    assert job["execution_id"] == raised.value.execution_id
    assert job["status"] == "interrupted"
    assert job["error"] == "submission_activation_failed"
    assert job["finished_at"] == durable.completed_at
    assert durable_jobs[-1] == job
    assert durable.lifecycle_status == "interrupted"
    assert service._controls == {}
    assert service._background == {}


@pytest.mark.asyncio
async def test_task_created_boundary_failure_cancels_without_rescheduling(
    tmp_path,
    monkeypatch,
):
    service = ExecutionService(store=ExecutionStore(tmp_path / "events.db"))
    background = RaiseAfterTaskRegistration()
    service._background = background
    emitted: list[str] = []
    execute_calls: list[str] = []
    start_callbacks: list = []
    completion_callbacks: list = []

    async def fake_execute(*_args, **_kwargs):
        execute_calls.append("called")
        raise AssertionError("aborted activation reached execution")

    monkeypatch.setattr(service, "execute", fake_execute)
    monkeypatch.setattr(service, "_emit", lambda name, _data: emitted.append(name))

    with pytest.raises(SubmissionActivationError) as raised:
        service.submit(
            ExecutionRequestV1(task="Fail after one task exists", strategy="direct"),
            on_start=start_callbacks.append,
            on_complete=completion_callbacks.append,
        )

    assert len(background.created_tasks) == 1
    task = background.created_tasks[0]
    await asyncio.gather(task, return_exceptions=True)
    durable = service.store.get(raised.value.execution_id)
    assert task.cancelled()
    assert execute_calls == []
    assert start_callbacks == []
    assert completion_callbacks == []
    assert durable.lifecycle_status == "interrupted"
    assert durable.interruption_reason == "submission_activation_failed"
    assert "execution_running" not in emitted
    assert emitted.count("execution_interrupted") == 1
    assert service._controls == {}
    assert service._background == {}
    assert service._activating == set()


@pytest.mark.asyncio
async def test_running_publication_failure_retains_claim_and_cannot_reactivate(
    tmp_path,
    monkeypatch,
):
    store = RejectInitialRunningStore(tmp_path / "events.db")
    service = ExecutionService(store=store)
    request = ExecutionRequestV1(
        task="Do not reactivate after running publication fails",
        strategy="direct",
    )
    emitted: list[str] = []
    start_callbacks: list = []
    completion_callbacks: list[ServiceExecution] = []
    monkeypatch.setattr(service, "_emit", lambda name, _data: emitted.append(name))

    queued = service.submit(
        request,
        on_start=start_callbacks.append,
        on_complete=completion_callbacks.append,
    )
    task = service._background[queued.execution_id]
    await task
    await asyncio.sleep(0)

    durable = store.get(queued.execution_id)
    with pytest.raises(SubmissionConsistencyError):
        service._activate_committed_submission(request, queued)

    assert store.running_attempts == 3
    assert durable.lifecycle_status == "queued"
    assert start_callbacks == []
    assert completion_callbacks == []
    assert emitted.count("execution_created") == 1
    assert emitted.count("execution_running") == 0
    assert emitted.count("execution_interrupted") == 0
    assert service._controls == {}
    assert service._background == {}
    assert service._activating == {queued.execution_id}


@pytest.mark.asyncio
async def test_crash_state_read_failure_retains_claim_and_cannot_reactivate(
    tmp_path,
    monkeypatch,
):
    store = FailCrashInspectionOnceStore(tmp_path / "events.db")
    service = ExecutionService(store=store)
    request = ExecutionRequestV1(
        task="Do not reactivate after crash inspection fails",
        strategy="direct",
    )
    emitted: list[str] = []
    execute_calls: list[str] = []

    async def crash_before_running(*_args, **_kwargs):
        execute_calls.append("called")
        raise RuntimeError("simulated execution crash before running publication")

    monkeypatch.setattr(service, "execute", crash_before_running)
    monkeypatch.setattr(service, "_emit", lambda name, _data: emitted.append(name))

    queued = service.submit(request)
    task = service._background[queued.execution_id]
    await task
    await asyncio.sleep(0)

    durable = store.get(queued.execution_id)
    with pytest.raises(SubmissionConsistencyError):
        service._activate_committed_submission(request, queued)

    assert durable.lifecycle_status == "queued"
    assert execute_calls == ["called"]
    assert emitted.count("execution_created") == 1
    assert emitted.count("execution_running") == 0
    assert emitted.count("execution_interrupted") == 0
    assert service._controls == {}
    assert service._background == {}
    assert service._activating == {queued.execution_id}


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
    assert row["request_hash_version"] == REQUEST_HASH_VERSION_V1
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
