"""
One-command setup to join the network as a worker node.

Usage:
    python join.py https://COORDINATOR                 # no code needed if enrolled
    python join.py https://COORDINATOR --ask-secret    # type the code, echo off
    python join.py https://COORDINATOR --secret-file ~/code.txt

`--secret VALUE` still works and still warns: an argument vector is readable by
every other user on the machine. `python worker_installer.py` is the guided
path and never accepts a code on the command line at all.

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
from worker_secret import (
    AdmissionSecretError,
    add_admission_secret_arguments,
    resolve_from_args,
    warn_if_argv_secret,
)

console = Console()

async def ensure_ollama():
    """Make sure Ollama is running and has the right model."""
    status = await check_ollama()
    if not status["ok"]:
        # The same diagnosis the guided installer gives, imported rather than
        # written a second time: a Mac with Ollama installed but never opened
        # must not be told to install Ollama.
        from worker_installer import ollama_absence_hint

        console.print("[red bold]Ollama is not running.[/red bold]")
        console.print(ollama_absence_hint())
        return False

    if not any(DEFAULT_MODEL in m for m in status["models"]):
        from worker_installer import ollama_cli_missing_hint

        console.print(f"Model [yellow]{DEFAULT_MODEL}[/yellow] not found. Pulling now (~2-3GB)...")
        try:
            result = subprocess.run(["ollama", "pull", DEFAULT_MODEL], capture_output=False)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            console.print("[red]The 'ollama' command is not on this terminal's path.[/red]")
            console.print(ollama_cli_missing_hint())
            return False
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
            "Explicit coordinator origin. https:// for anything on a network; "
            "http:// only for loopback local development"
        ),
    )
    # Three ways in, only one of which is an argument vector — and that one
    # warns about itself. See worker_secret for why prompting is opt-in.
    add_admission_secret_arguments(parser)
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

    # Before anything else, because by the time this runs the exposure has
    # already happened: the code is in this process's argument vector and in
    # the shell's history file. Saying so only on the paths that get as far as
    # a valid address would be saying so uselessly.
    warned_about_argv = warn_if_argv_secret(args.secret)

    try:
        server = normalize_coordinator(args.server)
    except WorkerIdentityError as exc:
        parser.error(str(exc))
    console.print(f"Joining network at [cyan]{server}[/cyan]\n")

    if not confirm_consent(server, args.yes):
        sys.exit(0)

    # After consent, so nobody types an invitation code before being told what
    # joining costs them, and before ensure_ollama() writes 2.5 GB to disk.
    try:
        secret = resolve_from_args(args, warn=not warned_about_argv)
    except AdmissionSecretError as exc:
        console.print(f"[red bold]Cannot read the admission secret:[/red bold] {exc}")
        sys.exit(1)

    if not await ensure_ollama():
        sys.exit(1)

    # Delegate entirely to node.main() — it handles register, poll, Rich output.
    # The secret is handed over as an argument to a Python function, not as an
    # element of an argument list, so it never reaches argv even in-process.
    node_argv = ["--server", server]
    if args.identity_file:
        node_argv += ["--identity-file", args.identity_file]
    from node import main as node_main
    await node_main(node_argv, admission_secret=secret)


if __name__ == "__main__":
    asyncio.run(main())
