"""End-to-end MCP check: a real client, a real server, real inference.

This is the video's Shot 2 — "any AI app can hand work to the swarm" — and until
now nothing had ever exercised it end to end. `tests/test_mcp_server.py` covers
the tool logic, but against a **fake pipeline** and an **in-process ASGI client**,
so it proves neither that the stdio protocol works nor that a real deliverable
comes back. The audit item asking for "the MCP flow with real inference" had been
open since Aug 5.

What this does, which is exactly what Claude Desktop does:

  1. starts a REAL uvicorn orchestrator
  2. launches mcp_server.py as a REAL subprocess over stdio
  3. completes the MCP handshake and lists tools
  4. calls pitch_task and gets a job_id
  5. polls get_job_status until the swarm finishes (real Ollama, minutes)
  6. calls get_result and checks a real deliverable came back

    python scripts/mcp_e2e.py                  # small task, ~5-15 min
    python scripts/mcp_e2e.py --task "..."     # your own

Ollama must be running. Restart it first if it has been grinding for hours —
measured: a long Ollama session slows calls until they hit the timeout.
"""

import argparse
import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
PORT = 8079
BASE = f"http://127.0.0.1:{PORT}"

# Deliberately small. This checks the MCP plumbing, not model quality — the eval
# set measures that. A big task just makes the check slower and flakier.
DEFAULT_TASK = (
    "Write a single Python file with a function slugify(text) that lowercases "
    "text, replaces spaces with hyphens, and strips punctuation. Include three "
    "assert statements demonstrating it. Standard library only."
)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def start_orchestrator(workdir: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=workdir,
        env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.time() + 45
    while time.time() < deadline:
        if proc.poll() is not None:
            print("ORCHESTRATOR DIED:\n", proc.stdout.read()[:2000])
            raise SystemExit(1)
        try:
            if httpx.get(f"{BASE}/health", timeout=2).status_code == 200:
                return proc
        except Exception:
            time.sleep(0.4)
    raise SystemExit("orchestrator never became healthy")


def _text(result) -> str:
    """Flatten an MCP tool result to text, whatever shape the SDK returns."""
    content = getattr(result, "content", result)
    if isinstance(content, list):
        return "\n".join(getattr(c, "text", str(c)) for c in content)
    return getattr(content, "text", str(content))


async def run(task: str, timeout_min: int) -> int:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    workdir = Path(tempfile.mkdtemp(prefix="mcp_e2e_"))
    print(f"\n[1] start the orchestrator (real uvicorn, {BASE})")
    orch = start_orchestrator(workdir)

    try:
        health = httpx.get(f"{BASE}/health", timeout=10).json()
        check("orchestrator healthy", health.get("status") == "ok", str(health.get("status")))
        if health.get("ollama") != "connected":
            check("Ollama connected", False, "start it with: ollama serve")
            return 1
        check("Ollama connected", True, ", ".join(health.get("models", []))[:60])

        print("\n[2] launch mcp_server.py as a real subprocess over stdio")
        params = StdioServerParameters(
            command=sys.executable,
            args=["mcp_server.py"],
            cwd=str(REPO),
            env={**os.environ, "ORCHESTRATOR_URL": BASE, "PYTHONIOENCODING": "utf-8"},
        )

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                check("MCP handshake completed", True,
                      getattr(getattr(init, "serverInfo", None), "name", "") or "connected")

                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                check("all five tools advertised", len(names) == 5, ", ".join(names))

                print("\n[3] pitch a task through MCP")
                pitched = _text(await session.call_tool("pitch_task", {"task": task}))
                # Must be `job_<digits>`. Matching bare "job_" grabs the literal
                # label "job_id:" out of "Task accepted. job_id: job_123", and
                # then every poll requests /jobs/job_id and 404s forever.
                found = re.search(r"job_\d+", pitched)
                job_id = found.group(0) if found else ""
                if not check("pitch_task returned a job_id", bool(job_id), pitched[:160]):
                    return 1

                print(f"\n[4] poll get_job_status (real inference — up to {timeout_min} min)")
                deadline = time.time() + timeout_min * 60
                status, last = "", ""
                while time.time() < deadline:
                    last = _text(await session.call_tool("get_job_status", {"job_id": job_id}))
                    low = last.lower()
                    if "complete" in low:
                        status = "complete"
                        break
                    if "failed" in low or "error" in low:
                        status = "failed"
                        break
                    mins = (time.time() - (deadline - timeout_min * 60)) / 60
                    print(f"    {mins:4.0f} min — {last.strip()[:90]}", flush=True)
                    await asyncio.sleep(20)

                if not check("job reached a terminal state", status == "complete",
                             status or f"still running after {timeout_min} min: {last[:100]}"):
                    return 1

                print("\n[5] fetch the deliverable through MCP")
                result = _text(await session.call_tool("get_result", {"job_id": job_id}))
                # Detail must be true whether the check passed or failed — printing
                # "no function found" next to a PASS is how a green run gets
                # misread as a broken one.
                has_code = "def " in result or "```" in result
                on_topic = "slug" in result.lower()
                check("get_result returned content", len(result) > 200, f"{len(result)} chars")
                check("deliverable contains real code", has_code,
                      "found a function or code fence" if has_code else "no function or code fence")
                check("deliverable is on-topic", on_topic,
                      "mentions slugify" if on_topic else "never mentions the requested function")

                print("\n[6] the other tools still answer over the same session")
                projects = _text(await session.call_tool("list_projects", {}))
                check("list_projects responds", bool(projects.strip()), projects.strip()[:80])
    finally:
        orch.terminate()
        try:
            orch.wait(timeout=10)
        except subprocess.TimeoutExpired:
            orch.kill()

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 58)
    print(f"MCP end-to-end: {passed}/{len(results)} checks passed")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} — {detail}")
    if passed == len(results):
        print("Shot 2 is real: a task went out over MCP and a deliverable came back.")
    return 0 if passed == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="End-to-end MCP check with real inference")
    ap.add_argument("--task", default=DEFAULT_TASK)
    ap.add_argument("--timeout-min", type=int, default=40)
    args = ap.parse_args()
    return asyncio.run(run(args.task, args.timeout_min))


if __name__ == "__main__":
    raise SystemExit(main())
