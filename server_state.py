"""
Shared server state and infrastructure.

Everything the route modules have in common lives here: in-memory orchestration
state, SQLite persistence, the WebSocket manager, event emission, rate limiting,
node auth, and the request/response models. Route modules import from here;
server.py assembles the app.
"""

import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request, WebSocket
from pydantic import BaseModel, field_validator

from config import get as get_config

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

_JOB_TTL = 7 * 24 * 3600    # keep finished jobs for 7 days
_RESULT_TTL = 3600           # keep raw task results for 1 hour

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


# ── Event emission ────────────────────────────────────────────────────
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


# ── Rate limiting ─────────────────────────────────────────────────────
def _check_rate_limit(request: Request) -> int:
    """Raise 429 if this IP has exceeded _RATE_MAX pitches in the last _RATE_WINDOW seconds.

    Returns the number of remaining pitches allowed in the current window.
    """
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window_start = now - _RATE_WINDOW
    timestamps = [t for t in _pitch_timestamps.get(ip, []) if t > window_start]
    if len(timestamps) >= _RATE_MAX:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: max {_RATE_MAX} pitches per {_RATE_WINDOW}s. Try again shortly.",
            headers={
                "X-RateLimit-Limit": str(_RATE_MAX),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(min(timestamps)) + _RATE_WINDOW),
            },
        )
    timestamps.append(now)
    _pitch_timestamps[ip] = timestamps
    return _RATE_MAX - len(timestamps)


# ── Pitch auth ───────────────────────────────────────────────────────
def _check_pitch_key(request: Request):
    """Raise 401 if pitch_key is configured and the request doesn't present it.

    Pitchers must include 'X-Pitch-Key: <value>' in their request headers.
    When pitch_key is empty in config, pitching is open (trusted-network mode).
    """
    key = get_config().get("pitch_key", "")
    if not key:
        return  # auth disabled
    provided = request.headers.get("X-Pitch-Key", "")
    if provided != key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Pitch-Key header")


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


# ── Request/response models ──────────────────────────────────────────
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


class TokenBatch(BaseModel):
    node_id: str
    tokens: str  # accumulated token string for this batch


class NewProjectRequest(BaseModel):
    name: str
    initial_task: str


# ── Output directory size cap ────────────────────────────────────────
def _prune_output_dir() -> list[str]:
    """Delete oldest runs until output/ is under the configured size cap.

    Returns the list of pruned run-directory names. No-op when the cap is 0
    or the directory doesn't exist. Run dirs are timestamp-named, so sorting
    by name is sorting by age.
    """
    import shutil

    max_mb = get_config().get("output_max_mb", 0)
    if not max_mb or not OUTPUT_DIR.exists():
        return []
    cap_bytes = max_mb * 1024 * 1024

    run_dirs = sorted(d for d in OUTPUT_DIR.iterdir() if d.is_dir())
    sizes = {}
    total = 0
    for d in run_dirs:
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        sizes[d] = size
        total += size

    pruned = []
    for d in run_dirs:  # oldest first
        if total <= cap_bytes:
            break
        try:
            shutil.rmtree(d)
            total -= sizes[d]
            pruned.append(d.name)
        except OSError:
            pass
    if pruned:
        try:
            _emit("output_pruned", {"runs_deleted": pruned, "cap_mb": max_mb})
        except Exception:
            pass  # pruning must never fail on event emission (e.g. no event loop)
    return pruned


# ── Background cleanup loop ──────────────────────────────────────────
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
                    finished_ts = datetime.fromisoformat(finished).timestamp()
                    if finished_ts < job_cutoff:
                        stale_jobs.append(jid)
                except Exception:
                    pass
        for jid in stale_jobs:
            jobs.pop(jid, None)

        # 4. Prune SQLite event log — keep only the last 2000 rows
        try:
            with _db_lock:
                con = sqlite3.connect(_DB_PATH)
                con.execute(
                    "DELETE FROM events WHERE id NOT IN "
                    "(SELECT id FROM events ORDER BY id DESC LIMIT 2000)"
                )
                con.commit()
                con.close()
        except Exception:
            pass

        # 5. Enforce the output/ directory size cap (config: output_max_mb)
        try:
            _prune_output_dir()
        except Exception:
            pass
