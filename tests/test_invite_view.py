"""The Connect view, and the source each of its sentences stands on.

`docs/design/HANDOFF-DELTA.md` §11 is the design pass this holds. What is
asserted here is the part a later edit could quietly undo:

* no credential value, and no piece of one, reaches the response;
* the consent list still stands on the installer's own paragraph;
* the tools listed are the tools the MCP server registers, both directions;
* the MCP server still sends a viewer key, and the routes it reads with it
  are still private, which is why the client panel names two levers;
* the warning panel follows the settings rather than the design's two rows;
* the route is operator-prefixed, and refused without a viewer key.
"""

from __future__ import annotations

import ast
import re
import secrets

import pytest
from fastapi.testclient import TestClient

import config
import invite_view
import worker_installer
from access_control import _PUBLIC_EXACT, is_public_or_separately_authenticated
from server import app
from tests.test_console_language import (
    BANNED_PATTERNS,
    BANNED_PHRASES,
    CREDITS_AS_A_UNIT,
    QUALITY_PERCENTAGE,
    _WORD,
    visible_text,
)
from tests.test_config_view import ROOT, TEMPLATES
from tests.test_templates import _check_balance

ROUTE = "/v1/operator/invite"
LONG = "k" * 40


def _settings(**overrides) -> dict:
    settings = dict(config.DEFAULTS)
    settings.update(overrides)
    return settings


def _fragment(**overrides) -> str:
    return invite_view.render(invite_view.build_record(_settings(**overrides)))


# ── credentials ──────────────────────────────────────────────────────


def test_no_piece_of_any_credential_reaches_the_response(monkeypatch):
    live = config.get()
    planted = {key: f"{key}-{secrets.token_hex(24)}" for key in ("viewer_key", "node_secret", "pitch_key")}
    for key, value in planted.items():
        monkeypatch.setitem(live, key, value)
    with TestClient(app) as client:
        response = client.get(ROUTE, headers={"X-Viewer-Key": planted["viewer_key"]})
    assert response.status_code == 200, response.text[:200]
    body = response.text
    # The planted values did reach the view's input, or this proves nothing.
    assert response.json()["authorities"] == {key: "set" for key in planted}
    for name, value in planted.items():
        pieces = {value[i:i + 6] for i in range(len(name) + 1, len(value) - 5)}
        found = sorted(piece for piece in pieces if piece in body)
        assert not found, f"part of {name} is in the response: {found[:3]}"


# ── tied to source ───────────────────────────────────────────────────


def _installer_paragraph() -> str:
    text = worker_installer.consent_text("https://example.test", "qwen3.5:4b",
                                         ROOT / "identity.json")
    text = re.sub(r"\[/?[a-z ]+\]", "", text)   # rich markup
    return re.sub(r"\s+", " ", text)


def test_every_consent_item_stands_on_the_installers_own_words():
    paragraph = _installer_paragraph()
    for _, text, phrases in invite_view.CONSENT:
        assert phrases, text
        for phrase in phrases:
            assert phrase in paragraph, (
                f"worker_installer.consent_text no longer says {phrase!r}, so the "
                f"Connect view's {text!r} has nothing under it"
            )


def test_the_installer_has_no_flag_that_skips_its_consent():
    """The consent panel says the installer has no way past the prompt."""
    parser = worker_installer.build_parser()
    yes = next(action for action in parser._actions if "--yes" in action.option_strings)
    assert "uninstall only" in yes.help.lower()
    source = (ROOT / "worker_installer.py").read_text(encoding="utf-8")
    confirm = source[source.index("def confirm("):source.index("def read_invitation(")]
    assert "yes" in confirm and "args" not in confirm


def test_the_join_commands_are_the_ones_docs_join_gives():
    join = (ROOT / "docs" / "JOIN.md").read_text(encoding="utf-8")
    for line in invite_view.JOIN_COMMANDS:
        assert line in join, line


def _registered_tools() -> set[str]:
    tree = ast.parse((ROOT / "mcp_server.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    names.add(node.name)
    # `server.tool()(fn)` registration, if the module uses that form instead.
    for call in ast.walk(tree):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Call)
                and isinstance(call.func.func, ast.Attribute)
                and call.func.func.attr in ("tool", "add_tool")):
            names.update(arg.id for arg in call.args if isinstance(arg, ast.Name))
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == "add_tool"):
            names.update(arg.id for arg in call.args if isinstance(arg, ast.Name))
    return names


def test_the_tools_listed_are_the_tools_the_mcp_server_registers():
    registered = _registered_tools()
    assert registered, "found no registered tool at all, so the comparison proves nothing"
    assert {name for name, _ in invite_view.MCP_TOOLS} == registered


def test_an_app_needs_the_viewer_key_to_read_back_what_it_pitched():
    """Why the client panel names two levers. If either half stops being true
    the panel overstates what an app holds."""
    source = (ROOT / "mcp_server.py").read_text(encoding="utf-8")
    assert 'headers["X-Viewer-Key"] = VIEWER_KEY' in source
    for path in ("/jobs/some-job", "/projects"):
        assert not is_public_or_separately_authenticated("GET", path), path
    assert "VIEWER_KEY" in (ROOT / "docs" / "MCP.md").read_text(encoding="utf-8")


def test_the_rate_is_read_from_settings():
    assert invite_view.build_record(_settings())["rate"] == "5 pitches a minute, per address"
    record = invite_view.build_record(_settings(pitch_rate_max=1, pitch_rate_window=3600))
    assert record["rate"] == "1 pitch per 1 hour, per address"


# ── the warning follows the settings ─────────────────────────────────


def test_defaults_warn_about_every_open_authority_and_the_two_declarations():
    fragment = _fragment()
    share = fragment[fragment.index('data-invite="share"'):fragment.index('data-invite="status"')]
    for key in ("viewer_key", "node_secret", "pitch_key", "https_enabled", "node_enrollment_mode"):
        assert f'<span class="iv-warn-key">{key}</span>' in share, key
    assert "is-warn" in fragment[fragment.index('data-invite="share"') - 60:fragment.index('data-invite="share"')]
    assert 'data-tab="config"' in share


def test_a_short_key_is_named_as_short_not_as_open():
    fragment = _fragment(viewer_key="short", node_secret=LONG + "n", pitch_key=LONG + "p")
    share = fragment[fragment.index('data-invite="share"'):fragment.index('data-invite="status"')]
    assert "viewer_key</span><span class=\"iv-warn-what\">— shorter than" in share
    assert "anyone can read" not in share


def test_a_coordinator_ready_to_share_says_so_and_warns_about_nothing():
    fragment = _fragment(viewer_key=LONG + "v", node_secret=LONG + "n", pitch_key=LONG + "p",
                         https_enabled=True, node_enrollment_mode="required")
    share = fragment[fragment.index('data-invite="share"'):fragment.index('data-invite="status"')]
    assert "data-share-ready" in share
    assert "iv-warn-key" not in share
    client = fragment[fragment.index('data-invite="client"'):]
    assert "Every machine on this network has its own enrolment" in client
    assert "compat" not in visible_text(client)


# ── the fragment's rules ─────────────────────────────────────────────


@pytest.fixture(params=["defaults", "all_set", "mixed"])
def any_fragment(request):
    return _fragment(**{
        "defaults": {},
        "all_set": {"viewer_key": LONG + "v", "node_secret": LONG + "n", "pitch_key": LONG + "p",
                    "https_enabled": True, "node_enrollment_mode": "required"},
        "mixed": {"viewer_key": "short", "pitch_key": LONG, "pitch_rate_window": 90},
    }[request.param])


def test_the_fragment_is_balanced(any_fragment):
    assert _check_balance(any_fragment) == []


def test_the_fragment_carries_no_prohibited_language(any_fragment):
    text = visible_text(any_fragment)
    lowered = text.lower()
    for phrase in BANNED_PHRASES:
        assert phrase not in lowered
    for pattern in _WORD.values():
        assert not pattern.search(text), pattern.search(text).group(0)
    for pattern, description in BANNED_PATTERNS:
        assert not pattern.search(text), f"{description}: {pattern.search(text).group(0)!r}"
    assert not QUALITY_PERCENTAGE.search(text)
    assert not CREDITS_AS_A_UNIT.search(text)
    # The design's consent list said this, and §7 of the handoff forbids it.
    assert "no api keys" not in lowered and "no cloud" not in lowered


def test_no_hyphenated_word_outside_a_command_is_left_breakable(any_fragment):
    """Commands do not wrap; they scroll. Everything else must not end a line
    on a hyphen, which is the fault Config and Evals both shipped once."""
    prose = re.sub(r'<div class="iv-cmd-lines">.*?</div></div>', " ", any_fragment, flags=re.S)
    assert re.findall(r"\w+-\w[\w-]*", visible_text(prose)) == []


def test_nothing_in_the_connect_styles_renders_below_11px():
    css = (TEMPLATES / "_dashboard.css").read_text(encoding="utf-8")
    block = css[css.index("/* ── Connect"):]
    sizes = [float(size) for size in re.findall(r"font-size:\s*([0-9.]+)px", block)]
    assert sizes and min(sizes) >= 11, sizes


# ── the route, its gate, and the console ─────────────────────────────


def test_the_route_is_operator_prefixed_and_refused_without_a_key(monkeypatch):
    assert ("GET", ROUTE) not in _PUBLIC_EXACT
    monkeypatch.setitem(config.get(), "viewer_key", LONG)
    with TestClient(app) as client:
        refused = client.get(ROUTE)
        assert refused.status_code == 401
        assert "invite_html" not in refused.text
        assert client.get(ROUTE, headers={"X-Viewer-Key": LONG}).status_code == 200


def test_the_handler_refuses_on_its_own_without_the_middleware(monkeypatch):
    from fastapi import FastAPI

    import routes_invite

    monkeypatch.setitem(config.get(), "viewer_key", LONG)
    bare = FastAPI()
    bare.include_router(routes_invite.router)
    with TestClient(bare) as client:
        assert client.get(ROUTE).status_code == 401


def test_the_console_has_a_connect_view_that_asks_when_opened():
    html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    js = (TEMPLATES / "_dashboard.js").read_text(encoding="utf-8")
    assert 'id="tab-invite"' in html and 'id="view-invite"' in html and 'id="invite-body"' in html
    assert re.search(r"const TABS = \[[^\]]*'invite'", js)
    assert "if (name === 'invite') loadInvite();" in js
    loader = js[js.index("async function loadInvite()"):]
    loader = loader[: loader.index("\n}\n")]
    assert f"apiJson('{ROUTE}')" in loader and "data.invite_html" in loader
    assert "setInterval(loadInvite" not in js
    assert "[data-copy]" in js


def test_no_console_hint_tells_anyone_to_join_over_plain_http():
    """The Nodes empty state used to say `python join.py http://YOUR_IP:8000`,
    which join.py refuses for any host that is not loopback."""
    for page in ("dashboard.html", "_dashboard.js"):
        source = (TEMPLATES / page).read_text(encoding="utf-8")
        assert "join.py" not in source, page
