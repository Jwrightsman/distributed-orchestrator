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
PAGES = ("index.html", "dashboard.html", "try.html")
ROUTES = {"/": "index.html", "/dashboard": "dashboard.html", "/try": "try.html"}

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


@pytest.mark.parametrize("page", PAGES)
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
