"""API-level tests for the orchestrator server.

Run against the in-process FastAPI app with TestClient — no Ollama, no network.
These lock in endpoint behavior so the server refactor (sprint 1.3) and the
security hardening (sprint 1.4) are provable, not assumed.
"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import routes_pitch
import server
import server_state


@pytest.fixture(autouse=True)
def clean_server_state():
    """Server keeps orchestration state in module-level dicts — reset per test."""
    for d in (
        server.nodes,
        server.task_results,
        server.task_inflight,
        server.node_failure_count,
        server.node_blacklist,
        server.jobs,
        server._pitch_timestamps,
    ):
        d.clear()
    server.task_queue.clear()
    server.pipeline_events.clear()
    yield


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield c


def _register(client, node_id="test-node", secret=None, **overrides):
    payload = {
        "node_id": node_id,
        "model": "qwen3.5:4b",
        "platform": "TestOS",
        "machine": "x86_64",
        "hostname": "test-host",
        **overrides,
    }
    headers = {"X-Node-Secret": secret} if secret else {}
    return client.post("/nodes/register", json=payload, headers=headers)


FAKE_RESULT = {
    "project_dir": "output/fake",
    "plan": [{"id": 1, "title": "t", "prompt": "p", "depends_on": []}],
    "results": {1: "output text"},
    "review": "## Quality Rating\nPASS\n\n## Issues Found\nNone\n\n## Final Assembled Output\ndone",
    "final_output": "done",
    "rating": "PASS",
    "code_files": [],
    "project_id": "",
}


async def _fake_pipeline(task, **kwargs):
    return dict(FAKE_RESULT)


# ── Health ───────────────────────────────────────────────────────────────

def test_health_shape(client):
    body = client.get("/health").json()
    assert body["status"] in ("ok", "degraded")
    assert body["nodes_online"] == 0
    assert body["tasks_pending"] == 0


# ── Node registration ────────────────────────────────────────────────────

def test_register_node_and_list(client):
    resp = _register(client)
    assert resp.status_code == 200
    assert "test-node" in resp.json()["message"]
    # model auto-tag applied
    assert "model:qwen3.5:4b" in resp.json()["capabilities"]

    listing = client.get("/nodes").json()
    assert listing["count"] == 1
    assert listing["nodes"][0]["node_id"] == "test-node"


# ── Node auth (sprint 1.4: verified, not assumed) ────────────────────────

@pytest.fixture
def node_secret(monkeypatch):
    import config

    cfg = config.DEFAULTS.copy()
    cfg["node_secret"] = "s3cret"
    monkeypatch.setattr(server_state, "get_config", lambda: cfg)
    return "s3cret"


def test_register_rejected_without_secret(client, node_secret):
    assert _register(client).status_code == 401


def test_register_rejected_with_wrong_secret(client, node_secret):
    assert _register(client, secret="wrong").status_code == 401


def test_register_accepted_with_secret(client, node_secret):
    assert _register(client, secret=node_secret).status_code == 200


def test_tasks_next_requires_secret(client, node_secret):
    resp = client.get("/tasks/next", params={"node_id": "x"})
    assert resp.status_code == 401


def test_submit_result_requires_secret(client, node_secret):
    resp = client.post(
        "/tasks/t1/result",
        json={"node_id": "x", "output": "data", "error": None},
    )
    assert resp.status_code == 401


def test_stream_tokens_requires_secret(client, node_secret):
    resp = client.post("/tasks/t1/stream", json={"node_id": "x", "tokens": "abc"})
    assert resp.status_code == 401


# ── Task distribution ────────────────────────────────────────────────────

def test_worker_gets_queued_task(client):
    _register(client)
    server.task_queue.append({"task_id": "t1", "title": "Build", "prompt": "p", "system": "s"})
    resp = client.get("/tasks/next", params={"node_id": "test-node"})
    assert resp.status_code == 200
    task = resp.json()
    assert task["task_id"] == "t1"
    assert task["assigned_to"] == "test-node"
    assert "t1" in server.task_inflight


def test_worker_respects_capability_requirements(client, monkeypatch):
    monkeypatch.setattr(server_state, "_LONG_POLL_TIMEOUT", 0.1)
    _register(client)  # capabilities: [model:qwen3.5:4b]
    server.task_queue.append(
        {"task_id": "t1", "title": "B", "prompt": "p", "system": "s", "requires": ["model:other-model"]}
    )
    resp = client.get("/tasks/next", params={"node_id": "test-node"})
    assert resp.status_code == 204  # can't serve it
    assert len(server.task_queue) == 1  # still queued for a capable node


def test_submit_result_awards_credits(client):
    _register(client)
    # Bound exactly as /tasks/next would issue it: an inflight task always has
    # an assigned node and attempt credentials.
    server.task_inflight["t1"] = {
        "task_id": "t1", "trace_id": "tr", "assigned_to": "test-node",
        "attempt_id": "a-t1", "nonce": "n-t1", "lease_expires_at": time.time() + 900,
    }
    resp = client.post(
        "/tasks/t1/result",
        json={"node_id": "test-node", "output": "the deliverable", "error": None,
              "elapsed_seconds": 2.0, "attempt_id": "a-t1", "nonce": "n-t1"},
    )
    assert resp.status_code == 200
    assert resp.json()["credits_earned"] == 5
    assert server.task_results["t1"]["output"] == "the deliverable"
    assert server.nodes["test-node"]["tasks_completed"] == 1
    # Contribution lands in the ledger (temp CWD)
    assert json.loads(Path("ledger.json").read_text())[0]["contributor"] == "test-node"


def test_circuit_breaker_blacklists_after_failures(client):
    _register(client)
    for i in range(server._FAILURE_THRESHOLD):
        server.task_inflight[f"t{i}"] = {
            "task_id": f"t{i}", "assigned_to": "test-node",
            "attempt_id": f"a-cb-{i}", "nonce": f"n-cb-{i}",
            "lease_expires_at": time.time() + 900,
        }
        client.post(
            f"/tasks/t{i}/result",
            json={"node_id": "test-node", "output": None, "error": "boom",
                  "attempt_id": f"a-cb-{i}", "nonce": f"n-cb-{i}"},
        )
    # Node is now blacklisted — next poll gets 429 circuit_open
    resp = client.get("/tasks/next", params={"node_id": "test-node"})
    assert resp.status_code == 429
    assert resp.json()["error"] == "circuit_open"


# ── Pitch endpoints ──────────────────────────────────────────────────────

def test_pitch_empty_task_rejected(client):
    assert client.post("/pitch", json={"task": "   "}).status_code == 422


def test_pitch_overlong_task_rejected(client):
    assert client.post("/pitch", json={"task": "x" * 1001}).status_code == 422


def test_pitch_runs_pipeline(client, monkeypatch):
    monkeypatch.setattr(routes_pitch, "run_pipeline", _fake_pipeline)
    resp = client.post("/pitch", json={"task": "build a thing"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["project_dir"] == "output/fake"
    assert body["results"] == {"1": "output text"}


def test_pitch_rate_limited(client, monkeypatch):
    monkeypatch.setattr(routes_pitch, "run_pipeline", _fake_pipeline)
    for _ in range(server._RATE_MAX):
        assert client.post("/pitch", json={"task": "t"}).status_code == 200
    resp = client.post("/pitch", json={"task": "t"})
    assert resp.status_code == 429
    assert resp.headers["X-RateLimit-Remaining"] == "0"


def test_pitch_async_rate_limited(client, monkeypatch):
    monkeypatch.setattr(routes_pitch, "run_pipeline", _fake_pipeline)
    for _ in range(server._RATE_MAX):
        assert client.post("/pitch/async", json={"task": "t"}).status_code == 200
    assert client.post("/pitch/async", json={"task": "t"}).status_code == 429


def test_pitch_async_returns_job(client, monkeypatch):
    monkeypatch.setattr(routes_pitch, "run_pipeline", _fake_pipeline)
    resp = client.post("/pitch/async", json={"task": "async thing"})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    status = client.get(f"/jobs/{job_id}")
    assert status.status_code == 200
    assert status.json()["task"] == "async thing"
    # TestClient runs the loop to completion on exit; here status may be
    # queued/running/complete depending on scheduling — all valid
    assert status.json()["status"] in ("queued", "running", "complete")


def test_get_unknown_job_404(client):
    assert client.get("/jobs/nope").status_code == 404


# ── History / gallery ────────────────────────────────────────────────────

def _write_fake_run(timestamp="20260801_120000", task="Build a widget", rating="PASS"):
    run_dir = Path("output") / timestamp
    run_dir.mkdir(parents=True)
    log = {
        "task": task,
        "timestamp": timestamp,
        "plan": [{"id": 1, "title": "t", "prompt": "p", "depends_on": []}],
        "results": {"1": "out"},
        "review": "## Quality Rating\nPASS",
        "rating": rating,
        "mode": "local",
        "project_id": "",
    }
    (run_dir / "full_log.json").write_text(json.dumps(log))
    (run_dir / "review.md").write_text("## Quality Rating\nPASS\n")
    (run_dir / "output.md").write_text("# The widget\ndone")
    return run_dir


def test_history_empty(client):
    body = client.get("/history").json()
    assert body["runs"] == []


def test_history_lists_runs_and_search(client):
    _write_fake_run("20260801_120000", task="Build a widget")
    _write_fake_run("20260801_130000", task="Write a poem")
    assert client.get("/history").json()["count"] == 2
    hits = client.get("/history", params={"search": "poem"}).json()
    assert hits["count"] == 1
    assert hits["runs"][0]["task"] == "Write a poem"


def test_history_detail_and_404(client):
    _write_fake_run("20260801_120000")
    body = client.get("/history/20260801_120000").json()
    assert body["task"] == "Build a widget"
    assert body["rating"] == "PASS"
    assert client.get("/history/29990101_000000").status_code == 404


def test_history_download_zip(client):
    _write_fake_run("20260801_120000")
    resp = client.get("/history/20260801_120000/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert resp.content[:2] == b"PK"  # ZIP magic


def test_gallery_cards(client):
    _write_fake_run("20260801_120000")
    body = client.get("/gallery").json()
    assert body["count"] == 1
    assert body["cards"][0]["task"] == "Build a widget"
    assert body["cards"][0]["preview"].startswith("# The widget")


def test_share_page_renders(client):
    _write_fake_run("20260801_120000")
    resp = client.get("/share/20260801_120000")
    assert resp.status_code == 200
    assert "Build a widget" in resp.text


# ── Standings / metrics / projects ───────────────────────────────────────

def test_standings_empty(client):
    assert client.get("/standings").json()["standings"] == []


def test_metrics_shape(client):
    body = client.get("/metrics").json()
    assert body["nodes_online"] == 0
    assert body["tasks_in_queue"] == 0


def test_projects_crud(client):
    resp = client.post("/projects", json={"name": "My Project", "initial_task": "build it"})
    assert resp.status_code == 200
    pid = resp.json()["project_id"]

    listing = client.get("/projects").json()["projects"]
    assert any(p["project_id"] == pid for p in listing)

    detail = client.get(f"/projects/{pid}").json()
    assert detail["name"] == "My Project"
    assert client.get("/projects/missing").status_code == 404


def test_events_endpoint(client):
    body = client.get("/events").json()
    assert isinstance(body["events"], list)


def test_dashboard_serves_template(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "<!DOCTYPE html>" in resp.text
    assert "text/html" in resp.headers["content-type"]
