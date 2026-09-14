"""Project IDs and all project-memory paths share the same storage boundary."""

import os
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from execution.contracts import ExecutionRequestV1
import memory
from project_ids import validate_project_id
from server_state import PitchRequest


BAD_IDS = [
    "", " ", ".", "..", "../outside", "..\\outside", "a/b", "a\\b", "/outside",
    "C:\\outside", "C:outside", "\\\\server\\share", "\\\\?\\C:\\outside", "name:stream",
    "name.", "name ", " name", "nul", "CON.txt", "aux", "LPT1", "COM9.log", "COM¹",
    "name\x00x", "name\nx", "a?b", "a*b", "a%2fb", "x" * 129,
]


@pytest.mark.parametrize("project_id", BAD_IDS)
def test_all_entry_contracts_reject_nonportable_ids(project_id):
    from cli import build_execution_request

    for construct in (
        lambda: validate_project_id(project_id),
        lambda: ExecutionRequestV1(task="authored", strategy="dag", project_id=project_id),
        lambda: PitchRequest(task="authored", project_id=project_id),
        lambda: build_execution_request("authored", project_id=project_id, strategy="dag"),
        lambda: memory.load_project(project_id),
        lambda: memory.get_memory_context(project_id),
        lambda: memory.add_iteration(project_id, {}, "authored"),
    ):
        with pytest.raises(ValueError):
            construct()
    assert not Path("projects").exists()


@pytest.mark.parametrize("project_id", ["todo-app", "A_1-v2.0", "café-数据", "x" * 128])
def test_valid_ids_are_preserved_verbatim(project_id):
    assert validate_project_id(project_id) == project_id
    assert ExecutionRequestV1(task="authored", project_id=project_id).project_id == project_id
    assert PitchRequest(task="authored", project_id=project_id).project_id == project_id


def _directory_link(link, target):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target.resolve()), str(link.absolute()))
    else:
        link.symlink_to(target.resolve(), target_is_directory=True)


def _file_link(link, target, kind):
    if kind == "hardlink":
        os.link(target, link)
    else:
        try:
            link.symlink_to(target.resolve())
        except OSError as exc:
            pytest.skip(f"file symlink creation unavailable: {exc.winerror if os.name == 'nt' else exc.errno}")


def test_traversal_cannot_read_or_modify_outside(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "meta.json").write_text('{"iteration_count": 0}', encoding="utf-8")
    (outside / "memory.md").write_text("authored private sentinel", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in outside.iterdir()}
    for operation in (memory.load_project, memory.get_memory_context):
        with pytest.raises(ValueError):
            operation("../outside")
    with pytest.raises(ValueError):
        memory.add_iteration("../outside", {}, "authored")
    assert {p.name: p.read_bytes() for p in outside.iterdir()} == before


@pytest.mark.parametrize("location", ["root", "project", "iterations", "iteration"])
def test_directory_links_and_windows_junctions_are_rejected(tmp_path, location):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "memory.md").write_text("outside sentinel", encoding="utf-8")
    if location == "root":
        _directory_link(Path("projects"), outside)
        with pytest.raises(ValueError):
            memory.list_projects()
        with pytest.raises(ValueError):
            memory.create_project("authored", "task")
    else:
        pid = memory.create_project("authored", "task")
        if location == "project":
            link = Path("projects") / "linked-project"
            _directory_link(link, outside)
            for read in (memory.load_project, memory.get_memory_context):
                with pytest.raises(ValueError):
                    read("linked-project")
            assert [p["project_id"] for p in memory.list_projects()] == [pid]
        else:
            link = Path("projects") / pid / "iterations"
            if location == "iterations":
                link.rmdir()
            else:
                link /= "1"
            _directory_link(link, outside)
            with pytest.raises(ValueError):
                memory.add_iteration(pid, {}, "authored")
            assert memory.load_project(pid)["iteration_count"] == 0
    assert (outside / "memory.md").read_text(encoding="utf-8") == "outside sentinel"
    assert list(outside.iterdir()) == [outside / "memory.md"]


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
@pytest.mark.parametrize("child", ["meta.json", "memory.md", "iterations/1/output.txt"])
def test_child_file_links_are_rejected_before_external_read_or_write(tmp_path, kind, child):
    pid = memory.create_project("authored", "task")
    link = Path("projects") / pid / child
    link.parent.mkdir(parents=True, exist_ok=True)
    content = link.read_bytes() if link.exists() else b"outside sentinel"
    if link.exists():
        link.unlink()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(content)
    _file_link(link, outside, kind)
    source = tmp_path / "authored-output"
    source.mkdir()
    (source / "output.txt").write_text("new authored output", encoding="utf-8")
    if child == "meta.json":
        with pytest.raises(ValueError):
            memory.load_project(pid)
        assert memory.list_projects() == []
    if child == "memory.md":
        with pytest.raises(ValueError):
            memory.get_memory_context(pid)
    with pytest.raises(ValueError):
        memory.add_iteration(pid, {"project_dir": str(source)}, "authored")
    assert outside.read_bytes() == content


def test_iteration_source_links_are_rejected(tmp_path):
    pid = memory.create_project("authored", "task")
    source = tmp_path / "output"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private sentinel", encoding="utf-8")
    _file_link(source / "leaked.txt", outside, "hardlink")
    with pytest.raises(ValueError):
        memory.add_iteration(pid, {"project_dir": str(source)}, "authored")
    assert not (Path("projects") / pid / "iterations" / "1" / "leaked.txt").exists()


def test_normal_project_files_and_iterations_still_work(tmp_path):
    pid = memory.create_project("CON", "task")
    validate_project_id(pid)
    source = tmp_path / "output"
    source.mkdir()
    (source / "output.txt").write_text("authored output", encoding="utf-8")
    for iteration in (1, 2):
        assert memory.add_iteration(pid, {"project_dir": str(source)}, "authored") == iteration
        assert memory.project_path(pid, "iterations", str(iteration), "output.txt").read_text() == "authored output"


def test_project_http_rejects_linked_iteration(tmp_path):
    from routes_projects import router

    pid = memory.create_project("authored", "task")
    outside = tmp_path / "outside"
    outside.mkdir()
    _directory_link(Path("projects") / pid / "iterations" / "1", outside)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get(f"/projects/{pid}").status_code == 422
        assert client.get("/projects/C:outside").status_code == 422


@pytest.mark.asyncio
async def test_mcp_rejects_project_path_before_network(monkeypatch):
    import mcp_server

    client = AsyncMock(side_effect=AssertionError("invalid ID must not reach HTTP"))
    monkeypatch.setattr(mcp_server, "_client", client)
    assert "Invalid project_id" in await mcp_server.continue_project("../outside", "task")
    assert "Invalid project_id" in await mcp_server.pitch_task("task", project_id="../outside")
    client.assert_not_called()
