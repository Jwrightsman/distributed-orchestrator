import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from coordinator_lock import CoordinatorLock
from scripts.preflight import PreflightReport, run_preflight
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
        validator_health = payload["validator_process"]
        assert validator_health["configured_execution_mode"] in {
            "auto",
            "subprocess",
            "inline",
        }
        assert validator_health["runner"]["runner_protocol_version"] == "1"
        assert "process_local_counters" in validator_health["runner"]
        assert "not correctness" in validator_health["runner"]["statement"]
        assert settings["viewer_key"] not in response.text

    # Lifespan shutdown releases the kernel lock for the next coordinator.
    replacement = CoordinatorLock(tmp_path)
    replacement.acquire()
    replacement.release()


@pytest.mark.parametrize(
    "argv",
    (
        ("uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"),
        ("uvicorn", "server:app", "--host=0.0.0.0", "--port", "8000"),
    ),
)
def test_runtime_bind_host_uses_uvicorn_arguments_for_preflight(
    tmp_path, monkeypatch, argv
):
    monkeypatch.delenv("MYCELIUM_BIND_HOST", raising=False)
    settings = {"deployment_mode": "local", "bind_host": "127.0.0.1"}
    config_path = tmp_path / "config.json"
    config_path.write_text('{"deployment_mode": "local"}', encoding="utf-8")

    runtime_host = server._runtime_bind_host(settings, argv=argv)
    report = run_preflight(
        config_path,
        state_dir=tmp_path,
        bind_host=runtime_host,
        check_lock=False,
    )
    bind_check = next(check for check in report.checks if check.name == "bind_host")

    assert runtime_host == "0.0.0.0"
    assert bind_check.status == "warning"
    assert "reachable beyond loopback" in bind_check.message


def test_runtime_bind_host_supports_explicit_signal_and_config_fallback(monkeypatch):
    settings = {"bind_host": "127.0.0.2"}
    monkeypatch.setenv("MYCELIUM_BIND_HOST", "  192.0.2.10  ")
    assert server._runtime_bind_host(settings, argv=()) == "192.0.2.10"

    monkeypatch.delenv("MYCELIUM_BIND_HOST")
    assert server._runtime_bind_host(settings, argv=("uvicorn", "server:app")) == (
        "127.0.0.2"
    )


def test_lifespan_acquires_lock_before_migrations_or_background_tasks(monkeypatch, tmp_path):
    events: list[str] = []
    identity = SimpleNamespace(instance_id="instance")
    preflight_arguments = {}

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

    def fake_preflight(*_args, **kwargs):
        preflight_arguments.update(kwargs)
        events.append("preflight")
        return PreflightReport(True, "local", ())

    monkeypatch.setenv("MYCELIUM_BIND_HOST", "0.0.0.0")
    monkeypatch.setattr(server, "validate_single_worker", lambda: events.append("workers"))
    monkeypatch.setattr(server, "CoordinatorLock", FakeLock)
    monkeypatch.setattr(server, "get_config", lambda: {"deployment_mode": "local"})
    monkeypatch.setattr(server, "run_preflight", fake_preflight)
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
    assert preflight_arguments["bind_host"] == "0.0.0.0"
    assert events[-1] == "lock-released"
