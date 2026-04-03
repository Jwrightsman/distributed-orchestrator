"""
Worker node — runs on any machine that wants to contribute compute.

Connects to the orchestrator server, picks up tasks, runs them locally
via Ollama, and sends results back. This is what turns a laptop into
a node in the network.

Usage:
    python node.py --server http://ORCHESTRATOR_IP:8000
    python node.py --server http://192.168.1.50:8000
"""

import argparse
import asyncio
import platform
import time

import httpx

from ollama_client import generate, check_ollama, DEFAULT_MODEL


async def register(server: str, node_id: str) -> dict:
    """Register this node with the orchestrator."""
    info = {
        "node_id": node_id,
        "model": DEFAULT_MODEL,
        "platform": platform.system(),
        "machine": platform.machine(),
        "hostname": platform.node(),
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{server}/nodes/register", json=info)
        resp.raise_for_status()
        return resp.json()


async def poll_and_execute(server: str, node_id: str):
    """Poll the orchestrator for tasks, execute them, return results."""
    async with httpx.AsyncClient(timeout=600) as client:
        # Ask for work
        resp = await client.get(f"{server}/tasks/next", params={"node_id": node_id})
        if resp.status_code == 204:
            return None  # No work available

        resp.raise_for_status()
        task = resp.json()

        task_id = task["task_id"]
        prompt = task["prompt"]
        system = task.get("system", "")

        print(f"  Running task {task_id}: {task.get('title', 'unnamed')}")
        start = time.time()

        try:
            result = await generate(prompt, system=system)
            elapsed = time.time() - start
            print(f"  Completed in {elapsed:.0f}s")

            # Send result back
            await client.post(
                f"{server}/tasks/{task_id}/result",
                json={
                    "node_id": node_id,
                    "output": result,
                    "elapsed_seconds": elapsed,
                },
            )
            return task_id
        except Exception as e:
            # Report failure
            await client.post(
                f"{server}/tasks/{task_id}/result",
                json={
                    "node_id": node_id,
                    "output": None,
                    "error": str(e),
                    "elapsed_seconds": time.time() - start,
                },
            )
            print(f"  Task failed: {e}")
            return None


async def main():
    parser = argparse.ArgumentParser(description="Join the network as a worker node")
    parser.add_argument("--server", required=True, help="Orchestrator URL (e.g. http://192.168.1.50:8000)")
    parser.add_argument("--node-id", default=None, help="Custom node ID (defaults to hostname)")
    args = parser.parse_args()

    node_id = args.node_id or platform.node()
    server = args.server.rstrip("/")

    # Pre-flight: check local Ollama
    status = await check_ollama()
    if not status["ok"]:
        print(f"ERROR: {status['error']}")
        return

    print(f"Node: {node_id}")
    print(f"Model: {DEFAULT_MODEL}")
    print(f"Connecting to: {server}")
    print()

    # Register with orchestrator
    try:
        reg = await register(server, node_id)
        print(f"Registered with orchestrator. {reg.get('message', '')}")
    except Exception as e:
        print(f"Failed to register: {e}")
        print("Is the orchestrator running? Start it with: python -m uvicorn server:app --host 0.0.0.0 --port 8000")
        return

    # Main loop: poll for work
    print("Waiting for tasks...\n")
    while True:
        try:
            task_id = await poll_and_execute(server, node_id)
            if task_id is None:
                await asyncio.sleep(3)  # No work, wait and retry
        except httpx.ConnectError:
            print("Lost connection to orchestrator. Retrying in 10s...")
            await asyncio.sleep(10)
        except KeyboardInterrupt:
            print("\nShutting down node.")
            break
        except Exception as e:
            print(f"Error: {e}. Retrying in 5s...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
