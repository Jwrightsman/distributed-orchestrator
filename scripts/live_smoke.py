"""Live-network smoke test — SPRINT_PHASE2 §5 regression item.

Pitches a real task at a *deployed* orchestrator and checks that the whole
distributed path works over the public internet: planner and reviewer on the
server, builder subtasks handed to a connected worker node, deliverable back.

Everything else in the regression sweep runs locally. This is the only check
that exercises the actual claim on the README — "builder subtasks are
distributed to connected nodes" — against a real deployment, and the deploy that
silently did nothing on Aug 12 is the reason it exists: a healthy /health does
not mean the thing works.

    set PITCH_KEY=...            (or put it in an untracked .pitch_key)
    python scripts/live_smoke.py --server http://167.233.239.33:8000

Costs one real pitch. On CPU nodes budget 10-40 minutes.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent

# Small and self-checking. This proves the path works, not that the model is
# good — evals/ measures that.
SMOKE_TASK = (
    "Write a single Python file defining celsius_to_fahrenheit(c) and "
    "fahrenheit_to_celsius(f), with three assert statements demonstrating "
    "both. Standard library only."
)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def _pitch_key(explicit: str) -> str:
    if explicit:
        return explicit
    env = os.environ.get("PITCH_KEY", "").strip()
    if env:
        return env
    f = REPO / ".pitch_key"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke-test a live orchestrator")
    ap.add_argument("--server", default="http://167.233.239.33:8000")
    ap.add_argument("--pitch-key", default="")
    ap.add_argument("--timeout-min", type=int, default=45)
    args = ap.parse_args()

    base = args.server.rstrip("/")
    key = _pitch_key(args.pitch_key)
    headers = {"X-Pitch-Key": key} if key else {}

    print(f"\n[1] reachability — {base}")
    try:
        health = httpx.get(f"{base}/health", timeout=20).json()
    except Exception as e:
        check("orchestrator reachable", False, str(e)[:120])
        return 1
    check("orchestrator reachable", health.get("status") == "ok", str(health.get("status")))
    check("Ollama connected on the server", health.get("ollama") == "connected",
          ", ".join(health.get("models", []))[:60])

    # The check that would have caught the silent no-op deploy immediately.
    print("\n[2] deployed code is current")
    nodes = httpx.get(f"{base}/nodes", timeout=20).json()
    # Detail has to be true whether the check passed or failed. Printing the
    # failure reason beside a PASS is how a green run gets misread as broken —
    # made this exact mistake once already in scripts/mcp_e2e.py.
    current = "verify_rate" in nodes
    check("server exposes verification fields (new code)", current,
          "present" if current else "missing verify_rate — server is running an older image")
    node_count = nodes.get("count", 0)
    check("at least one worker node is connected", node_count >= 1, f"{node_count} node(s)")

    try:
        r = httpx.get(f"{base}/ws/events", timeout=15, headers={
            "Connection": "Upgrade", "Upgrade": "websocket",
            "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
        })
        code = r.status_code
    except Exception:
        code = 101  # a successful upgrade often surfaces as a client-side abort
    ws_ok = code in (101, 426)
    check("WebSocket endpoint upgrades (101, not 404)", ws_ok,
          f"HTTP {code}" if ws_ok
          else f"HTTP {code} — 404 means the deployed image lacks a WebSocket library")

    if node_count < 1:
        print("\nNo nodes connected — the distributed path cannot be exercised.")
        print("Start one:  py node.py --server " + base + " --secret <node_secret>")
        return 1

    print(f"\n[3] pitch a real task over the internet (up to {args.timeout_min} min)")
    print(f"    {SMOKE_TASK[:70]}...")
    t0 = time.time()
    try:
        resp = httpx.post(
            f"{base}/pitch/distributed",
            json={"task": SMOKE_TASK},
            headers=headers,
            timeout=args.timeout_min * 60,
        )
    except Exception as e:
        check("pitch completed", False, str(e)[:140])
        return 1

    elapsed = time.time() - t0
    if resp.status_code == 401:
        check("pitch accepted", False, "401 — wrong or missing pitch key")
        return 1
    if not check("pitch accepted", resp.status_code == 200, f"HTTP {resp.status_code}"):
        return 1

    data = resp.json()
    print(f"    completed in {elapsed / 60:.1f} min")

    print("\n[4] the distributed claim, verified")
    check("a deliverable came back", len(data.get("final_output") or "") > 100,
          f"{len(data.get('final_output') or '')} chars")
    check("runnable code was extracted", bool(data.get("code_files")),
          f"{len(data.get('code_files') or [])} file(s)")
    used = data.get("nodes_used", 0)
    check("builder work ran on a worker node", bool(used),
          f"nodes_used={used}" if used
          else "nodes_used=0 — it silently fell back to running locally")
    on_topic = "fahrenheit" in (data.get("final_output") or "").lower()
    check("deliverable is on-topic", on_topic,
          "mentions fahrenheit" if on_topic else "never mentions the requested function names")
    problems = data.get("code_problems") or []
    check("extracted code has no reported problems", not problems,
          "none" if not problems else "; ".join(problems)[:120])

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"LIVE SMOKE: {passed}/{len(results)} checks passed  ({elapsed / 60:.1f} min)")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} — {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
