"""Tests for config loading (runs against a temp CWD — see conftest)."""

import json
from pathlib import Path

import config


def test_defaults_when_no_config_file():
    cfg = config.load()
    assert cfg["model"] == config.DEFAULTS["model"]
    assert cfg["timeout"] == config.DEFAULTS["timeout"]
    assert cfg["node_secret"] == ""


def test_config_json_overrides_defaults():
    Path("config.json").write_text(json.dumps({"model": "some-other-model", "timeout": 42}))
    cfg = config.load()
    assert cfg["model"] == "some-other-model"
    assert cfg["timeout"] == 42
    # Untouched keys keep their defaults
    assert cfg["planner_retries"] == config.DEFAULTS["planner_retries"]


def test_corrupt_config_falls_back_to_defaults():
    Path("config.json").write_text("{broken json")
    cfg = config.load()
    assert cfg == config.DEFAULTS


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
