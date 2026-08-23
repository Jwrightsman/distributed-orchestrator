"""Canonical execution service shared by REST, CLI, MCP, and legacy adapters."""

from __future__ import annotations

import asyncio
import copy
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
from execution.idempotency import SubmissionIdentity
from execution.persistence import (
    ExecutionStore,
    ExecutionTransitionConflictError,
    IdempotencyConflictError,
    SubmissionConsistencyError,
)
from execution.registry import StrategyRegistry, StrategySelector
from execution.strategies import DagStrategy, EnsembleStrategy, StrategyContext
from execution.validators import ValidatorRegistry

logger = logging.getLogger("mycelium.execution")

_REQUIRED_PERSISTENCE_ATTEMPTS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ServiceExecution:
    result: ExecutionResultV1
    legacy_payload: dict[str, Any]


@dataclass(frozen=True)
class SubmittedExecution:
    result: ExecutionResultV1
    replayed: bool


class ExecutionPersistenceError(RuntimeError):
    """Required execution state could not be committed within finite retries."""

    def __init__(self, execution_id: str, phase: str, attempts: int):
        super().__init__(f"required execution persistence failed during {phase}")
        self.execution_id = execution_id
        self.phase = phase
        self.attempts = attempts


class TerminalPersistenceError(ExecutionPersistenceError):
    """A terminal snapshot was not durably committed."""


@dataclass
class ExecutionControl:
    execution_id: str
    request: ExecutionRequestV1
    deadline_monotonic: float
    cancel_event: asyncio.Event
    result: ExecutionResultV1
    terminal_committed: bool = False
    terminal_event_emitted: bool = False


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
    def _snapshot_request(request: ExecutionRequestV1) -> ExecutionRequestV1:
        """Detach durable work from a caller-owned mutable model instance."""

        return ExecutionRequestV1.model_validate(request.model_dump(mode="python"))

    @staticmethod
    def _emit(event_type: str, data: dict[str, Any]) -> None:
        server_state._emit(event_type, data)

    def _safe_emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Keep telemetry failure from changing already committed lifecycle truth."""

        try:
            self._emit(event_type, data)
        except Exception as exc:
            logger.error(
                "event publication failed event_type=%s error_type=%s",
                event_type,
                type(exc).__name__,
            )

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

    def _evict_terminal_cache(
        self,
        execution_id: str,
        result: ExecutionResultV1 | None = None,
    ) -> None:
        """Drop redundant terminal snapshots after post-commit observers finish.

        SQLite is authoritative once a terminal snapshot commits.  Keeping the
        same request and result in these process-local maps forever makes every
        completed execution permanent resident memory.  Nonterminal snapshots
        remain cached so active work and persistence-failure boundaries retain
        their last published state.
        """

        terminal = result or self._live_results.get(execution_id)
        if terminal is None:
            try:
                terminal = self.store.get(execution_id)
            except Exception as exc:
                logger.error(
                    "terminal cache inspection failed execution_id=%s error_type=%s",
                    execution_id,
                    type(exc).__name__,
                )
                return
        if terminal.lifecycle_status not in {
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        }:
            return
        self._live_results.pop(execution_id, None)
        self._requests.pop(execution_id, None)

    def _commit_snapshot(
        self,
        request: ExecutionRequestV1,
        result: ExecutionResultV1,
        *,
        phase: str,
        terminal: bool = False,
        create: bool = False,
    ) -> ExecutionResultV1:
        """Validate, commit, and only then publish one authoritative snapshot."""

        snapshot = ExecutionResultV1.model_validate(dict(result.__dict__))
        last_error: Exception | None = None
        for attempt in range(1, _REQUIRED_PERSISTENCE_ATTEMPTS + 1):
            try:
                if create:
                    self.store.create(request, snapshot)
                else:
                    self.store.save(request, snapshot)
            except ExecutionTransitionConflictError as exc:
                logger.error(
                    "stale execution transition rejected execution_id=%s "
                    "phase=%s current=%s attempted=%s",
                    snapshot.execution_id,
                    phase,
                    exc.current,
                    exc.attempted,
                )
                error_type = (
                    TerminalPersistenceError if terminal else ExecutionPersistenceError
                )
                raise error_type(snapshot.execution_id, phase, attempt) from exc
            except Exception as exc:
                last_error = exc
                logger.error(
                    "required execution persistence failed execution_id=%s phase=%s "
                    "attempt=%s error_type=%s",
                    snapshot.execution_id,
                    phase,
                    attempt,
                    type(exc).__name__,
                )
                continue
            self._remember(request, snapshot)
            return snapshot

        error_type = TerminalPersistenceError if terminal else ExecutionPersistenceError
        raise error_type(
            snapshot.execution_id,
            phase,
            _REQUIRED_PERSISTENCE_ATTEMPTS,
        ) from last_error

    def _commit_submission(
        self,
        request: ExecutionRequestV1,
        identity: SubmissionIdentity,
        result_factory: Callable[[], ExecutionResultV1],
    ):
        queued_result: ExecutionResultV1 | None = None

        def stable_result_factory() -> ExecutionResultV1:
            """Allocate at most one candidate identity across persistence retries."""

            nonlocal queued_result
            if queued_result is None:
                queued_result = result_factory()
            return queued_result

        last_error: Exception | None = None
        for attempt in range(1, _REQUIRED_PERSISTENCE_ATTEMPTS + 1):
            try:
                return self.store.create_or_replay_submission(
                    request,
                    identity,
                    stable_result_factory,
                )
            except (IdempotencyConflictError, SubmissionConsistencyError):
                raise
            except Exception as exc:
                last_error = exc
                logger.error(
                    "required submission persistence failed phase=queued attempt=%s "
                    "error_type=%s",
                    attempt,
                    type(exc).__name__,
                )
        raise ExecutionPersistenceError(
            "unallocated",
            "queued_submission",
            _REQUIRED_PERSISTENCE_ATTEMPTS,
        ) from last_error

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
        _defer_terminal_cache_eviction: bool = False,
    ) -> ServiceExecution:
        request = self._snapshot_request(request)
        self.validate_request(request)
        execution_id = execution_id or uuid.uuid4().hex
        registered_current_task = False
        current_task = asyncio.current_task()
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
            control.request = request
        try:
            result = self._commit_snapshot(
                request,
                result,
                phase="running",
            )
        except ExecutionPersistenceError:
            self._controls.pop(execution_id, None)
            raise
        control.result = result
        self._controls[execution_id] = control
        if current_task is not None and execution_id not in self._background:
            self._background[execution_id] = current_task
            registered_current_task = True
        self._safe_emit(
            "execution_running",
            {"execution_id": execution_id, "lifecycle_status": "running"},
        )

        try:
            if on_running:
                try:
                    value = on_running(result.model_copy(deep=True))
                    if inspect.isawaitable(value):
                        await value
                except Exception as exc:
                    logger.error(
                        "execution start callback failed execution_id=%s error_type=%s",
                        execution_id,
                        type(exc).__name__,
                    )
                    self._safe_emit(
                        "execution_callback_failed",
                        {
                            "execution_id": execution_id,
                            "stage": "start",
                            "error": type(exc).__name__,
                        },
                    )

            selection = self.selector.select(request)
            strategy = self.registry.get(selection.selected)
            placement = select_placement(request)
            result.placement_selected = placement.selected
            result.placement_planned = placement.selected
            result.fallback_reason = placement.fallback_reason
            result = self._commit_snapshot(
                request,
                result,
                phase="placement_progress",
            )
            control.result = result

            def emit(event_type: str, data: dict[str, Any]) -> None:
                if event_type == "attempt_started":
                    attempt_starts.append(
                        (str(data.get("unit_id", "unknown")), str(data.get("placement", "local")))
                    )
                self._safe_emit(event_type, {"execution_id": execution_id, **data})

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
                    logger.warning(
                        "artifact registration failed execution_id=%s error_type=%s",
                        execution_id,
                        type(exc).__name__,
                    )
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
        except ExecutionPersistenceError:
            self._controls.pop(execution_id, None)
            if registered_current_task:
                self._background.pop(execution_id, None)
            raise
        except asyncio.TimeoutError:
            control.cancel_event.set()
            try:
                Dispatcher.cancel_execution(
                    execution_id,
                    reason="execution deadline exceeded",
                )
            except Exception as exc:
                logger.error(
                    "deadline dispatcher cancellation failed execution_id=%s "
                    "error_type=%s",
                    execution_id,
                    type(exc).__name__,
                )
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
        except asyncio.CancelledError:
            if control.terminal_committed:
                result = control.result.model_copy(deep=True)
                self._controls.pop(execution_id, None)
                if registered_current_task:
                    self._background.pop(execution_id, None)
                if not _defer_terminal_cache_eviction:
                    self._evict_terminal_cache(execution_id, result)
                return ServiceExecution(result=result, legacy_payload={})
            try:
                Dispatcher.cancel_execution(execution_id, reason="execution cancelled")
            except Exception as exc:
                logger.error(
                    "interruption dispatcher cancellation failed execution_id=%s "
                    "error_type=%s",
                    execution_id,
                    type(exc).__name__,
                )
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
            except asyncio.CancelledError:
                if control.terminal_committed:
                    result = control.result.model_copy(deep=True)
                    self._controls.pop(execution_id, None)
                    if registered_current_task:
                        self._background.pop(execution_id, None)
                    if not _defer_terminal_cache_eviction:
                        self._evict_terminal_cache(execution_id, result)
                    return ServiceExecution(result=result, legacy_payload={})
                try:
                    Dispatcher.cancel_execution(
                        execution_id,
                        reason="execution cancelled during artifact finalization",
                    )
                except Exception as exc:
                    logger.error(
                        "artifact-finalization cancellation failed execution_id=%s "
                        "error_type=%s",
                        execution_id,
                        type(exc).__name__,
                    )
                if control.cancel_event.is_set():
                    result.lifecycle_status = "cancelled"
                    result.status = "cancelled"
                    result.cancellation_requested = True
                    result.cancellation_reason = (
                        result.cancellation_reason or "cancelled by caller"
                    )
                    result.validation_outcome = "not_run"
                    result.assurance_level = "unverified"
                    result.errors = []
                else:
                    result.lifecycle_status = "interrupted"
                    result.status = "failed"
                    result.interruption_reason = (
                        "execution task was interrupted during artifact finalization"
                    )
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
            except ArtifactError as exc:
                result.artifact_integrity_mode = "invalid"
                logger.warning(
                    "could not seal terminal artifacts execution_id=%s error_type=%s",
                    execution_id,
                    type(exc).__name__,
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
            logger.error(
                "execution result normalization failed execution_id=%s error_type=%s",
                execution_id,
                type(exc).__name__,
            )
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
        try:
            result = self._commit_snapshot(
                request,
                result,
                phase="terminal",
                terminal=True,
            )
        except TerminalPersistenceError:
            self._controls.pop(execution_id, None)
            if registered_current_task:
                self._background.pop(execution_id, None)
            raise
        control.result = result
        control.terminal_committed = True
        error_codes = {error.code for error in result.errors}
        terminal_event = (
            "execution_completed"
            if result.lifecycle_status == "completed"
            else "execution_cancelled"
            if result.lifecycle_status == "cancelled"
            else "execution_interrupted"
            if result.lifecycle_status == "interrupted"
            else "execution_timed_out"
            if "execution_timeout" in error_codes
            else "execution_failed"
        )
        self._safe_emit(
            terminal_event,
            {
                "execution_id": execution_id,
                "status": result.status,
                "lifecycle_status": result.lifecycle_status,
                "assurance_level": result.assurance_level,
            },
        )
        control.terminal_event_emitted = True
        if request.project_id and legacy:
            # Project memory is a compatibility publication surface. Keep its
            # task/output/files staged until the canonical terminal snapshot
            # and normal lifecycle event above are published. The helper is
            # best-effort and creates no serialized pending payload.
            try:
                await orchestrator.commit_project_iteration(
                    request.project_id,
                    legacy,
                    request.task,
                )
            except asyncio.CancelledError:
                # A post-commit mirror cannot unwind canonical completion.
                pass
            except Exception as exc:
                logger.error(
                    "project memory publication failed execution_id=%s error_type=%s",
                    execution_id,
                    type(exc).__name__,
                )
        self._controls.pop(execution_id, None)
        if registered_current_task:
            self._background.pop(execution_id, None)
        if not _defer_terminal_cache_eviction:
            self._evict_terminal_cache(execution_id, result)
        return ServiceExecution(result=result, legacy_payload=legacy)

    def _activate_committed_submission(
        self,
        request: ExecutionRequestV1,
        queued: ExecutionResultV1,
        *,
        callbacks: dict[str, Any] | None = None,
        dag_runner: Callable[..., Any] | None = None,
        on_start: Callable[[ExecutionResultV1], Any] | None = None,
        on_complete: Callable[[ServiceExecution], Any] | None = None,
    ) -> ExecutionResultV1:
        """Publish and schedule a queued snapshot that SQLite already committed."""

        execution_id = queued.execution_id
        self._remember(request, queued)
        control = ExecutionControl(
            execution_id=execution_id,
            request=request,
            deadline_monotonic=time.monotonic() + request.timeout_seconds,
            cancel_event=asyncio.Event(),
            result=queued,
        )
        self._controls[execution_id] = control
        self._safe_emit(
            "execution_created",
            {
                "execution_id": execution_id,
                "job_id": queued.job_id,
                "protocol_version": "1",
            },
        )
        self._safe_emit(
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
                    job_id=queued.job_id,
                    created_at=queued.created_at,
                    callbacks=callbacks,
                    dag_runner=dag_runner,
                    control=control,
                    on_running=on_start,
                    _defer_terminal_cache_eviction=True,
                )
            except asyncio.CancelledError:
                raise
            except ExecutionPersistenceError as exc:
                logger.error(
                    "background execution persistence unavailable execution_id=%s "
                    "phase=%s attempts=%s",
                    execution_id,
                    exc.phase,
                    exc.attempts,
                )
                self._controls.pop(execution_id, None)
                return
            except Exception as exc:
                logger.error(
                    "background execution crashed execution_id=%s error_type=%s",
                    execution_id,
                    type(exc).__name__,
                )
                try:
                    durable = self.store.get(execution_id)
                except Exception as read_error:
                    logger.error(
                        "background crash state read failed execution_id=%s error_type=%s",
                        execution_id,
                        type(read_error).__name__,
                    )
                    self._controls.pop(execution_id, None)
                    return
                if durable is not None and durable.lifecycle_status in {
                    "completed",
                    "failed",
                    "cancelled",
                    "interrupted",
                }:
                    completed = ServiceExecution(result=durable, legacy_payload={})
                else:
                    crashed = control.result.model_copy(deep=True)
                    crashed.lifecycle_status = "interrupted"
                    crashed.status = "failed"
                    crashed.interruption_reason = (
                        f"background execution crashed: {type(exc).__name__}"
                    )
                    crashed.interrupted_at = _now()
                    crashed.completed_at = crashed.interrupted_at
                    crashed.retryable = True
                    crashed.errors = [
                        StructuredErrorV1(
                            code="background_execution_crashed",
                            message=(
                                f"{type(exc).__name__}: background execution crashed"
                            )[:500],
                            retryable=True,
                        )
                    ]
                    try:
                        crashed = self._commit_snapshot(
                            request,
                            crashed,
                            phase="background_crash_terminal",
                            terminal=True,
                        )
                    except ExecutionPersistenceError as persistence_error:
                        logger.error(
                            "background crash persistence unavailable execution_id=%s "
                            "phase=%s attempts=%s",
                            execution_id,
                            persistence_error.phase,
                            persistence_error.attempts,
                        )
                        self._controls.pop(execution_id, None)
                        return
                    control.result = crashed
                    control.terminal_committed = True
                    self._safe_emit(
                        "execution_interrupted",
                        {"execution_id": execution_id, "reason": "background_crash"},
                    )
                    control.terminal_event_emitted = True
                    completed = ServiceExecution(result=crashed, legacy_payload={})
            finally:
                self._controls.pop(execution_id, None)

            if on_complete:
                try:
                    value = on_complete(
                        ServiceExecution(
                            result=completed.result.model_copy(deep=True),
                            legacy_payload=copy.deepcopy(completed.legacy_payload),
                        )
                    )
                    if inspect.isawaitable(value):
                        await value
                except Exception as exc:
                    logger.error(
                        "completion callback failed execution_id=%s error_type=%s",
                        execution_id,
                        type(exc).__name__,
                    )
                    updated = completed.result.model_copy(deep=True)
                    updated.errors.append(
                        StructuredErrorV1(
                            code="completion_callback_failed",
                            message=f"{type(exc).__name__}: completion callback failed"[:500],
                            retryable=True,
                        )
                    )
                    updated.telemetry["completion_callback_failed"] = True
                    try:
                        updated = self._commit_snapshot(
                            request,
                            updated,
                            phase="completion_callback_metadata",
                            terminal=True,
                        )
                    except ExecutionPersistenceError as persistence_error:
                        logger.error(
                            "callback metadata persistence unavailable execution_id=%s "
                            "phase=%s attempts=%s",
                            execution_id,
                            persistence_error.phase,
                            persistence_error.attempts,
                        )
                        return
                    completed = ServiceExecution(
                        result=updated,
                        legacy_payload=completed.legacy_payload,
                    )
                    self._safe_emit(
                        "execution_callback_failed",
                        {
                            "execution_id": execution_id,
                            "stage": "completion",
                            "error": type(exc).__name__,
                        },
                    )

        task = asyncio.get_running_loop().create_task(run())
        self._background[execution_id] = task

        def done(completed_task: asyncio.Task) -> None:
            self._background.pop(execution_id, None)
            if not completed_task.cancelled():
                try:
                    completed_task.result()
                except Exception as exc:
                    logger.error(
                        "background task ended unexpectedly execution_id=%s error_type=%s",
                        execution_id,
                        type(exc).__name__,
                    )
            self._evict_terminal_cache(execution_id)

        task.add_done_callback(done)
        return queued

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
        request = self._snapshot_request(request)
        self.validate_request(request)
        execution_id = uuid.uuid4().hex
        queued = self._new_result(request, execution_id, job_id, "queued", _now())
        queued.lifecycle_status = "queued"
        queued = self._commit_snapshot(
            request,
            queued,
            phase="queued_submission",
            create=True,
        )
        return self._activate_committed_submission(
            request,
            queued,
            callbacks=callbacks,
            dag_runner=dag_runner,
            on_start=on_start,
            on_complete=on_complete,
        )

    def submit_idempotent(
        self,
        request: ExecutionRequestV1,
        identity: SubmissionIdentity,
        *,
        job_id: str | None = None,
        callbacks: dict[str, Any] | None = None,
        dag_runner: Callable[..., Any] | None = None,
        on_start: Callable[[ExecutionResultV1], Any] | None = None,
        on_complete: Callable[[ServiceExecution], Any] | None = None,
    ) -> SubmittedExecution:
        request = self._snapshot_request(request)
        self.validate_request(request)

        def queued_factory() -> ExecutionResultV1:
            queued = self._new_result(
                request,
                uuid.uuid4().hex,
                job_id,
                "queued",
                _now(),
            )
            queued.lifecycle_status = "queued"
            return queued

        committed = self._commit_submission(request, identity, queued_factory)
        if committed.replayed:
            return SubmittedExecution(
                result=committed.result.model_copy(deep=True),
                replayed=True,
            )
        queued = self._activate_committed_submission(
            request,
            committed.result,
            callbacks=callbacks,
            dag_runner=dag_runner,
            on_start=on_start,
            on_complete=on_complete,
        )
        return SubmittedExecution(result=queued, replayed=False)

    def get(self, execution_id: str) -> ExecutionResultV1 | None:
        result = self._live_results.get(execution_id) or self.store.get(execution_id)
        return result.model_copy(deep=True) if result is not None else None

    async def cancel(self, execution_id: str, reason: str = "cancelled by caller") -> ExecutionResultV1 | None:
        result = self.get(execution_id)
        if result is None:
            return None
        if result.lifecycle_status in ("completed", "failed", "cancelled", "interrupted"):
            self._evict_terminal_cache(execution_id, result)
            return result
        control = self._controls.get(execution_id)
        request = (
            self._requests.get(execution_id)
            or (control.request if control else None)
            or self.store.get_request(execution_id)
        )
        if request is None:
            raise ExecutionPersistenceError(execution_id, "cancellation_request", 0)

        cancelled = result.model_copy(deep=True)
        cancelled.cancellation_requested = True
        cancelled.cancellation_requested_at = _now()
        cancelled.cancellation_reason = reason[:500]
        cancelled.lifecycle_status = "cancelled"
        cancelled.status = "cancelled"
        cancelled.completed_at = _now()
        cancelled.cancelled_at = cancelled.completed_at
        cancelled.validation_outcome = "not_run"
        cancelled.assurance_level = "unverified"
        cancelled = self._commit_snapshot(
            request,
            cancelled,
            phase="cancellation_terminal",
            terminal=True,
        )
        if control is not None:
            control.result = cancelled
            control.terminal_committed = True
        if control is not None:
            control.cancel_event.set()
        try:
            Dispatcher.cancel_execution(execution_id, reason=reason)
        except Exception as exc:
            logger.error(
                "post-commit dispatcher cancellation failed execution_id=%s "
                "error_type=%s",
                execution_id,
                type(exc).__name__,
            )
        self._safe_emit(
            "execution_cancelled",
            {
                "execution_id": execution_id,
                # The authenticated durable result retains the operator's
                # bounded reason. Persisted lifecycle telemetry uses a fixed
                # code so caller text cannot become a prompt/output log.
                "reason": "cancellation_requested",
            },
        )
        if control is not None:
            control.terminal_event_emitted = True
        task = self._background.get(execution_id)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error(
                    "post-commit task cancellation failed execution_id=%s error_type=%s",
                    execution_id,
                    type(exc).__name__,
                )
        self._controls.pop(execution_id, None)
        self._evict_terminal_cache(execution_id, cancelled)
        return self.get(execution_id) or cancelled

    def reconcile_after_restart(self, restart_marker: str | None = None) -> list[str]:
        changed = self.store.reconcile_nonterminal(restart_marker)
        for execution_id in changed:
            result = self.store.get(execution_id)
            request = self.store.get_request(execution_id)
            if result is not None:
                self._live_results[execution_id] = result.model_copy(deep=True)
            if result is not None and request is not None:
                self._requests[execution_id] = request
            self._safe_emit(
                "execution_interrupted",
                {"execution_id": execution_id, "reason": "coordinator_restart", "retryable": True},
            )
            if result is not None:
                self._evict_terminal_cache(execution_id, result)
        return changed


_SERVICE: ExecutionService | None = None


def get_execution_service() -> ExecutionService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = ExecutionService()
    return _SERVICE
