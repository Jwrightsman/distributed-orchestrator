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
    import argparse
    parser = argparse.ArgumentParser(description="Join the network as a worker node (one-command setup)")
    parser.add_argument("server", help="Orchestrator URL (e.g. http://192.168.1.50:8000)")
    parser.add_argument("--secret", default="", help="Shared secret if the orchestrator has node_secret set in config.json")
    args = parser.parse_args()

    server = args.server.rstrip("/")
    console.print(f"Joining network at [cyan]{server}[/cyan]\n")

    if not await ensure_ollama():
        sys.exit(1)

    # Delegate entirely to node.main() — it handles register, poll, Rich output
    node_argv = ["node.py", "--server", server]
    if args.secret:
        node_argv += ["--secret", args.secret]
    sys.argv = node_argv
    from node import main as node_main
    await node_main()


if __name__ == "__main__":
    asyncio.run(main())
