"""
Live dashboard — watch the orchestrator work in your browser.

Shows connected nodes, active tasks, and pipeline progress in real-time.
Serves a web UI at http://localhost:8000/dashboard when the server runs.

Pages live in templates/ and are assembled here as they are served. Each
marker in a page is replaced by a partial:

    <!-- THEME -->            templates/_theme.html   (palette + theme toggle)
    <!-- DASHBOARD_CSS -->    templates/_dashboard.css
    <!-- RUN_DETAIL_CSS -->   templates/_run_detail.css
    <!-- STATUS_MODEL_JS -->  templates/_status_model.js
    <!-- DASHBOARD_JS -->     templates/_dashboard.js

The theme indirection exists because the palette used to be written out in
full inside every page, so a light theme meant editing three files and the
landing page would inevitably drift away from the dashboard. One definition,
every page follows.

The CSS/JS split exists for a plainer reason: dashboard.html was 80 KB of
markup, styles and script in one file, which is not navigable. There is still
no build step — the server pastes the parts together.
"""

import html
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import tracing
from config import get as get_config
from worker_protocol import SERVER_VERSION

router = APIRouter()

# Resolve relative to this file, not CWD — the server can be started from anywhere
_TEMPLATES_DIR = Path(__file__).parent / "templates"

_THEME_MARKER = "<!-- THEME -->"

# marker -> (partial filename, wrapper tag). A partial that is a real .css or
# .js file gets wrapped as it is injected, so the file on disk stays valid
# CSS/JS that an editor and a linter can both understand.
_PARTIALS = {
    "<!-- DASHBOARD_CSS -->": ("_dashboard.css", "style"),
    # Run detail is one structure on three surfaces, so it is one stylesheet
    # on all of them. The dashboard and run.html both take this marker; a
    # second copy of the rules is exactly the drift run_detail.py exists to
    # prevent.
    "<!-- RUN_DETAIL_CSS -->": ("_run_detail.css", "style"),
    # Before DASHBOARD_JS in every page that wants both: _dashboard.js reads
    # STATUS_MODEL at load. Dict order is insertion order, and a page that
    # carries only one of the two markers simply gets that one.
    "<!-- STATUS_MODEL_JS -->": ("_status_model.js", "script"),
    "<!-- DASHBOARD_JS -->": ("_dashboard.js", "script"),
}


def _read(name: str) -> str:
    return (_TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _page(name: str) -> str:
    """Read a template and inject the shared theme layer and any partials."""
    html = _read(name)
    if _THEME_MARKER not in html:
        # Better a themeless page than a 500 — but this is a bug, so make it
        # visible in the markup rather than silently shipping an unstyled page.
        return html.replace("<head>", "<head>\n<!-- WARNING: theme marker missing -->", 1)
    html = html.replace(_THEME_MARKER, _read("_theme.html"), 1)

    for marker, (partial, tag) in _PARTIALS.items():
        if marker in html:
            html = html.replace(marker, f"<{tag}>\n{_read(partial)}\n</{tag}>", 1)
    return html


def render(name: str, **slots: str) -> str:
    """Fill a template's `<!--SLOT:NAME-->` placeholders.

    The data-driven pages (a run, a node, the status page) are rendered on the
    server rather than fetched by JavaScript, for two reasons that both matter
    more than convenience: an OpenGraph crawler unrolling a link preview in
    Discord or Reddit does not run scripts, and a page meant to be linked
    from a launch post should not go blank when a fetch fails.

    Slot values are HTML and must already be escaped by the caller — see
    routes_run.py, which escapes at the point every value is turned into
    markup. Any slot the caller does not supply is emptied rather than left
    visible, so a forgotten slot degrades to a gap instead of leaking markup.
    """
    html = _page(name)
    for key, value in slots.items():
        html = html.replace(f"<!--SLOT:{key}-->", value)
    return re.sub(r"<!--SLOT:[A-Z_]+-->", "", html)


def tracing_state() -> str:
    """`off`, `propagating` or `exporting` — the three states tracing.py has.

    Not a boolean, and the difference is not cosmetic: `propagating` accepts and
    mints trace context without anything leaving the machine, while `exporting`
    additionally sends spans to a collector. Reading `tracing_enabled` alone
    would report `exporting` for a deployment that exports nothing, which is the
    mistake tracing.py's own docstring records.
    """
    if not tracing.propagation_enabled():
        return "off"
    return "exporting" if tracing.export_enabled() else "propagating"


def status_bar_at_load() -> dict[str, str]:
    """The status bar's four lampless cells, and the model name beside INFERENCE.

    These are config, not endpoints — nothing serves them, and none of them can
    change while the process runs. So they are read once, here, and the cells
    that carry them deliberately have no lamp: a cell with no lamp is making no
    claim about this second. See docs/design/status-model-2026-09-07 §07.

    MODEL is `config · model` on purpose. `/health.models` lists everything
    installed on the host and `/status.json.model` is whichever tag answered
    first; neither is the model this coordinator will use.
    """
    settings = get_config()
    tracing_now = tracing_state()
    evidence = str(settings.get("capability_evidence_mode", "off") or "off")
    return {
        "STATUS_MODEL_NAME": html.escape(str(settings.get("model", "") or "not configured")),
        "STATUS_TRACING": html.escape(tracing_now),
        "STATUS_TRACING_WHY": _TRACING_WHY[tracing_now],
        "STATUS_EVIDENCE": html.escape(evidence),
        "STATUS_EVIDENCE_WHY": _EVIDENCE_WHY.get(evidence, _EVIDENCE_WHY["shadow"]),
    }


# What each drawer value means for the person reading it (HANDOFF-UI §17.1).
# Each sentence is the behaviour of the module that owns the value, not the
# design file's copy, which described a different system: its MODE sentence
# was about where model calls go and its EVIDENCE one about manifests.
_TRACING_WHY = {
    "off": "No trace header is read or written and no span is built.",
    "propagating": "Trace context is accepted and passed on. Nothing leaves this machine.",
    "exporting": "Spans are sent to the collector this process was configured with.",
}
_EVIDENCE_WHY = {
    "off": "Nothing is recorded about how machines behave after work is handed out.",
    "shadow": "Recorded only. It never changes which machine is eligible, the order of work, "
              "or who gets it.",
}


def identity_at_load() -> dict[str, str]:
    """The machine identity block's config-backed rows (HANDOFF-UI §18).

    A viewer key and the enrolment mode become words here, never values: the
    block says whether the gate is open and never what the key is.
    """
    settings = get_config()
    viewer_set = bool(str(settings.get("viewer_key", "") or "").strip())
    required = settings.get("node_enrollment_mode") == "required"
    return {
        "SERVER_VERSION": html.escape(SERVER_VERSION),
        "IDENT_VIEWER_META": "set" if viewer_set else "not set",
        "IDENT_VIEWER_TONE": " is-ok" if viewer_set else " is-warn",
        "IDENT_VIEWER_TITLE": (
            "The key this console asks for before it shows anything" if viewer_set
            else "No key is set, so anybody who can reach this address can read the console"
        ),
        "IDENT_ENROLL_META": "enrolment required" if required else "compat",
        "IDENT_ENROLL_TONE": "" if required else " is-warn",
        "IDENT_INVITE_TITLE": (
            "The join command. Every machine enrols, so each can be revoked on its own"
            if required else
            "The join command. Enrolment is not required, so an older worker can join on "
            "the shared secret alone and cannot be revoked on its own"
        ),
    }


def composer_at_load() -> dict[str, str]:
    """Overview's rate note, and whether pitching here needs the pitch key.

    Only the fact that a pitch key is set reaches the page, never the key: a
    refused pitch says as much already, and saying it up front lets the page
    ask before the first pitch instead of recording a failed run.
    """
    settings = get_config()
    count = settings.get("pitch_rate_max", 5)
    window = settings.get("pitch_rate_window", 60)
    per = "min" if window == 60 else ("hour" if window == 3600 else f"{window}s")
    return {"PITCH_RATE_NOTE": html.escape(f"{count} / {per}"),
            "PITCH_KEY_REQUIRED": "true" if settings.get("pitch_key") else "false"}


@router.get("/", response_class=HTMLResponse)
async def landing():
    return _page("index.html")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return render("dashboard.html", **status_bar_at_load(), **identity_at_load(),
                  **composer_at_load())

