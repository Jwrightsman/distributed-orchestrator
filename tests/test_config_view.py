"""The Config view, and the credentials it never carries.

`docs/design/HANDOFF-DELTA.md` §10 is the design this holds. What is asserted
here is the part a later edit could quietly undo:

* no credential value, and no piece of one, reaches the response;
* every key in `config.DEFAULTS` is placed on the page exactly once, so a new
  setting cannot arrive unclassified;
* an address shows its origin and nothing after it;
* the keys the view says nothing reads are the keys a scan finds no reader
  for, in both directions;
* the durable and the lost lists are tied to the tables and the backup
  manifest that make them true;
* the route is operator-prefixed, and refused without a viewer key.
"""

from __future__ import annotations

import ast
import re
import secrets
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import config_view
from access_control import _PUBLIC_EXACT
from server import app
from tests.test_console_language import (
    BANNED_PATTERNS,
    BANNED_PHRASES,
    CREDITS_AS_A_UNIT,
    QUALITY_PERCENTAGE,
    _WORD,
    visible_text,
)
from tests.test_templates import _check_balance

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
ROUTE = "/v1/operator/config"


def _settings(**overrides) -> dict:
    settings = dict(config.DEFAULTS)
    settings.update(overrides)
    return settings


def _record(runtime=None, **overrides) -> dict:
    return config_view.build_record(_settings(**overrides), runtime or {})


def _rows(record: dict) -> dict[str, dict]:
    return {row["key"]: row for group in record["groups"] for row in group["rows"]}


def _token() -> str:
    return secrets.token_urlsafe(36)


def _pieces(value: str, width: int = 6) -> set[str]:
    return {value[i:i + width] for i in range(len(value) - width + 1)}


LONG = "k" * config.MIN_STATIC_CREDENTIAL_LENGTH


# ── credentials ──────────────────────────────────────────────────────


@pytest.fixture
def leaky(monkeypatch):
    """Every credential and every credential-shaped place set to a fresh random value."""
    live = config.get()
    planted: dict[str, str] = {}

    def plant(name: str) -> str:
        planted[name] = _token()
        return planted[name]

    for key in config_view.AUTHORITIES:
        monkeypatch.setitem(live, key, plant(key))
    monkeypatch.setitem(live, "provider", "openai")
    monkeypatch.setitem(live, "provider_model", "m")
    monkeypatch.setitem(live, "provider_api_key", plant("provider_api_key"))
    monkeypatch.setitem(
        live, "provider_base_url",
        f"https://{plant('user')}:{plant('password')}@api.example.com/{plant('path')}"
        f"?token={plant('query')}#{plant('fragment')}",
    )
    monkeypatch.setitem(live, "ollama_url", f"http://{plant('ollama_user')}@10.0.0.5:11434/")
    monkeypatch.setitem(live, "tracing_endpoint", f"https://collector.example.com/v1/{plant('otlp')}")
    monkeypatch.setitem(live, "viewer_kye", plant("misspelt_viewer_key"))
    return planted


def test_no_piece_of_any_credential_reaches_the_response(leaky):
    """The whole response, record and fragment, as bytes on the wire."""
    with TestClient(app) as client:
        response = client.get(ROUTE, headers={"X-Viewer-Key": leaky["viewer_key"]})
    assert response.status_code == 200, response.text[:200]
    body = response.text
    # The planted values did reach the view's input, or this proves nothing.
    assert '"viewer_key": "set"' in body.replace('":"', '": "')
    assert "viewer_kye" in body
    assert "api.example.com" in body and "10.0.0.5" in body
    for name, value in leaky.items():
        found = sorted(piece for piece in _pieces(value) if piece in body)
        assert not found, f"part of {name} is in the response: {found[:3]}"


@pytest.mark.parametrize(
    ("value", "word"),
    [("", "off"), (None, "off"), ("short", "set · short"), ("   ", "set · short"),
     (LONG, "set"), (12345, "set · short")],
)
def test_an_authority_becomes_one_of_three_words(value, word):
    assert config_view.credential_state("pitch_key", value) == word
    row = _rows(_record(pitch_key=value))["pitch_key"]
    assert row["value"] == word
    assert config_view.credential_state("provider_api_key", value) in ("off", "set")


def test_every_credential_row_prints_only_a_state_word():
    words = {"off", "set", "set · short"}
    for overrides in ({}, {k: LONG + k for k in config_view.CREDENTIAL_KEYS},
                      {k: "x" for k in config_view.CREDENTIAL_KEYS}):
        rows = _rows(_record(**overrides))
        for key in config_view.CREDENTIAL_KEYS:
            assert rows[key]["value"] in words, (key, rows[key]["value"])
            assert rows[key]["hint"] is None


def test_a_key_that_looks_like_a_credential_is_classified_as_one():
    """A new `*_key` or `*_secret` in config is a credential until someone says
    otherwise here, and an address-shaped key is an address."""
    for key in config.DEFAULTS:
        if re.search(r"(key|secret|password|credential|token)$", key):
            assert key in config_view.CREDENTIAL_KEYS, key
        if re.search(r"(url|endpoint|uri)$", key):
            assert key in config_view.ADDRESS_KEYS, key


def test_shared_authorities_are_named_never_valued():
    same = LONG + "same"
    record = _record(viewer_key=same, pitch_key=same, node_secret=LONG + "other")
    rows = _rows(record)
    assert "same value as" in rows["viewer_key"]["note"]
    assert "pitch_key" in rows["viewer_key"]["note"]
    assert rows["viewer_key"]["tone"] == "warn"
    assert "same value as" not in rows["node_secret"]["note"]
    # Compared stripped, as preflight compares them.
    assert config_view.shared_authorities(
        _settings(viewer_key=same, pitch_key=f" {same} ", node_secret="")
    ) == {"viewer_key": ["pitch_key"], "pitch_key": ["viewer_key"]}


@pytest.mark.parametrize(
    ("value", "shown", "trimmed"),
    [
        ("http://localhost:11434", "http://localhost:11434", False),
        ("http://localhost:11434/", "http://localhost:11434", False),
        ("https://api.x.ai/v1", "https://api.x.ai", True),
        ("https://u:p@host.example:8443/a?b=c#d", "https://host.example:8443", True),
        ("http://[::1]:11434", "http://[::1]:11434", False),
        ("not a url", "set · not shown", True),
        ("http://:99999/", "set · not shown", True),
        (42, "set · not shown", True),
        ("", "unset", False),
        (None, "unset", False),
    ],
)
def test_an_address_shows_its_origin_only(value, shown, trimmed):
    assert config_view.address_origin(value) == (shown, trimmed)


def test_unknown_keys_are_named_never_valued():
    record = _record(viewer_kye="value-that-must-not-show", extra_setting=7)
    assert record["unknown_keys"] == ["extra_setting", "viewer_kye"]
    fragment = config_view.render(record)
    assert "data-unknown-keys" in fragment
    assert "value-that-must-not-show" not in fragment
    assert "data-unknown-keys" not in config_view.render(_record())


# ── every key, placed once ───────────────────────────────────────────


def test_every_config_key_is_placed_exactly_once():
    placed = Counter(key for _, keys in config_view.GROUPS for key in keys)
    assert [key for key, count in placed.items() if count > 1] == []
    assert set(placed) == set(config.DEFAULTS), (
        "config.DEFAULTS and config_view.GROUPS disagree. A new setting has to be "
        "placed, and classified as a credential or an address if it is one, before "
        f"it can render: missing {sorted(set(config.DEFAULTS) - set(placed))}, "
        f"extra {sorted(set(placed) - set(config.DEFAULTS))}"
    )


def test_config_rows_are_keys_and_runtime_rows_are_not():
    for group in _record()["groups"]:
        for row in group["rows"]:
            if row["source"] == "config":
                assert row["key"] in config.DEFAULTS
            else:
                # Never spelt like a key, so it cannot be copied into config.json as one.
                assert row["key"] not in config.DEFAULTS and "_" not in row["key"]


def _tracked_python() -> list[Path]:
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"], cwd=ROOT,
            capture_output=True, check=True,
        ).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.fail(f"could not list tracked files, so the reader scan cannot run: {exc}")
    return [ROOT / name for name in listed.split("\0") if name]


def _readers(key: str, files: list[Path]) -> list[str]:
    pattern = re.compile(r"""(?:\bget\(\s*|\[\s*)["']""" + re.escape(key) + r"""["']""")
    return sorted(
        path.relative_to(ROOT).as_posix()
        for path in files
        if pattern.search(path.read_text(encoding="utf-8", errors="replace"))
    )


def test_the_keys_that_change_nothing_are_the_ones_nothing_reads():
    """The scan behind `INERT_KEYS`, repeated, over tracked source only.

    A reader is `.get("key"` or `["key"]` outside the tests, `config.py`, this
    view, and `status.py` — which prints settings for a person and is named in
    the rows that mention it. A looser match counted `"port"` in an unrelated
    diagnostic dict as a reader, which is why the pattern is a read.
    """
    files = [
        path for path in _tracked_python()
        if path.relative_to(ROOT).parts[0] != "tests"
        and path.name not in ("config.py", "config_view.py", "status.py")
    ]
    assert len(files) > 50, "the scan found almost no source; it proves nothing"
    # The pattern finds a reader where one is known to exist.
    assert "access_control.py" in _readers("viewer_key", files)
    assert "execution/artifacts.py" in _readers("artifact_retention_seconds", files)

    unread = {key for key in config.DEFAULTS if not _readers(key, files)}
    assert unread == set(config_view.INERT_KEYS), (
        f"nothing reads {sorted(unread)}, and the view says nothing reads "
        f"{sorted(config_view.INERT_KEYS)}"
    )
    status = [ROOT / "status.py"]
    assert _readers("port", status) and _readers("role_model_map", status)
    assert not _readers("tracing_endpoint", status)


def test_a_key_that_changes_nothing_says_so_and_no_other_row_does():
    rows = _rows(_record())
    for key in config_view.INERT_KEYS:
        assert rows[key]["hint"] in ("read by nothing", "not read by the server", "changes nothing")
    others = [k for k, row in rows.items()
              if k not in config_view.INERT_KEYS
              and row["hint"] in ("read by nothing", "not read by the server", "changes nothing")]
    assert others == []
    # The archived design described role_model_map as soft routing.
    assert not re.search(r"prefer|soft routing|matching model", rows["role_model_map"]["note"], re.I)


# ── the runtime rows ─────────────────────────────────────────────────


def test_last_backup_is_not_recorded_rather_than_never():
    """The archived design printed `never`. Nothing on the server can know."""
    row = _rows(_record())["last backup"]
    assert row["value"] == "not recorded"
    source = (ROOT / "scripts" / "backup.py").read_text(encoding="utf-8")
    assert "_reject_destination_inside_state(target, state_root)" in source, (
        "backup.py no longer refuses to write inside the state directory, so a "
        "backup may now leave a trace the server could read"
    )


def test_preflight_warnings_are_listed_and_escaped():
    record = _record({"preflight_warnings": ["<b>viewer_key</b> is disabled"], "lock_held": True})
    row = _rows(record)["preflight"]
    assert row["value"] == "1 warning" and row["tone"] == "warn"
    fragment = config_view.render(record)
    assert "&lt;b&gt;viewer_key&lt;/b&gt; is disabled" in fragment
    assert "<b>viewer_key</b>" not in fragment
    assert _rows(_record({"preflight_warnings": []}))["preflight"]["value"] == "no warnings"


def test_the_lock_row_keeps_its_date_on_one_line():
    """The date fault §8.11 recorded on the run page, in a new place."""
    record = _record({"lock_held": True, "instance_id": "abc", "pid": 7,
                      "started_at": "2026-09-12T21:08:55.1+00:00"})
    note = _rows(record)["coordinator lock"]["note"]
    assert '<span class="cf-nowrap">2026-09-12 21:08 UTC</span>' in note
    css = (TEMPLATES / "_dashboard.css").read_text(encoding="utf-8")
    assert re.search(r"\.cf-nowrap\s*\{\s*white-space:\s*nowrap;", css)


def test_the_runtime_rows_read_what_startup_recorded(monkeypatch):
    """Against what startup actually did, not against the attributes it left.

    Comparing a row to `app.state` compares the relay to itself: when the
    poison wrote a wrong directory there, and when it dropped the bind host,
    this test stayed green. So the directory is checked against the lock file
    the process really holds, and the host is forced through the launch path
    and looked for in the row.
    """
    from coordinator_lock import default_state_dir

    monkeypatch.setenv("MYCELIUM_BIND_HOST", "0.0.0.0")
    with TestClient(app) as client:
        body = client.get(ROUTE).json()
        state = client.app.state
        rows = {row["key"]: row for g in body["groups"] for row in g["rows"]}
        assert rows["coordinator lock"]["value"] == "held"
        assert state.coordinator_identity.instance_id in rows["coordinator lock"]["note"]
        assert rows["state directory"]["value"] == str(default_state_dir())
        assert (Path(rows["state directory"]["value"]) / ".mycelium-coordinator.lock").is_file()
        assert "0.0.0.0" in rows["bind_host"]["note"], rows["bind_host"]
        assert rows["preflight"]["value"] in (
            "no warnings", f"{len(state.preflight_warnings)} warning",
            f"{len(state.preflight_warnings)} warnings",
        )


def test_a_bind_host_the_launch_overrode_says_which_was_checked():
    row = _rows(_record({"bind_host": "0.0.0.0"}, bind_host="127.0.0.1"))["bind_host"]
    assert row["value"] == "127.0.0.1" and "0.0.0.0" in row["note"] and "A fallback" in row["note"]


# ── what survives a restart ──────────────────────────────────────────


def test_the_written_down_list_names_tables_that_exist():
    created = set()
    for path in _tracked_python():
        if path.relative_to(ROOT).parts[0] == "tests":
            continue
        created |= set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)",
                                  path.read_text(encoding="utf-8", errors="replace")))
    assert "executions" in created, "the table scan found nothing"
    for entry in _record()["written_down"]:
        assert entry["tables"], entry
        missing = [table for table in entry["tables"] if table not in created]
        assert not missing, f"{entry['item']!r} names tables no source creates: {missing}"


def test_the_gone_list_covers_everything_the_backup_leaves_out():
    tree = ast.parse((ROOT / "scripts" / "backup.py").read_text(encoding="utf-8"))
    not_included = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "not_included":
                    not_included = [element.value for element in value.elts]
    assert not_included, "read no not_included list from backup.py"
    covered = {phrase for entry in _record()["gone_on_restart"] for phrase in entry["covers"]}
    assert set(not_included) <= covered, set(not_included) - covered


def test_the_written_down_list_says_idempotency_keys_not_pitch_keys():
    """The archived design wrote "Pitch keys, by hash". `execution_submissions`
    holds idempotency keys; the pitch key is a credential and is stored nowhere."""
    items = " ".join(entry["item"] for entry in _record()["written_down"]).lower()
    assert "idempotency keys" in items and "pitch key" not in items


# ── the banner ───────────────────────────────────────────────────────


def test_all_three_off_names_all_three_and_carries_the_gate():
    fragment = config_view.render(_record())
    assert "All three keys are off" in fragment
    assert "can read, join and spend" in fragment
    assert 'class="banner is-danger cf-banner" data-gate-open' in fragment


def test_reading_closed_but_spending_open_is_a_warning_without_the_gate():
    fragment = config_view.render(_record(viewer_key=LONG + "v", node_secret=LONG + "n"))
    assert "pitch_key is off" in fragment and "can spend" in fragment
    assert 'class="banner is-warn cf-banner" role="status"' in fragment
    assert "data-gate-open" not in fragment


def test_no_banner_when_every_authority_is_set():
    fragment = config_view.render(
        _record(viewer_key=LONG + "v", node_secret=LONG + "n", pitch_key=LONG + "p")
    )
    assert 'class="banner' not in fragment


# ── the fragment's rules ─────────────────────────────────────────────


@pytest.fixture(params=["defaults", "all_set", "provider", "trusted", "odd"])
def any_fragment(request):
    runtime = {"lock_held": True, "instance_id": "i", "pid": 1,
               "started_at": "2026-09-12T00:00:00+00:00", "preflight_warnings": ["w"]}
    overrides = {
        "defaults": {},
        "all_set": {"viewer_key": LONG + "v", "node_secret": LONG + "n", "pitch_key": LONG + "p"},
        "provider": {"provider": "openai", "provider_api_key": "x", "provider_model": "m",
                     "verify_rate": 0.1, "public_pitch": True},
        "trusted": {"deployment_mode": "trusted_alpha", "node_enrollment_mode": "required",
                    "verify_rate": 0.5, "viewer_session_ttl_seconds": 10},
        "odd": {"role_model_map": {"builder": "gemma3:4b"}, "validator_execution_mode": "inline",
                "https_enabled": True, "trust_proxy_headers": True, "unknown_thing": 1},
    }[request.param]
    return config_view.render(config_view.build_record(_settings(**overrides), runtime))


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


def test_key_names_wrap_only_after_an_underscore(any_fragment):
    for key_html in re.findall(r'<span class="cf-key">(.*?)</span>', any_fragment):
        assert "<wbr>" not in key_html.replace("_<wbr>", ""), key_html


def test_a_long_value_takes_the_note_column_rather_than_wrapping_in_its_own():
    """At 1440px a state directory squeezed into the 11rem value column wrapped
    four times and broke at `state-`. A long value spans the value and note
    columns, on a phone as well, and its hyphenated segments are held."""
    record = _record({"state_dir": "/srv/mycelium/state-directory-with-a-long-name/coordinator"})
    fragment = config_view.render(record)
    row = fragment[fragment.index('data-key="state directory"') - 30:]
    row = row[: row.index("</div>")]
    assert 'class="cf-row is-wide"' in row
    assert '<span class="cf-nowrap">state-directory-with-a-long-name</span>' in row
    assert "srv/<wbr>mycelium/<wbr>" in row
    short = fragment[fragment.index('data-key="port"') - 30:]
    assert 'class="cf-row" data-key="port"' in short
    css = (TEMPLATES / "_dashboard.css").read_text(encoding="utf-8")
    assert re.search(r"\.cf-row\.is-wide \.cf-val\s*\{\s*grid-column: 2 / -1;", css)
    phone = css[css.index("@media (max-width: 900px)", css.index("/* ── Config view")):]
    assert re.search(r"\.cf-row\.is-wide \.cf-val \{ grid-column: 1 / -1;", phone)


def test_no_hyphenated_word_in_a_note_or_value_is_left_breakable(any_fragment):
    """At 1024px "abuse-risk" ended one line with "abuse-". Every hyphenated run
    in visible text is either reworded away or held in a span that does not wrap."""
    held = re.sub(r'<span class="cf-nowrap">[^<]*</span>', " ", any_fragment)
    text = visible_text(held)
    assert re.findall(r"\w+-\w[\w-]*", text) == []


def test_nothing_in_the_config_styles_renders_below_11px():
    css = (TEMPLATES / "_dashboard.css").read_text(encoding="utf-8")
    block = css[css.index("/* ── Config view"):]
    sizes = [float(size) for size in re.findall(r"font-size:\s*([0-9.]+)px", block)]
    assert sizes and min(sizes) >= 11, sizes


# ── the route, its gate, and the console ─────────────────────────────


def test_the_route_is_operator_prefixed_and_refused_without_a_key(monkeypatch):
    """Not in `_PUBLIC_EXACT`, under `/v1/operator/`, refused by its own handler.

    Asked of the running app by request, never by walking `app.routes`, which
    stopped flattening included routers in FastAPI 0.141.
    """
    import routes_config

    assert ("GET", ROUTE) not in _PUBLIC_EXACT
    assert [route.path for route in routes_config.router.routes] == [ROUTE]
    monkeypatch.setitem(config.get(), "viewer_key", LONG)
    with TestClient(app) as client:
        refused = client.get(ROUTE)
        assert refused.status_code == 401
        assert "config_html" not in refused.text
        assert client.get(ROUTE, headers={"X-Viewer-Key": LONG}).status_code == 200
        assert client.get("/config", headers={"X-Viewer-Key": LONG}).status_code == 404


def test_the_handler_refuses_on_its_own_without_the_middleware(monkeypatch):
    from fastapi import FastAPI

    import routes_config

    monkeypatch.setitem(config.get(), "viewer_key", LONG)
    bare = FastAPI()
    bare.include_router(routes_config.router)
    with TestClient(bare) as client:
        assert client.get(ROUTE).status_code == 401


def test_the_operator_prefix_is_refused_at_the_public_edge():
    caddyfile = (ROOT / "deploy" / "Caddyfile.public").read_text(encoding="utf-8")
    assert "@operator path /dashboard* /v1/operator/* /metrics" in caddyfile, (
        "the operator prefix is no longer refused at the public edge, so the Config "
        "route is reachable with a viewer key alone and its placement needs rethinking"
    )


def test_the_console_has_a_config_view_that_asks_when_opened():
    html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    js = (TEMPLATES / "_dashboard.js").read_text(encoding="utf-8")
    assert 'id="tab-config"' in html and 'data-tab="config"' in html
    assert 'id="view-config"' in html and 'id="config-body"' in html
    assert re.search(r"const TABS = \[[^\]]*'config'", js)
    assert "if (name === 'config') loadConfig();" in js
    loader = js[js.index("async function loadConfig()"):]
    loader = loader[: loader.index("\n}\n")]
    assert f"apiJson('{ROUTE}')" in loader
    assert "data.config_html" in loader
    assert "setInterval(loadConfig" not in js


def test_the_global_gate_banner_yields_only_to_a_drawn_one():
    """Suppressed on Config only once Config has drawn its own warning. A view
    that failed to load carries none, and hiding the global one there would
    leave the page silent about an open server."""
    js = (TEMPLATES / "_dashboard.js").read_text(encoding="utf-8")
    render = js[js.index("function renderBanner(d)"):]
    render = render[: render.index("\n}\n")]
    assert "[data-gate-open]" in render
    assert "const BANNER_SUPPRESSED_ON = ['config'];" in js
    loader = js[js.index("async function loadConfig()"):]
    assert "renderStatus();" in loader[: loader.index("\n}\n")]
