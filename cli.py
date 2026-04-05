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
import zipfile
import io
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table

from orchestrator import run_pipeline, OUTPUT_DIR
from ollama_client import check_ollama, auto_detect_model, DEFAULT_MODEL
from ledger import get_standings
from memory import create_project, load_project, list_projects, PROJECTS_DIR

console = Console()


def show_projects():
    """List all projects with iteration counts."""
    projects = list_projects()
    if not projects:
        console.print("[dim]No projects yet. Start one with:[/dim] py cli.py --new-project \"name\" \"task\"")
        return

    table = Table(title="Projects", border_style="dim")
    table.add_column("ID", style="cyan", min_width=16)
    table.add_column("Name")
    table.add_column("Iterations", justify="center")
    table.add_column("Last Updated", style="dim")

    for p in projects:
        ts = p.get("last_updated", "")[:10]
        table.add_row(
            p["project_id"],
            p["name"],
            str(p.get("iteration_count", 0)),
            ts,
        )
    console.print(table)
    console.print(f"[dim]Continue a project: py cli.py --project <id> \"next task\"[/dim]")


async def run_task(task: str, project_id: str | None = None):
    """Run a single task through the pipeline with live output."""
    # Show project context if continuing
    if project_id:
        try:
            meta = load_project(project_id)
            iteration = meta["iteration_count"] + 1
            console.print(Panel(
                f"[bold]{meta['name']}[/bold]\n"
                f"[dim]Iteration {iteration} · {meta['iteration_count']} previous run(s)[/dim]\n\n"
                f"{task}",
                title="[bold cyan]Continuing Project[/bold cyan]",
                border_style="cyan",
            ))
        except FileNotFoundError:
            console.print(f"[red]Project '{project_id}' not found.[/red]")
            return
    else:
        console.print(Panel(task, title="[bold cyan]Task Pitched[/bold cyan]", border_style="cyan"))
    console.print()

    start = time.time()

    def on_plan(subtasks):
        console.print("[bold yellow]PLAN[/bold yellow]")
        for st in subtasks:
            deps = f" (depends on: {st['depends_on']})" if st.get("depends_on") else ""
            console.print(f"  [{st['id']}] {st['title']}{deps}")
        console.print()

    # Track which subtasks are currently streaming so we can print headers once
    _streaming_subtask: dict = {}

    def on_token(token: str, subtask: dict):
        sid = subtask["id"]
        if sid not in _streaming_subtask:
            _streaming_subtask[sid] = True
            console.print(f"\n[bold green]BUILDER {sid}: {subtask['title']}[/bold green]")
        # Write token directly — no newline, no markup parsing
        console.file.write(token)
        console.file.flush()

    def on_build(subtask, output):
        # Token stream already printed the content; just close it with a blank line
        if subtask["id"] in _streaming_subtask:
            console.print()  # newline after streamed content
        else:
            # No streaming happened (e.g. distributed node) — show the full output
            console.print(Panel(
                Markdown(output),
                title=f"[bold green]BUILDER {subtask['id']}: {subtask['title']}[/bold green]",
                border_style="green",
            ))

    def on_review_start():
        console.print("\n[bold magenta]REVIEWER[/bold magenta] Checking combined output...\n")

    console.print("[bold yellow]PLANNER[/bold yellow] Decomposing task into subtasks...\n")

    try:
        result = await run_pipeline(
            task,
            on_plan=on_plan,
            on_build=on_build,
            on_review_start=on_review_start,
            on_token=on_token,
            project_id=project_id,
        )
    except ValueError as e:
        console.print(f"[red bold]Pipeline failed:[/red bold] {e}")
        return
    except Exception as e:
        console.print(f"[red bold]Unexpected error:[/red bold] {e}")
        return

    # Show the clean final output if available, otherwise fall back to full review
    output_content = result.get("final_output") or result["review"]
    console.print(Panel(
        Markdown(output_content),
        title="[bold magenta]OUTPUT[/bold magenta]",
        border_style="magenta",
    ))

    # Show extracted code files
    if result.get("code_files"):
        console.print(f"\n[bold green]Extracted {len(result['code_files'])} runnable file(s):[/bold green]")
        for f in result["code_files"]:
            console.print(f"  [dim]{f}[/dim]")

    elapsed = time.time() - start
    pid = result.get("project_id") or project_id
    if pid:
        console.print(f"\n[dim]Completed in {elapsed:.0f}s — saved to: {result['project_dir']}[/dim]")
        console.print(f"[dim]Continue this project: py cli.py --project {pid} \"your next task\"[/dim]")
    else:
        console.print(f"\n[dim]Completed in {elapsed:.0f}s — output saved to: {result['project_dir']}[/dim]")


def _relative_time(timestamp_str: str) -> str:
    """Turn a YYYYmmdd_HHMMSS timestamp into a human-readable relative time."""
    try:
        dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
        delta = int(time.time() - dt.timestamp())
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{delta // 60}m ago"
        if delta < 86400:
            return f"{delta // 3600}h ago"
        return f"{delta // 86400}d ago"
    except ValueError:
        return timestamp_str


def show_history():
    """Show past pipeline runs."""
    if not OUTPUT_DIR.exists():
        console.print("[dim]No past runs yet.[/dim]")
        return

    table = Table(title="Past Runs", border_style="dim")
    table.add_column("When", style="dim", min_width=10)
    table.add_column("Task", style="cyan")
    table.add_column("Subtasks", justify="center")
    table.add_column("Rating", justify="center")

    count = 0
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
            ts = log.get("timestamp", d.name)
            when = _relative_time(ts)

            # Check review file for rating
            review_file = d / "review.md"
            rating = "?"
            if review_file.exists():
                review = review_file.read_text(errors="ignore")
                if "PASS" in review:
                    rating = "[green]PASS[/green]"
                elif "NEEDS_WORK" in review:
                    rating = "[yellow]NEEDS_WORK[/yellow]"
                elif "FAIL" in review:
                    rating = "[red]FAIL[/red]"
            table.add_row(when, task, subtask_count, rating)
            count += 1
        except (json.JSONDecodeError, OSError):
            pass
        if count >= 20:
            break

    if count == 0:
        console.print("[dim]No past runs yet.[/dim]")
    else:
        console.print(table)


def show_standings():
    """Show guild standings — who contributed what."""
    standings = get_standings()
    if not standings:
        console.print("[dim]No contributions recorded yet.[/dim]")
        return

    table = Table(title="Guild Standings", border_style="dim")
    table.add_column("Rank", justify="center", style="bold")
    table.add_column("Contributor", style="cyan")
    table.add_column("Credits", justify="right", style="yellow")
    table.add_column("Tasks", justify="center")
    table.add_column("Pitches", justify="center")

    for i, s in enumerate(standings):
        rank = str(i + 1)
        table.add_row(
            rank,
            s["contributor"],
            f"{s['total_credits']:.0f}",
            str(s["compute_tasks"]),
            str(s["pitches"]),
        )

    console.print(table)


async def interactive():
    """Interactive mode — keep pitching tasks."""
    console.print(Panel(
        "[bold]Distributed AI Orchestrator[/bold]\n"
        "[dim]history · standings · projects · quit — or just type a task[/dim]",
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
            if task.lower() in ("standings", "s", "guild"):
                show_standings()
                continue
            if task.lower() in ("projects", "p"):
                show_projects()
                continue
            if task.lower() in ("quit", "exit", "q"):
                break
            await run_task(task)
            console.print()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Done.[/dim]")
            break


def _flag_value(flag: str) -> str | None:
    """Return the value after a --flag in sys.argv, or None."""
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


async def run_demo(fast: bool = False):
    """Demo mode — shows the full project memory flow in two pitches.

    Perfect for screen recording: creates a project, runs the first task,
    pauses so the viewer can read, then continues with a follow-up task
    that proves the AI remembers what it already built.

    Pass fast=True (--demo-fast) to skip the inter-pitch pause.
    """
    console.print(Panel(
        "[bold cyan]DEMO MODE[/bold cyan] — showing project memory in action\n\n"
        "[dim]Pitch 1:[/dim] Build a Python expense tracker with categories and a spending summary\n"
        "[dim]Pitch 2:[/dim] Add a monthly budget feature that warns when you're over budget\n\n"
        "[dim]The second pitch will load context from the first — the AI picks up exactly where it left off.[/dim]",
        title="[bold]Distributed AI Orchestrator[/bold]",
        border_style="cyan",
    ))
    console.print()

    # Pitch 1 — seed the project
    project_id = create_project("demo-app", "Build a Python expense tracker with categories and a spending summary")
    console.print(f"[dim]Project created: {project_id}[/dim]\n")
    await run_task(
        "Build a Python expense tracker with categories and a spending summary",
        project_id=project_id,
    )

    console.print()
    console.print("[bold cyan]─" * 60 + "[/bold cyan]")
    if not fast:
        console.print("[dim]Pausing 2 seconds before continuation pitch...[/dim]")
        await asyncio.sleep(2)
    console.print("[bold cyan]─" * 60 + "[/bold cyan]")
    console.print()

    # Pitch 2 — continuation that exercises project memory
    await run_task(
        "Add a monthly budget feature that warns when you're over budget",
        project_id=project_id,
    )

    console.print()
    console.print(Panel(
        f"[bold green]Demo complete.[/bold green]\n\n"
        f"Both runs saved under project [cyan]{project_id}[/cyan]\n"
        f"[dim]Continue anytime: py cli.py --project {project_id} \"your next task\"[/dim]",
        border_style="green",
    ))


def import_fork(zip_path: str):
    """Import a fork template ZIP and set up a new project ready to continue."""
    p = Path(zip_path)
    if not p.exists():
        console.print(f"[red bold]File not found:[/red bold] {zip_path}")
        return

    try:
        with zipfile.ZipFile(p, "r") as zf:
            names = zf.namelist()

            # Read task.txt
            if "task.txt" not in names:
                console.print("[red bold]Invalid fork ZIP:[/red bold] missing task.txt")
                return
            task = zf.read("task.txt").decode("utf-8", errors="ignore").strip()

            # Read memory.md if present
            memory_content = ""
            if "memory.md" in names:
                memory_content = zf.read("memory.md").decode("utf-8", errors="ignore")

            # Read fork_config.json for extra context (best-effort)
            fork_config = {}
            if "fork_config.json" in names:
                try:
                    fork_config = json.loads(zf.read("fork_config.json").decode("utf-8"))
                except Exception:
                    pass

    except zipfile.BadZipFile:
        console.print(f"[red bold]Not a valid ZIP file:[/red bold] {zip_path}")
        return

    # Derive a project name from the task (first ~40 chars)
    project_name = task[:40].strip()
    if not project_name:
        project_name = "forked-project"

    # Create the project
    project_id = create_project(project_name, task)

    # Write the imported memory.md to the project directory
    if memory_content:
        from memory import PROJECTS_DIR
        memory_file = PROJECTS_DIR / project_id / "memory.md"
        memory_file.write_text(memory_content, encoding="utf-8")

    original_ts = fork_config.get("original_timestamp", "")
    rating = fork_config.get("rating", "")
    origin_note = f"\n[dim]Original run: {original_ts}  Rating: {rating}[/dim]" if original_ts else ""

    console.print(Panel(
        f"[bold green]Fork imported as project:[/bold green] [cyan]{project_id}[/cyan]{origin_note}\n\n"
        f"[bold]Task:[/bold]\n{task}\n\n"
        f"[dim]Memory context loaded from fork.[/dim]\n\n"
        f"[bold]Run it:[/bold]\n  py cli.py --project {project_id} \"{task}\"",
        title="[bold cyan]Fork Imported[/bold cyan]",
        border_style="cyan",
    ))


async def main():
    # Pre-flight: check Ollama is running
    status = await check_ollama()
    if not status["ok"]:
        console.print(f"[red bold]ERROR:[/red bold] {status['error']}")
        sys.exit(1)

    # ── Flags that don't need Ollama ──
    import_zip = _flag_value("--import")
    if import_zip:
        import_fork(import_zip)
        return

    if "--history" in sys.argv:
        show_history()
        return
    if "--standings" in sys.argv:
        show_standings()
        return
    if "--projects" in sys.argv:
        show_projects()
        return

    # ── Create a new named project ──
    # Usage: py cli.py --new-project "name" "initial task"
    if "--new-project" in sys.argv:
        idx = sys.argv.index("--new-project")
        remaining = sys.argv[idx + 1:]
        if len(remaining) < 2:
            console.print("[red]Usage:[/red] py cli.py --new-project \"project name\" \"initial task\"")
            return
        proj_name = remaining[0]
        task = " ".join(remaining[1:])
        project_id = create_project(proj_name, task)
        console.print(f"[green]Project created:[/green] [cyan]{project_id}[/cyan]")
        await run_task(task, project_id=project_id)
        return

    # ── Continue an existing project ──
    # Usage: py cli.py --project my-app "next task"
    project_id = _flag_value("--project")
    if project_id:
        args = [a for a in sys.argv[1:] if not a.startswith("--") and a != project_id]
        if not args:
            # No task given — show project info and prompt
            try:
                meta = load_project(project_id)
                console.print(f"[bold]{meta['name']}[/bold] · {meta['iteration_count']} iteration(s)")
                console.print(f"[dim]Goal: {meta['initial_task']}[/dim]\n")
                task = console.input("[bold cyan]What's next?>[/bold cyan] ").strip()
                if task:
                    await run_task(task, project_id=project_id)
            except FileNotFoundError:
                console.print(f"[red]Project '{project_id}' not found.[/red]")
            return
        await run_task(" ".join(args), project_id=project_id)
        return

    # ── Demo mode — two-pitch project memory showcase ──
    if "--demo-fast" in sys.argv:
        await run_demo(fast=True)
        return
    if "--demo" in sys.argv:
        await run_demo()
        return

    # ── Direct task or interactive mode ──
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        await run_task(" ".join(args))
    else:
        await interactive()


if __name__ == "__main__":
    asyncio.run(main())
