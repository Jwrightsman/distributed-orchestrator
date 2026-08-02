"""Tests for the keyless public pitch endpoint (sprint 2.4)."""

import pytest
from fastapi.testclient import TestClient

import config
import routes_pitch
import server
import server_state


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


def test_per_ip_hourly_limit(client, public_enabled):
    for _ in range(server_state._PUBLIC_RATE_MAX):
        assert client.post("/public/pitch", json={"task": "build a widget"}).status_code == 200
    resp = client.post("/public/pitch", json={"task": "build a widget"})
    assert resp.status_code == 429
    assert "per hour" in resp.json()["detail"]


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
