"""
Observability routes — health, event stream (polling + WebSocket),
standings, metrics, and the raw contribution ledger.
"""

import platform
import time

import httpx
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from access_control import authorize_viewer_websocket, viewer_health_fields
from ledger import get_history, get_standings
from ollama_client import OLLAMA_URL
import server_state as state
from build_info import BUILD
from server_state import (
    jobs,
    node_blacklist,
    nodes,
    task_inflight,
    task_queue,
    task_results,
    ws_manager,
)

router = APIRouter()


@router.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
        ollama_status = "connected"
    except Exception:
        models = []
        ollama_status = "unavailable"

    return {
        "status": "ok" if ollama_status == "connected" else "degraded",
        "ollama": ollama_status,
        "models": models,
        "nodes_online": len(nodes),
        "tasks_pending": len(task_queue),
        "node_enrollment_required": state.node_enrollment_required(),
        **viewer_health_fields(),
    }


@router.get("/events")
async def get_events(since: int = 0):
    """Get pipeline events. since=0 returns last 100; since=N returns events with id > N."""
    if since < 0:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_event_cursor", "message": "since must be >= 0"},
        )
    return {"events": state.read_persisted_events(since=since, limit=100)}


@router.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    """WebSocket endpoint — clients receive pipeline events in real time."""
    if not await authorize_viewer_websocket(websocket):
        return
    await ws_manager.connect(websocket)
    # Replay the last 20 persisted events so new clients aren't blind. Which
    # store they come from is server_state's business, not this route's.
    try:
        for event in state.read_persisted_events(since=0, limit=20):
            await websocket.send_json(event)
    except Exception:
        pass
    try:
        while True:
            # Keep alive — ignore any incoming messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


@router.get("/standings")
async def standings():
    """Get contributor standings sorted by credits."""
    return {"standings": get_standings()}


@router.get("/status.json")
async def status_json():
    """Public liveness snapshot — no auth, safe to share, cheap to poll.

    Exists so a curious person (or an agent) can answer "is this network real
    and running?" without an invite, without a key, and without reading the
    dashboard. Everything here is already reachable through /health, /nodes and
    /metrics; this is those three collapsed into one stable shape so it can be
    linked from the landing page and quoted in a post.

    Deliberately contains no task text, no node hostnames beyond a count, and no
    credentials — it is designed to be pasted in public.
    """
    s = get_standings()
    online = list(nodes.values())
    uptime = max(0, int(time.time() - state.STARTED_AT))
    # Counted from the run directories, not get_history(): that helper takes a
    # limit (50 by default), so the old count silently stopped growing at 50 —
    # a number the landing page publishes as proof the network is real.
    from routes_status import _built_since

    model = None
    ollama_ok = False
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            tags = resp.json().get("models", [])
            ollama_ok = True
            model = tags[0]["name"] if tags else None
    except Exception:
        pass

    return {
        "service": "mycelium",
        "status": "ok" if ollama_ok else "degraded",
        "orchestrator_online": True,
        "inference_available": ollama_ok,
        "model": model,
        "nodes_online": len(online),
        "pitches_completed": _built_since(),
        "tasks_completed_total": sum(c["compute_tasks"] for c in s),
        "contributors": len(s),
        "uptime_seconds": uptime,
        "accepting_nodes": "by invite",
        "repo": "https://github.com/Jwrightsman/distributed-orchestrator",
        # Fingerprint of the source this process is running. The point is
        # deploy verification: a redeploy that silently did nothing looks
        # identical to one that worked, and that has already cost this project
        # a day. See build_info.py and scripts/verify_deploy.py.
        "build": BUILD,
    }


@router.get("/metrics")
async def metrics():
    """Operational snapshot — queue depth, latency, node count, job status."""
    s = get_standings()
    tasks_completed_total = sum(c["compute_tasks"] for c in s)

    elapsed_values = [
        r["elapsed_seconds"]
        for r in task_results.values()
        if r.get("elapsed_seconds") and r["elapsed_seconds"] > 0
    ]
    avg_latency = round(sum(elapsed_values) / len(elapsed_values), 1) if elapsed_values else None

    blacklisted_nodes = [
        nid for nid, until in node_blacklist.items()
        if time.time() < until
    ]

    # This machine's own ledger balance. The orchestrator earns credits for
    # pitches, local builds and reviews under its hostname, so a dashboard can
    # show whose balance it is displaying instead of implying it is the
    # viewer's — anyone can open this page.
    host = platform.node()
    host_entry = next((c for c in s if c["contributor"] == host), None)

    return {
        "orchestrator_id": host,
        "orchestrator_credits": round(host_entry["total_credits"], 1) if host_entry else 0,
        "tasks_completed_total": tasks_completed_total,
        "tasks_in_queue":        len(task_queue),
        "tasks_inflight":        len(task_inflight),
        "nodes_online":          len(nodes),
        "nodes_blacklisted":     len(blacklisted_nodes),
        "jobs_running":          sum(1 for j in jobs.values() if j["status"] == "running"),
        "jobs_queued":           sum(1 for j in jobs.values() if j["status"] == "queued"),
        "avg_task_latency_seconds": avg_latency,
    }


@router.get("/ledger")
async def ledger(contributor: str | None = None, limit: int = 50):
    """Get recent ledger entries."""
    return {"entries": get_history(contributor, limit)}
