"""Dispatcher consumes accepted receipts, never arbitrary task-id output."""

import asyncio
import time

import pytest

import server_state as state
from execution.contracts import ExecutionRequestV1
from execution.dispatch import Dispatcher, ExecutionUnit, PlacementDecision


@pytest.fixture(autouse=True)
def clean_state():
    for mapping in (
        state.nodes,
        state.task_inflight,
        state.task_results,
        state.settled_attempts,
    ):
        mapping.clear()
    state.task_queue.clear()
    state.accepted_result_broker.clear()
    state._init_db()


def _request() -> ExecutionRequestV1:
    return ExecutionRequestV1(
        task="complete the unit",
        placement="distributed",
        confidentiality="trusted_guild",
        remote_dispatch_consent=True,
    )


def _unit() -> ExecutionUnit:
    return ExecutionUnit("candidate-1", "candidate", "Candidate", "prompt", "system")


async def _wait_for_queue() -> dict:
    for _ in range(100):
        if state.task_queue:
            return state.task_queue[0]
        await asyncio.sleep(0.005)
    raise AssertionError("distributed task was not queued")


@pytest.mark.asyncio
async def test_arbitrary_task_result_map_entry_never_wakes_dispatcher():
    dispatcher = Dispatcher()
    running = asyncio.create_task(
        dispatcher._distributed(
            _unit(),
            _request(),
            "e" * 32,
            "ensemble",
            PlacementDecision("distributed", "test", qualifying_nodes=("worker",)),
            deadline_monotonic=time.monotonic() + 0.15,
        )
    )
    queued = await _wait_for_queue()
    state.task_results[queued["task_id"]] = {
        "node_id": "attacker",
        "output": "plausible but unbound",
        "error": None,
    }

    with pytest.raises(asyncio.TimeoutError):
        await running
    assert state.attempt_store.get_receipt_for_task(queued["task_id"]) is None


@pytest.mark.asyncio
async def test_cancellation_removes_queued_unit():
    dispatcher = Dispatcher()
    cancelled = asyncio.Event()
    running = asyncio.create_task(
        dispatcher._distributed(
            _unit(),
            _request(),
            "e" * 32,
            "ensemble",
            PlacementDecision("distributed", "test", qualifying_nodes=("worker",)),
            deadline_monotonic=time.monotonic() + 5,
            cancel_event=cancelled,
        )
    )
    queued = await _wait_for_queue()
    cancelled.set()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert all(task["task_id"] != queued["task_id"] for task in state.task_queue)


@pytest.mark.asyncio
async def test_cancellation_marks_leased_attempt_before_removing_it():
    dispatcher = Dispatcher()
    cancelled = asyncio.Event()
    running = asyncio.create_task(
        dispatcher._distributed(
            _unit(),
            _request(),
            "e" * 32,
            "ensemble",
            PlacementDecision("distributed", "test", qualifying_nodes=("worker",)),
            deadline_monotonic=time.monotonic() + 5,
            cancel_event=cancelled,
        )
    )
    queued = await _wait_for_queue()
    with state._task_queue_lock:
        state.task_queue.remove(queued)
        now = time.time()
        queued.update({
            "assigned_to": "worker",
            "assigned_at": now,
            "attempt_id": "leased-attempt",
            "nonce": "leased-attempt-nonce",
            "lease_expires_at": now + 5,
        })
        state.attempt_store.issue(
            queued,
            assigned_node_id="worker",
            attempt_id=queued["attempt_id"],
            nonce=queued["nonce"],
            issued_at=now,
            lease_expires_at=now + 5,
        )
        state.task_inflight[queued["task_id"]] = queued
    cancelled.set()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert state.attempt_store.get("leased-attempt").state == "cancelled"
    assert queued["task_id"] not in state.task_inflight
    assert state.attempt_store.get_receipt_for_task(queued["task_id"]) is None


@pytest.mark.asyncio
async def test_local_inference_obeys_absolute_deadline():
    cancelled_inside = asyncio.Event()

    async def slow_executor():
        try:
            await asyncio.sleep(10)
        finally:
            cancelled_inside.set()

    with pytest.raises(asyncio.TimeoutError):
        await Dispatcher()._local(
            _unit(),
            slow_executor,
            1024,
            deadline_monotonic=time.monotonic() + 0.05,
        )
    assert cancelled_inside.is_set()


@pytest.mark.asyncio
async def test_local_inference_obeys_cancellation_event():
    cancellation = asyncio.Event()

    async def slow_executor():
        await asyncio.sleep(10)
        return "late"

    async def cancel_soon():
        await asyncio.sleep(0.05)
        cancellation.set()

    trigger = asyncio.create_task(cancel_soon())
    with pytest.raises(asyncio.CancelledError):
        await Dispatcher()._local(
            _unit(),
            slow_executor,
            1024,
            deadline_monotonic=time.monotonic() + 5,
            cancel_event=cancellation,
        )
    await trigger
