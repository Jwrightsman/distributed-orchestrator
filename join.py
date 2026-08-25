"""
One-command setup to join the network as a worker node.

Usage:
    python join.py http://ORCHESTRATOR_IP:8000   # direct

Does everything:
  1. Validates the explicit coordinator origin you provide
  2. Checks if Ollama is installed and running
  3. Pulls the model if needed
  4. Creates or loads a private per-node enrollment identity
  5. Registers with the orchestrator
  6. Receives a process-local server-issued node session
  7. Starts polling for tasks
"""

import asyncio
import subprocess
import sys

from rich.console import Console
from rich.panel import Panel
from ollama_client import check_ollama, DEFAULT_MODEL
from worker_identity import WorkerIdentityError, normalize_coordinator

console = Console()

async def ensure_ollama():
    """Make sure Ollama is running and has the right model."""
    status = await check_ollama()
    if not status["ok"]:
        console.print("[red bold]Ollama is not running.[/red bold]")
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


def confirm_consent(server: str, assume_yes: bool) -> bool:
    """Show what joining actually does, and require a human to agree.

    This gate exists because Mycelium is discussed in agent-native communities,
    where the plausible failure is an agent reading "join the swarm" and running
    this unattended on a machine whose owner never asked for it. Joining donates
    someone's CPU to strangers and writes gigabytes to their disk. That is the
    machine owner's decision, not an agent's.

    `--yes` is for people deliberately scripting their own machines. AGENTS.md
    asks agents not to pass it on someone else's behalf.
    """
    if assume_yes:
        console.print("[dim]--yes given: skipping the consent prompt.[/dim]\n")
        return True

    console.print(Panel(
        f"You are about to join [cyan]{server}[/cyan] as a worker node.\n\n"
        "[bold]What this will do to THIS computer:[/bold]\n"
        f"  - Download the [bold]{DEFAULT_MODEL}[/bold] model (~2.5 GB) if it is not already here\n"
        "  - Use your CPU at full load, in bursts of minutes, to build parts of\n"
        "    tasks that [bold]other people[/bold] submit\n"
        "  - Receive assigned task prompts as readable text on this computer\n"
        "  - Send the resulting text back to that orchestrator\n"
        "  - Store one small private enrollment identity in your user configuration\n"
        "  - Keep running until you stop it\n\n"
        "[bold]What it will NOT do:[/bold]\n"
        "  - Run any code it receives - it returns text, nothing is executed here\n"
        "  - Open any inbound port - the connection is outbound only\n"
        "  - Read your files or send telemetry\n\n"
        "[dim]Stop any time with Ctrl+C or by closing this window. Work you were\n"
        "holding is reassigned automatically.[/dim]",
        title="[bold]Before you join[/bold]",
        border_style="yellow",
    ))

    if not sys.stdin.isatty():
        # Nobody is here to agree. Refuse rather than assume - an unattended
        # join is the exact scenario this gate exists to prevent.
        console.print(
            "\n[red bold]Not running in a terminal, so nobody can consent.[/red bold]\n"
            "[dim]If this is your own machine and you meant to script it, pass --yes.[/dim]"
        )
        return False

    try:
        answer = input("\nType 'yes' to join, or press Enter to cancel: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[dim]Cancelled.[/dim]")
        return False

    if answer not in ("y", "yes"):
        console.print("[dim]Cancelled - nothing was installed or started.[/dim]")
        return False
    console.print()
    return True


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Join the network as a worker node (one-command setup)")
    parser.add_argument(
        "server",
        help=(
            "Explicit orchestrator origin (HTTPS, private-overlay HTTP, or "
            "loopback local development)"
        ),
    )
    parser.add_argument(
        "--secret",
        default="",
        help=(
            "Shared node-admission secret; the orchestrator issues a separate "
            "process-local session after registration"
        ),
    )
    parser.add_argument(
        "--identity-file",
        help=(
            "Private durable worker identity JSON (default: a coordinator-hashed "
            "file in the current user's Mycelium configuration directory)"
        ),
    )
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip the consent prompt. For scripting your OWN machine; "
                             "agents should not pass this on someone else's behalf (see AGENTS.md).")
    args = parser.parse_args()

    try:
        server = normalize_coordinator(args.server)
    except WorkerIdentityError as exc:
        parser.error(str(exc))
    console.print(f"Joining network at [cyan]{server}[/cyan]\n")

    if not confirm_consent(server, args.yes):
        sys.exit(0)

    if not await ensure_ollama():
        sys.exit(1)

    # Delegate entirely to node.main() — it handles register, poll, Rich output
    node_argv = ["node.py", "--server", server]
    if args.secret:
        node_argv += ["--secret", args.secret]
    if args.identity_file:
        node_argv += ["--identity-file", args.identity_file]
    sys.argv = node_argv
    from node import main as node_main
    await node_main()


if __name__ == "__main__":
    asyncio.run(main())
