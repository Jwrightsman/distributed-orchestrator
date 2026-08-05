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
        console.print("  Status:  [green]running[/green]")
        console.print(f"  URL:     {config['ollama_url']}")
        console.print(f"  Models:  {', '.join(status['models']) or 'none pulled'}")
        console.print(f"  Active:  {DEFAULT_MODEL}")
    else:
        console.print("  Status:  [red]offline[/red]")
        console.print(f"  Error:   {status['error']}")

    console.print()

    # Config
    console.print("[bold]Config[/bold]")
    console.print(f"  Model:       {config['model']}")
    console.print(f"  Timeout:     {config['timeout']}s")
    console.print(f"  Retries:     {config['planner_retries']}")

    # Node auth
    secret = config.get("node_secret", "")
    if secret:
        console.print(f"  Node auth:   [green]enabled[/green] ({secret[:4]}{'*' * max(0, len(secret) - 4)})")
    else:
        console.print("  Node auth:   [dim]off (any node can join)[/dim]")

    # Pitch auth
    pitch_key = config.get("pitch_key", "")
    if pitch_key:
        console.print(f"  Pitch auth:  [green]enabled[/green] ({pitch_key[:4]}{'*' * max(0, len(pitch_key) - 4)})")
    else:
        console.print("  Pitch auth:  [dim]off (anyone can pitch)[/dim]")

    # Agent specialization
    role_map = config.get("role_model_map", {})
    if role_map:
        parts = ", ".join(f"{k}→{v}" for k, v in role_map.items())
        console.print(f"  Role routing: [cyan]{parts}[/cyan]")
    else:
        console.print("  Role routing: [dim]any node[/dim]")

    # External provider
    provider = config.get("provider")
    if provider and config.get("provider_api_key") and config.get("provider_model"):
        roles = ", ".join(config.get("provider_roles", []))
        console.print(f"  Provider:    [cyan]{provider}[/cyan] / {config['provider_model']} (roles: {roles})")
    else:
        console.print("  Provider:    [dim]Ollama only[/dim]")

    console.print()

    # Server + nodes (optional)
    if check_server:
        server = f"http://localhost:{config['port']}"
        console.print("[bold]Server[/bold]")
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{server}/health")
                health = resp.json()
                console.print("  Status:  [green]running[/green]")
                console.print(f"  URL:     {server}")
                console.print(f"  Tasks:   {health['tasks_pending']} pending")

                # Job stats
                try:
                    jresp = await client.get(f"{server}/jobs?limit=100")
                    jdata = jresp.json()
                    running = sum(1 for j in jdata["jobs"] if j["status"] == "running")
                    complete = sum(1 for j in jdata["jobs"] if j["status"] == "complete")
                    console.print(f"  Jobs:    {running} running · {complete} completed")
                except Exception:
                    pass

                resp = await client.get(f"{server}/nodes")
                nodes = resp.json()
                console.print(f"  Nodes:   {nodes['count']} connected")

                if nodes["count"] > 0:
                    console.print()
                    table = Table(title="Connected Nodes", border_style="dim")
                    table.add_column("Node", style="cyan")
                    table.add_column("Platform")
                    table.add_column("Model")
                    table.add_column("Capabilities", style="dim")
                    table.add_column("Tasks", justify="center")
                    table.add_column("Credits", justify="right", style="yellow")
                    for n in nodes["nodes"]:
                        # Filter model: cap tags — they're redundant with the model column
                        caps = [c for c in n.get("capabilities", []) if not c.startswith("model:")]
                        table.add_row(
                            n["node_id"],
                            f"{n['platform']} / {n['machine']}",
                            n["model"],
                            ", ".join(caps) or "—",
                            str(n["tasks_completed"]),
                            str(n.get("credits_earned", 0)),
                        )
                    console.print(table)
        except httpx.ConnectError:
            console.print("  Status:  [dim]offline[/dim]")
            console.print(f"  Start:   python -m uvicorn server:app --host 0.0.0.0 --port {config['port']}")
    else:
        console.print("[dim]Run with --server to check orchestrator + nodes[/dim]")


if __name__ == "__main__":
    asyncio.run(main())
