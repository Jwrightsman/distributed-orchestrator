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


def test_config_json_overrides_defaults():
    Path("config.json").write_text(
        json.dumps(
            {
                "model": "some-other-model",
                "timeout": 42,
                "capability_evidence_mode": "shadow",
                "capability_evidence_min_samples": 17,
            }
        )
    )
    cfg = config.load()
    assert cfg["model"] == "some-other-model"
    assert cfg["timeout"] == 42
    assert cfg["capability_evidence_mode"] == "shadow"
    assert cfg["capability_evidence_min_samples"] == 17
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
