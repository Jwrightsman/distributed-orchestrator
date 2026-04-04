"""
CLI interface — pitch tasks from the terminal.

Usage:
    python cli.py "Build me a landing page for a coffee shop"
    python cli.py                           # interactive mode
    python cli.py --history                  # show past runs
"""

import asyncio
import sys
import time
import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table

from orchestrator import run_pipeline, OUTPUT_DIR
from ollama_client import check_ollama, auto_detect_model, DEFAULT_MODEL

console = Console()


async def run_task(task: str):
    """Run a single task through the pipeline with live output."""
    console.print(Panel(task, title="[bold cyan]Task Pitched[/bold cyan]", border_style="cyan"))
    console.print()

    start = time.time()

    def on_plan(subtasks):
        console.print("[bold yellow]PLAN[/bold yellow]")
        for st in subtasks:
            deps = f" (depends on: {st['depends_on']})" if st.get("depends_on") else ""
            console.print(f"  [{st['id']}] {st['title']}{deps}")
        console.print()

    def on_build(subtask, output):
        console.print(Panel(
            Markdown(output),
            title=f"[bold green]BUILDER {subtask['id']}: {subtask['title']}[/bold green]",
            border_style="green",
        ))

    def on_review_start():
        console.print("[bold magenta]REVIEWER[/bold magenta] Checking combined output...\n")

    console.print("[bold yellow]PLANNER[/bold yellow] Decomposing task into subtasks...\n")

    try:
        result = await run_pipeline(
            task,
            on_plan=on_plan,
            on_build=on_build,
            on_review_start=on_review_start,
        )
    except ValueError as e:
        console.print(f"[red bold]Pipeline failed:[/red bold] {e}")
        return
    except Exception as e:
        console.print(f"[red bold]Unexpected error:[/red bold] {e}")
        return

    console.print(Panel(
        Markdown(result["review"]),
        title="[bold magenta]REVIEWER[/bold magenta]",
        border_style="magenta",
    ))

    elapsed = time.time() - start
    console.print(f"\n[dim]Completed in {elapsed:.0f}s — output saved to: {result['project_dir']}[/dim]")


def show_history():
    """Show past pipeline runs."""
    if not OUTPUT_DIR.exists():
        console.print("[dim]No past runs yet.[/dim]")
        return

    table = Table(title="Past Runs", border_style="dim")
    table.add_column("Time", style="dim")
    table.add_column("Task", style="cyan")
    table.add_column("Subtasks", justify="center")
    table.add_column("Rating", justify="center")

    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        log_file = d / "full_log.json"
        if not log_file.exists():
            continue
        try:
            log = json.loads(log_file.read_text())
            task = log.get("task", "?")
            if len(task) > 60:
                task = task[:57] + "..."
            subtask_count = str(len(log.get("plan", [])))
            review = log.get("review", "")
            rating = "?"
            if "PASS" in review:
                rating = "[green]PASS[/green]"
            elif "NEEDS_WORK" in review:
                rating = "[yellow]NEEDS_WORK[/yellow]"
            elif "FAIL" in review:
                rating = "[red]FAIL[/red]"
            table.add_row(d.name, task, subtask_count, rating)
        except (json.JSONDecodeError, OSError):
            pass

    console.print(table)


async def interactive():
    """Interactive mode — keep pitching tasks."""
    console.print(Panel(
        "[bold]Distributed AI Orchestrator[/bold]\n"
        "[dim]Type a task and press Enter. Type 'history' to see past runs. Ctrl+C to quit.[/dim]",
        border_style="cyan",
    ))

    # Show model info
    best = await auto_detect_model()
    model = best or DEFAULT_MODEL
    console.print(f"[dim]Model: {model}[/dim]\n")

    while True:
        try:
            task = console.input("[bold cyan]Pitch>[/bold cyan] ").strip()
            if not task:
                continue
            if task.lower() in ("history", "h"):
                show_history()
                continue
            if task.lower() in ("quit", "exit", "q"):
                break
            await run_task(task)
            console.print()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Done.[/dim]")
            break


async def main():
    # Pre-flight: check Ollama is running
    status = await check_ollama()
    if not status["ok"]:
        console.print(f"[red bold]ERROR:[/red bold] {status['error']}")
        sys.exit(1)

    # --history flag
    if "--history" in sys.argv:
        show_history()
        return

    # Direct task from command line
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        await run_task(" ".join(args))
    else:
        # Interactive mode
        await interactive()


if __name__ == "__main__":
    asyncio.run(main())
