"""
Pitch and job routes — the endpoints that actually run the pipeline.

/pitch runs synchronously; /pitch/async returns a job_id immediately;
/pitch/distributed fans builder subtasks out to worker nodes.
"""

import asyncio
import json
import re as _re
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response

from ollama_client import generate
import orchestrator
from orchestrator import (
    PipelineCancelled,
    _extract_final_output,
    _extract_issues,
    _extract_rating,
    compose_builder_prompt,
    extract_and_repair,
    make_run_dir,
    new_revision_record,
    plan,
    review,
    revise,
    run_pipeline,
)
from config import get as get_config
import server_state as state
from server_state import (
    PitchRequest,
    PitchResponse,
    _check_pitch_key,
    _check_rate_limit,
    _db_write_job,
    _emit,
    jobs,
    nodes,
    task_inflight,
    task_queue,
    task_results,
)

router = APIRouter()

# Strong references to in-flight verification collectors. Without these asyncio
# only holds a weak reference and the task can be garbage-collected before it
# finishes — the same trap the WebSocket broadcaster hit in server_state.
_verify_tasks: set = set()


def _spawn_comparison(dup_id, subtask_title, job_id, trace_id,
                      primary_node, primary_output, await_result, pool):
    """Wait for the duplicate answer in the background and record the verdict.

    Never raises into the pipeline: a spot check that fails must not be able to
    fail the deliverable it was checking.
    """
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
        return  # no loop (sync context) — nothing to collect against
    _verify_tasks.add(task)
    task.add_done_callback(_verify_tasks.discard)


@router.post("/pitch", response_model=PitchResponse)
async def pitch(req: PitchRequest, request: Request, response: Response):
    """Run the full pipeline locally (no distributed execution)."""
    _check_pitch_key(request)
    remaining = _check_rate_limit(request)
    response.headers["X-RateLimit-Limit"] = str(state._rate_limits()[0])
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    trace_id = str(uuid.uuid4())
    _emit("pitch", {"task": req.task, "trace_id": trace_id})

    def on_plan(subtasks):
        _emit("plan", {"task": req.task, "subtasks": [s["title"] for s in subtasks], "trace_id": trace_id})

    def on_build(subtask, output):
        _emit("build", {"task": req.task, "subtask": subtask["title"], "subtask_id": subtask["id"], "trace_id": trace_id})

    def on_review_start():
        _emit("review_start", {"task": req.task, "trace_id": trace_id})

    try:
        result = await run_pipeline(req.task, on_plan=on_plan, on_build=on_build, on_review_start=on_review_start, project_id=req.project_id)
    except ValueError as e:
        _emit("error", {"task": req.task, "message": str(e), "trace_id": trace_id})
        raise HTTPException(status_code=422, detail=str(e))

    result["results"] = {str(k): v for k, v in result["results"].items()}
    _emit("complete", {"task": req.task, "project_dir": result["project_dir"], "trace_id": trace_id})
    return result


# ── Async job system ─────────────────────────────────────────────────

@router.post("/pitch/async")
async def pitch_async(req: PitchRequest, request: Request, response: Response):
    """Submit a task and return immediately with a job_id.

    Poll GET /jobs/{job_id} for status and results.
    WebSocket clients on /ws/events receive live events as the job runs.
    """
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
        "cancel_requested": False,
    }
    _db_write_job(jobs[job_id])
    asyncio.create_task(_run_job(job_id, req.task, req.project_id, trace_id))
    return {"job_id": job_id, "status": "queued", "project_id": req.project_id, "trace_id": trace_id}


async def _run_job(job_id: str, task: str, project_id: str | None = None, trace_id: str = ""):
    """Background task: run the full pipeline for a job."""
    if not trace_id:
        trace_id = str(uuid.uuid4())
    jobs[job_id]["status"] = "running"
    jobs[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()
    _emit("pitch", {"task": task, "job_id": job_id, "trace_id": trace_id})

    def should_cancel() -> bool:
        return bool(jobs.get(job_id, {}).get("cancel_requested"))

    def on_plan(subtasks):
        _emit("plan", {"task": task, "job_id": job_id, "subtasks": [s["title"] for s in subtasks], "trace_id": trace_id})

    def on_build(subtask, output):
        _emit("build", {"task": task, "job_id": job_id, "subtask": subtask["title"], "subtask_id": subtask["id"], "trace_id": trace_id})

    def on_review_start():
        _emit("review_start", {"task": task, "job_id": job_id, "trace_id": trace_id})

    def on_token(token: str, subtask: dict):
        _emit("token", {
            "token": token,
            "subtask_id": subtask["id"],
            "job_id": job_id,
            "trace_id": trace_id,
        })

    # When worker nodes are connected, distribute builder subtasks to them.
    # Falls back to local Ollama automatically if a node times out or errors.
    dist_build_fn = None
    if nodes:
        _dist_nodes_used: set[str] = set()

        async def dist_build_fn(st: dict, context: str) -> str:
            # Same prompt the local builder would see — including the overall
            # project, so remote nodes don't drift off-format
            prompt = compose_builder_prompt(st, context, task)
            # Soft model routing: if role_model_map specifies a preferred model for
            # builders, only add a "requires" tag when at least one connected node
            # has that model — otherwise fall through to any node (no deadlock).
            preferred_model = get_config().get("role_model_map", {}).get("builder")
            requires: list[str] = []
            if preferred_model:
                model_tag = f"model:{preferred_model}"
                if any(model_tag in set(n.get("capabilities", [])) for n in nodes.values()):
                    requires = [model_tag]

            def _enqueue(tid: str, exclude: str | None = None) -> None:
                # A cancelled job must not put more work on anyone's machine,
                # even if the pipeline has not yet reached its next wave check.
                if should_cancel():
                    return
                entry = {
                    "task_id": tid,
                    "title": st["title"],
                    "prompt": prompt,
                    "system": orchestrator.BUILDER_SYSTEM,
                    "trace_id": trace_id,
                    "job_id": job_id,
                    "subtask_id": st["id"],
                    "requires": requires,
                }
                if exclude:
                    entry["exclude_node"] = exclude
                task_queue.append(entry)
                _emit("node_task_queued", {
                    "task_id": tid, "subtask": st["title"], "job_id": job_id,
                    "trace_id": trace_id, "verification": bool(exclude),
                })

            async def _await_result(tid: str, budget: float) -> dict | None:
                deadline = time.time() + budget
                while tid not in task_results:
                    if time.time() > deadline:
                        task_queue[:] = [t for t in task_queue if t["task_id"] != tid]
                        return None
                    await asyncio.sleep(1)
                return task_results.pop(tid)

            async def _holder_of(tid: str, wait: float) -> str | None:
                """Which node took this task. Needed before a duplicate can be
                queued, because the duplicate has to go somewhere else."""
                end = time.time() + wait
                while time.time() < end:
                    t = task_inflight.get(tid)
                    if t and t.get("assigned_to"):
                        return t["assigned_to"]
                    await asyncio.sleep(0.2)
                return None

            if should_cancel():
                raise PipelineCancelled([], [], "building")

            stamp = int(time.time() * 1000)
            primary_id = f"build_{st['id']}_{stamp}"
            _enqueue(primary_id)

            # Sampled second opinion on a different node. Off entirely at the
            # default verify_rate of 0, and never attempted with one node.
            pool = state.verification_pool
            state._refresh_verify_rate()
            dup_id = None
            if pool.should_verify(len(nodes)):
                holder = await _holder_of(primary_id, wait=5.0)
                if holder:  # nobody picked it up yet — skip, don't stall
                    dup_id = f"verify_{st['id']}_{stamp}"
                    _enqueue(dup_id, exclude=holder)

            tr = await _await_result(primary_id, 600)
            if tr is None or tr.get("error") or not tr.get("output"):
                # Timeout or bad output — fall back to local inference. There is
                # nothing left to compare the duplicate against, so drop it from
                # the queue and move on. Waiting for it here would add the whole
                # duplicate's latency to a task that has already failed; a result
                # that arrives late is swept by the janitor's result TTL.
                if dup_id:
                    task_queue[:] = [t for t in task_queue if t["task_id"] != dup_id]
                    task_results.pop(dup_id, None)
                return await generate(prompt, system=orchestrator.BUILDER_SYSTEM)

            if dup_id:
                # Collect the second opinion in the background. Verification is
                # a spot check on node honesty, not a gate on the deliverable —
                # making the pipeline wait for it would charge every sampled
                # task the slower node's latency for no benefit to the output.
                _spawn_comparison(
                    dup_id, st["title"], job_id, trace_id,
                    tr.get("node_id", "unknown"), tr["output"], _await_result, pool,
                )

            _dist_nodes_used.add(tr.get("node_id", "unknown"))
            return tr["output"]

    try:
        result = await run_pipeline(
            task,
            on_plan=on_plan,
            on_build=on_build,
            on_review_start=on_review_start,
            on_token=on_token if dist_build_fn is None else None,
            project_id=project_id,
            build_fn=dist_build_fn,
            should_cancel=should_cancel,
        )
        result["results"] = {str(k): v for k, v in result["results"].items()}
        jobs[job_id]["status"] = "complete"
        jobs[job_id]["result"] = result
        _emit("complete", {"task": task, "job_id": job_id, "project_dir": result["project_dir"], "trace_id": trace_id})
    except PipelineCancelled as c:
        # Not a failure. Work that was already running finished and was paid
        # for; the run simply stops here and says what it got through.
        jobs[job_id]["status"] = "cancelled"
        jobs[job_id]["cancelled_during"] = c.stage
        jobs[job_id]["completed_subtasks"] = c.completed
        jobs[job_id]["credits_settled"] = c.credits
        jobs[job_id]["error"] = None
        _emit("cancelled", {
            "task": task, "job_id": job_id, "trace_id": trace_id,
            "stage": c.stage, "completed": len(c.completed),
            "credits": sum(x.get("credits", 0) for x in c.credits),
        })
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        _emit("error", {"task": task, "job_id": job_id, "message": str(e), "trace_id": trace_id})

    jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
    _db_write_job(jobs[job_id])


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    """Stop a running pitch.

    On a 4B CPU model a pitch is minutes, and until now there was no way to
    take one back. What this does and — as importantly — what it does not:

      * Queued subtasks nobody has picked up yet are dropped immediately.
      * A subtask already running on a machine is **left alone**. Killing it
        mid-generation would throw away work someone's CPU has already done,
        and under attempt binding (#45) a reclaimed attempt settles nothing,
        so the node would be paid zero for real work. It finishes and is paid.
      * The job is marked `cancelling` while anything is still out, and
        `cancelled` once the pipeline unwinds.
      * Nothing already written to the ledger is reversed.

    Returns what is still outstanding so the caller can say "waiting for one
    machine to finish" rather than pretending it stopped instantly.
    """
    _check_pitch_key(request)
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")

    if job["status"] in ("complete", "failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=f"That job already finished ({job['status']}) — nothing to cancel.",
        )

    job["cancel_requested"] = True

    # Drop this job's unclaimed work. Anything in task_inflight has been handed
    # to a node and is deliberately left to finish.
    dropped = [t["task_id"] for t in task_queue if t.get("job_id") == job_id]
    task_queue[:] = [t for t in task_queue if t.get("job_id") != job_id]
    still_running = [
        tid for tid, t in task_inflight.items() if t.get("job_id") == job_id
    ]

    job["status"] = "cancelling"
    _db_write_job(job)
    _emit("cancelling", {
        "job_id": job_id, "task": job.get("task", ""),
        "dropped": len(dropped), "still_running": len(still_running),
        "trace_id": job.get("trace_id", ""),
    })

    return {
        "job_id": job_id,
        "status": "cancelling",
        "dropped_from_queue": len(dropped),
        "still_running": len(still_running),
        "detail": (
            f"Stopped. {len(still_running)} subtask(s) already on a machine will "
            "finish and be paid for."
            if still_running else
            "Stopped. Nothing was left running."
        ),
    }


@router.post("/public/pitch")
async def public_pitch(req: PitchRequest, request: Request):
    """Keyless pitching for the /try page — hard-limited, off by default.

    Guards, in order: feature flag, per-IP hourly limit, task length cap,
    content filter, global concurrent-job cap. Runs through the same async
    job machinery as /pitch/async.
    """
    if not get_config().get("public_pitch", False):
        raise HTTPException(status_code=404, detail="Public pitching is not enabled on this server")

    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window_start = now - state._PUBLIC_RATE_WINDOW
    stamps = [t for t in state._public_pitch_timestamps.get(ip, []) if t > window_start]
    if len(stamps) >= state._PUBLIC_RATE_MAX:
        raise HTTPException(
            status_code=429,
            detail=f"Public pitching is limited to {state._PUBLIC_RATE_MAX} tasks per hour. Try again later.",
        )

    if len(req.task) > state._PUBLIC_TASK_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"Keep public tasks under {state._PUBLIC_TASK_MAX} characters.",
        )

    lowered = req.task.lower()
    if any(term in lowered for term in state._PUBLIC_BLOCKLIST):
        raise HTTPException(
            status_code=422,
            detail="That task isn't something this public demo will build. Pitch something constructive!",
        )

    active_public = sum(
        1 for j in jobs.values() if j.get("source") == "public" and j["status"] in ("queued", "running")
    )
    if active_public >= state._PUBLIC_MAX_ACTIVE:
        raise HTTPException(
            status_code=503,
            detail="The public queue is full right now — try again in a few minutes.",
        )

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
    }
    _db_write_job(jobs[job_id])
    asyncio.create_task(_run_job(job_id, req.task, None, trace_id))
    return {"job_id": job_id, "status": "queued", "trace_id": trace_id}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Get the status and result of an async job.

    Falls back to SQLite for jobs not in the current in-memory store
    (e.g. jobs that completed before the last server restart).
    """
    if job_id not in jobs:
        # Try SQLite
        try:
            import sqlite3

            with state._db_lock:
                con = sqlite3.connect(state._DB_PATH)
                con.row_factory = sqlite3.Row
                row = con.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                con.close()
            if row:
                return {
                    "job_id": row["job_id"],
                    "task": row["task"],
                    "status": row["status"],
                    "submitted_at": row["submitted_at"],
                    "finished_at": row["finished_at"],
                    "error": row["error"],
                    "project_dir": row["project_dir"],
                    "plan": None,
                    "rating": row["rating"],
                }
        except Exception:
            pass
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    # Don't return the full results dict in the status response — keep it light
    return {
        "job_id": job["job_id"],
        "task": job["task"],
        "status": job["status"],
        "submitted_at": job["submitted_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "project_dir": job["result"]["project_dir"] if job["result"] else None,
        "plan": job["result"]["plan"] if job["result"] else None,
        "rating": job["result"].get("rating") if job["result"] else None,
        # A cancelled run is not a failed one, and saying what it did get
        # through is the difference between "stopped" and "wasted".
        "cancel_requested": bool(job.get("cancel_requested")),
        "cancelled_during": job.get("cancelled_during"),
        "completed_subtasks": job.get("completed_subtasks"),
        "credits_settled": sum(
            c.get("credits", 0) for c in (job.get("credits_settled") or [])
        ) or None,
    }


@router.get("/jobs")
async def list_jobs(limit: int = 20):
    """List recent async jobs."""
    recent = list(jobs.values())[-limit:]
    return {
        "jobs": [
            {
                "job_id": j["job_id"],
                "task": j["task"],
                "status": j["status"],
                "submitted_at": j["submitted_at"],
                "finished_at": j.get("finished_at"),
            }
            for j in reversed(recent)
        ],
        "count": len(recent),
    }


# ── Distributed pipeline ────────────────────────────────────────────

async def _dispatch_subtask(
    st: dict,
    subtasks: list[dict],
    results: dict[int, str],
    nodes_used: set[str],
    overall_task: str = "",
    stats: dict | None = None,
) -> tuple[int, str]:
    """Push one subtask to the worker queue and wait for its result.

    Falls back to local Ollama inference on timeout or worker error.
    Returns (subtask_id, output_text).

    `stats`, when given, receives {subtask_id: {executor, seconds, ...}} —
    which machine actually did this piece and how long it took. The run page
    shows it, and there is nowhere else to get it: the ledger records credits
    without a run id, so joining it back to a run means guessing by timestamp.
    """
    _t0 = time.time()

    def _record(executor: str, output: str, fell_back: bool = False):
        if stats is not None:
            stats[st["id"]] = {
                "seconds": round(time.time() - _t0, 1),
                "executor": executor,
                "chars": len(output or ""),
                "credits": 5,
                "fell_back_to_local": fell_back,
            }

    # Build context from resolved dependencies
    context_parts = []
    for dep_id in st.get("depends_on", []):
        if dep_id in results:
            dep_task = next((s for s in subtasks if s["id"] == dep_id), None)
            label = dep_task["title"] if dep_task else f"Subtask {dep_id}"
            context_parts.append(f"[{label}]:\n{results[dep_id]}")
    context = "\n\n".join(context_parts)

    prompt = compose_builder_prompt(st, context, overall_task)

    task_id = f"build_{st['id']}_{uuid.uuid4().hex}"
    task_queue.append({
        "task_id": task_id,
        "title": st["title"],
        "prompt": prompt,
        "system": orchestrator.BUILDER_SYSTEM,
    })

    deadline = time.time() + 600
    while task_id not in task_results:
        if time.time() > deadline:
            output = await generate(prompt, system=orchestrator.BUILDER_SYSTEM)
            nodes_used.add("local")
            _record("local", output, fell_back=True)
            return st["id"], output
        await asyncio.sleep(1)

    tr = task_results.pop(task_id)
    if tr.get("error") or not tr.get("output"):
        output = await generate(prompt, system=orchestrator.BUILDER_SYSTEM)
        nodes_used.add("local")
        _record("local", output, fell_back=True)
    else:
        output = tr["output"]
        node = tr.get("node_id", "unknown")
        nodes_used.add(node)
        _record(node, output)
    return st["id"], output


@router.post("/pitch/distributed")
async def pitch_distributed(req: PitchRequest, request: Request):
    """Pitch a task that gets distributed across connected nodes.

    Planner and reviewer run locally. Builder subtasks go to the worker
    task queue and execute in parallel across connected nodes. Falls back
    to local run_pipeline if no nodes are connected.

    Full feature parity with /pitch: project memory, reviser pass, code
    extraction, rating, events, and ZIP-downloadable output.
    """
    _check_pitch_key(request)
    _check_rate_limit(request)
    trace_id = str(uuid.uuid4())

    # Load project memory context
    memory_context = ""
    if req.project_id:
        try:
            from memory import get_memory_context
            memory_context = get_memory_context(req.project_id)
        except Exception:
            pass

    _emit("pitch", {"task": req.task, "trace_id": trace_id, "mode": "distributed"})

    # 1. Plan
    try:
        subtasks = await plan(req.task, memory_context=memory_context)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    _emit("plan", {"task": req.task, "subtasks": [s["title"] for s in subtasks], "trace_id": trace_id})

    # 2. If no worker nodes connected, fall back to full local pipeline
    if not nodes:
        try:
            result = await run_pipeline(req.task, project_id=req.project_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        result["results"] = {str(k): v for k, v in result["results"].items()}
        result["mode"] = "local"
        return result

    # 3. Distribute builder tasks across worker nodes (parallel waves)
    if len(task_queue) >= state._MAX_TASK_QUEUE:
        raise HTTPException(status_code=503, detail="Task queue is full — too many pending tasks")

    results: dict[int, str] = {}
    nodes_used: set[str] = set()
    subtask_stats: dict[int, dict] = {}
    credits: list[dict] = []
    _started = time.time()
    remaining = {st["id"]: st for st in subtasks}

    while remaining:
        ready = [st for st in remaining.values()
                 if all(dep_id in results for dep_id in st.get("depends_on", []))]
        if not ready:
            break
        wave_results = await asyncio.gather(*[
            _dispatch_subtask(st, subtasks, results, nodes_used, req.task, subtask_stats)
            for st in ready
        ])
        for subtask_id, output in wave_results:
            results[subtask_id] = output
            remaining.pop(subtask_id, None)
            _emit("build", {"task": req.task, "subtask_id": subtask_id, "trace_id": trace_id})

    for _sid, _meta in subtask_stats.items():
        _title = next((s["title"] for s in subtasks if s["id"] == _sid), f"subtask {_sid}")
        credits.append({"contributor": _meta["executor"], "type": "compute",
                        "credits": _meta["credits"], "for": "building " + _title})

    # 4. Review
    _emit("review_start", {"task": req.task, "trace_id": trace_id})
    _review_t0 = time.time()
    review_output = await review(req.task, subtasks, results, memory_context=memory_context)
    _review_seconds = round(time.time() - _review_t0, 1)

    # 5. Reviser pass (up to 2 rounds, same as local pipeline)
    rating = _extract_rating(review_output)
    final_output = _extract_final_output(review_output)
    issues = _extract_issues(review_output)
    revision = new_revision_record(rating, issues, final_output)
    for _ in range(2):
        if rating != "NEEDS_WORK" or not issues or not final_output:
            break
        revised = await revise(req.task, issues, final_output)
        if len(revised.strip()) <= len(final_output) // 2:
            revision["stopped_because"] = "the revision came back mostly empty"
            break
        revision["fired"] = True
        revision["passes"] += 1
        revision["chars_after"] = len(revised)
        final_output = revised
        issues = _extract_issues(revised)
        if not issues:
            rating = "PASS"
            revision["cleared_the_rating"] = True
            revision["stopped_because"] = "the reviewer's issues were gone"
            break
    else:
        if revision["fired"]:
            revision["stopped_because"] = "it hit the 2-pass limit"
    revision["rating_after"] = rating

    # 6. Save output files
    timestamp, project_dir = make_run_dir()

    (project_dir / "plan.json").write_text(json.dumps(subtasks, indent=2), encoding="utf-8")
    for st in subtasks:
        safe_title = _re.sub(r"[^\w\s-]", "", st["title"]).strip().replace(" ", "_")
        (project_dir / f"builder_{st['id']}_{safe_title}.md").write_text(results[st["id"]], encoding="utf-8")
    (project_dir / "review.md").write_text(review_output, encoding="utf-8")
    if final_output:
        (project_dir / "output.md").write_text(final_output, encoding="utf-8")

    # Same extract → verify → repair guarantees as the local pipeline
    final_output, code_files, code_problems = await extract_and_repair(
        req.task,
        final_output,
        review_output,
        project_dir,
        builder_outputs={
            f"builder {st['id']} ({st['title']})": results[st["id"]] for st in subtasks
        },
    )

    log = {
        "task": req.task,
        "timestamp": timestamp,
        "plan": subtasks,
        "results": {str(k): v for k, v in results.items()},
        "review": review_output,
        "rating": rating,
        "code_files": [str(f) for f in code_files],
        "code_problems": code_problems,
        "mode": "distributed",
        "nodes_used": list(nodes_used),
        "project_id": req.project_id or "",
        # Same record the local pipeline writes — see orchestrator.run_pipeline.
        # /run/{id} reads one shape regardless of how the work was executed.
        "started_at": datetime.fromtimestamp(_started, timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - _started, 1),
        "model": get_config().get("model", ""),
        "prompt_set": orchestrator._ACTIVE_PROMPT_SET.name,
        "subtask_stats": {str(k): v for k, v in subtask_stats.items()},
        "review_seconds": _review_seconds,
        "revision": revision,
        "credits": credits,
    }
    (project_dir / "full_log.json").write_text(json.dumps(log, indent=2), encoding="utf-8")

    # 7. Save iteration to project memory, auto-summarize if grown large
    if req.project_id:
        try:
            from memory import add_iteration, _summarize_memory, SUMMARIZE_THRESHOLD, PROJECTS_DIR as _PROJ_DIR
            add_iteration(req.project_id, {
                "project_dir": str(project_dir),
                "plan": subtasks,
                "final_output": final_output or "",
                "rating": rating,
            }, req.task)
            memory_file = _PROJ_DIR / req.project_id / "memory.md"
            if memory_file.exists():
                raw = memory_file.read_text(errors="ignore", encoding="utf-8")
                if len(raw) > SUMMARIZE_THRESHOLD:
                    compressed = await _summarize_memory(raw)
                    if compressed and compressed != raw:
                        memory_file.write_text(compressed, encoding="utf-8")
        except Exception:
            pass

    _emit("complete", {
        "task": req.task, "project_dir": str(project_dir),
        "rating": rating, "trace_id": trace_id, "mode": "distributed",
    })

    return {
        "project_dir": str(project_dir),
        "plan": subtasks,
        "results": {str(k): v for k, v in results.items()},
        "review": review_output,
        "final_output": final_output or "",
        "rating": rating,
        "code_files": [str(f) for f in code_files],
        "code_problems": code_problems,
        "mode": "distributed",
        "nodes_used": len(nodes_used),
        "project_id": req.project_id or "",
    }
