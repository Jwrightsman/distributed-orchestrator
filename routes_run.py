"""
The run permalink — /run/{id}.

A completed run used to be a card in a gallery grid with no URL of its own,
so there was nowhere to point when someone said "show me something the swarm
built". This is that page: one run, one address, server-rendered so it
previews correctly when pasted into Discord, Reddit or a comment.

It answers, in order: what was asked for, how the planner split it, which
machine built each piece and how long that took, what the reviewer said, what
the reviser changed, what files came out, and what the ledger settled.

Where a run predates a field, the page says so. The alternative — deriving a
plausible number — is how this project once published a figure that was true
when recorded and had quietly stopped being reproducible.
"""

import html as _html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard import render
from execution.publication import (
    LegacyRunNotPublished,
    published_file,
    require_legacy_run_publication,
)
from server_state import OUTPUT_DIR, jobs

router = APIRouter()

_RATING_CLASS = {"PASS": "is-pass", "NEEDS_WORK": "is-needs-work", "FAIL": "is-fail"}

# A run directory is a timestamp; a job id is job_<uuid4 hex>. The page accepts
# either, because "the id of this run" means different things depending on
# whether you came from the gallery or from an async pitch response.
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def esc(value) -> str:
    """Escape for HTML text and for a double-quoted attribute."""
    return _html.escape(str(value if value is not None else ""), quote=True)


# ── Loading ──────────────────────────────────────────────────────────

def _resolve(run_id: str) -> str:
    """Map a job id onto its run directory; pass a run directory through."""
    if run_id.startswith("job_"):
        job = jobs.get(run_id)
        project_dir = (job or {}).get("project_dir") or ""
        if not project_dir:
            raise HTTPException(status_code=404, detail="Run not found")
        return Path(project_dir).name
    return run_id


def load_run(run_id: str) -> dict:
    """Read one run off disk, tolerating logs written before newer fields."""
    if not _RUN_ID.match(run_id):
        raise HTTPException(status_code=404, detail="Run not found")

    run_dir = OUTPUT_DIR / _resolve(run_id)
    # Defence in depth: the pattern above already excludes separators, but a
    # path that escapes the output directory must never be readable.
    if run_dir.resolve().parent != OUTPUT_DIR.resolve() or not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="Run not found")

    log_file = run_dir / "full_log.json"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        log = json.loads(log_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Corrupt run log")

    try:
        publication = require_legacy_run_publication(run_dir, log)
        review = published_file(publication, "review.md")
        log["review"] = (
            review.read_text(errors="ignore", encoding="utf-8")
            if review
            else log.get("review", "")
        )
        output = published_file(publication, "output.md")
        log["final_output"] = (
            output.read_text(errors="ignore", encoding="utf-8")
            if output
            else ""
        )
    except (LegacyRunNotPublished, OSError) as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc

    # The page shows the final rating at the top and the reviewer's own verdict
    # in the review section — they are not the same thing when the reviser
    # fires. See orchestrator.ratings_for.
    from orchestrator import ratings_for
    log["rating"], log["reviewer_rating"] = ratings_for(log, log["review"])

    log["run_dir_name"] = run_dir.name
    return log


def _relative(timestamp: str) -> str:
    try:
        dt = datetime.strptime(timestamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    delta = int(datetime.now(timezone.utc).timestamp() - dt.timestamp())
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _duration(seconds) -> str:
    if not seconds:
        return ""
    seconds = int(round(float(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


# ── Fragments ────────────────────────────────────────────────────────

def _unrecorded(what: str, why: str) -> str:
    return f'<div class="unrecorded"><b>{esc(what)}</b> {esc(why)}</div>'


def _facts(log: dict) -> str:
    rating = log.get("rating") or "?"
    parts = [
        f'<span class="badge {_RATING_CLASS.get(rating, "is-unknown")}">{esc(rating)}</span>',
        '<span class="badge is-dist">DISTRIBUTED</span>' if log.get("mode") == "distributed"
        else '<span class="badge is-local">LOCAL</span>',
        f'<span class="fact"><b>{len(log.get("plan", []))}</b> subtasks</span>',
    ]
    nodes_used = log.get("nodes_used")
    if isinstance(nodes_used, list) and nodes_used:
        parts.append(f'<span class="fact"><b>{len(nodes_used)}</b> machines</span>')
    if log.get("duration_seconds"):
        parts.append(f'<span class="fact"><b>{esc(_duration(log["duration_seconds"]))}</b> end to end</span>')
    if log.get("model"):
        parts.append(f'<span class="fact">{esc(log["model"])}</span>')
    rel = _relative(log.get("timestamp", ""))
    if rel:
        parts.append(f'<span class="fact">{esc(rel)}</span>')
    return "".join(parts)


def _plan(log: dict) -> str:
    plan = log.get("plan") or []
    if not plan:
        return _unrecorded("No plan was recorded.", "The planner did not produce a decomposition for this run.")
    rows = []
    for st in plan:
        deps = st.get("depends_on") or []
        dep_html = (f'<div class="deps">waits for subtask {esc(", ".join(str(d) for d in deps))}</div>'
                    if deps else "")
        desc = st.get("description") or ""
        rows.append(
            f'<div class="step"><span class="n">{esc(st.get("id", "?"))}</span>'
            f'<div class="body"><div class="title">{esc(st.get("title", "Untitled"))}</div>'
            f'{f"<div class=\"desc\">{esc(desc)}</div>" if desc else ""}{dep_html}</div></div>'
        )
    return "".join(rows)


def _execution(log: dict) -> str:
    plan = log.get("plan") or []
    stats = log.get("subtask_stats") or {}
    if not plan:
        return _unrecorded("Nothing to show.", "This run has no recorded subtasks.")
    if not stats:
        where = ("across the machines listed above" if log.get("nodes_used")
                 else "on the orchestrator itself")
        return _unrecorded(
            "Per-subtask timing was not recorded for this run.",
            f"It ran before the pipeline started keeping it. The work happened {where}; "
            "how long each piece took was not written down. Runs from here on record it.",
        )

    rows = []
    for st in plan:
        meta = stats.get(str(st.get("id"))) or {}
        executor = meta.get("executor") or "—"
        note = ' <span class="self">(fell back to the orchestrator)</span>' if meta.get("fell_back_to_local") else ""
        seconds = meta.get("seconds")
        chars = meta.get("chars")
        rows.append(
            f"<tr><td class=\"mono\">{esc(st.get('id', '?'))}</td>"
            f"<td>{esc(st.get('title', 'Untitled'))}</td>"
            f"<td class=\"who mono\">{esc(executor)}{note}</td>"
            f"<td class=\"num mono\">{esc(_duration(seconds)) if seconds else '—'}</td>"
            f"<td class=\"num mono\">{esc(f'{chars:,}') if chars else '—'}</td></tr>"
        )
    review_row = ""
    if log.get("review_seconds"):
        review_row = (
            '<tr><td class="mono">—</td><td>Review and assembly</td>'
            f'<td class="who mono">orchestrator</td>'
            f'<td class="num mono">{esc(_duration(log["review_seconds"]))}</td>'
            '<td class="num mono">—</td></tr>'
        )
    return (
        '<div class="table-scroll"><table>'
        "<thead><tr><th>#</th><th>Subtask</th><th>Machine</th>"
        '<th class="num">Time</th><th class="num">Output</th></tr></thead>'
        f"<tbody>{''.join(rows)}{review_row}</tbody></table></div>"
    )


def _review(log: dict) -> str:
    # The reviewer's own verdict, before any revision pass — see load_run.
    rating = log.get("reviewer_rating") or "?"
    said = {
        "PASS": "The reviewer accepted the assembled result.",
        "NEEDS_WORK": "The reviewer accepted the result but raised issues with it.",
        "FAIL": "The reviewer rejected the assembled result.",
    }.get(rating, "The reviewer did not return a rating this page can read.")

    block = (
        f'<div class="verdict"><span class="badge {_RATING_CLASS.get(rating, "is-unknown")}">{esc(rating)}</span>'
        f'<span class="what">{esc(said)}</span></div>'
    )

    issues = ""
    revision = log.get("revision") or {}
    if revision.get("issues_raised"):
        issues = revision["issues_raised"]
    else:
        from orchestrator import _extract_issues
        issues = _extract_issues(log.get("review", "") or "")
    if issues.strip():
        block += f'<div class="issues">{esc(issues.strip())}</div>'
    return block


def _reviser(log: dict) -> str:
    revision = log.get("revision")
    if not revision:
        return _unrecorded(
            "The reviser's activity was not recorded for this run.",
            "It ran before the pipeline kept that record. Whether a revision pass fired "
            "here cannot be recovered from the log, so this page does not guess.",
        )
    if not revision.get("fired"):
        return (
            '<div class="verdict"><span class="badge is-unknown">DID NOT FIRE</span>'
            f'<span class="what">No revision pass ran, because '
            f'{esc(revision.get("stopped_because", "the reviewer was satisfied"))}.</span></div>'
        )

    before, after = revision.get("chars_before", 0), revision.get("chars_after", 0)
    delta = after - before
    change = ("grew by" if delta > 0 else "shrank by") + f" {abs(delta):,} characters"
    if delta == 0:
        change = "came back the same length"
    passes = revision.get("passes", 0)
    cleared = revision.get("cleared_the_rating")
    outcome = (
        f'It cleared the reviewer’s issues, so the rating became '
        f'{revision.get("rating_after", "PASS")}.'
        if cleared else
        f'The rating stayed {revision.get("rating_after", "?")}, and it stopped because '
        f'{revision.get("stopped_because", "it ran out of passes")}.'
    )
    return (
        '<div class="verdict"><span class="badge is-needs-work">FIRED</span>'
        f'<span class="what">The reviser ran <b>{passes} pass{"" if passes == 1 else "es"}</b>. '
        f"The output {esc(change)}. {esc(outcome)}</span></div>"
    )


def _files(log: dict) -> str:
    files = [Path(f).name for f in (log.get("code_files") or [])]
    problems = log.get("code_problems") or []
    if not files:
        return _unrecorded(
            "No runnable files came out of this run.",
            "The extractor pulls fenced code into real files when the output contains any; "
            "this one produced prose, or nothing it could safely write to disk.",
        )
    out = f'<div class="chips">{"".join(f"<span class=\"chip\">{esc(f)}</span>" for f in files)}</div>'
    if problems:
        listed = "".join(
            f'<span class="chip is-problem">{esc(p if isinstance(p, str) else json.dumps(p))}</span>'
            for p in problems[:8]
        )
        out += (
            '<p class="lede is-spaced">The mechanical check flagged these, '
            'and they are published rather than hidden:</p>'
            f'<div class="chips">{listed}</div>'
        )
    return out


def _credits(log: dict) -> str:
    credits = log.get("credits")
    if not credits:
        return _unrecorded(
            "This run's settlement was not itemised.",
            "The ledger recorded the credits, but without a run id attached, so they cannot be "
            "attributed back to this run specifically. The standings on the dashboard include them.",
        )
    rows = "".join(
        f'<tr><td class="who mono">{esc(c.get("contributor") or "unknown")}</td>'
        f'<td>{esc(c.get("for", c.get("type", "")))}</td>'
        f'<td class="num mono">+{esc(c.get("credits", 0))}</td></tr>'
        for c in credits
    )
    total = sum(float(c.get("credits", 0) or 0) for c in credits)
    return (
        '<div class="table-scroll"><table>'
        '<thead><tr><th>Contributor</th><th>For</th><th class="num">Credits</th></tr></thead>'
        f"<tbody>{rows}</tbody>"
        f'<tfoot><tr><td>Total</td><td></td><td class="num mono">{esc(f"{total:g}")}</td></tr></tfoot>'
        "</table></div>"
    )


_FENCE = re.compile(r"```(\w*)\n([\s\S]*?)```")


def _output(log: dict) -> str:
    text = (log.get("final_output") or "").strip() or (log.get("review") or "").strip()
    if not text:
        return _unrecorded("No output was saved.", "The pipeline did not reach an assembled result.")

    parts, last = [], 0
    for m in _FENCE.finditer(text):
        if m.start() > last:
            parts.append(f'<div class="prose">{esc(text[last:m.start()].strip())}</div>')
        lang = m.group(1) or "text"
        parts.append(
            f'<div class="code-block"><div class="head">{esc(lang)}</div>'
            f"<pre>{esc(m.group(2))}</pre></div>"
        )
        last = m.end()
    if last < len(text):
        parts.append(f'<div class="prose">{esc(text[last:].strip())}</div>')
    return "".join(parts) or f'<div class="prose">{esc(text)}</div>'


def _actions(run: str, log: dict) -> str:
    buttons = [
        f'<a class="btn btn-primary" href="/history/{esc(run)}/fork-template">Fork this run</a>',
        f'<a class="btn btn-ghost" href="/history/{esc(run)}/download">Download everything</a>',
        '<a class="btn btn-ghost" href="/dashboard#gallery">See what else the swarm built</a>',
    ]
    return "".join(buttons)


def _summary(log: dict) -> str:
    """The one line a link preview shows. It has to carry the whole story."""
    rating = log.get("rating") or "?"
    n = len(log.get("plan", []))
    verdict = {"PASS": "passed review", "NEEDS_WORK": "needed work",
               "FAIL": "failed review"}.get(rating, "was built")
    where = ("across volunteer machines" if log.get("mode") == "distributed"
             else "on one machine")
    duration = _duration(log.get("duration_seconds"))
    tail = f" in {duration}" if duration else ""
    return (f"Split into {n} subtasks and built {where} by local AI models{tail}, "
            f"then {verdict}. Mycelium runs on ordinary computers — no cloud, no API keys.")


# ── Route ────────────────────────────────────────────────────────────

@router.get("/run/{run_id}", response_class=HTMLResponse)
async def run_page(run_id: str, request: Request):
    """A permanent, shareable page for one completed run."""
    log = load_run(run_id)
    run = log["run_dir_name"]
    task = log.get("task") or "Untitled task"
    origin = str(request.base_url).rstrip("/")

    # A pitch can be a paragraph; a <title> and an OG title cannot.
    short = task if len(task) <= 70 else task[:67].rstrip() + "…"

    return render(
        "run.html",
        TITLE=esc(f"{short} — Mycelium"),
        OG_TITLE=esc(short),
        META_DESCRIPTION=esc(_summary(log)),
        OG_URL=esc(f"{origin}/run/{run}"),
        HEADLINE=esc(task),
        FACTS=_facts(log),
        RUN_ID=esc(f"run {run}") + (
            f' · project {esc(log["project_id"])}' if log.get("project_id") else ""
        ),
        PLAN=_plan(log),
        EXECUTION=_execution(log),
        REVIEW=_review(log),
        REVISER=_reviser(log),
        FILES=_files(log),
        CREDITS=_credits(log),
        OUTPUT=_output(log),
        ACTIONS=_actions(run, log),
    )
