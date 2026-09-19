"""The node map's three scales, and the honesty contract the drawn two carry.

Live is measured, from /nodes. The other two — one run at its widest, and three
runs in a field of 125 — are drawn: pictures of how work spreads, not data. A
picture that starts being read as a measurement is the whole risk, so what is
pinned here is what a later edit could quietly undo:

* every panel on a drawn scale ends with a DATA row that says so, and no drawn
  panel ever claims to be live;
* the drawn scales carry no points, no credits, no task counts and no rates —
  those mean something specific in the ledger, and an invented one would be
  indistinguishable from an earned one;
* the caption under a drawn scale is the warning colour, not the quiet one;
* the numbers the captions argue from are this repo's numbers: the breaker's
  three failures and 60 seconds, and the 3-5 subtasks the planner is asked for;
* the switcher opens on live, so nobody lands on a drawing by accident.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import server_state

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
JS = (TEMPLATES / "_dashboard.js").read_text(encoding="utf-8")
CSS = (TEMPLATES / "_dashboard.css").read_text(encoding="utf-8")
HTML = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")


def _function(name: str) -> str:
    """The body of one top-level function in _dashboard.js."""
    start = JS.index(f"function {name}(")
    end = JS.index("\n}\n", start)
    body = JS[start:end]
    assert len(body) > 400, f"{name} looks empty — the marker moved"
    return body


DRAWN = _function("demoModel") + _function("swarmModel")


def test_every_panel_on_a_drawn_scale_says_it_is_drawn():
    # Each inspectable subject carries a rows: list, and each rows: list ends
    # with the DATA row. Counting them together is what makes a new subject
    # added without one fail rather than pass quietly.
    rows = len(re.findall(r"\brows: \[", DRAWN))
    data = re.findall(r"\['DATA', ([^\]]+)\]", DRAWN)
    assert rows >= 6, "the drawn scales lost their inspectable subjects"
    assert len(data) == rows, f"{rows} panels but {len(data)} DATA rows"
    for value in data:
        assert "live" not in value, f"a drawn panel claims to be live: {value}"
        assert "simulated" in value or "drawn, not measured" in value or "you are here" in value, value


def _drawn_words() -> str:
    """Every quoted literal in the drawn models, which is all a reader sees.

    Scanning the source itself would match `i % 37` and pass on nothing.
    """
    words = " ".join(re.findall(r"'([^'\\]*)'", DRAWN)).lower()
    assert "circuit open" in words and "holding nothing" in words, "the literals stopped being found"
    return words


def test_the_drawn_scales_invent_no_points_and_no_rates():
    banned = ["points", "credits", "credit", "tasks completed", "percent", "success rate", "uptime", "average"]
    words = _drawn_words()
    for word in banned:
        assert word not in words, f"a drawn scale carries {word!r}"
    # A rate or a ledger unit on a drawn bubble would be indistinguishable from
    # an earned one, which is the one thing this map must never show.
    assert not re.search(r"\d+\s*%", words), "a drawn scale shows a rate"
    assert not re.search(r"\d+\s*(pts|points)\b", words)


def test_the_caption_under_a_drawn_scale_is_the_warning_colour():
    assert ".nodemap-note.is-drawn { color: var(--warn-text); }" in CSS
    assert "note.classList.toggle('is-drawn', _nodemapScale !== 'live')" in JS


@pytest.mark.parametrize("scale", ["live", "demo", "swarm"])
def test_each_scale_has_its_own_caption_and_legend(scale):
    text = JS[JS.index("const NODEMAP_TEXT = {"):]
    text = text[: text.index("\n};")]
    block = text[text.index(f"  {scale}: {{"):]
    for field in ("keys:", "hint:", "note:"):
        assert field in block.split("\n  },")[0], f"{scale} has no {field}"


def test_the_switcher_opens_on_the_measured_scale():
    seg = HTML[HTML.index('class="seg nodemap-scale"'):]
    seg = seg[: seg.index("</div>")]
    checked = re.findall(r'aria-checked="(\w+)"[^>]*data-nodemap-scale="(\w+)"', seg)
    assert checked == [("true", "live"), ("false", "demo"), ("false", "swarm")], checked
    assert "drawn rather than measured" in seg, "the drawn scales are not labelled as drawn"


def test_the_swarm_caption_uses_this_repo_s_circuit_breaker():
    assert server_state._FAILURE_THRESHOLD == 3
    assert server_state._BLACKLIST_DURATION == 60
    claim = "Three consecutive failures, so /tasks/next answers 429 for 60s"
    assert claim in JS, "the drawn caption no longer matches server_state"


def test_the_ceiling_the_drawn_scales_argue_from_is_the_planner_s():
    planner = (ROOT / "prompts" / "v2.py").read_text(encoding="utf-8")
    assert "3-5 subtasks" in planner
    # Asked for, not guaranteed: the map may not turn a ceiling into a count.
    for phrase in ("is asked for 3-5 subtasks", "asked for 3-5 subtasks"):
        assert phrase in JS
    assert "always splits into 5" not in JS and "always five" not in JS


def test_a_drawn_scale_never_links_a_bubble_to_a_real_machine_page():
    # page: is what makes a mark a link to /node/{id}. A drawn machine has no
    # page, and inventing one would 404 at best and mislead at worst.
    assert "page:" not in DRAWN
    assert "drawn: true" in DRAWN


def test_the_drawn_models_are_deterministic():
    # No Math.random anywhere: a bubble must read the same on every paint, or
    # two people looking at the same map would be arguing about different ones.
    assert "Math.random" not in JS
