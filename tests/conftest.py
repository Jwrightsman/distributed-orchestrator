"""Shared fixtures.

Every test runs in an isolated temp directory (the modules resolve their data
files — ledger.json, config.json, projects/ — relative to CWD), with the
module-level caches reset so tests can't leak state into each other.
No Ollama: anything that would call generate() gets a mock.
"""

import json
import sys
from pathlib import Path

import pytest

# Make the repo root importable when pytest is run from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import ledger  # noqa: E402
import execution.artifacts as artifact_module  # noqa: E402
import execution.service as service_module  # noqa: E402
import execution.sharing as sharing_module  # noqa: E402


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
    artifact_module._ARTIFACT_STORE = None
    sharing_module._SHARE_STORE = None
    service_module._SERVICE = None
    yield
    if hasattr(config.get, "_cache"):
        del config.get._cache
    ledger._cache = None
    ledger._cache_mtime = 0.0
    artifact_module._ARTIFACT_STORE = None
    sharing_module._SHARE_STORE = None
    service_module._SERVICE = None


# ── Parse-precheck budget ────────────────────────────────────────────────
# The extract → verify stage runs its parser in a subprocess bounded by
# `validator_subprocess_timeout_seconds` (config DEFAULTS: 10). That budget is
# right for production and wrong as the basis of a test assertion. The work is
# dominated by spawning an interpreter and fsyncing a staging tree, so its cost
# tracks what the disk is doing rather than how fast the machine is: measured
# here it is ~0.29s idle and >1s under a fsync storm, and fsync latency has no
# ceiling. When the subprocess overruns, `check_code_files_isolated` reports the
# overrun as the problem string "validator_timeout" in the same list as real
# parse defects — so a test that reads `problems` cannot tell "the validator
# never ran" from "the extracted code is broken", and blames the code.
#
# 120s is the configuration maximum — ~400x the idle cost and ~100x the cost
# under that storm. Missing it means something is genuinely wrong with the
# runner, not that CI was busy.
PARSE_VALIDATOR_TEST_TIMEOUT_SECONDS = 120

# The only counters a run that reached a verdict is allowed to move. Anything
# else advancing means the subprocess never returned one, so whatever landed in
# `problems` describes the runner and not the code under test. Written as an
# allowlist so a failure mode added to the runner later is covered here without
# anyone having to remember to come back.
_COUNTERS_A_VERDICT_MAY_MOVE = frozenset(
    {"reset_at", "subprocess_runs", "successful_responses", "validation_failures"}
)


class ParseValidator:
    """The parse precheck on a budget CI cannot miss, plus the audit that says
    whether it actually reached a verdict.

    `assert_reached_a_verdict()` is what stops a timeout being scored as a
    parse defect. It reads the runner's own counters rather than pattern
    matching the problem strings, so "the validator never ran" and "the code is
    broken" stay distinguishable however the runner fails.
    """

    def __init__(self):
        self._baseline = self._counters()

    @staticmethod
    def _counters() -> dict:
        from execution.validator_process import ValidatorProcessExecutor

        # The runner keeps one process-wide counter store, so any executor
        # reports the same numbers — including the ones the code under test
        # built for itself.
        return dict(
            ValidatorProcessExecutor().diagnostics()["process_local_counters"]
        )

    def assert_reached_a_verdict(self) -> None:
        moved = {
            name: (self._baseline.get(name, 0), value)
            for name, value in self._counters().items()
            if name not in _COUNTERS_A_VERDICT_MAY_MOVE
            and value != self._baseline.get(name, 0)
        }
        assert not moved, (
            "the parse validator never returned a verdict, so the reported "
            "problems describe the runner and not the code under test "
            f"(counters before/after: {moved})"
        )


@pytest.fixture
def parse_validator(tmp_path):
    """Give the parse subprocess a budget no CI machine can miss, and hand back
    the audit for whether it met it.

    The budget is pinned through configuration rather than injected, because
    the exposed callers reach the parser by more than one route — directly, and
    down through `run_pipeline` — and a pinned budget covers both without every
    caller having to thread an executor down to it.
    """

    settings = tmp_path / "config.json"
    assert not settings.exists(), (
        "this fixture pins the parse budget by writing the isolated working "
        "directory's config.json; the test already wrote one"
    )
    settings.write_text(
        json.dumps(
            {
                "validator_subprocess_timeout_seconds": (
                    PARSE_VALIDATOR_TEST_TIMEOUT_SECONDS
                )
            }
        ),
        encoding="utf-8",
    )
    if hasattr(config.get, "_cache"):
        del config.get._cache

    # Fail here rather than leaving the budget silently back at its default and
    # the flake this fixture exists to remove silently back with it.
    assert (
        config.get()["validator_subprocess_timeout_seconds"]
        == PARSE_VALIDATOR_TEST_TIMEOUT_SECONDS
    ), "the pinned parse budget did not reach the loaded configuration"

    return ParseValidator()
