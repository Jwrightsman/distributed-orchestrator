"""
CLI interface — pitch a task directly from the terminal.

Usage:
    python cli.py "Build me a landing page for a coffee shop"
"""

import asyncio
import sys

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

from orchestrator import run_pipeline

console = Console()


async def main():
    if len(sys.argv) < 2:
        console.print("[red]Usage: python cli.py \"your task description\"[/red]")
        sys.exit(1)

    task = " ".join(sys.argv[1:])

    console.print(Panel(task, title="[bold cyan]Task Pitched[/bold cyan]", border_style="cyan"))
    console.print()

    # ── Plan ──
    console.print("[bold yellow]PLANNER[/bold yellow] Decomposing task into subtasks...")
    result = await run_pipeline(task)

    # Show plan
    console.print()
    console.print("[bold yellow]PLAN[/bold yellow]")
    for st in result["plan"]:
        deps = f" (depends on: {st['depends_on']})" if st.get("depends_on") else ""
        console.print(f"  [{st['id']}] {st['title']}{deps}")

    # Show builder outputs
    console.print()
    for st in result["plan"]:
        console.print(Panel(
            Markdown(result["results"][st["id"]]),
            title=f"[bold green]BUILDER {st['id']}: {st['title']}[/bold green]",
            border_style="green",
        ))

    # Show review
    console.print()
    console.print(Panel(
        Markdown(result["review"]),
        title="[bold magenta]REVIEWER[/bold magenta]",
        border_style="magenta",
    ))

    console.print(f"\n[dim]Full output saved to: {result['project_dir']}[/dim]")


if __name__ == "__main__":
    asyncio.run(main())
