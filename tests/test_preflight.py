import json
from pathlib import Path

import config
from coordinator_lock import CoordinatorLock
from scripts import preflight


def _trusted_config(tmp_path: Path) -> tuple[Path, dict]:
    path = tmp_path / "config.json"
    config.ensure_trusted_alpha_config(path, private_overlay=True)
    return path, json.loads(path.read_text(encoding="utf-8"))


def _check(report: preflight.PreflightReport, name: str):
    return next(check for check in report.checks if check.name == name)


def test_valid_trusted_alpha_configuration_passes_without_disclosing_secrets(tmp_path):
    path, stored = _trusted_config(tmp_path)

    report = preflight.run_preflight(path, state_dir=tmp_path)
    rendered = report.as_json()

    assert report.ok
    assert report.mode == "trusted_alpha"
    assert _check(report, "coordinator_lock").status == "pass"
    for name in ("viewer_key", "pitch_key", "node_secret"):
        assert stored[name] not in rendered


def test_malformed_trusted_alpha_configuration_fails(tmp_path):
    path, _stored = _trusted_config(tmp_path)
    path.write_text("{broken", encoding="utf-8")

    report = preflight.run_preflight(path, state_dir=tmp_path)

    assert not report.ok
    assert _check(report, "config_json").status == "error"


def test_unacknowledged_public_pitch_fails_trusted_preflight(tmp_path):
    path, stored = _trusted_config(tmp_path)
    stored["public_pitch"] = True
    stored["public_pitch_acknowledged"] = False
    config.save(stored, path)

    report = preflight.run_preflight(path, state_dir=tmp_path)

    assert not report.ok
    assert _check(report, "public_pitch").status == "error"


def test_missing_viewer_authority_fails_trusted_preflight(tmp_path):
    path, stored = _trusted_config(tmp_path)
    stored["viewer_key"] = ""
    config.save(stored, path)

    report = preflight.run_preflight(path, state_dir=tmp_path)

    assert not report.ok
    assert _check(report, "viewer_key").status == "error"


def test_trusted_alpha_rejects_legacy_only_node_enrollment(tmp_path):
    path, stored = _trusted_config(tmp_path)
    stored["node_enrollment_mode"] = "compat"
    config.save(stored, path)

    report = preflight.run_preflight(path, state_dir=tmp_path)

    assert not report.ok
    assert _check(report, "node_enrollment_mode").status == "error"
    assert "durable node enrollment" in _check(report, "node_enrollment_mode").message


def test_trusted_alpha_requires_https_or_private_overlay(tmp_path):
    path, stored = _trusted_config(tmp_path)
    stored["private_overlay"] = False
    stored["https_enabled"] = False
    config.save(stored, path)

    report = preflight.run_preflight(path, state_dir=tmp_path)

    assert not report.ok
    assert _check(report, "worker_transport").status == "error"


def test_local_compatibility_mode_is_explicit_warning(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "deployment_mode": "local",
                "node_enrollment_mode": "compat",
            }
        ),
        encoding="utf-8",
    )

    report = preflight.run_preflight(path, state_dir=tmp_path)

    assert report.ok
    assert _check(report, "node_enrollment_mode").status == "warning"


def test_cookie_and_https_mismatch_fails_trusted_preflight(tmp_path):
    path, stored = _trusted_config(tmp_path)
    stored["https_enabled"] = True
    stored["viewer_cookie_secure"] = False
    config.save(stored, path)

    report = preflight.run_preflight(path, state_dir=tmp_path)

    assert not report.ok
    assert _check(report, "https_cookie").status == "error"


def test_active_coordinator_fails_lock_check(tmp_path):
    path, _stored = _trusted_config(tmp_path)
    held = CoordinatorLock(tmp_path)
    held.acquire()
    try:
        report = preflight.run_preflight(path, state_dir=tmp_path)
    finally:
        held.release()

    assert not report.ok
    assert _check(report, "coordinator_lock").status == "error"


def test_local_nonloopback_open_authorities_warn_but_do_not_fail(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"deployment_mode": "local", "bind_host": "0.0.0.0"}),
        encoding="utf-8",
    )

    report = preflight.run_preflight(path, state_dir=tmp_path)

    assert report.ok
    assert _check(report, "bind_host").status == "warning"


def test_health_gate_requires_ok_and_private_route_protection():
    assert preflight.deployment_health_ready(
        {
            "status": "ok",
            "private_routes_protected": True,
            "node_enrollment_required": True,
        }
    )
    assert not preflight.deployment_health_ready(
        {
            "status": "degraded",
            "private_routes_protected": True,
            "node_enrollment_required": True,
        }
    )
    assert not preflight.deployment_health_ready(
        {
            "status": "ok",
            "private_routes_protected": False,
            "node_enrollment_required": True,
        }
    )
    assert not preflight.deployment_health_ready(
        {"status": "ok", "private_routes_protected": True}
    )
