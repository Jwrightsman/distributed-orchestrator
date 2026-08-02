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
_TEMPLATES_DIR = Path(__file__).parent / "templates"


@router.get("/", response_class=HTMLResponse)
async def landing():
    return (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return (_TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")


@router.get("/try", response_class=HTMLResponse)
async def try_page():
    return (_TEMPLATES_DIR / "try.html").read_text(encoding="utf-8")
