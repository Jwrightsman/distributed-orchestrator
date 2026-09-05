"""Dispatcher consumes accepted receipts, never arbitrary task-id output."""

import asyncio
import time

import pytest

import server_state as state
from execution.contracts import ExecutionRequestV1
from execution.dispatch import Dispatcher, ExecutionUnit, PlacementDecision
from node_capabilities import (
    NodeCapabilityDescriptorV1,
    NodeCapabilitySnapshotStore,
    capability_descriptor_digest,
)
from node_enrollments import NodeEnrollmentStore
from tests.deadline_guards import await_condition


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


def _descriptor_binding(enrollment_id: str) -> tuple[str, str]:
    descriptor = NodeCapabilityDescriptorV1.model_validate(
        {
            "executor": {"kind": "ollama", "worker_protocol_version": "1"},
            "models": [{"provider": "ollama", "name": "qwen3.5:4b"}],
            "hardware": {
                "architecture": "x86_64",
                "logical_cpu_count": 4,
                "total_memory_bytes": 8 * 1024**3,
            },
            "features": ["code"],
            "limits": {
                "max_concurrent_execution_units": 1,
                "max_output_bytes": 1_048_576,
            },
            "isolation": {"kind": "none"},
        }
    )
    NodeCapabilitySnapshotStore(state._DB_PATH).remember(enrollment_id, descriptor)
    return descriptor.descriptor_version, capability_descriptor_digest(descriptor)


async def _wait_for_queue() -> dict:
    # 0.5s of polling used to end in "distributed task was not queued", which
    # a loaded machine could reach without anything being wrong with queueing.
    await await_condition(
        lambda: state.task_queue, what="the distributed task to be queued"
    )
    return state.task_queue[0]


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


@pytest.mark.asyncio
async def test_reclaimed_attempt_reports_real_retry_and_reassignment_counts():
    enrolled = NodeEnrollmentStore(state._DB_PATH).bootstrap(
        node_id="worker",
        credential="dispatch-test-enrollment-credential-with-enough-entropy",
    )
    descriptor_version, descriptor_hash = _descriptor_binding(
        enrolled.record.enrollment_id
    )
    dispatcher = Dispatcher()
    running = asyncio.create_task(
        dispatcher._distributed(
            _unit(),
            _request(),
            "e" * 32,
            "ensemble",
            PlacementDecision("distributed", "test", qualifying_nodes=("worker",)),
            deadline_monotonic=time.monotonic() + 5,
        )
    )
    queued = await _wait_for_queue()
    with state._task_queue_lock:
        state.task_queue.remove(queued)
        queued["selected_model"] = {
            "provider": "ollama",
            "name": "qwen3.5:4b",
            "digest": None,
        }
        first_issued = time.time()
        state.attempt_store.issue(
            queued,
            assigned_node_id="worker",
            assigned_enrollment_id=enrolled.record.enrollment_id,
            assigned_credential_version=enrolled.record.credential_version,
            assigned_session_id="session-first",
            assigned_descriptor_version=descriptor_version,
            assigned_descriptor_hash=descriptor_hash,
            attempt_id="attempt-first",
            nonce="nonce-first",
            issued_at=first_issued,
            lease_expires_at=first_issued + 30,
        )
        assert state.attempt_store.transition_active(
            attempt_id="attempt-first",
            state="reclaimed",
            reason="worker lease reclaimed",
        )
        second_issued = time.time()
        queued.update(
            {
                "assigned_to": "worker",
                "assigned_enrollment_id": enrolled.record.enrollment_id,
                "assigned_credential_version": enrolled.record.credential_version,
                "assigned_session_id": "session-second",
                "assigned_at": second_issued,
                "attempt_id": "attempt-second",
                "nonce": "nonce-second",
                "lease_expires_at": second_issued + 30,
            }
        )
        state.attempt_store.issue(
            queued,
            assigned_node_id="worker",
            assigned_enrollment_id=enrolled.record.enrollment_id,
            assigned_credential_version=enrolled.record.credential_version,
            assigned_session_id="session-second",
            assigned_descriptor_version=descriptor_version,
            assigned_descriptor_hash=descriptor_hash,
            attempt_id="attempt-second",
            nonce="nonce-second",
            issued_at=second_issued,
            lease_expires_at=second_issued + 30,
        )
        state.task_inflight[queued["task_id"]] = queued

    settled = state.attempt_store.settle(
        task_id=queued["task_id"],
        node_id="worker",
        output="accepted output",
        error=None,
        elapsed_seconds=1,
        contract_version="1",
        attempt_id="attempt-second",
        nonce="nonce-second",
        execution_id=queued["execution_id"],
        execution_unit_id=queued["execution_unit_id"],
        execution_unit_kind=queued["execution_unit_kind"],
        session_id="session-second",
        enrollment_id=enrolled.record.enrollment_id,
        credential_version=enrolled.record.credential_version,
    )
    state.accepted_result_broker.publish(settled.receipt)
    result = await running

    assert result.attempt_count == 2
    assert result.retry_count == 1
    assert result.reassignment_count == 1
    assert result.enrollment_id == enrolled.record.enrollment_id
    assert result.session_id == "session-second"
