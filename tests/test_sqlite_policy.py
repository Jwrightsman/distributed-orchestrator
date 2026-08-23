"""All durable stores share one bounded, foreign-key-safe SQLite policy."""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import server_state as state
from execution.artifacts import ArtifactStore
from execution.attempts import AttemptStore
from execution.contracts import ExecutionRequestV1
from execution.persistence import ExecutionStore
from execution.service import ExecutionService
from execution.sharing import CreateExecutionShareV1, ShareStore
from ledger import insert_contribution_in_transaction
from sqlite_store import connection, foreign_keys_enabled, retry_busy, transaction


def _task(index: int) -> dict:
    return {
        "task_id": f"task-{index}",
        "execution_id": f"execution-{index:02d}",
        "execution_unit_id": f"unit-{index}",
        "execution_unit_kind": "direct_candidate",
        "contract_version": "1",
        "max_output_bytes": 4096,
    }


def test_connection_policy_enables_wal_foreign_keys_and_bounded_retry(tmp_path, monkeypatch):
    database = tmp_path / "events.db"
    with connection(database) as con:
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert con.execute("PRAGMA synchronous").fetchone()[0] in (1, 2)
    assert foreign_keys_enabled(database)

    calls = 0
    sleeps: list[float] = []

    def always_busy():
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(time, "sleep", sleeps.append)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        retry_busy(always_busy, attempts=3)
    assert calls == 3
    assert len(sleeps) == 2


def test_concurrent_store_initialization_is_idempotent(tmp_path):
    database = tmp_path / "events.db"
    storage = tmp_path / "storage"
    storage.mkdir()

    def initialize(index: int):
        if index % 4 == 0:
            AttemptStore(database).migrate()
        elif index % 4 == 1:
            ExecutionStore(database).migrate()
        elif index % 4 == 2:
            ArtifactStore(database, allowed_roots=[storage]).migrate()
        else:
            ShareStore(str(database)).migrate()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(initialize, range(32)))

    with connection(database) as con:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "attempts",
        "accepted_result_receipts",
        "executions",
        "artifact_roots",
        "artifact_entries",
        "execution_shares",
        "contributions",
    } <= tables


def test_concurrent_cross_store_writes_are_complete_and_atomic(tmp_path, monkeypatch):
    database = tmp_path / "events.db"
    storage = tmp_path / "storage"
    storage.mkdir()
    attempts = AttemptStore(database)
    executions = ExecutionStore(database)
    artifacts = ArtifactStore(database, allowed_roots=[storage])
    shares = ShareStore(str(database))
    attempts.migrate()
    executions.migrate()
    artifacts.migrate()
    shares.migrate()
    monkeypatch.setattr(state, "_DB_PATH", database)
    monkeypatch.setattr(state, "attempt_store", attempts)
    state._init_db()

    request = ExecutionRequestV1(task="concurrent alpha work", strategy="direct")
    service = ExecutionService(store=executions, artifacts=artifacts)
    count = 8

    def settle_attempt(index: int):
        task = _task(index)
        attempt_id = f"attempt-{index}"
        nonce = f"nonce-{index}-unguessable"
        attempts.issue(
            task,
            assigned_node_id=f"node-{index}",
            attempt_id=attempt_id,
            nonce=nonce,
            issued_at=100.0,
            lease_expires_at=1000.0,
        )
        attempts.settle(
            task_id=task["task_id"],
            node_id=f"node-{index}",
            output=f"result-{index}",
            error=None,
            elapsed_seconds=1.0,
            contract_version="1",
            attempt_id=attempt_id,
            nonce=nonce,
            execution_id=task["execution_id"],
            execution_unit_id=task["execution_unit_id"],
            execution_unit_kind=task["execution_unit_kind"],
            now=200.0,
        )

    def save_execution(index: int):
        result = service._new_result(
            request,
            f"{index:032x}",
            f"job-{index}",
            "queued",
        )
        executions.save(request, result)

    def finalize_artifact(index: int):
        root = storage / f"artifact-{index}"
        root.mkdir()
        (root / "result.txt").write_text(f"artifact-{index}", encoding="utf-8")
        execution_id = f"artifact-{index}"
        artifacts.register_root(execution_id, root, active=True)
        artifacts.seal_manifest(execution_id)

    def create_and_revoke_share(index: int):
        execution_id = f"share-execution-{index}"
        created = shares.create(execution_id, CreateExecutionShareV1())
        assert shares.revoke(execution_id, created.share_id)

    def write_event(index: int):
        state._db_write_event("test", str(index), {"index": index})

    def write_job(index: int):
        state._db_write_job(
            {
                "job_id": f"job-{index}",
                "task": f"task-{index}",
                "project_id": None,
                "status": "complete",
                "submitted_at": str(index),
                "started_at": str(index),
                "finished_at": str(index),
                "error": None,
                "result": None,
                "trace_id": f"trace-{index}",
            }
        )

    def write_contribution(index: int):
        with transaction(database) as con:
            assert insert_contribution_in_transaction(
                con,
                contribution_id=f"manual-{index}",
                contributor=f"operator-{index}",
                contribution_type="pitch",
                points=1,
                basis="pitch_submission",
            )

    operations = []
    for index in range(count):
        operations.extend(
            (
                (settle_attempt, index),
                (save_execution, index),
                (finalize_artifact, index),
                (create_and_revoke_share, index),
                (write_event, index),
                (write_job, index),
                (write_contribution, index),
            )
        )
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(operation, index) for operation, index in operations]
        for future in futures:
            future.result(timeout=30)

    with connection(database) as con:
        assert con.execute("SELECT COUNT(*) FROM attempts WHERE state='settled'").fetchone()[0] == count
        assert con.execute("SELECT COUNT(*) FROM accepted_result_receipts").fetchone()[0] == count
        assert con.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == count
        assert con.execute("SELECT COUNT(*) FROM artifact_roots WHERE manifest_state='sealed'").fetchone()[0] == count
        assert con.execute("SELECT COUNT(*) FROM artifact_entries").fetchone()[0] == count
        assert con.execute("SELECT COUNT(*) FROM execution_shares WHERE revoked_at IS NOT NULL").fetchone()[0] == count
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == count
        assert con.execute("SELECT COUNT(*) FROM jobs WHERE status='complete'").fetchone()[0] == count
        # Accepted attempts and explicit pitch rows share the same durable ledger.
        assert con.execute("SELECT COUNT(*) FROM contributions").fetchone()[0] == count * 2
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                """
                INSERT INTO artifact_entries
                    (execution_id, relative_path, media_type, size_bytes, sha256,
                     role, created_at)
                VALUES ('missing-root', 'x', 'text/plain', 1, ?, 'deliverable', 'now')
                """,
                ("0" * 64,),
            )
