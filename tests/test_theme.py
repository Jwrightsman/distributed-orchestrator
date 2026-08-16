"""The shared theme layer, and the rule that keeps it shared.

Before this, every page wrote the palette out in full at its point of use — 116
hardcoded colours in the dashboard alone. That made a light theme impossible
without touching a hundred call sites, and guaranteed the landing page would
drift away from the dashboard the first time anyone adjusted a colour.

The regression these tests exist to prevent is subtle: a hardcoded colour is
invisible in dark mode (it was picked for dark mode) and only breaks in light,
which nobody looks at while developing.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
PAGES = ("index.html", "dashboard.html", "try.html", "run.html", "status.html")
# /node/{id} renders status.html — one layout, two sets of figures.
ROUTES = {"/": "index.html", "/dashboard": "dashboard.html", "/try": "try.html"}

# Partials carry most of the dashboard's styling now, so the no-hardcoded-colour
# rule has to follow the CSS out of the page it came from. Without this, the
# split would have quietly created a hole in the rule it was meant to preserve.
STYLED = PAGES + ("_dashboard.css", "_dashboard.js")

# Entities like &#9654; are not colours.
COLOR = re.compile(r"(?<![&\w])#[0-9A-Fa-f]{6}\b|rgba?\([0-9,. ]+\)")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("page", PAGES)
def test_every_page_requests_the_shared_theme(page):
    assert "<!-- THEME -->" in (TEMPLATES / page).read_text(encoding="utf-8"), (
        f"{page} has no theme marker, so it would render unstyled"
    )


@pytest.mark.parametrize("page", STYLED)
def test_no_page_hardcodes_a_colour(page):
    found = COLOR.findall((TEMPLATES / page).read_text(encoding="utf-8"))
    assert not found, (
        f"{page} hardcodes {len(found)} colour(s) ({sorted(set(found))[:4]}). "
        "Use a token from templates/_theme.html — a hardcoded colour looks fine "
        "in dark mode and breaks in light."
    )


def test_theme_partial_defines_both_themes():
    theme = (TEMPLATES / "_theme.html").read_text(encoding="utf-8")
    assert ":root {" in theme
    assert '[data-theme="light"]' in theme
    assert "prefers-color-scheme" in theme, "no system default"
    assert "localStorage" in theme, "a theme choice that does not persist is decoration"


@pytest.mark.parametrize("route,page", ROUTES.items())
def test_served_pages_carry_the_tokens(client, route, page):
    """The marker is only useful if the server actually substitutes it."""
    body = client.get(route).text
    assert "<!-- THEME -->" not in body, f"{route} shipped the raw marker"
    assert '[data-theme="light"]' in body, f"{route} served without the light theme"
    assert "--accent:" in body, f"{route} served without design tokens"


def test_theme_is_applied_before_paint(client):
    """A theme applied after render flashes the wrong colours on every load."""
    body = client.get("/dashboard").text
    head = body[: body.index("</head>")] if "</head>" in body else body
    assert "data-theme" in head, "theme is set after <head>, so the page will flash"


def test_missing_marker_degrades_visibly_not_silently(tmp_path, monkeypatch):
    import dashboard

    monkeypatch.setattr(dashboard, "_TEMPLATES_DIR", tmp_path)
    (tmp_path / "index.html").write_text("<head></head><body>hi</body>", encoding="utf-8")
    out = dashboard._page("index.html")
    assert "WARNING: theme marker missing" in out


# ── Public status endpoint ───────────────────────────────────────────

def test_status_json_is_public_and_safe_to_share(client):
    """The landing page's proof panel and any curious agent both read this.

    It must need no auth (an invite-gated network still has to be checkable)
    and must leak nothing: no task text, no hostnames, no keys.
    """
    r = client.get("/status.json")
    assert r.status_code == 200, "status.json must not require auth"
    d = r.json()
    for field in ("service", "status", "nodes_online", "pitches_completed",
                  "uptime_seconds", "model", "orchestrator_online"):
        assert field in d, f"missing {field}"
    blob = r.text.lower()
    for leak in ("secret", "pitch_key", "node_secret", "password", "token"):
        assert leak not in blob, f"status.json leaks {leak}"


def test_status_json_reports_orchestrator_up_even_with_no_nodes(client):
    """An empty swarm is not a broken one — the landing page depends on this
    distinction to render its zero state honestly."""
    import server_state as state
    state.nodes.clear()
    d = client.get("/status.json").json()
    assert d["nodes_online"] == 0
    assert d["orchestrator_online"] is True
