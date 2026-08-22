"""Total execution deadlines, cancellation, and callback visibility."""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

import execution.strategies as strategies
from execution.artifacts import ArtifactStore
from execution.contracts import ExecutionRequestV1
from execution.persistence import ExecutionStore
from execution.service import ExecutionControl, ExecutionService


def _service(tmp_path) -> ExecutionService:
    database = tmp_path / "events.db"
    service = ExecutionService(
        store=ExecutionStore(database),
        artifacts=ArtifactStore(database, allowed_roots=[tmp_path]),
    )
    service.store.migrate()
    service.artifacts.migrate()
    service._emit = lambda *args, **kwargs: None
    return service


def _short_control(service: ExecutionService, request: ExecutionRequestV1) -> ExecutionControl:
    execution_id = "d" * 32
    return ExecutionControl(
        execution_id=execution_id,
        request=request,
        deadline_monotonic=time.monotonic() + 0.8,
        cancel_event=asyncio.Event(),
        result=service._new_result(request, execution_id, None, "queued"),
    )


async def _wait_until(predicate, *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached")


@pytest.mark.asyncio
async def test_total_deadline_cancels_local_generation(tmp_path, monkeypatch):
    cancelled = asyncio.Event()

    async def slow_generation(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    monkeypatch.setattr(strategies, "generate", slow_generation)
    monkeypatch.setattr(strategies.EnsembleStrategy, "artifact_root", tmp_path / "artifacts")
    service = _service(tmp_path)
    request = ExecutionRequestV1(task="Build it", strategy="direct", timeout_seconds=1)

    run = await service.execute(
        request,
        execution_id="d" * 32,
        control=_short_control(service, request),
    )

    assert run.result.lifecycle_status == "failed"
    assert run.result.errors[0].code == "execution_timeout"
    assert run.result.retryable is True
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_ensemble_deadline_cancels_and_joins_every_candidate(tmp_path, monkeypatch):
    started: list[int] = []
    stopped: list[int] = []

    async def slow_generation(*args, **kwargs):
        index = len(started) + 1
        started.append(index)
        try:
            await asyncio.sleep(10)
        finally:
            stopped.append(index)

    monkeypatch.setattr(strategies, "generate", slow_generation)
    monkeypatch.setattr(strategies.EnsembleStrategy, "artifact_root", tmp_path / "artifacts")
    service = _service(tmp_path)
    request = ExecutionRequestV1(
        task="Build it",
        strategy="ensemble",
        strategy_options={"candidates": 2, "concurrency": 2},
        timeout_seconds=1,
    )

    run = await service.execute(
        request,
        execution_id="d" * 32,
        control=_short_control(service, request),
    )

    assert run.result.lifecycle_status == "failed"
    assert started == [1, 2]
    assert sorted(stopped) == [1, 2]


@pytest.mark.asyncio
async def test_total_deadline_applies_during_dag_planning(tmp_path):
    async def slow_planner(*args, **kwargs):
        await asyncio.sleep(10)

    service = _service(tmp_path)
    request = ExecutionRequestV1(task="Plan it", strategy="dag", timeout_seconds=1)
    run = await service.execute(
        request,
        execution_id="d" * 32,
        control=_short_control(service, request),
        dag_runner=slow_planner,
    )

    assert run.result.lifecycle_status == "failed"
    assert run.result.errors[0].code == "execution_timeout"


@pytest.mark.asyncio
async def test_cancellation_while_queued_is_terminal_and_idempotent(tmp_path, monkeypatch):
    async def should_not_finish(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(strategies, "generate", should_not_finish)
    monkeypatch.setattr(strategies.EnsembleStrategy, "artifact_root", tmp_path / "artifacts")
    service = _service(tmp_path)
    queued = service.submit(ExecutionRequestV1(task="Build it", strategy="direct"))

    first = await service.cancel(queued.execution_id, "operator requested cancellation")
    second = await service.cancel(queued.execution_id, "duplicate request")

    assert first.lifecycle_status == "cancelled"
    assert first.cancellation_reason == "operator requested cancellation"
    assert second.lifecycle_status == "cancelled"
    assert service.store.get(queued.execution_id).lifecycle_status == "cancelled"


@pytest.mark.asyncio
async def test_cancellation_during_local_generation_stops_work(tmp_path, monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def slow_generation(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()

    monkeypatch.setattr(strategies, "generate", slow_generation)
    monkeypatch.setattr(strategies.EnsembleStrategy, "artifact_root", tmp_path / "artifacts")
    service = _service(tmp_path)
    queued = service.submit(ExecutionRequestV1(task="Build it", strategy="direct"))
    await asyncio.wait_for(started.wait(), timeout=1)

    cancelled = await service.cancel(queued.execution_id, "stop now")

    assert cancelled.lifecycle_status == "cancelled"
    assert cancelled.cancelled_at
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_completion_callback_exception_is_persisted_and_emitted(tmp_path, monkeypatch):
    emitted = []

    async def generated(*args, **kwargs):
        return "complete output"

    async def broken_callback(_run):
        raise RuntimeError("callback broke")

    monkeypatch.setattr(strategies, "generate", generated)
    monkeypatch.setattr(strategies.EnsembleStrategy, "artifact_root", tmp_path / "artifacts")
    service = _service(tmp_path)
    service._emit = lambda event, data: emitted.append((event, data))
    queued = service.submit(
        ExecutionRequestV1(task="Build it", strategy="direct"),
        on_complete=broken_callback,
    )

    await _wait_until(
        lambda: bool(
            (result := service.get(queued.execution_id))
            and result.telemetry.get("completion_callback_failed")
        )
    )
    result = service.get(queued.execution_id)
    assert any(error.code == "completion_callback_failed" for error in result.errors)
    assert any(event == "execution_callback_failed" for event, _ in emitted)


def test_submit_rejects_project_memory_for_direct_before_persistence(tmp_path):
    service = _service(tmp_path)
    request = ExecutionRequestV1(
        task="Continue the project",
        strategy="direct",
        project_id="project-1",
    )

    with pytest.raises(ValueError, match="project_id is not supported"):
        service.submit(request)

    with sqlite3.connect(service.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_running_persistence_failure_becomes_interrupted(tmp_path, monkeypatch):
    service = _service(tmp_path)
    original_save = service.store.save
    failed_once = False

    def fail_first_running_save(request, result):
        nonlocal failed_once
        if result.lifecycle_status == "running" and not failed_once:
            failed_once = True
            raise sqlite3.OperationalError("transient write failure")
        return original_save(request, result)

    monkeypatch.setattr(service.store, "save", fail_first_running_save)
    request = ExecutionRequestV1(task="Build it", strategy="direct")
    run = await service.execute(request)

    assert run.result.lifecycle_status == "interrupted"
    assert run.result.retryable is True
    assert service.store.get(run.result.execution_id).lifecycle_status == "interrupted"


@pytest.mark.asyncio
async def test_terminal_persistence_retries_transient_failure(tmp_path, monkeypatch):
    async def generated(*args, **kwargs):
        return "complete output"

    service = _service(tmp_path)
    monkeypatch.setattr(strategies, "generate", generated)
    monkeypatch.setattr(strategies.EnsembleStrategy, "artifact_root", tmp_path / "artifacts")
    original_save = service.store.save
    failed_once = False

    def fail_first_terminal_save(request, result):
        nonlocal failed_once
        if result.lifecycle_status == "completed" and not failed_once:
            failed_once = True
            raise sqlite3.OperationalError("transient final write failure")
        return original_save(request, result)

    monkeypatch.setattr(service.store, "save", fail_first_terminal_save)
    run = await service.execute(ExecutionRequestV1(task="Build it", strategy="direct"))

    assert failed_once is True
    assert run.result.lifecycle_status == "completed"
    assert service.store.get(run.result.execution_id).lifecycle_status == "completed"


@pytest.mark.asyncio
async def test_start_callback_runs_after_running_state_is_durable(tmp_path, monkeypatch):
    observed = []

    async def generated(*args, **kwargs):
        return "complete output"

    service = _service(tmp_path)
    monkeypatch.setattr(strategies, "generate", generated)
    monkeypatch.setattr(strategies.EnsembleStrategy, "artifact_root", tmp_path / "artifacts")

    def on_start(result):
        observed.append(service.store.get(result.execution_id).lifecycle_status)

    queued = service.submit(
        ExecutionRequestV1(task="Build it", strategy="direct"),
        on_start=on_start,
    )
    await _wait_until(
        lambda: service.get(queued.execution_id).lifecycle_status == "completed"
    )

    assert observed == ["running"]
