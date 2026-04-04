"""
One-command setup to join the network as a worker node.

Usage:
    python join.py http://ORCHESTRATOR_IP:8000

Does everything:
  1. Checks if Ollama is installed and running
  2. Pulls the model if needed
  3. Registers with the orchestrator
  4. Starts polling for tasks
"""

import asyncio
import subprocess
import sys

from rich.console import Console
from ollama_client import check_ollama, DEFAULT_MODEL

console = Console()


async def ensure_ollama():
    """Make sure Ollama is running and has the right model."""
    status = await check_ollama()
    if not status["ok"]:
        console.print(f"[red bold]Ollama is not running.[/red bold]")
        console.print("Start it with: [dim]ollama serve[/dim]")
        console.print("Download from: [dim]https://ollama.com[/dim]")
        return False

    if not any(DEFAULT_MODEL in m for m in status["models"]):
        console.print(f"Model [yellow]{DEFAULT_MODEL}[/yellow] not found. Pulling now (~2-3GB)...")
        result = subprocess.run(["ollama", "pull", DEFAULT_MODEL], capture_output=False)
        if result.returncode != 0:
            console.print(f"[red]Failed to pull {DEFAULT_MODEL}.[/red]")
            return False
        console.print(f"[green]{DEFAULT_MODEL} ready.[/green]\n")

    return True


async def main():
    if len(sys.argv) < 2:
        console.print("[bold]Usage:[/bold] py join.py http://ORCHESTRATOR_IP:8000")
        console.print("\nExample:")
        console.print("  [dim]py join.py http://192.168.1.50:8000[/dim]")
        sys.exit(1)

    server = sys.argv[1].rstrip("/")
    console.print(f"Joining network at [cyan]{server}[/cyan]\n")

    if not await ensure_ollama():
        sys.exit(1)

    # Delegate entirely to node.main() — it handles register, poll, Rich output
    import sys as _sys
    _sys.argv = ["node.py", "--server", server]
    from node import main as node_main
    await node_main()


if __name__ == "__main__":
    asyncio.run(main())
