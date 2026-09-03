"""
FastAPI server for the orchestrator — app assembly.

Runs on the main machine. Accepts task pitches, decomposes them,
and distributes subtasks to worker nodes across the network.

The implementation lives in focused modules:
  server_state.py     — shared state, SQLite persistence, events, auth, rate limits
  routes_pitch.py     — /pitch, /pitch/async, /pitch/distributed, /jobs*
  routes_nodes.py     — /nodes*, /tasks* (worker protocol + circuit breaker)
  routes_history.py   — /history*, /share/*, /gallery
  routes_projects.py  — /projects*
  routes_events.py    — /health, /events, /ws/events, /standings, /metrics, /ledger
  dashboard.py        — /dashboard (HTML in templates/dashboard.html)

Usage:
  python -m uvicorn server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager, suppress
from typing import Mapping, Sequence

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import routes_events
import routes_executions
import routes_access
import routes_history
import routes_nodes
import routes_pitch
import routes_projects
import routes_run
import routes_status
import routes_try
from dashboard import router as dashboard_router
from access_control import ViewerAccessMiddleware, warn_if_viewer_auth_unconfigured
from config import CONFIG_FILE, get as get_config
from coordinator_lock import CoordinatorLock, validate_single_worker
from execution.artifacts import get_artifact_store
from execution.service import get_execution_service
from execution.sharing import get_share_store
from scripts.preflight import run_preflight
from worker_protocol import SERVER_VERSION
from server_state import (
    _cleanup_stale_nodes,
    _db_load_jobs,
    _init_db,
    begin_capability_shadow_runtime,
    shutdown_capability_shadow_evaluations,
)

# Re-exported for back-compat: tests and scripts reach server state through
# this module (server.nodes, server.jobs, ...). Same objects as server_state's.
from server_state import (  # noqa: F401
    _BLACKLIST_DURATION,
    _FAILURE_THRESHOLD,
    _LONG_POLL_TIMEOUT,
    _MAX_TASK_QUEUE,
    _RATE_MAX,
    _RATE_WINDOW,
    OUTPUT_DIR,
    _pitch_timestamps,
    jobs,
    node_blacklist,
    node_failure_count,
    nodes,
    pipeline_events,
    task_inflight,
    task_queue,
    task_results,
    ws_manager,
)

_LOG = logging.getLogger("mycelium.startup")


def _runtime_bind_host(
    settings: Mapping[str, object],
    *,
    argv: Sequence[str] | None = None,
) -> str:
    """Resolve the interface the running ASGI server actually requested.

    Uvicorn's host is a process-launch option, so it can differ from the
    compatibility value in config.json. Embedders can declare it explicitly;
    normal Uvicorn and Docker launches expose it in their process arguments.
    """
    explicit = os.environ.get("MYCELIUM_BIND_HOST", "").strip()
    if explicit:
        return explicit

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    resolved: str | None = None
    for index, argument in enumerate(arguments):
        if argument == "--host" and index + 1 < len(arguments):
            candidate = arguments[index + 1].strip()
            if candidate:
                resolved = candidate
        elif argument.startswith("--host="):
            candidate = argument.partition("=")[2].strip()
            if candidate:
                resolved = candidate
    if resolved is not None:
        return resolved
    return str(settings.get("bind_host", "127.0.0.1"))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = get_config()
    deployment_mode = str(settings.get("deployment_mode", "local"))
    validate_single_worker()
    coordinator_lock = CoordinatorLock(deployment_mode=deployment_mode)
    identity = coordinator_lock.acquire()
    try:
        # The OS lock is deliberately held before any migration, reconciliation,
        # or background task can mutate shared state.
        preflight = run_preflight(
            CONFIG_FILE,
            state_dir=coordinator_lock.state_dir,
            requested_mode=deployment_mode,
            bind_host=_runtime_bind_host(settings),
            check_lock=False,
        )
        errors = [check for check in preflight.checks if check.status == "error"]
        if errors:
            details = "; ".join(f"{check.name}: {check.message}" for check in errors)
            raise RuntimeError(f"deployment preflight failed: {details}")
        warnings = [
            check
            for check in preflight.checks
            if check.status == "warning" and check.name != "coordinator_lock"
        ]
        for check in warnings:
            _LOG.warning("Preflight warning [%s]: %s", check.name, check.message)

        app.state.coordinator_identity = identity
        app.state.deployment_mode = deployment_mode
        app.state.preflight_warnings = tuple(check.message for check in warnings)
        _LOG.info(
            "Coordinator instance %s started in %s mode with the single-process lock held",
            identity.instance_id,
            deployment_mode,
        )

        begin_capability_shadow_runtime()
        _init_db()
        _db_load_jobs()
        get_artifact_store().migrate()
        get_share_store().migrate()
        reconcile_executions = getattr(get_execution_service(), "reconcile_after_restart", None)
        if reconcile_executions:
            reconcile_executions()
        # ``_db_load_jobs`` above reconciles legacy queued/running rows before
        # exposing them; canonical reconciliation follows the same fail-closed rule.
        warn_if_viewer_auth_unconfigured()
        cleanup_task = asyncio.create_task(_cleanup_stale_nodes())
        try:
            yield
        finally:
            await shutdown_capability_shadow_evaluations()
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
    finally:
        coordinator_lock.release()


app = FastAPI(title="Mycelium", version=SERVER_VERSION, lifespan=_lifespan)
app.add_middleware(ViewerAccessMiddleware)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(_request, exc):
    """Return useful validation structure without echoing request-body secrets."""

    errors = []
    for error in exc.errors():
        cleaned = dict(error)
        # Pydantic includes the rejected raw value by default. Request models
        # carry prompts, output, attempt nonces, and enrollment credentials, so
        # no validation response should reflect any raw input value.
        cleaned.pop("input", None)
        errors.append(cleaned)
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(errors)},
    )


@app.get("/v1/operator/health")
async def operator_health():
    """Private process identity and deployment-mode health for operators."""
    identity = getattr(app.state, "coordinator_identity", None)
    try:
        validator_diagnostics = get_execution_service().validators.diagnostics()
    except Exception:
        validator_diagnostics = {"status": "unavailable"}
    return {
        "status": "ok" if identity is not None else "starting",
        "instance_id": identity.instance_id if identity is not None else None,
        "deployment_mode": getattr(app.state, "deployment_mode", "unknown"),
        "single_coordinator_lock": identity is not None,
        "preflight_warnings": list(getattr(app.state, "preflight_warnings", ())),
        "validator_process": validator_diagnostics,
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    """Return a generic 500 and log only a secret-safe error description."""
    import logging

    safe_path = routes_access.redact_share_token_path(request.url.path)
    logging.getLogger("mycelium").error(
        "unhandled error on %s error_type=%s",
        safe_path,
        type(exc).__name__,
    )
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


app.include_router(dashboard_router)
app.include_router(routes_events.router)
app.include_router(routes_executions.router)
app.include_router(routes_access.router)
app.include_router(routes_pitch.router)
app.include_router(routes_nodes.router)
app.include_router(routes_history.router)
app.include_router(routes_projects.router)
app.include_router(routes_run.router)
app.include_router(routes_status.router)
app.include_router(routes_try.router)
