"""
FastAPI server for the orchestrator.

Runs on the main machine. Accepts task pitches, decomposes them,
and distributes subtasks to worker nodes across the network.

Endpoints:
  POST /pitch              — submit a task, get back the full pipeline result
  GET  /health             — check if the server and Ollama are up
  POST /nodes/register     — worker node announces itself
  GET  /nodes              — list connected nodes
  GET  /tasks/next         — worker node asks for work
  POST /tasks/{id}/result  — worker node returns completed work
  POST /pitch/distributed  — pitch a task that runs across the network

Usage:
  python -m uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

import httpx

from orchestrator import run_pipeline, plan, review, BUILDER_SYSTEM
from ollama_client import OLLAMA_URL, generate
from ledger import get_standings, get_history, log_contribution

from dashboard import router as dashboard_router

app = FastAPI(title="Distributed AI Orchestrator", version="0.3.0")
app.include_router(dashboard_router)

# ── Global exception handler — always return JSON, never leak stack traces ──
@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)},
    )

# ── Node staleness threshold (seconds) ───────────────────────────────────
_NODE_TIMEOUT = 90
_MAX_TASK_QUEUE = 100

# ── Pipeline event log (for dashboard live updates) ──────────────────
pipeline_events: list[dict] = []   # recent events for polling fallback

# ── WebSocket connection manager ──────────────────────────────────────
class _WSManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket):
        self._connections.discard(ws) if hasattr(self._connections, 'discard') else None
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in list(self._connections):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = _WSManager()


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    """WebSocket endpoint — clients receive pipeline events in real time."""
    await ws_manager.connect(websocket)
    # Send recent history so the client doesn't start blind
    for event in pipeline_events[-20:]:
        try:
            await websocket.send_json(event)
        except Exception:
            break
    try:
        while True:
            # Keep alive — ignore any incoming messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)

# ── In-memory state ──────────────────────────────────────────────────
nodes: dict[str, dict] = {}          # node_id -> info
task_queue: list[dict] = []          # pending tasks for workers
task_results: dict[str, dict] = {}   # task_id -> result
task_inflight: dict[str, dict] = {}  # task_id -> task (assigned but not yet returned)

# ── Async job store ──────────────────────────────────────────────────
# Jobs allow /pitch/async to return immediately with a job_id.
# Status: "queued" | "running" | "complete" | "failed"
jobs: dict[str, dict] = {}          # job_id -> job record
OUTPUT_DIR = Path("output")


# ── Models ───────────────────────────────────────────────────────────
class PitchRequest(BaseModel):
    task: str
    project_id: str | None = None   # optional: continue an existing project

    @field_validator("task")
    @classmethod
    def task_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("task cannot be empty")
        if len(v) > 1000:
            raise ValueError("task must be 1000 characters or fewer")
        return v

class PitchResponse(BaseModel):
    project_dir: str
    plan: list[dict]
    results: dict[str, str]
    review: str

class NodeRegistration(BaseModel):
    node_id: str
    model: str
    platform: str
    machine: str
    hostname: str
    cpu_count: int | None = None
    ram_gb: float | None = None
    gpu: str | None = None

class TaskResult(BaseModel):
    node_id: str
    output: str | None
    error: str | None = None
    elapsed_seconds: float = 0


# ── Background: clean up stale nodes ────────────────────────────────────
@app.on_event("startup")
async def _start_background_tasks():
    asyncio.create_task(_cleanup_stale_nodes())


async def _cleanup_stale_nodes():
    """Remove nodes that haven't checked in within _NODE_TIMEOUT seconds.

    Any in-flight tasks assigned to a dead node are returned to the queue
    so another node (or local fallback) can pick them up.
    """
    while True:
        await asyncio.sleep(30)
        cutoff = time.time() - _NODE_TIMEOUT
        stale = [nid for nid, n in nodes.items() if n.get("last_seen", 0) < cutoff]
        for nid in stale:
            nodes.pop(nid, None)
            # Reclaim any in-flight tasks assigned to this dead node
            reclaimed = [
                tid for tid, t in task_inflight.items()
                if t.get("assigned_to") == nid
            ]
            for tid in reclaimed:
                task = task_inflight.pop(tid)
                task.pop("assigned_to", None)
                task.pop("assigned_at", None)
                task_queue.append(task)
                _emit("task_reclaimed", {"task_id": tid, "node_id": nid})


# ── Health ───────────────────────────────────────────────────────────
@app.get("/health")
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
    }


# ── Local pipeline ───────────────────────────────────────────────────
def _emit(event_type: str, data: dict):
    """Push an event to the pipeline log and broadcast to WebSocket clients."""
    event = {
        "type": event_type,
        "time": datetime.now(timezone.utc).isoformat(),
        **data,
    }
    pipeline_events.append(event)
    if len(pipeline_events) > 100:
        pipeline_events.pop(0)
    # Fire-and-forget broadcast — don't await in a sync context
    asyncio.get_event_loop().create_task(ws_manager.broadcast(event))


@app.post("/pitch", response_model=PitchResponse)
async def pitch(req: PitchRequest):
    """Run the full pipeline locally (no distributed execution)."""
    _emit("pitch", {"task": req.task})

    def on_plan(subtasks):
        _emit("plan", {"task": req.task, "subtasks": [s["title"] for s in subtasks]})

    def on_build(subtask, output):
        _emit("build", {"task": req.task, "subtask": subtask["title"], "subtask_id": subtask["id"]})

    def on_review_start():
        _emit("review_start", {"task": req.task})

    try:
        result = await run_pipeline(req.task, on_plan=on_plan, on_build=on_build, on_review_start=on_review_start, project_id=req.project_id)
    except ValueError as e:
        _emit("error", {"task": req.task, "message": str(e)})
        raise HTTPException(status_code=422, detail=str(e))

    result["results"] = {str(k): v for k, v in result["results"].items()}
    _emit("complete", {"task": req.task, "project_dir": result["project_dir"]})
    return result


@app.get("/events")
async def get_events(since: int = 0):
    """Get recent pipeline events (for dashboard polling)."""
    return {"events": pipeline_events[since:]}


# ── Async job system ─────────────────────────────────────────────────

@app.post("/pitch/async")
async def pitch_async(req: PitchRequest):
    """Submit a task and return immediately with a job_id.

    Poll GET /jobs/{job_id} for status and results.
    WebSocket clients on /ws/events receive live events as the job runs.
    """
    job_id = f"job_{int(time.time() * 1000)}"
    jobs[job_id] = {
        "job_id": job_id,
        "task": req.task,
        "project_id": req.project_id,
        "status": "queued",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "result": None,
        "error": None,
    }
    asyncio.create_task(_run_job(job_id, req.task, req.project_id))
    return {"job_id": job_id, "status": "queued", "project_id": req.project_id}


async def _run_job(job_id: str, task: str, project_id: str | None = None):
    """Background task: run the full pipeline for a job."""
    jobs[job_id]["status"] = "running"
    _emit("pitch", {"task": task, "job_id": job_id})

    def on_plan(subtasks):
        _emit("plan", {"task": task, "job_id": job_id, "subtasks": [s["title"] for s in subtasks]})

    def on_build(subtask, output):
        _emit("build", {"task": task, "job_id": job_id, "subtask": subtask["title"], "subtask_id": subtask["id"]})

    def on_review_start():
        _emit("review_start", {"task": task, "job_id": job_id})

    try:
        result = await run_pipeline(task, on_plan=on_plan, on_build=on_build, on_review_start=on_review_start, project_id=project_id)
        result["results"] = {str(k): v for k, v in result["results"].items()}
        jobs[job_id]["status"] = "complete"
        jobs[job_id]["result"] = result
        _emit("complete", {"task": task, "job_id": job_id, "project_dir": result["project_dir"]})
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        _emit("error", {"task": task, "job_id": job_id, "message": str(e)})

    jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Get the status and result of an async job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    # Don't return the full results dict in the status response — keep it light
    return {
        "job_id": job["job_id"],
        "task": job["task"],
        "status": job["status"],
        "submitted_at": job["submitted_at"],
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "project_dir": job["result"]["project_dir"] if job["result"] else None,
        "plan": job["result"]["plan"] if job["result"] else None,
        "rating": job["result"].get("rating") if job["result"] else None,
    }


@app.get("/jobs")
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


@app.get("/history")
async def history():
    """List past pipeline runs from the output folder."""
    runs = []
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
            if d.is_dir():
                log_file = d / "full_log.json"
                if log_file.exists():
                    try:
                        log = json.loads(log_file.read_text())
                        # Quick rating check from review.md if available
                        rating = log.get("rating", "?")
                        if rating == "?":
                            review_f = d / "review.md"
                            if review_f.exists():
                                for line in review_f.read_text(errors="ignore").splitlines():
                                    if line.strip() in ("PASS", "NEEDS_WORK", "FAIL"):
                                        rating = line.strip()
                                        break
                        runs.append({
                            "timestamp": log.get("timestamp", d.name),
                            "task": log.get("task", "Unknown"),
                            "subtask_count": len(log.get("plan", [])),
                            "rating": rating,
                            "dir": str(d),
                        })
                    except json.JSONDecodeError:
                        pass
            if len(runs) >= 20:
                break
    return {"runs": runs, "count": len(runs)}


@app.get("/history/{timestamp}")
async def history_detail(timestamp: str):
    """Get full details of a past pipeline run."""
    run_dir = OUTPUT_DIR / timestamp
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")

    log_file = run_dir / "full_log.json"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log file not found")

    try:
        log = json.loads(log_file.read_text())
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Corrupt log file")

    review_file = run_dir / "review.md"
    review_content = review_file.read_text() if review_file.exists() else ""

    output_file = run_dir / "output.md"
    final_output = output_file.read_text() if output_file.exists() else ""

    # Derive rating from review file (most reliable source)
    rating = "?"
    for line in review_content.splitlines():
        if line.strip() in ("PASS", "NEEDS_WORK", "FAIL"):
            rating = line.strip()
            break

    # Build code file list from the code/ subdir
    code_dir = run_dir / "code"
    code_files = [f.name for f in sorted(code_dir.iterdir())] if code_dir.exists() else []

    return {
        "task": log.get("task"),
        "timestamp": log.get("timestamp"),
        "plan": log.get("plan", []),
        "results": log.get("results", {}),
        "review": review_content,
        "final_output": final_output,
        "rating": rating,
        "code_files": code_files,
    }


# ── Ledger / Standings ───────────────────────────────────────────────
@app.get("/standings")
async def standings():
    """Get contributor standings sorted by credits."""
    return {"standings": get_standings()}


@app.get("/ledger")
async def ledger(contributor: str | None = None, limit: int = 50):
    """Get recent ledger entries."""
    return {"entries": get_history(contributor, limit)}


# ── Projects ─────────────────────────────────────────────────────────

class NewProjectRequest(BaseModel):
    name: str
    initial_task: str


@app.get("/projects")
async def get_projects():
    """List all projects."""
    from memory import list_projects
    return {"projects": list_projects()}


@app.post("/projects")
async def create_new_project(req: NewProjectRequest):
    """Create a new project and return its ID."""
    from memory import create_project
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Project name cannot be empty")
    project_id = create_project(req.name.strip(), req.initial_task.strip())
    return {"project_id": project_id, "name": req.name.strip()}


@app.get("/projects/{project_id}")
async def get_project(project_id: str):
    """Get project metadata and memory."""
    from memory import load_project, get_memory_context, PROJECTS_DIR
    try:
        meta = load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    memory = get_memory_context(project_id)
    # List iteration dirs
    iter_dir = PROJECTS_DIR / project_id / "iterations"
    iterations = sorted([d.name for d in iter_dir.iterdir() if d.is_dir()], key=lambda x: int(x)) if iter_dir.exists() else []
    return {**meta, "memory_context": memory, "iterations": iterations}


# ── Node management ──────────────────────────────────────────────────
@app.post("/nodes/register")
async def register_node(reg: NodeRegistration):
    nodes[reg.node_id] = {
        "node_id": reg.node_id,
        "model": reg.model,
        "platform": reg.platform,
        "machine": reg.machine,
        "hostname": reg.hostname,
        "cpu_count": reg.cpu_count,
        "ram_gb": reg.ram_gb,
        "gpu": reg.gpu,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "last_seen": time.time(),
        "tasks_completed": 0,
        "credits_earned": 0,
        "current_task": None,
    }
    return {"message": f"Welcome, {reg.node_id}. You are node #{len(nodes)} in the network."}


@app.get("/nodes")
async def list_nodes():
    return {"nodes": list(nodes.values()), "count": len(nodes)}


# ── Task distribution ────────────────────────────────────────────────
@app.get("/tasks/next")
async def next_task(node_id: str):
    """Worker asks for the next available task."""
    if node_id in nodes:
        nodes[node_id]["last_seen"] = time.time()

    if not task_queue:
        return Response(status_code=204)  # No work

    task = task_queue.pop(0)
    task["assigned_to"] = node_id
    task["assigned_at"] = time.time()

    if node_id in nodes:
        nodes[node_id]["current_task"] = task.get("title", task["task_id"])
    task_inflight[task["task_id"]] = task
    _emit("node_busy", {"node_id": node_id, "task_title": task.get("title", task["task_id"])})

    return task


@app.post("/tasks/{task_id}/result")
async def submit_result(task_id: str, result: TaskResult):
    """Worker submits completed task."""
    task_inflight.pop(task_id, None)
    task_results[task_id] = {
        "task_id": task_id,
        "node_id": result.node_id,
        "output": result.output,
        "error": result.error,
        "elapsed_seconds": result.elapsed_seconds,
        "completed_at": time.time(),
    }
    credits_earned = 0
    if result.node_id in nodes:
        nodes[result.node_id]["tasks_completed"] += 1
        nodes[result.node_id]["last_seen"] = time.time()
        nodes[result.node_id]["current_task"] = None
    if result.output and not result.error:
        credits_earned = 5
        log_contribution(result.node_id, "compute", credits=credits_earned, task=task_id)
        if result.node_id in nodes:
            nodes[result.node_id]["credits_earned"] = nodes[result.node_id].get("credits_earned", 0) + credits_earned
    _emit("node_idle", {
        "node_id": result.node_id,
        "credits_earned": credits_earned,
        "elapsed_seconds": result.elapsed_seconds,
        "success": bool(result.output and not result.error),
    })
    return {"status": "accepted", "credits_earned": credits_earned}


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


@app.post("/pitch/distributed")
async def pitch_distributed(req: PitchRequest):
    """Pitch a task that gets distributed across connected nodes.

    The planner and reviewer run locally on the orchestrator.
    Builder subtasks get pushed to the task queue for worker nodes.
    Falls back to local execution if no nodes are connected.
    """
    # 1. Plan (runs locally on orchestrator)
    try:
        subtasks = await plan(req.task)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # 2. If no worker nodes, fall back to local
    if not nodes:
        try:
            result = await run_pipeline(req.task)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        result["results"] = {str(k): v for k, v in result["results"].items()}
        result["mode"] = "local"
        return result

    # 3. Distribute builder tasks to worker nodes (parallel waves by dependency level)
    from orchestrator import _MAX_CONTEXT_CHARS

    results: dict[int, str] = {}
    nodes_used: set[str] = set()

    if len(task_queue) >= _MAX_TASK_QUEUE:
        raise HTTPException(status_code=503, detail="Task queue is full — too many pending tasks")

    remaining = {st["id"]: st for st in subtasks}

    while remaining:
        # Find subtasks whose dependencies are all resolved
        ready = [
            st for st in remaining.values()
            if all(dep_id in results for dep_id in st.get("depends_on", []))
        ]
        if not ready:
            break  # shouldn't happen with valid dep graph

        # Launch all ready tasks in parallel
        wave_results = await asyncio.gather(*[
            _dispatch_subtask(st, subtasks, results, nodes_used)
            for st in ready
        ])
        for subtask_id, output in wave_results:
            results[subtask_id] = output
            remaining.pop(subtask_id, None)

    # 4. Review (runs locally on orchestrator)
    review_output = await review(req.task, subtasks, results)

    # 5. Save
    import re
    from orchestrator import _extract_final_output
    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    project_dir = OUTPUT_DIR / timestamp
    project_dir.mkdir()

    (project_dir / "plan.json").write_text(json.dumps(subtasks, indent=2))
    for st in subtasks:
        safe_title = re.sub(r"[^\w\s-]", "", st["title"]).strip().replace(" ", "_")
        (project_dir / f"builder_{st['id']}_{safe_title}.md").write_text(results[st["id"]])
    (project_dir / "review.md").write_text(review_output)
    final_output = _extract_final_output(review_output)
    if final_output:
        (project_dir / "output.md").write_text(final_output)

    log = {
        "task": req.task,
        "timestamp": timestamp,
        "plan": subtasks,
        "results": {str(k): v for k, v in results.items()},
        "review": review_output,
        "mode": "distributed",
    }
    (project_dir / "full_log.json").write_text(json.dumps(log, indent=2))

    return {
        "project_dir": str(project_dir),
        "plan": subtasks,
        "results": {str(k): v for k, v in results.items()},
        "review": review_output,
        "mode": "distributed",
        "nodes_used": len(nodes_used),
    }
