"""
One-command setup to join the network as a worker node.

Usage:
    python join.py http://ORCHESTRATOR_IP:8000   # direct
    python join.py                               # auto-discover on LAN

Does everything:
  1. Discovers the orchestrator (or uses the URL you provide)
  2. Checks if Ollama is installed and running
  3. Pulls the model if needed
  4. Registers with the orchestrator
  5. Starts polling for tasks
"""

import asyncio
import socket
import subprocess
import sys

import httpx
from rich.console import Console
from rich.panel import Panel
from ollama_client import check_ollama, DEFAULT_MODEL

console = Console()

# Port to scan when auto-discovering the orchestrator
_DISCOVERY_PORT = 8000


async def _try_host(ip: str, port: int) -> str | None:
    """Return the base URL if a healthy orchestrator is found at ip:port."""
    url = f"http://{ip}:{port}"
    try:
        async with httpx.AsyncClient(timeout=1.2) as client:
            r = await client.get(f"{url}/health")
            if r.status_code == 200 and r.json().get("status") in ("ok", "degraded"):
                return url
    except Exception:
        pass
    return None


async def discover_orchestrator(port: int = _DISCOVERY_PORT) -> str | None:
    """Scan common LAN IPs for a running orchestrator.

    Checks localhost first, then the subnet's likely gateway (.1, .254) and
    a handful of common static IPs.  Returns the first URL that responds.
    """
    # Derive local subnet from the machine's outbound interface
    prefix = "192.168.1"  # fallback
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        prefix = ".".join(local_ip.split(".")[:3])
    except Exception:
        pass

    candidates = [
        "127.0.0.1",            # local machine
        f"{prefix}.1",          # typical gateway / server
        f"{prefix}.254",
        f"{prefix}.100",
        f"{prefix}.50",
        f"{prefix}.10",
    ]
    # Remove duplicates while preserving order
    seen: set[str] = set()
    unique = [c for c in candidates if not (c in seen or seen.add(c))]  # type: ignore[func-returns-value]

    tasks = [asyncio.create_task(_try_host(ip, port)) for ip in unique]
    found = None
    for coro in asyncio.as_completed(tasks):
        result = await coro
        if result and not found:
            found = result
    # Cancel remaining tasks
    for t in tasks:
        t.cancel()
    return found


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
        "  - Send the resulting text back to that orchestrator\n"
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
    parser.add_argument("server", nargs="?", default=None, help="Orchestrator URL (e.g. http://192.168.1.50:8000) — omit to auto-discover")
    parser.add_argument("--secret", default="", help="Shared secret if the orchestrator has node_secret set in config.json")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip the consent prompt. For scripting your OWN machine; "
                             "agents should not pass this on someone else's behalf (see AGENTS.md).")
    args = parser.parse_args()

    if args.server:
        server = args.server.rstrip("/")
        console.print(f"Joining network at [cyan]{server}[/cyan]\n")
    else:
        console.print("[dim]No server URL given — scanning local network for an orchestrator...[/dim]")
        server = await discover_orchestrator()
        if server:
            console.print(f"[green]Found orchestrator at[/green] [cyan]{server}[/cyan]\n")
        else:
            console.print(
                "[red bold]No orchestrator found on the local network.[/red bold]\n"
                "[dim]Make sure the server is running:[/dim]\n"
                "  [dim]python -m uvicorn server:app --host 0.0.0.0 --port 8000[/dim]\n"
                "[dim]Or pass the URL directly:[/dim]\n"
                "  [dim]python join.py http://ORCHESTRATOR_IP:8000[/dim]"
            )
            sys.exit(1)

    if not confirm_consent(server, args.yes):
        sys.exit(0)

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
