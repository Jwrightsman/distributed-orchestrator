"""Shared fixtures.

Every test runs in an isolated temp directory (the modules resolve their data
files — ledger.json, config.json, projects/ — relative to CWD), with the
module-level caches reset so tests can't leak state into each other.
No Ollama: anything that would call generate() gets a mock.
"""

import sys
from pathlib import Path

import pytest

# Make the repo root importable when pytest is run from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import ledger  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Run each test in its own empty directory with fresh module caches."""
    monkeypatch.chdir(tmp_path)
    # ledger caches parsed JSON keyed on mtime — reset between tests
    ledger._cache = None
    ledger._cache_mtime = 0.0
    # config.get caches the loaded dict on the function object
    if hasattr(config.get, "_cache"):
        del config.get._cache
    yield
    if hasattr(config.get, "_cache"):
        del config.get._cache
    ledger._cache = None
    ledger._cache_mtime = 0.0
