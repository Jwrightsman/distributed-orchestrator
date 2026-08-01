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

from extract import extract_code_files
from ollama_client import generate
from orchestrator import (
    BUILDER_SYSTEM,
    _extract_final_output,
    _extract_issues,
    _extract_rating,
    plan,
    review,
    revise,
    run_pipeline,
)
from config import get as get_config
import server_state as state
from server_state import (
    OUTPUT_DIR,
    PitchRequest,
    PitchResponse,
    _check_pitch_key,
    _check_rate_limit,
    _db_write_job,
    _emit,
    jobs,
    nodes,
    task_queue,
    task_results,
)

router = APIRouter()


@router.post("/pitch", response_model=PitchResponse)
async def pitch(req: PitchRequest, request: Request, response: Response):
    """Run the full pipeline locally (no distributed execution)."""
    _check_pitch_key(request)
    remaining = _check_rate_limit(request)
    response.headers["X-RateLimit-Limit"] = str(state._RATE_MAX)
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
    response.headers["X-RateLimit-Limit"] = str(state._RATE_MAX)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    job_id = f"job_{int(time.time() * 1000)}"
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
            from orchestrator import _MAX_CONTEXT_CHARS
            prompt = st["prompt"]
            if context:
                if len(context) > _MAX_CONTEXT_CHARS:
                    context = "...[truncated]\n\n" + context[-_MAX_CONTEXT_CHARS:]
                prompt = f"Context from previous subtasks:\n{context}\n\n---\n\nYour task:\n{prompt}"
            task_id = f"build_{st['id']}_{int(time.time() * 1000)}"
            # Soft model routing: if role_model_map specifies a preferred model for
            # builders, only add a "requires" tag when at least one connected node
            # has that model — otherwise fall through to any node (no deadlock).
            preferred_model = get_config().get("role_model_map", {}).get("builder")
            requires: list[str] = []
            if preferred_model:
                model_tag = f"model:{preferred_model}"
                if any(model_tag in set(n.get("capabilities", [])) for n in nodes.values()):
                    requires = [model_tag]

            task_queue.append({
                "task_id": task_id,
                "title": st["title"],
                "prompt": prompt,
                "system": BUILDER_SYSTEM,
                "trace_id": trace_id,
                "job_id": job_id,
                "subtask_id": st["id"],
                "requires": requires,
            })
            _emit("node_task_queued", {"task_id": task_id, "subtask": st["title"], "job_id": job_id, "trace_id": trace_id})
            deadline = time.time() + 600
            while task_id not in task_results:
                if time.time() > deadline:
                    # Timeout — fall back to local inference
                    task_queue[:] = [t for t in task_queue if t["task_id"] != task_id]
                    return await generate(prompt, system=BUILDER_SYSTEM)
                await asyncio.sleep(1)
            tr = task_results.pop(task_id)
            if tr.get("error") or not tr.get("output"):
                return await generate(prompt, system=BUILDER_SYSTEM)
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
        )
        result["results"] = {str(k): v for k, v in result["results"].items()}
        jobs[job_id]["status"] = "complete"
        jobs[job_id]["result"] = result
        _emit("complete", {"task": task, "job_id": job_id, "project_dir": result["project_dir"], "trace_id": trace_id})
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        _emit("error", {"task": task, "job_id": job_id, "message": str(e), "trace_id": trace_id})

    jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
    _db_write_job(jobs[job_id])


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

    job_id = f"job_{int(time.time() * 1000)}"
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
) -> tuple[int, str]:
    """Push one subtask to the worker queue and wait for its result.

    Falls back to local Ollama inference on timeout or worker error.
    Returns (subtask_id, output_text).
    """
    from orchestrator import _MAX_CONTEXT_CHARS

    # Build context from resolved dependencies
    context_parts = []
    for dep_id in st.get("depends_on", []):
        if dep_id in results:
            dep_task = next((s for s in subtasks if s["id"] == dep_id), None)
            label = dep_task["title"] if dep_task else f"Subtask {dep_id}"
            context_parts.append(f"[{label}]:\n{results[dep_id]}")
    context = "\n\n".join(context_parts)
    if len(context) > _MAX_CONTEXT_CHARS:
        context = "...[truncated]\n\n" + context[-_MAX_CONTEXT_CHARS:]

    prompt = st["prompt"]
    if context:
        prompt = f"Context from previous subtasks:\n{context}\n\n---\n\nYour task:\n{prompt}"

    task_id = f"build_{st['id']}_{int(time.time() * 1000)}"
    task_queue.append({
        "task_id": task_id,
        "title": st["title"],
        "prompt": prompt,
        "system": BUILDER_SYSTEM,
    })

    deadline = time.time() + 600
    while task_id not in task_results:
        if time.time() > deadline:
            output = await generate(prompt, system=BUILDER_SYSTEM)
            nodes_used.add("local")
            return st["id"], output
        await asyncio.sleep(1)

    tr = task_results.pop(task_id)
    if tr.get("error") or not tr.get("output"):
        output = await generate(prompt, system=BUILDER_SYSTEM)
        nodes_used.add("local")
    else:
        output = tr["output"]
        nodes_used.add(tr.get("node_id", "unknown"))
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
    remaining = {st["id"]: st for st in subtasks}

    while remaining:
        ready = [st for st in remaining.values()
                 if all(dep_id in results for dep_id in st.get("depends_on", []))]
        if not ready:
            break
        wave_results = await asyncio.gather(*[
            _dispatch_subtask(st, subtasks, results, nodes_used) for st in ready
        ])
        for subtask_id, output in wave_results:
            results[subtask_id] = output
            remaining.pop(subtask_id, None)
            _emit("build", {"task": req.task, "subtask_id": subtask_id, "trace_id": trace_id})

    # 4. Review
    _emit("review_start", {"task": req.task, "trace_id": trace_id})
    review_output = await review(req.task, subtasks, results, memory_context=memory_context)

    # 5. Reviser pass (up to 2 rounds, same as local pipeline)
    rating = _extract_rating(review_output)
    final_output = _extract_final_output(review_output)
    issues = _extract_issues(review_output)
    for _ in range(2):
        if rating != "NEEDS_WORK" or not issues or not final_output:
            break
        revised = await revise(req.task, issues, final_output)
        if len(revised.strip()) <= len(final_output) // 2:
            break
        final_output = revised
        issues = _extract_issues(revised)
        if not issues:
            rating = "PASS"
            break

    # 6. Save output files
    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    project_dir = OUTPUT_DIR / timestamp
    project_dir.mkdir()

    (project_dir / "plan.json").write_text(json.dumps(subtasks, indent=2))
    for st in subtasks:
        safe_title = _re.sub(r"[^\w\s-]", "", st["title"]).strip().replace(" ", "_")
        (project_dir / f"builder_{st['id']}_{safe_title}.md").write_text(results[st["id"]])
    (project_dir / "review.md").write_text(review_output)
    if final_output:
        (project_dir / "output.md").write_text(final_output)

    extract_source = final_output or review_output
    code_files = extract_code_files(extract_source, project_dir)

    log = {
        "task": req.task,
        "timestamp": timestamp,
        "plan": subtasks,
        "results": {str(k): v for k, v in results.items()},
        "review": review_output,
        "rating": rating,
        "code_files": [str(f) for f in code_files],
        "mode": "distributed",
        "nodes_used": list(nodes_used),
        "project_id": req.project_id or "",
    }
    (project_dir / "full_log.json").write_text(json.dumps(log, indent=2))

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
                raw = memory_file.read_text(errors="ignore")
                if len(raw) > SUMMARIZE_THRESHOLD:
                    compressed = await _summarize_memory(raw)
                    if compressed and compressed != raw:
                        memory_file.write_text(compressed)
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
        "mode": "distributed",
        "nodes_used": len(nodes_used),
        "project_id": req.project_id or "",
    }
