"""Production strategy behavior without a live Ollama or worker process."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import execution.strategies as strategies
import server_state as state
from execution.contracts import ExecutionRequestV1
from execution.artifacts import ArtifactSecurityError, ArtifactStore
from execution.dispatch import DispatchResult, Dispatcher
from execution.persistence import ExecutionStore
from execution.service import ExecutionService
from execution.validators import ValidatorRegistry


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    for store in (state.nodes, state.task_queue, state.task_inflight, state.task_results):
        store.clear()
    monkeypatch.setattr(strategies.EnsembleStrategy, "artifact_root", tmp_path / "candidates")
    monkeypatch.setattr(state, "_emit", lambda *args, **kwargs: None)


def service(tmp_path) -> ExecutionService:
    db_path = tmp_path / "executions.db"
    return ExecutionService(
        store=ExecutionStore(db_path),
        artifacts=ArtifactStore(db_path, allowed_roots=[tmp_path]),
    )


def dag_result(tmp_path: Path, output: str = "complete output") -> dict:
    run = tmp_path / "dag-run"
    run.mkdir(exist_ok=True)
    (run / "output.md").write_text(output, encoding="utf-8")
    return {
        "project_dir": str(run),
        "plan": [{"id": 1, "title": "Build", "prompt": "whole", "depends_on": []}],
        "results": {1: output},
        "review": output,
        "final_output": output,
        "rating": "PASS",
        "code_files": [],
        "code_problems": [],
        "project_id": "",
    }


@pytest.mark.asyncio
async def test_dag_local_executes_units_through_shared_dispatcher(tmp_path, monkeypatch):
    calls = []

    async def local_build(subtask, context, **kwargs):
        calls.append((subtask["id"], context, kwargs["task"]))
        return "local unit output"

    async def runner(task, build_fn, **kwargs):
        output = await build_fn({"id": 1, "title": "Build", "prompt": "p", "depends_on": []}, "")
        return dag_result(tmp_path, output)

    monkeypatch.setattr(strategies.orchestrator, "build", local_build)
    run = await service(tmp_path).execute(
        ExecutionRequestV1(task="Build it", strategy="dag", placement="local"),
        dag_runner=runner,
    )

    assert run.result.status == "completed"
    assert run.result.execution_units[0].placement == "local"
    assert calls == [(1, "", "Build it")]


@pytest.mark.asyncio
async def test_dag_distributed_uses_the_same_strategy_and_full_worker_dispatch(tmp_path, monkeypatch):
    state.nodes["worker"] = {"capabilities": [], "last_seen": 0}
    dispatched = []

    async def remote(self, unit, request, execution_id, strategy, decision, **kwargs):
        dispatched.append((unit, strategy, decision.qualifying_nodes))
        return DispatchResult(
            unit=unit,
            status="completed",
            output="remote unit output",
            placement="distributed",
            node_id="worker",
            attempt_count=1,
        )

    async def runner(task, build_fn, **kwargs):
        output = await build_fn({"id": 1, "title": "Build", "prompt": "p", "depends_on": []}, "")
        return dag_result(tmp_path, output)

    monkeypatch.setattr(Dispatcher, "_distributed", remote)
    run = await service(tmp_path).execute(
        ExecutionRequestV1(
            task="Build it",
            strategy="dag",
            placement="distributed",
            confidentiality="trusted_guild",
            remote_dispatch_consent=True,
        ),
        dag_runner=runner,
    )

    assert run.result.placement_selected == "distributed"
    assert run.result.participating_nodes == ["worker"]
    assert dispatched[0][0].kind == "dag_subtask"
    assert dispatched[0][1:] == ("dag", ("worker",))


@pytest.mark.asyncio
async def test_dag_no_node_fallback_and_review_options_are_recorded(tmp_path, monkeypatch):
    received = {}

    async def local_build(*args, **kwargs):
        return "fallback output"

    async def runner(task, build_fn, **kwargs):
        received.update(kwargs)
        output = await build_fn({"id": 1, "title": "Build", "prompt": "p", "depends_on": []}, "")
        return dag_result(tmp_path, output)

    monkeypatch.setattr(strategies.orchestrator, "build", local_build)
    request = ExecutionRequestV1.model_validate({
        "task": "Build it",
        "strategy": "dag",
        "strategy_options": {
            "maximum_subtasks": 2,
            "review_enabled": False,
            "revision_enabled": False,
        },
        "placement": "distributed",
        "confidentiality": "trusted_guild",
        "remote_dispatch_consent": True,
    })
    run = await service(tmp_path).execute(request, dag_runner=runner)

    assert run.result.placement_selected == "local"
    assert "No connected node" in run.result.fallback_reason
    assert received["maximum_subtasks"] == 2
    assert received["review_enabled"] is False
    assert received["revision_enabled"] is False


@pytest.mark.asyncio
async def test_direct_is_one_production_ensemble_candidate(tmp_path, monkeypatch):
    async def generated(*args, **kwargs):
        return "one complete candidate"

    monkeypatch.setattr(strategies, "generate", generated)
    run = await service(tmp_path).execute(
        ExecutionRequestV1(task="Do it", strategy="direct", placement="local")
    )

    assert run.result.strategy_requested == "direct"
    assert run.result.strategy_selected == "ensemble"
    assert run.result.strategy_options["candidates"] == 1
    assert len(run.result.candidates) == 1
    assert run.result.winning_candidate == "candidate-1"


@pytest.mark.asyncio
async def test_ensemble_partial_failure_does_not_cancel_other_candidates(tmp_path, monkeypatch):
    call = 0

    async def generated(*args, **kwargs):
        nonlocal call
        call += 1
        if call == 2:
            raise RuntimeError("candidate crashed")
        return f"complete candidate {call}"

    monkeypatch.setattr(strategies, "generate", generated)
    request = ExecutionRequestV1.model_validate({
        "task": "Do it",
        "strategy": "ensemble",
        "strategy_options": {"candidates": 3, "concurrency": 1},
        "placement": "local",
    })
    run = await service(tmp_path).execute(request)

    assert run.result.status == "completed"
    assert [item.status for item in run.result.candidates].count("failed") == 1
    assert run.result.winning_candidate in {"candidate-1", "candidate-3"}


@pytest.mark.asyncio
async def test_ensemble_all_candidate_failure_is_structured(tmp_path, monkeypatch):
    async def generated(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(strategies, "generate", generated)
    request = ExecutionRequestV1.model_validate({
        "task": "Do it",
        "strategy": "ensemble",
        "strategy_options": {"candidates": 2, "concurrency": 2},
        "placement": "local",
    })
    run = await service(tmp_path).execute(request)

    assert run.result.status == "failed"
    assert run.result.winning_candidate is None
    assert run.result.errors[0].code == "all_candidates_failed"


@pytest.mark.asyncio
async def test_deterministic_validator_selects_the_valid_candidate(tmp_path, monkeypatch):
    outputs = iter(('{"answer": "wrong"}', '{"answer": 42}', "not json"))

    async def generated(*args, **kwargs):
        return next(outputs)

    monkeypatch.setattr(strategies, "generate", generated)
    request = ExecutionRequestV1.model_validate({
        "task": "Return structured data",
        "strategy": "ensemble",
        "strategy_options": {"candidates": 3, "concurrency": 1},
        "placement": "local",
        "output_contract": {
            "kind": "structured_json",
            "json_schema": {
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
            },
            "validators": [{"name": "json_schema"}],
        },
    })
    run = await service(tmp_path).execute(request)

    assert run.result.status == "completed"
    assert run.result.winning_candidate == "candidate-2"
    assert "required validation passed" in run.result.winner_selection_explanation
    assert "not general behavioral correctness" in run.result.winner_selection_explanation


@pytest.mark.asyncio
async def test_unverified_fallback_is_never_described_as_verified(tmp_path, monkeypatch):
    async def generated(*args, **kwargs):
        return "not json but still an output"

    monkeypatch.setattr(strategies, "generate", generated)
    request = ExecutionRequestV1.model_validate({
        "task": "Return JSON",
        "strategy": "ensemble",
        "strategy_options": {"candidates": 2, "concurrency": 1},
        "placement": "local",
        "output_contract": {
            "kind": "structured_json",
            "validators": [{"name": "structured_json"}],
        },
        "verification": {"allow_unverified_fallback": True},
    })
    run = await service(tmp_path).execute(request)

    assert run.result.status == "unverified"
    assert run.result.lifecycle_status == "completed"
    assert run.result.validation_outcome == "partial"
    assert run.result.assurance_level == "structural"
    assert run.result.candidates[0].status in {"unverified", "rejected"}
    assert "not a deterministic correctness claim" in run.result.winner_selection_explanation


@pytest.mark.asyncio
async def test_ensemble_concurrency_is_bounded(tmp_path, monkeypatch):
    active = 0
    maximum = 0

    async def generated(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "candidate"

    monkeypatch.setattr(strategies, "generate", generated)
    request = ExecutionRequestV1.model_validate({
        "task": "Do it",
        "strategy": "ensemble",
        "strategy_options": {"candidates": 5, "concurrency": 2},
        "placement": "local",
    })
    run = await service(tmp_path).execute(request)

    assert len(run.result.candidates) == 5
    assert maximum == 2


@pytest.mark.asyncio
async def test_distributed_ensemble_fans_out_complete_candidates_with_one_worker(tmp_path, monkeypatch):
    state.nodes["only-worker"] = {"capabilities": [], "last_seen": 0}
    units = []

    async def remote(self, unit, request, execution_id, strategy, decision, **kwargs):
        units.append(unit)
        return DispatchResult(
            unit=unit,
            status="completed",
            output=f"complete: {unit.prompt}",
            placement="distributed",
            node_id="only-worker",
            attempt_count=1,
        )

    monkeypatch.setattr(Dispatcher, "_distributed", remote)
    request = ExecutionRequestV1.model_validate({
        "task": "Build the whole artifact",
        "strategy": "ensemble",
        "strategy_options": {"candidates": 3, "concurrency": 3},
        "placement": "distributed",
        "confidentiality": "trusted_guild",
        "remote_dispatch_consent": True,
    })
    run = await service(tmp_path).execute(request)

    assert len(units) == 3
    assert all(unit.kind == "candidate" for unit in units)
    assert all("Build the whole artifact" in unit.prompt for unit in units)
    assert run.result.participating_nodes == ["only-worker"]
    assert run.result.telemetry["candidate_count"] == 3


@pytest.mark.asyncio
async def test_candidate_directory_failure_does_not_cancel_peers(tmp_path, monkeypatch):
    original_mkdir = Path.mkdir

    def flaky_mkdir(path, *args, **kwargs):
        if path.name == "candidate_2":
            raise OSError("candidate directory unavailable")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", flaky_mkdir)
    monkeypatch.setattr(strategies, "generate", lambda *args, **kwargs: asyncio.sleep(0, result="ok"))
    run = await service(tmp_path).execute(
        ExecutionRequestV1(
            task="Build it",
            strategy="ensemble",
            strategy_options={"candidates": 3, "concurrency": 1},
        )
    )

    failed = next(item for item in run.result.candidates if item.candidate_id == "candidate-2")
    assert failed.status == "failed"
    assert failed.failure_stage == "directory_creation"
    assert run.result.lifecycle_status == "completed"
    assert run.result.winning_candidate in {"candidate-1", "candidate-3"}


@pytest.mark.asyncio
async def test_candidate_extraction_failure_does_not_cancel_peers(tmp_path, monkeypatch):
    original = strategies.ensemble.materialise

    def flaky_materialise(candidate, root):
        if candidate.index == 2:
            raise RuntimeError("extractor failed")
        return original(candidate, root)

    monkeypatch.setattr(strategies.ensemble, "materialise", flaky_materialise)
    monkeypatch.setattr(strategies, "generate", lambda *args, **kwargs: asyncio.sleep(0, result="ok"))
    run = await service(tmp_path).execute(
        ExecutionRequestV1(
            task="Build it",
            strategy="ensemble",
            strategy_options={"candidates": 3, "concurrency": 1},
        )
    )

    failed = next(item for item in run.result.candidates if item.candidate_id == "candidate-2")
    assert failed.failure_stage == "artifact_extraction"
    assert run.result.winning_candidate in {"candidate-1", "candidate-3"}


@pytest.mark.asyncio
async def test_candidate_validator_exception_does_not_cancel_peers(tmp_path, monkeypatch):
    outputs = iter(("candidate one", "validator-bomb", "candidate three"))
    original = ValidatorRegistry.validate

    def flaky_validate(self, request, output, files, **kwargs):
        if output == "validator-bomb":
            raise RuntimeError("validator failed")
        return original(self, request, output, files, **kwargs)

    async def generated(*args, **kwargs):
        return next(outputs)

    monkeypatch.setattr(ValidatorRegistry, "validate", flaky_validate)
    monkeypatch.setattr(strategies, "generate", generated)
    run = await service(tmp_path).execute(
        ExecutionRequestV1(
            task="Build it",
            strategy="ensemble",
            strategy_options={"candidates": 3, "concurrency": 1},
        )
    )

    failed = next(item for item in run.result.candidates if item.candidate_id == "candidate-2")
    assert failed.failure_stage == "validation"
    assert run.result.winning_candidate in {"candidate-1", "candidate-3"}


@pytest.mark.asyncio
async def test_candidate_manifest_failure_does_not_poison_winner(tmp_path, monkeypatch):
    original = ArtifactStore.validate_subtree

    def flaky_manifest(self, execution_id, prefix):
        if prefix == "candidate_2":
            raise ArtifactSecurityError("unsafe candidate tree")
        return original(self, execution_id, prefix)

    monkeypatch.setattr(ArtifactStore, "validate_subtree", flaky_manifest)
    monkeypatch.setattr(strategies, "generate", lambda *args, **kwargs: asyncio.sleep(0, result="ok"))
    run = await service(tmp_path).execute(
        ExecutionRequestV1(
            task="Build it",
            strategy="ensemble",
            strategy_options={"candidates": 3, "concurrency": 1},
        )
    )

    failed = next(item for item in run.result.candidates if item.candidate_id == "candidate-2")
    assert failed.failure_stage == "manifest_creation"
    assert run.result.output_reference.endswith("/artifacts")
    assert all("candidate_2/" not in path for path in run.result.produced_files)


@pytest.mark.asyncio
async def test_production_tie_break_ignores_output_length(tmp_path, monkeypatch):
    outputs = iter(("short", "much longer output " * 200))

    async def generated(*args, **kwargs):
        return next(outputs)

    monkeypatch.setattr(strategies, "generate", generated)
    run = await service(tmp_path).execute(
        ExecutionRequestV1(
            task="Build it",
            strategy="ensemble",
            strategy_options={"candidates": 2, "concurrency": 1},
        )
    )

    assert run.result.winning_candidate == "candidate-1"
    assert "stable candidate identifier" in run.result.winner_selection_explanation


@pytest.mark.asyncio
async def test_first_valid_means_first_acceptable_completion(tmp_path, monkeypatch):
    calls = 0

    async def generated(*args, **kwargs):
        nonlocal calls
        calls += 1
        index = calls
        await asyncio.sleep(0.05 if index == 1 else 0.005)
        return f"candidate {index}"

    monkeypatch.setattr(strategies, "generate", generated)
    run = await service(tmp_path).execute(
        ExecutionRequestV1(
            task="Build it",
            strategy="ensemble",
            strategy_options={
                "candidates": 2,
                "concurrency": 2,
                "selection_policy": "first_valid",
            },
        )
    )

    assert run.result.winning_candidate == "candidate-2"
    assert "first acceptable completion order" in run.result.winner_selection_explanation


@pytest.mark.asyncio
async def test_all_validation_rejected_without_fallback_is_failed(tmp_path, monkeypatch):
    async def generated(*args, **kwargs):
        return "not json"

    monkeypatch.setattr(strategies, "generate", generated)
    run = await service(tmp_path).execute(
        ExecutionRequestV1(
            task="Return JSON",
            strategy="ensemble",
            strategy_options={"candidates": 2, "concurrency": 1},
            output_contract={"kind": "structured_json"},
            verification={"allow_unverified_fallback": False},
        )
    )

    assert run.result.lifecycle_status == "failed"
    assert run.result.winning_candidate is None
    assert all(item.status == "rejected" for item in run.result.candidates)


@pytest.mark.asyncio
async def test_selected_candidate_manifest_contains_only_winner(tmp_path, monkeypatch):
    outputs = iter(("```python\nprint(1)\n```", "```python\nprint(2)\n```"))

    async def generated(*args, **kwargs):
        return next(outputs)

    monkeypatch.setattr(strategies, "generate", generated)
    execution_service = service(tmp_path)
    run = await execution_service.execute(
        ExecutionRequestV1(
            task="Build it",
            strategy="ensemble",
            strategy_options={"candidates": 2, "concurrency": 1},
        )
    )
    manifest = execution_service.artifacts.refresh_manifest(run.result.execution_id)

    assert run.result.winning_candidate == "candidate-1"
    assert manifest.entries
    assert all(entry.source_candidate_id == "candidate-1" for entry in manifest.entries)
    assert all(entry.relative_path.startswith("candidate_1/") for entry in manifest.entries)
    assert run.result.produced_files == [entry.relative_path for entry in manifest.entries]


@pytest.mark.asyncio
async def test_remote_to_local_fallback_is_reported_as_mixed(tmp_path, monkeypatch):
    state.nodes["worker"] = {"capabilities": [], "last_seen": 0}

    async def remote_failure(self, unit, request, execution_id, strategy, decision, **kwargs):
        return DispatchResult(
            unit=unit,
            status="failed",
            placement="distributed",
            error="worker failed",
            attempt_count=1,
            observed_placements=("distributed",),
        )

    async def local_build(*args, **kwargs):
        return "local fallback output"

    async def runner(task, build_fn, **kwargs):
        output = await build_fn(
            {"id": 1, "title": "Build", "prompt": "p", "depends_on": []},
            "",
        )
        return dag_result(tmp_path, output)

    monkeypatch.setattr(Dispatcher, "_distributed", remote_failure)
    monkeypatch.setattr(strategies.orchestrator, "build", local_build)
    run = await service(tmp_path).execute(
        ExecutionRequestV1(
            task="Build it",
            strategy="dag",
            placement="distributed",
            confidentiality="trusted_guild",
            remote_dispatch_consent=True,
        ),
        dag_runner=runner,
    )

    assert run.result.placement_observed == "mixed"
    assert run.result.observed_placements == ["distributed", "local"]
    assert run.result.units_distributed == 1
    assert run.result.units_local == 1
    assert run.result.attempt_count == 2
    assert run.result.fallback_count == 1
