import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_lock import (
    CoordinatorLock,
    CoordinatorLockError,
    validate_single_worker,
)


def test_second_coordinator_is_rejected_until_first_releases(tmp_path):
    first = CoordinatorLock(tmp_path, deployment_mode="trusted_alpha")
    first_identity = first.acquire()

    with pytest.raises(CoordinatorLockError, match="another Mycelium coordinator"):
        CoordinatorLock(tmp_path).acquire()

    first.release()
    metadata = json.loads((tmp_path / ".mycelium-coordinator.lock").read_text())
    assert metadata["instance_id"] == first_identity.instance_id
    assert metadata["deployment_mode"] == "trusted_alpha"

    replacement = CoordinatorLock(tmp_path)
    replacement.acquire()
    replacement.release()


def test_different_state_directories_have_independent_locks(tmp_path):
    first = CoordinatorLock(tmp_path / "one")
    second = CoordinatorLock(tmp_path / "two")

    first.acquire()
    second.acquire()
    second.release()
    first.release()


@pytest.mark.parametrize(
    ("environment", "arguments"),
    [
        ({"WEB_CONCURRENCY": "2"}, []),
        ({"UVICORN_WORKERS": "3"}, []),
        ({}, ["--workers", "2"]),
        ({}, ["--workers=4"]),
        ({"GUNICORN_CMD_ARGS": "--workers 2"}, []),
    ],
)
def test_multiworker_launch_is_rejected(environment, arguments):
    with pytest.raises(CoordinatorLockError, match="multi-worker launch rejected"):
        validate_single_worker(environment, arguments)


def test_single_worker_launch_is_accepted():
    validate_single_worker({"WEB_CONCURRENCY": "1"}, ["--workers=1"])


def test_operating_system_releases_lock_after_process_death(tmp_path):
    code = """
import sys, time
from coordinator_lock import CoordinatorLock
lock = CoordinatorLock(sys.argv[1])
lock.acquire()
print('locked', flush=True)
time.sleep(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(CoordinatorLockError):
            CoordinatorLock(tmp_path).acquire()
    finally:
        process.terminate()
        process.communicate(timeout=10)

    recovered = CoordinatorLock(tmp_path)
    recovered.acquire()
    recovered.release()
