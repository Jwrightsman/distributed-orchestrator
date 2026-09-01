"""Private-by-default viewer access without conflating worker or pitch keys."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import access_control
import routes_access
import server
from access_control import ViewerAccessMiddleware, authorize_viewer_websocket


@pytest.fixture
def viewer_config(monkeypatch):
    config = {
        "viewer_key": "viewer-secret",
        "viewer_session_ttl_seconds": 3600,
        "viewer_cookie_secure": False,
    }
    monkeypatch.setattr(access_control, "get_config", lambda: config)
    monkeypatch.setattr(routes_access, "get_config", lambda: config)
    return config


@pytest.fixture
def gated_app():
    app = FastAPI()
    app.add_middleware(ViewerAccessMiddleware)
    app.include_router(routes_access.router)

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket):
        if not await authorize_viewer_websocket(websocket):
            return
        await websocket.accept()
        await websocket.send_json({"ok": True})

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
    async def catch_all(path: str):
        return {"path": path}

    return app


def test_server_installs_viewer_middleware():
    assert any(item.cls is ViewerAccessMiddleware for item in server.app.user_middleware)


@pytest.mark.parametrize(
    "path",
    [
        "/v1/executions/" + "a" * 32,
        "/jobs/job_private",
        "/jobs",
        "/events",
        "/nodes",
        "/history",
        "/history/run-id",
        "/gallery",
        "/run/run-id",
        "/projects",
        "/projects/private-project",
        "/ledger",
        "/standings",
        "/metrics",
        "/dashboard",
        "/status",
        "/node/private-node",
        "/v1/operator/health",
    ],
)
def test_sensitive_read_routes_require_viewer(viewer_config, gated_app, path):
    with TestClient(gated_app) as client:
        response = client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("path", ["/", "/try", "/health", "/status.json", "/v1/shares/public-token"])
def test_deliberate_public_get_allowlist(viewer_config, gated_app, path):
    with TestClient(gated_app) as client:
        response = client.get(path)
    # The synthetic app may return a share 404 because no such token exists;
    # critically, the viewer middleware did not turn it into a 401.
    assert response.status_code != 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/pitch"),
        ("POST", "/pitch/async"),
        ("POST", "/pitch/distributed"),
        ("POST", "/v1/executions"),
        ("POST", "/nodes/register"),
        ("GET", "/tasks/next"),
        ("POST", "/tasks/task-1/result"),
        ("POST", "/tasks/task-1/stream"),
        ("POST", "/tasks/task-1/tokens"),
        ("POST", "/nodes/worker-1/heartbeat"),
        ("POST", "/nodes/worker-1/drain"),
    ],
)
def test_separately_authenticated_protocol_routes_are_not_viewer_gated(
    viewer_config, gated_app, method, path
):
    with TestClient(gated_app) as client:
        response = client.request(method, path)
    assert response.status_code != 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/nodes/worker-1/heartbeat"),
        ("POST", "/nodes/worker-1/heartbeat/extra"),
        ("GET", "/tasks/task-1/tokens"),
        ("POST", "/tasks/task-1/unknown"),
    ],
)
def test_worker_protocol_exemptions_are_method_and_shape_specific(
    viewer_config, gated_app, method, path
):
    with TestClient(gated_app) as client:
        response = client.request(method, path)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Viewer-Key": "viewer-secret"},
        {"Authorization": "Bearer viewer-secret"},
    ],
)
def test_header_credentials_grant_private_access(viewer_config, gated_app, headers):
    with TestClient(gated_app) as client:
        assert client.get("/events", headers=headers).status_code == 200


def test_wrong_viewer_key_is_denied(viewer_config, gated_app):
    with TestClient(gated_app) as client:
        assert client.get("/events", headers={"X-Viewer-Key": "wrong"}).status_code == 401


def test_signed_httponly_session_cookie_grants_access(viewer_config, gated_app):
    with TestClient(gated_app) as client:
        response = client.post("/v1/viewer/session", json={"viewer_key": "viewer-secret"})
        assert response.status_code == 200
        set_cookie = response.headers["set-cookie"].lower()
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
        assert "viewer-secret" not in set_cookie
        assert client.get("/events").status_code == 200

        logout = client.delete("/v1/viewer/session")
        assert logout.status_code == 204
        assert client.get("/events").status_code == 401


def test_tampered_and_expired_session_is_rejected(viewer_config):
    cookie, _ = access_control.issue_viewer_session(now=1_000, ttl_seconds=60)
    assert access_control.valid_viewer_session(cookie, now=1_059)
    assert not access_control.valid_viewer_session(cookie + "x", now=1_059)
    assert not access_control.valid_viewer_session(cookie, now=1_060)


def test_websocket_authentication(viewer_config, gated_app):
    with TestClient(gated_app) as client:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect("/ws/events"):
                pass
        assert rejected.value.code == 4401

        with client.websocket_connect(
            "/ws/events", headers={"X-Viewer-Key": "viewer-secret"}
        ) as websocket:
            assert websocket.receive_json() == {"ok": True}


def test_unconfigured_local_development_is_open_but_warns(monkeypatch, caplog):
    monkeypatch.setattr(access_control, "get_config", lambda: {"viewer_key": ""})
    app = FastAPI()
    app.add_middleware(ViewerAccessMiddleware)

    @app.get("/events")
    async def events():
        return {"events": []}

    with TestClient(app) as client:
        assert client.get("/events").status_code == 200
    assert access_control.viewer_health_fields() == {
        "private_routes_protected": False,
        "warnings": [
            "viewer_key is not configured; task-, result-, project-, and machine-sensitive routes are unprotected"
        ],
    }
    with caplog.at_level(logging.WARNING, logger="mycelium.access"):
        access_control.warn_if_viewer_auth_unconfigured()
    assert "private read routes are unprotected" in caplog.text
