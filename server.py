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

from fastapi import FastAPI, HTTPException, Request, Response
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
pipeline_events: list[dict] = []   # recent events for SSE streaming

# ── In-memory state ──────────────────────────────────────────────────
nodes: dict[str, dict] = {}          # node_id -> info
task_queue: list[dict] = []          # pending tasks for workers
task_results: dict[str, dict] = {}   # task_id -> result
OUTPUT_DIR = Path("output")


# ── Models ───────────────────────────────────────────────────────────
class PitchRequest(BaseModel):
    task: str

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
    """Remove nodes that haven't checked in within _NODE_TIMEOUT seconds."""
    while True:
        await asyncio.sleep(30)
        cutoff = time.time() - _NODE_TIMEOUT
        stale = [nid for nid, n in nodes.items() if n.get("last_seen", 0) < cutoff]
        for nid in stale:
            nodes.pop(nid, None)


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
    """Push an event to the pipeline log for the dashboard."""
    pipeline_events.append({
        "type": event_type,
        "time": datetime.now(timezone.utc).isoformat(),
        **data,
    })
    # Keep only last 100 events
    if len(pipeline_events) > 100:
        pipeline_events.pop(0)


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
        result = await run_pipeline(req.task, on_plan=on_plan, on_build=on_build, on_review_start=on_review_start)
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
                        runs.append({
                            "timestamp": log.get("timestamp", d.name),
                            "task": log.get("task", "Unknown"),
                            "subtask_count": len(log.get("plan", [])),
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

    return {
        "task": log.get("task"),
        "timestamp": log.get("timestamp"),
        "plan": log.get("plan", []),
        "results": log.get("results", {}),
        "review": review_content,
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


# ── Node management ──────────────────────────────────────────────────
@app.post("/nodes/register")
async def register_node(reg: NodeRegistration):
    nodes[reg.node_id] = {
        "node_id": reg.node_id,
        "model": reg.model,
        "platform": reg.platform,
        "machine": reg.machine,
        "hostname": reg.hostname,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "last_seen": time.time(),
        "tasks_completed": 0,
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
    return task


@app.post("/tasks/{task_id}/result")
async def submit_result(task_id: str, result: TaskResult):
    """Worker submits completed task."""
    task_results[task_id] = {
        "task_id": task_id,
        "node_id": result.node_id,
        "output": result.output,
        "error": result.error,
        "elapsed_seconds": result.elapsed_seconds,
        "completed_at": time.time(),
    }
    if result.node_id in nodes:
        nodes[result.node_id]["tasks_completed"] += 1
        nodes[result.node_id]["last_seen"] = time.time()
    # Log to ledger
    if result.output and not result.error:
        log_contribution(result.node_id, "compute", credits=5, task=task_id)
    return {"status": "accepted"}


# ── Distributed pipeline ────────────────────────────────────────────
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

    # 3. Distribute builder tasks to worker nodes
    results: dict[int, str] = {}
    nodes_used: set[str] = set()

    if len(task_queue) >= _MAX_TASK_QUEUE:
        raise HTTPException(status_code=503, detail="Task queue is full — too many pending tasks")

    for st in sorted(subtasks, key=lambda s: s["id"]):
        # Build context from dependencies (reuse the same truncation logic as local)
        from orchestrator import _MAX_CONTEXT_CHARS
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

        # Wait for result (poll)
        deadline = time.time() + 600
        while task_id not in task_results:
            if time.time() > deadline:
                # Timeout — fall back to local execution for this subtask
                results[st["id"]] = await generate(prompt, system=BUILDER_SYSTEM)
                nodes_used.add("local")
                break
            await asyncio.sleep(1)
        else:
            tr = task_results.pop(task_id)
            if tr.get("error") or not tr.get("output"):
                results[st["id"]] = await generate(prompt, system=BUILDER_SYSTEM)
                nodes_used.add("local")
            else:
                results[st["id"]] = tr["output"]
                nodes_used.add(tr.get("node_id", "unknown"))

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
