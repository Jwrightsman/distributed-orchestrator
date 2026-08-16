"""How the dashboard behaves when you click it.

Three regressions these pin:

1. Four native `prompt()` calls, one of them directly on the share path — a
   blocking grey box with the origin printed above it, which reads as a
   hijacked page rather than a feature, and which freezes the tab while a
   pipeline runs behind it.

2. The whole dashboard was one URL. showTab() swapped a div and never touched
   the address bar, so no view could be linked, bookmarked or refreshed into,
   and Back left the page entirely.

3. Loading, empty and error were one state. A panel mid-fetch looked exactly
   like a network with nothing in it, and a failed fetch looked like an empty
   one — silently, because every loader swallowed its exception.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
JS = (TEMPLATES / "_dashboard.js").read_text(encoding="utf-8")
CSS = (TEMPLATES / "_dashboard.css").read_text(encoding="utf-8")
HTML = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")

VIEWS = ("overview", "runs", "gallery", "nodes", "projects", "guild")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ── 1. No native dialogs ─────────────────────────────────────────────

def test_no_native_dialogs_anywhere():
    """prompt(), alert() and confirm() all block the page and cannot be
    styled. The clipboard fallback is the one that mattered most: it sat on
    the share path, which is the thing a stranger is most likely to do."""
    import re
    code = re.sub(r"//.*", "", JS)          # the comments explain the removal
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    for call in ("prompt(", "alert(", "confirm("):
        assert call not in code, f"a native {call[:-1]}() is back"


def test_one_shared_feedback_helper():
    """Every action confirmed itself differently before — some flashed a
    button, some did nothing at all."""
    assert "function notify(" in JS
    assert "function copyToClipboard(" in JS
    # And nothing hand-rolls its own toast any more.
    assert JS.count("class = 'toast'") + JS.count('className = "toast"') <= 1


def test_a_blocked_clipboard_degrades_to_something_selectable():
    """Plain HTTP has no clipboard API, which is exactly how this server is
    served — so the fallback is the common path, not the rare one."""
    assert "toast-field" in JS and "toast-field" in CSS
    assert "input.select()" in JS, "the fallback text is not pre-selected"


def test_naming_a_project_is_an_inline_form():
    for part in ('id="new-project-form"', 'id="new-project-name"',
                 'id="new-project-submit"', 'id="new-project-cancel"',
                 'id="new-project-error"'):
        assert part in HTML, f"missing {part}"
    assert 'aria-expanded="false"' in HTML
    assert 'role="alert"' in HTML, "the form's error is not announced"


# ── 2. Real URLs ─────────────────────────────────────────────────────

@pytest.mark.parametrize("view", VIEWS)
def test_every_view_has_its_own_url(client, view):
    r = client.get(f"/dashboard/{view}")
    assert r.status_code == 200, f"/dashboard/{view} does not resolve"
    assert "<title>" in r.text


def test_an_open_run_is_part_of_the_url(client):
    assert client.get("/dashboard/runs/20260814_040809").status_code == 200


def test_an_unknown_view_is_a_404_not_a_blank_dashboard(client):
    assert client.get("/dashboard/nonsense").status_code == 404


def test_history_is_pushed_so_back_works():
    """replaceState leaves Back pointing at whatever came before the page."""
    assert "history.pushState" in JS, "navigation does not add a history entry"
    assert "addEventListener('popstate'" in JS, "Back is not handled"


def test_legacy_hash_links_still_resolve():
    """/dashboard#gallery and #run=<id> were the only deep links that existed;
    they are rewritten to paths rather than broken."""
    assert "run=(.+)" in JS or "^run=" in JS
    assert "legacy: true" in JS


# ── 3. Loading, empty and error are three things ─────────────────────

def test_lists_show_a_skeleton_while_loading():
    assert "function skeleton(" in JS
    assert ".skel-line" in CSS
    for loader in ("loadHistory", "loadGallery", "loadStandings", "loadProjects"):
        body = JS[JS.index(f"async function {loader}("):]
        body = body[:body.index("\n}\n")]
        assert "skeleton(" in body, f"{loader} has no loading state"


def test_every_empty_state_offers_the_action_that_resolves_it():
    assert "function emptyState(" in JS
    assert "EMPTY_ACTIONS" in JS
    for action in ("focus-pitch", "new-project", "clear-search", "copy-join"):
        assert f"'{action}'" in JS, f"no handler for the {action} empty state"


def test_a_failed_fetch_says_so_and_offers_a_retry():
    """Every loader used to `catch (e) {}` — a dead server and an empty one
    were indistinguishable, and neither said anything."""
    assert "function errorState(" in JS
    assert "RETRIES" in JS
    for loader in ("loadHistory", "loadGallery", "loadStandings", "loadProjects"):
        body = JS[JS.index(f"async function {loader}("):]
        body = body[:body.index("\n}\n")]
        assert "errorState(" in body, f"{loader} fails silently"
        assert "catch (e) {}" not in body, f"{loader} still swallows its error"


def test_a_dropped_live_feed_is_visible(client):
    """Silence was the only signal, and a quiet network looks the same."""
    assert 'id="conn-banner"' in client.get("/dashboard").text
    assert "setConnectionState" in JS
    assert "conn-banner" in CSS


def test_skeletons_respect_reduced_motion():
    block = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".skel-line" in block, "the loading shimmer ignores reduced motion"


# ── The in-dashboard preview stayed reachable ────────────────────────

def test_a_run_can_be_previewed_without_leaving_the_dashboard():
    """Both paths exist on purpose: the title is the shareable page, Preview
    keeps your place. A link nested in a link would be neither."""
    assert "data-preview" in JS
    assert "function openRun(" in JS
    assert '/run/${encodeURIComponent(r.timestamp)}' in JS, "no permalink on a history row"


# ── 4. Keyboard ──────────────────────────────────────────────────────

def test_the_command_palette_exists_and_is_a_dialog(client):
    body = client.get("/dashboard").text
    assert 'id="palette"' in body
    assert 'role="combobox"' in body, "the palette input is not announced as one"
    assert 'role="listbox"' in body and 'aria-controls="palette-list"' in body


def test_the_palette_is_built_from_the_same_actions_the_mouse_uses():
    """Two lists of what the app can do drift apart; one does not."""
    assert "function baseCommands(" in JS
    for cmd in ("focusPitch()", "openNewProjectForm()", "toggleTheme()", "copyToClipboard("):
        assert cmd in JS[JS.index("function baseCommands("):JS.index("let _paletteItems")], (
            f"the palette does not reuse {cmd}"
        )


def test_recent_runs_are_reachable_from_the_palette():
    block = JS[JS.index("async function openPalette("):JS.index("function closePalette(")]
    assert "/history?limit=" in block, "the palette cannot open a recent run"
    assert "openRun(" in block


@pytest.mark.parametrize("key,view", [("o", "overview"), ("r", "runs"), ("g", "gallery"),
                                      ("n", "nodes"), ("p", "projects"), ("u", "guild")])
def test_g_then_key_switches_view(key, view):
    block = JS[JS.index("const GO_KEYS"):JS.index("/** The commands")]
    assert f"{key}: '{view}'" in block, f"g {key} does not go to {view}"


def test_shortcuts_never_fire_while_typing():
    """`/` inside the pitch box has to be a slash."""
    assert "function isTyping(" in JS
    handler = JS[JS.index("document.addEventListener('keydown', (e) => {"):]
    assert "if (typing) return;" in handler[:2000], "a shortcut can steal a keystroke from a field"


def test_the_shortcut_reference_lists_every_shortcut(client):
    assert 'id="shortcuts"' in client.get("/dashboard").text
    block = JS[JS.index("const SHORTCUTS = ["):JS.index("const GO_KEYS")]
    for key in ("⌘K", "/", "?", "Esc", "g"):
        assert key in block, f"{key} is not documented in the reference"


# ── 6. Elapsed time ──────────────────────────────────────────────────

def test_one_elapsed_helper_rather_than_a_copy_per_caller():
    assert "function startElapsed(" in JS and "function formatElapsed(" in JS
    # The pipeline card's hand-rolled interval is gone.
    assert "const elapsedTicker = setInterval" not in JS


def test_a_busy_node_shows_how_long_it_has_been_building():
    """40–330 seconds per subtask on this hardware: a card that says only
    "building" cannot be told from one that has wedged."""
    block = JS[JS.index("function _setNodeBusy("):JS.index("function _setNodeIdle(")]
    assert "startElapsed(" in block
    assert "node-elapsed" in CSS


def test_the_node_clock_stops_when_the_node_goes_idle():
    """A ticking interval per node, never cleared, is a leak on a page people
    leave open for hours."""
    block = JS[JS.index("function _setNodeIdle("):JS.index("function _setNodeBlacklisted(")]
    assert "_nodeClocks[nodeId]?.()" in block and "delete _nodeClocks[nodeId]" in block


def test_the_try_page_shows_elapsed_time():
    """It is the page with the longest wait and the least to look at."""
    src = (TEMPLATES / "try.html").read_text(encoding="utf-8")
    assert "elapsed" in src and "stopClock" in src
