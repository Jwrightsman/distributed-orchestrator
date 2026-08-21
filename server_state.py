"""
Shared server state and infrastructure.

Everything the route modules have in common lives here: in-memory orchestration
state, SQLite persistence, the WebSocket manager, event emission, rate limiting,
node auth, and the request/response models. Route modules import from here;
server.py assembles the app.
"""

import asyncio
import json
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request, WebSocket
from pydantic import BaseModel, Field, field_validator

from config import get as get_config
from execution.contracts import (
    ConfidentialityV1,
    ExecutionRequirementsV1,
    OutputContractV1,
    PlacementV1,
    StrategyNameV1,
    StrategyOptionsV1,
    VerificationPolicyV1,
)
from verification import VerificationPool

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

# ── Public pitch page limits (/try — keyless, so much harsher) ────────────
_PUBLIC_RATE_WINDOW = 3600   # seconds
_PUBLIC_RATE_MAX = 2         # pitches per IP per window
_PUBLIC_TASK_MAX = 300       # task length cap (chars)
_PUBLIC_MAX_ACTIVE = 3       # concurrent public jobs across all visitors
_public_pitch_timestamps: dict[str, list[float]] = {}

# Basic content filter for keyless public pitching. Substring match, so it
# over-blocks (e.g. "hackathon") — acceptable at this trust tier.
_PUBLIC_BLOCKLIST = (
    "hack", "malware", "ransomware", "exploit", "phishing", "ddos", "botnet",
    "keylogger", "spyware", "crack password", "bypass", "nude", "porn",
    "sexual", "nsfw", "bomb", "weapon", "ghost gun", "suicide", "kill ",
)

# ── Pipeline event log (for dashboard live updates) ──────────────────
pipeline_events: list[dict] = []   # recent events for polling fallback

# ── In-memory state ──────────────────────────────────────────────────
nodes: dict[str, dict] = {}          # node_id -> info

# ── Task attempt binding ─────────────────────────────────────────────
#
# node_secret is *network admission*, not per-node identity: everyone holding it
# presents the same credential. Result submission used to trust the node_id in
# the request body and locate the task by task_id alone, so any admitted node
# could submit a result attributed to a different node and take its credit.
#
# The fix is deliberately small. When a task is handed out the server mints an
# attempt: an id and a nonce, both unguessable and both distinct from task_id
# (which is visible in events and logs). A result is only settled if the
# submitting node is the assigned node, the nonce matches, the lease has not
# expired, and the attempt has not already been settled.
#
# DEFERRED, and this is not equivalent to it: per-node keypairs with signed
# receipts, revocation and rotation. That is the right long-term answer and is
# tracked in ROADMAP §5. What is here stops an admitted node stealing another
# node's credit; it does not stop a node that holds the shared secret from
# joining under a name of its choosing in the first place.
ATTEMPT_LEASE_SECONDS = 900

# attempt_id -> the outcome we already recorded, so a retry is idempotent
# rather than a second payment.
settled_attempts: dict[str, dict] = {}
_MAX_SETTLED = 5000


def remember_settlement(
    attempt_id: str,
    outcome: dict,
    *,
    node_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """Record an attempt as settled, bounding the memory this can consume."""
    settled_attempts[attempt_id] = {
        "response": outcome,
        "node_id": node_id,
        "task_id": task_id,
    }
    if len(settled_attempts) > _MAX_SETTLED:
        for stale in list(settled_attempts)[: len(settled_attempts) - _MAX_SETTLED]:
            settled_attempts.pop(stale, None)


# Process start, for the public /status.json uptime figure. A stranger deciding
# whether this network is real cares that it has been up for days, not seconds.
STARTED_AT = time.time()
task_queue: list[dict] = []          # pending tasks for workers
task_results: dict[str, dict] = {}   # task_id -> result
task_inflight: dict[str, dict] = {}  # task_id -> task (assigned but not yet returned)
_task_queue_lock = threading.RLock()


def enqueue_task(task: dict) -> bool:
    """Atomically enforce the pending-task cap for every generated unit."""
    with _task_queue_lock:
        if len(task_queue) >= _MAX_TASK_QUEUE:
            return False
        task_queue.append(task)
        return True


def remove_queued_task(task_id: str) -> bool:
    """Remove a queued task by id without a check-then-mutate race."""
    with _task_queue_lock:
        for index, task in enumerate(task_queue):
            if task.get("task_id") == task_id:
                task_queue.pop(index)
                return True
    return False

# ── Circuit breaker state ─────────────────────────────────────────────
node_failure_count: dict[str, int] = {}   # node_id -> consecutive failure count
node_blacklist: dict[str, float] = {}     # node_id -> blacklist_until timestamp

# ── Verification & reputation ─────────────────────────────────────────
# One pool for the process. verify_rate is read from config at first use rather
# than captured at import, so a config edit takes effect on the next pitch
# instead of needing a restart.
verification_pool = VerificationPool(verify_rate=0.0)

# node_id -> timestamp it started waiting in GET /tasks/next. Used to give
# better-rated nodes first refusal on a task without ever starving a worse one.
waiting_nodes: dict[str, float] = {}

# A node counts as "currently waiting" only if it polled within this window.
_WAITING_FRESH = 3.0
# How long a lower-rated node defers before taking the work anyway.
_ROUTING_DEFER = 1.5


def touch_node(node_id: str) -> None:
    """Mark a node alive, re-admitting it if the janitor already evicted it.

    Closing a laptop lid is the most likely thing that happens to a volunteer
    node, and it used to strand one permanently:

      1. the lid closes, nothing reaches the server for `_NODE_TIMEOUT`, and the
         janitor correctly evicts the node and reclaims its work;
      2. the lid opens and the long poll simply *resumes* — the node never sees
         a connection error, so it never re-registers;
      3. the endpoint only refreshed `last_seen` `if node_id in nodes`, so the
         node stayed absent from the registry forever.

    An absent node is not merely cosmetic: both pitch paths only hand work to
    nodes when the registry is non-empty, so the node polls indefinitely and
    receives nothing while the dashboard shows an empty swarm. On camera that is
    a demo that looks broken while it is in fact working.

    Re-admitting server-side rather than asking the node to notice means this is
    fixed for nodes already deployed, which cannot be updated remotely. The
    placeholder record is replaced with full hardware details the next time the
    node registers normally.
    """
    node = nodes.get(node_id)
    if node is not None:
        node["last_seen"] = time.time()
        return
    now = time.time()
    nodes[node_id] = {
        "node_id": node_id,
        "model": "unknown",          # replaced on the next real registration
        "platform": "unknown",
        "machine": "unknown",
        "hostname": node_id,
        "cpu_count": None,
        "ram_gb": None,
        "gpu": None,
        "capabilities": [],
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "last_seen": now,
        "tasks_completed": 0,
        "credits_earned": 0,
        "current_task": None,
        "readmitted": True,
    }
    _emit("node_readmitted", {"node_id": node_id})


def _refresh_verify_rate() -> float:
    """Sync the pool's sample rate with config. Returns the active rate."""
    try:
        rate = float(get_config().get("verify_rate", 0.0) or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    verification_pool.verify_rate = max(0.0, min(1.0, rate))
    return verification_pool.verify_rate

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

# Strong references to in-flight broadcasts. Without these, asyncio only holds
# a weak reference and a broadcast can be garbage-collected before it is
# delivered ("Task was destroyed but it is pending!").
_broadcast_tasks: set = set()


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

    # Emitting from a sync context (a script, a test, the CLI) must record the
    # event rather than blow up: with no loop running there is no WebSocket
    # client to broadcast to anyway. get_event_loop() used to be used here,
    # which raises on Python 3.12+ outside a coroutine.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(ws_manager.broadcast(event))
    _broadcast_tasks.add(task)
    task.add_done_callback(_broadcast_tasks.discard)


# ── Rate limiting ─────────────────────────────────────────────────────
def _rate_limits() -> tuple[int, int]:
    """(max pitches, window seconds) per IP, from config.

    The module constants stay as the fallback so a malformed config can never
    disable the limiter — it just reverts to the safe default.
    """
    cfg = get_config()
    try:
        return int(cfg.get("pitch_rate_max", _RATE_MAX)), int(
            cfg.get("pitch_rate_window", _RATE_WINDOW)
        )
    except (TypeError, ValueError):
        return _RATE_MAX, _RATE_WINDOW


def _check_rate_limit(request: Request) -> int:
    """Raise 429 if this IP has exceeded the configured pitch rate.

    Returns the number of remaining pitches allowed in the current window.
    """
    rate_max, rate_window = _rate_limits()
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window_start = now - rate_window
    timestamps = [t for t in _pitch_timestamps.get(ip, []) if t > window_start]
    if len(timestamps) >= rate_max:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: max {rate_max} pitches per {rate_window}s. Try again shortly.",
            headers={
                "X-RateLimit-Limit": str(rate_max),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(min(timestamps)) + rate_window),
            },
        )
    timestamps.append(now)
    _pitch_timestamps[ip] = timestamps
    return rate_max - len(timestamps)


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
    if not secrets.compare_digest(str(provided), str(key)):
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
    if not secrets.compare_digest(str(provided), str(secret)):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Node-Secret header")


# ── Request/response models ──────────────────────────────────────────
class PitchRequest(BaseModel):
    task: str
    project_id: str | None = None   # optional: continue an existing project
    strategy: StrategyNameV1 = "auto"
    strategy_options: StrategyOptionsV1 | None = None
    candidates: int | None = Field(default=None, ge=1, le=5)
    placement: PlacementV1 | None = None
    requirements: ExecutionRequirementsV1 = Field(default_factory=ExecutionRequirementsV1)
    output_contract: OutputContractV1 | None = None
    verification: VerificationPolicyV1 = Field(default_factory=VerificationPolicyV1)
    confidentiality: ConfidentialityV1 = "trusted_guild"

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
    capabilities: list[str] = Field(default_factory=list)


class TaskResult(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    output: str | None = Field(default=None, max_length=10_485_760)
    error: str | None = Field(default=None, max_length=500)
    elapsed_seconds: float = Field(default=0, ge=0, le=7200)
    # Issued with the task. Absent means an old node build: the result is still
    # recorded so work is never lost, but it cannot be settled for credit.
    attempt_id: str | None = Field(default=None, max_length=128)
    nonce: str | None = Field(default=None, max_length=128)
    contract_version: str | None = Field(default=None, max_length=16)
    execution_id: str | None = Field(default=None, max_length=64)
    execution_unit_id: str | None = Field(default=None, max_length=128)
    execution_unit_kind: str | None = Field(default=None, max_length=64)


class TokenBatch(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    tokens: str = Field(min_length=1, max_length=65_536)
    contract_version: str | None = Field(default=None, max_length=16)
    attempt_id: str | None = Field(default=None, max_length=128)
    nonce: str | None = Field(default=None, max_length=128)
    execution_id: str | None = Field(default=None, max_length=64)
    execution_unit_id: str | None = Field(default=None, max_length=128)


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
        _cleanup_pass()


def _cleanup_pass():
    """One sweep of the janitor. Split out from the loop so it can be tested.

    Never raises: this runs unattended behind a background task, and a failure
    here must not take the orchestrator down mid-demo.
    """
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
            with _task_queue_lock:
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
