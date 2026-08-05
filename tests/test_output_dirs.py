"""Run directories must not collide.

Output directories are named to the second. Two pitches finishing inside the
same second raised FileExistsError and killed one of the runs — reachable
whenever jobs overlap through /pitch/async, and it took down 27 of 28 prompts
the first time the eval harness ran.
"""

from datetime import datetime, timezone

import orchestrator


def test_second_run_in_same_second_gets_its_own_dir(tmp_path):
    ts1, dir1 = orchestrator.make_run_dir(tmp_path)
    ts2, dir2 = orchestrator.make_run_dir(tmp_path)

    assert dir1 != dir2
    assert dir1.is_dir() and dir2.is_dir()
    assert ts1 != ts2


def test_many_rapid_runs_all_succeed(tmp_path):
    made = [orchestrator.make_run_dir(tmp_path) for _ in range(10)]

    timestamps = [ts for ts, _ in made]
    dirs = [d for _, d in made]
    assert len(set(timestamps)) == 10
    assert len(set(dirs)) == 10
    assert all(d.is_dir() for d in dirs)


def test_timestamps_stay_strictly_parseable(tmp_path):
    """History views parse these names with a strict format — keep it valid."""
    for _ in range(5):
        ts, _ = orchestrator.make_run_dir(tmp_path)
        datetime.strptime(ts, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)


def test_creates_parent_directory_when_missing(tmp_path):
    target = tmp_path / "not_yet"
    ts, run_dir = orchestrator.make_run_dir(target)

    assert run_dir.is_dir()
    assert run_dir.parent == target
