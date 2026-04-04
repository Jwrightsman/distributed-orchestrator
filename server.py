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
import io
import json
import sqlite3
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator

import httpx

from orchestrator import (
    run_pipeline, plan, review, revise, BUILDER_SYSTEM,
    _extract_rating, _extract_final_output, _extract_issues,
)
from extract import extract_code_files
from ollama_client import OLLAMA_URL, generate
from ledger import get_standings, get_history, log_contribution
from config import get as get_config

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
_LONG_POLL_TIMEOUT = 25   # seconds to hold GET /tasks/next open waiting for work

# ── Circuit breaker thresholds ────────────────────────────────────────────
_FAILURE_THRESHOLD = 3    # consecutive failures before blacklisting
_BLACKLIST_DURATION = 60  # seconds a blacklisted node sits out

# ── Pitch rate limiting (per IP) ──────────────────────────────────────────
_RATE_WINDOW = 60         # seconds
_RATE_MAX = 5             # max pitches per IP per window
_pitch_timestamps: dict[str, list[float]] = {}   # ip -> list of recent timestamps

# ── Pipeline event log (for dashboard live updates) ──────────────────
pipeline_events: list[dict] = []   # recent events for polling fallback

# ── SQLite event persistence ──────────────────────────────────────────
_DB_PATH = Path("events.db")
_db_lock = threading.Lock()

def _init_db() -> None:
    with _db_lock:
        con = sqlite3.connect(_DB_PATH)
        con.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id   INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                time TEXT NOT NULL,
                data TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id      TEXT PRIMARY KEY,
                task        TEXT,
                project_id  TEXT,
                status      TEXT,
                submitted_at TEXT,
                finished_at TEXT,
                error       TEXT,
                project_dir TEXT,
                rating      TEXT,
                trace_id    TEXT
            )
        """)
        con.commit()
        con.close()


def _db_write_job(job: dict) -> None:
    with _db_lock:
        con = sqlite3.connect(_DB_PATH)
        con.execute(
            """INSERT OR REPLACE INTO jobs
               (job_id, task, project_id, status, submitted_at, finished_at, error, project_dir, rating, trace_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job["job_id"], job.get("task"), job.get("project_id"),
                job.get("status"), job.get("submitted_at"), job.get("finished_at"),
                job.get("error"),
                job["result"]["project_dir"] if job.get("result") else None,
                job["result"].get("rating") if job.get("result") else None,
                job.get("trace_id"),
            ),
        )
        con.commit()
        con.close()


def _db_load_jobs() -> None:
    """Populate in-memory jobs dict from SQLite on startup (last 200 jobs)."""
    try:
        with _db_lock:
            con = sqlite3.connect(_DB_PATH)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM jobs ORDER BY submitted_at DESC LIMIT 200"
            ).fetchall()
            con.close()
        for row in rows:
            jid = row["job_id"]
            if jid not in jobs:
                jobs[jid] = {
                    "job_id": jid,
                    "task": row["task"],
                    "project_id": row["project_id"],
                    "status": row["status"],
                    "submitted_at": row["submitted_at"],
                    "finished_at": row["finished_at"],
                    "error": row["error"],
                    "result": {"project_dir": row["project_dir"], "rating": row["rating"]}
                             if row["project_dir"] else None,
                    "trace_id": row["trace_id"],
                }
    except Exception:
        pass  # non-fatal — in-memory state is still usable

def _db_write_event(event_type: str, event_time: str, data: dict) -> int:
    blob = {k: v for k, v in data.items() if k not in ("type", "time")}
    with _db_lock:
        con = sqlite3.connect(_DB_PATH)
        cur = con.execute(
            "INSERT INTO events (type, time, data) VALUES (?, ?, ?)",
            (event_type, event_time, json.dumps(blob)),
        )
        rowid = cur.lastrowid
        con.commit()
        con.close()
    return rowid

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
    # Replay last 20 persisted events from SQLite so new clients aren't blind
    try:
        with _db_lock:
            con = sqlite3.connect(_DB_PATH)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT id, type, time, data FROM events ORDER BY id DESC LIMIT 20"
            ).fetchall()
            con.close()
        for row in reversed(rows):
            blob = json.loads(row["data"])
            blob.update({"id": row["id"], "type": row["type"], "time": row["time"]})
            await websocket.send_json(blob)
    except Exception:
        pass
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

# ── Circuit breaker state ─────────────────────────────────────────────
node_failure_count: dict[str, int] = {}   # node_id -> consecutive failure count
node_blacklist: dict[str, float] = {}     # node_id -> blacklist_until timestamp

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
    # Optional capability tags — e.g. ["code", "large-context"] or ["gpu", "fast"]
    # Used by the dispatcher to match tasks to capable nodes.
    capabilities: list[str] = []

class TaskResult(BaseModel):
    node_id: str
    output: str | None
    error: str | None = None
    elapsed_seconds: float = 0


# ── Background: clean up stale nodes ────────────────────────────────────
@app.on_event("startup")
async def _start_background_tasks():
    _init_db()
    _db_load_jobs()
    asyncio.create_task(_cleanup_stale_nodes())


_JOB_TTL = 7 * 24 * 3600    # keep finished jobs for 7 days
_RESULT_TTL = 3600           # keep raw task results for 1 hour

async def _cleanup_stale_nodes():
    """Remove stale nodes, reclaim their in-flight tasks, and prune old records.

    Runs every 30 seconds.
    """
    while True:
        await asyncio.sleep(30)
        now = time.time()

        # 1. Dead nodes
        cutoff = now - _NODE_TIMEOUT
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

        # 2. Old task results (only needed long enough for the caller to collect them)
        result_cutoff = now - _RESULT_TTL
        stale_results = [
            tid for tid, r in task_results.items()
            if r.get("completed_at", now) < result_cutoff
        ]
        for tid in stale_results:
            task_results.pop(tid, None)

        # 3. Old async jobs (finished jobs older than 7 days)
        job_cutoff = now - _JOB_TTL
        stale_jobs = []
        for jid, job in jobs.items():
            finished = job.get("finished_at")
            if finished and job["status"] in ("complete", "failed"):
                try:
                    from datetime import datetime, timezone
                    finished_ts = datetime.fromisoformat(finished).timestamp()
                    if finished_ts < job_cutoff:
                        stale_jobs.append(jid)
                except Exception:
                    pass
        for jid in stale_jobs:
            jobs.pop(jid, None)


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
    """Push an event to the pipeline log, SQLite, and broadcast to WebSocket clients."""
    event = {
        "type": event_type,
        "time": datetime.now(timezone.utc).isoformat(),
        **data,
    }
    # Token events are high-frequency — broadcast only, don't pollute the event log
    if event_type != "token":
        pipeline_events.append(event)
        if len(pipeline_events) > 100:
            pipeline_events.pop(0)
        event["id"] = _db_write_event(event_type, event["time"], data)
    asyncio.get_event_loop().create_task(ws_manager.broadcast(event))


def _check_rate_limit(request: Request) -> None:
    """Raise 429 if this IP has exceeded _RATE_MAX pitches in the last _RATE_WINDOW seconds."""
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window_start = now - _RATE_WINDOW
    timestamps = [t for t in _pitch_timestamps.get(ip, []) if t > window_start]
    if len(timestamps) >= _RATE_MAX:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: max {_RATE_MAX} pitches per {_RATE_WINDOW}s. Try again shortly.",
        )
    timestamps.append(now)
    _pitch_timestamps[ip] = timestamps


@app.post("/pitch", response_model=PitchResponse)
async def pitch(req: PitchRequest, request: Request):
    """Run the full pipeline locally (no distributed execution)."""
    _check_rate_limit(request)
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


@app.get("/events")
async def get_events(since: int = 0):
    """Get pipeline events. since=0 returns last 100; since=N returns events with id > N."""
    with _db_lock:
        con = sqlite3.connect(_DB_PATH)
        con.row_factory = sqlite3.Row
        if since == 0:
            rows = con.execute(
                "SELECT id, type, time, data FROM events ORDER BY id DESC LIMIT 100"
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = con.execute(
                "SELECT id, type, time, data FROM events WHERE id > ? ORDER BY id",
                (since,),
            ).fetchall()
        con.close()

    events = []
    for row in rows:
        blob = json.loads(row["data"])
        blob["id"]   = row["id"]
        blob["type"] = row["type"]
        blob["time"] = row["time"]
        events.append(blob)
    return {"events": events}


# ── Async job system ─────────────────────────────────────────────────

@app.post("/pitch/async")
async def pitch_async(req: PitchRequest, request: Request):
    """Submit a task and return immediately with a job_id.

    Poll GET /jobs/{job_id} for status and results.
    WebSocket clients on /ws/events receive live events as the job runs.
    """
    _check_rate_limit(request)
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

    try:
        result = await run_pipeline(task, on_plan=on_plan, on_build=on_build, on_review_start=on_review_start, on_token=on_token, project_id=project_id)
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


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Get the status and result of an async job.

    Falls back to SQLite for jobs not in the current in-memory store
    (e.g. jobs that completed before the last server restart).
    """
    if job_id not in jobs:
        # Try SQLite
        try:
            with _db_lock:
                con = sqlite3.connect(_DB_PATH)
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


@app.get("/history/{timestamp}/download")
async def download_history(timestamp: str):
    """Download all files from a run as a ZIP archive."""
    run_dir = OUTPUT_DIR / timestamp
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(run_dir.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(run_dir))
    zip_buffer.seek(0)

    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=output_{timestamp}.zip"},
    )


# ── Gallery ──────────────────────────────────────────────────────────
@app.get("/gallery")
async def gallery(limit: int = 30):
    """Return completed runs as gallery cards — for the Swarm Gallery page."""
    cards = []
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            log_file = d / "full_log.json"
            if not log_file.exists():
                continue
            try:
                log = json.loads(log_file.read_text())
                rating = log.get("rating", "?")
                # Read first 300 chars of final output as preview
                preview = ""
                output_file = d / "output.md"
                if output_file.exists():
                    preview = output_file.read_text(errors="ignore")[:300]
                elif log.get("review"):
                    from orchestrator import _extract_final_output
                    fo = _extract_final_output(log["review"])
                    preview = (fo or "")[:300]
                # Code file list
                code_dir = d / "code"
                code_files = [f.name for f in sorted(code_dir.iterdir())] if code_dir.exists() else []
                cards.append({
                    "timestamp": log.get("timestamp", d.name),
                    "task": log.get("task", "Unknown"),
                    "rating": rating,
                    "subtask_count": len(log.get("plan", [])),
                    "preview": preview.strip(),
                    "code_files": code_files,
                    "project_id": log.get("project_id") or None,
                })
            except (json.JSONDecodeError, OSError):
                pass
            if len(cards) >= limit:
                break
    return {"cards": cards, "count": len(cards)}


# ── Ledger / Standings ───────────────────────────────────────────────
@app.get("/standings")
async def standings():
    """Get contributor standings sorted by credits."""
    return {"standings": get_standings()}


@app.get("/metrics")
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

    return {
        "tasks_completed_total": tasks_completed_total,
        "tasks_in_queue":        len(task_queue),
        "tasks_inflight":        len(task_inflight),
        "nodes_online":          len(nodes),
        "nodes_blacklisted":     len(blacklisted_nodes),
        "jobs_running":          sum(1 for j in jobs.values() if j["status"] == "running"),
        "jobs_queued":           sum(1 for j in jobs.values() if j["status"] == "queued"),
        "avg_task_latency_seconds": avg_latency,
    }


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


# ── Node auth ────────────────────────────────────────────────────────
def _check_node_auth(request: Request):
    """Raise 401 if node_secret is configured and the request doesn't present it.

    Nodes must include 'X-Node-Secret: <value>' in their request headers.
    When node_secret is empty in config, all nodes are allowed (trusted-network mode).
    """
    secret = get_config().get("node_secret", "")
    if not secret:
        return  # auth disabled
    provided = request.headers.get("X-Node-Secret", "")
    if provided != secret:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Node-Secret header")


# ── Node management ──────────────────────────────────────────────────
@app.post("/nodes/register")
async def register_node(reg: NodeRegistration, request: Request):
    _check_node_auth(request)
    nodes[reg.node_id] = {
        "node_id": reg.node_id,
        "model": reg.model,
        "platform": reg.platform,
        "machine": reg.machine,
        "hostname": reg.hostname,
        "cpu_count": reg.cpu_count,
        "ram_gb": reg.ram_gb,
        "gpu": reg.gpu,
        "capabilities": reg.capabilities,
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
    deadline = time.time() + _LONG_POLL_TIMEOUT
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


@app.post("/tasks/{task_id}/result")
async def submit_result(task_id: str, result: TaskResult, request: Request):
    _check_node_auth(request)
    """Worker submits completed task."""
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
        if count >= _FAILURE_THRESHOLD:
            node_blacklist[result.node_id] = time.time() + _BLACKLIST_DURATION
            _emit("node_blacklisted", {
                "node_id": result.node_id,
                "failure_count": count,
                "blacklist_seconds": _BLACKLIST_DURATION,
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

    Planner and reviewer run locally. Builder subtasks go to the worker
    task queue and execute in parallel across connected nodes. Falls back
    to local run_pipeline if no nodes are connected.

    Full feature parity with /pitch: project memory, reviser pass, code
    extraction, rating, events, and ZIP-downloadable output.
    """
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
    if len(task_queue) >= _MAX_TASK_QUEUE:
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
    import re as _re
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
