import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

import config
from coordinator_lock import CoordinatorLock
from scripts.preflight import PreflightReport
import server


def test_operator_health_is_private_and_reports_process_identity(tmp_path):
    settings = config.DEFAULTS.copy()
    settings["viewer_key"] = "v" * config.MIN_STATIC_CREDENTIAL_LENGTH
    config.get._cache = settings

    with TestClient(server.app) as client:
        assert client.get("/v1/operator/health").status_code == 401
        response = client.get(
            "/v1/operator/health",
            headers={"X-Viewer-Key": settings["viewer_key"]},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["deployment_mode"] == "local"
        assert payload["single_coordinator_lock"] is True
        assert len(payload["instance_id"]) == 32
        assert settings["viewer_key"] not in response.text

    # Lifespan shutdown releases the kernel lock for the next coordinator.
    replacement = CoordinatorLock(tmp_path)
    replacement.acquire()
    replacement.release()


def test_lifespan_acquires_lock_before_migrations_or_background_tasks(monkeypatch, tmp_path):
    events: list[str] = []
    identity = SimpleNamespace(instance_id="instance")

    class FakeLock:
        state_dir = tmp_path

        def __init__(self, *, deployment_mode):
            events.append(f"lock-created:{deployment_mode}")

        def acquire(self):
            events.append("lock-acquired")
            return identity

        def release(self):
            events.append("lock-released")

    class FakeStore:
        def __init__(self, name):
            self.name = name

        def migrate(self):
            events.append(f"migrate:{self.name}")

    class FakeService:
        def reconcile_after_restart(self):
            events.append("reconcile")

    async def cleanup():
        events.append("cleanup-started")
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "validate_single_worker", lambda: events.append("workers"))
    monkeypatch.setattr(server, "CoordinatorLock", FakeLock)
    monkeypatch.setattr(server, "get_config", lambda: {"deployment_mode": "local"})
    monkeypatch.setattr(
        server,
        "run_preflight",
        lambda *_args, **_kwargs: (
            events.append("preflight") or PreflightReport(True, "local", ())
        ),
    )
    monkeypatch.setattr(server, "_init_db", lambda: events.append("init-db"))
    monkeypatch.setattr(server, "_db_load_jobs", lambda: events.append("load-jobs"))
    monkeypatch.setattr(server, "get_artifact_store", lambda: FakeStore("artifacts"))
    monkeypatch.setattr(server, "get_share_store", lambda: FakeStore("shares"))
    monkeypatch.setattr(server, "get_execution_service", lambda: FakeService())
    monkeypatch.setattr(server, "warn_if_viewer_auth_unconfigured", lambda: None)
    monkeypatch.setattr(server, "_cleanup_stale_nodes", cleanup)

    async def exercise_lifespan():
        app = SimpleNamespace(state=SimpleNamespace())
        async with server._lifespan(app):
            await asyncio.sleep(0)
            assert events.index("lock-acquired") < events.index("init-db")
            assert events.index("lock-acquired") < events.index("cleanup-started")

    asyncio.run(exercise_lifespan())
    assert events[-1] == "lock-released"
