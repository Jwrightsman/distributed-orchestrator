"""
Node management and task distribution routes.

Workers register here, long-poll for work, and submit results. The circuit
breaker (consecutive-failure blacklist) also lives on this surface.
"""

import asyncio
import secrets
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
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


def _should_defer(node_id: str, waiting_since: float) -> bool:
    """Should this node hold back and let a better-rated one take the work?

    Reputation orders who gets *first refusal*, never who is eligible: after a
    short grace period this node takes the task regardless. A poorly-rated node
    is offered work last, not never — exclusion is the circuit breaker's job.

    Returns False whenever every waiting node has the same routing weight,
    which is always the case while verification is off. That keeps
    verify_rate=0 a genuine no-op rather than a silent reordering.
    """
    if time.time() - waiting_since >= state._ROUTING_DEFER:
        return False  # waited long enough — never starve a node
    now = time.time()
    contenders = [node_id] + [
        n for n, ts in state.waiting_nodes.items()
        if n != node_id and now - ts < state._WAITING_FRESH
    ]
    if len(contenders) < 2:
        return False
    pool = state.verification_pool
    if len({pool.reputation(n).routing_weight for n in contenders}) < 2:
        return False  # nothing to choose between
    return pool.rank(contenders)[0] != node_id


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
    """Connected nodes, each with its verification record attached.

    Reputation is merged in here rather than stored on the node record so a
    node that disconnects and comes back does not reset its history.
    """
    pool = state.verification_pool
    out = []
    for n in nodes.values():
        rep = pool.reputation(n["node_id"]).as_dict()
        out.append({**n, **{k: v for k, v in rep.items() if k != "node_id"}})
    return {"nodes": out, "count": len(nodes), "verify_rate": pool.verify_rate}


@router.get("/tasks/next")
async def next_task(node_id: str, request: Request):
    """Worker asks for the next available task.

    Long-polls up to _LONG_POLL_TIMEOUT seconds — holds the connection open
    until work arrives or the timeout expires. Much more efficient than the
    node polling every few seconds and getting empty 204s.

    Returns 429 if the node is circuit-breaker blacklisted.
    """
    _check_node_auth(request)
    state.touch_node(node_id)

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

    def _find_task() -> int | None:
        """Index of the first task this node may take, or None.

        Peeks rather than pops: whether this node is *allowed* to take it may
        still depend on which other nodes are waiting.
        """
        for i, t in enumerate(task_queue):
            # A verification duplicate must land on a different node than the
            # original, or it compares a node against itself and proves nothing.
            if t.get("exclude_node") and t["exclude_node"] == node_id:
                continue
            eligible = set(t.get("eligible_nodes", []))
            if eligible and node_id not in eligible:
                continue
            required = set(t.get("requires", []))
            if not required or required.issubset(node_caps):
                return i
        return None

    # Long-poll: wait up to _LONG_POLL_TIMEOUT for a task to appear
    deadline = time.time() + state._LONG_POLL_TIMEOUT
    waiting_since = time.time()
    state.waiting_nodes[node_id] = waiting_since
    try:
        while True:
            state.waiting_nodes[node_id] = time.time()  # liveness, not wait start
            idx = _find_task()
            if idx is not None and not _should_defer(node_id, waiting_since):
                with state._task_queue_lock:
                    # Another TestClient thread may have claimed it between the
                    # peek and this mutation. Recompute while holding the lock.
                    idx = _find_task()
                    if idx is None:
                        continue
                    task = task_queue.pop(idx)
                task["assigned_to"] = node_id
                task["assigned_at"] = time.time()
                # Bind this handout to this node. The nonce is unguessable and
                # separate from task_id, which appears in events and logs.
                task["attempt_id"] = uuid.uuid4().hex
                task["nonce"] = secrets.token_urlsafe(24)
                lease_seconds = min(7200, max(1, int(task.get("lease_seconds", state.ATTEMPT_LEASE_SECONDS))))
                task["lease_expires_at"] = task["assigned_at"] + lease_seconds
                if node_id in nodes:
                    nodes[node_id]["current_task"] = task.get("title", task["task_id"])
                task_inflight[task["task_id"]] = task
                _emit("node_busy", {"node_id": node_id, "task_title": task.get("title", task["task_id"])})
                _emit("attempt_started", {
                    "task_id": task["task_id"],
                    "attempt_id": task["attempt_id"],
                    "execution_id": task.get("execution_id"),
                    "unit_id": task.get("execution_unit_id"),
                    "node_id": node_id,
                    "placement": "distributed",
                })
                return task

            if time.time() >= deadline:
                return Response(status_code=204)

            # Poll faster while deferring so the grace period costs little.
            await asyncio.sleep(0.25 if idx is not None else 0.5)
    finally:
        state.waiting_nodes.pop(node_id, None)


def _attempt_rejection(pending: dict, result, *, strict_v1: bool) -> str | None:
    """Why this submission must not settle, or None if it may.

    Checks are ordered cheapest-first and each one is a real attack:
      - wrong node   -> an admitted node claiming another node's credit
      - bad nonce    -> a guessed or replayed task_id without the handout
      - expired lease-> work returned long after it was reclaimed and redone
    """
    if result.node_id != pending.get("assigned_to"):
        return "submitting node is not the assigned node"
    if strict_v1:
        expected_contract = pending.get("contract_version")
        if not result.contract_version:
            return "missing contract version"
        if not expected_contract or not secrets.compare_digest(
            str(result.contract_version), str(expected_contract)
        ):
            return "contract version does not match"
    expected = pending.get("nonce")
    if expected and (strict_v1 or result.nonce):
        if strict_v1 and not result.nonce:
            return "missing attempt nonce"
        if not secrets.compare_digest(str(result.nonce), str(expected)):
            return "attempt nonce does not match"
    expected_attempt = pending.get("attempt_id")
    if strict_v1 and not result.attempt_id:
        return "missing attempt id"
    if result.attempt_id and expected_attempt and not secrets.compare_digest(
        str(result.attempt_id), str(expected_attempt)
    ):
        return "attempt id does not match"
    if strict_v1:
        if pending.get("contract_version") != "1":
            return "task was not issued under execution contract version 1"
        for field, label in (
            ("execution_id", "execution id"),
            ("execution_unit_id", "execution unit id"),
            ("execution_unit_kind", "execution unit kind"),
        ):
            supplied = getattr(result, field, None)
            expected_value = pending.get(field)
            if not supplied:
                return f"missing {label}"
            if not expected_value or not secrets.compare_digest(str(supplied), str(expected_value)):
                return f"{label} does not match"
    expires = pending.get("lease_expires_at")
    if expires and time.time() > expires:
        return "lease expired"
    return None


@router.post("/tasks/{task_id}/result")
async def submit_result(task_id: str, result: TaskResult, request: Request):
    """Worker submits completed task."""
    _check_node_auth(request)

    # Idempotent settlement: a retry after a dropped connection must return the
    # original outcome, not a second payment.
    if result.attempt_id and result.attempt_id in state.settled_attempts:
        settled = state.settled_attempts[result.attempt_id]
        if settled.get("node_id") and settled["node_id"] != result.node_id:
            raise HTTPException(status_code=403, detail="result rejected: settled attempt belongs to another node")
        if settled.get("task_id") and settled["task_id"] != task_id:
            raise HTTPException(status_code=403, detail="result rejected: settled attempt belongs to another task")
        return settled.get("response", settled)

    pending = task_inflight.get(task_id)
    if pending is not None:
        # The server-issued attempt is authoritative. A v1 worker cannot opt
        # into the legacy path by omitting its submitted version or bindings.
        strict_v1 = pending.get("contract_version") == "1"
        reason = _attempt_rejection(pending, result, strict_v1=strict_v1)
        if reason:
            # Reject loudly. Silently ignoring this would hide exactly the
            # behaviour worth knowing about.
            _emit("result_rejected", {
                "task_id": task_id,
                "claimed_by": result.node_id,
                "assigned_to": pending.get("assigned_to"),
                "reason": reason,
            })
            raise HTTPException(status_code=403, detail=f"result rejected: {reason}")

    task = task_inflight.pop(task_id, None)
    trace_id = task.get("trace_id", "") if task else ""

    # Only work we actually handed out earns credit. A node retrying after a
    # dropped connection, or reporting a task that was already reclaimed, still
    # gets its result recorded — but paying twice would inflate the ledger and
    # the standings on the dashboard.
    was_inflight = task is not None

    task_results[task_id] = {
        "task_id": task_id,
        "node_id": result.node_id,
        "output": result.output,
        "error": result.error,
        "elapsed_seconds": result.elapsed_seconds,
        "completed_at": time.time(),
        "trace_id": trace_id,
        "attempt_id": result.attempt_id,
        "contract_version": result.contract_version,
        "execution_id": result.execution_id,
        "execution_unit_id": result.execution_unit_id,
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
    # Re-admit first, then update. A node whose lid was closed long enough to be
    # evicted still deserves credit for work it actually finished, and gating
    # this on prior membership meant a returning node stayed invisible.
    state.touch_node(result.node_id)
    if was_inflight:
        nodes[result.node_id]["tasks_completed"] += 1
    nodes[result.node_id]["current_task"] = None
    # Credit requires a verified attempt. An unverifiable result is still
    # recorded above so late work is never lost — it just is not paid.
    legacy_bound = (
        was_inflight
        and bool(task and task.get("nonce") and task.get("attempt_id"))
        and bool(result.nonce and result.attempt_id)
        and task.get("contract_version") != "1"
    )
    protocol_v1_bound = (
        was_inflight
        and result.contract_version == "1"
        and task.get("contract_version") == "1"
        and bool(result.nonce and result.attempt_id and result.execution_id and result.execution_unit_id)
    )
    verified = legacy_bound or protocol_v1_bound
    if success and was_inflight and verified:
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
    outcome = {"status": "accepted", "credits_earned": credits_earned}
    if task and task.get("attempt_id"):
        state.remember_settlement(
            task["attempt_id"],
            outcome,
            node_id=result.node_id,
            task_id=task_id,
        )
    _emit("attempt_completed", {
        "task_id": task_id,
        "attempt_id": result.attempt_id,
        "execution_id": result.execution_id or (task.get("execution_id") if task else None),
        "unit_id": result.execution_unit_id or (task.get("execution_unit_id") if task else None),
        "node_id": result.node_id,
        "status": "completed" if success else "failed",
        "placement": "distributed",
    })
    return outcome


@router.post("/tasks/{task_id}/stream")
async def stream_task_tokens(task_id: str, batch: TokenBatch, request: Request):
    """Worker node relays streamed tokens back to the orchestrator.

    The server re-emits them as WebSocket 'token' events so the dashboard
    live-streams output from remote nodes, not just local builds.

    Streaming tokens also counts as a heartbeat, and used not to. `last_seen`
    was refreshed only by /tasks/next and /tasks/{id}/result, so a node stayed
    "alive" only between tasks — a build longer than _NODE_TIMEOUT (90 s) went
    silent by that measure while the node was in fact sending a batch every
    0.3 s. The janitor then evicted a working node, reclaimed the task it was
    mid-way through, and re-queued it into an empty registry where nothing
    could pick it up. Found in a dress rehearsal: on a single-node setup the
    fourth subtask was reclaimed under the node, the dashboard dropped to
    0 nodes, and the run stalled — while the node's own terminal showed it
    building. A node emitting tokens for a task is the strongest liveness
    signal there is.
    """
    _check_node_auth(request)
    task = task_inflight.get(task_id)
    if not task or not batch.tokens:
        return {"ok": False}
    if task.get("assigned_to") != batch.node_id:
        raise HTTPException(status_code=403, detail="stream rejected: submitting node is not the assigned node")
    if task.get("contract_version") == "1":
        for supplied, expected, label in (
            (batch.attempt_id, task.get("attempt_id"), "attempt id"),
            (batch.nonce, task.get("nonce"), "attempt nonce"),
            (batch.execution_id, task.get("execution_id"), "execution id"),
            (batch.execution_unit_id, task.get("execution_unit_id"), "execution unit id"),
        ):
            if not supplied:
                raise HTTPException(status_code=403, detail=f"stream rejected: missing {label}")
            if not expected or not secrets.compare_digest(str(supplied), str(expected)):
                raise HTTPException(status_code=403, detail=f"stream rejected: {label} does not match")
        if batch.contract_version != "1":
            raise HTTPException(status_code=403, detail="stream rejected: contract version does not match")
        if time.time() > task.get("lease_expires_at", 0):
            raise HTTPException(status_code=403, detail="stream rejected: lease expired")
    state.touch_node(batch.node_id)
    _emit("token", {
        "token": batch.tokens,
        "subtask_id": task.get("subtask_id", 0),
        "job_id": task.get("job_id", ""),
        "trace_id": task.get("trace_id", ""),
        "source": "node",
        "node_id": batch.node_id,
    })
    return {"ok": True}
