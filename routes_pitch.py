"""Pitch compatibility adapters and the legacy async job surface.

All three pitch endpoints construct ``ExecutionRequestV1`` and delegate to the
canonical execution service. Strategy implementations, distributed dispatch,
validation, and persistence do not live in this routing module.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError

import orchestrator
import server_state as state
from config import get as get_config
from execution.contracts import EnsembleOptionsV1, ExecutionRequestV1
from execution.service import (
    ExecutionPersistenceError,
    ServiceExecution,
    SubmissionActivationError,
    get_execution_service,
)
from execution.sharing import CreateExecutionShareV1, get_share_store
from ollama_client import generate as _generate
from server_state import (
    PitchRequest,
    _check_pitch_key,
    _check_rate_limit,
    _db_write_job,
    _emit,
    jobs,
)
from verification import verification_identity_key

# Imported as a module attribute for compatibility with tests and scripts that
# replace the legacy DAG runner. The service receives this callable explicitly.
run_pipeline = orchestrator.run_pipeline
plan = orchestrator.plan
compose_builder_prompt = orchestrator.compose_builder_prompt
# Kept as a public module hook for older in-process test/deployment shims. The
# canonical dispatcher invokes the same Ollama integration through orchestrator.
generate = _generate

router = APIRouter()

# Strong references to in-flight sampled-agreement collectors. This legacy
# diagnostic is not correctness or a routing signal; canonical output
# validation is a separate registry under execution/validators.py.
_verify_tasks: set = set()
_PUBLIC_INFERENCE_SEMAPHORE = asyncio.Semaphore(1)
_LEGACY_MIRROR_PERSISTENCE_ATTEMPTS = 3
logger = logging.getLogger("mycelium.execution.legacy")


def _persistence_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "execution_persistence_unavailable",
            "message": (
                "Required execution state could not be committed. "
                "Verify durable state before retrying."
            ),
        },
    )


def _activation_unavailable(exc: SubmissionActivationError) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "submission_activation_failed",
            "message": "The execution was recorded but could not be started.",
            "execution_id": exc.execution_id,
        },
    )


def _commit_legacy_job(job: dict[str, Any], *, phase: str) -> None:
    """Persist a legacy mirror before replacing its process-local projection."""

    last_error: Exception | None = None
    for attempt in range(1, _LEGACY_MIRROR_PERSISTENCE_ATTEMPTS + 1):
        try:
            _db_write_job(job)
            return
        except Exception as exc:
            last_error = exc
            logger.error(
                "required legacy mirror persistence failed job_id=%s phase=%s "
                "attempt=%s error_type=%s",
                job.get("job_id", "unknown"),
                phase,
                attempt,
                type(exc).__name__,
            )
    raise ExecutionPersistenceError(
        str(job.get("execution_id") or job.get("job_id") or "unknown"),
        f"legacy_mirror_{phase}",
        _LEGACY_MIRROR_PERSISTENCE_ATTEMPTS,
    ) from last_error


def _safe_legacy_emit(event_type: str, data: dict[str, Any]) -> None:
    """Keep compatibility telemetry failure from changing execution truth."""

    try:
        _emit(event_type, data)
    except Exception as exc:
        logger.error(
            "legacy event publication failed event_type=%s error_type=%s",
            event_type,
            type(exc).__name__,
        )


def _spawn_comparison(
    dup_id,
    subtask_title,
    job_id,
    trace_id,
    primary_node,
    primary_output,
    await_result,
    pool,
    *,
    primary_enrollment_id=None,
    primary_session_id=None,
):
    """Wait for a sampled duplicate in the background and record its shape."""
    async def _collect():
        try:
            dup = await await_result(dup_id, 600)
            if not dup or dup.get("error") or not dup.get("output"):
                return
            verdict = pool.record_comparison(
                primary_node, primary_output,
                dup.get("node_id", "unknown"), dup["output"],
                identity_a=verification_identity_key(
                    enrollment_id=primary_enrollment_id,
                    session_id=primary_session_id,
                ),
                identity_b=verification_identity_key(
                    enrollment_id=dup.get("enrollment_id"),
                    session_id=dup.get("session_id"),
                ),
                enrollment_id_a=primary_enrollment_id,
                enrollment_id_b=dup.get("enrollment_id"),
            )
            _safe_legacy_emit("verification", {
                "job_id": job_id,
                "trace_id": trace_id,
                **verdict,
            })
        except Exception:
            pass

    try:
        task = asyncio.get_running_loop().create_task(_collect())
    except RuntimeError:
        return
    _verify_tasks.add(task)
    task.add_done_callback(_verify_tasks.discard)


def _execution_request(req: PitchRequest, default_placement: str) -> ExecutionRequestV1:
    strategy = req.strategy
    options = req.strategy_options
    if req.candidates is not None:
        if options is not None and not isinstance(options, EnsembleOptionsV1):
            raise HTTPException(status_code=422, detail="candidates requires ensemble strategy options")
        if isinstance(options, EnsembleOptionsV1) and options.candidates != req.candidates:
            raise HTTPException(status_code=422, detail="candidates conflicts with strategy_options.candidates")
        concurrency = min(req.candidates, options.concurrency if isinstance(options, EnsembleOptionsV1) else 1)
        options = EnsembleOptionsV1(
            candidates=req.candidates,
            concurrency=concurrency,
            selection_policy=(options.selection_policy if isinstance(options, EnsembleOptionsV1) else "validated_score"),
        )
        if strategy == "auto" and req.candidates > 1:
            strategy = "ensemble"
    resolved_placement = req.placement or default_placement
    try:
        return ExecutionRequestV1(
            task=req.task,
            project_id=req.project_id,
            strategy=strategy,
            strategy_options=options,
            placement=resolved_placement,
            requirements=req.requirements,
            output_contract=req.output_contract,
            verification=req.verification,
            confidentiality=req.confidentiality,
            # Legacy adapters historically allowed auto/distributed dispatch.
            # Recording that adapter-owned consent preserves compatibility
            # without weakening privacy-safe canonical defaults.
            remote_dispatch_consent=(
                resolved_placement in ("auto", "distributed")
                and req.confidentiality != "local_only"
            ),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc


def _normalized_metadata(run: ServiceExecution) -> dict[str, Any]:
    result = run.result
    return {
        "execution_id": result.execution_id,
        "protocol_version": result.protocol_version,
        "execution_status": result.status,
        "lifecycle_status": result.lifecycle_status,
        "validation_outcome": result.validation_outcome,
        "assurance_level": result.assurance_level,
        "strategy_requested": result.strategy_requested,
        "strategy_selected": result.strategy_selected,
        "strategy_version": result.strategy_version,
        "strategy_options": result.strategy_options,
        "selector_reason": result.selector_reason,
        "selector_version": result.selector_version,
        "placement_requested": result.placement_requested,
        "placement_selected": result.placement_selected,
        "fallback_reason": result.fallback_reason,
        "validation_evidence": [item.model_dump(mode="json") for item in result.validation_evidence],
        "winning_candidate": result.winning_candidate,
        "winner_selection_explanation": result.winner_selection_explanation,
    }


def _compat_payload(run: ServiceExecution) -> dict[str, Any]:
    payload = dict(run.legacy_payload)
    payload.update(_normalized_metadata(run))
    return payload


def _raise_sync_failure(run: ServiceExecution) -> None:
    if run.result.status != "failed":
        return
    error = run.result.errors[0] if run.result.errors else None
    message = error.message if error else "execution failed"
    if error and error.code == "placement_unavailable":
        raise HTTPException(status_code=503, detail=message)
    if message.startswith("ValueError:"):
        raise HTTPException(status_code=422, detail=message.split(":", 1)[1].strip())
    raise RuntimeError(message)


def _callbacks(task: str, trace_id: str, job_id: str | None = None) -> dict[str, Any]:
    # Task, subtask, and generated-token text intentionally stay out of the
    # persisted pipeline event log. Token events remain ephemeral WebSocket
    # output because server_state._emit does not persist that event type.
    common = {"trace_id": trace_id}
    if job_id:
        common["job_id"] = job_id

    def on_plan(subtasks):
        _safe_legacy_emit("plan", {**common, "subtask_count": len(subtasks)})

    def on_build(subtask, output):
        _safe_legacy_emit("build", {**common, "subtask_id": subtask["id"]})

    def on_review_start():
        _safe_legacy_emit("review_start", common)

    def on_token(token, subtask):
        _safe_legacy_emit("token", {**common, "token": token, "subtask_id": subtask["id"]})

    return {
        "on_plan": on_plan,
        "on_build": on_build,
        "on_review_start": on_review_start,
        "on_token": on_token,
    }


@router.post("/pitch")
async def pitch(req: PitchRequest, request: Request, response: Response):
    """Compatibility endpoint: synchronous DAG/local unless explicitly overridden."""
    _check_pitch_key(request)
    remaining = _check_rate_limit(request)
    response.headers["X-RateLimit-Limit"] = str(state._rate_limits()[0])
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    trace_id = str(uuid.uuid4())

    canonical = _execution_request(req, default_placement="local")
    service = get_execution_service()
    try:
        service.validate_request(canonical)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        run = await service.execute(
            canonical,
            callbacks=_callbacks(req.task, trace_id),
            dag_runner=run_pipeline,
            on_running=lambda _result: _safe_legacy_emit(
                "pitch",
                {"trace_id": trace_id},
            ),
        )
    except ExecutionPersistenceError as exc:
        raise _persistence_unavailable() from exc
    _raise_sync_failure(run)
    _safe_legacy_emit("complete", {
        "project_dir": run.legacy_payload.get("project_dir"),
        "execution_id": run.result.execution_id,
        "trace_id": trace_id,
    })
    return _compat_payload(run)


@router.post("/pitch/async")
async def pitch_async(req: PitchRequest, request: Request, response: Response):
    """Compatibility async endpoint backed by the canonical execution service."""
    _check_pitch_key(request)
    remaining = _check_rate_limit(request)
    response.headers["X-RateLimit-Limit"] = str(state._rate_limits()[0])
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    job_id = f"job_{uuid.uuid4().hex}"
    trace_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "task": req.task,
        "project_id": req.project_id,
        "status": "queued",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "result": None,
        "error": None,
        "trace_id": trace_id,
    }
    canonical = _execution_request(req, default_placement="auto")
    try:
        get_execution_service().validate_request(canonical)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    job["execution_request"] = canonical.model_dump(mode="json")
    try:
        _commit_legacy_job(job, phase="queued")
    except ExecutionPersistenceError as exc:
        raise _persistence_unavailable() from exc
    jobs[job_id] = job
    # Keep the legacy helper's four-argument call contract: a few integrations
    # replace this hook to control background execution in-process.
    try:
        await _run_job(job_id, req.task, req.project_id, trace_id)
    except SubmissionActivationError as exc:
        raise _activation_unavailable(exc) from exc
    except ExecutionPersistenceError as exc:
        raise _persistence_unavailable() from exc
    return {
        "job_id": job_id,
        "execution_id": jobs[job_id].get("execution_id"),
        "status": "queued",
        "project_id": req.project_id,
        "trace_id": trace_id,
        "protocol_version": "1",
    }


async def _run_job(
    job_id: str,
    task: str,
    project_id: str | None = None,
    trace_id: str = "",
    canonical: ExecutionRequestV1 | None = None,
):
    """Submit one legacy job to the canonical background execution service."""
    trace_id = trace_id or str(uuid.uuid4())
    saved_request = jobs.get(job_id, {}).get("execution_request")
    canonical = canonical or (
        ExecutionRequestV1.model_validate(saved_request)
        if saved_request
        else ExecutionRequestV1(
            task=task,
            project_id=project_id,
            strategy="auto",
            placement="auto",
        )
    )

    def started(_result):
        job = jobs.get(job_id)
        if not job:
            return
        updated = dict(job)
        updated["status"] = "running"
        updated["started_at"] = datetime.now(timezone.utc).isoformat()
        _commit_legacy_job(updated, phase="running")
        jobs[job_id] = updated

    async def completed(run: ServiceExecution):
        job = jobs.get(job_id)
        if not job:
            return
        updated = dict(job)
        if run.result.lifecycle_status == "failed":
            updated["status"] = "failed"
            updated["error"] = run.result.errors[0].message if run.result.errors else "execution failed"
            updated["result"] = None
        elif run.result.lifecycle_status == "cancelled":
            updated["status"] = "cancelled"
            updated["error"] = run.result.cancellation_reason
            updated["result"] = None
        elif run.result.lifecycle_status == "interrupted":
            updated["status"] = "interrupted"
            updated["error"] = run.result.interruption_reason
            updated["result"] = None
        else:
            updated["status"] = "complete"
            updated["result"] = _compat_payload(run)
        updated["finished_at"] = datetime.now(timezone.utc).isoformat()
        _commit_legacy_job(updated, phase="terminal")
        jobs[job_id] = updated

    execution_callbacks = _callbacks(task, trace_id, job_id)
    if jobs.get(job_id, {}).get("source") == "public":
        execution_callbacks["execution_semaphore"] = _PUBLIC_INFERENCE_SEMAPHORE

    try:
        queued = get_execution_service().submit(
            canonical,
            job_id=job_id,
            callbacks=execution_callbacks,
            dag_runner=run_pipeline,
            on_start=started,
            on_complete=completed,
        )
    except SubmissionActivationError as exc:
        interrupted = exc.result
        job = jobs.get(job_id)
        if job is not None:
            updated = dict(job)
            updated["execution_id"] = interrupted.execution_id
            updated["strategy_requested"] = interrupted.strategy_requested
            updated["strategy_selected"] = interrupted.strategy_selected
            updated["selector_reason"] = interrupted.selector_reason
            updated["status"] = "interrupted"
            updated["error"] = "submission_activation_failed"
            updated["result"] = None
            updated["finished_at"] = interrupted.completed_at or datetime.now(
                timezone.utc
            ).isoformat()
            try:
                _commit_legacy_job(
                    updated,
                    phase="submission_activation_failure",
                )
            except ExecutionPersistenceError as mirror_error:
                # Canonical durable state is authoritative. Preserve the
                # activation exception (and stable execution ID) even when the
                # compatibility mirror cannot be updated.
                raise exc from mirror_error
            jobs[job_id] = updated
        raise
    updated = dict(jobs[job_id])
    updated["execution_id"] = queued.execution_id
    updated["strategy_requested"] = queued.strategy_requested
    updated["strategy_selected"] = queued.strategy_selected
    updated["selector_reason"] = queued.selector_reason
    _commit_legacy_job(updated, phase="execution_binding")
    jobs[job_id] = updated


@router.post("/public/pitch")
async def public_pitch(req: PitchRequest, request: Request):
    """Keyless, tightly bounded compatibility surface for the /try page."""
    if not get_config().get("public_pitch", False):
        raise HTTPException(status_code=404, detail="Public pitching is not enabled on this server")

    ip = request.client.host if request.client else "unknown"
    now = time.time()
    stamps = [
        value for value in state._public_pitch_timestamps.get(ip, [])
        if value > now - state._PUBLIC_RATE_WINDOW
    ]
    if len(stamps) >= state._PUBLIC_RATE_MAX:
        raise HTTPException(
            status_code=429,
            detail=f"Public pitching is limited to {state._PUBLIC_RATE_MAX} tasks per hour. Try again later.",
        )
    if len(req.task) > state._PUBLIC_TASK_MAX:
        raise HTTPException(status_code=422, detail=f"Keep public tasks under {state._PUBLIC_TASK_MAX} characters.")
    if any(term in req.task.lower() for term in state._PUBLIC_BLOCKLIST):
        raise HTTPException(
            status_code=422,
            detail="That task isn't something this public demo will build. Pitch something constructive!",
        )
    overrides = set(req.model_fields_set) - {"task"}
    if overrides:
        raise HTTPException(
            status_code=422,
            detail=(
                "Public pitching accepts only 'task'; execution strategy, placement, project, "
                "validator, and confidentiality settings are server-owned."
            ),
        )
    active_for_source = sum(
        1
        for job in jobs.values()
        if job.get("source") == "public"
        and job.get("source_ip") == ip
        and job["status"] in ("queued", "running")
    )
    if active_for_source >= state._PUBLIC_MAX_ACTIVE_PER_SOURCE:
        raise HTTPException(status_code=429, detail="Only one active public execution is allowed per source.")
    active = sum(
        1 for job in jobs.values()
        if job.get("source") == "public" and job["status"] in ("queued", "running")
    )
    if active >= state._PUBLIC_MAX_ACTIVE:
        raise HTTPException(status_code=503, detail="The public queue is full right now — try again in a few minutes.")

    stamps.append(now)
    state._public_pitch_timestamps[ip] = stamps
    job_id = f"job_{uuid.uuid4().hex}"
    trace_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "task": req.task,
        "project_id": None,
        "status": "queued",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "result": None,
        "error": None,
        "trace_id": trace_id,
        "source": "public",
        "source_ip": ip,
    }
    # Never feed a keyless caller's protocol knobs into execution. This fixed
    # profile is one local model call with no project or executable validators.
    canonical = ExecutionRequestV1(
        task=req.task,
        project_id=None,
        strategy="direct",
        strategy_options=EnsembleOptionsV1(candidates=1, concurrency=1),
        placement="local",
        confidentiality="local_only",
        timeout_seconds=120,
        max_output_bytes=65_536,
        network_policy="disabled",
    )
    job["execution_request"] = canonical.model_dump(mode="json")
    try:
        _commit_legacy_job(job, phase="queued")
    except ExecutionPersistenceError as exc:
        raise _persistence_unavailable() from exc
    jobs[job_id] = job
    try:
        await _run_job(job_id, req.task, None, trace_id)
    except SubmissionActivationError as exc:
        raise _activation_unavailable(exc) from exc
    except ExecutionPersistenceError as exc:
        raise _persistence_unavailable() from exc
    created_share = get_share_store().create(
        jobs[job_id]["execution_id"],
        CreateExecutionShareV1(
            expires_in_seconds=3600,
            allow_artifact_download=True,
            redact_node_identity=True,
            include_candidate_details=False,
        ),
    )
    return {
        "job_id": job_id,
        "execution_id": jobs[job_id].get("execution_id"),
        "status": "queued",
        "trace_id": trace_id,
        "protocol_version": "1",
        "share_token": created_share.token,
        "share_url": f"/v1/shares/{created_share.token}",
    }


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Get legacy job status, augmented with canonical strategy metadata."""
    if job_id not in jobs:
        normalized = get_execution_service().store.get_by_job_id(job_id)
        if normalized:
            return {
                "job_id": job_id,
                "execution_id": normalized.execution_id,
                "task": normalized.task,
                "status": "complete" if normalized.status in ("completed", "unverified") else normalized.status,
                "submitted_at": normalized.created_at,
                "started_at": normalized.started_at,
                "finished_at": normalized.completed_at,
                "error": normalized.errors[0].message if normalized.errors else None,
                "project_dir": normalized.output_reference,
                "plan": None,
                "rating": normalized.review_metadata.get("rating"),
                "strategy_requested": normalized.strategy_requested,
                "strategy_selected": normalized.strategy_selected,
                "placement_selected": normalized.placement_selected,
            }
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    result = job.get("result") or {}
    normalized = None
    if not job.get("execution_id") or not job.get("strategy_selected"):
        normalized = get_execution_service().store.get_by_job_id(job_id)
    return {
        "job_id": job["job_id"],
        "execution_id": job.get("execution_id") or (
            normalized.execution_id if normalized else None
        ),
        "task": job["task"],
        "status": job["status"],
        "submitted_at": job["submitted_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "project_dir": result.get("project_dir"),
        "plan": result.get("plan"),
        "rating": result.get("rating"),
        "strategy_requested": (
            job.get("strategy_requested")
            or result.get("strategy_requested")
            or (normalized.strategy_requested if normalized else None)
        ),
        "strategy_selected": (
            job.get("strategy_selected")
            or result.get("strategy_selected")
            or (normalized.strategy_selected if normalized else None)
        ),
        "selector_reason": (
            job.get("selector_reason")
            or result.get("selector_reason")
            or (normalized.selector_reason if normalized else None)
        ),
        "placement_selected": result.get("placement_selected") or (
            normalized.placement_selected if normalized else None
        ),
    }


@router.get("/jobs")
async def list_jobs(limit: int = 20):
    recent = list(jobs.values())[-max(1, min(limit, 100)):]
    summaries = []
    for job in reversed(recent):
        normalized = None
        if not job.get("execution_id") or not job.get("strategy_selected"):
            normalized = get_execution_service().store.get_by_job_id(job["job_id"])
        summaries.append(
            {
                "job_id": job["job_id"],
                "execution_id": job.get("execution_id") or (
                    normalized.execution_id if normalized else None
                ),
                "task": job["task"],
                "status": job["status"],
                "submitted_at": job["submitted_at"],
                "finished_at": job.get("finished_at"),
                "strategy_selected": job.get("strategy_selected") or (
                    normalized.strategy_selected if normalized else None
                ),
            }
        )
    return {
        "jobs": summaries,
        "count": len(recent),
    }


@router.post("/pitch/distributed")
async def pitch_distributed(req: PitchRequest, request: Request):
    """Compatibility adapter: DAG/distributed by default, with visible fallback."""
    _check_pitch_key(request)
    _check_rate_limit(request)
    trace_id = str(uuid.uuid4())
    canonical = _execution_request(req, default_placement="distributed")
    service = get_execution_service()
    try:
        service.validate_request(canonical)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        run = await service.execute(
            canonical,
            callbacks=_callbacks(req.task, trace_id),
            dag_runner=run_pipeline,
            on_running=lambda _result: _safe_legacy_emit(
                "pitch",
                {"trace_id": trace_id, "mode": "distributed"},
            ),
        )
    except ExecutionPersistenceError as exc:
        raise _persistence_unavailable() from exc
    _raise_sync_failure(run)
    return _compat_payload(run)
