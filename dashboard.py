"""
Live dashboard — watch the orchestrator work in your browser.

Shows connected nodes, active tasks, and pipeline progress in real-time.
Serves a web UI at http://localhost:8000/dashboard when the server runs.

The page itself lives in templates/dashboard.html; this module just serves it.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

# Resolve relative to this file, not CWD — the server can be started from anywhere
_TEMPLATE_FILE = Path(__file__).parent / "templates" / "dashboard.html"


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return _TEMPLATE_FILE.read_text(encoding="utf-8")
