"""The console shell from the Sept 18 design: rail, top bar, identity block.

`docs/design/console-2026-09-18/spec/HANDOFF-UI.md` §15-18 are the source.
What is asserted is what a later edit could quietly undo:

* a fresh session lands on the collapsed rail, and only a remembered choice
  opens it (§15.4);
* every rail item is named for a screen reader and a pointer in the collapsed
  state, where the icon is all there is (§15.4);
* the identity block claims nothing the repo cannot back: the version is the
  code's, there is no "up to date" without a release feed, no sign-out
  without a session (§18);
* the bar's drawer says what each configuration value means, in sentences
  about this repo's behaviour rather than the design file's (§17.1).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard
from server import app
from worker_protocol import SERVER_VERSION

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


class _NavItems(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "button" and "nav-item" in (a.get("class") or "").split():
            self.items.append(a)


def test_every_rail_item_is_named_when_only_its_icon_shows(client):
    parser = _NavItems()
    parser.feed(client.get("/dashboard").text)
    assert len(parser.items) >= 9, "found too few rail items for this to mean anything"
    for item in parser.items:
        assert item.get("title"), f"{item.get('id')} has no hover description"
        assert item.get("aria-label"), f"{item.get('id')} has no name for a screen reader"


def test_a_fresh_session_lands_on_the_collapsed_rail():
    html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    assert "rail-open" not in html, "the markup opens the rail before any choice was remembered"
    js = (TEMPLATES / "_dashboard.js").read_text(encoding="utf-8")
    assert "setRail(localStorage.getItem('mycelium-rail') === '1');" in js
    css = (TEMPLATES / "_dashboard.css").read_text(encoding="utf-8")
    nav = css[css.index("\n.nav {"):]
    assert "width: 58px;" in nav[: nav.index("}")], "the rail's resting width is not the collapsed one"


def test_the_identity_block_names_the_codes_version_and_claims_no_more(client):
    body = client.get("/dashboard").text
    menu = body[body.index('id="ident"'):body.index('id="ident-toggle"')]
    assert f"Version <span class=\"mono\">{SERVER_VERSION}</span>" in menu
    text = re.sub(r"<[^>]+>", " ", menu).lower()
    assert "up to date" not in text, "there is no release feed for that to be checked against"
    assert "sign out" not in text and "log out" not in text, "there is no session to end"
    assert "has no accounts" in text


def test_the_identity_trigger_keeps_a_44px_target():
    css = (TEMPLATES / "_dashboard.css").read_text(encoding="utf-8")
    block = css[css.index(".ident-trigger {"):]
    assert "min-height: 44px;" in block[: block.index("}")]


@pytest.mark.parametrize("viewer_key,word", [("", "not set"), ("k" * 40, "set")])
def test_the_viewer_key_row_is_a_word_never_a_value(monkeypatch, viewer_key, word):
    import config
    monkeypatch.setitem(config.get(), "viewer_key", viewer_key)
    rows = dashboard.identity_at_load()
    assert rows["IDENT_VIEWER_META"] == word
    if viewer_key:
        assert all(viewer_key[:6] not in value for value in rows.values())


def test_the_drawer_sentences_describe_this_repo():
    """The design's drawer copy described a different system. The evidence
    sentence is the one with a rule behind it: shadow evidence never routes."""
    shadow = dashboard._EVIDENCE_WHY["shadow"].lower()
    assert "never" in shadow and "eligible" in shadow
    assert set(dashboard._TRACING_WHY) == {"off", "propagating", "exporting"}
    html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    drawer = html[html.index('id="statusbar-drawer"'):html.index('class="statusbar-severity"')]
    assert "/api/status" not in drawer, "no such endpoint; the bar polls /health"
    assert "/health" in drawer
