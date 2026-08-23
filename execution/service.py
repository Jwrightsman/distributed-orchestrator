"""Canonical execution service shared by REST, CLI, MCP, and legacy adapters."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import orchestrator
import server_state
from config import get as get_config
from execution.contracts import (
    CandidateSummaryV1,
    ExecutionRequestV1,
    ExecutionResultV1,
    ExecutionUnitSummaryV1,
    StructuredErrorV1,
    ValidationEvidenceV1,
)
from execution.artifacts import ArtifactError, ArtifactStore
from execution.dispatch import Dispatcher, PlacementUnavailable, select_placement
from execution.persistence import ExecutionStore
from execution.registry import StrategyRegistry, StrategySelector
from execution.strategies import DagStrategy, EnsembleStrategy, StrategyContext
from execution.validators import ValidatorRegistry

logger = logging.getLogger("mycelium.execution")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ServiceExecution:
    result: ExecutionResultV1
    legacy_payload: dict[str, Any]


@dataclass
class ExecutionControl:
    execution_id: str
    request: ExecutionRequestV1
    deadline_monotonic: float
    cancel_event: asyncio.Event
    result: ExecutionResultV1


class ExecutionService:
    def __init__(
        self,
        store: ExecutionStore | None = None,
        registry: StrategyRegistry | None = None,
        selector: StrategySelector | None = None,
        validators: ValidatorRegistry | None = None,
        artifacts: ArtifactStore | None = None,
    ):
        self.store = store or ExecutionStore()
        self.registry = registry or self._default_registry()
        self.selector = selector or StrategySelector()
        self.validators = validators or ValidatorRegistry.default()
        self.artifacts = artifacts or ArtifactStore(self.store.path)
        self._background: dict[str, asyncio.Task] = {}
        self._controls: dict[str, ExecutionControl] = {}
        self._live_results: dict[str, ExecutionResultV1] = {}
        self._requests: dict[str, ExecutionRequestV1] = {}

    @staticmethod
    def _default_registry() -> StrategyRegistry:
        registry = StrategyRegistry()
        registry.register(DagStrategy())
        registry.register(EnsembleStrategy())
        return registry

    def validate_request(self, request: ExecutionRequestV1) -> None:
        """Validate cross-component rules that depend on strategy selection."""
        selection = self.selector.select(request)
        if request.project_id and selection.selected == "ensemble":
            raise ValueError(
                "project_id is not supported for ensemble/direct until selected-result-only memory updates exist"
            )

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
        created = created_at or _now()
        try:
            deadline_at = (datetime.fromisoformat(created) + timedelta(seconds=request.timeout_seconds)).isoformat()
        except ValueError:
            deadline_at = None
        trusted_alpha = get_config().get("deployment_mode", "local") == "trusted_alpha"
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
            created_at=created,
            deadline_at=deadline_at,
            remote_dispatch_consent=getattr(request, "remote_dispatch_consent", False),
            posthoc_verification_status=("disabled" if trusted_alpha else "not_requested"),
            posthoc_reason=(
                "sampled duplicate verification is disabled in trusted-alpha mode "
                "until it has durable post-hoc semantics"
                if trusted_alpha
                else None
            ),
        )

    def _remember(self, request: ExecutionRequestV1, result: ExecutionResultV1) -> None:
        self._requests[result.execution_id] = request
        # Never expose the mutable result object that a running strategy is
        # still assembling.  In particular, terminal lifecycle fields are set
        # before artifact sealing and result normalization finish.  Publishing
        # a deep snapshot keeps readers on the last completed persistence
        # boundary until the terminal event is ready.
        self._live_results[result.execution_id] = result.model_copy(deep=True)

    def _save(self, request: ExecutionRequestV1, result: ExecutionResultV1, *, required: bool = False) -> None:
        self._remember(request, result)
        try:
            self.store.save(request, result)
        except Exception:
            logger.exception("failed to persist execution %s", result.execution_id)
            self._emit(
                "execution_persistence_failed",
                {"execution_id": result.execution_id, "lifecycle_status": getattr(result, "lifecycle_status", result.status)},
            )
            if required:
                raise

    def _persist_terminal(self, request: ExecutionRequestV1, result: ExecutionResultV1) -> bool:
        """Retry terminal persistence so a transient write cannot strand running state."""
        for attempt in range(1, 4):
            try:
                self.store.save(request, result)
                return True
            except Exception:
                logger.exception(
                    "terminal persistence attempt %s failed for %s",
                    attempt,
                    result.execution_id,
                )
        self._emit(
            "execution_terminal_persistence_failed",
            {
                "execution_id": result.execution_id,
                "lifecycle_status": result.lifecycle_status,
                "attempts": 3,
            },
        )
        return False

    @staticmethod
    def _terminal_projection(lifecycle: str, validation_outcome: str) -> str:
        if lifecycle == "completed":
            return "completed" if validation_outcome == "passed" else "unverified"
        return "failed" if lifecycle == "interrupted" else lifecycle

    @staticmethod
    def _apply_terminal_progress(
        result: ExecutionResultV1,
        context: StrategyContext | None,
        execution_id: str,
        attempt_starts: list[tuple[str, str]],
        placement_fallback: str | None,
    ) -> None:
        """Merge completed in-process work with durable remote attempts."""
        dispatch_results = context.dispatch_results if context is not None else []
        durable = server_state.attempt_store.execution_attempt_summary(execution_id)

        observed_by_result = [
            (item, item.observed_placements or (item.placement,))
            for item in dispatch_results
        ]
        completed_local_units = {
            item.unit.unit_id
            for item, observed in observed_by_result
            if "local" in observed
        }
        started_local_units = {
            unit_id for unit_id, placement in attempt_starts if placement == "local"
        }
        unfinished_local_units = started_local_units - completed_local_units
        context_remote_attempts = sum(
            max(0, item.attempt_count - (1 if "local" in observed else 0))
            for item, observed in observed_by_result
            if "distributed" in observed
        )
        context_attempts = sum(item.attempt_count for item in dispatch_results)
        additional_remote_attempts = max(
            0,
            durable["attempt_count"] - context_remote_attempts,
        )
        context_retries = sum(item.retry_count for item in dispatch_results)
        context_reassignments = sum(item.reassignment_count for item in dispatch_results)

        observed = {
            placement
            for _, placements in observed_by_result
            for placement in placements
        }
        if started_local_units:
            observed.add("local")
        if durable["unit_count"]:
            observed.add("distributed")
        result.observed_placements = sorted(observed)
        result.units_local = len(completed_local_units | unfinished_local_units)
        completed_remote_units = sum(
            "distributed" in placements for _, placements in observed_by_result
        )
        result.units_distributed = max(completed_remote_units, durable["unit_count"])
        result.placement_observed = (
            "mixed"
            if len(observed) > 1
            else next(iter(observed))
            if observed
            else "none"
        )
        result.attempt_count = (
            context_attempts
            + len(unfinished_local_units)
            + additional_remote_attempts
        )
        result.retry_count = context_retries + max(
            0,
            durable["retry_count"] - context_retries,
        )
        result.reassignment_count = context_reassignments + max(
            0,
            durable["reassignment_count"] - context_reassignments,
        )
        result.participating_nodes = sorted(
            {item.node_id for item in dispatch_results if item.node_id}
        )
        unit_fallbacks = [
            item.fallback_reason for item in dispatch_results if item.fallback_reason
        ]
        result.fallback_count = len(unit_fallbacks) or int(bool(placement_fallback))
        reasons = list(unit_fallbacks)
        if placement_fallback:
            reasons.insert(0, placement_fallback)
        if reasons:
            result.fallback_reason = "; ".join(dict.fromkeys(reasons))[:1000]
        result.telemetry = {
            **result.telemetry,
            "observed_placements": result.observed_placements,
            "units_local": result.units_local,
            "units_distributed": result.units_distributed,
            "fallback_count": result.fallback_count,
            "attempt_count": result.attempt_count,
            "retry_count": result.retry_count,
            "reassignment_count": result.reassignment_count,
        }

    async def execute(
        self,
        request: ExecutionRequestV1,
        *,
        execution_id: str | None = None,
        job_id: str | None = None,
        created_at: str | None = None,
        callbacks: dict[str, Any] | None = None,
        dag_runner: Callable[..., Any] | None = None,
        control: ExecutionControl | None = None,
        on_running: Callable[[ExecutionResultV1], Any] | None = None,
    ) -> ServiceExecution:
        execution_id = execution_id or uuid.uuid4().hex
        registered_current_task = False
        current_task = asyncio.current_task()
        if current_task is not None and execution_id not in self._background:
            self._background[execution_id] = current_task
            registered_current_task = True
        result = self._new_result(request, execution_id, job_id, "running", created_at)
        result.lifecycle_status = "running"
        result.started_at = _now()
        started = time.perf_counter()
        progress_accounted = False
        attempt_starts: list[tuple[str, str]] = []
        if control is None:
            control = ExecutionControl(
                execution_id=execution_id,
                request=request,
                deadline_monotonic=time.monotonic() + request.timeout_seconds,
                cancel_event=asyncio.Event(),
                result=result,
            )
        else:
            control.result = result
        self._controls[execution_id] = control
        try:
            self._save(request, result, required=True)
        except Exception as exc:
            result.lifecycle_status = "interrupted"
            result.status = "failed"
            result.interruption_reason = "failed to persist running execution state"
            result.interrupted_at = _now()
            result.completed_at = result.interrupted_at
            result.retryable = True
            result.errors = [
                {
                    "code": "running_persistence_failed",
                    "message": f"{type(exc).__name__}: failed to persist running state"[:500],
                    "retryable": True,
                }
            ]
            result = ExecutionResultV1.model_validate(dict(result.__dict__))
            control.result = result
            self._persist_terminal(request, result)
            self._emit(
                "execution_interrupted",
                {
                    "execution_id": execution_id,
                    "reason": "running_persistence_failed",
                    "retryable": True,
                },
            )
            self._remember(request, result)
            self._controls.pop(execution_id, None)
            if registered_current_task:
                self._background.pop(execution_id, None)
            return ServiceExecution(result=result, legacy_payload={})

        if on_running:
            try:
                value = on_running(result)
                if inspect.isawaitable(value):
                    await value
            except Exception as exc:
                logger.exception("execution start callback failed for %s", execution_id)
                self._emit(
                    "execution_callback_failed",
                    {"execution_id": execution_id, "stage": "start", "error": type(exc).__name__},
                )

        selection = self.selector.select(request)
        strategy = self.registry.get(selection.selected)
        try:
            self.validate_request(request)
            placement = select_placement(request)
            result.placement_selected = placement.selected
            result.placement_planned = placement.selected
            result.fallback_reason = placement.fallback_reason
            self._save(request, result)

            def emit(event_type: str, data: dict[str, Any]) -> None:
                if event_type == "attempt_started":
                    attempt_starts.append(
                        (str(data.get("unit_id", "unknown")), str(data.get("placement", "local")))
                    )
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
                deadline_monotonic=control.deadline_monotonic,
                cancel_event=control.cancel_event,
                artifacts=self.artifacts,
            )
            remaining = control.deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("execution deadline expired before strategy start")

            async def run_strategy():
                semaphore = (callbacks or {}).get("execution_semaphore")
                if semaphore is None:
                    return await strategy.execute(request, selection.options, context)
                async with semaphore:
                    return await strategy.execute(request, selection.options, context)

            outcome = await asyncio.wait_for(run_strategy(), timeout=remaining)

            result.lifecycle_status = getattr(
                outcome,
                "lifecycle_status",
                "failed" if outcome.status == "failed" else "completed",
            )
            # Strategy outcomes intentionally use plain dictionaries so
            # adapters remain lightweight.  Normalize nested protocol models
            # before service logic reads their attributes.
            result.execution_units = [
                ExecutionUnitSummaryV1.model_validate(item)
                for item in outcome.execution_units
            ]
            result.candidates = [
                CandidateSummaryV1.model_validate(item)
                for item in outcome.candidates
            ]
            result.winning_candidate = outcome.winning_candidate
            result.winner_selection_explanation = outcome.winner_selection_explanation
            result.validation_evidence = [
                ValidationEvidenceV1.model_validate(item)
                for item in outcome.validation_evidence
            ]
            result.review_metadata = outcome.review_metadata
            result.revision_metadata = outcome.revision_metadata
            result.produced_files = outcome.produced_files
            result.output_reference = outcome.output_reference
            result.output_preview = outcome.output_preview
            result.errors = outcome.errors

            # Canonical artifact responses expose only authenticated API paths
            # and normalized manifest entries. Raw project_dir paths remain in
            # the authenticated legacy payload for compatibility.
            legacy = dict(outcome.legacy_payload)
            artifact_root = legacy.get("project_dir")
            result.output_reference = None
            result.produced_files = []
            for candidate in result.candidates:
                candidate.produced_files = []
            if artifact_root:
                try:
                    def build_manifest():
                        self.artifacts.register_root(
                            execution_id,
                            artifact_root,
                            strategy=selection.selected,
                            # Keep retention's durable active guard in place
                            # until hashing and result finalization complete.
                            active=True,
                        )
                        if selection.selected == "ensemble" and result.winning_candidate:
                            candidate_number = result.winning_candidate.rsplit("-", 1)[-1]
                            self.artifacts.set_manifest_prefix(
                                execution_id,
                                f"candidate_{candidate_number}",
                            )
                        return self.artifacts.seal_manifest(execution_id)

                    remaining = control.deadline_monotonic - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    manifest = await asyncio.wait_for(asyncio.to_thread(build_manifest), timeout=remaining)
                    result.produced_files = [entry.relative_path for entry in manifest.entries]
                    result.primary_deliverables = [
                        entry.relative_path
                        for entry in manifest.entries
                        if entry.role == "deliverable"
                    ]
                    result.artifact_manifest_url = (
                        f"/v1/executions/{execution_id}/artifacts?role=deliverable"
                    )
                    result.audit_manifest_url = (
                        f"/v1/executions/{execution_id}/artifacts?role=audit"
                    )
                    result.sealed_manifest_hash = manifest.manifest_hash
                    result.artifact_integrity_mode = manifest.integrity_mode
                    result.output_reference = f"/v1/executions/{execution_id}/artifacts"
                    for candidate in result.candidates:
                        candidate.produced_files = [
                            entry.relative_path
                            for entry in manifest.entries
                            if entry.source_candidate_id == candidate.candidate_id
                        ]
                except ArtifactError as exc:
                    logger.warning("artifact registration failed for %s: %s", execution_id, exc)
                    result.errors.append(
                        {
                            "code": "artifact_manifest_failed",
                            "message": str(exc)[:500],
                            "retryable": True,
                        }
                    )

            # The strategy owns the acceptance decision.  In particular, a
            # failed structural check must not be promoted to structural
            # assurance merely because that validator ran.
            result.validation_summary = outcome.validation_summary
            result.validation_outcome = outcome.validation_outcome
            result.assurance_level = outcome.assurance_level
            result.status = self._terminal_projection(
                result.lifecycle_status,
                result.validation_outcome,
            )

            nodes = sorted({item.node_id for item in context.dispatch_results if item.node_id})
            result.participating_nodes = nodes
            unit_fallbacks = [
                item.fallback_reason
                for item in context.dispatch_results
                if item.fallback_reason
            ]
            fallback_count = len(unit_fallbacks)
            if not unit_fallbacks and placement.fallback_reason:
                fallback_count = 1
            fallbacks = list(unit_fallbacks)
            if placement.fallback_reason:
                fallbacks.insert(0, placement.fallback_reason)
            if fallbacks:
                result.fallback_reason = "; ".join(dict.fromkeys(fallbacks))[:1000]
            observed = sorted(
                {
                    placement_name
                    for item in context.dispatch_results
                    for placement_name in (item.observed_placements or (item.placement,))
                }
            )
            result.observed_placements = observed
            result.units_local = sum(
                "local" in (item.observed_placements or (item.placement,))
                for item in context.dispatch_results
            )
            result.units_distributed = sum(
                "distributed" in (item.observed_placements or (item.placement,))
                for item in context.dispatch_results
            )
            if len(observed) == 1:
                result.placement_selected = observed[0]
            elif len(observed) > 1:
                # Compatibility field cannot represent mixed placement. The
                # additive observed_placements/unit counts are authoritative.
                result.placement_selected = None
            result.placement_observed = (
                "mixed" if len(observed) > 1 else observed[0] if observed else "none"
            )
            result.credit_records = outcome.telemetry.pop("credit_records", [])
            retry_count = sum(getattr(item, "retry_count", 0) for item in context.dispatch_results)
            reassignment_count = sum(getattr(item, "reassignment_count", 0) for item in context.dispatch_results)
            result.telemetry = {
                **outcome.telemetry,
                "placement_reason": placement.reason,
                "placement_requested": request.placement,
                "placement_planned": placement.selected,
                "observed_placements": observed,
                "units_local": result.units_local,
                "units_distributed": result.units_distributed,
                "fallback_count": fallback_count,
                "participating_node_count": len(nodes),
                "observed_compute_ms": sum(item.duration_ms for item in context.dispatch_results),
                "attempt_count": sum(item.attempt_count for item in context.dispatch_results),
                "retry_count": retry_count,
                "reassignment_count": reassignment_count,
            }
            result.fallback_count = fallback_count
            result.attempt_count = sum(item.attempt_count for item in context.dispatch_results)
            result.retry_count = retry_count
            result.reassignment_count = reassignment_count
            progress_accounted = True
        except asyncio.TimeoutError:
            control.cancel_event.set()
            Dispatcher.cancel_execution(execution_id, reason="execution deadline exceeded")
            result.lifecycle_status = "failed"
            result.status = "failed"
            result.validation_outcome = "not_run"
            result.assurance_level = "unverified"
            result.retryable = True
            result.errors = [
                {
                    "code": "execution_timeout",
                    "message": f"Execution exceeded its total {request.timeout_seconds}s deadline.",
                    "retryable": True,
                }
            ]
            legacy = {}
            self._emit("execution_timed_out", {"execution_id": execution_id})
        except asyncio.CancelledError:
            Dispatcher.cancel_execution(execution_id, reason="execution cancelled")
            if control.cancel_event.is_set():
                result.lifecycle_status = "cancelled"
                result.status = "cancelled"
                result.cancellation_requested = True
                result.cancellation_reason = result.cancellation_reason or "cancelled by caller"
                result.validation_outcome = "not_run"
                result.assurance_level = "unverified"
                result.errors = []
                legacy = {}
            else:
                result.lifecycle_status = "interrupted"
                result.status = "failed"
                result.interruption_reason = "execution task was interrupted"
                result.interrupted_at = _now()
                result.retryable = True
                result.errors = [
                    {
                        "code": "execution_interrupted",
                        "message": "Execution task was interrupted.",
                        "retryable": True,
                    }
                ]
                legacy = {}
        except PlacementUnavailable as exc:
            result.lifecycle_status = "failed"
            result.status = "failed"
            result.errors = [{"code": "placement_unavailable", "message": str(exc), "retryable": True}]
            legacy = {}
        except Exception as exc:
            result.lifecycle_status = "failed"
            result.status = "failed"
            result.errors = [
                {
                    "code": "execution_failed",
                    "message": f"{type(exc).__name__}: {exc}"[:500],
                    "retryable": False,
                }
            ]
            legacy = {}

        if not progress_accounted:
            self._apply_terminal_progress(
                result,
                context if "context" in locals() else None,
                execution_id,
                attempt_starts,
                placement.fallback_reason if "placement" in locals() else None,
            )

        result.completed_at = _now()
        result.duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        result.telemetry = {**result.telemetry, "total_duration_ms": result.duration_ms}
        should_clear_artifact_marker = (
            "artifact_root" in locals() and bool(artifact_root)
        ) or (
            "context" in locals() and context.artifact_root_path is not None
        )
        if should_clear_artifact_marker:
            try:
                terminal_manifest = await asyncio.to_thread(
                    self.artifacts.seal_manifest,
                    execution_id,
                )
                result.produced_files = [
                    entry.relative_path for entry in terminal_manifest.entries
                ]
                result.primary_deliverables = [
                    entry.relative_path
                    for entry in terminal_manifest.entries
                    if entry.role == "deliverable"
                ]
                result.artifact_manifest_url = (
                    f"/v1/executions/{execution_id}/artifacts?role=deliverable"
                )
                result.audit_manifest_url = (
                    f"/v1/executions/{execution_id}/artifacts?role=audit"
                )
                result.sealed_manifest_hash = terminal_manifest.manifest_hash
                result.artifact_integrity_mode = terminal_manifest.integrity_mode
                result.output_reference = f"/v1/executions/{execution_id}/artifacts"
            except ArtifactError as exc:
                result.artifact_integrity_mode = "invalid"
                logger.warning(
                    "could not seal terminal artifacts for %s: %s",
                    execution_id,
                    exc,
                )
        # Assignment validation is intentionally disabled on the wire models so
        # strategy adapters can assemble results efficiently. Re-validate once
        # at the service boundary so persistence and every caller always see
        # fully typed protocol objects rather than nested dictionaries. A bad
        # adapter result must itself become a durable terminal failure; it may
        # never escape while leaving the prior row marked running.
        try:
            result = ExecutionResultV1.model_validate(dict(result.__dict__))
        except Exception as exc:
            logger.exception("execution result normalization failed for %s", execution_id)
            fallback = self._new_result(
                request,
                execution_id,
                job_id,
                "failed",
                created_at=result.created_at,
            )
            fallback.lifecycle_status = "failed"
            fallback.started_at = result.started_at
            fallback.completed_at = _now()
            fallback.duration_ms = max(0, int((time.perf_counter() - started) * 1000))
            fallback.retryable = False
            fallback.errors = [
                {
                    "code": "result_normalization_failed",
                    "message": f"{type(exc).__name__}: strategy result violated protocol bounds"[:500],
                    "retryable": False,
                }
            ]
            result = ExecutionResultV1.model_validate(dict(fallback.__dict__))
            legacy = {}
        control.result = result
        self._persist_terminal(request, result)
        self._emit(
            "execution_completed"
            if result.lifecycle_status == "completed"
            else "execution_cancelled"
            if result.lifecycle_status == "cancelled"
            else "execution_interrupted"
            if result.lifecycle_status == "interrupted"
            else "execution_failed",
            {
                "execution_id": execution_id,
                "status": result.status,
                "lifecycle_status": result.lifecycle_status,
                "assurance_level": result.assurance_level,
            },
        )
        self._remember(request, result)
        self._controls.pop(execution_id, None)
        if registered_current_task:
            self._background.pop(execution_id, None)
        return ServiceExecution(result=result, legacy_payload=legacy)

    def submit(
        self,
        request: ExecutionRequestV1,
        *,
        job_id: str | None = None,
        callbacks: dict[str, Any] | None = None,
        dag_runner: Callable[..., Any] | None = None,
        on_start: Callable[[ExecutionResultV1], Any] | None = None,
        on_complete: Callable[[ServiceExecution], Any] | None = None,
    ) -> ExecutionResultV1:
        # Reject unsupported cross-strategy semantics before creating a job or
        # durable queued row.  All direct callers share this boundary.
        self.validate_request(request)
        execution_id = uuid.uuid4().hex
        created_at = _now()
        queued = self._new_result(request, execution_id, job_id, "queued", created_at)
        queued.lifecycle_status = "queued"
        control = ExecutionControl(
            execution_id=execution_id,
            request=request,
            deadline_monotonic=time.monotonic() + request.timeout_seconds,
            cancel_event=asyncio.Event(),
            result=queued,
        )
        self._controls[execution_id] = control
        self._save(request, queued, required=True)
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
            try:
                completed = await self.execute(
                    request,
                    execution_id=execution_id,
                    job_id=job_id,
                    created_at=created_at,
                    callbacks=callbacks,
                    dag_runner=dag_runner,
                    control=control,
                    on_running=on_start,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("background execution %s crashed", execution_id)
                crashed = control.result
                crashed.lifecycle_status = "interrupted"
                crashed.status = "failed"
                crashed.interruption_reason = f"background execution crashed: {type(exc).__name__}"
                crashed.interrupted_at = _now()
                crashed.completed_at = crashed.interrupted_at
                crashed.retryable = True
                crashed.errors = [
                    {
                        "code": "background_execution_crashed",
                        "message": f"{type(exc).__name__}: {exc}"[:500],
                        "retryable": True,
                    }
                ]
                crashed = ExecutionResultV1.model_validate(dict(crashed.__dict__))
                control.result = crashed
                self._persist_terminal(request, crashed)
                self._emit(
                    "execution_interrupted",
                    {"execution_id": execution_id, "reason": "background_crash"},
                )
                self._remember(request, crashed)
                completed = ServiceExecution(result=crashed, legacy_payload={})
            if on_complete:
                try:
                    value = on_complete(completed)
                    if inspect.isawaitable(value):
                        await value
                except Exception as exc:
                    logger.exception("completion callback failed for %s", execution_id)
                    completed.result.errors.append(
                        StructuredErrorV1(
                            code="completion_callback_failed",
                            message=f"{type(exc).__name__}: {exc}"[:500],
                            retryable=True,
                        )
                    )
                    completed.result.telemetry["completion_callback_failed"] = True
                    self._persist_terminal(request, completed.result)
                    self._emit(
                        "execution_callback_failed",
                        {"execution_id": execution_id, "stage": "completion", "error": type(exc).__name__},
                    )
                    self._remember(request, completed.result)

        task = asyncio.get_running_loop().create_task(run())
        self._background[execution_id] = task

        def done(completed_task: asyncio.Task) -> None:
            self._background.pop(execution_id, None)
            if completed_task.cancelled():
                return
            try:
                completed_task.result()
            except Exception:
                logger.exception("background execution %s crashed", execution_id)

        task.add_done_callback(done)
        return queued

    def get(self, execution_id: str) -> ExecutionResultV1 | None:
        result = self._live_results.get(execution_id) or self.store.get(execution_id)
        return result.model_copy(deep=True) if result is not None else None

    async def cancel(self, execution_id: str, reason: str = "cancelled by caller") -> ExecutionResultV1 | None:
        result = self.get(execution_id)
        if result is None:
            return None
        if result.lifecycle_status in ("completed", "failed", "cancelled", "interrupted"):
            return result
        control = self._controls.get(execution_id)
        if control:
            control.cancel_event.set()

        def mark_cancelled(target: ExecutionResultV1) -> None:
            target.cancellation_requested = True
            target.cancellation_requested_at = _now()
            target.cancellation_reason = reason[:500]
            target.lifecycle_status = "cancelled"
            target.status = "cancelled"
            target.completed_at = _now()
            target.cancelled_at = target.completed_at
            target.validation_outcome = "not_run"
            target.assurance_level = "unverified"

        mark_cancelled(result)
        if control is not None:
            mark_cancelled(control.result)
        request = self._requests.get(execution_id) or (control.request if control else None)
        if request:
            self._persist_terminal(request, result)
        Dispatcher.cancel_execution(execution_id, reason=reason)
        task = self._background.get(execution_id)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
        published = self.get(execution_id)
        if published is not None and published.lifecycle_status == "cancelled":
            return published
        self._emit("execution_cancelled", {"execution_id": execution_id, "reason": reason[:500]})
        if request:
            self._remember(request, result)
        return self.get(execution_id) or result

    def reconcile_after_restart(self, restart_marker: str | None = None) -> list[str]:
        changed = self.store.reconcile_nonterminal(restart_marker)
        for execution_id in changed:
            self._emit(
                "execution_interrupted",
                {"execution_id": execution_id, "reason": "coordinator_restart", "retryable": True},
            )
        return changed


_SERVICE: ExecutionService | None = None


def get_execution_service() -> ExecutionService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = ExecutionService()
    return _SERVICE
