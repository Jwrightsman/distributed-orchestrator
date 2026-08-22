"""Tests for the MCP server tools.

The tools are exercised against the real FastAPI app in-process (httpx
ASGITransport) with the pipeline stubbed — verifies the whole MCP → HTTP →
job-store → history path without Ollama.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import mcp_server
import routes_pitch
import server
import server_state

RUN_TS = "20260801_120000"


@pytest.fixture(autouse=True)
def clean_state():
    for d in (
        server.nodes,
        server.task_results,
        server.task_inflight,
        server.jobs,
        server._pitch_timestamps,
    ):
        d.clear()
    server.task_queue.clear()
    # ASGITransport doesn't run the lifespan — create the SQLite tables ourselves
    server_state._init_db()
    yield


@pytest.fixture(autouse=True)
def asgi_client(monkeypatch):
    """Point the MCP server's HTTP client at the in-process app."""
    def make_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        )
    monkeypatch.setattr(mcp_server, "_client", make_client)


@pytest.fixture(autouse=True)
def fake_pipeline(monkeypatch):
    async def _fake(task, **kwargs):
        run_dir = Path("output") / RUN_TS
        run_dir.mkdir(parents=True, exist_ok=True)
        log = {
            "task": task,
            "timestamp": RUN_TS,
            "plan": [{"id": 1, "title": "Build it", "prompt": "p", "depends_on": []}],
            "results": {"1": "out"},
            "review": "## Quality Rating\nPASS",
            "rating": "PASS",
            "mode": "local",
            "project_id": kwargs.get("project_id") or "",
        }
        (run_dir / "full_log.json").write_text(json.dumps(log))
        (run_dir / "review.md").write_text("## Quality Rating\nPASS\n")
        (run_dir / "output.md").write_text("# The Deliverable\nComplete swarm output.")
        return {
            "project_dir": str(run_dir),
            "plan": log["plan"],
            "results": {1: "out"},
            "review": log["review"],
            "final_output": "# The Deliverable\nComplete swarm output.",
            "rating": "PASS",
            "code_files": [],
            "project_id": kwargs.get("project_id") or "",
        }

    monkeypatch.setattr(routes_pitch, "run_pipeline", _fake)


async def _drain_jobs():
    """Let the _run_job background task scheduled by /pitch/async finish."""
    for _ in range(20):
        await asyncio.sleep(0.01)
        if server.jobs and all(j["status"] in ("complete", "failed") for j in server.jobs.values()):
            return


def _job_id_from(text: str) -> str:
    import re

    # Job ids are UUID-based, not timestamps. A \d+ pattern silently captured
    # "job_5" from "job_5a3f..." and then looked up a job that never existed.
    m = re.search(r"job_[0-9a-f]{8,}", text)
    assert m, f"no job_id in: {text}"
    return m.group(0)


@pytest.mark.asyncio
async def test_pitch_task_returns_job_id():
    out = await mcp_server.pitch_task("Build a widget")
    assert "job_id" in out
    assert "get_job_status" in out  # tells the agent how to follow up


@pytest.mark.asyncio
async def test_full_flow_pitch_status_result():
    pitched = await mcp_server.pitch_task("Build a widget")
    job_id = _job_id_from(pitched)
    await _drain_jobs()

    status = await mcp_server.get_job_status(job_id)
    assert "complete" in status
    assert "Build it" in status  # subtask title surfaces

    result = await mcp_server.get_result(job_id)
    assert "Rating: PASS" in result
    assert "Complete swarm output." in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("canonical_rating", "expected_rating"),
    [("FAIL", "FAIL"), (None, "PASS")],
)
async def test_get_result_prefers_canonical_rating_without_leaking_project_dir(
    monkeypatch, canonical_rating, expected_rating
):
    execution_id = "e" * 32
    private_path = r"C:\private\output\run-123"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/jobs/job_rating":
            return httpx.Response(
                200,
                json={
                    "job_id": "job_rating",
                    "execution_id": execution_id,
                    "status": "complete",
                    "rating": "PASS",
                    "project_dir": private_path,
                },
            )
        if path == f"/v1/executions/{execution_id}":
            review_metadata = {"rating": canonical_rating} if canonical_rating else {}
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "lifecycle_status": "completed",
                    "assurance_level": "model_judged",
                    "strategy_selected": "dag",
                    "winning_candidate": None,
                    "review_metadata": review_metadata,
                    "output_preview": "bounded canonical preview",
                },
            )
        if path == f"/v1/executions/{execution_id}/artifacts":
            return httpx.Response(200, json={"entries": []})
        return httpx.Response(404)

    def make_client():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://test",
        )

    monkeypatch.setattr(mcp_server, "_client", make_client)

    result = await mcp_server.get_result("job_rating")

    assert f"Rating: {expected_rating}" in result
    assert "bounded canonical preview" in result
    assert private_path not in result


@pytest.mark.asyncio
async def test_get_status_unknown_job():
    out = await mcp_server.get_job_status("job_nope")
    assert "No job" in out


@pytest.mark.asyncio
async def test_get_result_while_running(monkeypatch):
    # Freeze the job in 'queued' by stubbing the runner to do nothing
    async def _never_runs(job_id, task, project_id=None, trace_id=""):
        pass
    monkeypatch.setattr(routes_pitch, "_run_job", _never_runs)

    pitched = await mcp_server.pitch_task("Build a widget")
    job_id = _job_id_from(pitched)
    out = await mcp_server.get_result(job_id)
    assert "still" in out


@pytest.mark.asyncio
async def test_list_projects_empty():
    out = await mcp_server.list_projects()
    assert "No projects yet" in out


@pytest.mark.asyncio
async def test_continue_project_missing():
    out = await mcp_server.continue_project("ghost-project", "add things")
    assert "No project" in out


@pytest.mark.asyncio
async def test_continue_project_existing():
    from memory import create_project

    pid = create_project("Widget App", "build a widget")
    listed = await mcp_server.list_projects()
    assert pid in listed

    out = await mcp_server.continue_project(pid, "add a second widget")
    assert "job_id" in out


@pytest.mark.asyncio
async def test_pitch_requires_key_message(monkeypatch):
    import config

    cfg = config.DEFAULTS.copy()
    cfg["pitch_key"] = "secret"
    monkeypatch.setattr(server_state, "get_config", lambda: cfg)
    out = await mcp_server.pitch_task("Build a widget")
    assert "PITCH_KEY" in out  # explains exactly what to set


def test_five_tools_registered():
    tools = asyncio.run(mcp_server.server.list_tools())
    assert {t.name for t in tools} == {
        "pitch_task",
        "get_job_status",
        "get_result",
        "list_projects",
        "continue_project",
    }
