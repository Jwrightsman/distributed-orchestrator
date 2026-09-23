"""Dashboard pitching keeps the pitch and viewer authorities separate.

A viewer session lets the dashboard read; it never lets it spend compute. On a
coordinator with a pitch_key, the dashboard must send X-Pitch-Key, and a
refused pitch must be told apart from a lapsed viewer session so the page
asks for the right key instead of locking itself.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import access_control
import config
import dashboard
import routes_access
import routes_pitch
import server
import server_state

VIEWER_KEY = "viewer-secret-for-the-dashboard-test"
PITCH_KEY = "pitch-secret-for-the-dashboard-test"


@pytest.fixture(autouse=True)
def clean_server_state():
    server.jobs.clear()
    server._pitch_timestamps.clear()
    yield
    server.jobs.clear()
    server._pitch_timestamps.clear()


@pytest.fixture
def keyed_config(monkeypatch):
    cfg = config.DEFAULTS.copy()
    cfg.update({
        "viewer_key": VIEWER_KEY,
        "pitch_key": PITCH_KEY,
        "viewer_session_ttl_seconds": 3600,
        "viewer_cookie_secure": False,
    })
    for module in (access_control, routes_access, server_state, dashboard):
        monkeypatch.setattr(module, "get_config", lambda: cfg)
    return cfg


async def _fake_pipeline(task, **kwargs):
    return {
        "project_dir": "output/fake",
        "plan": [{"id": 1, "title": "t", "prompt": "p", "depends_on": []}],
        "results": {1: "out"},
        "review": "## Quality Rating\nPASS",
        "final_output": "out",
        "rating": "PASS",
        "code_files": [],
        "project_id": "",
    }


@pytest.fixture
def signed_in(keyed_config, monkeypatch):
    """A browser that has exchanged the viewer key for a session cookie."""
    monkeypatch.setattr(routes_pitch, "run_pipeline", _fake_pipeline)
    with TestClient(server.app) as client:
        login = client.post("/v1/viewer/session", json={"viewer_key": VIEWER_KEY})
        assert login.status_code == 200
        # The session really is good: a viewer-only read answers.
        assert client.get("/jobs").status_code == 200
        yield client


def _dashboard_pitch(client, headers=None):
    """The request the dashboard's composer makes."""
    return client.post(
        "/pitch/async",
        json={"task": "Build a hello world script", "strategy": "auto"},
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def test_viewer_session_alone_cannot_pitch(signed_in):
    resp = _dashboard_pitch(signed_in)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid or missing X-Pitch-Key header"
    assert "Bearer" not in resp.headers.get("WWW-Authenticate", "")
    assert server.jobs == {}


def test_viewer_key_presented_as_pitch_key_is_refused(signed_in):
    resp = _dashboard_pitch(signed_in, {"X-Pitch-Key": VIEWER_KEY})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid or missing X-Pitch-Key header"
    assert server.jobs == {}


def test_pitch_refusal_and_viewer_refusal_say_different_things(keyed_config):
    """The page tells the two 401s apart by the detail; they must differ."""
    with TestClient(server.app) as client:
        viewer = client.get("/jobs")
        pitch = _dashboard_pitch(client)
    assert viewer.status_code == pitch.status_code == 401
    assert viewer.json()["detail"] != pitch.json()["detail"]


def test_dashboard_flow_with_pitch_key_succeeds(signed_in):
    resp = _dashboard_pitch(signed_in, {"X-Pitch-Key": PITCH_KEY})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    status = signed_in.get(f"/jobs/{job_id}")
    assert status.status_code == 200
    assert status.json()["task"] == "Build a hello world script"


def test_served_dashboard_recognises_the_pitch_refusal(signed_in):
    """The script compares against the coordinator's own words, not a copy
    that can drift: read the detail off a real refusal, then find it."""
    detail = _dashboard_pitch(signed_in).json()["detail"]
    page = signed_in.get("/dashboard")
    assert page.status_code == 200
    html = page.text
    assert f"const PITCH_KEY_REFUSED = '{detail}';" in html
    assert "if (detail === PITCH_KEY_REFUSED) throw new PitchKeyRefused();" in html
    # The key rides a header, from this tab's sessionStorage, never a URL.
    assert "headers['X-Pitch-Key'] = pitchKeyInMemory;" in html
    assert "sessionStorage.setItem(PITCH_KEY_SLOT, key)" in html
    assert "localStorage.setItem(PITCH_KEY_SLOT" not in html
    assert 'id="pitch-key-form"' in html
    assert 'id="pitch-key-input" type="password"' in html
