"""
Live dashboard — watch the orchestrator work in your browser.

Shows connected nodes, active tasks, and pipeline progress in real-time.
Serves a web UI at http://localhost:8000/dashboard when the server runs.

Pages live in templates/. Each one carries a `<!-- THEME -->` marker, and this
module swaps in the shared theme layer (`templates/_theme.html`) as it serves.

That indirection exists for one reason: the palette used to be written out in
full inside every page, so a light theme meant editing three files and the
landing page would inevitably drift away from the dashboard. One definition,
every page follows.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

# Resolve relative to this file, not CWD — the server can be started from anywhere
_TEMPLATES_DIR = Path(__file__).parent / "templates"

_THEME_MARKER = "<!-- THEME -->"


def _page(name: str) -> str:
    """Read a template and inject the shared theme layer."""
    html = (_TEMPLATES_DIR / name).read_text(encoding="utf-8")
    if _THEME_MARKER not in html:
        # Better a themeless page than a 500 — but this is a bug, so make it
        # visible in the markup rather than silently shipping an unstyled page.
        return html.replace("<head>", "<head>\n<!-- WARNING: theme marker missing -->", 1)
    theme = (_TEMPLATES_DIR / "_theme.html").read_text(encoding="utf-8")
    return html.replace(_THEME_MARKER, theme, 1)


@router.get("/", response_class=HTMLResponse)
async def landing():
    return _page("index.html")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return _page("dashboard.html")


@router.get("/try", response_class=HTMLResponse)
async def try_page():
    return _page("try.html")
