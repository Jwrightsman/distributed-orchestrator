"""Offline controls for review truth, builder ownership, and generation contracts."""

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest

import orchestrator
import server_state as state
from execution.attempts import AttemptRejected, AttemptStore
from execution.contracts import ExecutionRequestV1
from execution.service import ExecutionService
from execution.persistence import ExecutionStore
from execution.artifacts import ArtifactStore
from execution.dispatch import PlacementUnavailable
from execution.generation import MAX_GENERATION_BRIEF_BYTES, generation_brief
from execution.registry import StrategyOutcome, StrategyRegistry


PLAN = [{"id": 1, "title": "Build", "prompt": "Complete the deliverable", "depends_on": []}]
BAD = "An authored defective deliverable. " * 5
GOOD = "An authored corrected deliverable. " * 5


def review_text(rating, output, issues="None"):
    return f"## Quality Rating\n{rating}\n\n## Issues Found\n{issues}\n\n## Final Assembled Output\n{output}"


@pytest.fixture
def pipeline_fakes(monkeypatch):
    async def plan(*args, **kwargs):
        return PLAN

    async def build(*args, **kwargs):
        return BAD

    async def extract(task, output, review, path, **kwargs):
        return output, [], [], None

    monkeypatch.setattr(orchestrator, "plan", plan)
    monkeypatch.setattr(orchestrator, "build", build)
    monkeypatch.setattr(orchestrator, "extract_and_repair", extract)
    monkeypatch.setattr(orchestrator, "log_contribution", lambda *args, **kwargs: None)


@pytest.mark.asyncio
async def test_dag_generation_receives_contract_only_requirement(tmp_path, monkeypatch, pipeline_fakes):
    seen = {}

    async def plan(task, **kwargs):
        seen["planner"] = task
        return PLAN

    async def build(subtask, context, **kwargs):
        seen["builder"] = orchestrator.compose_builder_prompt(subtask, context, kwargs["task"])
        return GOOD

    async def review(task, *args, **kwargs):
        seen["reviewer"] = task
        return review_text("PASS", GOOD)

    monkeypatch.setattr(orchestrator, "plan", plan)
    monkeypatch.setattr(orchestrator, "build", build)
    monkeypatch.setattr(orchestrator, "review", review)
    database = tmp_path / "executions.db"
    service = ExecutionService(store=ExecutionStore(database), artifacts=ArtifactStore(database, allowed_roots=[tmp_path]))
    service._emit = lambda *args, **kwargs: None
    request = ExecutionRequestV1(task="Make a report", strategy="dag", output_contract={"kind": "single_artifact", "format": "txt", "required_files": ["required-only-in-contract.txt"]})
    await service.execute(request)
    assert set(seen) == {"planner", "builder", "reviewer"}
    assert all("required-only-in-contract.txt" in prompt for prompt in seen.values())


@pytest.mark.parametrize("text", ["", "PASS", "## Quality Rating\nprobably PASS", "## Quality Rating\nPASS\nFAIL", "## Quality Rating\nPASS\n## Quality Rating\nFAIL", "## Final Assembled Output\nPASS"])
def test_missing_malformed_or_ambiguous_rating_is_unknown(text):
    assert orchestrator._extract_rating(text) == "UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.parametrize("revised, verdict, expected", [(BAD, "PASS", "FAIL"), (GOOD, "unrated", "FAIL"), (GOOD, "FAIL", "FAIL"), (GOOD, "PASS", "PASS"), ("tiny", "PASS", "FAIL")])
async def test_revision_success_requires_changed_output_and_separate_review(monkeypatch, pipeline_fakes, revised, verdict, expected):
    reviews = []

    async def review(task, subtasks, results, **kwargs):
        reviews.append(results)
        return review_text("FAIL", BAD, "Fix the authored defect") if len(reviews) == 1 else review_text(verdict, revised, "None" if verdict != "FAIL" else "Still defective")

    async def revise(*args):
        return revised

    monkeypatch.setattr(orchestrator, "review", review)
    monkeypatch.setattr(orchestrator, "revise", revise)
    result = await orchestrator.run_pipeline("Build a deliverable")
    record = json.loads((Path(result["project_dir"]) / "full_log.json").read_text(encoding="utf-8"))["revision"]
    assert result["rating"] == expected
    assert record["attempted"] is True
    assert record["successful"] is (expected == "PASS")
    assert record["cleared_the_rating"] is (expected == "PASS")
    if expected == "PASS":
        assert len(reviews) == 2
        assert record["reviews"][0]["rating"] == "PASS"
        assert result["final_output"] == GOOD


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_pipeline", [False, True])
async def test_builder_wave_drains_sibling_before_return(monkeypatch, pipeline_fakes, cancel_pipeline):
    entered = asyncio.Event()
    stopped = asyncio.Event()
    gate = asyncio.Event()
    completed = []

    async def plan(*args, **kwargs):
        return PLAN + [{"id": 2, "title": "Sibling", "prompt": "Wait", "depends_on": []}]

    async def build(subtask, *args, **kwargs):
        if subtask["id"] == 1:
            await entered.wait()
            if not cancel_pipeline:
                raise RuntimeError("authored builder failure")
            await gate.wait()
        else:
            entered.set()
            try:
                await gate.wait()
                completed.append(2)
            finally:
                stopped.set()
        return GOOD

    monkeypatch.setattr(orchestrator, "plan", plan)
    monkeypatch.setattr(orchestrator, "build", build)
    running = asyncio.create_task(orchestrator.run_pipeline("Build a deliverable"))
    await asyncio.wait_for(entered.wait(), 2)
    if cancel_pipeline:
        running.cancel()
    try:
        with pytest.raises(asyncio.CancelledError if cancel_pipeline else RuntimeError):
            await running
        assert stopped.is_set()
        assert completed == []
    finally:
        gate.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "placement", "outcome", "normalization", "finalization"])
async def test_terminal_failure_revokes_queued_and_leased_work_preserving_receipt(tmp_path, monkeypatch, failure):
    database = tmp_path / "runtime.db"
    attempts = AttemptStore(database)
    monkeypatch.setattr(state, "attempt_store", attempts)
    monkeypatch.setattr(state, "task_queue", [])
    monkeypatch.setattr(state, "task_inflight", {})
    submissions = {}
    execution_id = "a" * 32

    class FailingStrategy:
        identifier = "dag"
        version = "1"

        async def execute(self, request, options, context):
            for task_id in ("accepted", "leased", "queued"):
                task = {"task_id": task_id, "execution_id": execution_id, "contract_version": "1", "execution_unit_id": task_id, "execution_unit_kind": "dag_subtask"}
                if task_id == "queued":
                    state.enqueue_task(task)
                    continue
                now = time.time()
                attempt_id = "attempt-" + task_id
                nonce = "nonce-" + task_id
                attempts.issue(task, assigned_node_id="worker", attempt_id=attempt_id, nonce=nonce, issued_at=now, lease_expires_at=now + 60)
                task["attempt_id"] = attempt_id
                submissions[task_id] = {"task_id": task_id, "node_id": "worker", "output": GOOD, "error": None, "elapsed_seconds": 1.0, "contract_version": "1", "attempt_id": attempt_id, "nonce": nonce, "execution_id": execution_id, "execution_unit_id": task_id, "execution_unit_kind": "dag_subtask"}
                if task_id == "accepted":
                    attempts.settle(**submissions[task_id])
                else:
                    state.task_inflight[task_id] = task
            if failure == "exception":
                raise RuntimeError("authored failure")
            if failure == "placement":
                raise PlacementUnavailable("authored placement failure")
            if failure == "normalization":
                return StrategyOutcome(output_preview="x" * 100_000)
            if failure == "finalization":
                context.artifact_root_path = tmp_path
                return StrategyOutcome()
            return StrategyOutcome(status="failed")

    registry = StrategyRegistry()
    registry.register(FailingStrategy())
    service = ExecutionService(store=ExecutionStore(database), artifacts=ArtifactStore(database, allowed_roots=[tmp_path]), registry=registry)
    if failure == "finalization":
        def fail_seal(*args, **kwargs):
            raise RuntimeError("authored finalization failure")
        monkeypatch.setattr(service.artifacts, "seal_manifest", fail_seal)
    events = []

    def emitted(event, data):
        if event == "execution_failed":
            assert not state.task_queue
            assert not state.task_inflight
        events.append(event)

    service._emit = emitted
    run = await service.execute(ExecutionRequestV1(task="Authored failure", strategy="dag"), execution_id=execution_id)
    assert run.result.lifecycle_status == "failed"
    assert service.store.get(execution_id).lifecycle_status == "failed"
    assert state.task_queue == []
    assert state.task_inflight == {}
    assert attempts.get("attempt-leased").state == "cancelled"
    with pytest.raises(AttemptRejected):
        attempts.settle(**submissions["leased"])
    assert attempts.get("attempt-accepted").state == "settled"
    assert attempts.get_receipt_for_task("accepted") is not None
    assert attempts.settle(**submissions["accepted"]).replayed is True
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM contributions").fetchone()[0] == 1
    assert events.count("execution_failed") == 1


@pytest.mark.asyncio
async def test_revocation_failure_does_not_publish_terminal_failure(tmp_path, monkeypatch):
    from execution.dispatch import Dispatcher
    from execution.service import TerminalPersistenceError

    class FailingStrategy:
        identifier = "dag"
        version = "1"

        async def execute(self, *args):
            raise RuntimeError("authored strategy failure")

    registry = StrategyRegistry()
    registry.register(FailingStrategy())
    db = tmp_path / "failure.db"
    service = ExecutionService(store=ExecutionStore(db), artifacts=ArtifactStore(db, allowed_roots=[tmp_path]),
                               registry=registry)
    calls = []
    def fail_revoke(*args, **kwargs):
        calls.append(args)
        raise OSError("authored unavailable persistence")
    monkeypatch.setattr(Dispatcher, "cancel_execution", fail_revoke)
    events = []
    service._emit = lambda event, *args: events.append(event)
    execution_id = "b" * 32
    with pytest.raises(TerminalPersistenceError, match="dispatcher_revocation"):
        await service.execute(ExecutionRequestV1(task="authored", strategy="dag"), execution_id=execution_id)
    assert 1 < len(calls) <= 5
    assert execution_id not in service._controls and execution_id not in service._background
    assert service.store.get(execution_id).lifecycle_status == "running"
    assert "execution_failed" not in events


def test_generation_brief_has_only_bounded_public_contract_inputs():
    public = {"kind": "structured_json", "json_schema": {"type": "object", "properties": {"report_title": {"type": "string"}}, "required": ["report_title"]}}
    private_fixture = {"reference_answer": "private reference sentinel", "expected_stdout": "private stdout sentinel"}
    request = ExecutionRequestV1(task="Write a report", output_contract=public)
    brief = generation_brief(request.task, request.output_contract)
    assert "report_title" in brief
    assert all(secret not in brief for secret in private_fixture.values())
    assert len(brief.encode("utf-8")) <= MAX_GENERATION_BRIEF_BYTES
    assert generation_brief("unchanged task", None) == "unchanged task"
