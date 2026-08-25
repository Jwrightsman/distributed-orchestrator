import json
import os
import stat

import pytest

import config


def _secret(character: str) -> str:
    return character * config.MIN_STATIC_CREDENTIAL_LENGTH


def test_trusted_alpha_upgrade_generates_three_distinct_authorities(tmp_path):
    path = tmp_path / "config.json"

    result = config.ensure_trusted_alpha_config(
        path, model="test-model", private_overlay=True
    )

    stored = json.loads(path.read_text(encoding="utf-8"))
    values = [stored[name] for name in ("viewer_key", "pitch_key", "node_secret")]
    assert len(set(values)) == 3
    assert all(config.credential_meets_policy(value) for value in values)
    assert set(result.generated_authorities) == {
        "viewer_key",
        "pitch_key",
        "node_secret",
    }
    assert result.preserved_authorities == ()
    assert stored["deployment_mode"] == "trusted_alpha"
    assert stored["node_enrollment_mode"] == "required"
    assert stored["private_overlay"] is True
    assert stored["bind_host"] == "0.0.0.0"
    assert stored["model"] == "test-model"
    assert config.deployment_marker_path(path).is_file()
    assert not any(value in repr(result) for value in values)


def test_trusted_alpha_upgrade_does_not_invent_private_transport(tmp_path):
    path = tmp_path / "config.json"

    config.ensure_trusted_alpha_config(path)

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["private_overlay"] is False


def test_trusted_alpha_upgrade_preserves_safe_existing_values(tmp_path):
    path = tmp_path / "config.json"
    original = {
        "node_secret": _secret("n"),
        "pitch_key": _secret("p"),
        "viewer_key": _secret("v"),
        "custom_operator_setting": {"keep": True},
    }
    path.write_text(json.dumps(original), encoding="utf-8")

    result = config.ensure_trusted_alpha_config(path)
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert result.generated_authorities == ()
    assert set(result.preserved_authorities) == {
        "viewer_key",
        "pitch_key",
        "node_secret",
    }
    for name in ("viewer_key", "pitch_key", "node_secret"):
        assert stored[name] == original[name]
    assert stored["custom_operator_setting"] == {"keep": True}


def test_two_key_installation_adds_viewer_without_rotating_existing_keys(tmp_path):
    path = tmp_path / "config.json"
    original = {
        "node_secret": _secret("n"),
        "pitch_key": _secret("p"),
        "operator_note": "preserve me",
    }
    path.write_text(json.dumps(original), encoding="utf-8")

    result = config.ensure_trusted_alpha_config(path)
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert stored["node_secret"] == original["node_secret"]
    assert stored["pitch_key"] == original["pitch_key"]
    assert config.credential_meets_policy(stored["viewer_key"])
    assert len({stored["node_secret"], stored["pitch_key"], stored["viewer_key"]}) == 3
    assert result.generated_authorities == ("viewer_key",)
    assert set(result.preserved_authorities) == {"node_secret", "pitch_key"}
    assert stored["operator_note"] == "preserve me"


def test_duplicate_authority_is_rotated_without_rotating_earlier_authority(tmp_path):
    path = tmp_path / "config.json"
    duplicate = _secret("x")
    path.write_text(
        json.dumps(
            {
                "node_secret": duplicate,
                "pitch_key": duplicate,
                "viewer_key": _secret("v"),
            }
        ),
        encoding="utf-8",
    )

    result = config.ensure_trusted_alpha_config(path)
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert stored["node_secret"] == duplicate
    assert stored["pitch_key"] != duplicate
    assert "node_secret" in result.preserved_authorities
    assert "pitch_key" in result.generated_authorities


def test_trusted_alpha_upgrade_replaces_legacy_enrollment_mode(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "node_enrollment_mode": "compat",
                "node_secret": _secret("n"),
                "pitch_key": _secret("p"),
                "viewer_key": _secret("v"),
            }
        ),
        encoding="utf-8",
    )

    config.ensure_trusted_alpha_config(path)

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["node_enrollment_mode"] == "required"


def test_invalid_enrollment_mode_fails_strict_loading(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "deployment_mode": "trusted_alpha",
                "node_enrollment_mode": "implicit",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(config.ConfigError, match="node_enrollment_mode"):
        config.load(path, strict=True)


def test_trusted_marker_fails_closed_when_json_is_damaged(tmp_path):
    path = tmp_path / "config.json"
    config.ensure_trusted_alpha_config(path)
    path.write_text("{damaged", encoding="utf-8")

    with pytest.raises(config.ConfigError, match="configuration JSON is invalid"):
        config.load(path)


def test_manual_trusted_mode_is_remembered_out_of_band(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "deployment_mode": "trusted_alpha",
                "node_secret": _secret("n"),
                "pitch_key": _secret("p"),
                "viewer_key": _secret("v"),
            }
        ),
        encoding="utf-8",
    )

    assert config.load(path)["deployment_mode"] == "trusted_alpha"
    assert config.deployment_marker_path(path).is_file()
    path.write_text("{damaged", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(path)


def test_local_damaged_json_retains_compatibility_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{damaged", encoding="utf-8")

    assert config.load(path) == config.DEFAULTS


def test_environment_can_require_strict_mode(tmp_path, monkeypatch):
    path = tmp_path / "missing.json"
    monkeypatch.setenv("MYCELIUM_DEPLOYMENT_MODE", "trusted_alpha")

    with pytest.raises(config.ConfigError, match="missing"):
        config.load(path)


@pytest.mark.skipif(os.name == "nt", reason="Windows permissions are ACL-based")
def test_saved_configuration_is_private_to_owner(tmp_path):
    path = tmp_path / "config.json"
    config.ensure_trusted_alpha_config(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.deployment_marker_path(path).stat().st_mode) == 0o600
