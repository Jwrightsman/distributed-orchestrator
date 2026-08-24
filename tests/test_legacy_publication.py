"""Durable publication boundaries for legacy run and project surfaces."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rich.console import Console

import cli
import execution.service as service_module
import orchestrator
import routes_history
import routes_run
import routes_status
import routes_try
import scripts.capture_demo_asset as capture_demo_asset
from execution.artifacts import ArtifactStore
from execution.contracts import ExecutionRequestV1
from execution.persistence import ExecutionStore
from execution.publication import (
    LegacyRunNotPublished,
    require_legacy_run_publication,
)
from execution.registry import StrategyOutcome, StrategyRegistry
from execution.service import ExecutionService, TerminalPersistenceError
from memory import create_project, get_memory_context, load_project

EXECUTION_ID = "p" * 32
RUN_NAME = "20260823_120000"


def _service(tmp_path: Path, output_dir: Path) -> ExecutionService:
    database = tmp_path / "events.db"
    service = ExecutionService(
        store=ExecutionStore(database),
        artifacts=ArtifactStore(database, allowed_roots=[output_dir]),
    )
    service.store.migrate()
    service.artifacts.migrate()
    service._emit = lambda *_args, **_kwargs: None
    return service


def _write_run(
    output_dir: Path,
    *,
    execution_id: str | None = EXECUTION_ID,
    marked: bool = True,
    name: str = RUN_NAME,
) -> tuple[Path, dict]:
    run_dir = output_dir / name
    run_dir.mkdir(parents=True)
    code_dir = run_dir / "code"
    code_dir.mkdir()
    (code_dir / "app.py").write_text("print('committed')\n", encoding="utf-8")
    (run_dir / "review.md").write_text(
        "## Quality Rating\nPASS\n",
        encoding="utf-8",
    )
    (run_dir / "output.md").write_text("durable output", encoding="utf-8")
    log = {
        "task": "Durable publication task",
        "timestamp": name,
        "plan": [{"id": 1, "title": "Build", "depends_on": []}],
        "results": {"1": "candidate"},
        "review": "## Quality Rating\nPASS\n",
        "rating": "PASS",
        "code_files": [str(code_dir / "app.py")],
        "code_problems": [],
        "mode": "local",
        "project_id": "",
        "execution_id": execution_id,
    }
    if marked:
        log["publication_boundary"] = "canonical_terminal_v1"
    (run_dir / "full_log.json").write_text(
        json.dumps(log, indent=2),
        encoding="utf-8",
    )
    return run_dir, log


def _running_row(service: ExecutionService, execution_id: str = EXECUTION_ID):
    request = ExecutionRequestV1(task="Durable publication task", strategy="dag")
    result = service._new_result(request, execution_id, None, "running")
    result.lifecycle_status = "running"
    result.started_at = datetime.now(timezone.utc).isoformat()
    service.store.create(request, result)
    return request, result


def _commit_terminal(
    service: ExecutionService,
    request: ExecutionRequestV1,
    running,
    manifest,
):
    terminal = running.model_copy(deep=True)
    terminal.lifecycle_status = "completed"
    terminal.status = "completed"
    terminal.validation_outcome = "passed"
    terminal.assurance_level = "structural"
    terminal.completed_at = datetime.now(timezone.utc).isoformat()
    terminal.sealed_manifest_hash = manifest.manifest_hash
    terminal.artifact_integrity_mode = "sealed"
    service.store.save(request, terminal)
    return terminal


@pytest.fixture
def publication_runtime(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    service = _service(tmp_path, output_dir)
    monkeypatch.setattr(service_module, "_SERVICE", service)
    for module in (
        routes_history,
        routes_run,
        routes_status,
        routes_try,
        cli,
    ):
        monkeypatch.setattr(module, "OUTPUT_DIR", output_dir)

    async def inference_ready():
        return True, "test-model"

    monkeypatch.setattr(routes_status, "_inference", inference_ready)
    monkeypatch.setattr(routes_status, "nodes", {})
    monkeypatch.setattr(routes_status, "task_queue", [])
    monkeypatch.setattr(routes_status, "get_standings", lambda: [])
    monkeypatch.setattr(routes_try, "get_config", lambda: {"public_pitch": False})

    app = FastAPI()
    app.include_router(routes_history.router)
    app.include_router(routes_run.router)
    app.include_router(routes_status.router)
    app.include_router(routes_try.router)
    return output_dir, service, TestClient(app)


def _cli_history(monkeypatch) -> str:
    stream = io.StringIO()
    monkeypatch.setattr(
        cli,
        "console",
        Console(file=stream, force_terminal=False, color_system=None, width=120),
    )
    cli.show_history()
    return stream.getvalue()


def test_current_run_is_hidden_until_durable_terminal_commit(
    publication_runtime,
    monkeypatch,
):
    output_dir, service, client = publication_runtime
    run_dir, _ = _write_run(output_dir)
    request, running = _running_row(service)
    service.artifacts.register_root(EXECUTION_ID, run_dir, active=True)
    manifest = service.artifacts.seal_manifest(EXECUTION_ID)

    assert client.get("/history").json()["count"] == 0
    assert client.get("/gallery").json()["count"] == 0
    assert client.get(f"/history/{RUN_NAME}").status_code == 404
    assert client.get(f"/history/{RUN_NAME}/download").status_code == 404
    assert client.get(f"/run/{RUN_NAME}").status_code == 404
    assert "Durable publication task" not in client.get("/status").text
    assert f"/run/{RUN_NAME}" not in client.get("/try").text
    assert "Durable publication task" not in _cli_history(monkeypatch)

    _commit_terminal(service, request, running, manifest)

    assert client.get("/history").json()["count"] == 1
    assert client.get("/gallery").json()["count"] == 1
    assert client.get(f"/history/{RUN_NAME}").status_code == 200
    archive = client.get(f"/history/{RUN_NAME}/download")
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        assert {"full_log.json", "output.md", "code/app.py"}.issubset(
            zipped.namelist()
        )
    assert client.get(f"/run/{RUN_NAME}").status_code == 200
    assert "Durable publication task" in client.get("/status").text
    assert f"/run/{RUN_NAME}" in client.get("/try").text
    assert "Durable publication task" in _cli_history(monkeypatch)


def test_guessed_download_without_log_is_not_published(publication_runtime):
    output_dir, _, client = publication_runtime
    run_dir = output_dir / "20260823_120001"
    run_dir.mkdir()
    (run_dir / "output.md").write_text("staged secret", encoding="utf-8")

    assert client.get("/history/20260823_120001/download").status_code == 404


def test_sealed_root_authority_survives_mutable_log_tampering(
    publication_runtime,
):
    output_dir, service, _ = publication_runtime
    run_dir, log = _write_run(output_dir)
    original = (run_dir / "full_log.json").read_text(encoding="utf-8")
    request, running = _running_row(service)
    service.artifacts.register_root(EXECUTION_ID, run_dir, active=True)
    manifest = service.artifacts.seal_manifest(EXECUTION_ID)
    _commit_terminal(service, request, running, manifest)

    for key in ("publication_boundary", "execution_id"):
        tampered = dict(log)
        tampered.pop(key)
        (run_dir / "full_log.json").write_text(
            json.dumps(tampered, indent=2),
            encoding="utf-8",
        )
        with pytest.raises(LegacyRunNotPublished):
            require_legacy_run_publication(run_dir, tampered)
        (run_dir / "full_log.json").write_text(original, encoding="utf-8")


def test_unmarked_legacy_rows_keep_terminal_only_compatibility(
    publication_runtime,
):
    output_dir, service, client = publication_runtime
    _, _ = _write_run(output_dir, marked=False)
    request, running = _running_row(service)

    assert client.get(f"/history/{RUN_NAME}").status_code == 404

    terminal = running.model_copy(deep=True)
    terminal.lifecycle_status = "completed"
    terminal.status = "completed"
    terminal.completed_at = datetime.now(timezone.utc).isoformat()
    service.store.save(request, terminal)

    assert client.get(f"/history/{RUN_NAME}").status_code == 200
    assert client.get(f"/history/{RUN_NAME}/download").status_code == 200


def test_registered_unmarked_legacy_live_terminal_remains_visible(
    publication_runtime,
):
    output_dir, service, client = publication_runtime
    run_dir, _ = _write_run(output_dir, marked=False)
    request, running = _running_row(service)
    service.artifacts.register_root(EXECUTION_ID, run_dir, active=False)

    terminal = running.model_copy(deep=True)
    terminal.lifecycle_status = "completed"
    terminal.status = "completed"
    terminal.completed_at = datetime.now(timezone.utc).isoformat()
    service.store.save(request, terminal)

    assert client.get(f"/history/{RUN_NAME}").status_code == 200
    assert client.get(f"/history/{RUN_NAME}/download").status_code == 200


def test_unmarked_restart_reconciliation_does_not_publish_old_staged_files(
    publication_runtime,
):
    output_dir, service, client = publication_runtime
    _write_run(output_dir, marked=False)
    _running_row(service)

    assert service.store.reconcile_nonterminal("restart-publication-test") == [
        EXECUTION_ID
    ]
    assert client.get(f"/history/{RUN_NAME}").status_code == 404
    assert client.get(f"/history/{RUN_NAME}/download").status_code == 404


def test_marked_unregistered_run_without_execution_identity_is_not_historical(
    publication_runtime,
):
    output_dir, _, client = publication_runtime
    _write_run(output_dir, execution_id=None, marked=True)

    assert client.get(f"/history/{RUN_NAME}").status_code == 404


def test_demo_capture_rejects_staged_run_before_destination_mutation(
    publication_runtime,
    tmp_path,
    monkeypatch,
):
    output_dir, service, _ = publication_runtime
    run_dir, _ = _write_run(output_dir)
    _running_row(service)
    service.artifacts.register_root(EXECUTION_ID, run_dir, active=True)
    service.artifacts.seal_manifest(EXECUTION_ID)

    assets_dir = tmp_path / "docs" / "demo-assets"
    existing = assets_dir / "demo"
    existing.mkdir(parents=True)
    sentinel = existing / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(capture_demo_asset, "ASSETS_DIR", assets_dir)
    monkeypatch.setattr(capture_demo_asset, "REPO_ROOT", tmp_path)

    assert capture_demo_asset.capture(run_dir, "demo", False, "") == 1
    assert sentinel.read_text(encoding="utf-8") == "keep"

    historical, _ = _write_run(
        output_dir,
        execution_id=None,
        marked=False,
        name="20260823_120002",
    )
    assert capture_demo_asset.capture(historical, "historical", False, "") == 0
    assert (assets_dir / "historical" / "code" / "app.py").is_file()


class ProjectDagStrategy:
    identifier = "dag"
    version = "project-publication-test"

    def __init__(self, root: Path):
        self.root = root

    async def execute(self, request, options, context):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "full_log.json").write_text("{}", encoding="utf-8")
        return StrategyOutcome(
            status="completed",
            validation_outcome="passed",
            assurance_level="structural",
            legacy_payload={
                "project_dir": str(self.root),
                "plan": [{"id": 1, "title": "Build"}],
                "rating": "PASS",
                "final_output": "project result",
            },
        )


def _project_service(tmp_path: Path) -> ExecutionService:
    registry = StrategyRegistry()
    registry.register(ProjectDagStrategy(tmp_path / "run"))
    database = tmp_path / "project-events.db"
    return ExecutionService(
        store=ExecutionStore(database),
        registry=registry,
        artifacts=ArtifactStore(database, allowed_roots=[tmp_path]),
    )


@pytest.mark.asyncio
async def test_project_memory_publishes_after_event_and_before_on_complete(
    tmp_path,
    monkeypatch,
):
    project_id = create_project("Durable project", "Initial goal")
    service = _project_service(tmp_path)
    order = []

    def emit(event_type, _data):
        if event_type == "execution_completed":
            assert load_project(project_id)["iteration_count"] == 0
            order.append("event")

    service._emit = emit
    original_commit = orchestrator.commit_project_iteration

    async def observed_commit(*args):
        assert order == ["event"]
        await original_commit(*args)
        order.append("memory")

    monkeypatch.setattr(orchestrator, "commit_project_iteration", observed_commit)

    def completed(_run):
        assert load_project(project_id)["iteration_count"] == 1
        order.append("on_complete")

    queued = service.submit(
        ExecutionRequestV1(
            task="Private project task",
            project_id=project_id,
            strategy="dag",
        ),
        on_complete=completed,
    )
    task = service._background[queued.execution_id]
    await asyncio.shield(task)
    await asyncio.sleep(0)

    assert order == ["event", "memory", "on_complete"]
    assert "Private project task" in get_memory_context(project_id)


@pytest.mark.asyncio
async def test_permanent_terminal_failure_leaves_project_memory_unchanged(
    tmp_path,
    monkeypatch,
):
    project_id = create_project("Failed project", "Initial goal")
    service = _project_service(tmp_path)
    terminal_attempts = 0
    original_save = service.store.save

    def fail_terminal(request, result):
        nonlocal terminal_attempts
        if result.lifecycle_status in {"completed", "failed"}:
            terminal_attempts += 1
            raise sqlite3.OperationalError("terminal write unavailable")
        return original_save(request, result)

    monkeypatch.setattr(service.store, "save", fail_terminal)

    with pytest.raises(TerminalPersistenceError):
        await service.execute(
            ExecutionRequestV1(
                task="Never publish this iteration",
                project_id=project_id,
                strategy="dag",
            )
        )

    assert terminal_attempts == 3
    assert load_project(project_id)["iteration_count"] == 0
    assert get_memory_context(project_id) == ""
    assert list((Path("projects") / project_id / "iterations").iterdir()) == []


@pytest.mark.asyncio
async def test_project_memory_hook_failure_cannot_unpublish_terminal_result(
    tmp_path,
    monkeypatch,
):
    service = _project_service(tmp_path)
    events = []
    service._emit = lambda event_type, _data: events.append(event_type)

    async def broken_memory_hook(*_args):
        raise RuntimeError("memory mirror unavailable")

    monkeypatch.setattr(orchestrator, "commit_project_iteration", broken_memory_hook)
    run = await service.execute(
        ExecutionRequestV1(
            task="Still complete",
            project_id="missing-project-is-best-effort",
            strategy="dag",
        )
    )

    assert run.result.lifecycle_status == "completed"
    assert service.store.get(run.result.execution_id).lifecycle_status == "completed"
    assert "execution_completed" in events
