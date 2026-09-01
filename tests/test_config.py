"""Tests for config loading (runs against a temp CWD — see conftest)."""

import json
from pathlib import Path

import pytest

import config


def test_defaults_when_no_config_file():
    cfg = config.load()
    assert cfg["model"] == config.DEFAULTS["model"]
    assert cfg["timeout"] == config.DEFAULTS["timeout"]
    assert cfg["node_secret"] == ""
    assert cfg["node_enrollment_mode"] == "compat"
    assert cfg["private_overlay"] is False
    assert cfg["capability_evidence_mode"] == "off"
    assert cfg["capability_evidence_min_samples"] == 5
    assert cfg["validator_execution_mode"] == "auto"
    assert cfg["validator_subprocess_timeout_seconds"] == 10
    assert cfg["validator_subprocess_memory_mb"] == 256
    assert cfg["validator_subprocess_request_max_bytes"] == 2 * 1_024 * 1_024
    assert cfg["validator_subprocess_response_max_bytes"] == 32 * 1_024


def test_config_json_overrides_defaults():
    Path("config.json").write_text(
        json.dumps(
            {
                "model": "some-other-model",
                "timeout": 42,
                "capability_evidence_mode": "shadow",
                "capability_evidence_min_samples": 17,
                "validator_execution_mode": "subprocess",
                "validator_subprocess_timeout_seconds": 7,
                "validator_subprocess_memory_mb": 128,
                "validator_subprocess_request_max_bytes": 65_536,
                "validator_subprocess_response_max_bytes": 4_096,
            }
        )
    )
    cfg = config.load()
    assert cfg["model"] == "some-other-model"
    assert cfg["timeout"] == 42
    assert cfg["capability_evidence_mode"] == "shadow"
    assert cfg["capability_evidence_min_samples"] == 17
    assert cfg["validator_execution_mode"] == "subprocess"
    assert cfg["validator_subprocess_timeout_seconds"] == 7
    assert cfg["validator_subprocess_memory_mb"] == 128
    assert cfg["validator_subprocess_request_max_bytes"] == 65_536
    assert cfg["validator_subprocess_response_max_bytes"] == 4_096
    # Untouched keys keep their defaults
    assert cfg["planner_retries"] == config.DEFAULTS["planner_retries"]


def test_corrupt_config_falls_back_to_defaults():
    Path("config.json").write_text("{broken json")
    cfg = config.load()
    assert cfg == config.DEFAULTS


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("capability_evidence_mode", "active"),
        ("capability_evidence_mode", None),
        ("capability_evidence_min_samples", 0),
        ("capability_evidence_min_samples", 1_001),
        ("capability_evidence_min_samples", True),
        ("capability_evidence_min_samples", 5.0),
    ],
)
def test_invalid_capability_evidence_config_falls_back_locally(name, value):
    Path("config.json").write_text(json.dumps({name: value}))

    cfg = config.load()

    assert cfg[name] == config.DEFAULTS[name]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("capability_evidence_mode", "active"),
        ("capability_evidence_min_samples", 0),
        ("capability_evidence_min_samples", 1_001),
        ("capability_evidence_min_samples", True),
    ],
)
def test_invalid_capability_evidence_config_fails_strict_loading(name, value):
    Path("config.json").write_text(json.dumps({name: value}))

    with pytest.raises(config.ConfigError, match=name):
        config.load(strict=True)


@pytest.mark.parametrize("minimum", [1, 1_000])
def test_capability_evidence_sample_bounds_are_inclusive(minimum):
    Path("config.json").write_text(
        json.dumps({"capability_evidence_min_samples": minimum})
    )

    assert config.load()["capability_evidence_min_samples"] == minimum


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("validator_execution_mode", "process"),
        ("validator_execution_mode", None),
        ("validator_subprocess_timeout_seconds", 0),
        ("validator_subprocess_timeout_seconds", 121),
        ("validator_subprocess_timeout_seconds", True),
        ("validator_subprocess_timeout_seconds", 10.0),
        ("validator_subprocess_memory_mb", 127),
        ("validator_subprocess_memory_mb", 1_025),
        ("validator_subprocess_memory_mb", False),
        ("validator_subprocess_memory_mb", 256.0),
        ("validator_subprocess_request_max_bytes", 16_383),
        ("validator_subprocess_request_max_bytes", 16 * 1_024 * 1_024 + 1),
        ("validator_subprocess_request_max_bytes", True),
        ("validator_subprocess_request_max_bytes", 65_536.0),
        ("validator_subprocess_response_max_bytes", 1_023),
        ("validator_subprocess_response_max_bytes", 256 * 1_024 + 1),
        ("validator_subprocess_response_max_bytes", True),
        ("validator_subprocess_response_max_bytes", 4_096.0),
    ],
)
def test_invalid_validator_subprocess_config_falls_back_locally(name, value):
    Path("config.json").write_text(json.dumps({name: value}))

    cfg = config.load()

    assert cfg[name] == config.DEFAULTS[name]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("validator_execution_mode", "process"),
        ("validator_subprocess_timeout_seconds", 0),
        ("validator_subprocess_timeout_seconds", True),
        ("validator_subprocess_memory_mb", 1_025),
        ("validator_subprocess_memory_mb", False),
        ("validator_subprocess_request_max_bytes", 16_383),
        ("validator_subprocess_request_max_bytes", True),
        ("validator_subprocess_response_max_bytes", 256 * 1_024 + 1),
        ("validator_subprocess_response_max_bytes", True),
    ],
)
def test_invalid_validator_subprocess_config_fails_strict_loading(name, value):
    Path("config.json").write_text(json.dumps({name: value}))

    with pytest.raises(config.ConfigError, match=name):
        config.load(strict=True)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (
            "validator_subprocess_timeout_seconds",
            config.MIN_VALIDATOR_SUBPROCESS_TIMEOUT_SECONDS,
        ),
        (
            "validator_subprocess_timeout_seconds",
            config.MAX_VALIDATOR_SUBPROCESS_TIMEOUT_SECONDS,
        ),
        (
            "validator_subprocess_memory_mb",
            config.MIN_VALIDATOR_SUBPROCESS_MEMORY_MB,
        ),
        (
            "validator_subprocess_memory_mb",
            config.MAX_VALIDATOR_SUBPROCESS_MEMORY_MB,
        ),
        (
            "validator_subprocess_request_max_bytes",
            config.MIN_VALIDATOR_SUBPROCESS_REQUEST_MAX_BYTES,
        ),
        (
            "validator_subprocess_request_max_bytes",
            config.MAX_VALIDATOR_SUBPROCESS_REQUEST_MAX_BYTES,
        ),
        (
            "validator_subprocess_response_max_bytes",
            config.MIN_VALIDATOR_SUBPROCESS_RESPONSE_MAX_BYTES,
        ),
        (
            "validator_subprocess_response_max_bytes",
            config.MAX_VALIDATOR_SUBPROCESS_RESPONSE_MAX_BYTES,
        ),
    ],
)
def test_validator_subprocess_numeric_bounds_are_inclusive(name, value):
    Path("config.json").write_text(json.dumps({name: value}))

    assert config.load()[name] == value


def test_get_caches():
    cfg1 = config.get()
    Path("config.json").write_text(json.dumps({"model": "changed-after-cache"}))
    cfg2 = config.get()
    assert cfg1 is cfg2  # same cached object — config is read once per process


def test_save_round_trip():
    cfg = config.load()
    cfg["model"] = "round-trip-model"
    config.save(cfg)
    assert json.loads(Path("config.json").read_text())["model"] == "round-trip-model"
