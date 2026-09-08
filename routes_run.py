"""
The run permalink — /run/{id}.

A completed run used to be a card in a gallery grid with no URL of its own,
so there was nowhere to point when someone said "show me something the swarm
built". This is that page: one run, one address, server-rendered so it
previews correctly when pasted into Discord, Reddit or a comment.

The page leads with the deliverable and the plan follows, because plan is
process and the code is the product. That structure is built by `run_detail.py`
and is the same markup the console's run modal renders — one structure, two
shells, so neither can drift from the other. What stays here is what the
surface does not carry: the reviewer's own verdict, the reviser, settlement,
and the full assembled output.

Where a run predates a field, the page says so. The alternative — deriving a
plausible number — is how this project once published a figure that was true
when recorded and had quietly stopped being reproducible.
"""

import html as _html
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import run_detail
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
        # Kept rather than discarded: it already carries the execution id, the
        # sealed manifest and the integrity mode, so run detail reaches the
        # canonical record through the authority this route had to establish
        # anyway. No new route, and no second trip through the store.
        log["_publication"] = publication
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


# ── Run detail ───────────────────────────────────────────────────────

# How much of a deliverable the panel shows before it starts saying so.
PREVIEW_LINES = 14

def durable_record(publication) -> object | None:
    """The canonical execution record behind this run directory, if there is one.

    `require_legacy_run_publication` has already resolved the artifact-root
    binding, so the execution id is in hand and this is a store read rather
    than a lookup. Reading it here is not the provenance route §8.7 asks for:
    the server is reading its own store to render its own page, and no new
    HTTP endpoint appears.
    """
    execution_id = getattr(publication, "execution_id", None)
    if not execution_id:
        return None
    try:
        from execution.service import get_execution_service

        return get_execution_service().store.get(execution_id)
    except Exception:  # a page must not 500 because a store read failed
        return None


def provenance_envelope(publication) -> object | None:
    """The envelope for this run, or None when it has none.

    A legacy execution that predates envelopes renders the panel absent — not
    an empty panel and not an error — so this returning None is the whole of
    that behaviour.
    """
    execution_id = getattr(publication, "execution_id", None)
    if not execution_id:
        return None
    try:
        import server_state

        return server_state.provenance_envelope_store.get(execution_id)
    except Exception:
        return None


def _preview(log: dict, publication, durable) -> dict | None:
    """The first deliverable-role file, with its size and what checked it."""
    manifest = getattr(publication, "manifest", None)
    if manifest is not None:
        entries = [e for e in manifest.entries if str(e.role) == "deliverable"]
        name = entries[0].relative_path if entries else None
        size = entries[0].size_bytes if entries else None
    else:
        files = log.get("code_files") or []
        name = Path(files[0]).name if files else None
        size = None
        if name:
            name = f"code/{name}"

    if not name:
        return None

    try:
        path = published_file(publication, name)
        text = path.read_text(errors="ignore", encoding="utf-8") if path else ""
        if size is None and path is not None:
            size = path.stat().st_size
    except (LegacyRunNotPublished, OSError):
        return None

    checks = [run_detail._bytes(size)] if size else []
    if log.get("code_precheck_error"):
        checks.append("not checked")
    elif durable is not None:
        checks.extend(durable.validation_summary.checks_passed)
    # A preview, not the file: the whole thing is one authenticated download
    # away and a page is not a code viewer. But a silent 14 lines of a
    # 500-line file says "this file is 14 lines", so a cut preview says it was
    # cut and how much of the file it is showing.
    lines = text.splitlines()
    shown = lines[:PREVIEW_LINES]
    if len(lines) > PREVIEW_LINES:
        checks.append(f"first {PREVIEW_LINES} of {len(lines)} lines")

    return {
        "name": name,
        "text": "\n".join(shown),
        "checks": [c for c in checks if c],
    }


def _prose(log: dict) -> str:
    """The first paragraph of the assembled result, with no fenced code in it."""
    text = (log.get("final_output") or "").strip() or (log.get("review") or "").strip()
    without_code = _FENCE.sub(" ", text).strip()
    for block in without_code.split("\n\n"):
        cleaned = " ".join(block.split())
        if len(cleaned) > 40 and not cleaned.startswith("#"):
            return cleaned[:400]
    return ""


def run_surface(log: dict, run: str, surface: str = "server") -> str:
    """The shared run-detail structure, built once in run_detail.py."""
    publication = log.get("_publication")
    durable = durable_record(publication)
    return run_detail.render(
        run_detail.build_view(
            log,
            publication=publication,
            durable=durable,
            envelope=provenance_envelope(publication),
            surface=surface,
            run_id=run,
            preview=_preview(log, publication, durable),
            prose=_prose(log),
        )
    )


def _summary(log: dict, envelope=None) -> str:
    """The one line a link preview shows.

    This page is shareable, and the preview is all a reader gets before they
    decide whether to open it — so when the run has an envelope, its sentence
    leads. That sentence was written to survive being pasted with no page
    around it: it names the producer, the model and the checking, and states
    the limit in the same breath rather than in a tooltip. It is the reason
    the envelope is a sentence at all rather than a field list.
    """
    rating = log.get("rating") or "?"
    n = len(log.get("plan", []))
    verdict = {"PASS": "passed review", "NEEDS_WORK": "needed work",
               "FAIL": "failed review"}.get(rating, "was built")
    where = ("across invited machines" if log.get("mode") == "distributed"
             else "on one machine")

    if envelope is not None:
        return (
            f"{run_detail.envelope_sentence(dict(envelope.payload))} "
            f"Split into {n} units, built {where}, and {verdict}."
        )

    duration = _duration(log.get("duration_seconds"))
    tail = f" in {duration}" if duration else ""
    return (f"Split into {n} subtasks and built {where} by local AI models{tail}, "
            f"then {verdict}. Mycelium runs local models on computers you trust.")


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

    envelope = provenance_envelope(log.get("_publication"))

    return render(
        "run.html",
        TITLE=esc(f"{short} — Mycelium"),
        OG_TITLE=esc(short),
        META_DESCRIPTION=esc(_summary(log, envelope)),
        OG_URL=esc(f"{origin}/run/{run}"),
        RUN_DETAIL=run_surface(log, run, surface="server"),
        REVIEW=_review(log),
        REVISER=_reviser(log),
        CREDITS=_credits(log),
        OUTPUT=_output(log),
    )
