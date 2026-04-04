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
import os
import platform
import time

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from ollama_client import generate, check_ollama, DEFAULT_MODEL

console = Console()


def _hardware_info() -> dict:
    """Collect basic hardware info to send on registration."""
    info: dict = {
        "cpu_count": os.cpu_count(),
        "ram_gb": None,
        "gpu": None,
    }
    try:
        import psutil  # type: ignore
        info["ram_gb"] = round(psutil.virtual_memory().total / 1024 ** 3, 1)
    except ImportError:
        pass
    # Best-effort GPU detection — non-critical
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            info["gpu"] = r.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return info


async def register(server: str, node_id: str) -> dict:
    """Register this node with the orchestrator."""
    hw = _hardware_info()
    info = {
        "node_id": node_id,
        "model": DEFAULT_MODEL,
        "platform": platform.system(),
        "machine": platform.machine(),
        "hostname": platform.node(),
        **hw,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{server}/nodes/register", json=info)
        resp.raise_for_status()
        return resp.json()


async def poll_and_execute(server: str, node_id: str, session: dict) -> str | None:
    """Poll the orchestrator for tasks, execute them, return task_id or None."""
    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.get(f"{server}/tasks/next", params={"node_id": node_id})
        if resp.status_code == 204:
            return None  # No work available

        resp.raise_for_status()
        task = resp.json()

        task_id = task["task_id"]
        title = task.get("title", "unnamed")
        prompt = task["prompt"]
        system = task.get("system", "")

        console.print(Panel(
            f"[dim]{task_id}[/dim]",
            title=f"[bold yellow]TASK[/bold yellow]  {title}",
            border_style="yellow",
        ))

        start = time.time()
        try:
            result = await generate(prompt, system=system)
            elapsed = time.time() - start

            submit_resp = await client.post(
                f"{server}/tasks/{task_id}/result",
                json={
                    "node_id": node_id,
                    "output": result,
                    "elapsed_seconds": elapsed,
                },
            )
            credits = 0
            if submit_resp.status_code == 200:
                credits = submit_resp.json().get("credits_earned", 0)

            session["tasks"] += 1
            session["credits"] += credits

            console.print(
                f"[bold green]DONE[/bold green]  {title} "
                f"[dim]({elapsed:.0f}s)[/dim]  "
                f"[bold yellow]+{credits} credits[/bold yellow]  "
                f"[dim]session total: {session['credits']} credits[/dim]"
            )
            console.print()
            return task_id

        except Exception as e:
            await client.post(
                f"{server}/tasks/{task_id}/result",
                json={
                    "node_id": node_id,
                    "output": None,
                    "error": str(e),
                    "elapsed_seconds": time.time() - start,
                },
            )
            console.print(f"[red bold]FAILED[/red bold]  {title}: {e}\n")
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
        console.print(f"[red bold]ERROR:[/red bold] {status['error']}")
        return

    console.print(Panel(
        f"[bold]Node ID:[/bold]   {node_id}\n"
        f"[bold]Model:[/bold]     {DEFAULT_MODEL}\n"
        f"[bold]Orchestrator:[/bold] {server}",
        title="[bold cyan]Distributed AI Node[/bold cyan]",
        border_style="cyan",
    ))

    # Register with orchestrator
    try:
        reg = await register(server, node_id)
        console.print(f"[green]Connected.[/green] {reg.get('message', '')}\n")
    except Exception as e:
        console.print(f"[red bold]Could not connect to orchestrator at {server}[/red bold]")
        console.print(f"[dim]{e}[/dim]")
        console.print("\nMake sure the orchestrator is running:")
        console.print("  [dim]py -m uvicorn server:app --host 0.0.0.0 --port 8000[/dim]")
        return

    console.print("[dim]Waiting for tasks... (Ctrl+C to stop)[/dim]\n")

    session = {"tasks": 0, "credits": 0}
    registered = True

    while True:
        try:
            # Re-register if we lost and regained connection
            if not registered:
                reg = await register(server, node_id)
                console.print(f"[green]Reconnected.[/green] {reg.get('message', '')}\n")
                registered = True

            task_id = await poll_and_execute(server, node_id, session)
            if task_id is None:
                await asyncio.sleep(3)

        except httpx.ConnectError:
            if registered:
                console.print("[yellow]Lost connection to orchestrator. Retrying...[/yellow]")
            registered = False
            await asyncio.sleep(10)

        except KeyboardInterrupt:
            _print_session_summary(node_id, session)
            break

        except Exception as e:
            console.print(f"[red]Error:[/red] {e}. Retrying in 5s...")
            await asyncio.sleep(5)


def _print_session_summary(node_id: str, session: dict):
    table = Table(title="Session Summary", box=box.SIMPLE, border_style="dim")
    table.add_column("Node")
    table.add_column("Tasks Completed", justify="center")
    table.add_column("Credits Earned", justify="right", style="yellow")
    table.add_row(node_id, str(session["tasks"]), str(session["credits"]))
    console.print()
    console.print(table)
    console.print("[dim]Thanks for contributing to the network.[/dim]\n")


if __name__ == "__main__":
    asyncio.run(main())
