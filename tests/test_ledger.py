"""Tests for the contribution ledger (runs against a temp CWD — see conftest)."""

import json
from pathlib import Path

from ledger import get_history, get_standings, log_contribution


def test_log_creates_ledger_file():
    log_contribution("node-a", "compute", credits=5, task="build")
    data = json.loads(Path("ledger.json").read_text())
    assert len(data) == 1
    assert data[0]["contributor"] == "node-a"
    assert data[0]["credits"] == 5


def test_standings_aggregates_credits():
    log_contribution("node-a", "compute", credits=5)
    log_contribution("node-a", "compute", credits=5)
    log_contribution("node-b", "pitch", credits=1)
    standings = get_standings()
    assert standings[0]["contributor"] == "node-a"
    assert standings[0]["total_credits"] == 10
    assert standings[0]["compute_tasks"] == 2
    assert standings[1]["contributor"] == "node-b"
    assert standings[1]["pitches"] == 1


def test_standings_sorted_by_credits_desc():
    log_contribution("small", "compute", credits=1)
    log_contribution("big", "compute", credits=100)
    standings = get_standings()
    assert [s["contributor"] for s in standings] == ["big", "small"]


def test_standings_empty_ledger():
    assert get_standings() == []


def test_history_filter_and_limit():
    for i in range(5):
        log_contribution("node-a", "compute", credits=i)
    log_contribution("node-b", "pitch", credits=1)
    assert len(get_history("node-a")) == 5
    assert len(get_history("node-a", limit=2)) == 2
    assert all(e["contributor"] == "node-b" for e in get_history("node-b"))


def test_corrupt_ledger_treated_as_empty():
    Path("ledger.json").write_text("{not valid json")
    assert get_standings() == []
    # And logging still works afterwards
    log_contribution("node-a", "compute", credits=5)
    assert len(get_standings()) == 1
