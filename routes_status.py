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


# Outcome vocabulary for the public page. The reviewer's rating is a judgement
# about the run; it is not an assurance level and is never rendered as one.
_OUTCOME = {"PASS": ("passed", "is-ok"), "NEEDS_WORK": ("partial", "is-warn"),
            "FAIL": ("failed", "is-down")}


def _assurance(log: dict) -> str:
    """Which class of evidence actually ran, for a legacy run directory.

    Only two of the five labels are reachable from a `full_log.json`, because
    only two kinds of evidence were ever recorded in one: the extractor wrote
    files and the parse precheck reached a verdict about them, or it did not.
    Contract validation, behaviour testing and model review are not recorded
    here, so they are not claimed here.

    A precheck that never reached a verdict is `not checked` and not `passed
    with no problems` — an empty problem list beside a precheck error means
    nothing was learned about the code, which is the distinction PR #73 exists
    to preserve.
    """
    if log.get("code_precheck_error"):
        return "not checked"
    return "structure checked" if log.get("code_files") else "not checked"


def _recent_runs(limit: int = 8) -> list[dict]:
    """Outcome, assurance, placement and age — deliberately nothing else.

    This feeds the one page that answers a stranger. What was asked for is
    somebody's writing about work they wanted done, and the run's own page is
    reachable only through a share capability its owner created on purpose, so
    neither the task text nor a run link is assembled here at all. They are not
    fetched and then hidden; the values never enter the template.
    """
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
        outcome, cls = _OUTCOME.get(str(log.get("rating", "")), ("unknown", "is-none"))
        runs.append({
            "outcome": outcome,
            "outcome_class": cls,
            "assurance": _assurance(log),
            "placement": "distributed" if log.get("mode") == "distributed" else "local",
            "when": _ago(log.get("timestamp", d.name)),
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
        summary = ("Work pitched now is dispatched to the machines this orchestrator has "
                   "invited. This page carries counts only: it is the one page here that "
                   "answers without a sign-in, so it says how much has happened and not "
                   "what any of it was.")
    else:
        lamp, headline = "is-warn", "Online, no machines connected"
        summary = ("The orchestrator is up and inference works, but no invited machines "
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

    # Machines are a count and nothing else. Which machines they are, what
    # hardware they run and what each has earned is what /nodes and /node/{id}
    # are viewer-gated for; naming that as private is more honest than an
    # unexplained absence.
    if online:
        count = len(online)
        word = "machine is" if count == 1 else "machines are"
        nodes_html = (
            f'<div class="empty"><b>{count} {word} connected.</b> '
            "Which machines they are, what hardware they run, what each one is building and "
            "what it has earned are private — that is the console's Nodes view, and it needs "
            "an operator sign-in.</div>"
        )
    else:
        nodes_html = (
            '<div class="empty"><b>No machines are connected right now.</b> '
            "That is a real state of a small network, not a fault — the orchestrator still "
            "runs work on itself. Joining takes one command on any machine with 8&nbsp;GB of RAM, "
            "and an invitation — an enrolment token from whoever runs this orchestrator:"
            f'<div class="cmd"><span class="p" aria-hidden="true">$</span>'
            f'python join.py {esc(str(request.base_url).rstrip("/"))}</div></div>'
        )

    recent = _recent_runs()
    if recent:
        rows = "".join(
            f'<tr><td><span class="n {esc(r["outcome_class"])}">{esc(r["outcome"])}</span></td>'
            f'<td class="mono">{esc(r["assurance"])}</td>'
            f'<td class="mono">{esc(r["placement"])}</td>'
            f'<td class="num mono">{esc(r["when"])}</td></tr>'
            for r in recent
        )
        recent_html = (
            '<div class="table-scroll"><table>'
            '<thead><tr><th>Outcome</th><th>Assurance</th><th>Placement</th>'
            '<th class="num">When</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
            '<p class="note">What was asked for is not listed. A task description is '
            "somebody's writing about work they wanted done, and a run page is reachable "
            "only through a link its owner deliberately created.</p>"
        )
    else:
        recent_html = ('<div class="empty"><b>Nothing built yet.</b> '
                       "Completed runs appear here as an outcome and an age.</div>")

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
        NODES_LEDE="How many computers are offering compute right now.",
        NODES=nodes_html,
        RECENT_LEDE=("The last things this network finished. Failures are counted here too."),
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
        _fig(f"{credits:g}", "points earned", "is-live" if credits else "is-none"),
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
            '<thead><tr><th>Work</th><th>Type</th><th class="num">Points</th>'
            '<th class="num">When (UTC)</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
            # The footnote travels with the column. A page whose whole subject
            # is what one machine earned is the last place to drop the sentence
            # saying what earning does not mean.
            '<p class="note">Points mean a nonempty, attempt-bound worker result was '
            "accepted. They do not mean the candidate was selected, that validation passed, "
            "or that the output is correct — and they are not money, a token, or a claim on "
            "future value.</p>"
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
                             f"and earned {credits:g} contribution points."),
        LAMP_CLASS=lamp,
        HEADLINE=esc(headline),
        UPTIME=esc(f"{builds} entries on the ledger"),
        SUMMARY=esc(summary),
        FIGURES=figures,
        NODES_LEDE="What this machine reports about itself while it is connected.",
        NODES=nodes_html,
        RECENT_LEDE="What this machine has been handed, and what it earned for it.",
        RECENT=recent_html,
        BUILD='<div class="cmd"><span class="p" aria-hidden="true">#</span>'
              f'{esc(BUILD)}</div>' if BUILD else "",
    )
