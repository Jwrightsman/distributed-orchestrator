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

from execution.attempts import AttemptRejected, WorkerPayloadLimitExceeded
from ledger import sync_compatibility_ledger
from node_sessions import DuplicateNodeSession
import server_state as state
from server_state import (
    NodeRegistration,
    TaskResult,
    TokenBatch,
    _check_node_auth,
    _check_node_session,
    _emit,
    _safe_diagnostic_emit,
    node_blacklist,
    node_failure_count,
    nodes,
    task_inflight,
    task_queue,
    task_results,
)

router = APIRouter()


def _clear_assignment(task: dict) -> None:
    for field in (
        "assigned_to",
        "assigned_session_id",
        "assigned_at",
        "attempt_id",
        "nonce",
        "lease_expires_at",
    ):
        task.pop(field, None)


def _reclaim_replaced_session(node_id: str, replaced_session_id: str) -> None:
    """Close and requeue work that belonged to a replaced stale session."""

    with state._task_queue_lock:
        task_ids = [
            task_id
            for task_id, task in task_inflight.items()
            if task.get("assigned_to") == node_id
            and task.get("assigned_session_id") == replaced_session_id
        ]
        for task_id in task_ids:
            task = task_inflight.get(task_id)
            if task is None:
                continue
            attempt_id = task.get("attempt_id")
            try:
                changed = bool(
                    attempt_id
                    and state.attempt_store.transition_active(
                        attempt_id=attempt_id,
                        state="reclaimed",
                        reason="assigned node session was replaced after staleness",
                    )
                )
            except Exception as exc:
                _safe_diagnostic_emit(
                    "attempt_reclaim_failed",
                    {
                        "task_id": task_id,
                        "node_id": node_id,
                        "phase": "session_replacement",
                        "error_type": type(exc).__name__,
                    },
                )
                continue
            if attempt_id and not changed:
                continue
            task = task_inflight.pop(task_id)
            _clear_assignment(task)
            deadline = task.get("execution_deadline_at")
            if not deadline or float(deadline) > time.time():
                task_queue.append(task)
            _emit("task_reclaimed", {
                "task_id": task_id,
                "node_id": node_id,
                "reason": "node session replaced after staleness",
            })


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
    try:
        grant = state.node_sessions.register(
            reg.node_id,
            presented_token=request.headers.get("X-Node-Session"),
        )
    except DuplicateNodeSession as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "node_id_in_use",
                "message": str(exc),
                "reclaim_after_seconds": state._NODE_TIMEOUT,
            },
        ) from exc

    if grant.replaced_session_id:
        _reclaim_replaced_session(reg.node_id, grant.replaced_session_id)

    # Auto-add a "model:<name>" capability tag so tasks can soft-route by model.
    caps = list(reg.capabilities)
    model_tag = f"model:{reg.model}"
    if model_tag not in caps:
        caps.append(model_tag)
    existing = nodes.get(reg.node_id) if grant.idempotent else None
    lifetime = state.attempt_store.lifetime_contribution_summary(reg.node_id)
    session_tasks = int((existing or {}).get("session_tasks_completed", 0))
    session_points = float((existing or {}).get("session_contribution_points", 0))
    session_metadata = grant.record.public_metadata()
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
        "registered_at": (existing or {}).get(
            "registered_at", datetime.now(timezone.utc).isoformat()
        ),
        "last_seen": time.time(),
        "session_id": grant.record.session_id,
        "session_started_at": session_metadata["session_started_at"],
        "session_expires_at": session_metadata["session_expires_at"],
        "session_tasks_completed": session_tasks,
        "session_contribution_points": session_points,
        **lifetime,
        # Compatibility aliases for older dashboards.  These are session, not
        # lifetime, values and new clients should use the explicit names above.
        "tasks_completed": session_tasks,
        "credits_earned": session_points,
        "current_task": (existing or {}).get("current_task"),
        "draining": False,
    }
    return {
        "message": f"Welcome, {reg.node_id}. You are node #{len(nodes)} in the network.",
        "node_id": reg.node_id,
        "capabilities": caps,
        "session_id": grant.record.session_id,
        "session_token": grant.session_token,
        "session_expires_at": session_metadata["session_expires_at"],
        "session_started_at": session_metadata["session_started_at"],
        "idempotent": grant.idempotent,
        **lifetime,
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
        lifetime = state.attempt_store.lifetime_contribution_summary(n["node_id"])
        n.update(lifetime)
        rep = pool.reputation(n["node_id"]).as_dict()
        out.append({**n, **{k: v for k, v in rep.items() if k != "node_id"}})
    return {"nodes": out, "count": len(nodes), "verify_rate": pool.verify_rate}


@router.post("/nodes/{node_id}/heartbeat")
async def heartbeat_node(node_id: str, request: Request):
    """Refresh one registered worker session without polling for work."""

    _check_node_auth(request)
    session_record = _check_node_session(request, node_id)
    if not state.touch_node(session_record.node_id):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "node_registration_required",
                "message": "node registry entry is absent; register again",
                "action": "register_again",
            },
            headers={"X-Node-Session-Required": "true"},
        )
    node = nodes[session_record.node_id]
    node.update(
        state.attempt_store.lifetime_contribution_summary(session_record.node_id)
    )
    return {
        "ok": True,
        "node_id": session_record.node_id,
        "session_id": session_record.session_id,
        "session_tasks_completed": node.get("session_tasks_completed", 0),
        "session_contribution_points": node.get(
            "session_contribution_points", 0
        ),
        "lifetime_tasks_completed": node.get("lifetime_tasks_completed", 0),
        "lifetime_contribution_points": node.get(
            "lifetime_contribution_points", 0
        ),
    }


@router.post("/nodes/{node_id}/drain")
async def drain_node(node_id: str, request: Request):
    """Stop handing new work to a session while allowing its current result."""

    _check_node_auth(request)
    session_record = _check_node_session(request, node_id)
    if not state.touch_node(session_record.node_id):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "node_registration_required",
                "message": "node registry entry is absent; register again",
                "action": "register_again",
            },
            headers={"X-Node-Session-Required": "true"},
        )
    nodes[session_record.node_id]["draining"] = True
    state.waiting_nodes.pop(session_record.node_id, None)
    _emit("node_draining", {
        "node_id": session_record.node_id,
        "session_id": session_record.session_id,
    })
    return {
        "ok": True,
        "node_id": session_record.node_id,
        "session_id": session_record.session_id,
        "draining": True,
        "current_task": nodes[session_record.node_id].get("current_task"),
    }


@router.get("/tasks/next")
async def next_task(node_id: str, request: Request):
    """Worker asks for the next available task.

    Long-polls up to _LONG_POLL_TIMEOUT seconds — holds the connection open
    until work arrives or the timeout expires. Much more efficient than the
    node polling every few seconds and getting empty 204s.

    Returns 429 if the node is circuit-breaker blacklisted.
    """
    _check_node_auth(request)
    session_record = _check_node_session(request, node_id)
    node_id = session_record.node_id
    if not state.touch_node(node_id):
        state.node_sessions.invalidate_node(
            node_id, session_id=session_record.session_id
        )
        raise HTTPException(
            status_code=401,
            detail={
                "code": "node_registration_required",
                "message": "node registry entry is absent; register again",
                "action": "register_again",
            },
            headers={"X-Node-Session-Required": "true"},
        )
    if nodes[node_id].get("draining"):
        return Response(status_code=204)

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
                # The token may reach its absolute expiry while this request is
                # held in a long poll.  Recheck immediately before handout.
                session_record = _check_node_session(request, node_id)
                with state._task_queue_lock:
                    # Another TestClient thread may have claimed it between the
                    # peek and this mutation. Recompute while holding the lock.
                    idx = _find_task()
                    if idx is None:
                        continue
                    task = task_queue.pop(idx)
                    task["assigned_to"] = node_id
                    task["assigned_session_id"] = session_record.session_id
                    task["assigned_at"] = time.time()
                    execution_deadline = task.get("execution_deadline_at")
                    if execution_deadline and task["assigned_at"] >= float(execution_deadline):
                        _emit("worker_task_expired", {
                            "task_id": task["task_id"],
                            "execution_id": task.get("execution_id"),
                            "reason": "execution deadline elapsed before assignment",
                        })
                        continue
                    # Bind this handout to this node. The nonce is unguessable
                    # and separate from task_id, which appears in events/logs.
                    task["attempt_id"] = uuid.uuid4().hex
                    task["nonce"] = secrets.token_urlsafe(24)
                    lease_seconds = min(
                        7200,
                        max(
                            1,
                            int(
                                task.get(
                                    "lease_seconds",
                                    state.ATTEMPT_LEASE_SECONDS,
                                )
                            ),
                        ),
                    )
                    task["lease_expires_at"] = task["assigned_at"] + lease_seconds
                    if execution_deadline:
                        task["lease_expires_at"] = min(
                            task["lease_expires_at"], float(execution_deadline)
                        )
                    try:
                        state.attempt_store.issue(
                            task,
                            assigned_node_id=node_id,
                            attempt_id=task["attempt_id"],
                            nonce=task["nonce"],
                            issued_at=task["assigned_at"],
                            lease_expires_at=task["lease_expires_at"],
                            assigned_session_id=session_record.session_id,
                        )
                    except Exception as exc:
                        # Never expose work unless its authority is durable.
                        _clear_assignment(task)
                        task_queue.insert(min(idx, len(task_queue)), task)
                        _safe_diagnostic_emit(
                            "attempt_issue_failed",
                            {
                                "task_id": task["task_id"],
                                "node_id": node_id,
                                "phase": "attempt_issue",
                                "error_type": type(exc).__name__,
                            },
                        )
                        raise HTTPException(
                            status_code=503,
                            detail="worker attempt could not be issued durably",
                        ) from exc
                    if node_id in nodes:
                        nodes[node_id]["current_task"] = task.get(
                            "title", task["task_id"]
                        )
                    task_inflight[task["task_id"]] = task
                    _emit("node_busy", {
                        "node_id": node_id,
                        "task_id": task["task_id"],
                        "unit_id": task.get("execution_unit_id"),
                    })
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


def _settle_and_publish(task_id: str, result: TaskResult, *, session_id: str):
    """Serialize in-memory lifecycle changes around the SQLite transaction."""
    with state._task_queue_lock:
        pending = task_inflight.get(task_id)
        if pending is not None and state.attempt_store.active_for_task(task_id) is None:
            # Rolling-upgrade compatibility: make an already-issued, fully
            # bound in-memory attempt durable before considering its result.
            state.attempt_store.adopt_inflight(pending)
        outcome = state.attempt_store.settle(
            task_id=task_id,
            node_id=result.node_id,
            output=result.output,
            error=result.error,
            elapsed_seconds=result.elapsed_seconds,
            contract_version=result.contract_version,
            attempt_id=result.attempt_id,
            nonce=result.nonce,
            execution_id=result.execution_id,
            execution_unit_id=result.execution_unit_id,
            execution_unit_kind=result.execution_unit_kind,
            session_id=session_id,
        )
        receipt = outcome.receipt
        state.accepted_result_broker.publish(receipt)
        if outcome.replayed:
            return pending, outcome, None, ""

        task = task_inflight.get(task_id)
        if task and task.get("attempt_id") == receipt.attempt_id:
            task = task_inflight.pop(task_id)
        else:
            task = None
        trace_id = task.get("trace_id", "") if task else ""
        # Compatibility/diagnostic mirror. Dispatcher authority is exclusively
        # the accepted receipt broker above.
        task_results[task_id] = receipt.as_legacy_result(trace_id=trace_id)
        return pending, outcome, task, trace_id


@router.post("/tasks/{task_id}/result")
async def submit_result(task_id: str, result: TaskResult, request: Request):
    """Settle a result only through its active server-issued attempt."""
    _check_node_auth(request)
    session_record = _check_node_session(request, result.node_id)
    if not state.touch_node(session_record.node_id):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "node_registration_required",
                "message": "node registry entry is absent; register again",
                "action": "register_again",
            },
            headers={"X-Node-Session-Required": "true"},
        )
    pending = task_inflight.get(task_id)
    try:
        pending, outcome, _task, trace_id = _settle_and_publish(
            task_id, result, session_id=session_record.session_id
        )
    except WorkerPayloadLimitExceeded as exc:
        try:
            quarantine_id = state.attempt_store.quarantine(
                task_id=task_id,
                claimed_attempt_id=result.attempt_id,
                claimed_node_id=result.node_id,
                claimed_execution_id=result.execution_id,
                claimed_unit_id=result.execution_unit_id,
                claimed_unit_kind=result.execution_unit_kind,
                claimed_contract_version=result.contract_version,
                output=result.output,
                error=result.error,
                reason=exc.reason,
            )
        except Exception:
            quarantine_id = None
        _emit("result_rejected", {
            "task_id": task_id,
            "claimed_by": result.node_id,
            "assigned_to": pending.get("assigned_to") if pending else None,
            "reason": exc.reason,
            "attempt_state": exc.state,
            "error_code": exc.code,
            "quarantined": quarantine_id is not None,
            "quarantine_id": quarantine_id,
        })
        return JSONResponse(
            status_code=413,
            content={
                "status": "rejected",
                "error": exc.code,
                "detail": exc.reason,
                "field": exc.field,
                "max_bytes": exc.limit,
                "observed_bytes": exc.observed,
                "quarantined": quarantine_id is not None,
            },
        )
    except AttemptRejected as exc:
        try:
            quarantine_id = state.attempt_store.quarantine(
                task_id=task_id,
                claimed_attempt_id=result.attempt_id,
                claimed_node_id=result.node_id,
                claimed_execution_id=result.execution_id,
                claimed_unit_id=result.execution_unit_id,
                claimed_unit_kind=result.execution_unit_kind,
                claimed_contract_version=result.contract_version,
                output=result.output,
                error=result.error,
                reason=exc.reason,
            )
        except Exception:
            quarantine_id = None
        _emit("result_rejected", {
            "task_id": task_id,
            "claimed_by": result.node_id,
            "assigned_to": pending.get("assigned_to") if pending else None,
            "reason": exc.reason,
            "attempt_state": exc.state,
            "quarantined": quarantine_id is not None,
            "quarantine_id": quarantine_id,
        })
        raise HTTPException(status_code=403, detail=f"result rejected: {exc.reason}") from exc

    receipt = outcome.receipt
    if outcome.replayed:
        try:
            sync_compatibility_ledger()
        except Exception:
            pass
        return outcome.response

    success = bool(receipt.output and not receipt.error)
    node_id = receipt.assigned_node_id
    if not success:
        count = node_failure_count.get(node_id, 0) + 1
        node_failure_count[node_id] = count
        if count >= state._FAILURE_THRESHOLD:
            node_blacklist[node_id] = time.time() + state._BLACKLIST_DURATION
            _emit("node_blacklisted", {
                "node_id": node_id,
                "failure_count": count,
                "blacklist_seconds": state._BLACKLIST_DURATION,
            })
    else:
        node_failure_count[node_id] = 0

    state.touch_node(node_id)
    node_record = nodes[node_id]
    node_record["session_tasks_completed"] = int(
        node_record.get("session_tasks_completed", 0)
    ) + 1
    node_record["tasks_completed"] = node_record["session_tasks_completed"]
    node_record["current_task"] = None
    credits_earned = int(outcome.response.get("credits_earned", 0))
    if credits_earned:
        node_record["session_contribution_points"] = (
            float(node_record.get("session_contribution_points", 0))
            + credits_earned
        )
    node_record["credits_earned"] = node_record.get(
        "session_contribution_points", 0
    )
    node_record.update(state.attempt_store.lifetime_contribution_summary(node_id))
    try:
        sync_compatibility_ledger()
    except Exception:
        # The SQLite contribution is already atomic with settlement. Failure to
        # refresh a compatibility projection must not alter the accepted reply.
        pass

    _emit("node_idle", {
        "node_id": node_id,
        "credits_earned": credits_earned,
        "contribution_basis": "compute_contribution" if credits_earned else None,
        "points_are_monetary": False,
        "elapsed_seconds": receipt.elapsed_seconds,
        "success": success,
        "trace_id": trace_id,
    })
    _emit("attempt_completed", {
        "task_id": receipt.task_id,
        "attempt_id": receipt.attempt_id,
        "execution_id": receipt.execution_id,
        "unit_id": receipt.execution_unit_id,
        "unit_kind": receipt.execution_unit_kind,
        "node_id": node_id,
        "status": "completed" if success else "failed",
        "placement": "distributed",
    })
    return outcome.response


@router.post("/tasks/{task_id}/tokens")
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
    session_record = _check_node_session(request, batch.node_id)
    if session_record.node_id not in nodes:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "node_registration_required",
                "message": "node registry entry is absent; register again",
                "action": "register_again",
            },
            headers={"X-Node-Session-Required": "true"},
        )
    task = task_inflight.get(task_id)
    try:
        stream_outcome = state.attempt_store.record_stream_batch(
            task_id=task_id,
            node_id=batch.node_id,
            tokens=batch.tokens,
            contract_version=batch.contract_version,
            attempt_id=batch.attempt_id,
            nonce=batch.nonce,
            execution_id=batch.execution_id,
            execution_unit_id=batch.execution_unit_id,
            execution_unit_kind=batch.execution_unit_kind,
            session_id=session_record.session_id,
        )
    except AttemptRejected as exc:
        raise HTTPException(
            status_code=403, detail=f"stream rejected: {exc.reason}"
        ) from exc

    state.touch_node(batch.node_id)
    if not stream_outcome.accepted:
        if batch.node_id in nodes:
            nodes[batch.node_id]["current_task"] = None
        if stream_outcome.emit_limit_event:
            _emit("stream_limit_exceeded", {
                "task_id": task_id,
                "attempt_id": stream_outcome.attempt_id,
                "node_id": batch.node_id,
                "error": stream_outcome.error_code,
                "detail": stream_outcome.detail,
                "max_output_bytes": stream_outcome.max_output_bytes,
                "streamed_bytes": stream_outcome.streamed_bytes,
                "stream_batch_count": stream_outcome.stream_batch_count,
            })
        return JSONResponse(
            status_code=(
                429
                if stream_outcome.error_code == "stream_rate_limit_exceeded"
                else 413
            ),
            content={
                "ok": False,
                "status": "limit_exceeded",
                "error": stream_outcome.error_code,
                "detail": stream_outcome.detail,
                "max_output_bytes": stream_outcome.max_output_bytes,
                "streamed_bytes": stream_outcome.streamed_bytes,
                "stream_batch_count": stream_outcome.stream_batch_count,
            },
        )

    _emit("token", {
        "token": batch.tokens,
        "subtask_id": task.get("subtask_id", 0) if task else 0,
        "job_id": task.get("job_id", "") if task else "",
        "trace_id": task.get("trace_id", "") if task else "",
        "source": "node",
        "node_id": batch.node_id,
    })
    return {"ok": True}
