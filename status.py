"""
Quick status check — see what's running without opening the dashboard.

Usage:
    python status.py              # check local Ollama + models
    python status.py --server     # also check orchestrator server + nodes
"""

import asyncio
import sys

import httpx
from rich.console import Console
from rich.table import Table

from ollama_client import check_ollama, DEFAULT_MODEL
from config import get as get_config

console = Console()


async def main():
    config = get_config()
    check_server = "--server" in sys.argv

    # Ollama status
    console.print("[bold]Ollama[/bold]")
    status = await check_ollama()
    if status["ok"]:
        console.print(f"  Status:  [green]running[/green]")
        console.print(f"  URL:     {config['ollama_url']}")
        console.print(f"  Models:  {', '.join(status['models']) or 'none pulled'}")
        console.print(f"  Active:  {DEFAULT_MODEL}")
    else:
        console.print(f"  Status:  [red]offline[/red]")
        console.print(f"  Error:   {status['error']}")

    console.print()

    # Config
    console.print("[bold]Config[/bold]")
    console.print(f"  Model:    {config['model']}")
    console.print(f"  Timeout:  {config['timeout']}s")
    console.print(f"  Retries:  {config['planner_retries']}")

    console.print()

    # Server + nodes (optional)
    if check_server:
        server = f"http://localhost:{config['port']}"
        console.print("[bold]Server[/bold]")
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{server}/health")
                health = resp.json()
                console.print(f"  Status:  [green]running[/green]")
                console.print(f"  URL:     {server}")
                console.print(f"  Tasks:   {health['tasks_pending']} pending")

                resp = await client.get(f"{server}/nodes")
                nodes = resp.json()
                console.print(f"  Nodes:   {nodes['count']} connected")

                if nodes["count"] > 0:
                    console.print()
                    table = Table(title="Connected Nodes", border_style="dim")
                    table.add_column("Node", style="cyan")
                    table.add_column("Platform")
                    table.add_column("Model")
                    table.add_column("Tasks Done", justify="center")
                    for n in nodes["nodes"]:
                        table.add_row(
                            n["node_id"],
                            f"{n['platform']} / {n['machine']}",
                            n["model"],
                            str(n["tasks_completed"]),
                        )
                    console.print(table)
        except httpx.ConnectError:
            console.print(f"  Status:  [dim]offline[/dim]")
            console.print(f"  Start:   py -m uvicorn server:app --host 0.0.0.0 --port {config['port']}")
    else:
        console.print("[dim]Run with --server to check orchestrator + nodes[/dim]")


if __name__ == "__main__":
    asyncio.run(main())
