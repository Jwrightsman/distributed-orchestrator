"""
History, gallery, and sharing routes — everything that reads past runs
out of the output/ directory.
"""

import io
import json
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse

from server_state import OUTPUT_DIR

router = APIRouter()


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
                task = log.get("task", "Unknown")
                if query and query not in task.lower():
                    continue
                rating = log.get("rating", "?")
                if rating == "?":
                    review_f = d / "review.md"
                    if review_f.exists():
                        for line in review_f.read_text(errors="ignore", encoding="utf-8").splitlines():
                            if line.strip() in ("PASS", "NEEDS_WORK", "FAIL"):
                                rating = line.strip()
                                break
                runs.append({
                    "timestamp": log.get("timestamp", d.name),
                    "task": task,
                    "subtask_count": len(log.get("plan", [])),
                    "rating": rating,
                    "project_id": log.get("project_id") or None,
                    "mode": log.get("mode", "local"),
                    "dir": str(d),
                })
            except json.JSONDecodeError:
                pass
            if len(runs) >= limit:
                break
    return {"runs": runs, "count": len(runs)}


@router.get("/history/{timestamp}")
async def history_detail(timestamp: str):
    """Get full details of a past pipeline run."""
    run_dir = OUTPUT_DIR / timestamp
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")

    log_file = run_dir / "full_log.json"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log file not found")

    try:
        log = json.loads(log_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Corrupt log file")

    review_file = run_dir / "review.md"
    review_content = review_file.read_text(encoding="utf-8") if review_file.exists() else ""

    output_file = run_dir / "output.md"
    final_output = output_file.read_text(encoding="utf-8") if output_file.exists() else ""

    # The final rating, not the reviewer's pre-revision one. Reading it off
    # review.md alone made this endpoint contradict /history and /gallery for
    # the same run — see orchestrator.ratings_for.
    from orchestrator import ratings_for
    rating, reviewer_rating = ratings_for(log, review_content)

    # Build code file list from the code/ subdir
    code_dir = run_dir / "code"
    code_files = [f.name for f in sorted(code_dir.iterdir())] if code_dir.exists() else []

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
        "mode": log.get("mode", "local"),
        "project_id": log.get("project_id") or None,
    }


@router.get("/history/{timestamp}/download")
async def download_history(timestamp: str):
    """Download all files from a run as a ZIP archive."""
    run_dir = OUTPUT_DIR / timestamp
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(run_dir.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(run_dir))
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
    run_dir = OUTPUT_DIR / timestamp
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")

    log_file = run_dir / "full_log.json"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log file not found")

    try:
        log = json.loads(log_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Corrupt log file")

    task = log.get("task", "")
    rating = log.get("rating", "?")
    project_id = log.get("project_id") or ""

    # Final output for memory summary
    output_file = run_dir / "output.md"
    final_output = output_file.read_text(errors="ignore", encoding="utf-8") if output_file.exists() else ""

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
                rating = log.get("rating", "?")
                # Read first 300 chars of final output as preview
                preview = ""
                output_file = d / "output.md"
                if output_file.exists():
                    preview = output_file.read_text(errors="ignore", encoding="utf-8")[:300]
                elif log.get("review"):
                    from orchestrator import _extract_final_output
                    fo = _extract_final_output(log["review"])
                    preview = (fo or "")[:300]
                # Code file list
                code_dir = d / "code"
                code_files = [f.name for f in sorted(code_dir.iterdir())] if code_dir.exists() else []
                nodes_used_raw = log.get("nodes_used", [])
                nodes_used_count = len(nodes_used_raw) if isinstance(nodes_used_raw, list) else 0
                cards.append({
                    "timestamp": log.get("timestamp", d.name),
                    "task": log.get("task", "Unknown"),
                    "rating": rating,
                    "subtask_count": len(log.get("plan", [])),
                    "preview": preview.strip(),
                    "code_files": code_files,
                    "project_id": log.get("project_id") or None,
                    "mode": log.get("mode", "local"),
                    "nodes_used": nodes_used_count,
                })
            except (json.JSONDecodeError, OSError):
                pass
            if len(cards) >= limit:
                break
    return {"cards": cards, "count": len(cards)}
