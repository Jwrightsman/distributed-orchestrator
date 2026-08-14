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
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

import routes_events
import routes_history
import routes_nodes
import routes_pitch
import routes_projects
from dashboard import router as dashboard_router
from server_state import _cleanup_stale_nodes, _db_load_jobs, _init_db

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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _init_db()
    _db_load_jobs()
    cleanup_task = asyncio.create_task(_cleanup_stale_nodes())
    try:
        yield
    finally:
        cleanup_task.cancel()


app = FastAPI(title="Mycelium", version="0.3.0", lifespan=_lifespan)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    """Return a generic 500 rather than the exception text.

    Tracebacks and exception strings routinely carry filesystem paths, config
    values and query fragments. The detail still reaches the operator through
    the server log; it just stops reaching the caller.
    """
    import logging

    logging.getLogger("mycelium").exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


app.include_router(dashboard_router)
app.include_router(routes_events.router)
app.include_router(routes_pitch.router)
app.include_router(routes_nodes.router)
app.include_router(routes_history.router)
app.include_router(routes_projects.router)
