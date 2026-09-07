"""What the human-readable /status page is allowed to say.

/status is the page a stranger checks. It used to render a table of every
connected machine -- name, model, platform, tasks, credits -- and a recent-work
table carrying task text and a link to each run page. None of that is the
page's job: a task description is somebody's writing about work they wanted
done, and a run is publishable only through a share capability its owner
deliberately created.

These tests grep the rendered response rather than trusting the template,
because the failure being prevented is a value reaching the page, not a
particular markup shape. A future refactor that reintroduces the machine table
under different markup should still fail here.

/node/{id} is the counterpart and stays viewer-gated: it renders hostname, CPU,
GPU, RAM and enrolment id, which is exactly what /nodes is protected for.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes_status
from access_control import ViewerAccessMiddleware, is_public_or_separately_authenticated

RUN_NAME = "20260901_101500"

# Values that must never reach the public page. Distinctive on purpose: a
# substring search for "laptop" would collide with ordinary prose.
SECRET_TASK = "Build a payroll exporter for Acme Holdings"
SECRET_HOST = "jetts-thinkpad-x1.lan"
SECRET_NODE_ID = "node-zzq-7741"


def _write_run(output_dir, *, name: str = RUN_NAME, task: str = SECRET_TASK) -> None:
    run_dir = output_dir / name
    run_dir.mkdir(parents=True)
    (run_dir / "full_log.json").write_text(
        json.dumps({
            "task": task,
            "timestamp": name,
            "rating": "PASS",
            "mode": "distributed",
            "code_files": ["app.py"],
            "code_problems": [],
        }),
        encoding="utf-8",
    )


@pytest.fixture
def status_client(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    monkeypatch.setattr(routes_status, "OUTPUT_DIR", output_dir)

    async def inference_ready():
        return True, "test-model:4b"

    monkeypatch.setattr(routes_status, "_inference", inference_ready)
    monkeypatch.setattr(routes_status, "task_queue", [])
    monkeypatch.setattr(routes_status, "get_standings", lambda: [])
    monkeypatch.setattr(routes_status, "nodes", {
        SECRET_NODE_ID: {
            "node_id": SECRET_NODE_ID,
            "hostname": SECRET_HOST,
            "model": "qwen3.5:4b",
            "platform": "Windows-11",
            "machine": "AMD64",
            "cpu_count": 8,
            "gpu": "none",
            "ram_gb": 8,
            "tasks_completed": 12,
            "credits_earned": 34.5,
            "current_task": SECRET_TASK,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        },
    })

    app = FastAPI()
    app.include_router(routes_status.router)
    return output_dir, TestClient(app)


def test_public_status_names_no_machine_and_no_task_text(status_client):
    """The redaction, asserted by grepping the response."""
    output_dir, client = status_client
    _write_run(output_dir)

    body = client.get("/status").text
    assert client.get("/status").status_code == 200

    for leaked in (SECRET_TASK, SECRET_HOST, SECRET_NODE_ID):
        assert leaked not in body, f"/status leaked {leaked!r}"

    # Hardware and per-machine accounting are equally not the public page's job.
    for leaked in ("qwen3.5:4b", "Windows-11", "AMD64", "34.5", "node-"):
        assert leaked not in body, f"/status leaked {leaked!r}"


def test_public_status_links_to_no_run_page(status_client):
    """A run is reachable through a deliberate share, never through /status."""
    output_dir, client = status_client
    _write_run(output_dir)

    body = client.get("/status").text
    assert "/run/" not in body, "/status linked to a run page"
    assert f"/node/{SECRET_NODE_ID}" not in body
    assert "/node/" not in body, "/status linked to a machine page"


def test_public_status_still_reports_counts_and_says_detail_is_private(status_client):
    """Redaction is not silence. The page still answers the stranger's question."""
    output_dir, client = status_client
    _write_run(output_dir)

    body = client.get("/status").text
    assert "1 machine is connected" in body
    assert "private" in body, "the page does not say the per-machine detail is private"
    # Outcome, assurance, placement and age are what recent work is allowed to be.
    for shown in ("passed", "structure checked", "distributed", "Outcome", "Assurance"):
        assert shown in body, f"/status stopped reporting {shown!r}"


def test_recent_work_reports_a_starved_check_as_unchecked_not_as_passing(status_client):
    """PR #73's distinction, carried onto the public page.

    A precheck that never reached a verdict said nothing about the code. It
    must not be rendered with the same assurance as a check that ran and found
    nothing wrong.
    """
    output_dir, client = status_client
    run_dir = output_dir / RUN_NAME
    run_dir.mkdir(parents=True)
    (run_dir / "full_log.json").write_text(
        json.dumps({
            "task": SECRET_TASK,
            "timestamp": RUN_NAME,
            "rating": "PASS",
            "mode": "local",
            "code_files": ["app.py"],
            "code_problems": [],
            "code_precheck_error": "validator_timeout",
        }),
        encoding="utf-8",
    )

    body = client.get("/status").text
    assert "not checked" in body
    assert "structure checked" not in body
    # The runner's own failure reason is not a fact about the stranger's business.
    assert "validator_timeout" not in body


def test_status_is_not_in_the_public_allowlist(status_client):
    """The redaction shipped; the exposure deliberately did not.

    Redacting /status and publishing it are separate decisions. This one was
    made explicitly: the page stays behind the viewer key. Flipping it is one
    line in `_PUBLIC_EXACT`, and this test is here so that line is a decision
    somebody makes rather than a side effect of a later refactor.
    """
    assert not is_public_or_separately_authenticated("GET", "/status")


@pytest.mark.parametrize("path", ["/node/anything", "/status"])
def test_private_pages_answer_401_without_a_credential(tmp_path, monkeypatch, path):
    """/node/{id} renders hostname, CPU, GPU, RAM and enrolment id."""
    monkeypatch.setattr("access_control._viewer_key", lambda: "a-configured-viewer-key")

    app = FastAPI()
    app.add_middleware(ViewerAccessMiddleware)
    app.include_router(routes_status.router)
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
