"""
CLI interface — pitch a task directly from the terminal.

Usage:
    python cli.py "Build me a landing page for a coffee shop"
"""

import asyncio
import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

from orchestrator import run_pipeline
from ollama_client import check_ollama

console = Console()


async def main():
    if len(sys.argv) < 2:
        console.print("[red]Usage: python cli.py \"your task description\"[/red]")
        sys.exit(1)

    task = " ".join(sys.argv[1:])

    # Pre-flight: check Ollama is running
    status = await check_ollama()
    if not status["ok"]:
        console.print(f"[red bold]ERROR:[/red bold] {status['error']}")
        sys.exit(1)

    console.print(Panel(task, title="[bold cyan]Task Pitched[/bold cyan]", border_style="cyan"))
    console.print()

    start = time.time()

    # ── Callbacks for live progress ──
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

    # ── Run pipeline ──
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
        sys.exit(1)
    except Exception as e:
        console.print(f"[red bold]Unexpected error:[/red bold] {e}")
        sys.exit(1)

    # Show review
    console.print(Panel(
        Markdown(result["review"]),
        title="[bold magenta]REVIEWER[/bold magenta]",
        border_style="magenta",
    ))

    elapsed = time.time() - start
    console.print(f"\n[dim]Completed in {elapsed:.0f}s — output saved to: {result['project_dir']}[/dim]")


if __name__ == "__main__":
    asyncio.run(main())
