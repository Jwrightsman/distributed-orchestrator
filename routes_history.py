"""
History, gallery, and sharing routes — everything that reads past runs
out of the output/ directory.
"""

import io
import json
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from config import get as get_config
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
                log = json.loads(log_file.read_text())
                task = log.get("task", "Unknown")
                if query and query not in task.lower():
                    continue
                rating = log.get("rating", "?")
                if rating == "?":
                    review_f = d / "review.md"
                    if review_f.exists():
                        for line in review_f.read_text(errors="ignore").splitlines():
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
        log = json.loads(log_file.read_text())
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Corrupt log file")

    review_file = run_dir / "review.md"
    review_content = review_file.read_text() if review_file.exists() else ""

    output_file = run_dir / "output.md"
    final_output = output_file.read_text() if output_file.exists() else ""

    # Derive rating from review file (most reliable source)
    rating = "?"
    for line in review_content.splitlines():
        if line.strip() in ("PASS", "NEEDS_WORK", "FAIL"):
            rating = line.strip()
            break

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
        "code_files": code_files,
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
        log = json.loads(log_file.read_text())
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Corrupt log file")

    task = log.get("task", "")
    rating = log.get("rating", "?")
    project_id = log.get("project_id") or ""

    # Final output for memory summary
    output_file = run_dir / "output.md"
    final_output = output_file.read_text(errors="ignore") if output_file.exists() else ""

    # memory.md — use project memory if available, else build a starter
    memory_content = ""
    if project_id:
        try:
            from memory import PROJECTS_DIR
            proj_memory_file = PROJECTS_DIR / project_id / "memory.md"
            if proj_memory_file.exists():
                memory_content = proj_memory_file.read_text(errors="ignore")
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
        "This ZIP was exported from the Distributed AI Orchestrator.\n\n"
        "## How to use\n\n"
        "1. **Install the orchestrator** — follow the README at https://github.com/yourusername/distributed-orchestrator\n"
        "2. **Import this fork** — run:\n"
        f"   ```\n   py cli.py --import fork_{timestamp}.zip\n   ```\n"
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
async def share_page(timestamp: str, request: Request):
    """Shareable standalone HTML page for a past run — designed for Twitter/Discord/Reddit."""
    run_dir = OUTPUT_DIR / timestamp
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")

    log_file = run_dir / "full_log.json"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log file not found")

    try:
        log = json.loads(log_file.read_text())
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Corrupt log file")

    task = log.get("task", "Unknown task")
    rating = log.get("rating", "?")
    mode = log.get("mode", "local")
    subtask_count = len(log.get("plan", []))
    model = log.get("model", get_config().get("model", "qwen3.5:4b"))

    output_file = run_dir / "output.md"
    final_output = output_file.read_text(errors="ignore") if output_file.exists() else ""

    # Derive rating from review if not in log
    if not rating or rating == "?":
        review_file = run_dir / "review.md"
        if review_file.exists():
            for line in review_file.read_text(errors="ignore").splitlines():
                if line.strip() in ("PASS", "NEEDS_WORK", "FAIL"):
                    rating = line.strip()
                    break

    preview_400 = final_output[:400] if final_output else ""
    og_desc = (final_output[:200] if final_output else "AI-generated with Distributed Orchestrator").replace('"', '&quot;')
    og_title = task.replace('"', '&quot;')

    origin = str(request.base_url).rstrip("/")

    # Rating colours
    rating_color_map = {"PASS": "#00FF88", "NEEDS_WORK": "#E8FF47", "FAIL": "#FF5555"}
    rating_bg_map = {"PASS": "#00FF8818", "NEEDS_WORK": "#E8FF4718", "FAIL": "#FF555518"}
    rating_color = rating_color_map.get(rating, "#888")
    rating_bg = rating_bg_map.get(rating, "#88888818")

    # Relative time
    try:
        dt = datetime.strptime(timestamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
        delta = int(datetime.now(timezone.utc).timestamp() - dt.timestamp())
        if delta < 60:
            rel_time = "just now"
        elif delta < 3600:
            rel_time = f"{delta // 60}m ago"
        elif delta < 86400:
            rel_time = f"{delta // 3600}h ago"
        else:
            rel_time = f"{delta // 86400}d ago"
    except Exception:
        rel_time = timestamp

    dist_badge = '<span style="font-family:\'Consolas\',monospace;font-size:10px;font-weight:700;color:#00FFAA;background:#00FFAA18;border:1px solid #00FFAA30;border-radius:4px;padding:2px 8px;letter-spacing:.5px;">DIST</span>' if mode == "distributed" else ""

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(task)} — Distributed AI Orchestrator</title>
<meta property="og:title" content="{og_title}">
<meta property="og:description" content="{og_desc}">
<meta property="og:type" content="website">
<meta property="og:url" content="{origin}/share/{timestamp}">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{og_title}">
<meta name="twitter:description" content="{og_desc}">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: #08090C;
    color: #D8D8D8;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 40px 16px 60px;
  }}
  .card {{
    width: 100%;
    max-width: 680px;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 16px;
    padding: 36px 40px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }}
  .logo-tag {{
    font-family: 'Consolas', monospace;
    font-size: 10px;
    color: #00FFAA;
    letter-spacing: 3px;
    font-weight: 600;
    margin-bottom: 4px;
  }}
  .task-title {{
    font-size: 22px;
    font-weight: 800;
    color: #F0F0F0;
    line-height: 1.35;
    letter-spacing: -0.3px;
  }}
  .meta-row {{
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    align-items: center;
  }}
  .badge {{
    font-family: 'Consolas', monospace;
    font-size: 11px;
    font-weight: 700;
    border-radius: 5px;
    padding: 3px 10px;
    border: 1px solid;
  }}
  .preview-box {{
    background: rgba(0,0,0,0.3);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 10px;
    padding: 18px 20px;
    font-family: 'Consolas', monospace;
    font-size: 12px;
    color: #888;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 200px;
    overflow: hidden;
    position: relative;
  }}
  .preview-box::after {{
    content: '';
    position: absolute;
    bottom: 0; left: 0; right: 0;
    height: 36px;
    background: linear-gradient(transparent, rgba(2,3,5,0.97));
    border-radius: 0 0 10px 10px;
  }}
  .meta-detail {{
    font-family: 'Consolas', monospace;
    font-size: 11px;
    color: #444;
  }}
  .actions {{
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }}
  .btn {{
    flex: 1;
    min-width: 160px;
    padding: 13px 20px;
    border-radius: 9px;
    font-size: 13px;
    font-weight: 700;
    cursor: pointer;
    text-align: center;
    text-decoration: none;
    transition: all 0.15s;
    display: block;
  }}
  .btn-primary {{
    background: #00FFAA;
    color: #08090C;
    border: none;
  }}
  .btn-primary:hover {{ background: #00e899; }}
  .btn-secondary {{
    background: none;
    color: #00FFAA;
    border: 1.5px solid rgba(0,255,170,0.35);
  }}
  .btn-secondary:hover {{ background: rgba(0,255,170,0.08); border-color: #00FFAA; }}
  .footer {{
    margin-top: 32px;
    font-size: 11px;
    color: #333;
    text-align: center;
    line-height: 1.7;
  }}
  .footer a {{ color: #444; text-decoration: none; }}
  .footer a:hover {{ color: #00FFAA; }}
  @media (max-width: 480px) {{
    .card {{ padding: 24px 20px; }}
    .task-title {{ font-size: 18px; }}
  }}
</style>
</head>
<body>
<div class="card">
  <div>
    <div class="logo-tag">DISTRIBUTED AI ORCHESTRATOR</div>
    <div class="task-title">{esc(task)}</div>
  </div>
  <div class="meta-row">
    {dist_badge}
    <span class="badge" style="color:{rating_color};background:{rating_bg};border-color:{rating_color}30;">{esc(rating)}</span>
    <span class="meta-detail">{subtask_count} subtasks</span>
    <span class="meta-detail">·</span>
    <span class="meta-detail">{esc(model)}</span>
    <span class="meta-detail">·</span>
    <span class="meta-detail">{rel_time}</span>
  </div>
  {f'<div class="preview-box">{esc(preview_400)}</div>' if preview_400 else ''}
  <div class="actions">
    <a class="btn btn-primary" href="{origin}/history/{timestamp}/fork-template" download="fork_{timestamp}.zip">Fork this project</a>
    <a class="btn btn-secondary" href="{origin}/dashboard#run={timestamp}" target="_blank">View full output ↗</a>
  </div>
</div>
<div class="footer">
  Built with <a href="https://github.com/yourusername/distributed-orchestrator" target="_blank">Distributed AI Orchestrator</a>
  &nbsp;·&nbsp; Run your own on any hardware
</div>
</body>
</html>"""

    return HTMLResponse(content=html)


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
                log = json.loads(log_file.read_text())
                rating = log.get("rating", "?")
                # Read first 300 chars of final output as preview
                preview = ""
                output_file = d / "output.md"
                if output_file.exists():
                    preview = output_file.read_text(errors="ignore")[:300]
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
