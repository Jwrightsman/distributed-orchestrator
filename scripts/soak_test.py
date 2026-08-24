"""Soak test — 20 measured pitches in one server session (SPRINT_PHASE2 §2).

Watches for what a long launch day actually does to the process: memory growth,
SQLite bloat, event-buffer leaks, orphaned in-flight tasks, and latency drift.
The model is stubbed (scripts/_soak_app.py) because none of those are model
behavior — this is an infrastructure test, and stubbing keeps the numbers
readable instead of drowning them in generation time.

    python scripts/soak_test.py            # 1 warmup + 20 measured pitches
    python scripts/soak_test.py --pitches 50  # 1 warmup + 50 measured

Needs no Ollama. Exits non-zero if a leak threshold is crossed.
"""

import argparse
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = 8078
BASE = f"http://127.0.0.1:{PORT}"


def _port_is_free() -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", PORT))
            return True
        except OSError:
            return False


def rss_mb(pid: int) -> float:
    """Resident memory of the server process, or -1.0 if it cannot be measured.

    This read /proc only, which does not exist on Windows — so on the machine
    this project is developed on it silently returned -1.0 while the summary
    still announced "no leaks". The published "+0.9 MB RSS" was a Linux number
    presented as a universal one. psutil first, /proc as the fallback.
    """
    try:
        import psutil

        return psutil.Process(pid).memory_info().rss / 1024 / 1024
    except Exception:
        pass
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return -1.0


def start_server(workdir: Path) -> subprocess.Popen:
    if not _port_is_free():
        raise SystemExit(
            f"Port {PORT} is already in use; refusing an ambiguous soak run."
        )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "scripts._soak_app:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=workdir,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.time() + 45
    while time.time() < deadline:
        if proc.poll() is not None:
            print("SERVER DIED:\n", proc.stdout.read()[:3000])
            raise SystemExit(1)
        try:
            # Readiness must not depend on Ollama. The public ``/health``
            # endpoint intentionally spends up to five seconds probing it, so
            # a shorter client timeout can abandon every otherwise-healthy
            # response and leak the subprocess on startup failure.
            if httpx.get(f"{BASE}/v1/operator/health", timeout=5).status_code == 200:
                return proc
        except Exception:
            time.sleep(0.4)
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    output = proc.stdout.read()[:3000] if proc.stdout is not None else ""
    print("SERVER STARTUP OUTPUT:\n", output)
    raise SystemExit("server never became healthy")


def run_pitch(i: int, timeout: int = 120) -> tuple[bool, float, str]:
    start = time.time()
    r = httpx.post(f"{BASE}/pitch/async", json={"task": f"soak pitch {i}"}, timeout=20)
    if r.status_code != 200:
        return False, time.time() - start, f"HTTP {r.status_code}"
    job_id = r.json()["job_id"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = httpx.get(f"{BASE}/jobs/{job_id}", timeout=10).json()
        if job["status"] == "complete":
            return True, time.time() - start, ""
        if job["status"] == "failed":
            return False, time.time() - start, str(job.get("error"))[:120]
        time.sleep(0.25)
    return False, time.time() - start, "timed out waiting for job"


def settle_collectable_cycles() -> dict:
    """Run full GC inside the isolated server before comparing its RSS.

    Completed asyncio/ASGI request graphs can remain unreachable until Python's
    cyclic collector happens to run.  Sampling one arbitrary side of that
    collection makes a sawtooth look like linear retained growth.  A real leak
    remains reachable and therefore still contributes to the post-GC RSS.
    """

    response = httpx.post(f"{BASE}/_soak/collect", timeout=20)
    response.raise_for_status()
    return response.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pitches", type=int, default=20)
    args = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="soak_"))
    # Back-to-back pitching is the point of a soak, so lift the per-IP rate
    # limit for this run only — config.json is read from the working directory,
    # so the production default is untouched.
    (workdir / "config.json").write_text(
        json.dumps({"pitch_rate_max": args.pitches * 5, "pitch_rate_window": 60})
    )
    print(f"workdir: {workdir}")
    proc = start_server(workdir)
    pid = proc.pid

    db = workdir / "events.db"
    samples = []
    failures = []

    # Establish one complete request's import/validator/allocator high-water
    # mark before measuring steady-state retention.  A genuine per-pitch leak
    # still grows across every measured pitch; one-time lazy initialization
    # does not get mislabelled as a leak merely because the default run is
    # short.
    warmup_ok, warmup_secs, warmup_detail = run_pitch(0)
    print(
        f"warmup: {'ok' if warmup_ok else 'FAILED'} in {warmup_secs:.2f}s"
        + (f" ({warmup_detail})" if warmup_detail else "")
    )
    if not warmup_ok:
        failures.append((0, f"warmup: {warmup_detail}"))
    settle_collectable_cycles()
    baseline_rss = rss_mb(pid)
    print(f"\nbaseline RSS: {baseline_rss:.1f} MB\n")
    print(f"{'#':>3}  {'ok':>3}  {'secs':>6}  {'RSS MB':>7}  {'db KB':>7}  "
          f"{'events':>6}  {'inflight':>8}  {'queue':>5}")

    for i in range(1, args.pitches + 1):
        ok, secs, detail = run_pitch(i)
        if not ok:
            failures.append((i, detail))

        health = httpx.get(f"{BASE}/health", timeout=10).json()
        events = httpx.get(f"{BASE}/events?since=0", timeout=10).json().get("events", [])
        nodes = httpx.get(f"{BASE}/nodes", timeout=10).json()

        sample = {
            "i": i, "ok": ok, "secs": secs,
            "rss": rss_mb(pid),
            "db_kb": db.stat().st_size / 1024 if db.exists() else 0,
            "events": len(events),
            "inflight": health.get("tasks_pending", 0),
            "nodes": nodes["count"],
        }
        samples.append(sample)
        print(f"{i:>3}  {'y' if ok else 'N':>3}  {secs:>6.2f}  {sample['rss']:>7.1f}  "
              f"{sample['db_kb']:>7.1f}  {sample['events']:>6}  "
              f"{sample['inflight']:>8}  {len(events):>5}")

    final_state = settle_collectable_cycles()
    final_rss = rss_mb(pid)
    growth = final_rss - baseline_rss
    first_half = [s["secs"] for s in samples[: len(samples) // 2] if s["ok"]]
    second_half = [s["secs"] for s in samples[len(samples) // 2:] if s["ok"]]

    print("\n" + "=" * 64)
    print(f"pitches:        {args.pitches}  ({len(failures)} failed)")
    rss_measured = baseline_rss >= 0 and final_rss >= 0
    if rss_measured:
        print(f"RSS (post-GC):  {baseline_rss:.1f} -> {final_rss:.1f} MB  (growth {growth:+.1f} MB)")
        print(
            f"GC settled:     {final_state['collected']} unreachable object(s) "
            "before final sample"
        )
    else:
        print("RSS:            NOT MEASURED on this platform — pip install psutil")
    print(f"events.db:      {samples[-1]['db_kb']:.1f} KB")
    print(f"event log rows: {samples[-1]['events']} (server caps the API view at 100)")
    print(f"tasks pending:  {samples[-1]['inflight']} (must be 0 — orphaned tasks otherwise)")
    if first_half and second_half:
        m1, m2 = statistics.mean(first_half), statistics.mean(second_half)
        print(f"latency:        {m1:.2f}s first half -> {m2:.2f}s second half "
              f"({(m2 / m1 - 1) * 100:+.0f}%)")

    outputs = list((workdir / "output").glob("*")) if (workdir / "output").exists() else []
    expected_outputs = args.pitches + 1 - len(failures)
    print(f"output dirs:    {len(outputs)} (expect {expected_outputs}, including warmup)")
    print(
        "execution maps: "
        f"live={final_state['service_live_results']}, "
        f"requests={final_state['service_requests']}, "
        f"controls={final_state['service_controls']}, "
        f"background={final_state['service_background']} "
        f"(legacy jobs retained={final_state['jobs']})"
    )

    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()

    problems = []
    if failures:
        problems.append(f"{len(failures)} pitch(es) failed: {failures[:3]}")
    # Rate, not total. A flat 100 MB ceiling passes a 60-pitch run and fails the
    # same leak at 120 — which is exactly what happened: 60 pitches grew 78 MB
    # (under the bar) and 120 grew 151 MB (over it), from an identical
    # 1.25 MB/pitch linear leak. A per-pitch rate makes the verdict independent
    # of how long you happened to run it.
    per_pitch = growth / max(1, args.pitches)
    if rss_measured and per_pitch > 0.5:
        problems.append(
            f"RSS grew {growth:.1f} MB over {args.pitches} pitches "
            f"({per_pitch:.2f} MB/pitch — linear growth means a leak)"
        )
    if samples[-1]["inflight"] != 0:
        problems.append(f"{samples[-1]['inflight']} orphaned in-flight task(s)")
    retained_execution_state = {
        name: final_state[name]
        for name in (
            "service_live_results",
            "service_requests",
            "service_controls",
            "service_background",
        )
        if final_state[name] != 0
    }
    if retained_execution_state:
        problems.append(
            f"terminal execution state retained in process maps: {retained_execution_state}"
        )
    if first_half and second_half and statistics.mean(second_half) > 3 * statistics.mean(first_half):
        problems.append("latency more than tripled across the run")

    print()
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        print(f"\nworkdir kept for inspection: {workdir}")
        return 1

    if rss_measured:
        print("SOAK CLEAN — no leaks, no orphans, no latency drift")
    else:
        # Never claim a check that did not run. This is exactly how "+0.9 MB
        # RSS" became a repo-wide claim from a Linux-only measurement.
        print("SOAK CLEAN — no orphans, no latency drift")
        print("  (memory growth NOT checked: install psutil to measure it)")
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
