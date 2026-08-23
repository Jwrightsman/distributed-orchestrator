"""Security-hardening tests (sprint 1.4 — WAN readiness).

Node auth is covered in test_server_api.py. This file verifies the pitch_key
gate on every pitch endpoint, the /pitch/distributed rate limit, and the
output/ directory size cap.
"""

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
import routes_pitch
import server
import server_state
from server_state import _prune_output_dir


@pytest.fixture(autouse=True)
def clean_server_state():
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


@pytest.fixture
def pitch_key(monkeypatch):
    cfg = config.DEFAULTS.copy()
    cfg["pitch_key"] = "k3y"
    monkeypatch.setattr(server_state, "get_config", lambda: cfg)
    return "k3y"


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


def test_unhandled_error_log_redacts_share_capability(caplog):
    token = "plaintext-share-capability"
    request = SimpleNamespace(
        url=SimpleNamespace(path=f"/v1/shares/{token}/artifacts/result.txt")
    )

    with caplog.at_level(logging.ERROR, logger="mycelium"):
        response = asyncio.run(
            server.unhandled_exception_handler(request, RuntimeError("failed"))
        )

    assert response.status_code == 500
    assert token not in caplog.text
    assert "/v1/shares/<redacted>/artifacts/result.txt" in caplog.text


async def _fake_plan(task, max_retries=None, memory_context=""):
    return [{"id": 1, "title": "t", "prompt": "p", "depends_on": []}]


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Stub out every path that would reach Ollama.

    /pitch/distributed calls plan() before its no-nodes fallback to
    run_pipeline — both must be stubbed or tests block on real inference.
    """
    monkeypatch.setattr(routes_pitch, "run_pipeline", _fake_pipeline)
    monkeypatch.setattr(routes_pitch, "plan", _fake_plan)


# ── pitch_key gate ───────────────────────────────────────────────────────

@pytest.mark.parametrize("endpoint", ["/pitch", "/pitch/async", "/pitch/distributed"])
def test_pitch_endpoints_reject_missing_key(client, pitch_key, endpoint):
    assert client.post(endpoint, json={"task": "t"}).status_code == 401


@pytest.mark.parametrize("endpoint", ["/pitch", "/pitch/async", "/pitch/distributed"])
def test_pitch_endpoints_reject_wrong_key(client, pitch_key, endpoint):
    resp = client.post(endpoint, json={"task": "t"}, headers={"X-Pitch-Key": "wrong"})
    assert resp.status_code == 401


@pytest.mark.parametrize("endpoint", ["/pitch", "/pitch/async", "/pitch/distributed"])
def test_pitch_endpoints_accept_correct_key(client, pitch_key, endpoint, stub_pipeline):
    resp = client.post(endpoint, json={"task": "t"}, headers={"X-Pitch-Key": pitch_key})
    assert resp.status_code == 200


def test_pitch_open_when_no_key_configured(client, stub_pipeline):
    assert client.post("/pitch", json={"task": "t"}).status_code == 200


# ── /pitch/distributed rate limit (was the one unprotected pitch surface) ─

def test_pitch_distributed_rate_limited(client, stub_pipeline):
    for _ in range(server._RATE_MAX):
        assert client.post("/pitch/distributed", json={"task": "t"}).status_code == 200
    assert client.post("/pitch/distributed", json={"task": "t"}).status_code == 429


# ── output/ size cap ─────────────────────────────────────────────────────

def _fake_run(name: str, size_bytes: int):
    d = Path("output") / name
    d.mkdir(parents=True)
    (d / "output.md").write_bytes(b"x" * size_bytes)
    (d / "full_log.json").write_text(json.dumps({"task": name}))


def _set_cap(monkeypatch, mb):
    cfg = config.DEFAULTS.copy()
    cfg["output_max_mb"] = mb
    monkeypatch.setattr(server_state, "get_config", lambda: cfg)


def test_prune_deletes_oldest_runs_until_under_cap(monkeypatch):
    _set_cap(monkeypatch, 1)  # 1 MB cap
    _fake_run("20260801_010000", 600 * 1024)
    _fake_run("20260801_020000", 600 * 1024)
    _fake_run("20260801_030000", 600 * 1024)  # total ~1.8MB

    pruned = _prune_output_dir()

    # 1.8MB over a 1MB cap: dropping the two oldest gets under; newest survives
    assert pruned == ["20260801_010000", "20260801_020000"]
    remaining = sorted(d.name for d in Path("output").iterdir())
    assert remaining == ["20260801_030000"]


def test_prune_noop_under_cap(monkeypatch):
    _set_cap(monkeypatch, 10)
    _fake_run("20260801_010000", 1024)
    assert _prune_output_dir() == []
    assert (Path("output") / "20260801_010000").exists()


def test_prune_disabled_when_cap_zero(monkeypatch):
    _set_cap(monkeypatch, 0)
    _fake_run("20260801_010000", 5 * 1024 * 1024)
    assert _prune_output_dir() == []
    assert (Path("output") / "20260801_010000").exists()


def test_prune_never_deletes_an_active_artifact_root(monkeypatch):
    _set_cap(monkeypatch, 1)
    _fake_run("20260801_010000", 600 * 1024)
    _fake_run("20260801_020000", 600 * 1024)
    _fake_run("20260801_030000", 600 * 1024)
    active = (Path("output") / "20260801_010000").resolve()

    class Store:
        def active_root_paths(self):
            return {active}

    import execution.artifacts as artifacts

    monkeypatch.setattr(artifacts, "get_artifact_store", lambda: Store())

    pruned = _prune_output_dir()

    assert "20260801_010000" not in pruned
    assert active.exists()


# ── The 500 handler must not echo the exception ──────────────────────
#
# This was hardened once and silently un-hardened: a second
# @app.exception_handler(Exception) was registered further down server.py, and
# Starlette keys handlers by exception class, so the last registration wins.
# The generic handler became dead code and the leaky one served every 500 —
# on a public orchestrator. The count assertion is the part that matters: it
# fails the moment someone adds another handler rather than editing this one.

def test_only_one_handler_is_registered_for_unhandled_exceptions():
    """Two handlers for Exception means the last one wins, silently."""
    registered = [
        name for name, obj in vars(server).items()
        if callable(obj) and getattr(obj, "__doc__", None)
        and "generic 500" in (obj.__doc__ or "")
    ]
    assert len(registered) == 1, f"expected one 500 handler, found {registered}"
    assert server.app.exception_handlers[Exception].__name__ == registered[0]


def test_unhandled_exception_does_not_leak_its_message(monkeypatch):
    """A 500 must not carry exception text — it routinely holds paths and config."""
    secret = "C:/Users/someone/.ssh/swarm_orchestrator"

    async def boom():
        raise RuntimeError(f"connection failed reading {secret}")

    server.app.router.add_api_route("/_test_boom", boom, methods=["GET"])
    try:
        client = TestClient(server.app, raise_server_exceptions=False)
        resp = client.get("/_test_boom")
        assert resp.status_code == 500
        assert secret not in resp.text
        assert "RuntimeError" not in resp.text
        assert resp.json() == {"detail": "internal server error"}
    finally:
        server.app.router.routes = [
            r for r in server.app.router.routes
            if getattr(r, "path", None) != "/_test_boom"
        ]
