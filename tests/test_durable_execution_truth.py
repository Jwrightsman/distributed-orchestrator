"""Fault tests for commit-before-publication execution invariants."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from threading import Event as ThreadEvent

import pytest
from fastapi import HTTPException, Request, Response

import routes_access
import routes_executions
import routes_pitch
from execution.artifacts import ArtifactNotFound, ArtifactStore
from execution.contracts import ExecutionRequestV1
from execution.persistence import ExecutionStore, ExecutionTransitionConflictError
from execution.registry import StrategyOutcome, StrategyRegistry
from execution.service import (
    ExecutionPersistenceError,
    ExecutionService,
    ServiceExecution,
    TerminalPersistenceError,
)
from execution.sharing import CreateExecutionShareV1, ShareStore


TERMINAL_LIFECYCLES = {"completed", "failed", "cancelled", "interrupted"}
TERMINAL_EVENTS = {
    "execution_completed",
    "execution_failed",
    "execution_cancelled",
    "execution_interrupted",
    "execution_timed_out",
}


class ImmediateDagStrategy:
    identifier = "dag"
    version = "fault-test"

    async def execute(self, request, options, context):
        return StrategyOutcome(
            status="completed",
            validation_outcome="passed",
            assurance_level="structural",
            output_preview="durably committed output",
        )


class BlockingDagStrategy:
    identifier = "dag"
    version = "fault-test"

    def __init__(self, started: asyncio.Event, release: asyncio.Event):
        self.started = started
        self.release = release

    async def execute(self, request, options, context):
        self.started.set()
        await self.release.wait()
        return StrategyOutcome(
            status="completed",
            validation_outcome="passed",
            assurance_level="structural",
        )


class ArtifactDagStrategy:
    identifier = "dag"
    version = "fault-test"

    def __init__(self, root: Path):
        self.root = root

    async def execute(self, request, options, context):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "deliverable.txt").write_text(
            "terminal material",
            encoding="utf-8",
        )
        context.artifact_root_path = self.root
        context.artifacts.register_root(
            context.execution_id,
            self.root,
            strategy=self.identifier,
            active=True,
        )
        return StrategyOutcome(
            status="completed",
            validation_outcome="passed",
            assurance_level="structural",
            output_preview="terminal material",
        )


def _service(tmp_path, strategy=None) -> ExecutionService:
    database = tmp_path / "events.db"
    registry = StrategyRegistry()
    registry.register(strategy or ImmediateDagStrategy())
    service = ExecutionService(
        store=ExecutionStore(database),
        registry=registry,
        artifacts=ArtifactStore(database, allowed_roots=[tmp_path]),
    )
    service.store.migrate()
    service.artifacts.migrate()
    service._emit = lambda *args, **kwargs: None
    return service


def _request() -> ExecutionRequestV1:
    return ExecutionRequestV1(task="Exercise durable publication", strategy="dag")


async def _join_background(service: ExecutionService, execution_id: str) -> None:
    task = service._background[execution_id]
    await asyncio.shield(task)
    # Let the task's synchronous done callback remove the final registry entry.
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_transient_terminal_write_commits_before_publication(tmp_path, monkeypatch):
    service = _service(tmp_path)
    original_save = service.store.save
    original_remember = service._remember
    terminal_attempts = 0
    callback_runs: list[ServiceExecution] = []
    terminal_event_observations = []
    publication_order = []

    def transient_terminal_failure(request, result):
        nonlocal terminal_attempts
        if result.lifecycle_status in TERMINAL_LIFECYCLES:
            terminal_attempts += 1
            if terminal_attempts == 1:
                raise sqlite3.OperationalError("transient terminal write failure")
        saved = original_save(request, result)
        if result.lifecycle_status in TERMINAL_LIFECYCLES:
            publication_order.append("durable")
        return saved

    def capture_remember(request, result):
        original_remember(request, result)
        if result.lifecycle_status in TERMINAL_LIFECYCLES:
            publication_order.append("live")

    def capture_event(name, data):
        if name in TERMINAL_EVENTS:
            publication_order.append("event")
            execution_id = data["execution_id"]
            terminal_event_observations.append(
                (
                    name,
                    service.get(execution_id),
                    service.store.get(execution_id),
                )
            )

    def capture_callback(run):
        publication_order.append("callback")
        callback_runs.append(run)

    monkeypatch.setattr(service.store, "save", transient_terminal_failure)
    monkeypatch.setattr(service, "_remember", capture_remember)
    service._emit = capture_event
    queued = service.submit(_request(), on_complete=capture_callback)
    await _join_background(service, queued.execution_id)

    assert terminal_attempts == 2
    assert publication_order == ["durable", "live", "event", "callback"]
    assert len(terminal_event_observations) == 1
    assert len(callback_runs) == 1
    returned = callback_runs[0].result
    live = service.get(queued.execution_id)
    durable = service.store.get(queued.execution_id)
    event_live = terminal_event_observations[0][1]
    event_durable = terminal_event_observations[0][2]
    assert returned.lifecycle_status == "completed"
    assert returned.model_dump(mode="json") == live.model_dump(mode="json")
    assert returned.model_dump(mode="json") == durable.model_dump(mode="json")
    assert returned.model_dump(mode="json") == event_live.model_dump(mode="json")
    assert returned.model_dump(mode="json") == event_durable.model_dump(mode="json")


@pytest.mark.asyncio
async def test_background_terminal_cache_is_evicted_after_completion_observer(tmp_path):
    service = _service(tmp_path)
    callback_observations = []

    def observe_completion(run):
        execution_id = run.result.execution_id
        callback_observations.append(
            (
                service.get(execution_id).lifecycle_status,
                execution_id in service._live_results,
                execution_id in service._requests,
            )
        )

    queued = service.submit(_request(), on_complete=observe_completion)
    await _join_background(service, queued.execution_id)

    assert callback_observations == [("completed", True, True)]
    assert service._live_results == {}
    assert service._requests == {}
    assert service._controls == {}
    assert service._background == {}
    assert service.get(queued.execution_id).lifecycle_status == "completed"
    assert service.store.get(queued.execution_id).lifecycle_status == "completed"


@pytest.mark.asyncio
async def test_permanent_terminal_write_is_not_published(tmp_path, monkeypatch):
    service = _service(tmp_path)
    original_save = service.store.save
    execution_id = "p" * 32
    terminal_attempts = 0
    emitted = []

    def permanent_terminal_failure(request, result):
        nonlocal terminal_attempts
        if result.lifecycle_status in TERMINAL_LIFECYCLES:
            terminal_attempts += 1
            raise sqlite3.OperationalError("permanent terminal write failure")
        return original_save(request, result)

    monkeypatch.setattr(service.store, "save", permanent_terminal_failure)
    service._emit = lambda name, data: emitted.append((name, data))

    with pytest.raises(TerminalPersistenceError) as raised:
        await service.execute(_request(), execution_id=execution_id)

    assert raised.value.phase == "terminal"
    assert terminal_attempts == 3
    assert not any(name in TERMINAL_EVENTS for name, _ in emitted)
    assert service.get(execution_id).lifecycle_status == "running"
    assert service.store.get(execution_id).lifecycle_status == "running"
    assert execution_id not in service._controls
    assert execution_id not in service._background


def test_initial_queued_write_failure_leaves_no_process_local_state(tmp_path, monkeypatch):
    service = _service(tmp_path)
    create_attempts = 0
    emitted = []

    def permanent_create_failure(request, result):
        nonlocal create_attempts
        create_attempts += 1
        raise sqlite3.OperationalError("queued write failure")

    monkeypatch.setattr(service.store, "create", permanent_create_failure)
    service._emit = lambda name, data: emitted.append((name, data))

    with pytest.raises(ExecutionPersistenceError) as raised:
        service.submit(_request())

    assert raised.value.phase == "queued_submission"
    assert create_attempts == 3
    assert service._live_results == {}
    assert service._requests == {}
    assert service._controls == {}
    assert service._background == {}
    assert emitted == []
    with sqlite3.connect(service.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_running_write_failure_does_not_publish_or_start(tmp_path, monkeypatch):
    service = _service(tmp_path)
    original_save = service.store.save
    running_attempts = 0
    start_callbacks = []
    emitted = []

    def permanent_running_failure(request, result):
        nonlocal running_attempts
        if result.lifecycle_status == "running":
            running_attempts += 1
            raise sqlite3.OperationalError("running write failure")
        return original_save(request, result)

    monkeypatch.setattr(service.store, "save", permanent_running_failure)
    service._emit = lambda name, data: emitted.append((name, data))
    queued = service.submit(_request(), on_start=start_callbacks.append)
    await _join_background(service, queued.execution_id)

    assert running_attempts == 3
    assert start_callbacks == []
    assert not any(name == "execution_running" for name, _ in emitted)
    assert not any(name in TERMINAL_EVENTS for name, _ in emitted)
    assert service.get(queued.execution_id).lifecycle_status == "queued"
    assert service.store.get(queued.execution_id).lifecycle_status == "queued"
    assert queued.execution_id not in service._controls
    assert queued.execution_id not in service._background


@pytest.mark.asyncio
async def test_running_callback_cannot_mutate_authoritative_snapshot(tmp_path):
    service = _service(tmp_path)

    def mutate_observation(observed):
        observed.lifecycle_status = "cancelled"
        observed.status = "cancelled"
        observed.cancellation_reason = "observer tried to author lifecycle truth"

    returned = await service.execute(
        _request(),
        execution_id="o" * 32,
        on_running=mutate_observation,
    )

    assert returned.result.lifecycle_status == "completed"
    assert service.get("o" * 32).lifecycle_status == "completed"
    assert service.store.get("o" * 32).lifecycle_status == "completed"


@pytest.mark.asyncio
async def test_cancellation_during_async_running_callback_is_durably_interrupted(
    tmp_path,
):
    service = _service(tmp_path)
    callback_entered = asyncio.Event()
    callback_release = asyncio.Event()
    emitted = []
    service._emit = lambda name, data: emitted.append((name, data))

    async def blocked_callback(observed):
        assert observed.lifecycle_status == "running"
        callback_entered.set()
        await callback_release.wait()

    execution_id = "b" * 32
    task = asyncio.create_task(
        service.execute(
            _request(),
            execution_id=execution_id,
            on_running=blocked_callback,
        )
    )
    await callback_entered.wait()
    task.cancel()
    returned = await task

    assert returned.result.lifecycle_status == "interrupted"
    assert service.get(execution_id).lifecycle_status == "interrupted"
    assert service.store.get(execution_id).lifecycle_status == "interrupted"
    assert [name for name, _ in emitted if name in TERMINAL_EVENTS] == [
        "execution_interrupted"
    ]
    assert execution_id not in service._controls
    assert execution_id not in service._background


@pytest.mark.asyncio
async def test_background_terminal_persistence_error_is_not_reclassified_or_callbacked(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path)
    original_save = service.store.save
    terminal_attempts = []
    callbacks = []
    emitted = []

    def permanent_terminal_failure(request, result):
        if result.lifecycle_status in TERMINAL_LIFECYCLES:
            terminal_attempts.append(result.lifecycle_status)
            raise sqlite3.OperationalError("terminal write failure")
        return original_save(request, result)

    monkeypatch.setattr(service.store, "save", permanent_terminal_failure)
    service._emit = lambda name, data: emitted.append((name, data))
    queued = service.submit(_request(), on_complete=callbacks.append)
    await _join_background(service, queued.execution_id)

    assert terminal_attempts == ["completed", "completed", "completed"]
    assert callbacks == []
    assert not any(name in TERMINAL_EVENTS for name, _ in emitted)
    assert service.get(queued.execution_id).lifecycle_status == "running"
    assert service.store.get(queued.execution_id).lifecycle_status == "running"
    assert queued.execution_id not in service._controls
    assert queued.execution_id not in service._background


@pytest.mark.asyncio
async def test_cancellation_persistence_failure_does_not_claim_cancellation(
    tmp_path,
    monkeypatch,
):
    started = asyncio.Event()
    release = asyncio.Event()
    service = _service(tmp_path, BlockingDagStrategy(started, release))
    original_save = service.store.save
    cancellation_attempts = 0
    callbacks = []
    emitted = []

    def permanent_cancellation_failure(request, result):
        nonlocal cancellation_attempts
        if result.lifecycle_status == "cancelled":
            cancellation_attempts += 1
            raise sqlite3.OperationalError("cancellation write failure")
        return original_save(request, result)

    monkeypatch.setattr(service.store, "save", permanent_cancellation_failure)
    service._emit = lambda name, data: emitted.append((name, data))
    queued = service.submit(_request(), on_complete=callbacks.append)
    task = service._background[queued.execution_id]
    await started.wait()
    monkeypatch.setattr(routes_executions, "get_execution_service", lambda: service)

    with pytest.raises(HTTPException) as raised:
        await routes_executions.cancel_execution(queued.execution_id)

    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "execution_persistence_unavailable"
    assert cancellation_attempts == 3
    assert callbacks == []
    assert not any(name == "execution_cancelled" for name, _ in emitted)
    assert service.get(queued.execution_id).lifecycle_status == "running"
    assert service.store.get(queued.execution_id).lifecycle_status == "running"

    release.set()
    await asyncio.shield(task)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_post_commit_dispatcher_cancel_failure_still_cancels_local_task(
    tmp_path,
    monkeypatch,
):
    started = asyncio.Event()
    release = asyncio.Event()
    service = _service(tmp_path, BlockingDagStrategy(started, release))
    callbacks = []
    emitted = []
    post_commit_order = []

    def capture_event(name, data):
        emitted.append((name, data))
        if name == "execution_cancelled":
            assert service._controls[queued.execution_id].cancel_event.is_set()
            assert service.store.get(queued.execution_id).lifecycle_status == "cancelled"
            post_commit_order.append("event")

    service._emit = capture_event
    queued = service.submit(_request(), on_complete=callbacks.append)
    task = service._background[queued.execution_id]
    await started.wait()

    def fail_dispatcher_cleanup(*_args, **_kwargs):
        post_commit_order.append("dispatcher")
        raise sqlite3.OperationalError("dispatcher cleanup failure")

    monkeypatch.setattr(
        "execution.service.Dispatcher.cancel_execution",
        fail_dispatcher_cleanup,
    )
    private_reason = "private operator prompt copied into a cancellation reason"
    result = await service.cancel(queued.execution_id, private_reason)

    assert result.lifecycle_status == "cancelled"
    assert result.cancellation_reason == private_reason
    assert service.get(queued.execution_id).lifecycle_status == "cancelled"
    assert service.store.get(queued.execution_id).lifecycle_status == "cancelled"
    assert task.done()
    assert [name for name, _ in emitted if name in TERMINAL_EVENTS] == [
        "execution_cancelled"
    ]
    assert post_commit_order == ["dispatcher", "event"]
    assert private_reason not in repr(emitted)
    assert len(callbacks) == 1
    assert callbacks[0].result.lifecycle_status == "cancelled"
    assert queued.execution_id not in service._live_results
    assert queued.execution_id not in service._requests


@pytest.mark.asyncio
async def test_callback_metadata_persistence_failure_preserves_committed_terminal(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path)
    original_save = service.store.save
    metadata_attempts = 0
    callback_lifecycles = []
    emitted = []

    def fail_callback_metadata(request, result):
        nonlocal metadata_attempts
        if result.telemetry.get("completion_callback_failed"):
            metadata_attempts += 1
            raise sqlite3.OperationalError("callback metadata write failure")
        return original_save(request, result)

    def broken_callback(run):
        callback_lifecycles.append(run.result.lifecycle_status)
        raise RuntimeError("callback failed")

    monkeypatch.setattr(service.store, "save", fail_callback_metadata)
    service._emit = lambda name, data: emitted.append((name, data))
    queued = service.submit(_request(), on_complete=broken_callback)
    await _join_background(service, queued.execution_id)

    live = service.get(queued.execution_id)
    durable = service.store.get(queued.execution_id)
    assert callback_lifecycles == ["completed"]
    assert metadata_attempts == 3
    assert live.lifecycle_status == "completed"
    assert durable.lifecycle_status == "completed"
    assert not live.telemetry.get("completion_callback_failed")
    assert not durable.telemetry.get("completion_callback_failed")
    assert not any(error.code == "completion_callback_failed" for error in live.errors)
    assert [name for name, _ in emitted if name in TERMINAL_EVENTS] == [
        "execution_completed"
    ]


@pytest.mark.asyncio
async def test_event_publication_failure_preserves_committed_terminal(tmp_path):
    service = _service(tmp_path)
    execution_id = "e" * 32
    attempted_terminal_events = []

    def fail_terminal_event(name, data):
        if name in TERMINAL_EVENTS:
            attempted_terminal_events.append(name)
            raise sqlite3.OperationalError("event write failure")

    service._emit = fail_terminal_event
    returned = await service.execute(_request(), execution_id=execution_id)

    live = service.get(execution_id)
    durable = service.store.get(execution_id)
    assert attempted_terminal_events == ["execution_completed"]
    assert returned.result.lifecycle_status == "completed"
    assert live.lifecycle_status == "completed"
    assert durable.lifecycle_status == "completed"
    assert returned.result.model_dump(mode="json") == live.model_dump(mode="json")
    assert returned.result.model_dump(mode="json") == durable.model_dump(mode="json")
    assert execution_id not in service._live_results
    assert execution_id not in service._requests


@pytest.mark.asyncio
async def test_uncommitted_terminal_artifacts_remain_unpublished_after_reconciliation(
    tmp_path,
    monkeypatch,
):
    artifact_root = tmp_path / "artifact-output"
    service = _service(tmp_path, ArtifactDagStrategy(artifact_root))
    original_save = service.store.save
    execution_id = "a" * 32

    def permanent_terminal_failure(request, result):
        if result.lifecycle_status in TERMINAL_LIFECYCLES:
            raise sqlite3.OperationalError("terminal write failure")
        return original_save(request, result)

    monkeypatch.setattr(service.store, "save", permanent_terminal_failure)
    with pytest.raises(TerminalPersistenceError):
        await service.execute(_request(), execution_id=execution_id)

    staged_manifest = service.artifacts.get_manifest(execution_id)
    assert staged_manifest.integrity_mode == "sealed"
    assert staged_manifest.manifest_hash
    assert service.store.get(execution_id).sealed_manifest_hash is None

    shares = ShareStore(service.store.path)
    created_share = shares.create(
        execution_id,
        CreateExecutionShareV1(allow_artifact_download=True),
    )
    monkeypatch.setattr(routes_access, "get_execution_service", lambda: service)
    monkeypatch.setattr(routes_access, "get_artifact_store", lambda: service.artifacts)
    monkeypatch.setattr(routes_access, "get_share_store", lambda: shares)

    with pytest.raises(ArtifactNotFound):
        routes_access._committed_artifact_manifest(execution_id)
    with pytest.raises(ArtifactNotFound):
        routes_access._public_share_manifest(created_share.token)
    public_running = await routes_access.public_execution_share(
        created_share.token,
        Response(),
    )
    assert public_running.lifecycle_status == "running"
    assert public_running.output_preview == ""
    assert public_running.produced_files == []

    assert service.reconcile_after_restart("artifact-failure-restart") == [execution_id]
    assert service.get(execution_id).lifecycle_status == "interrupted"
    with pytest.raises(ArtifactNotFound):
        routes_access._committed_artifact_manifest(execution_id)
    with pytest.raises(ArtifactNotFound):
        routes_access._public_share_manifest(created_share.token)
    public_interrupted = await routes_access.public_execution_share(
        created_share.token,
        Response(),
    )
    assert public_interrupted.lifecycle_status == "interrupted"
    assert public_interrupted.output_preview == ""
    assert public_interrupted.produced_files == []


@pytest.mark.asyncio
async def test_cancellation_during_artifact_finalization_commits_interruption(
    tmp_path,
    monkeypatch,
):
    artifact_root = tmp_path / "cancelled-artifact-output"
    service = _service(tmp_path, ArtifactDagStrategy(artifact_root))
    original_seal = service.artifacts.seal_manifest
    seal_entered = ThreadEvent()
    seal_release = ThreadEvent()
    emitted = []
    service._emit = lambda name, data: emitted.append((name, data))

    def blocked_seal(execution_id):
        seal_entered.set()
        assert seal_release.wait(timeout=5)
        return original_seal(execution_id)

    monkeypatch.setattr(service.artifacts, "seal_manifest", blocked_seal)
    execution_id = "f" * 32
    task = asyncio.create_task(service.execute(_request(), execution_id=execution_id))
    assert await asyncio.to_thread(seal_entered.wait, 5)
    task.cancel()
    seal_release.set()
    returned = await task

    assert returned.result.lifecycle_status == "interrupted"
    assert service.get(execution_id).lifecycle_status == "interrupted"
    assert service.store.get(execution_id).lifecycle_status == "interrupted"
    assert service.store.get(execution_id).sealed_manifest_hash is None
    assert [name for name, _ in emitted if name in TERMINAL_EVENTS] == [
        "execution_interrupted"
    ]
    assert execution_id not in service._controls
    assert execution_id not in service._background


def test_reconciliation_refreshes_stale_live_snapshot_before_event(tmp_path):
    service = _service(tmp_path)
    request = _request()
    execution_id = "r" * 32
    queued = service._new_result(request, execution_id, None, "queued")
    queued.lifecycle_status = "queued"
    service.store.create(request, queued)
    service._remember(request, queued)
    observed_during_event = []

    def capture_event(name, data):
        if name == "execution_interrupted":
            observed_during_event.append(
                service.get(data["execution_id"]).lifecycle_status
            )

    service._emit = capture_event
    assert service.reconcile_after_restart("stale-live-restart") == [execution_id]

    assert observed_during_event == ["interrupted"]
    assert service.get(execution_id).lifecycle_status == "interrupted"
    assert service.store.get(execution_id).lifecycle_status == "interrupted"
    assert execution_id not in service._live_results
    assert execution_id not in service._requests


def test_terminal_execution_rejects_stale_nonterminal_overwrite(tmp_path):
    service = _service(tmp_path)
    request = _request()
    execution_id = "t" * 32
    running = service._new_result(request, execution_id, None, "running")
    running.lifecycle_status = "running"
    service.store.create(request, running)
    completed = running.model_copy(deep=True)
    completed.status = "completed"
    completed.lifecycle_status = "completed"
    completed.validation_outcome = "passed"
    completed.assurance_level = "structural"
    completed.completed_at = completed.created_at
    service.store.save(request, completed)

    with pytest.raises(ExecutionTransitionConflictError) as raised:
        service.store.save(request, running)

    assert raised.value.current == "completed"
    assert raised.value.attempted == "running"
    durable = service.store.get(execution_id)
    assert durable.lifecycle_status == "completed"
    assert durable.model_dump(mode="json") == completed.model_dump(mode="json")


def test_legacy_lifecycle_events_omit_prompt_and_generated_text(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        routes_pitch,
        "_emit",
        lambda event_type, data: emitted.append((event_type, data)),
    )
    callbacks = routes_pitch._callbacks(
        "private requester prompt",
        "trace-sanitized-events",
        "job-sanitized-events",
    )

    callbacks["on_plan"](
        [
            {
                "id": 1,
                "title": "private generated title",
                "prompt": "private generated subtask prompt",
            }
        ]
    )
    callbacks["on_build"](
        {"id": 1, "title": "private generated title"},
        "private generated output",
    )

    assert emitted == [
        (
            "plan",
            {
                "trace_id": "trace-sanitized-events",
                "job_id": "job-sanitized-events",
                "subtask_count": 1,
            },
        ),
        (
            "build",
            {
                "trace_id": "trace-sanitized-events",
                "job_id": "job-sanitized-events",
                "subtask_id": 1,
            },
        ),
    ]


def test_legacy_callback_event_failure_is_non_authoritative(monkeypatch):
    def fail_event_write(event_type, data):
        raise sqlite3.OperationalError(f"event write failed: {event_type}")

    monkeypatch.setattr(routes_pitch, "_emit", fail_event_write)
    callbacks = routes_pitch._callbacks(
        "private requester prompt",
        "trace-event-failure",
        "job-event-failure",
    )

    callbacks["on_plan"]([{"id": 1, "title": "private generated title"}])
    callbacks["on_build"](
        {"id": 1, "title": "private generated title"},
        "private generated output",
    )
    callbacks["on_review_start"]()


@pytest.mark.asyncio
async def test_post_terminal_legacy_event_failure_does_not_mask_response(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path)
    attempted_events = []

    def fail_only_complete(event_type, data):
        attempted_events.append(event_type)
        if event_type == "complete":
            raise sqlite3.OperationalError("post-terminal event write failed")

    monkeypatch.setattr(routes_pitch, "_check_pitch_key", lambda request: None)
    monkeypatch.setattr(routes_pitch, "_check_rate_limit", lambda request: 99)
    monkeypatch.setattr(routes_pitch, "get_execution_service", lambda: service)
    monkeypatch.setattr(routes_pitch, "_emit", fail_only_complete)

    payload = await routes_pitch.pitch(
        routes_pitch.PitchRequest(
            task="Return the durable result despite telemetry failure",
            strategy="dag",
        ),
        Request({"type": "http", "headers": [], "client": ("testclient", 50000)}),
        Response(),
    )

    execution_id = payload["execution_id"]
    assert attempted_events == ["pitch", "complete"]
    assert payload["lifecycle_status"] == "completed"
    assert service.get(execution_id).lifecycle_status == "completed"
    assert service.store.get(execution_id).lifecycle_status == "completed"


@pytest.mark.asyncio
async def test_sync_pitch_initial_persistence_failure_emits_no_acceptance_event(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path)
    emitted = []

    def fail_running_write(request, result):
        assert result.lifecycle_status == "running"
        raise sqlite3.OperationalError("initial running write failed")

    monkeypatch.setattr(service.store, "save", fail_running_write)
    monkeypatch.setattr(service, "_emit", lambda name, data: emitted.append(name))
    monkeypatch.setattr(routes_pitch, "_emit", lambda name, data: emitted.append(name))
    monkeypatch.setattr(routes_pitch, "_check_pitch_key", lambda request: None)
    monkeypatch.setattr(routes_pitch, "_check_rate_limit", lambda request: 99)
    monkeypatch.setattr(routes_pitch, "get_execution_service", lambda: service)

    with pytest.raises(HTTPException) as raised:
        await routes_pitch.pitch(
            routes_pitch.PitchRequest(
                task="Do not acknowledge an uncommitted execution",
                strategy="dag",
            ),
            Request(
                {"type": "http", "headers": [], "client": ("testclient", 50000)}
            ),
            Response(),
        )

    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "execution_persistence_unavailable"
    assert emitted == []
    assert service._live_results == {}
    assert service._controls == {}
    assert service._background == {}


@pytest.mark.asyncio
async def test_reloaded_legacy_job_recovers_canonical_execution_metadata(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path)
    request = _request()
    job_id = "job_reloaded_canonical_metadata"
    execution_id = "j" * 32
    queued = service._new_result(request, execution_id, job_id, "queued")
    queued.lifecycle_status = "queued"
    service.store.create(request, queued)
    service.store.reconcile_nonterminal("legacy-metadata-restart")
    routes_pitch.jobs[job_id] = {
        "job_id": job_id,
        "task": request.task,
        "status": "interrupted",
        "submitted_at": queued.created_at,
        "finished_at": None,
    }
    monkeypatch.setattr(routes_pitch, "get_execution_service", lambda: service)

    try:
        detail = await routes_pitch.get_job(job_id)
        listing = await routes_pitch.list_jobs(limit=100)
        summary = next(item for item in listing["jobs"] if item["job_id"] == job_id)

        assert detail["execution_id"] == execution_id
        assert detail["strategy_requested"] == request.strategy
        assert detail["strategy_selected"] == "dag"
        assert detail["selector_reason"]
        assert summary["execution_id"] == execution_id
        assert summary["strategy_selected"] == "dag"
    finally:
        routes_pitch.jobs.pop(job_id, None)


@pytest.mark.asyncio
async def test_legacy_mirror_write_failure_keeps_last_durable_in_memory_state(
    tmp_path,
    monkeypatch,
):
    service = _service(tmp_path)
    request = _request()
    job_id = "job_durable_mirror_fault"
    queued = service._new_result(request, "m" * 32, job_id, "queued")
    queued.lifecycle_status = "queued"
    captured = {}
    durable_jobs = []

    class CapturingService:
        def submit(self, submitted_request, **kwargs):
            assert submitted_request == request
            captured.update(kwargs)
            return queued

    def fail_terminal_mirror_write(job):
        if job["status"] in {"running", "complete"}:
            raise sqlite3.OperationalError("legacy mirror write failure")
        durable_jobs.append(dict(job))

    routes_pitch.jobs[job_id] = {
        "job_id": job_id,
        "task": request.task,
        "project_id": None,
        "status": "queued",
        "submitted_at": queued.created_at,
        "result": None,
        "error": None,
        "trace_id": "trace-durable-mirror",
    }
    monkeypatch.setattr(routes_pitch, "get_execution_service", lambda: CapturingService())
    monkeypatch.setattr(routes_pitch, "_db_write_job", fail_terminal_mirror_write)

    try:
        await routes_pitch._run_job(
            job_id,
            request.task,
            trace_id="trace-durable-mirror",
            canonical=request,
        )
        baseline = dict(routes_pitch.jobs[job_id])
        assert durable_jobs[-1] == baseline

        running = queued.model_copy(
            update={"status": "running", "lifecycle_status": "running"}
        )
        with pytest.raises(ExecutionPersistenceError) as running_error:
            captured["on_start"](running)
        assert running_error.value.phase == "legacy_mirror_running"
        assert routes_pitch.jobs[job_id] == baseline

        completed = queued.model_copy(
            update={
                "status": "completed",
                "lifecycle_status": "completed",
                "completed_at": queued.created_at,
            }
        )
        with pytest.raises(ExecutionPersistenceError) as terminal_error:
            await captured["on_complete"](
                ServiceExecution(result=completed, legacy_payload={})
            )
        assert terminal_error.value.phase == "legacy_mirror_terminal"
        assert routes_pitch.jobs[job_id] == baseline
    finally:
        routes_pitch.jobs.pop(job_id, None)
