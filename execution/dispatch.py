"""Placement selection and shared local/distributed execution-unit dispatch."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import server_state as state
from execution.contracts import ExecutionRequestV1, SelectedPlacementV1


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
    ) -> DispatchResult:
        if decision.selected == "local":
            result = await self._local(unit, local_executor)
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

        remote = await self._distributed(unit, request, execution_id, strategy, decision)
        if remote.status == "completed":
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
        local = await self._local(unit, local_executor)
        local.fallback_reason = reason
        local.attempt_count += remote.attempt_count
        return local

    async def _local(self, unit: ExecutionUnit, executor: LocalExecutor) -> DispatchResult:
        started = time.perf_counter()
        self.emit("attempt_started", {"unit_id": unit.unit_id, "placement": "local"})
        try:
            output = await executor()
            status = "completed" if output else "failed"
            error = None if output else "local executor returned empty output"
        except Exception as exc:
            output = ""
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"[:500]
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
    ) -> DispatchResult:
        started = time.perf_counter()
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
            "requires": list(request.requirements.required_capabilities),
            "eligible_nodes": list(decision.qualifying_nodes),
            "lease_seconds": min(7200, max(state.ATTEMPT_LEASE_SECONDS, request.timeout_seconds + 30)),
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
        deadline = time.monotonic() + request.timeout_seconds
        while task_id not in state.task_results:
            if time.monotonic() >= deadline:
                self._cancel(task_id)
                return DispatchResult(
                    unit=unit,
                    status="failed",
                    placement="distributed",
                    error=f"distributed attempt timed out after {request.timeout_seconds}s",
                    duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    attempt_count=1,
                )
            await asyncio.sleep(0.05)

        raw = state.task_results.pop(task_id)
        duration = max(0, int((time.perf_counter() - started) * 1000))
        output = raw.get("output") or ""
        error = raw.get("error")
        return DispatchResult(
            unit=unit,
            status="completed" if output and not error else "failed",
            output=output,
            placement="distributed",
            node_id=raw.get("node_id"),
            error=str(error)[:500] if error else (None if output else "worker returned empty output"),
            duration_ms=duration,
            attempt_count=1,
        )

    @staticmethod
    def _cancel(task_id: str) -> None:
        if state.remove_queued_task(task_id):
            return
        task = state.task_inflight.pop(task_id, None)
        if task and task.get("attempt_id"):
            state.remember_settlement(
                task["attempt_id"],
                {"status": "cancelled", "credits_earned": 0},
            )
