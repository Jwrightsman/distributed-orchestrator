"""Validators run when the coordinator itself runs from a Windows venv.

A Windows venv's ``Scripts\\python.exe`` is a redirector that starts the base
interpreter as a second process.  The validator Job Object admits exactly one
process, so launching the redirector made every validator return
``validator_crash`` (62 tests in a venv-hosted suite).  The launch now starts
the base interpreter directly and names the venv through
``__PYVENV_LAUNCHER__``; the Job Object limit is unchanged.

CI has no Windows job, so the end-to-end test below only runs on Windows
machines.  The parametrized test runs everywhere and pins the part that must
never drift: the command and the environment agree about which interpreter is
starting and in which venv.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import execution.validator_process as validator_process

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("os_name", "in_venv", "same_file", "redirected"),
    [
        ("nt", True, False, True),
        ("nt", False, False, False),
        ("nt", True, True, False),
        ("posix", True, False, False),
    ],
    ids=["windows-venv", "windows-no-venv", "windows-venv-real-copy", "posix-venv"],
)
def test_the_launch_command_and_environment_agree(
    tmp_path, monkeypatch, os_name, in_venv, same_file, redirected
):
    base = tmp_path / "base" / "python.exe"
    base.parent.mkdir()
    base.write_bytes(b"")
    launcher = base if same_file else tmp_path / "venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(validator_process.os, "name", os_name)
    monkeypatch.setattr(sys, "executable", str(launcher))
    monkeypatch.setattr(sys, "_base_executable", str(base), raising=False)
    monkeypatch.setattr(sys, "base_prefix", str(base.parent))
    monkeypatch.setattr(
        sys, "prefix", str(launcher.parent.parent) if in_venv else str(base.parent)
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "must-not-leak"))

    # Only the two decisions run under the patched platform: pathlib refuses
    # to build a path for a platform other than the real one.
    interpreter = validator_process.validator_python_executable()
    environment = validator_process._sanitized_environment(tmp_path)
    monkeypatch.undo()

    assert "PYTHONPATH" not in environment
    if redirected:
        assert interpreter == str(base)
        assert environment["__PYVENV_LAUNCHER__"] == str(launcher)
    else:
        assert interpreter == str(launcher)
        assert "__PYVENV_LAUNCHER__" not in environment


def test_the_production_command_uses_that_interpreter_and_stays_isolated(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        validator_process, "validator_python_executable", lambda: "chosen-python"
    )
    command = validator_process.ValidatorProcessExecutor._default_command(tmp_path)
    assert command[0] == "chosen-python"
    assert command[1:3] == ("-I", "-B"), "the child must ignore PYTHON* variables"


_PROBE_CHILD = r"""
import json
import os
import subprocess
import sys

request = json.load(sys.stdin)
try:
    import mycelium_venv_marker  # present only in the venv's site-packages
    marker = True
except ImportError:
    marker = False
try:
    subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).wait(timeout=30)
    spawn_blocked = False
except OSError:
    spawn_blocked = True
with open(OBSERVATION, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "prefix": sys.prefix,
            "marker": marker,
            "environment": sorted(os.environ),
            "spawn_blocked": spawn_blocked,
        },
        handle,
    )
json.dump(
    {
        "protocol_version": request["protocol_version"],
        "validator_name": request["validator_name"],
        "validator_version": request["validator_version"],
        "ok": True,
        "score": 1.0,
        "detail": {"json_type": "object"},
        "failure_reason": None,
    },
    sys.stdout,
)
"""

_VENV_PARENT = r"""
import json
import sys
from pathlib import Path

sys.path.insert(0, REPOSITORY_ROOT)
from execution.validator_process import (
    ValidatorProcessExecutor,
    ValidatorProcessSettings,
    _sanitized_environment,
    validator_python_executable,
)

settings = ValidatorProcessSettings(execution_mode="subprocess", timeout_seconds=120)


def run(executor):
    outcome = executor.execute(
        validator_name="structured_json",
        validator_version="2",
        output="{}",
        files=[],
        contract=None,
        artifact_root=None,
        max_output_bytes=1024,
    )
    return {
        "completed": outcome.completed,
        "ok": outcome.ok,
        "failure_reason": outcome.failure_reason,
    }


report = {
    "parent_executable": sys.executable,
    "parent_prefix": sys.prefix,
    "launch_executable": validator_python_executable(),
    "allowlisted": sorted(_sanitized_environment(Path(PROBE).parent)),
    "production": run(ValidatorProcessExecutor(settings)),
    "probe": run(
        ValidatorProcessExecutor(
            settings,
            command_factory=lambda _work: (validator_python_executable(), "-I", PROBE),
        )
    ),
}
print("REPORT " + json.dumps(report))
"""


@pytest.mark.skipif(os.name != "nt", reason="Windows venv redirector and Job Object")
def test_validators_run_from_a_windows_venv_inside_a_one_process_job(tmp_path):
    venv = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    site_packages = venv / "Lib" / "site-packages"
    # The venv is isolated from the base install, like a normal venv.  It
    # reaches this suite's dependencies through its own site-packages, so the
    # production runner can only import pydantic if the child really is
    # running with the venv's configuration.
    reachable = [entry for entry in sys.path if entry and Path(entry).is_dir()]
    (site_packages / "mycelium_test_dependencies.pth").write_text(
        "\n".join(reachable) + "\n", encoding="utf-8"
    )
    (site_packages / "mycelium_venv_marker.py").write_text("", encoding="utf-8")

    observation = tmp_path / "observation.json"
    probe = tmp_path / "probe_child.py"
    probe.write_text(
        f"OBSERVATION = {str(observation)!r}\n" + _PROBE_CHILD, encoding="utf-8"
    )
    parent = tmp_path / "venv_parent.py"
    parent.write_text(
        f"REPOSITORY_ROOT = {str(REPOSITORY_ROOT)!r}\nPROBE = {str(probe)!r}\n"
        + _VENV_PARENT,
        encoding="utf-8",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"PYTHONPATH", "PYTHONHOME", "__PYVENV_LAUNCHER__"}
    }

    completed = subprocess.run(
        [str(venv / "Scripts" / "python.exe"), str(parent)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    report_line = next(
        line for line in completed.stdout.splitlines() if line.startswith("REPORT ")
    )
    report = json.loads(report_line.removeprefix("REPORT "))

    assert report["production"] == {
        "completed": True,
        "ok": True,
        "failure_reason": None,
    }, report
    assert report["probe"]["completed"] is True, report
    # Not vacuous: the parent really is behind the redirector.
    assert Path(report["parent_prefix"]).resolve() == venv.resolve()
    assert Path(report["launch_executable"]).resolve() != Path(
        report["parent_executable"]
    ).resolve()
    assert "__PYVENV_LAUNCHER__" in report["allowlisted"]

    seen = json.loads(observation.read_text(encoding="utf-8"))
    assert Path(seen["prefix"]).resolve() == venv.resolve()
    assert seen["marker"] is True, "the child did not get the venv's site-packages"
    # The interpreter consumes the launcher variable at startup; the child sees
    # nothing beyond the parent's allowlist.
    assert set(seen["environment"]) <= set(report["allowlisted"]) - {
        "__PYVENV_LAUNCHER__"
    }
    assert seen["spawn_blocked"] is True, (
        "the validator could start a second process; the one-process Job Object "
        "limit is no longer in force"
    )
