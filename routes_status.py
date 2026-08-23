"""
Human-readable status — /status, and one machine's own page at /node/{id}.

/status.json has existed for a while and nothing but a machine could read it.
This is the same facts laid out for a person: is the orchestrator up, is
inference working, who is connected, what has it built lately, and which
build of the source is actually running.

/node/{id} exists so an operator who joined a machine can bookmark it and see
what it has earned, rather than hunting for it in a modal on the dashboard.
"""

import json
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import server_state as state
from build_info import BUILD
from dashboard import render
from execution.publication import (
    LegacyRunNotPublished,
    require_legacy_run_publication,
)
from ledger import get_history, get_standings
from ollama_client import OLLAMA_URL
from routes_run import esc
from server_state import OUTPUT_DIR, nodes, task_queue

router = APIRouter()


def _uptime(seconds: int) -> str:
    if seconds >= 86400:
        return f"up {seconds // 86400}d {(seconds % 86400) // 3600}h"
    if seconds >= 3600:
        return f"up {seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"up {max(1, seconds // 60)}m"


def _ago(timestamp: str) -> str:
    try:
        dt = datetime.strptime(timestamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return timestamp
    delta = int(datetime.now(timezone.utc).timestamp() - dt.timestamp())
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


async def _inference() -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            tags = resp.json().get("models", [])
        return True, (tags[0]["name"] if tags else "")
    except Exception:
        return False, ""


def _recent_runs(limit: int = 8) -> list[dict]:
    runs = []
    if not OUTPUT_DIR.exists():
        return runs
    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if not d.is_dir() or not (d / "full_log.json").exists():
            continue
        try:
            log = json.loads((d / "full_log.json").read_text(encoding="utf-8"))
            require_legacy_run_publication(d, log)
        except (json.JSONDecodeError, LegacyRunNotPublished, OSError):
            continue
        runs.append({
            "timestamp": log.get("timestamp", d.name),
            "task": log.get("task", "Unknown"),
            "rating": log.get("rating", "?"),
            "mode": log.get("mode", "local"),
        })
        if len(runs) >= limit:
            break
    return runs


def _built_since(hours: int | None = None) -> int:
    """Completed runs, optionally only those in the last `hours`.

    Counted from the run directories rather than the ledger: get_history()
    takes a `limit` (50 by default), so a ledger-derived count silently stops
    growing — which is how /status.json has been reporting a capped figure.
    """
    if not OUTPUT_DIR.exists():
        return 0
    cutoff = time.time() - hours * 3600 if hours else None
    n = 0
    for d in OUTPUT_DIR.iterdir():
        if not d.is_dir() or not (d / "full_log.json").exists():
            continue
        try:
            log = json.loads((d / "full_log.json").read_text(encoding="utf-8"))
            require_legacy_run_publication(d, log)
            if cutoff is None or datetime.strptime(
                d.name[:15],
                "%Y%m%d_%H%M%S",
            ).replace(tzinfo=timezone.utc).timestamp() >= cutoff:
                n += 1
        except (
            json.JSONDecodeError,
            LegacyRunNotPublished,
            OSError,
            ValueError,
        ):
            continue
    return n


def _fig(value, label: str, cls: str = "") -> str:
    return (f'<div class="fig"><span class="n {cls}">{esc(value)}</span>'
            f'<span class="k">{esc(label)}</span></div>')


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    """What a stranger checks to see whether this network is alive."""
    online = list(nodes.values())
    standings = get_standings()
    uptime = max(0, int(time.time() - state.STARTED_AT))
    inference_ok, model = await _inference()
    day, week, total = _built_since(24), _built_since(24 * 7), _built_since()

    if not inference_ok:
        lamp, headline = "is-down", "Inference is offline"
        summary = ("The orchestrator is answering, but the local model server is not "
                   "reachable, so nothing can be built right now.")
    elif online:
        lamp, headline = "is-ok", f"Live — {len(online)} machine{'' if len(online) == 1 else 's'} connected"
        summary = ("Work pitched now gets split up and handed to the machines below. "
                   "Every completed run gets a page of its own.")
    else:
        lamp, headline = "is-warn", "Online, no machines connected"
        summary = ("The orchestrator is up and inference works, but no volunteer machines "
                   "are offering compute at the moment. This network is small on purpose — "
                   "testers are added a few at a time.")

    figures = "".join([
        _fig(len(online), "machines joined", "is-live" if online else "is-none"),
        _fig(total, "tasks built"),
        _fig(day, "built today", "is-none" if day == 0 else ""),
        _fig(week, "built this week", "is-none" if week == 0 else ""),
        _fig(len(task_queue), "queued now", "is-none" if not task_queue else ""),
        _fig(model.split(":")[0] if model else "none", "model"),
        _fig(len(standings), "contributors"),
        _fig(sum(c["compute_tasks"] for c in standings), "subtasks executed"),
    ])

    if online:
        rows = "".join(
            f'<tr><td class="mono"><a href="/node/{esc(n.get("node_id"))}">{esc(n.get("node_id"))}</a></td>'
            f'<td class="mono">{esc(n.get("model", "—"))}</td>'
            f'<td>{esc(n.get("platform", "—"))}</td>'
            f'<td class="num mono">{esc(n.get("tasks_completed", 0))}</td>'
            f'<td class="num mono">{esc(n.get("credits_earned", 0))}</td></tr>'
            for n in online
        )
        nodes_html = (
            '<div class="table-scroll"><table>'
            '<thead><tr><th>Machine</th><th>Model</th><th>Platform</th>'
            '<th class="num">Tasks</th><th class="num">Credits</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
        )
    else:
        nodes_html = (
            '<div class="empty"><b>No machines are connected right now.</b> '
            "That is a real state of a small network, not a fault — the orchestrator still "
            "runs work on itself. Joining takes one command on any machine with 8&nbsp;GB of RAM:"
            f'<div class="cmd"><span class="p" aria-hidden="true">$</span>'
            f'python join.py {esc(str(request.base_url).rstrip("/"))}</div></div>'
        )

    recent = _recent_runs()
    if recent:
        rows = "".join(
            f'<tr><td><a href="/run/{esc(r["timestamp"])}">{esc(r["task"][:90])}</a></td>'
            f'<td class="mono">{esc(r["rating"])}</td>'
            f'<td class="mono">{esc(r["mode"])}</td>'
            f'<td class="num mono">{esc(_ago(r["timestamp"]))}</td></tr>'
            for r in recent
        )
        recent_html = (
            '<div class="table-scroll"><table>'
            '<thead><tr><th>Task</th><th>Rating</th><th>Mode</th><th class="num">When</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
        )
    else:
        recent_html = ('<div class="empty"><b>Nothing built yet.</b> '
                       "Completed runs appear here, each with its own page.</div>")

    build_html = (
        f'<div class="cmd"><span class="p" aria-hidden="true">#</span>{esc(BUILD)}</div>'
        if BUILD else
        '<div class="empty"><b>No build fingerprint.</b> This process predates build stamping, '
        "so a deploy that silently did nothing would not be visible here.</div>"
    )

    return render(
        "status.html",
        META_DESCRIPTION=esc(
            f"{headline}. {total} tasks built, {len(standings)} contributors, "
            f"{_uptime(uptime)}."
        ),
        LAMP_CLASS=lamp,
        HEADLINE=esc(headline),
        UPTIME=esc(_uptime(uptime)),
        SUMMARY=esc(summary),
        FIGURES=figures,
        NODES=nodes_html,
        RECENT=recent_html,
        BUILD=build_html,
    )


@router.get("/node/{node_id}", response_class=HTMLResponse)
async def node_page(node_id: str, request: Request):
    """One machine's own page, so its operator can bookmark it.

    A node that has disconnected still gets a page: its ledger entries are
    permanent even when the machine is not currently offering compute, and
    "my laptop earned nothing and nobody can say why" is a question this
    project has had to answer before.
    """
    node = nodes.get(node_id)
    entries = [e for e in get_history(node_id, limit=200)]
    if node is None and not entries:
        raise HTTPException(status_code=404, detail="No machine by that name has ever connected")

    credits = round(sum(float(e.get("credits", 0) or 0) for e in entries), 1)
    builds = sum(1 for e in entries if e.get("type") == "compute")

    if node:
        lamp, headline = "is-ok", f"{node_id} is connected"
        summary = ("This machine is currently offering compute to the network. "
                   "It is handed a subtask when one is ready and its dependencies are met.")
    else:
        lamp, headline = "is-warn", f"{node_id} is not connected"
        summary = ("This machine is not currently offering compute. Everything it earned "
                   "is below and stays on the ledger — disconnecting does not undo it.")

    # A connected machine can report its hardware; a disconnected one cannot,
    # and two dashes where the figures should be reads as a broken page. Show
    # what is actually knowable in each case.
    figures = [
        _fig(builds, "subtasks built", "is-live" if builds else "is-none"),
        _fig(f"{credits:g}", "credits earned", "is-live" if credits else "is-none"),
    ]
    if node:
        figures += [_fig(node.get("model") or "—", "model"),
                    _fig(node.get("ram_gb") or "—", "GB RAM")]
    else:
        stamps = [e.get("timestamp", 0) for e in entries if e.get("timestamp")]
        fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%d %b")  # noqa: E731
        figures += [_fig(fmt(min(stamps)) if stamps else "—", "first seen"),
                    _fig(fmt(max(stamps)) if stamps else "—", "last seen")]
    figures = "".join(figures)

    recent = entries[-12:][::-1]
    if recent:
        rows = "".join(
            f'<tr><td>{esc(e.get("task", "")[:90])}</td>'
            f'<td class="mono">{esc(e.get("type", ""))}</td>'
            f'<td class="num mono">+{esc(e.get("credits", 0))}</td>'
            f'<td class="num mono">'
            f'{esc(datetime.fromtimestamp(e.get("timestamp", 0), timezone.utc).strftime("%Y-%m-%d %H:%M"))}'
            "</td></tr>"
            for e in recent
        )
        recent_html = (
            '<div class="table-scroll"><table>'
            '<thead><tr><th>Work</th><th>Type</th><th class="num">Credits</th>'
            '<th class="num">When (UTC)</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
        )
    else:
        recent_html = ('<div class="empty"><b>Nothing recorded yet.</b> '
                       "This machine has registered but has not been handed work.</div>")

    if node:
        detail = [
            ("Platform", f'{node.get("platform", "—")} / {node.get("machine", "—")}'),
            ("Hostname", node.get("hostname")),
            ("CPU", f'{node.get("cpu_count")} cores' if node.get("cpu_count") else None),
            ("GPU", node.get("gpu") or "none — CPU inference"),
            ("Joined", (node.get("registered_at") or "")[:19].replace("T", " ") + " UTC"),
            ("Doing now", node.get("current_task") or "idle"),
        ]
        rows = "".join(
            f'<tr><td>{esc(k)}</td><td class="mono">{esc(v)}</td></tr>'
            for k, v in detail if v
        )
        nodes_html = f'<div class="table-scroll"><table><tbody>{rows}</tbody></table></div>'
    else:
        nodes_html = (
            '<div class="empty"><b>This machine is offline.</b> '
            "Hardware details are reported at registration, so they are only shown while it is "
            "connected. Rejoin with:"
            f'<div class="cmd"><span class="p" aria-hidden="true">$</span>'
            f'python join.py {esc(str(request.base_url).rstrip("/"))}</div></div>'
        )

    return render(
        "status.html",
        META_DESCRIPTION=esc(f"{node_id} has built {builds} subtasks for the Mycelium network "
                             f"and earned {credits:g} credits."),
        LAMP_CLASS=lamp,
        HEADLINE=esc(headline),
        UPTIME=esc(f"{builds} entries on the ledger"),
        SUMMARY=esc(summary),
        FIGURES=figures,
        NODES=nodes_html,
        RECENT=recent_html,
        BUILD='<div class="cmd"><span class="p" aria-hidden="true">#</span>'
              f'{esc(BUILD)}</div>' if BUILD else "",
    )
