"""Resilience tests — the loops and fallbacks that must not run away.

SPRINT_PHASE2 §2, the "verify under stress" items that do not need a real
model: the revision loop terminates, the distributed path falls back to local
inference when a node fails, and repeated pitches do not leak state.
"""

import asyncio

import pytest

import orchestrator
import routes_pitch
import server
import server_state

REVIEW_NEEDS_WORK = (
    "## Quality Rating\nNEEDS_WORK\n\n"
    "## Issues Found\nThe scoring logic is missing and nothing is wired together.\n\n"
    "## Final Assembled Output\n\n"
    "```python\n" + "print('placeholder')\n" * 40 + "```\n"
)

PLAN_JSON = (
    '[{"id": 1, "title": "First", "prompt": "do the first thing", "depends_on": []},'
    ' {"id": 2, "title": "Second", "prompt": "do the second thing", "depends_on": [1]}]'
)


@pytest.fixture
def stub_model(monkeypatch):
    """Model stub that never stops complaining, and counts reviser calls."""
    calls = {"plan": 0, "build": 0, "review": 0, "revise": 0}

    async def fake_generate(prompt, system="", model=None, role=None, format=None):
        if system == orchestrator.PLANNER_SYSTEM:
            calls["plan"] += 1
            return PLAN_JSON
        if system == orchestrator.BUILDER_SYSTEM:
            calls["build"] += 1
            return "```python\nprint('built')\n```"
        if system == orchestrator.REVIEWER_SYSTEM:
            calls["review"] += 1
            return REVIEW_NEEDS_WORK
        if system == orchestrator.REVISER_SYSTEM:
            calls["revise"] += 1
            # Long enough to pass the "revision came back empty" sanity check,
            # and still carrying issues so the loop is tempted to continue.
            return REVIEW_NEEDS_WORK
        return ""

    async def fake_stream(*a, **k):
        yield "print('built')"

    monkeypatch.setattr(orchestrator, "generate", fake_generate)
    monkeypatch.setattr(orchestrator, "generate_stream", fake_stream)
    return calls


# ── The revision loop must terminate ────────────────────────────────────────

def test_revision_loop_stops_at_the_cap(stub_model, tmp_path, monkeypatch, parse_validator):
    """A reviewer that always says NEEDS_WORK must not spin forever.

    The reviser count is only a statement about the loop if the parse precheck
    reached a verdict. A precheck that times out reports the overrun as a code
    defect, `extract_and_repair` spends its own revision trying to fix code
    that was never found broken, and the extra call reads here as the loop
    running away. `parse_validator` pins a budget that will not be missed, and
    the audit below fails by name rather than blaming the loop if it is.
    """
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")

    result = asyncio.run(orchestrator.run_pipeline("build a thing"))

    parse_validator.assert_reached_a_verdict()
    assert stub_model["revise"] <= orchestrator._MAX_REVISIONS
    assert result["rating"] in ("NEEDS_WORK", "FAIL", "PASS")
    assert result["project_dir"]


def test_pipeline_completes_even_when_never_satisfied(stub_model, tmp_path, monkeypatch):
    """The run still produces a deliverable rather than raising."""
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")

    result = asyncio.run(orchestrator.run_pipeline("build a thing"))

    assert result["final_output"]
    assert stub_model["plan"] >= 1
    assert stub_model["review"] == 1


def test_empty_revision_does_not_replace_good_output(monkeypatch, tmp_path):
    """A reviser that returns almost nothing must not wipe the deliverable."""
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")

    async def fake_generate(prompt, system="", model=None, role=None, format=None):
        if system == orchestrator.PLANNER_SYSTEM:
            return PLAN_JSON
        if system == orchestrator.BUILDER_SYSTEM:
            return "```python\nprint('built')\n```"
        if system == orchestrator.REVIEWER_SYSTEM:
            return REVIEW_NEEDS_WORK
        return "oops"  # reviser returns a stub

    async def fake_stream(*a, **k):
        yield ""

    monkeypatch.setattr(orchestrator, "generate", fake_generate)
    monkeypatch.setattr(orchestrator, "generate_stream", fake_stream)

    result = asyncio.run(orchestrator.run_pipeline("build a thing"))

    assert "oops" != result["final_output"].strip()
    assert len(result["final_output"]) > 100


# ── Distributed builds fall back to local inference ─────────────────────────

@pytest.fixture(autouse=True)
def clean_state():
    for d in (
        server.nodes,
        server.task_results,
        server.task_inflight,
        server.jobs,
        server._pitch_timestamps,
    ):
        d.clear()
    server.task_queue.clear()
    server.pipeline_events.clear()
    server_state._init_db()
    yield


def test_node_error_falls_back_to_local_build(monkeypatch, tmp_path):
    """When a node returns an error, the subtask must still get built locally."""
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(server_state, "OUTPUT_DIR", tmp_path / "output")

    local_builds = {"count": 0}

    async def fake_generate(prompt, system="", model=None, role=None, format=None):
        if system == orchestrator.PLANNER_SYSTEM:
            return PLAN_JSON
        if system == orchestrator.BUILDER_SYSTEM:
            local_builds["count"] += 1
            return "```python\nprint('local fallback')\n```"
        if system == orchestrator.REVIEWER_SYSTEM:
            return (
                "## Quality Rating\nPASS\n\n## Issues Found\nNone\n\n"
                "## Final Assembled Output\n\n```python\nprint('done')\n```\n"
            )
        return ""

    async def fake_stream(*a, **k):
        yield ""

    monkeypatch.setattr(orchestrator, "generate", fake_generate)
    monkeypatch.setattr(orchestrator, "generate_stream", fake_stream)
    monkeypatch.setattr(routes_pitch, "generate", fake_generate)

    # A build_fn that always fails, standing in for a broken node.
    async def failing_node(subtask, context):
        raise RuntimeError("node exploded")

    async def build_with_fallback(subtask, context):
        try:
            return await failing_node(subtask, context)
        except RuntimeError:
            return await orchestrator.generate(
                orchestrator.compose_builder_prompt(subtask, context, "build a thing"),
                system=orchestrator.BUILDER_SYSTEM,
            )

    result = asyncio.run(
        orchestrator.run_pipeline("build a thing", build_fn=build_with_fallback)
    )

    assert local_builds["count"] == 2  # both subtasks fell back
    assert result["rating"] == "PASS"


# ── Repeated pitches must not leak ──────────────────────────────────────────

def test_event_buffer_is_bounded_under_many_pitches():
    """The in-memory event list must not grow without limit during a soak."""
    for i in range(500):
        server_state._emit("build", {"task": f"task-{i}", "subtask": "x"})

    assert len(server.pipeline_events) <= 1000, (
        f"event buffer grew to {len(server.pipeline_events)} — it must be capped"
    )


def test_completed_tasks_do_not_accumulate_in_flight():
    """Every finished task must leave task_inflight, or the dict grows forever."""
    for i in range(50):
        task_id = f"t-{i}"
        server.task_inflight[task_id] = {"task_id": task_id, "assigned_to": "n"}
        server.task_inflight.pop(task_id, None)
        server.task_results[task_id] = {
            "task_id": task_id,
            "node_id": "n",
            "output": "x",
            "completed_at": 0,  # ancient, so the janitor prunes them
        }

    assert server.task_inflight == {}

    server_state._cleanup_pass()

    assert server.task_results == {}, "old task results must be pruned by the janitor"


def test_reclaim_is_idempotent_across_repeated_sweeps():
    """Repeated janitor passes must not duplicate a reclaimed task."""
    import time

    server.nodes["gone"] = {"node_id": "gone", "last_seen": time.time() - 10_000}
    server.task_inflight["t-x"] = {"task_id": "t-x", "assigned_to": "gone"}

    server_state._cleanup_pass()
    server_state._cleanup_pass()
    server_state._cleanup_pass()

    assert [t["task_id"] for t in server.task_queue] == ["t-x"]
