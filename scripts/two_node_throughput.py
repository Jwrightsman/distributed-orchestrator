"""Does a second machine finish more checked work per hour? One batch, measured.

Submits the same batch of independent tasks to a running coordinator, all at
once, as `strategy=direct` with `placement=distributed` and local fallback
OFF. Only connected workers generate; the coordinator only dispatches. Waits for
every result, downloads each deliverable, and grades it with `evals/grading.py`.
The grader RUNS each generated script (scratch folder, scrubbed environment,
timeout; not a sandbox), hence `--i-approve-running-generated-code`.

Run it twice with the same tasks, once with each worker set, then compare:

    py scripts/two_node_throughput.py --server https://COORDINATOR --expect-nodes 2 \
        --label two-machines --i-approve-running-generated-code --output C:\\mycelium-2node\\two
    py scripts/two_node_throughput.py --server https://COORDINATOR --expect-nodes 1 \
        --label one-machine --i-approve-running-generated-code --output C:\\mycelium-2node\\one
    py scripts/two_node_throughput.py --compare C:\\mycelium-2node\\one C:\\mycelium-2node\\two

The pitch and viewer keys are read from MYCELIUM_PITCH_KEY / MYCELIUM_VIEWER_KEY
or asked for without echo. They are never written to disk or printed.

Prompts go to every connected worker's owner, so every connected worker must
belong to someone who agreed to take part. The tasks are the public
development corpus items, not private data.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import corpus, grading  # noqa: E402

DEFAULT_ITEMS = (
    "extract-log-errors", "extract-jsonl-fields", "transform-csv-to-jsonl",
    "transform-dedupe-orders", "classify-orders-by-size", "classify-sales-performance",
)
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def _key(env, prompt):
    value = os.environ.get(env, "").strip()
    return value or getpass.getpass(prompt).strip()


def request_for(item):
    return {
        "protocol_version": "1",
        "task": item.task,
        "strategy": "direct",
        "strategy_options": {"kind": "ensemble", "candidates": 1, "concurrency": 1},
        "placement": "distributed",
        "remote_dispatch_consent": True,
        "confidentiality": "trusted_guild",
        "requirements": {"allow_local_fallback": False},
        "output_contract": {"kind": "code", "format": item.artifact},
        "timeout_seconds": 7200,
    }


async def fetch_deliverables(client, execution_id, target):
    target.mkdir(parents=True, exist_ok=True)
    response = await client.get(f"/v1/executions/{execution_id}/artifacts")
    if response.status_code != 200:
        return []
    files = []
    for entry in response.json().get("entries", []):
        if entry.get("role", "deliverable") != "deliverable":
            continue
        rel = entry["relative_path"]
        name = Path(rel).name
        if not name or name.startswith("."):
            continue
        body = await client.get(f"/v1/executions/{execution_id}/artifacts/{quote(rel)}")
        if body.status_code == 200:
            path = target / name
            path.write_bytes(body.content)
            files.append(str(path))
    return files


async def run(args):
    out = args.output.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty. Use a new folder for each run.")
    out.mkdir(parents=True, exist_ok=True)
    items = {i.id: i for i in corpus.load_corpus()}
    batch = [items[name] for name in args.items]

    headers = {"X-Pitch-Key": _key("MYCELIUM_PITCH_KEY", "Pitch key (hidden): "),
               "X-Viewer-Key": _key("MYCELIUM_VIEWER_KEY", "Viewer key (hidden): ")}
    async with httpx.AsyncClient(base_url=args.server.rstrip("/"), headers=headers,
                                 timeout=60) as client:
        nodes = (await client.get("/nodes")).json()
        connected = [n.get("node_id") for n in nodes.get("nodes", [])]
        print(f"Connected workers ({len(connected)}): {', '.join(connected) or 'none'}")
        if len(connected) != args.expect_nodes:
            raise SystemExit(f"Expected {args.expect_nodes} connected worker(s). Connect or stop "
                             "workers until the count matches, then rerun.")

        started = time.monotonic()
        submitted = {}
        for item in batch:
            response = await client.post("/v1/executions", json=request_for(item))
            if response.status_code != 202:
                raise SystemExit(f"Submit refused for {item.id}: {response.status_code} "
                                 f"{response.text[:300]}")
            submitted[response.json()["execution_id"]] = item
        print(f"Submitted {len(batch)} tasks at once. Waiting (this is model time, "
              "minutes per task)...")

        done = {}
        while len(done) < len(submitted):
            await asyncio.sleep(10)
            for execution_id, item in submitted.items():
                if execution_id in done:
                    continue
                response = await client.get(f"/v1/executions/{execution_id}")
                if response.status_code != 200:
                    continue
                result = response.json()
                status = result.get("lifecycle_status") or result.get("status")
                if status in TERMINAL:
                    minutes = (time.monotonic() - started) / 60
                    done[execution_id] = (result, minutes)
                    nodes_used = ", ".join(result.get("participating_nodes") or []) or "none"
                    print(f"  {item.id:28s} {status:10s} at {minutes:5.1f} min on {nodes_used}")
        wall_minutes = (time.monotonic() - started) / 60

        rows = []
        for execution_id, (result, minutes) in done.items():
            item = submitted[execution_id]
            files = await fetch_deliverables(client, execution_id, out / "graded" / item.id)
            grade = await asyncio.to_thread(grading.grade, item, files) if files else None
            rows.append({
                "item_id": item.id, "execution_id": execution_id,
                "lifecycle_status": result.get("lifecycle_status"),
                "participating_nodes": result.get("participating_nodes") or [],
                "finished_minutes": round(minutes, 2),
                "accepted": bool(grade and grade.passed),
                "failed_checks": grade.failed_checks if grade else ["no deliverable"],
            })

    accepted = sum(r["accepted"] for r in rows)
    summary = {"label": args.label, "workers": connected, "tasks": len(rows),
               "accepted": accepted, "wall_minutes": round(wall_minutes, 2),
               "accepted_per_hour": round(accepted / (wall_minutes / 60), 2) if wall_minutes else None,
               "rows": rows}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\n{args.label}: {accepted}/{len(rows)} accepted in {wall_minutes:.1f} min "
          f"on {len(connected)} worker(s) = {summary['accepted_per_hour']} accepted per hour")
    print(f"Saved in {out}")


def compare(one, two):
    a = json.loads((Path(one) / "summary.json").read_text(encoding="utf-8"))
    b = json.loads((Path(two) / "summary.json").read_text(encoding="utf-8"))
    for s in (a, b):
        print(f"{s['label']:14s} workers {len(s['workers'])}  accepted {s['accepted']}/{s['tasks']}  "
              f"{s['wall_minutes']:6.1f} min  {s['accepted_per_hour']} accepted/hour")
    if a["wall_minutes"] and b["wall_minutes"]:
        print(f"Speed-up in finishing the batch: {a['wall_minutes'] / b['wall_minutes']:.2f}x")
    print("One batch of six on two specific machines: a direction, not a general result.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server")
    parser.add_argument("--expect-nodes", type=int)
    parser.add_argument("--label")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--items", nargs="+", default=list(DEFAULT_ITEMS))
    parser.add_argument("--i-approve-running-generated-code", action="store_true")
    parser.add_argument("--compare", nargs=2, metavar=("ONE_MACHINE_DIR", "TWO_MACHINE_DIR"))
    args = parser.parse_args()
    if args.compare:
        compare(*args.compare)
        return
    missing = [flag for flag, value in (("--server", args.server), ("--expect-nodes", args.expect_nodes),
                                        ("--label", args.label), ("--output", args.output)) if not value]
    if missing:
        raise SystemExit("Missing: " + ", ".join(missing))
    if not args.i_approve_running_generated_code:
        raise SystemExit("Grading runs each generated Python script (scratch folder, scrubbed "
                         "environment, timeout; not a sandbox). Add "
                         "--i-approve-running-generated-code to accept that.")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
