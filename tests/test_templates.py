"""Structural checks on the served HTML.

These exist because of a bug that was invisible for months: dashboard.html
opened `<main class="content">` and never closed it. Browsers recover from
that silently, so the page *looked* fine — but Gallery and Guild ended up
inside `<main>` by accident rather than by structure, and the two modals ended
up as content of the current view rather than as dialogs covering it.

Nothing rendered wrong, which is exactly why nobody found it. A parser does.

The accessibility assertions are here for the same reason: a missing landmark
or an unlabelled control is invisible to the person who wrote the page and
decisive for the person using a screen reader.
"""

from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

# Routes that render a full page, and how to reach one that needs an id.
STATIC_ROUTES = ("/", "/dashboard", "/try", "/status")

VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
# Foreign content: SVG uses self-closing syntax the HTML parser reports as
# start tags, so its subtree is skipped rather than balanced.
FOREIGN = {"svg"}


class _Balance(HTMLParser):
    """Track open elements and report the first thing that does not close."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.errors: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if self._skip_depth:
            if tag in FOREIGN:
                self._skip_depth += 1
            return
        if tag in FOREIGN:
            self._skip_depth = 1
            return
        if tag in VOID:
            return
        self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        pass  # <foo /> opens and closes in one go

    def handle_endtag(self, tag):
        if self._skip_depth:
            if tag in FOREIGN:
                self._skip_depth -= 1
            return
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append(f"line {self.getpos()[0]}: </{tag}> with nothing open")
            return
        open_tag, open_line = self.stack[-1]
        if open_tag == tag:
            self.stack.pop()
            return
        # Mismatch: find out whether this end tag closes something further up,
        # which means everything between it was never closed.
        for depth in range(len(self.stack) - 1, -1, -1):
            if self.stack[depth][0] == tag:
                unclosed = self.stack[depth + 1:]
                names = ", ".join(f"<{t}> opened line {ln}" for t, ln in unclosed)
                self.errors.append(
                    f"line {self.getpos()[0]}: </{tag}> closes the element opened "
                    f"on line {self.stack[depth][1]}, but {names} never closed"
                )
                del self.stack[depth:]
                return
        self.errors.append(f"line {self.getpos()[0]}: </{tag}> matches no open element")


def _check_balance(html: str) -> list[str]:
    p = _Balance()
    p.feed(html)
    p.close()
    errs = list(p.errors)
    errs += [f"<{t}> opened on line {ln} is never closed" for t, ln in p.stack]
    return errs


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("route", STATIC_ROUTES)
def test_served_html_is_balanced(client, route):
    """Every element that opens, closes — in the assembled page, not the source.

    Checking the served output rather than the template is deliberate: the
    theme, CSS and JS partials are pasted in at request time, so a partial
    that breaks the document would not show up in a template-only check.
    """
    errors = _check_balance(client.get(route).text)
    assert not errors, f"{route} is malformed:\n  " + "\n  ".join(errors)


def test_run_page_is_balanced(client):
    """The run permalink is generated per run, so it needs its own check."""
    runs = client.get("/history?limit=1").json()["runs"]
    if not runs:
        pytest.skip("no runs on disk to render")
    r = client.get(f"/run/{runs[0]['timestamp']}")
    assert r.status_code == 200
    errors = _check_balance(r.text)
    assert not errors, "/run/{id} is malformed:\n  " + "\n  ".join(errors)


def test_dashboard_closes_main_and_keeps_dialogs_outside_it(client):
    """The specific regression: <main> unclosed, so dialogs lived inside it."""
    body = client.get("/dashboard").text
    assert body.count("<main") == 1 and "</main>" in body, "dashboard has no closed <main>"
    main = body[body.index("<main"):body.index("</main>")]
    for dialog in ('id="node-modal"', 'id="output-modal"'):
        assert dialog not in main, f"{dialog} is inside <main>; a dialog covers the page"


@pytest.mark.parametrize("route", STATIC_ROUTES)
def test_every_page_has_landmarks_and_a_title(client, route):
    body = client.get(route).text
    assert "<title>" in body, f"{route} has no title"
    assert "<main" in body, f"{route} has no main landmark"
    assert 'lang="en"' in body, f"{route} does not declare a language"
    assert "viewport" in body, f"{route} has no viewport meta, so it renders zoomed out on a phone"


def test_dashboard_has_a_skip_link_pointing_at_main(client):
    body = client.get("/dashboard").text
    assert 'class="skip-link"' in body
    assert 'href="#main"' in body
    assert 'id="main"' in body, "the skip link points at nothing"


def test_dashboard_marks_the_active_section(client):
    """aria-current is how a screen reader answers 'where am I'."""
    body = client.get("/dashboard").text
    assert body.count('aria-current="page"') >= 1


def test_live_regions_are_announced(client):
    """A log that updates silently is invisible to a screen reader."""
    body = client.get("/dashboard").text
    assert 'id="event-log"' in body and "aria-live" in body
    log = body[body.index('id="event-log"'):body.index('id="event-log"') + 220]
    assert "aria-live" in log, "the activity log does not announce updates"


def test_no_inline_event_handlers_anywhere(client):
    """An onclick on a div is not keyboard-operable.

    Every control that used to carry one is now a real button or link with a
    listener bound in _dashboard.js.
    """
    for route in STATIC_ROUTES:
        body = client.get(route).text
        for handler in ("onclick=", "onmouseover=", "onmouseout=", "oninput="):
            assert handler not in body, f"{route} still uses an inline {handler[:-1]}"


def test_pages_honour_reduced_motion():
    """index.html has always done this; the dashboard is the page with the
    pulsing cards, the shimmer and the credit flash, so it matters more."""
    for name in ("_dashboard.css", "index.html"):
        assert "prefers-reduced-motion" in (TEMPLATES / name).read_text(encoding="utf-8"), (
            f"{name} does not honour prefers-reduced-motion"
        )


def test_try_page_is_usable_on_a_phone():
    """/try is the page most likely to be opened from a link in a post."""
    src = (TEMPLATES / "try.html").read_text(encoding="utf-8")
    assert "@media" in src, "try.html has no media queries at all"
    assert "aria-live" in src, "run status updates are not announced"
    assert "<main" in src, "no main landmark"


def test_try_textarea_uses_a_surface_token_not_a_border_token():
    """--border-subtle as a background fill is a token misuse: it is defined as
    a hairline colour, so it renders as a barely-there tint and drifts the
    moment borders are retuned."""
    src = (TEMPLATES / "try.html").read_text(encoding="utf-8")
    block = src[src.index("textarea {"):src.index("}", src.index("textarea {"))]
    assert "--border-subtle" not in block, "textarea fills its background with a border token"
    assert "--surface" in block, "textarea has no surface token for its fill"
