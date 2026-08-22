"""Placement selection and shared local/distributed execution-unit dispatch."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import server_state as state
from execution.contracts import ExecutionRequestV1, SelectedPlacementV1
from execution.attempts import ReceiptBindingError


class PlacementUnavailable(RuntimeError):
    pass


class QueueFull(RuntimeError):
    pass


@dataclass(frozen=True)
class PlacementDecision:
    selected: SelectedPlacementV1
    reason: str
    fallback_reason: str | None = None
    qualifying_nodes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionUnit:
    unit_id: str
    kind: str
    title: str
    prompt: str
    system: str
    depends_on: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DispatchResult:
    unit: ExecutionUnit
    status: str
    output: str = ""
    placement: SelectedPlacementV1 = "local"
    node_id: str | None = None
    fallback_reason: str | None = None
    error: str | None = None
    duration_ms: int = 0
    attempt_count: int = 0
    retry_count: int = 0
    reassignment_count: int = 0


def qualifying_nodes(request: ExecutionRequestV1) -> list[str]:
    required = set(request.requirements.required_capabilities)
    approved = set(request.requirements.approved_node_ids)
    matches: list[str] = []
    now = time.time()
    for node_id, node in state.nodes.items():
        if node_id in state.node_blacklist and state.node_blacklist[node_id] > now:
            continue
        if request.confidentiality == "approved_nodes" and node_id not in approved:
            continue
        if required and not required.issubset(set(node.get("capabilities", []))):
            continue
        matches.append(node_id)
    return sorted(matches)


def select_placement(request: ExecutionRequestV1) -> PlacementDecision:
    nodes = tuple(qualifying_nodes(request))
    if request.placement == "local":
        return PlacementDecision("local", "Selected local because the caller explicitly requested it.")

    if request.confidentiality == "local_only":
        return PlacementDecision(
            "local",
            "Selected local because confidentiality='local_only' prohibits contributor-node dispatch.",
        )

    if request.placement == "distributed":
        if nodes:
            return PlacementDecision(
                "distributed",
                f"Selected distributed because {len(nodes)} qualifying node(s) are available.",
                qualifying_nodes=nodes,
            )
        reason = "No connected node satisfies the execution requirements and confidentiality policy."
        if request.requirements.allow_local_fallback:
            return PlacementDecision(
                "local",
                "Selected local using the documented distributed-placement fallback.",
                fallback_reason=reason,
            )
        raise PlacementUnavailable(reason)

    if nodes:
        return PlacementDecision(
            "distributed",
            f"Auto placement selected distributed because {len(nodes)} qualifying node(s) are available.",
            qualifying_nodes=nodes,
        )
    return PlacementDecision(
        "local",
        "Auto placement selected local because no qualifying contributor node is available.",
    )


LocalExecutor = Callable[[], Awaitable[str]]
EventEmitter = Callable[[str, dict[str, Any]], None]
_verification_tasks: set[asyncio.Task] = set()


class Dispatcher:
    """Execute strategy units without embedding strategy reduction logic."""

    def __init__(self, emit: EventEmitter | None = None):
        self.emit = emit or (lambda *_: None)

    async def execute(
        self,
        unit: ExecutionUnit,
        request: ExecutionRequestV1,
        execution_id: str,
        strategy: str,
        decision: PlacementDecision,
        local_executor: LocalExecutor,
        deadline_monotonic: float | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> DispatchResult:
        deadline = deadline_monotonic or (time.monotonic() + request.timeout_seconds)
        if decision.selected == "local":
            result = await self._local(
                unit,
                local_executor,
                request.max_output_bytes,
                deadline_monotonic=deadline,
                cancel_event=cancel_event,
            )
            result.fallback_reason = decision.fallback_reason
            if decision.fallback_reason:
                self.emit(
                    "placement_fallback",
                    {
                        "execution_id": execution_id,
                        "unit_id": unit.unit_id,
                        "reason": decision.fallback_reason,
                        "placement_selected": "local",
                    },
                )
            return result

        remote_options: dict[str, Any] = {}
        if deadline_monotonic is not None:
            remote_options["deadline_monotonic"] = deadline
        if cancel_event is not None:
            remote_options["cancel_event"] = cancel_event
        remote = await self._distributed(
            unit,
            request,
            execution_id,
            strategy,
            decision,
            **remote_options,
        )
        if remote.status == "completed":
            if unit.kind == "dag_subtask":
                self._maybe_verify_remote(
                    unit,
                    request,
                    execution_id,
                    strategy,
                    decision,
                    remote,
                    deadline_monotonic=deadline_monotonic,
                    cancel_event=cancel_event,
                )
            return remote
        if not request.requirements.allow_local_fallback:
            return remote

        reason = remote.error or "distributed execution failed"
        self.emit(
            "placement_fallback",
            {
                "execution_id": execution_id,
                "unit_id": unit.unit_id,
                "reason": reason,
                "placement_selected": "local",
            },
        )
        local = await self._local(
            unit,
            local_executor,
            request.max_output_bytes,
            deadline_monotonic=deadline,
            cancel_event=cancel_event,
        )
        local.fallback_reason = reason
        local.attempt_count += remote.attempt_count
        local.retry_count += remote.retry_count
        local.reassignment_count += remote.reassignment_count
        return local

    def _maybe_verify_remote(
        self,
        unit: ExecutionUnit,
        request: ExecutionRequestV1,
        execution_id: str,
        strategy: str,
        decision: PlacementDecision,
        primary: DispatchResult,
        deadline_monotonic: float | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """Sample a second node without adding its latency to the deliverable."""
        state._refresh_verify_rate()
        alternatives = tuple(node for node in decision.qualifying_nodes if node != primary.node_id)
        pool = state.verification_pool
        if not alternatives or not pool.should_verify(len(decision.qualifying_nodes)):
            return

        duplicate = ExecutionUnit(
            unit_id=f"{unit.unit_id}-verification-{uuid.uuid4().hex[:8]}",
            kind=unit.kind,
            title=f"Verification: {unit.title}",
            prompt=unit.prompt,
            system=unit.system,
            depends_on=unit.depends_on,
            metadata={**unit.metadata, "verification_of": unit.unit_id},
        )
        verification_decision = PlacementDecision(
            selected="distributed",
            reason="Sampled verification on a node other than the primary worker.",
            qualifying_nodes=alternatives,
        )

        async def collect() -> None:
            remote_options: dict[str, Any] = {}
            if deadline_monotonic is not None:
                remote_options["deadline_monotonic"] = deadline_monotonic
            if cancel_event is not None:
                remote_options["cancel_event"] = cancel_event
            secondary = await self._distributed(
                duplicate,
                request,
                execution_id,
                strategy,
                verification_decision,
                **remote_options,
            )
            if secondary.status != "completed" or not secondary.node_id:
                return
            verdict = pool.record_comparison(
                primary.node_id or "unknown",
                primary.output,
                secondary.node_id,
                secondary.output,
            )
            self.emit(
                "verification",
                {
                    "execution_id": execution_id,
                    "unit_id": unit.unit_id,
                    **verdict,
                },
            )

        task = asyncio.create_task(collect())
        _verification_tasks.add(task)
        task.add_done_callback(_verification_tasks.discard)

    async def _local(
        self,
        unit: ExecutionUnit,
        executor: LocalExecutor,
        max_output_bytes: int,
        *,
        deadline_monotonic: float | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> DispatchResult:
        started = time.perf_counter()
        work: asyncio.Task | None = None
        cancellation: asyncio.Task | None = None
        self.emit("attempt_started", {"unit_id": unit.unit_id, "placement": "local"})
        try:
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError
            remaining = (
                max(0.0, deadline_monotonic - time.monotonic())
                if deadline_monotonic is not None
                else None
            )
            if remaining is not None and remaining <= 0:
                raise TimeoutError("execution deadline exceeded before local inference")

            work = asyncio.create_task(executor())
            cancellation = (
                asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
            )
            waits = {work}
            if cancellation is not None:
                waits.add(cancellation)
            done, _ = await asyncio.wait(
                waits,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation is not None and cancellation in done:
                work.cancel()
                with suppress(asyncio.CancelledError):
                    await work
                raise asyncio.CancelledError
            if work in done:
                output = await work
            else:
                work.cancel()
                with suppress(asyncio.CancelledError):
                    await work
                raise TimeoutError("execution deadline exceeded during local inference")
            output_size = len(output.encode("utf-8")) if output else 0
            status = "completed" if output and output_size <= max_output_bytes else "failed"
            error = (
                None
                if status == "completed"
                else f"local output exceeds max_output_bytes={max_output_bytes}"
                if output_size > max_output_bytes
                else "local executor returned empty output"
            )
            if status == "failed" and output_size > max_output_bytes:
                output = ""
        except asyncio.CancelledError:
            duration = max(0, int((time.perf_counter() - started) * 1000))
            self.emit(
                "attempt_completed",
                {
                    "unit_id": unit.unit_id,
                    "placement": "local",
                    "status": "cancelled",
                    "duration_ms": duration,
                },
            )
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            duration = max(0, int((time.perf_counter() - started) * 1000))
            self.emit(
                "attempt_completed",
                {
                    "unit_id": unit.unit_id,
                    "placement": "local",
                    "status": "failed",
                    "duration_ms": duration,
                    "reason": "execution deadline exceeded",
                },
            )
            raise asyncio.TimeoutError(str(exc)) from exc
        except Exception as exc:
            output = ""
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            for pending in (work, cancellation):
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending
        duration = max(0, int((time.perf_counter() - started) * 1000))
        self.emit(
            "attempt_completed",
            {"unit_id": unit.unit_id, "placement": "local", "status": status, "duration_ms": duration},
        )
        return DispatchResult(
            unit=unit,
            status=status,
            output=output,
            placement="local",
            node_id=None,
            error=error,
            duration_ms=duration,
            attempt_count=1,
        )

    async def _distributed(
        self,
        unit: ExecutionUnit,
        request: ExecutionRequestV1,
        execution_id: str,
        strategy: str,
        decision: PlacementDecision,
        *,
        deadline_monotonic: float | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> DispatchResult:
        started = time.perf_counter()
        deadline = deadline_monotonic or (time.monotonic() + request.timeout_seconds)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError("execution deadline expired before distributed dispatch")
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError
        task_id = f"unit_{uuid.uuid4().hex}"
        contract_summary = request.output_contract.model_dump(mode="json") if request.output_contract else None
        verification_summary = request.verification.model_dump(mode="json")
        task = {
            "task_id": task_id,
            "title": unit.title,
            "prompt": unit.prompt,
            "system": unit.system,
            "contract_version": "1",
            "execution_id": execution_id,
            "strategy": strategy,
            "execution_unit_id": unit.unit_id,
            "execution_unit_kind": unit.kind,
            "output_contract": contract_summary,
            "verification_policy": verification_summary,
            "max_output_bytes": request.max_output_bytes,
            "requires": list(request.requirements.required_capabilities),
            "eligible_nodes": list(decision.qualifying_nodes),
            # The worker lease cannot outlive the caller's total execution
            # deadline. A late result is rejected by the durable attempt state.
            "lease_seconds": min(7200, max(1, int(remaining + 0.999))),
            "execution_deadline_at": time.time() + remaining,
        }
        if not state.enqueue_task(task):
            return DispatchResult(
                unit=unit,
                status="failed",
                placement="distributed",
                error="task queue is full",
                duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                attempt_count=0,
            )

        self.emit(
            "execution_unit_queued",
            {
                "execution_id": execution_id,
                "unit_id": unit.unit_id,
                "task_id": task_id,
                "placement": "distributed",
            },
        )
        receipt = None
        try:
            while receipt is None:
                try:
                    receipt = state.accepted_result_broker.get_matching(
                        task_id=task_id,
                        execution_id=execution_id,
                        execution_unit_id=unit.unit_id,
                        execution_unit_kind=unit.kind,
                        contract_version="1",
                    )
                except ReceiptBindingError as exc:
                    self._cancel(task_id, reason=str(exc))
                    attempts = state.attempt_store.count_attempts(task_id)
                    return DispatchResult(
                        unit=unit,
                        status="failed",
                        placement="distributed",
                        error=str(exc),
                        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                        attempt_count=attempts,
                        retry_count=max(0, attempts - 1),
                        reassignment_count=max(0, attempts - 1),
                    )
                if receipt is not None:
                    break
                if cancel_event and cancel_event.is_set():
                    self._cancel(task_id, reason="execution cancellation requested")
                    raise asyncio.CancelledError
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    cancelled = self._cancel(task_id, reason="execution deadline exceeded")
                    if not cancelled:
                        # Settlement and cancellation can meet at the deadline.
                        # If settlement won the database race, consume it.
                        receipt = state.accepted_result_broker.get_matching(
                            task_id=task_id,
                            execution_id=execution_id,
                            execution_unit_id=unit.unit_id,
                            execution_unit_kind=unit.kind,
                            contract_version="1",
                        )
                    if receipt is None:
                        raise asyncio.TimeoutError(
                            "distributed attempt exceeded the execution deadline"
                        )
                    break
                await asyncio.sleep(min(0.05, remaining))
        except asyncio.CancelledError:
            self._cancel(task_id, reason="dispatcher task cancelled")
            raise

        # Remove only the compatibility mirror. The immutable receipt remains
        # durable and is the authority consumed above.
        state.task_results.pop(task_id, None)
        duration = max(0, int((time.perf_counter() - started) * 1000))
        output = receipt.output or ""
        error = receipt.error
        if len(output.encode("utf-8")) > request.max_output_bytes:
            output = ""
            error = f"worker output exceeds max_output_bytes={request.max_output_bytes}"
        attempts = max(1, state.attempt_store.count_attempts(task_id))
        return DispatchResult(
            unit=unit,
            status="completed" if output and not error else "failed",
            output=output,
            placement="distributed",
            node_id=receipt.assigned_node_id,
            error=str(error)[:500] if error else (None if output else "worker returned empty output"),
            duration_ms=duration,
            attempt_count=attempts,
            retry_count=max(0, attempts - 1),
            reassignment_count=max(0, attempts - 1),
        )

    @staticmethod
    def _cancel(task_id: str, *, reason: str) -> bool:
        """Remove queued work or durably cancel one active attempt."""
        with state._task_queue_lock:
            if state.remove_queued_task(task_id):
                return True
            task = state.task_inflight.get(task_id)
            if not task:
                return False
            attempt_id = task.get("attempt_id")
            changed = bool(
                attempt_id
                and state.attempt_store.transition_active(
                    attempt_id=attempt_id,
                    state="cancelled",
                    reason=reason,
                )
            )
            record = state.attempt_store.get(attempt_id) if attempt_id else None
            terminal_without_receipt = bool(
                record
                and record.state
                in {"expired", "reclaimed", "cancelled", "superseded", "interrupted"}
            )
            if changed or not attempt_id or terminal_without_receipt:
                state.task_inflight.pop(task_id, None)
            return changed or not attempt_id or terminal_without_receipt

    @staticmethod
    def cancel_execution(execution_id: str, *, reason: str = "execution cancelled") -> int:
        """Cancel all queued and leased units belonging to an execution."""
        with state._task_queue_lock:
            queued = [
                task.get("task_id")
                for task in state.task_queue
                if task.get("execution_id") == execution_id
            ]
            for task_id in queued:
                if task_id:
                    state.remove_queued_task(task_id)
            active_task_ids = state.attempt_store.cancel_execution(execution_id, reason)
            for task_id in active_task_ids:
                state.task_inflight.pop(task_id, None)
        return len(queued) + len(active_task_ids)
