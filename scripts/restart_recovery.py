"""Kill-and-restart recovery check against a REAL uvicorn server (SPRINT_PHASE2 §2).

No Ollama needed: pitches fail fast, but the job record, its persistence, the
node registry and the WebSocket stream are all exercised for real — including
across a hard kill.

Run from the repo root.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

PORT = 8077
BASE = f"http://127.0.0.1:{PORT}"
WORKDIR = Path(tempfile.mkdtemp(prefix="restart_recovery_"))

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def start_server() -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=WORKDIR,
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 45
    while time.time() < deadline:
        if proc.poll() is not None:
            print("SERVER DIED:\n", proc.stdout.read()[:3000])
            raise SystemExit(1)
        try:
            r = httpx.get(f"{BASE}/health", timeout=2)
            if r.status_code == 200:
                return proc
        except Exception:
            time.sleep(0.4)
    raise SystemExit("server never became healthy")


def stop_server(proc: subprocess.Popen, hard: bool) -> None:
    proc.send_signal(signal.SIGKILL if hard else signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


async def collect_ws_events(stop: asyncio.Event, seen: list) -> None:
    """Stay subscribed and record events; reconnect if the socket drops."""
    import websockets

    while not stop.is_set():
        try:
            async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws/events") as ws:
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        seen.append(json.loads(msg))
                    except asyncio.TimeoutError:
                        continue
        except Exception:
            await asyncio.sleep(0.4)  # server down / restarting — retry


def register_node(node_id: str) -> httpx.Response:
    return httpx.post(f"{BASE}/nodes/register", timeout=10, json={
        "node_id": node_id, "model": "qwen3.5:4b", "platform": "Linux",
        "machine": "x86_64", "hostname": node_id, "cpu_count": 4,
        "ram_gb": 8.0, "gpu": "", "capabilities": [],
    })


async def main() -> int:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    for stale in ("events.db",):
        (WORKDIR / stale).unlink(missing_ok=True)

    print("\n[1] start server")
    proc = start_server()
    check("server healthy without Ollama", httpx.get(f"{BASE}/health").json()["status"] == "degraded")

    stop = asyncio.Event()
    seen: list = []
    ws_task = asyncio.create_task(collect_ws_events(stop, seen))
    await asyncio.sleep(1.0)

    print("\n[2] create state")
    register_node("node-before")
    check("node registered", httpx.get(f"{BASE}/nodes").json()["count"] == 1)

    job_ids = []
    for i in range(3):
        r = httpx.post(f"{BASE}/pitch/async", json={"task": f"restart probe {i}"}, timeout=15)
        job_ids.append(r.json()["job_id"])
    check("3 async jobs accepted", len(job_ids) == 3)

    # Jobs fail (no Ollama) but must reach a terminal state rather than hanging.
    deadline = time.time() + 60
    statuses = {}
    while time.time() < deadline:
        statuses = {j: httpx.get(f"{BASE}/jobs/{j}", timeout=10).json()["status"] for j in job_ids}
        if all(s in ("complete", "failed") for s in statuses.values()):
            break
        await asyncio.sleep(1)
    check("jobs reached a terminal state", all(s in ("complete", "failed") for s in statuses.values()),
          str(statuses))

    failed_job = job_ids[0]
    before = httpx.get(f"{BASE}/jobs/{failed_job}", timeout=10).json()
    check("failure has a readable message, not a traceback",
          bool(before.get("error")) and "Traceback" not in str(before.get("error")),
          str(before.get("error"))[:90])

    # The httpx calls above are synchronous and block this event loop, so the
    # collector task needs a clear window to actually drain the socket.
    await asyncio.sleep(2.0)
    events_before = len(seen)
    check("WebSocket received live events", events_before > 0, f"{events_before} events")

    print("\n[3] HARD KILL (SIGKILL) and restart")
    stop_server(proc, hard=True)
    await asyncio.sleep(1.5)
    dead = False
    try:
        httpx.get(f"{BASE}/health", timeout=2)
    except Exception:
        dead = True
    check("server is actually down", dead)

    proc = start_server()
    check("server came back up", httpx.get(f"{BASE}/health").status_code == 200)

    print("\n[4] verify recovery")
    after = httpx.get(f"{BASE}/jobs/{failed_job}", timeout=10)
    check("job survived the restart", after.status_code == 200 and after.json()["job_id"] == failed_job,
          f"HTTP {after.status_code}")
    if after.status_code == 200:
        check("job status survived", after.json()["status"] == before["status"],
              f"{before['status']} -> {after.json()['status']}")

    hist = httpx.get(f"{BASE}/events?since=0", timeout=10)
    check("event history survived the restart",
          hist.status_code == 200 and len(hist.json().get("events", [])) > 0,
          f"{len(hist.json().get('events', []))} events")

    nodes_now = httpx.get(f"{BASE}/nodes", timeout=10).json()["count"]
    check("node registry cleared on restart (nodes must re-register)", nodes_now == 0,
          f"{nodes_now} nodes")

    register_node("node-after")
    check("node can re-register after restart", httpx.get(f"{BASE}/nodes").json()["count"] == 1)

    r = httpx.post(f"{BASE}/pitch/async", json={"task": "post-restart probe"}, timeout=15)
    check("server accepts new work after restart", r.status_code == 200)

    check("dashboard still renders", httpx.get(f"{BASE}/dashboard", timeout=10).status_code == 200)
    check("landing page still renders", httpx.get(f"{BASE}/", timeout=10).status_code == 200)

    await asyncio.sleep(2.0)
    check("WebSocket client reconnected and is receiving again", len(seen) > events_before,
          f"{events_before} -> {len(seen)}")

    stop.set()
    ws_task.cancel()
    stop_server(proc, hard=False)

    print("\n" + "=" * 60)
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    for name, _, detail in failed:
        print(f"  FAILED: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
