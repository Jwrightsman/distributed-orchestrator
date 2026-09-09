"""
History, gallery, and sharing routes — everything that reads past runs
out of the output/ directory.
"""

import io
import json
import logging
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

import run_detail
from execution.artifacts import ArtifactError
from execution.publication import (
    LegacyRunNotPublished,
    published_file,
    published_paths,
    require_legacy_run_publication,
)
from server_state import OUTPUT_DIR

logger = logging.getLogger("mycelium.history")
router = APIRouter()


def _run_dir(timestamp: str):
    run_dir = OUTPUT_DIR / timestamp
    try:
        if run_dir.resolve().parent != OUTPUT_DIR.resolve() or not run_dir.is_dir():
            raise HTTPException(status_code=404, detail="Run not found")
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    return run_dir


@router.get("/history")
async def history(search: str = "", limit: int = 50):
    """List past pipeline runs from the output folder.

    Pass ?search=<text> to filter runs whose task text contains the query
    (case-insensitive). Returns up to `limit` most recent matching runs.
    """
    query = search.strip().lower()
    runs = []
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            log_file = d / "full_log.json"
            if not log_file.exists():
                continue
            try:
                log = json.loads(log_file.read_text(encoding="utf-8"))
                publication = require_legacy_run_publication(d, log)
                task = log.get("task", "Unknown")
                if query and query not in task.lower():
                    continue
                rating = log.get("rating", "?")
                if rating == "?":
                    review_f = published_file(publication, "review.md")
                    if review_f is not None:
                        for line in review_f.read_text(errors="ignore", encoding="utf-8").splitlines():
                            if line.strip() in ("PASS", "NEEDS_WORK", "FAIL"):
                                rating = line.strip()
                                break
                runs.append({
                    "timestamp": log.get("timestamp", d.name),
                    "task": task,
                    "subtask_count": len(log.get("plan", [])),
                    "rating": rating,
                    # Carried so a list card can tell PASS from UNCHECKED. Without
                    # it the list would render PASS - "the reviewer passed it and
                    # the mechanical check found no defects" - over a run whose
                    # check never reached a verdict, while the run's own modal
                    # said UNCHECKED for the same run.
                    "code_precheck_error": log.get("code_precheck_error"),
                    "project_id": log.get("project_id") or None,
                    "mode": log.get("mode", "local"),
                    "dir": str(d),
                })
            except (json.JSONDecodeError, LegacyRunNotPublished, OSError):
                pass
            if len(runs) >= limit:
                break
    return {"runs": runs, "count": len(runs)}


def _relative(timestamp: str) -> str:
    """An age, for the console only.

    The console polls and re-renders, so an age it can correct is honest
    there. `/run/{id}` never gets one: it is a snapshot with no client, and
    "2h 14m ago" on a page nothing refreshes is a sentence that stops being
    true the moment it is written.
    """
    try:
        moment = datetime.strptime(timestamp, "%Y%m%d_%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return ""
    delta = int(datetime.now(timezone.utc).timestamp() - moment.timestamp())
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _ledger_chain() -> dict | None:
    """The chain verdict for the console panel, or None if it cannot be read.

    Read here rather than fetched over HTTP: the console's own route already
    runs inside the coordinator, and a second network hop would only add a way
    for the panel to disagree with the endpoint. The gating still governs where
    the panel appears — `/v1/operator/ledger-chain` is refused at the public
    edge, so this is passed on the console surface and never on `/run/{id}`.

    A complete walk every time it actually walks, bounded by the same short TTL
    the endpoint serves, so opening ten runs does not walk the chain ten times.
    """
    try:
        from ledger import walk_ledger_chain
        import server_state

        return walk_ledger_chain(server_state._DB_PATH).as_dict()
    except Exception:
        logger.warning("ledger chain could not be walked", exc_info=True)
        return None


def _detail_html(
    log: dict,
    timestamp: str,
    publication,
    *,
    review: str = "",
    final_output: str = "",
) -> str:
    """The console's run detail, from the same builder /run/{id} uses.

    This is one added field on an endpoint that already had consumers --
    `evals/run_evals.py` reads its JSON on every completed remote run. A
    rendering bug must not be able to take that away, so a failure here costs
    the console its panel and nothing else. The console renders its own
    could-not-load state when the fragment comes back empty.
    """
    from routes_run import durable_record, provenance_envelope

    log = dict(log)
    log["_publication"] = publication
    # Both live beside the log rather than in it, and the deliverable panel's
    # prose is drawn from them.
    log["review"] = review
    log["final_output"] = final_output
    durable = durable_record(publication)
    try:
        from routes_run import _preview, _prose

        preview, prose = _preview(log, publication, durable), _prose(log)
    except (LegacyRunNotPublished, OSError):
        preview, prose = None, ""

    try:
        return run_detail.render(
            run_detail.build_view(
                log,
                publication=publication,
                durable=durable,
                envelope=provenance_envelope(publication),
                chain=_ledger_chain(),
                surface="console",
                run_id=timestamp,
                relative_age=_relative(str(log.get("timestamp") or timestamp)),
                preview=preview,
                prose=prose,
            )
        )
    except Exception:
        logger.warning("run detail could not be rendered for %s", timestamp, exc_info=True)
        return ""


@router.get("/history/{timestamp}")
async def history_detail(timestamp: str):
    """Get full details of a past pipeline run."""
    run_dir = _run_dir(timestamp)

    log_file = run_dir / "full_log.json"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log file not found")

    try:
        log = json.loads(log_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Corrupt log file")

    try:
        publication = require_legacy_run_publication(run_dir, log)
    except LegacyRunNotPublished as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc

    try:
        review_file = published_file(publication, "review.md")
        review_content = review_file.read_text(encoding="utf-8") if review_file else ""
        output_file = published_file(publication, "output.md")
        final_output = output_file.read_text(encoding="utf-8") if output_file else ""
        code_files = [
            path.rsplit("/", 1)[-1]
            for path in published_paths(publication, "code")
        ]
    except (LegacyRunNotPublished, OSError) as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc

    # The final rating, not the reviewer's pre-revision one. Reading it off
    # review.md alone made this endpoint contradict /history and /gallery for
    # the same run — see orchestrator.ratings_for.
    from orchestrator import ratings_for
    rating, reviewer_rating = ratings_for(log, review_content)

    return {
        "task": log.get("task"),
        "timestamp": log.get("timestamp"),
        "plan": log.get("plan", []),
        "results": log.get("results", {}),
        "review": review_content,
        "final_output": final_output,
        "rating": rating,
        "reviewer_rating": reviewer_rating,
        "code_files": code_files,
        "code_problems": log.get("code_problems", []),
        "code_precheck_error": log.get("code_precheck_error"),
        "mode": log.get("mode", "local"),
        "project_id": log.get("project_id") or None,
        # Run detail is one structure on three surfaces, so the console gets
        # the structure rather than the parts to rebuild it from. Built by
        # run_detail.py, the same call /run/{id} makes, which is what stops
        # the two from drifting -- and what lets the console read lifecycle,
        # validation and assurance separately without a new route: the
        # publication this route already had to establish carries the
        # execution id, and the server reads its own store from there.
        "detail_html": _detail_html(
            log,
            timestamp,
            publication,
            review=review_content,
            final_output=final_output,
        ),
    }


@router.get("/history/{timestamp}/download")
async def download_history(timestamp: str):
    """Download all files from a run as a ZIP archive."""
    run_dir = _run_dir(timestamp)
    log_file = run_dir / "full_log.json"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        log = json.loads(log_file.read_text(encoding="utf-8"))
        publication = require_legacy_run_publication(run_dir, log)
    except (json.JSONDecodeError, LegacyRunNotPublished, OSError) as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc

    if publication.sealed:
        try:
            assert publication.artifacts is not None
            assert publication.execution_id is not None
            prepared = publication.artifacts.prepare_archive(publication.execution_id)
        except (ArtifactError, OSError) as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        return FileResponse(
            prepared.path,
            media_type="application/zip",
            filename=f"output_{timestamp}.zip",
            headers={"Content-Length": str(prepared.size_bytes)},
            background=BackgroundTask(prepared.path.unlink, missing_ok=True),
        )

    try:
        direct_files = [
            (resolved, file_path.relative_to(run_dir))
            for file_path in sorted(run_dir.rglob("*"))
            if file_path.is_file()
            and (
                resolved := published_file(
                    publication,
                    file_path.relative_to(run_dir).as_posix(),
                )
            )
            is not None
        ]
    except (LegacyRunNotPublished, OSError) as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for resolved, relative_path in direct_files:
            zf.write(resolved, relative_path)
    zip_buffer.seek(0)

    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=output_{timestamp}.zip"},
    )


@router.get("/history/{timestamp}/fork-template")
async def fork_template(timestamp: str):
    """Download a fork template ZIP for a past run.

    Contains task.txt, memory.md, fork_config.json, and README.md.
    """
    run_dir = _run_dir(timestamp)

    log_file = run_dir / "full_log.json"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log file not found")

    try:
        log = json.loads(log_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Corrupt log file")

    try:
        publication = require_legacy_run_publication(run_dir, log)
    except LegacyRunNotPublished as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc

    task = log.get("task", "")
    rating = log.get("rating", "?")
    project_id = log.get("project_id") or ""

    # Final output for memory summary
    try:
        output_file = published_file(publication, "output.md")
        final_output = (
            output_file.read_text(errors="ignore", encoding="utf-8")
            if output_file
            else ""
        )
    except (LegacyRunNotPublished, OSError) as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc

    # memory.md — use project memory if available, else build a starter
    memory_content = ""
    if project_id:
        try:
            from memory import PROJECTS_DIR
            proj_memory_file = PROJECTS_DIR / project_id / "memory.md"
            if proj_memory_file.exists():
                memory_content = proj_memory_file.read_text(errors="ignore", encoding="utf-8")
        except Exception:
            pass
    if not memory_content:
        summary_preview = final_output[:600] if final_output else "(no output)"
        memory_content = (
            f"# Project Memory\n\n"
            f"## Original Task\n{task}\n\n"
            f"## Output Summary\n{summary_preview}\n\n"
            f"## Notes\nForked from run {timestamp}. Continue building from here.\n"
        )

    fork_config = {
        "original_task": task,
        "original_timestamp": timestamp,
        "rating": rating,
        "suggested_next_steps": f"Fork of: {task}. Continue from where this left off.",
    }

    readme_content = (
        "# Fork Template\n\n"
        "This ZIP was exported from Mycelium.\n\n"
        "## How to use\n\n"
        "1. **Install the orchestrator** — follow the README at https://github.com/yourusername/distributed-orchestrator\n"
        "2. **Import this fork** — run:\n"
        f"   ```\n   python cli.py --import fork_{timestamp}.zip\n   ```\n"
        "3. **Or paste manually** — copy the content of `task.txt` into the dashboard pitch input at http://localhost:8000/dashboard\n\n"
        "## Files\n\n"
        "- `task.txt` — the original task prompt\n"
        "- `memory.md` — project memory / context from the original run\n"
        "- `fork_config.json` — metadata about the original run\n"
        "- `README.md` — this file\n\n"
        "## Original task\n\n"
        f"> {task}\n\n"
        f"**Rating:** {rating}\n"
    )

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("task.txt", task)
        zf.writestr("memory.md", memory_content)
        zf.writestr("fork_config.json", json.dumps(fork_config, indent=2))
        zf.writestr("README.md", readme_content)
    zip_buffer.seek(0)

    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=fork_{timestamp}.zip"},
    )


@router.get("/share/{timestamp}")
async def share_page(timestamp: str):
    """Old share links keep working — /run/{id} is the page now.

    This used to be a standalone page with its own hardcoded palette, written
    before the theme layer existed. It could not do light mode, it drifted
    from every other page the first time a colour changed, and it duplicated
    a worse version of what /run/{id} now shows. One shareable artifact is
    the point: a link posted six months ago and a link posted today should
    land on the same page.
    """
    return RedirectResponse(url=f"/run/{timestamp}", status_code=301)


# ── Gallery ──────────────────────────────────────────────────────────
@router.get("/gallery")
async def gallery(limit: int = 30):
    """Return completed runs as gallery cards — for the Swarm Gallery page."""
    cards = []
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            log_file = d / "full_log.json"
            if not log_file.exists():
                continue
            try:
                log = json.loads(log_file.read_text(encoding="utf-8"))
                publication = require_legacy_run_publication(d, log)
                rating = log.get("rating", "?")
                # Read first 300 chars of final output as preview
                preview = ""
                output_file = published_file(publication, "output.md")
                if output_file is not None:
                    preview = output_file.read_text(errors="ignore", encoding="utf-8")[:300]
                elif log.get("review"):
                    from orchestrator import _extract_final_output
                    fo = _extract_final_output(log["review"])
                    preview = (fo or "")[:300]
                # Code file list
                code_files = [
                    path.rsplit("/", 1)[-1]
                    for path in published_paths(publication, "code")
                ]
                nodes_used_raw = log.get("nodes_used", [])
                nodes_used_count = len(nodes_used_raw) if isinstance(nodes_used_raw, list) else 0
                cards.append({
                    "timestamp": log.get("timestamp", d.name),
                    "task": log.get("task", "Unknown"),
                    "rating": rating,
                    # Same reason as /history: an empty problem list beside a
                    # precheck error means "not checked", not "checked clean".
                    "code_precheck_error": log.get("code_precheck_error"),
                    "subtask_count": len(log.get("plan", [])),
                    "preview": preview.strip(),
                    "code_files": code_files,
                    "project_id": log.get("project_id") or None,
                    "mode": log.get("mode", "local"),
                    "nodes_used": nodes_used_count,
                })
            except (json.JSONDecodeError, LegacyRunNotPublished, OSError):
                pass
            if len(cards) >= limit:
                break
    return {"cards": cards, "count": len(cards)}
