"""Stopping a running pitch.

On a 4B CPU model a pitch is minutes, and there was no way to take one back.

The thing this must not do is kill work mid-flight. A builder subtask is a
single model call; interrupting it throws away CPU someone already spent, and
under attempt binding a reclaimed attempt settles nothing — so the node would
be paid zero for real work. That is the exact failure this project has already
hit once, from the other direction: a node was evicted mid-build and paid +0
credits for a 329-second subtask it went on to finish.

So cancellation stops *dispatch*, not execution: queued work nobody has picked
up is dropped, anything already on a machine finishes and is paid for.
"""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import orchestrator
import server_state
from orchestrator import PipelineCancelled
from server import app

PLAN_JSON = (
    '[{"id": 1, "title": "First", "prompt": "a", "depends_on": []},'
    ' {"id": 2, "title": "Second", "prompt": "b", "depends_on": [1]},'
    ' {"id": 3, "title": "Third", "prompt": "c", "depends_on": [2]}]'
)
REVIEW = "## Quality Rating\nPASS\n\n## Final Assembled Output\n\n```python\nprint('ok')\n```\n"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_state():
    for store in (server_state.jobs, server_state.task_results, server_state.task_inflight):
        store.clear()
    server_state.task_queue.clear()
    yield
    for store in (server_state.jobs, server_state.task_results, server_state.task_inflight):
        store.clear()
    server_state.task_queue.clear()


def _stub(monkeypatch):
    async def fake_generate(prompt, system="", model=None, role=None, format=None):
        if system == orchestrator.PLANNER_SYSTEM:
            return PLAN_JSON
        if system == orchestrator.BUILDER_SYSTEM:
            return "```python\nprint('built')\n```"
        if system == orchestrator.REVIEWER_SYSTEM:
            return REVIEW
        return REVIEW

    async def fake_stream(*a, **k):
        yield "x"

    monkeypatch.setattr(orchestrator, "generate", fake_generate)
    monkeypatch.setattr(orchestrator, "generate_stream", fake_stream)


# ── The pipeline itself ──────────────────────────────────────────────

def test_a_run_stops_between_waves_not_inside_one(tmp_path, monkeypatch):
    """The subtasks here are a dependency chain, so each wave is one subtask.
    Cancelling after the first must leave subtask 1 finished and 2 and 3
    never dispatched."""
    _stub(monkeypatch)
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")

    built = []
    real_build = orchestrator.build

    async def counting_build(st, context="", on_token=None, task=""):
        built.append(st["id"])
        return await real_build(st, context, on_token=on_token, task=task)

    monkeypatch.setattr(orchestrator, "build", counting_build)

    with pytest.raises(PipelineCancelled) as exc:
        asyncio.run(orchestrator.run_pipeline(
            "build a thing", should_cancel=lambda: len(built) >= 1))

    assert built == [1], f"dispatch continued after the stop: {built}"
    assert exc.value.stage == "building"
    assert [c["id"] for c in exc.value.completed] == [1]


def test_a_stopped_run_reports_what_it_finished_and_paid(tmp_path, monkeypatch):
    """"Cancelled" with nothing beside it reads as work thrown away."""
    _stub(monkeypatch)
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")

    built = []
    real_build = orchestrator.build

    async def counting_build(st, context="", on_token=None, task=""):
        built.append(st["id"])
        return await real_build(st, context, on_token=on_token, task=task)

    monkeypatch.setattr(orchestrator, "build", counting_build)

    with pytest.raises(PipelineCancelled) as exc:
        asyncio.run(orchestrator.run_pipeline(
            "build a thing", should_cancel=lambda: len(built) >= 2))

    c = exc.value
    assert len(c.completed) == 2
    assert all(s["executor"] for s in c.completed), "no machine named for finished work"
    assert all(s["seconds"] >= 0 for s in c.completed)
    # 1 for the pitch + 5 per completed subtask. The reviewer never ran.
    assert sum(x["credits"] for x in c.credits) == 1 + 5 + 5
    assert not any(x["type"] == "review" for x in c.credits)


def test_completed_work_is_still_on_the_ledger_after_a_stop(tmp_path, monkeypatch):
    """Nothing already earned is reversed by stopping."""
    _stub(monkeypatch)
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")
    import ledger

    built = []
    real_build = orchestrator.build

    async def counting_build(st, context="", on_token=None, task=""):
        built.append(st["id"])
        return await real_build(st, context, on_token=on_token, task=task)

    monkeypatch.setattr(orchestrator, "build", counting_build)
    with pytest.raises(PipelineCancelled):
        asyncio.run(orchestrator.run_pipeline(
            "build a thing", should_cancel=lambda: len(built) >= 1))

    entries = ledger.get_history()
    assert any(e["type"] == "compute" for e in entries), "the finished build was not paid"


def test_a_run_nobody_stops_is_unaffected(tmp_path, monkeypatch):
    """should_cancel that always returns False must change nothing."""
    _stub(monkeypatch)
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")
    result = asyncio.run(orchestrator.run_pipeline("build a thing", should_cancel=lambda: False))
    assert result["rating"] == "PASS"
    assert len(result["plan"]) == 3


def test_stopping_before_the_review_skips_it(tmp_path, monkeypatch):
    """Reviewing a run somebody just stopped spends another model call on a
    deliverable nobody is waiting for."""
    _stub(monkeypatch)
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")

    reviews = []
    real_review = orchestrator.review

    async def counting_review(*a, **k):
        reviews.append(1)
        return await real_review(*a, **k)

    monkeypatch.setattr(orchestrator, "review", counting_review)

    done_building = {"yes": False}
    real_build = orchestrator.build

    async def flagging_build(st, context="", on_token=None, task=""):
        out = await real_build(st, context, on_token=on_token, task=task)
        if st["id"] == 3:
            done_building["yes"] = True
        return out

    monkeypatch.setattr(orchestrator, "build", flagging_build)

    with pytest.raises(PipelineCancelled) as exc:
        asyncio.run(orchestrator.run_pipeline(
            "build a thing", should_cancel=lambda: done_building["yes"]))
    assert exc.value.stage == "review"
    assert not reviews, "the reviewer ran on a cancelled run"


# ── The endpoint ─────────────────────────────────────────────────────

def _queued_job(job_id="job_test", status="running"):
    server_state.jobs[job_id] = {
        "job_id": job_id, "task": "build a thing", "status": status,
        "submitted_at": "2026-08-16T00:00:00+00:00", "result": None,
        "error": None, "trace_id": "t", "cancel_requested": False,
    }
    return job_id


def test_cancelling_drops_queued_work_but_not_running_work(client):
    job_id = _queued_job()
    server_state.task_queue.extend([
        {"task_id": "a", "job_id": job_id, "title": "one"},
        {"task_id": "b", "job_id": job_id, "title": "two"},
        {"task_id": "c", "job_id": "other_job", "title": "not ours"},
    ])
    server_state.task_inflight["d"] = {"task_id": "d", "job_id": job_id,
                                       "assigned_to": "laptop"}

    body = client.post(f"/jobs/{job_id}/cancel").json()

    assert body["status"] == "cancelling"
    assert body["dropped_from_queue"] == 2
    assert body["still_running"] == 1
    assert [t["task_id"] for t in server_state.task_queue] == ["c"], "another job's work was dropped"
    assert "d" in server_state.task_inflight, "work already on a machine was cancelled out from under it"
    assert "will finish and be paid" in body["detail"]


def test_the_stop_flag_is_what_the_pipeline_reads(client):
    job_id = _queued_job()
    client.post(f"/jobs/{job_id}/cancel")
    assert server_state.jobs[job_id]["cancel_requested"] is True
    assert server_state.jobs[job_id]["status"] == "cancelling"


def test_cancelling_a_finished_job_is_a_clear_409(client):
    for status in ("complete", "failed", "cancelled"):
        job_id = _queued_job(f"job_{status}", status=status)
        r = client.post(f"/jobs/{job_id}/cancel")
        assert r.status_code == 409
        assert status in r.json()["detail"]


def test_cancelling_an_unknown_job_is_a_404(client):
    assert client.post("/jobs/job_nope/cancel").status_code == 404


def test_the_status_endpoint_reports_a_stop(client):
    job_id = _queued_job()
    server_state.jobs[job_id].update({
        "status": "cancelled",
        "cancelled_during": "building",
        "completed_subtasks": [{"id": 1, "title": "First", "executor": "laptop"}],
        "credits_settled": [{"credits": 1}, {"credits": 5}],
    })
    body = client.get(f"/jobs/{job_id}").json()
    assert body["status"] == "cancelled"
    assert body["cancelled_during"] == "building"
    assert len(body["completed_subtasks"]) == 1
    assert body["credits_settled"] == 6


def test_a_cancelled_job_is_eventually_pruned():
    """Without this a stopped job sits in memory forever — the janitor only
    knew about complete and failed."""
    src = (Path(__file__).resolve().parent.parent / "server_state.py").read_text(encoding="utf-8")
    assert '("complete", "failed", "cancelled")' in src


def test_the_client_offers_a_stop_and_explains_what_it_does():
    js = (Path(__file__).resolve().parent.parent / "templates" / "_dashboard.js").read_text(encoding="utf-8")
    assert "function cancelJob(" in js
    assert "/cancel" in js
    assert "'cancelled'" in js, "the poller does not recognise a stopped job"
    assert "cancel-note" in js, "a stopped run does not report what it finished"


def test_a_cancelled_run_leaves_no_orphaned_inflight_task(client):
    """The inflight entry stays on purpose — but the janitor must still be
    able to reclaim or expire it, so it is a normal entry, not a special one."""
    job_id = _queued_job()
    server_state.task_inflight["d"] = {
        "task_id": "d", "job_id": job_id, "assigned_to": "laptop",
        "assigned_at": 0, "title": "one", "prompt": "p",
    }
    client.post(f"/jobs/{job_id}/cancel")

    server_state.nodes.clear()          # the node disappears mid-subtask
    server_state._cleanup_pass()
    assert "d" not in server_state.task_inflight or server_state.task_inflight["d"]
    # Either reclaimed to the queue or still held — never silently lost.
    assert ("d" in server_state.task_inflight
            or any(t["task_id"] == "d" for t in server_state.task_queue))


def test_the_run_log_of_a_stopped_run_is_not_written(tmp_path, monkeypatch):
    """A stopped run has no assembled deliverable, so it must not leave a
    half-written run directory that the gallery would then show."""
    _stub(monkeypatch)
    out = tmp_path / "output"
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", out)

    built = []
    real_build = orchestrator.build

    async def counting_build(st, context="", on_token=None, task=""):
        built.append(st["id"])
        return await real_build(st, context, on_token=on_token, task=task)

    monkeypatch.setattr(orchestrator, "build", counting_build)
    with pytest.raises(PipelineCancelled):
        asyncio.run(orchestrator.run_pipeline(
            "build a thing", should_cancel=lambda: len(built) >= 1))

    logs = list(out.rglob("full_log.json")) if out.exists() else []
    assert not logs, f"a stopped run wrote {[p.parent.name for p in logs]}"
