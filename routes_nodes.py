"""
Node management and task distribution routes.

Workers register here, long-poll for work, and submit results. The circuit
breaker (consecutive-failure blacklist) also lives on this surface.
"""

import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ledger import log_contribution
import server_state as state
from server_state import (
    NodeRegistration,
    TaskResult,
    TokenBatch,
    _check_node_auth,
    _emit,
    node_blacklist,
    node_failure_count,
    nodes,
    task_inflight,
    task_queue,
    task_results,
)

router = APIRouter()


@router.post("/nodes/register")
async def register_node(reg: NodeRegistration, request: Request):
    _check_node_auth(request)
    # Auto-add a "model:<name>" capability tag so tasks can soft-route by model.
    caps = list(reg.capabilities)
    model_tag = f"model:{reg.model}"
    if model_tag not in caps:
        caps.append(model_tag)
    nodes[reg.node_id] = {
        "node_id": reg.node_id,
        "model": reg.model,
        "platform": reg.platform,
        "machine": reg.machine,
        "hostname": reg.hostname,
        "cpu_count": reg.cpu_count,
        "ram_gb": reg.ram_gb,
        "gpu": reg.gpu,
        "capabilities": caps,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "last_seen": time.time(),
        "tasks_completed": 0,
        "credits_earned": 0,
        "current_task": None,
    }
    return {
        "message": f"Welcome, {reg.node_id}. You are node #{len(nodes)} in the network.",
        "capabilities": caps,
    }


@router.get("/nodes")
async def list_nodes():
    return {"nodes": list(nodes.values()), "count": len(nodes)}


@router.get("/tasks/next")
async def next_task(node_id: str, request: Request):
    """Worker asks for the next available task.

    Long-polls up to _LONG_POLL_TIMEOUT seconds — holds the connection open
    until work arrives or the timeout expires. Much more efficient than the
    node polling every few seconds and getting empty 204s.

    Returns 429 if the node is circuit-breaker blacklisted.
    """
    _check_node_auth(request)
    if node_id in nodes:
        nodes[node_id]["last_seen"] = time.time()

    # Circuit breaker — check if this node is blacklisted for repeated failures
    if node_id in node_blacklist:
        if time.time() < node_blacklist[node_id]:
            remaining = int(node_blacklist[node_id] - time.time())
            return JSONResponse(
                status_code=429,
                content={"error": "circuit_open", "retry_after": remaining},
            )
        else:
            # Blacklist expired — let it back in
            node_blacklist.pop(node_id, None)
            node_failure_count[node_id] = 0

    # Collect this node's capabilities for task matching
    node_caps: set[str] = set(nodes[node_id].get("capabilities", [])) if node_id in nodes else set()

    def _pick_task() -> dict | None:
        """Return the first task this node can handle, respecting capability requirements."""
        for i, t in enumerate(task_queue):
            required = set(t.get("requires", []))
            if not required or required.issubset(node_caps):
                return task_queue.pop(i)
        return None

    # Long-poll: wait up to _LONG_POLL_TIMEOUT for a task to appear
    deadline = time.time() + state._LONG_POLL_TIMEOUT
    while True:
        task = _pick_task()
        if task:
            task["assigned_to"] = node_id
            task["assigned_at"] = time.time()
            if node_id in nodes:
                nodes[node_id]["current_task"] = task.get("title", task["task_id"])
            task_inflight[task["task_id"]] = task
            _emit("node_busy", {"node_id": node_id, "task_title": task.get("title", task["task_id"])})
            return task

        if time.time() >= deadline:
            return Response(status_code=204)

        await asyncio.sleep(0.5)


@router.post("/tasks/{task_id}/result")
async def submit_result(task_id: str, result: TaskResult, request: Request):
    """Worker submits completed task."""
    _check_node_auth(request)
    task = task_inflight.pop(task_id, None)
    trace_id = task.get("trace_id", "") if task else ""

    task_results[task_id] = {
        "task_id": task_id,
        "node_id": result.node_id,
        "output": result.output,
        "error": result.error,
        "elapsed_seconds": result.elapsed_seconds,
        "completed_at": time.time(),
        "trace_id": trace_id,
    }

    # Circuit breaker: track consecutive failures per node
    success = bool(result.output and not result.error)
    if not success:
        count = node_failure_count.get(result.node_id, 0) + 1
        node_failure_count[result.node_id] = count
        if count >= state._FAILURE_THRESHOLD:
            node_blacklist[result.node_id] = time.time() + state._BLACKLIST_DURATION
            _emit("node_blacklisted", {
                "node_id": result.node_id,
                "failure_count": count,
                "blacklist_seconds": state._BLACKLIST_DURATION,
            })
    else:
        node_failure_count[result.node_id] = 0  # reset on success

    credits_earned = 0
    if result.node_id in nodes:
        nodes[result.node_id]["tasks_completed"] += 1
        nodes[result.node_id]["last_seen"] = time.time()
        nodes[result.node_id]["current_task"] = None
    if success:
        credits_earned = 5
        log_contribution(result.node_id, "compute", credits=credits_earned, task=task_id)
        if result.node_id in nodes:
            nodes[result.node_id]["credits_earned"] = nodes[result.node_id].get("credits_earned", 0) + credits_earned
    _emit("node_idle", {
        "node_id": result.node_id,
        "credits_earned": credits_earned,
        "elapsed_seconds": result.elapsed_seconds,
        "success": success,
        "trace_id": trace_id,
    })
    return {"status": "accepted", "credits_earned": credits_earned}


@router.post("/tasks/{task_id}/stream")
async def stream_task_tokens(task_id: str, batch: TokenBatch, request: Request):
    """Worker node relays streamed tokens back to the orchestrator.

    The server re-emits them as WebSocket 'token' events so the dashboard
    live-streams output from remote nodes, not just local builds.
    """
    _check_node_auth(request)
    task = task_inflight.get(task_id)
    if not task or not batch.tokens:
        return {"ok": False}
    _emit("token", {
        "token": batch.tokens,
        "subtask_id": task.get("subtask_id", 0),
        "job_id": task.get("job_id", ""),
        "trace_id": task.get("trace_id", ""),
        "source": "node",
        "node_id": batch.node_id,
    })
    return {"ok": True}
