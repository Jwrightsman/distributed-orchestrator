"""What a run writes down about itself.

/run/{id} shows which machine built each subtask, how long it took, what the
reviser did, and what the ledger settled. None of that was recorded anywhere
before — it is written into full_log.json as the run finishes.

Recording it at the time rather than reconstructing it later is the whole
point: the ledger has no run id, so joining credits back to a run means
matching by timestamp window, which is wrong the moment two pipelines
overlap. These tests pin the record so a page built on it cannot quietly
start showing "not recorded" for every new run.
"""

import asyncio
import json
from pathlib import Path

import pytest

import orchestrator

PLAN_JSON = (
    '[{"id": 1, "title": "First", "prompt": "do the first thing", "depends_on": []},'
    ' {"id": 2, "title": "Second", "prompt": "do the second thing", "depends_on": [1]}]'
)
REVIEW_PASS = (
    "## Quality Rating\nPASS\n\n"
    "## Final Assembled Output\n\n```python\n" + "print('ok')\n" * 30 + "```\n"
)
REVIEW_NEEDS_WORK = (
    "## Quality Rating\nNEEDS_WORK\n\n"
    "## Issues Found\nThe scoring logic is missing.\n\n"
    "## Final Assembled Output\n\n```python\n" + "print('placeholder')\n" * 40 + "```\n"
)


def _stub(monkeypatch, review, revised=None):
    async def fake_generate(prompt, system="", model=None, role=None, format=None):
        if system == orchestrator.PLANNER_SYSTEM:
            return PLAN_JSON
        if system == orchestrator.BUILDER_SYSTEM:
            return "```python\nprint('built')\n```"
        if system == orchestrator.REVIEWER_SYSTEM:
            return review
        if system == orchestrator.REVISER_SYSTEM:
            return revised if revised is not None else review
        return ""

    async def fake_stream(*a, **k):
        yield "print('built')"

    monkeypatch.setattr(orchestrator, "generate", fake_generate)
    monkeypatch.setattr(orchestrator, "generate_stream", fake_stream)


def _run(tmp_path, monkeypatch, review, revised=None) -> dict:
    _stub(monkeypatch, review, revised)
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")
    result = asyncio.run(orchestrator.run_pipeline("build a thing"))
    log = json.loads((Path(result["project_dir"]) / "full_log.json").read_text(encoding="utf-8"))
    return log


def test_the_log_records_who_built_each_subtask_and_how_long(tmp_path, monkeypatch):
    log = _run(tmp_path, monkeypatch, REVIEW_PASS)

    stats = log["subtask_stats"]
    assert set(stats) == {"1", "2"}, "not every subtask was recorded"
    for meta in stats.values():
        assert meta["executor"], "no machine recorded for a subtask"
        assert meta["seconds"] >= 0
        assert meta["chars"] > 0
    assert log["review_seconds"] >= 0
    assert log["duration_seconds"] >= 0
    assert log["started_at"].endswith("+00:00"), "started_at is not an absolute UTC time"


def test_a_local_run_names_this_machine_rather_than_leaving_it_blank(tmp_path, monkeypatch):
    import platform
    log = _run(tmp_path, monkeypatch, REVIEW_PASS)
    assert all(m["executor"] == platform.node() for m in log["subtask_stats"].values())


def test_a_dispatched_run_leaves_the_executor_for_the_dispatcher(tmp_path, monkeypatch):
    """run_pipeline cannot know where a caller-supplied build_fn sent the work;
    only the dispatcher can fill that in, so it must not guess."""
    _stub(monkeypatch, REVIEW_PASS)
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")

    async def elsewhere(st, context):
        return "```python\nprint('remote')\n```"

    result = asyncio.run(orchestrator.run_pipeline("build a thing", build_fn=elsewhere))
    log = json.loads((Path(result["project_dir"]) / "full_log.json").read_text(encoding="utf-8"))
    assert all(m["executor"] is None for m in log["subtask_stats"].values())


def test_credits_are_itemised_against_the_run(tmp_path, monkeypatch):
    """ledger.json has no run id — attributing by timestamp window is wrong as
    soon as two pipelines overlap, so the run records its own settlement."""
    log = _run(tmp_path, monkeypatch, REVIEW_PASS)

    kinds = [c["type"] for c in log["credits"]]
    assert kinds.count("pitch") == 1
    assert kinds.count("compute") == 2, "one per subtask"
    assert kinds.count("review") == 1
    assert sum(c["credits"] for c in log["credits"]) == 1 + 5 + 5 + 3
    assert all(c["for"] for c in log["credits"]), "a credit with no reason is not an explanation"


def test_a_reviser_that_never_needed_to_run_says_why(tmp_path, monkeypatch):
    log = _run(tmp_path, monkeypatch, REVIEW_PASS)
    rev = log["revision"]
    assert rev["fired"] is False and rev["passes"] == 0
    assert rev["stopped_because"], "a reviser that did not fire must say why not"
    assert rev["rating_after"] == "PASS"


def test_a_reviser_that_gave_up_is_recorded_as_such(tmp_path, monkeypatch):
    """The interesting third case: it fired, spent both passes, and the rating
    did not move. A boolean would lose exactly this."""
    log = _run(tmp_path, monkeypatch, REVIEW_NEEDS_WORK)
    rev = log["revision"]
    assert rev["fired"] is True
    assert rev["passes"] == orchestrator._MAX_REVISIONS
    assert rev["cleared_the_rating"] is False
    assert "limit" in rev["stopped_because"]
    assert rev["rating_before"] == "NEEDS_WORK"


def test_a_reviser_that_fixed_it_flips_the_rating(tmp_path, monkeypatch):
    fixed = "## Final Assembled Output\n\n```python\n" + "print('fixed')\n" * 60 + "```\n"
    log = _run(tmp_path, monkeypatch, REVIEW_NEEDS_WORK, revised=fixed)
    rev = log["revision"]
    assert rev["fired"] is True and rev["passes"] == 1
    assert rev["cleared_the_rating"] is True
    assert rev["rating_before"] == "NEEDS_WORK" and rev["rating_after"] == "PASS"
    assert log["rating"] == "PASS"
    assert rev["chars_after"] != rev["chars_before"], "no change was recorded"


def test_the_run_page_renders_a_freshly_recorded_run(tmp_path, monkeypatch):
    """The whole chain: pipeline writes the record, the page reads it back and
    shows real numbers instead of 'not recorded'."""
    from fastapi.testclient import TestClient
    from server import app

    log = _run(tmp_path, monkeypatch, REVIEW_PASS)
    monkeypatch.setattr("server_state.OUTPUT_DIR", tmp_path / "output")

    with TestClient(app) as c:
        body = c.get(f"/run/{log['timestamp']}").text
    assert "not recorded" not in body.lower(), "a fresh run should have everything"
    assert "not itemised" not in body.lower()
    for title in ("First", "Second"):
        assert title in body
    assert ">14<" in body, "the settlement total is missing"


@pytest.mark.parametrize("field", ["subtask_stats", "revision", "credits",
                                   "duration_seconds", "review_seconds", "model"])
def test_the_distributed_path_writes_the_same_shape(field):
    """Two writers, one shape — /run/{id} reads one record regardless of how
    the work was executed. Checked in the source because exercising the
    distributed path needs connected nodes."""
    src = Path(__file__).resolve().parent.parent / "routes_pitch.py"
    text = src.read_text(encoding="utf-8")
    log_block = text[text.index('"mode": "distributed",'):]
    log_block = log_block[:log_block.index("(project_dir /")]
    assert f'"{field}"' in log_block, f"the distributed run log omits {field}"
