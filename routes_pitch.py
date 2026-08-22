"""Pitch compatibility adapters and the legacy async job surface.

All three pitch endpoints construct ``ExecutionRequestV1`` and delegate to the
canonical execution service. Strategy implementations, distributed dispatch,
validation, and persistence do not live in this routing module.
"""

from __future__ import annotations

import asyncio
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
from execution.service import ServiceExecution, get_execution_service
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

# Imported as a module attribute for compatibility with tests and scripts that
# replace the legacy DAG runner. The service receives this callable explicitly.
run_pipeline = orchestrator.run_pipeline
plan = orchestrator.plan
compose_builder_prompt = orchestrator.compose_builder_prompt
# Kept as a public module hook for older in-process test/deployment shims. The
# canonical dispatcher invokes the same Ollama integration through orchestrator.
generate = _generate

router = APIRouter()

# Strong references to in-flight verification collectors. This helper remains
# for the sampled node-reputation mechanism; canonical output validation is a
# separate registry under execution/validators.py.
_verify_tasks: set = set()
_PUBLIC_INFERENCE_SEMAPHORE = asyncio.Semaphore(1)


def _spawn_comparison(dup_id, subtask_title, job_id, trace_id,
                      primary_node, primary_output, await_result, pool):
    """Wait for a sampled duplicate in the background and record its shape."""
    async def _collect():
        try:
            dup = await await_result(dup_id, 600)
            if not dup or dup.get("error") or not dup.get("output"):
                return
            verdict = pool.record_comparison(
                primary_node, primary_output,
                dup.get("node_id", "unknown"), dup["output"],
            )
            _emit("verification", {
                "subtask": subtask_title,
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
    common = {"task": task, "trace_id": trace_id}
    if job_id:
        common["job_id"] = job_id

    def on_plan(subtasks):
        _emit("plan", {**common, "subtasks": [item["title"] for item in subtasks]})

    def on_build(subtask, output):
        _emit("build", {**common, "subtask": subtask["title"], "subtask_id": subtask["id"]})

    def on_review_start():
        _emit("review_start", common)

    def on_token(token, subtask):
        _emit("token", {**common, "token": token, "subtask_id": subtask["id"]})

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
    _emit("pitch", {"task": req.task, "trace_id": trace_id})

    canonical = _execution_request(req, default_placement="local")
    service = get_execution_service()
    try:
        service.validate_request(canonical)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run = await service.execute(
        canonical,
        callbacks=_callbacks(req.task, trace_id),
        dag_runner=run_pipeline,
    )
    _raise_sync_failure(run)
    _emit("complete", {
        "task": req.task,
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
    jobs[job_id] = {
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
        jobs.pop(job_id, None)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    jobs[job_id]["execution_request"] = canonical.model_dump(mode="json")
    _db_write_job(jobs[job_id])
    # Keep the legacy helper's four-argument call contract: a few integrations
    # replace this hook to control background execution in-process.
    await _run_job(job_id, req.task, req.project_id, trace_id)
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
        job["status"] = "running"
        job["started_at"] = datetime.now(timezone.utc).isoformat()
        _db_write_job(job)

    async def completed(run: ServiceExecution):
        job = jobs.get(job_id)
        if not job:
            return
        if run.result.lifecycle_status == "failed":
            job["status"] = "failed"
            job["error"] = run.result.errors[0].message if run.result.errors else "execution failed"
            job["result"] = None
        elif run.result.lifecycle_status == "cancelled":
            job["status"] = "cancelled"
            job["error"] = run.result.cancellation_reason
            job["result"] = None
        elif run.result.lifecycle_status == "interrupted":
            job["status"] = "interrupted"
            job["error"] = run.result.interruption_reason
            job["result"] = None
        else:
            job["status"] = "complete"
            job["result"] = _compat_payload(run)
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _db_write_job(job)

    execution_callbacks = _callbacks(task, trace_id, job_id)
    if jobs.get(job_id, {}).get("source") == "public":
        execution_callbacks["execution_semaphore"] = _PUBLIC_INFERENCE_SEMAPHORE

    queued = get_execution_service().submit(
        canonical,
        job_id=job_id,
        callbacks=execution_callbacks,
        dag_runner=run_pipeline,
        on_start=started,
        on_complete=completed,
    )
    jobs[job_id]["execution_id"] = queued.execution_id
    jobs[job_id]["strategy_requested"] = queued.strategy_requested
    jobs[job_id]["strategy_selected"] = queued.strategy_selected
    jobs[job_id]["selector_reason"] = queued.selector_reason
    _db_write_job(jobs[job_id])


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
    jobs[job_id] = {
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
    jobs[job_id]["execution_request"] = canonical.model_dump(mode="json")
    _db_write_job(jobs[job_id])
    await _run_job(job_id, req.task, None, trace_id)
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
    return {
        "job_id": job["job_id"],
        "execution_id": job.get("execution_id"),
        "task": job["task"],
        "status": job["status"],
        "submitted_at": job["submitted_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "project_dir": result.get("project_dir"),
        "plan": result.get("plan"),
        "rating": result.get("rating"),
        "strategy_requested": job.get("strategy_requested") or result.get("strategy_requested"),
        "strategy_selected": job.get("strategy_selected") or result.get("strategy_selected"),
        "selector_reason": job.get("selector_reason") or result.get("selector_reason"),
        "placement_selected": result.get("placement_selected"),
    }


@router.get("/jobs")
async def list_jobs(limit: int = 20):
    recent = list(jobs.values())[-max(1, min(limit, 100)):]
    return {
        "jobs": [
            {
                "job_id": job["job_id"],
                "execution_id": job.get("execution_id"),
                "task": job["task"],
                "status": job["status"],
                "submitted_at": job["submitted_at"],
                "finished_at": job.get("finished_at"),
                "strategy_selected": job.get("strategy_selected"),
            }
            for job in reversed(recent)
        ],
        "count": len(recent),
    }


@router.post("/pitch/distributed")
async def pitch_distributed(req: PitchRequest, request: Request):
    """Compatibility adapter: DAG/distributed by default, with visible fallback."""
    _check_pitch_key(request)
    _check_rate_limit(request)
    trace_id = str(uuid.uuid4())
    _emit("pitch", {"task": req.task, "trace_id": trace_id, "mode": "distributed"})
    canonical = _execution_request(req, default_placement="distributed")
    service = get_execution_service()
    try:
        service.validate_request(canonical)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run = await service.execute(
        canonical,
        callbacks=_callbacks(req.task, trace_id),
        dag_runner=run_pipeline,
    )
    _raise_sync_failure(run)
    return _compat_payload(run)
