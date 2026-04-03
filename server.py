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

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

import httpx

from orchestrator import run_pipeline, plan, review, BUILDER_SYSTEM
from ollama_client import OLLAMA_URL, generate

from dashboard import router as dashboard_router

app = FastAPI(title="Distributed AI Orchestrator", version="0.3.0")
app.include_router(dashboard_router)

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


# ── Health ───────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
        return {
            "status": "ok",
            "ollama": "connected",
            "models": models,
            "nodes_online": len(nodes),
            "tasks_pending": len(task_queue),
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Ollama unavailable: {e}")


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
    if not req.task.strip():
        raise HTTPException(status_code=400, detail="Task cannot be empty")

    _emit("pitch", {"task": req.task})

    def on_plan(subtasks):
        _emit("plan", {"task": req.task, "subtasks": [s["title"] for s in subtasks]})

    def on_build(subtask, output):
        _emit("build", {"task": req.task, "subtask": subtask["title"], "subtask_id": subtask["id"]})

    def on_review_start():
        _emit("review_start", {"task": req.task})

    result = await run_pipeline(req.task, on_plan=on_plan, on_build=on_build, on_review_start=on_review_start)
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
    return {"status": "accepted"}


# ── Distributed pipeline ────────────────────────────────────────────
@app.post("/pitch/distributed")
async def pitch_distributed(req: PitchRequest):
    """Pitch a task that gets distributed across connected nodes.

    The planner and reviewer run locally on the orchestrator.
    Builder subtasks get pushed to the task queue for worker nodes.
    Falls back to local execution if no nodes are connected.
    """
    if not req.task.strip():
        raise HTTPException(status_code=400, detail="Task cannot be empty")

    # 1. Plan (runs locally on orchestrator)
    subtasks = await plan(req.task)

    # 2. If no worker nodes, fall back to local
    if not nodes:
        result = await run_pipeline(req.task)
        result["results"] = {str(k): v for k, v in result["results"].items()}
        result["mode"] = "local"
        return result

    # 3. Distribute builder tasks to worker nodes
    results: dict[int, str] = {}

    for st in sorted(subtasks, key=lambda s: s["id"]):
        # Build context from dependencies
        context_parts = []
        for dep_id in st.get("depends_on", []):
            if dep_id in results:
                dep_task = next((s for s in subtasks if s["id"] == dep_id), None)
                label = dep_task["title"] if dep_task else f"Subtask {dep_id}"
                context_parts.append(f"[{label}]:\n{results[dep_id]}")
        context = "\n\n".join(context_parts)

        prompt = st["prompt"]
        if context:
            prompt = f"Context from previous subtasks:\n{context}\n\n---\n\nYour task:\n{prompt}"

        task_id = f"build_{st['id']}_{int(time.time())}"

        # Push to queue
        task_queue.append({
            "task_id": task_id,
            "title": st["title"],
            "prompt": prompt,
            "system": BUILDER_SYSTEM,
        })

        # Wait for result (poll)
        timeout = time.time() + 600
        while task_id not in task_results:
            if time.time() > timeout:
                # Timeout — fall back to local execution for this task
                results[st["id"]] = await generate(prompt, system=BUILDER_SYSTEM)
                break
            await asyncio.sleep(1)
        else:
            tr = task_results.pop(task_id)
            if tr.get("error") or not tr.get("output"):
                # Node failed — run locally
                results[st["id"]] = await generate(prompt, system=BUILDER_SYSTEM)
            else:
                results[st["id"]] = tr["output"]

    # 4. Review (runs locally on orchestrator)
    review_output = await review(req.task, subtasks, results)

    # 5. Save
    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    project_dir = OUTPUT_DIR / f"{timestamp}"
    project_dir.mkdir()

    import re
    (project_dir / "plan.json").write_text(json.dumps(subtasks, indent=2))
    for st in subtasks:
        safe_title = re.sub(r"[^\w\s-]", "", st["title"]).strip().replace(" ", "_")
        (project_dir / f"builder_{st['id']}_{safe_title}.md").write_text(results[st["id"]])
    (project_dir / "review.md").write_text(review_output)

    return {
        "project_dir": str(project_dir),
        "plan": subtasks,
        "results": {str(k): v for k, v in results.items()},
        "review": review_output,
        "mode": "distributed",
        "nodes_used": len(set(
            task_results.get(f"build_{st['id']}_{int(time.time())}", {}).get("node_id", "local")
            for st in subtasks
        )),
    }
