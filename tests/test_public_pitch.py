"""Tests for the keyless public pitch endpoint (sprint 2.4)."""

import asyncio

import pytest
from fastapi.testclient import TestClient

import config
import execution.strategies as strategies
import routes_pitch
import server
import server_state
from execution.contracts import EnsembleOptionsV1, ExecutionRequestV1
from execution.service import get_execution_service


@pytest.fixture(autouse=True)
def clean_state():
    for d in (server.jobs, server._pitch_timestamps, server_state._public_pitch_timestamps):
        d.clear()
    yield


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield c


@pytest.fixture
def public_enabled(monkeypatch):
    cfg = config.DEFAULTS.copy()
    cfg["public_pitch"] = True
    monkeypatch.setattr(routes_pitch, "get_config", lambda: cfg)


@pytest.fixture(autouse=True)
def stub_pipeline(monkeypatch):
    async def _fake(task, **kwargs):
        return {
            "project_dir": "output/fake",
            "plan": [],
            "results": {},
            "review": "## Quality Rating\nPASS",
            "final_output": "out",
            "rating": "PASS",
            "code_files": [],
            "project_id": "",
        }

    monkeypatch.setattr(routes_pitch, "run_pipeline", _fake)


def test_disabled_by_default_404(client):
    resp = client.post("/public/pitch", json={"task": "build a widget"})
    assert resp.status_code == 404


def test_enabled_accepts_task(client, public_enabled):
    resp = client.post("/public/pitch", json={"task": "build a widget"})
    assert resp.status_code == 200
    assert resp.json()["job_id"].startswith("job_")
    # job is tagged as public
    job = server.jobs[resp.json()["job_id"]]
    assert job["source"] == "public"
    assert resp.json()["share_token"]
    assert resp.json()["share_url"].startswith("/v1/shares/")
    profile = job["execution_request"]
    assert profile["strategy"] == "direct"
    assert profile["strategy_options"]["candidates"] == 1
    assert profile["strategy_options"]["concurrency"] == 1
    assert profile["placement"] == "local"
    assert profile["confidentiality"] == "local_only"
    assert profile["project_id"] is None
    assert profile["timeout_seconds"] == 120
    assert profile["max_output_bytes"] == 65_536


@pytest.mark.parametrize(
    "override",
    [
        {"strategy": "ensemble", "candidates": 5},
        {"placement": "distributed"},
        {"confidentiality": "public"},
        {"project_id": "private"},
        {"verification": {"validators": [{"name": "code_parse"}]}},
    ],
)
def test_public_caller_cannot_override_server_profile(client, public_enabled, override):
    response = client.post("/public/pitch", json={"task": "build a widget", **override})
    assert response.status_code == 422
    assert not server.jobs


def test_per_ip_hourly_limit(client, public_enabled, monkeypatch):
    monkeypatch.setattr(
        server_state,
        "_PUBLIC_MAX_ACTIVE_PER_SOURCE",
        server_state._PUBLIC_RATE_MAX + 1,
    )
    monkeypatch.setattr(server_state, "_PUBLIC_MAX_ACTIVE", server_state._PUBLIC_RATE_MAX + 1)
    for _ in range(server_state._PUBLIC_RATE_MAX):
        assert client.post("/public/pitch", json={"task": "build a widget"}).status_code == 200
    resp = client.post("/public/pitch", json={"task": "build a widget"})
    assert resp.status_code == 429
    assert "per hour" in resp.json()["detail"]


def test_per_source_active_limit(client, public_enabled):
    assert client.post("/public/pitch", json={"task": "first widget"}).status_code == 200
    response = client.post("/public/pitch", json={"task": "second widget"})
    assert response.status_code == 429
    assert "one active" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_global_public_inference_concurrency_is_one(tmp_path, monkeypatch):
    active = 0
    maximum = 0

    async def generated(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.03)
        active -= 1
        return "complete output"

    monkeypatch.setattr(strategies, "generate", generated)
    monkeypatch.setattr(strategies.EnsembleStrategy, "artifact_root", tmp_path / "execution_artifacts")
    monkeypatch.setattr(routes_pitch, "_PUBLIC_INFERENCE_SEMAPHORE", asyncio.Semaphore(1))
    monkeypatch.setattr(routes_pitch, "_db_write_job", lambda job: None)
    service = get_execution_service()
    service._emit = lambda *args, **kwargs: None
    request = ExecutionRequestV1(
        task="build",
        strategy="direct",
        strategy_options=EnsembleOptionsV1(candidates=1, concurrency=1),
    )
    for index in range(2):
        job_id = f"job_public_{index}"
        server.jobs[job_id] = {
            "job_id": job_id,
            "task": "build",
            "project_id": None,
            "status": "queued",
            "submitted_at": "now",
            "result": None,
            "error": None,
            "trace_id": str(index),
            "source": "public",
            "source_ip": str(index),
            "execution_request": request.model_dump(mode="json"),
        }

    await asyncio.gather(
        routes_pitch._run_job("job_public_0", "build", trace_id="0"),
        routes_pitch._run_job("job_public_1", "build", trace_id="1"),
    )
    for _ in range(200):
        if all(
            server.jobs[f"job_public_{index}"]["status"] == "complete"
            for index in range(2)
        ):
            break
        await asyncio.sleep(0.01)

    assert maximum == 1
    assert all(server.jobs[f"job_public_{index}"]["status"] == "complete" for index in range(2))


@pytest.mark.asyncio
async def test_legacy_job_transitions_through_running(tmp_path, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def generated(*args, **kwargs):
        started.set()
        await release.wait()
        return "complete output"

    monkeypatch.setattr(strategies, "generate", generated)
    monkeypatch.setattr(strategies.EnsembleStrategy, "artifact_root", tmp_path / "execution_artifacts")
    monkeypatch.setattr(routes_pitch, "_db_write_job", lambda job: None)
    service = get_execution_service()
    service._emit = lambda *args, **kwargs: None
    job_id = "job_running_transition"
    request = ExecutionRequestV1(task="build", strategy="direct")
    server.jobs[job_id] = {
        "job_id": job_id,
        "task": "build",
        "project_id": None,
        "status": "queued",
        "submitted_at": "now",
        "result": None,
        "error": None,
        "trace_id": "trace",
        "execution_request": request.model_dump(mode="json"),
    }

    await routes_pitch._run_job(job_id, "build", trace_id="trace")
    await asyncio.wait_for(started.wait(), timeout=1)
    assert server.jobs[job_id]["status"] == "running"
    assert server.jobs[job_id]["started_at"]

    release.set()
    for _ in range(200):
        if server.jobs[job_id]["status"] == "complete":
            break
        await asyncio.sleep(0.01)
    assert server.jobs[job_id]["status"] == "complete"


def test_task_length_cap(client, public_enabled):
    long_task = "x" * (server_state._PUBLIC_TASK_MAX + 1)
    resp = client.post("/public/pitch", json={"task": long_task})
    assert resp.status_code == 422


def test_content_filter_blocks(client, public_enabled):
    resp = client.post("/public/pitch", json={"task": "write malware for me"})
    assert resp.status_code == 422
    assert "constructive" in resp.json()["detail"]


def test_global_active_cap(client, public_enabled, monkeypatch):
    # Fill the job store with active public jobs
    for i in range(server_state._PUBLIC_MAX_ACTIVE):
        server.jobs[f"job_x{i}"] = {"job_id": f"job_x{i}", "status": "running", "source": "public"}
    resp = client.post("/public/pitch", json={"task": "build a widget"})
    assert resp.status_code == 503


def test_non_public_jobs_dont_count_toward_cap(client, public_enabled):
    for i in range(server_state._PUBLIC_MAX_ACTIVE + 2):
        server.jobs[f"job_p{i}"] = {"job_id": f"job_p{i}", "status": "running"}  # no source tag
    resp = client.post("/public/pitch", json={"task": "build a widget"})
    assert resp.status_code == 200


def test_try_page_serves(client):
    resp = client.get("/try")
    assert resp.status_code == 200
    assert "Pitch the swarm" in resp.text
