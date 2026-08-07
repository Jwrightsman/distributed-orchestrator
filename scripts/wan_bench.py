"""
Measure what running the swarm over the internet actually costs (sprint §3).

The interesting question is not "how long does a pitch take" — that is dominated
by model inference and would be the same on one machine. It is: **how much does
the network add**, and does anything break over a real WAN link that works on a
LAN.

So this measures the transport separately from the thinking:

  1. HTTP round-trip to the orchestrator          (raw network latency)
  2. Node registration                            (join cost)
  3. Task dispatch: queued -> node holding it     (the long-poll hand-off)
  4. Result submission                            (upload cost, real payload size)
  5. Sustained polling failure rate               (does the link stay up)

Run against the live orchestrator from a machine somewhere else entirely:

    python scripts/wan_bench.py --server http://IP:8000 --secret NODE_SECRET

Add --pitch-key KEY to also time a full end-to-end distributed pitch, which
reports the network share of total wall clock. That part needs a model on the
orchestrator and takes minutes.

Writes JSON to scripts/wan_results/ so the numbers in the README are traceable
to a run rather than remembered.
"""

import argparse
import asyncio
import json
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

RESULTS_DIR = Path(__file__).parent / "wan_results"


def cpu_contention_factor() -> float:
    """How much slower a fixed CPU burst runs than on an idle machine.

    Client-side timing is only meaningful when the client is idle. Measured
    Aug 6: running this benchmark while a local eval pinned the CPU reported a
    1513 ms median HTTP round-trip against a true figure near 190 ms — an 8x
    overstatement that would have gone into the README as a real number.

    Returns roughly 1.0 idle; climbs under load. No dependency on psutil.
    """
    best = None
    for _ in range(5):
        t0 = time.perf_counter()
        x = 0
        for i in range(200_000):
            x += i * i
        elapsed = time.perf_counter() - t0
        best = elapsed if best is None else min(best, elapsed)
    # Calibrated so a modern idle core lands near 1.0
    return round(best / 0.012, 2)


def _stats(samples: list[float]) -> dict:
    """Latency summary. Medians and p95 matter here; means hide the stalls."""
    if not samples:
        return {}
    ordered = sorted(samples)
    return {
        "n": len(samples),
        "min_ms": round(ordered[0] * 1000, 1),
        "median_ms": round(statistics.median(ordered) * 1000, 1),
        "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1] * 1000, 1),
        "max_ms": round(ordered[-1] * 1000, 1),
    }


async def measure_http_rtt(client: httpx.AsyncClient, server: str, n: int) -> dict:
    """Raw request latency to an endpoint that does no real work."""
    samples, failures = [], 0
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            resp = await client.get(f"{server}/health")
            resp.raise_for_status()
            samples.append(time.perf_counter() - t0)
        except Exception:
            failures += 1
        await asyncio.sleep(0.2)
    return {**_stats(samples), "failures": failures}


async def measure_registration(client: httpx.AsyncClient, server: str, secret: str, n: int) -> dict:
    """Cost of a node joining — the first thing a stranger's machine does."""
    headers = {"X-Node-Secret": secret} if secret else {}
    samples, failures = [], 0
    for i in range(n):
        payload = {
            "node_id": f"wanbench-{i}",
            "model": "qwen3.5:4b",
            "platform": platform.system(),
            "machine": platform.machine(),
            "hostname": f"wanbench-{i}",
        }
        t0 = time.perf_counter()
        try:
            resp = await client.post(f"{server}/nodes/register", json=payload, headers=headers)
            resp.raise_for_status()
            samples.append(time.perf_counter() - t0)
        except Exception:
            failures += 1
    return {**_stats(samples), "failures": failures}


async def measure_result_submission(
    client: httpx.AsyncClient, server: str, secret: str, n: int, payload_kb: int
) -> dict:
    """Time uploading a finished builder result across the link.

    This is the hop that carries real bytes: builder outputs are kilobytes of
    code, and upload is the slow direction on most home connections. Task
    *dispatch* is not measured separately — it is the same request/response hop
    as `/health`, so the HTTP round-trip above already bounds it, and queueing a
    task synthetically would require a full pitch to run first.
    """
    headers = {"X-Node-Secret": secret} if secret else {}
    node_id = "wanbench-worker"
    await client.post(
        f"{server}/nodes/register",
        json={
            "node_id": node_id,
            "model": "qwen3.5:4b",
            "platform": platform.system(),
            "machine": platform.machine(),
            "hostname": node_id,
        },
        headers=headers,
    )

    body = "x" * (payload_kb * 1024)
    samples, failures = [], 0
    for i in range(n):
        # The server records results for unknown task ids without granting
        # credits, so this measures transport without polluting the ledger.
        task_id = f"wanbench_{int(time.time() * 1000)}_{i}"
        t0 = time.perf_counter()
        try:
            r = await client.post(
                f"{server}/tasks/{task_id}/result",
                json={"node_id": node_id, "output": body, "error": None, "elapsed_seconds": 1.0},
                headers=headers,
            )
            r.raise_for_status()
            samples.append(time.perf_counter() - t0)
        except Exception:
            failures += 1
        await asyncio.sleep(0.2)

    return {"payload_kb": payload_kb, **_stats(samples), "failures": failures}


async def measure_poll_stability(
    client: httpx.AsyncClient, server: str, secret: str, minutes: float
) -> dict:
    """Does the link survive being idle? Long-polls are where flaky NAT shows up."""
    headers = {"X-Node-Secret": secret} if secret else {}
    node_id = "wanbench-idle"
    deadline = time.time() + minutes * 60
    polls, empty, errors = 0, 0, 0
    while time.time() < deadline:
        try:
            resp = await client.get(
                f"{server}/tasks/next", params={"node_id": node_id}, headers=headers, timeout=40
            )
            polls += 1
            if resp.status_code == 204:
                empty += 1
        except Exception:
            errors += 1
    return {
        "duration_minutes": minutes,
        "polls": polls,
        "returned_empty": empty,
        "errors": errors,
        "error_rate_pct": round(100 * errors / max(polls + errors, 1), 2),
    }


async def measure_end_to_end(
    client: httpx.AsyncClient, server: str, pitch_key: str, task: str
) -> dict:
    """One real pitch, start to finish, so network overhead has a denominator."""
    headers = {"Content-Type": "application/json"}
    if pitch_key:
        headers["X-Pitch-Key"] = pitch_key
    t0 = time.perf_counter()
    resp = await client.post(f"{server}/pitch/async", json={"task": task}, headers=headers)
    resp.raise_for_status()
    job_id = resp.json()["job_id"]

    while True:
        await asyncio.sleep(15)
        j = await client.get(f"{server}/jobs/{job_id}")
        data = j.json()
        if data.get("status") in ("complete", "failed"):
            return {
                "job_id": job_id,
                "status": data.get("status"),
                "rating": data.get("rating"),
                "wall_clock_s": round(time.perf_counter() - t0, 1),
                "subtasks": len(data.get("plan") or []),
                "error": data.get("error"),
            }


async def main():
    ap = argparse.ArgumentParser(description="Measure WAN cost of the distributed swarm")
    ap.add_argument("--server", required=True)
    ap.add_argument("--secret", default="", help="node_secret")
    ap.add_argument("--pitch-key", default="", help="enables the end-to-end pitch")
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--payload-kb", type=int, default=8, help="simulated builder output size")
    ap.add_argument("--idle-minutes", type=float, default=2.0)
    ap.add_argument("--task", default="Write a Python function that reverses a string, with a docstring")
    ap.add_argument(
        "--allow-loaded-client",
        action="store_true",
        help="record numbers even if this machine is busy (they will be marked untrusted)",
    )
    args = ap.parse_args()

    server = args.server.rstrip("/")

    contention = cpu_contention_factor()
    trustworthy = contention < 2.0
    if not trustworthy:
        msg = (
            f"This machine is busy (CPU contention factor {contention}). Client-side "
            f"latency will be inflated and must not be published."
        )
        if not args.allow_loaded_client:
            print(f"REFUSING TO MEASURE: {msg}")
            print("Wait until nothing heavy is running, or pass --allow-loaded-client.")
            return
        print(f"WARNING: {msg}")

    results = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "server": server,
        "client_platform": f"{platform.system()} {platform.machine()}",
        "cpu_contention_factor": contention,
        "trustworthy": trustworthy,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        print("1/5 HTTP round-trip...")
        results["http_rtt"] = await measure_http_rtt(client, server, args.samples)
        print(f"    median {results['http_rtt'].get('median_ms')} ms")

        print("2/5 node registration...")
        results["registration"] = await measure_registration(client, server, args.secret, 5)
        print(f"    median {results['registration'].get('median_ms')} ms")

        print(f"3/5 result submission ({args.payload_kb} KB payload)...")
        results["result_submission"] = await measure_result_submission(
            client, server, args.secret, 8, args.payload_kb
        )
        print(f"    median {results['result_submission'].get('median_ms')} ms")

        print(f"4/5 idle poll stability ({args.idle_minutes} min)...")
        results["poll_stability"] = await measure_poll_stability(
            client, server, args.secret, args.idle_minutes
        )
        print(f"    error rate {results['poll_stability']['error_rate_pct']}%")

        if args.pitch_key:
            print("5/5 end-to-end pitch (minutes)...")
            results["end_to_end"] = await measure_end_to_end(client, server, args.pitch_key, args.task)
            e2e = results["end_to_end"]
            print(f"    {e2e['status']} in {e2e['wall_clock_s']}s, rating {e2e['rating']}")
        else:
            print("5/5 skipped (no --pitch-key)")

    # Network share of a real run — the honest headline
    if "end_to_end" in results and results["end_to_end"].get("wall_clock_s"):
        # Per subtask the network carries: one dispatch (bounded by HTTP RTT)
        # plus one result upload.
        per_task_ms = (
            results["http_rtt"].get("median_ms", 0)
            + results["result_submission"].get("median_ms", 0)
        )
        subtasks = max(results["end_to_end"].get("subtasks") or 1, 1)
        overhead_s = per_task_ms * subtasks / 1000
        total = results["end_to_end"]["wall_clock_s"]
        results["network_share"] = {
            "estimated_network_seconds": round(overhead_s, 2),
            "total_seconds": total,
            "network_pct_of_wall_clock": round(100 * overhead_s / total, 3),
        }

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = RESULTS_DIR / f"wan_{stamp}.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nSaved {out}")
    if "network_share" in results:
        ns = results["network_share"]
        print(
            f"Network accounted for {ns['estimated_network_seconds']}s of "
            f"{ns['total_seconds']}s ({ns['network_pct_of_wall_clock']}%)"
        )


if __name__ == "__main__":
    asyncio.run(main())
