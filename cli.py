"""
CLI interface — pitch tasks from the terminal.

Usage:
    python cli.py "Build me a landing page for a coffee shop"
    python cli.py                           # interactive mode
    python cli.py --history                  # show past runs

Showcases — the swarm builds something that opens in your browser:
    python cli.py --demo-showcase chart      # bar chart, measured 10/10 (see below)
    python cli.py --demo-showcase clock      # animated analog clock, 3/4
    python cli.py --demo-showcase particles  # particle field, 3/4
    python cli.py --demo-showcase            # Snake game, 2/10 — the hard case

Reliability numbers are real runs checked in a real browser
(docs/showcase-ceiling.md). The chart is the one that is safe to generate live
on camera; the game is not, and a verified-playable copy is committed at
docs/demo-assets/snake-game/. Add --no-open to skip launching the browser.
"""

import asyncio
import argparse
import sys
import time
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# Legacy Windows consoles default to cp1252, which can't encode the demo's
# unicode output and crashes rich mid-print. Force UTF-8 (lossy if impossible).
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table

from orchestrator import run_pipeline, OUTPUT_DIR
from execution.contracts import EnsembleOptionsV1, ExecutionRequestV1
from execution.service import get_execution_service
from ollama_client import check_ollama, auto_detect_model, DEFAULT_MODEL
from ledger import get_standings
from memory import create_project, load_project, list_projects, PROJECTS_DIR
import showcase

SERVER_URL = "http://localhost:8000"
PITCH_KEY = os.environ.get("PITCH_KEY", "")
VIEWER_KEY = os.environ.get("VIEWER_KEY", "")

console = Console()


def _server_headers(*, pitch: bool = False, viewer: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {}
    if pitch and PITCH_KEY:
        headers["X-Pitch-Key"] = PITCH_KEY
    if viewer and VIEWER_KEY:
        headers["X-Viewer-Key"] = VIEWER_KEY
    return headers


def _health_node_count(health: dict) -> int:
    """Consume the stable, privacy-safe health schema."""
    try:
        return max(0, int(health.get("nodes_online", 0) or 0))
    except (TypeError, ValueError):
        return 0


def show_projects():
    """List all projects with iteration counts."""
    projects = list_projects()
    if not projects:
        console.print("[dim]No projects yet. Start one with:[/dim] python cli.py --new-project \"name\" \"task\"")
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
    console.print("[dim]Continue a project: python cli.py --project <id> \"next task\"[/dim]")


async def run_task(
    task: str,
    project_id: str | None = None,
    *,
    strategy: str = "auto",
    candidates: int | None = None,
    placement: str = "local",
):
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

    if strategy in ("ensemble", "direct"):
        console.print("[bold yellow]GENERATOR[/bold yellow] Producing complete candidate(s)...\n")
    else:
        console.print("[bold yellow]PLANNER[/bold yellow] Decomposing task into subtasks...\n")

    try:
        strategy_options = None
        if strategy == "direct":
            strategy_options = EnsembleOptionsV1(candidates=1, concurrency=1)
        elif candidates is not None:
            strategy_options = EnsembleOptionsV1(
                candidates=candidates,
                concurrency=candidates if placement == "distributed" else 1,
            )
        request = ExecutionRequestV1(
            task=task,
            project_id=project_id,
            strategy=strategy,
            strategy_options=strategy_options,
            placement=placement,
        )
        execution = await get_execution_service().execute(
            request,
            callbacks={
                "on_plan": on_plan,
                "on_build": on_build,
                "on_review_start": on_review_start,
                "on_token": on_token,
            },
            dag_runner=run_pipeline,
        )
        if execution.result.status == "failed":
            message = execution.result.errors[0].message if execution.result.errors else "execution failed"
            raise ValueError(message)
        result = dict(execution.legacy_payload)
        result.update({
            "execution_id": execution.result.execution_id,
            "strategy_requested": execution.result.strategy_requested,
            "strategy_selected": execution.result.strategy_selected,
            "selector_reason": execution.result.selector_reason,
            "placement_selected": execution.result.placement_selected,
            "fallback_reason": execution.result.fallback_reason,
        })
    except ValueError as e:
        console.print(f"[red bold]Pipeline failed:[/red bold] {e}")
        return None
    except Exception as e:
        # Some exceptions (httpx timeouts) stringify to "" — never print a blank error
        console.print(f"[red bold]Unexpected error:[/red bold] {str(e) or type(e).__name__}")
        return None

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

    # Surface code that still doesn't parse after the repair pass — never let a
    # PASS rating imply the code runs
    if result.get("code_problems"):
        console.print("\n[bold yellow]Warning — extracted code has problems:[/bold yellow]")
        for p in result["code_problems"]:
            console.print(f"  [yellow]{p}[/yellow]")

    elapsed = time.time() - start
    pid = result.get("project_id") or project_id
    if pid:
        console.print(f"\n[dim]Completed in {elapsed:.0f}s — saved to: {result['project_dir']}[/dim]")
        console.print(f"[dim]Continue this project: python cli.py --project {pid} \"your next task\"[/dim]")
    else:
        console.print(f"\n[dim]Completed in {elapsed:.0f}s — output saved to: {result['project_dir']}[/dim]")
    return result


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
            log = json.loads(log_file.read_text(encoding="utf-8"))
            task = log.get("task", "?")
            if len(task) > 60:
                task = task[:57] + "..."
            subtask_count = str(len(log.get("plan", [])))
            ts = log.get("timestamp", d.name)
            when = _relative_time(ts)

            # The run's final rating — the same rule the web surfaces use.
            # This used to substring-match the whole review file, so a review
            # whose prose happened to contain "PASS" reported a PASS, and a
            # run the reviser rescued still reported the reviewer's original
            # complaint. See orchestrator.ratings_for.
            from orchestrator import ratings_for
            review_file = d / "review.md"
            review_text = review_file.read_text(errors="ignore", encoding="utf-8") if review_file.exists() else ""
            final, _reviewer = ratings_for(log, review_text)
            rating = {
                "PASS": "[green]PASS[/green]",
                "NEEDS_WORK": "[yellow]NEEDS_WORK[/yellow]",
                "FAIL": "[red]FAIL[/red]",
            }.get(final, "?")
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


async def interactive(
    strategy: str = "auto",
    candidates: int | None = None,
    placement: str = "local",
):
    """Interactive mode — keep pitching tasks."""
    console.print(Panel(
        "[bold]Mycelium[/bold]\n"
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
            await run_task(
                task,
                strategy=strategy,
                candidates=candidates,
                placement=placement,
            )
            console.print()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Done.[/dim]")
            break


def _flag_value(flag: str, argv: list[str] | None = None) -> str | None:
    """Return the value after a --flag, or None."""
    values = sys.argv if argv is None else argv
    for i, a in enumerate(values):
        if a == flag and i + 1 < len(values):
            return values[i + 1]
    return None


def parse_execution_args(argv: list[str]):
    """Parse canonical strategy flags while leaving the legacy CLI modes intact."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--strategy", choices=("auto", "dag", "ensemble", "direct"), default="auto")
    parser.add_argument("--candidates", type=int, choices=range(1, 6))
    parser.add_argument("--placement", choices=("auto", "local", "distributed"), default="local")
    options, remaining = parser.parse_known_args(argv)
    if options.strategy == "dag" and options.candidates is not None:
        parser.error("--candidates cannot be used with --strategy dag")
    if options.strategy == "direct" and options.candidates not in (None, 1):
        parser.error("--strategy direct is ensemble with exactly one candidate")
    return options, remaining


# The showcase pitches live in showcase.py so that cli.py and the reliability
# harness that measures them can never drift apart. SHOWCASE_TASK is kept as an
# alias because the Snake wording is cited by docs/showcase-ceiling.md.
SHOWCASE_TASK = showcase.SNAKE.pitch


async def run_demo_showcase(candidate_id: str = showcase.DEFAULT_CANDIDATE, open_browser: bool = True):
    """Showcase demo — the swarm builds a visual artifact, then it opens in your browser."""
    import webbrowser

    try:
        cand = showcase.get(candidate_id)
    except KeyError as e:
        console.print(f"[red]{e}[/red]\n")
        console.print("[bold]Available showcases:[/bold]")
        console.print(showcase.describe())
        sys.exit(2)

    console.print(Panel(
        f"[bold]SHOWCASE[/bold] — the swarm builds {cand.title.lower()}, live.\n"
        f"[dim]{cand.blurb.capitalize()}. "
        + ("When the pipeline finishes, it opens in your default browser."
           if open_browser else "Browser launch disabled (--no-open).")
        + "[/dim]",
        border_style="magenta",
    ))
    console.print()

    result = await run_task(cand.pitch)
    if not result:
        console.print("[bold red]✗ Showcase aborted[/bold red] — pipeline failed (error above).")
        sys.exit(1)

    html_files = [f for f in result.get("code_files", []) if str(f).endswith(".html")]
    if not html_files:
        console.print(
            "\n[yellow]No HTML file was extracted from the output — check "
            f"{result['project_dir']} for what the swarm produced.[/yellow]"
        )
        return

    artifact = Path(html_files[0]).resolve()
    if open_browser:
        console.print(f"\n[bold green]Opening it:[/bold green] [cyan]{artifact}[/cyan]")
        webbrowser.open(artifact.as_uri())
    else:
        console.print(f"\n[bold green]Built:[/bold green] [cyan]{artifact}[/cyan]")


async def run_demo(fast: bool = False):
    """Demo mode — automated screen-recording script for the expense tracker showcase.

    Runs two pitches on the same project to demonstrate:
      1. The full planner→builder→reviewer pipeline
      2. Persistent project memory — the second pitch knows everything from the first

    Pass fast=True (--demo-fast) to skip the 3-second inter-pitch pause.
    """
    PITCH_1 = "Build a Python expense tracker with categories, date tracking, and a spending summary report"
    PITCH_2 = "Add a monthly budget feature that warns when spending exceeds the budget"
    PROJECT_NAME = "demo-expense-tracker"

    console.print(Panel(
        "[bold cyan]DEMO MODE[/bold cyan]\n\n"
        f"[bold]Pitch 1:[/bold] {PITCH_1}\n\n"
        f"[bold]Pitch 2:[/bold] {PITCH_2}\n\n"
        "[dim]The second pitch loads full context from the first run — the AI knows\n"
        "exactly what was already built and picks up without repeating work.[/dim]",
        title="[bold]Mycelium — Demo[/bold]",
        border_style="cyan",
    ))
    console.print()

    # ── Pitch 1: Build the expense tracker ──────────────────────────────
    project_id = create_project(PROJECT_NAME, PITCH_1)
    console.print(f"[dim]Project: {project_id}[/dim]\n")
    result_1 = await run_task(PITCH_1, project_id=project_id)
    if result_1 is None:
        console.print(Panel(
            "[bold red]✗ Demo aborted[/bold red] — pitch 1 failed (error above).",
            border_style="red",
        ))
        sys.exit(1)

    # Pause — gives the viewer time to read the output before the next pitch
    console.print()
    if not fast:
        console.print("[dim]── continuing in 3 seconds... ──[/dim]")
        await asyncio.sleep(3)
    console.print()

    # ── Pitch 2: Add budget feature ──────────────────────────────────────
    result_2 = await run_task(PITCH_2, project_id=project_id)
    if result_2 is None:
        console.print(Panel(
            "[bold red]✗ Demo aborted[/bold red] — pitch 2 failed (error above).",
            border_style="red",
        ))
        sys.exit(1)

    # ── Summary ─────────────────────────────────────────────────────────
    # Show memory growth — concrete proof the context was accumulated
    memory_lines = 0
    memory_bytes = 0
    memory_file = PROJECTS_DIR / project_id / "memory.md"
    if memory_file.exists():
        raw = memory_file.read_text(errors="ignore", encoding="utf-8")
        memory_lines = len([ln for ln in raw.splitlines() if ln.strip()])
        memory_bytes = len(raw.encode())

    console.print()
    console.print(Panel(
        f"[bold green]✓ Demo complete[/bold green]\n\n"
        f"  [bold]Project:[/bold]    {project_id}\n"
        f"  [bold]Iterations:[/bold] 2 (expense tracker → added monthly budgets)\n"
        f"  [bold]Memory:[/bold]     {memory_lines} lines / {memory_bytes / 1024:.1f} KB of context accumulated\n\n"
        f"[dim]The memory file now contains both iterations — every future pitch on\n"
        f"this project starts with that full context loaded automatically.[/dim]\n\n"
        f"  Continue: [cyan]python cli.py --project {project_id} \"your next task\"[/cyan]\n"
        f"  Share:    open [cyan]http://localhost:8000/dashboard[/cyan] → Gallery",
        title="[bold cyan]Project Memory in Action[/bold cyan]",
        border_style="green",
    ))


async def run_demo_live():
    """Live distributed demo — submits pitches to the local server so builder tasks
    route to connected nodes. Designed for the two-laptop screen recording.

    Both machines must be ready before running:
      Machine 1 (you):   python -m uvicorn server:app --host 0.0.0.0 --port 8000
                         python node.py --server http://localhost:8000 --node-id Laptop-1
      Machine 2 (friend): python join.py  (auto-discovers you on the LAN)

    Then on Machine 1:   python cli.py --demo-live
    """
    PITCH_1 = "Build a Python expense tracker with CSV import and monthly budgets"
    PITCH_2 = "Add monthly budget warnings and dark mode to the CLI"
    PROJECT_NAME = "demo-expense-tracker-live"

    # ── Pre-flight: check server ─────────────────────────────────────────
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            health = (await client.get(f"{SERVER_URL}/health")).json()
        except Exception:
            console.print(Panel(
                "[red bold]Server not running.[/red bold]\n\n"
                "Start it first:\n"
                "  [cyan]python -m uvicorn server:app --host 0.0.0.0 --port 8000[/cyan]",
                border_style="red",
            ))
            return

        node_count = _health_node_count(health)
        nodes = []
        if node_count and VIEWER_KEY:
            detail = await client.get(
                f"{SERVER_URL}/nodes",
                headers=_server_headers(viewer=True),
            )
            if detail.status_code == 200:
                nodes = detail.json().get("nodes", [])

    # ── Node list panel ──────────────────────────────────────────────────
    if node_count == 0:
        node_status = (
            "[yellow]⚠  No worker nodes connected — tasks will run locally as fallback.[/yellow]\n"
            "[dim]To add a node: python node.py --server http://localhost:8000 --node-id Laptop-1[/dim]"
        )
    elif nodes:
        node_lines = []
        for n in nodes:
            caps = [c for c in n.get("capabilities", []) if not c.startswith("model:")]
            cpu = n.get("cpu_model") or "CPU"
            ram = n.get("ram_gb")
            hw = f"{cpu}" + (f" · {ram}GB RAM" if ram else "")
            caps_str = ", ".join(caps) if caps else "builder"
            node_lines.append(f"  [green]●[/green] [bold]{n['node_id']}[/bold]  {hw}  [{caps_str}]")
        node_status = f"[green]{node_count} node(s) connected:[/green]\n" + "\n".join(node_lines)
    else:
        node_status = (
            f"[green]{node_count} node(s) connected.[/green]\n"
            "[dim]Set VIEWER_KEY to display private node hardware details.[/dim]"
        )

    console.print(Panel(
        "[bold cyan]LIVE DISTRIBUTED DEMO[/bold cyan]\n\n"
        f"[bold]Pitch 1:[/bold] {PITCH_1}\n\n"
        f"[bold]Pitch 2:[/bold] {PITCH_2}\n\n"
        f"{node_status}\n\n"
        "[dim]Builder tasks will distribute across all connected nodes.\n"
        "Each node earns credits. Watch the Guild Standings at the end.[/dim]",
        title="[bold]Mycelium — Live Demo[/bold]",
        border_style="cyan",
    ))
    console.print()

    # ── Create project locally (for memory.md path tracking) ────────────
    project_id = create_project(PROJECT_NAME, PITCH_1)
    console.print(f"[dim]Project: {project_id}[/dim]\n")

    # ── Helper: submit + watch a pitch ──────────────────────────────────
    async def run_live_pitch(task: str, pitch_num: int):
        console.print(Panel(
            task,
            title=f"[bold cyan]Pitch {pitch_num}[/bold cyan]",
            border_style="cyan",
        ))
        console.print()

        # Submit to server
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{SERVER_URL}/pitch/async",
                json={"task": task, "project_id": project_id},
                headers=_server_headers(pitch=True),
            )
            resp.raise_for_status()
            data = resp.json()
            job_id = data["job_id"]

        console.print(f"[dim]Job {job_id} queued — watching events...[/dim]\n")

        # Poll events + job status until complete
        last_event_id = 0
        seen_subtasks: set = set()
        console.print("[bold yellow]PLANNER[/bold yellow] Decomposing task into subtasks...\n")

        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                # Fetch new events
                try:
                    ev_resp = await client.get(
                        f"{SERVER_URL}/events",
                        params={"since": last_event_id},
                        headers=_server_headers(viewer=True),
                    )
                    if ev_resp.status_code == 200:
                        ev_data = ev_resp.json()
                        for ev in ev_data.get("events", []):
                            eid = ev.get("id", 0)
                            if eid > last_event_id:
                                last_event_id = eid
                            ev_job = ev.get("job_id", "")
                            if ev_job and ev_job != job_id:
                                continue  # skip events from other jobs
                            _render_event(ev, seen_subtasks)
                except Exception:
                    pass  # network blip — keep polling

                # Check job status
                try:
                    job_resp = await client.get(
                        f"{SERVER_URL}/jobs/{job_id}",
                        headers=_server_headers(viewer=True),
                    )
                    job = job_resp.json()
                    status = job.get("status", "queued")
                    if status in ("complete", "failed"):
                        if status == "failed":
                            console.print(f"\n[red bold]Job failed:[/red bold] {job.get('error', '?')}")
                        break
                except Exception:
                    pass

                await asyncio.sleep(2)

            # Final fetch — catch any trailing events
            try:
                ev_resp = await client.get(
                    f"{SERVER_URL}/events",
                    params={"since": last_event_id},
                    headers=_server_headers(viewer=True),
                )
                if ev_resp.status_code == 200:
                    for ev in ev_resp.json().get("events", []):
                        ev_job = ev.get("job_id", "")
                        if ev_job and ev_job != job_id:
                            continue
                        _render_event(ev, seen_subtasks)
            except Exception:
                pass

        # Show rating from completed job
        if status == "complete":
            rating = job.get("rating", "?")
            color = {"PASS": "green", "NEEDS_WORK": "yellow", "FAIL": "red"}.get(rating, "white")
            console.print(f"\n[{color} bold]REVIEWER  ──  {rating}[/{color} bold]")
            if job.get("project_dir"):
                console.print(f"[dim]Output: {job['project_dir']}[/dim]")

        console.print()

    # ── Pitch 1 ──────────────────────────────────────────────────────────
    await run_live_pitch(PITCH_1, 1)

    # Pause for viewer
    console.print("[dim]── continuing in 3 seconds — watch: the next pitch loads memory from Pitch 1 ──[/dim]")
    await asyncio.sleep(3)
    console.print()

    # ── Pitch 2 ──────────────────────────────────────────────────────────
    memory_file = PROJECTS_DIR / project_id / "memory.md"
    if memory_file.exists():
        raw = memory_file.read_text(errors="ignore", encoding="utf-8")
        lines = len([ln for ln in raw.splitlines() if ln.strip()])
        console.print(f"[dim]Loading project memory: {lines} lines of context from Pitch 1[/dim]\n")

    await run_live_pitch(PITCH_2, 2)

    # ── Summary panel ────────────────────────────────────────────────────
    memory_lines = 0
    memory_bytes = 0
    if memory_file.exists():
        raw = memory_file.read_text(errors="ignore", encoding="utf-8")
        memory_lines = len([ln for ln in raw.splitlines() if ln.strip()])
        memory_bytes = len(raw.encode())

    console.print()
    console.print(Panel(
        f"[bold green]✓ Live demo complete[/bold green]\n\n"
        f"  [bold]Project:[/bold]    {project_id}\n"
        f"  [bold]Iterations:[/bold] 2  (expense tracker → budget warnings + dark mode)\n"
        f"  [bold]Memory:[/bold]     {memory_lines} lines / {memory_bytes / 1024:.1f} KB accumulated\n"
        f"  [bold]Nodes:[/bold]      {node_count} worker(s) participated\n\n"
        f"[dim]Open the dashboard to see the Guild Standings and share your project:[/dim]\n"
        f"  [cyan]http://localhost:8000/dashboard[/cyan]",
        title="[bold cyan]Project Memory in Action[/bold cyan]",
        border_style="green",
    ))

    # ── Guild standings ──────────────────────────────────────────────────
    console.print()
    show_standings()


def _render_event(ev: dict, seen_subtasks: set):
    """Print a single server event in a readable format for the live demo terminal."""
    etype = ev.get("type", "")
    # The server event contract is flat: event metadata and event-specific
    # fields are peers. Keeping a second nested shape made the live CLI render
    # empty plans even though the wire payload was correct.
    data = ev

    if etype == "plan":
        subtasks = data.get("subtasks", [])
        console.print("[bold yellow]PLAN[/bold yellow]")
        for i, s in enumerate(subtasks, 1):
            console.print(f"  [{i}] {s}")
        console.print()

    elif etype == "build":
        sid = data.get("subtask_id", data.get("subtask", "?"))
        title = data.get("subtask", sid)
        node = data.get("node_id", "local")
        if sid not in seen_subtasks:
            seen_subtasks.add(sid)
            console.print(f"[bold green]BUILDER {sid}[/bold green]  {title}  [dim]→ {node}[/dim]")

    elif etype == "node_task_queued":
        console.print(f"[dim]  ↳ queued: {data.get('subtask', '?')}[/dim]")

    elif etype == "review_start":
        console.print("\n[bold magenta]REVIEWER[/bold magenta] Assembling and rating output...\n")

    elif etype == "revise":
        console.print(f"[bold yellow]REVISER[/bold yellow] Auto-fixing: {data.get('issues', ['issues'])}")

    elif etype == "error":
        console.print(f"[red]ERROR:[/red] {data.get('message', '?')}")


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
        f"[bold]Run it:[/bold]\n  python cli.py --project {project_id} \"{task}\"",
        title="[bold cyan]Fork Imported[/bold cyan]",
        border_style="cyan",
    ))


async def main():
    execution_options, argv = parse_execution_args(sys.argv[1:])

    async def execute_task(task: str, project_id: str | None = None):
        return await run_task(
            task,
            project_id=project_id,
            strategy=execution_options.strategy,
            candidates=execution_options.candidates,
            placement=execution_options.placement,
        )

    # ── Flags that don't need Ollama ──
    # These read local files only, so they must work with Ollama stopped —
    # checking first would make `--history` fail on a machine that has never
    # installed a model.
    import_zip = _flag_value("--import", argv)
    if import_zip:
        import_fork(import_zip)
        return

    if "--history" in argv:
        show_history()
        return
    if "--standings" in argv:
        show_standings()
        return
    if "--projects" in argv:
        show_projects()
        return

    # Pre-flight: everything below this point runs inference
    status = await check_ollama()
    if not status["ok"]:
        console.print(f"[red bold]ERROR:[/red bold] {status['error']}")
        sys.exit(1)

    # ── Create a new named project ──
    # Usage: python cli.py --new-project "name" "initial task"
    if "--new-project" in argv:
        idx = argv.index("--new-project")
        remaining = argv[idx + 1:]
        if len(remaining) < 2:
            console.print("[red]Usage:[/red] python cli.py --new-project \"project name\" \"initial task\"")
            return
        proj_name = remaining[0]
        task = " ".join(remaining[1:])
        project_id = create_project(proj_name, task)
        console.print(f"[green]Project created:[/green] [cyan]{project_id}[/cyan]")
        await execute_task(task, project_id=project_id)
        return

    # ── Continue an existing project ──
    # Usage: python cli.py --project my-app "next task"
    project_id = _flag_value("--project", argv)
    if project_id:
        args = [a for a in argv if not a.startswith("--") and a != project_id]
        if not args:
            # No task given — show project info and prompt
            try:
                meta = load_project(project_id)
                console.print(f"[bold]{meta['name']}[/bold] · {meta['iteration_count']} iteration(s)")
                console.print(f"[dim]Goal: {meta['initial_task']}[/dim]\n")
                task = console.input("[bold cyan]What's next?>[/bold cyan] ").strip()
                if task:
                    await execute_task(task, project_id=project_id)
            except FileNotFoundError:
                console.print(f"[red]Project '{project_id}' not found.[/red]")
            return
        await execute_task(" ".join(args), project_id=project_id)
        return

    # ── Demo mode ──────────────────────────────────────────────────────
    if "--demo-showcase" in argv:
        # Optional candidate id right after the flag: --demo-showcase clock
        idx = argv.index("--demo-showcase")
        rest = [a for a in argv[idx + 1:] if not a.startswith("--")]
        await run_demo_showcase(
            rest[0] if rest else showcase.DEFAULT_CANDIDATE,
            open_browser="--no-open" not in argv,
        )
        return
    if "--demo-fast" in argv:
        await run_demo(fast=True)
        return
    if "--demo-live" in argv:
        await run_demo_live()
        return
    if "--demo" in argv:
        await run_demo()
        return

    # ── Direct task or interactive mode ──
    args = [a for a in argv if not a.startswith("--")]
    if args:
        await execute_task(" ".join(args))
    else:
        await interactive(
            strategy=execution_options.strategy,
            candidates=execution_options.candidates,
            placement=execution_options.placement,
        )


if __name__ == "__main__":
    asyncio.run(main())
