"""Canonical execution service shared by REST, CLI, MCP, and legacy adapters."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import orchestrator
import server_state
from execution.contracts import ExecutionRequestV1, ExecutionResultV1
from execution.dispatch import Dispatcher, PlacementUnavailable, select_placement
from execution.persistence import ExecutionStore
from execution.registry import StrategyRegistry, StrategySelector
from execution.strategies import DagStrategy, EnsembleStrategy, StrategyContext
from execution.validators import ValidatorRegistry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ServiceExecution:
    result: ExecutionResultV1
    legacy_payload: dict[str, Any]


class ExecutionService:
    def __init__(
        self,
        store: ExecutionStore | None = None,
        registry: StrategyRegistry | None = None,
        selector: StrategySelector | None = None,
        validators: ValidatorRegistry | None = None,
    ):
        self.store = store or ExecutionStore()
        self.registry = registry or self._default_registry()
        self.selector = selector or StrategySelector()
        self.validators = validators or ValidatorRegistry.default()
        self._background: set[asyncio.Task] = set()

    @staticmethod
    def _default_registry() -> StrategyRegistry:
        registry = StrategyRegistry()
        registry.register(DagStrategy())
        registry.register(EnsembleStrategy())
        return registry

    @staticmethod
    def _emit(event_type: str, data: dict[str, Any]) -> None:
        server_state._emit(event_type, data)

    def _new_result(
        self,
        request: ExecutionRequestV1,
        execution_id: str,
        job_id: str | None,
        status: str,
        created_at: str | None = None,
    ) -> ExecutionResultV1:
        selection = self.selector.select(request)
        strategy = self.registry.get(selection.selected)
        return ExecutionResultV1(
            execution_id=execution_id,
            job_id=job_id,
            status=status,
            task=request.task,
            project_id=request.project_id,
            strategy_requested=request.strategy,
            strategy_selected=selection.selected,
            strategy_version=strategy.version,
            strategy_options=selection.options.model_dump(mode="json"),
            selector_reason=selection.reason,
            selector_version=selection.selector_version,
            placement_requested=request.placement,
            created_at=created_at or _now(),
        )

    async def execute(
        self,
        request: ExecutionRequestV1,
        *,
        execution_id: str | None = None,
        job_id: str | None = None,
        created_at: str | None = None,
        callbacks: dict[str, Any] | None = None,
        dag_runner: Callable[..., Any] | None = None,
    ) -> ServiceExecution:
        execution_id = execution_id or uuid.uuid4().hex
        result = self._new_result(request, execution_id, job_id, "running", created_at)
        result.started_at = _now()
        started = time.perf_counter()
        self.store.save(request, result)

        selection = self.selector.select(request)
        strategy = self.registry.get(selection.selected)
        try:
            placement = select_placement(request)
            result.placement_selected = placement.selected
            result.fallback_reason = placement.fallback_reason
            self.store.save(request, result)

            def emit(event_type: str, data: dict[str, Any]) -> None:
                self._emit(event_type, {"execution_id": execution_id, **data})

            context = StrategyContext(
                execution_id=execution_id,
                placement=placement,
                dispatcher=Dispatcher(emit=emit),
                validators=self.validators,
                emit=emit,
                selector_reason=selection.reason,
                selector_version=selection.selector_version,
                callbacks=callbacks or {},
                dag_runner=dag_runner or orchestrator.run_pipeline,
            )
            outcome = await strategy.execute(request, selection.options, context)

            result.status = outcome.status
            result.execution_units = outcome.execution_units
            result.candidates = outcome.candidates
            result.winning_candidate = outcome.winning_candidate
            result.winner_selection_explanation = outcome.winner_selection_explanation
            result.validation_evidence = outcome.validation_evidence
            result.review_metadata = outcome.review_metadata
            result.revision_metadata = outcome.revision_metadata
            result.produced_files = outcome.produced_files
            result.output_reference = outcome.output_reference
            result.output_preview = outcome.output_preview
            result.errors = outcome.errors

            nodes = sorted({item.node_id for item in context.dispatch_results if item.node_id})
            result.participating_nodes = nodes
            fallbacks = [item.fallback_reason for item in context.dispatch_results if item.fallback_reason]
            if placement.fallback_reason:
                fallbacks.insert(0, placement.fallback_reason)
            if fallbacks:
                result.fallback_reason = "; ".join(dict.fromkeys(fallbacks))[:1000]
            if context.dispatch_results and all(item.placement == "local" for item in context.dispatch_results):
                result.placement_selected = "local"
            result.credit_records = outcome.telemetry.pop("credit_records", [])
            result.telemetry = {
                **outcome.telemetry,
                "placement_reason": placement.reason,
                "fallback_count": len(fallbacks),
                "participating_node_count": len(nodes),
                "observed_compute_ms": sum(item.duration_ms for item in context.dispatch_results),
                "retry_count": max(
                    0,
                    sum(item.attempt_count for item in context.dispatch_results) - len(context.dispatch_results),
                ),
            }
            legacy = dict(outcome.legacy_payload)
        except PlacementUnavailable as exc:
            result.status = "failed"
            result.errors = [{"code": "placement_unavailable", "message": str(exc), "retryable": True}]
            legacy = {}
        except Exception as exc:
            result.status = "failed"
            result.errors = [
                {
                    "code": "execution_failed",
                    "message": f"{type(exc).__name__}: {exc}"[:500],
                    "retryable": False,
                }
            ]
            legacy = {}

        result.completed_at = _now()
        result.duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        result.telemetry = {**result.telemetry, "total_duration_ms": result.duration_ms}
        # Assignment validation is intentionally disabled on the wire models so
        # strategy adapters can assemble results efficiently. Re-validate once
        # at the service boundary so persistence and every caller always see
        # fully typed protocol objects rather than nested dictionaries.
        result = ExecutionResultV1.model_validate(dict(result.__dict__))
        self.store.save(request, result)
        self._emit(
            "execution_completed" if result.status in ("completed", "unverified") else "execution_failed",
            {"execution_id": execution_id, "status": result.status},
        )
        return ServiceExecution(result=result, legacy_payload=legacy)

    def submit(
        self,
        request: ExecutionRequestV1,
        *,
        job_id: str | None = None,
        callbacks: dict[str, Any] | None = None,
        dag_runner: Callable[..., Any] | None = None,
        on_complete: Callable[[ServiceExecution], Any] | None = None,
    ) -> ExecutionResultV1:
        execution_id = uuid.uuid4().hex
        created_at = _now()
        queued = self._new_result(request, execution_id, job_id, "queued", created_at)
        self.store.save(request, queued)
        self._emit(
            "execution_created",
            {"execution_id": execution_id, "job_id": job_id, "protocol_version": "1"},
        )
        self._emit(
            "strategy_selected",
            {
                "execution_id": execution_id,
                "strategy_requested": queued.strategy_requested,
                "strategy_selected": queued.strategy_selected,
                "selector_reason": queued.selector_reason,
            },
        )

        async def run() -> None:
            completed = await self.execute(
                request,
                execution_id=execution_id,
                job_id=job_id,
                created_at=created_at,
                callbacks=callbacks,
                dag_runner=dag_runner,
            )
            if on_complete:
                value = on_complete(completed)
                if inspect.isawaitable(value):
                    await value

        task = asyncio.get_running_loop().create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return queued

    def get(self, execution_id: str) -> ExecutionResultV1 | None:
        return self.store.get(execution_id)


_SERVICE: ExecutionService | None = None


def get_execution_service() -> ExecutionService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = ExecutionService()
    return _SERVICE
