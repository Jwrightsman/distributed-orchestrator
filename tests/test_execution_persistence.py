"""Canonical execution metadata survives SQLite reopening and migration."""

import sqlite3

from execution.contracts import ExecutionRequestV1
from execution.persistence import ExecutionStore
from execution.service import ExecutionService


def _queued(service: ExecutionService, request: ExecutionRequestV1, execution_id="a" * 32):
    return service._new_result(request, execution_id, "job_test", "queued")


def test_execution_metadata_survives_reopening_database(tmp_path):
    path = tmp_path / "events.db"
    request = ExecutionRequestV1(task="Build something", strategy="direct", placement="local")
    first = ExecutionStore(path)
    service = ExecutionService(store=first)
    result = _queued(service, request)
    first.save(request, result)

    reopened = ExecutionStore(path)
    loaded = reopened.get(result.execution_id)
    assert loaded is not None
    assert loaded.strategy_requested == "direct"
    assert loaded.strategy_selected == "ensemble"
    assert loaded.strategy_options["candidates"] == 1
    assert loaded.selector_version == "conservative-v1"


def test_completed_normalized_result_remains_queryable(tmp_path):
    path = tmp_path / "events.db"
    request = ExecutionRequestV1(task="Build something")
    service = ExecutionService(store=ExecutionStore(path))
    result = _queued(service, request)
    result.status = "completed"
    result.output_reference = "output/run/output.md"
    service.store.save(request, result)

    loaded = ExecutionStore(path).get(result.execution_id)
    assert loaded.status == "completed"
    assert loaded.output_reference.endswith("output.md")


def test_migration_preserves_legacy_jobs(tmp_path):
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task TEXT)")
        con.execute("INSERT INTO jobs VALUES ('job_old', 'legacy work')")
        con.commit()

    ExecutionStore(path).migrate()

    with sqlite3.connect(path) as con:
        assert con.execute("SELECT task FROM jobs WHERE job_id='job_old'").fetchone()[0] == "legacy work"
        assert con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='executions'"
        ).fetchone()


def test_migration_is_idempotent(tmp_path):
    store = ExecutionStore(tmp_path / "events.db")
    for _ in range(5):
        store.migrate()
    with sqlite3.connect(store.path) as con:
        count = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='executions'"
        ).fetchone()[0]
    assert count == 1
