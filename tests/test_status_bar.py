"""The status bar: what it shows, what it refuses to show, and which cells lie.

`tests/test_status_model.py` covers the state machine. This file covers the row
that renders it, and in particular the three fields that look like the right
answer and are not. Each of those has cost somebody an afternoon somewhere:

1. **Model name.** `/health.models` lists everything installed on the host, and
   `/status.json.model` is whichever tag answered Ollama first. Neither is the
   model this coordinator will use. `config · model` is.
2. **RUNNING / QUEUED.** `/health.tasks_pending` is the *subtask* queue. Jobs
   are `/metrics · jobs_running` and `jobs_queued`, and they are different
   numbers — `test_the_subtask_queue_and_the_job_queue_are_different_numbers`
   makes them disagree on purpose rather than asserting they might.
3. **Tracing.** Three states in `tracing.py` — off, propagating, exporting —
   and `tracing_enabled` alone reports `exporting` for a deployment that
   exports nothing. That was found once by a test asserting its own
   precondition; this is the UI half of the same check.

Everything else here is about the two rules a reader depends on without being
told: a cell with a lamp is claiming something about this second, a cell
without one is not; and the third value survives greyscale because it is
carried by shape rather than by hue.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import dashboard
from server import app

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
DASHBOARD_JS = TEMPLATES / "_dashboard.js"
DASHBOARD_CSS = TEMPLATES / "_dashboard.css"
NODE = shutil.which("node")


def missing_node_complaint(ci, node):
    """The complaint when CI has no `node`, or None when there is nothing wrong.

    Pure and taking both inputs as arguments so the check below can prove it
    fires, rather than trusting that it would.
    """
    if ci and not node:
        return (
            "CI has no `node` on PATH, so tests/test_status_model.py skipped "
            "every scenario in the design's rig and the status model went "
            "untested."
        )
    return None


def test_a_missing_node_in_ci_is_a_failure_rather_than_a_silent_skip():
    """The status model rig needs Node, and a skipped rig proves nothing.

    This lives here rather than in tests/test_status_model.py on purpose. That
    module carries a module-level `skipif` for a missing Node, and a
    module-level mark applies to every test in its module — so the guard that
    was originally written there would have been skipped by the very condition
    it existed to catch. This file has no module-level mark, so this always
    runs.
    """
    assert missing_node_complaint(os.environ.get("CI"), NODE) is None


def test_that_guard_can_actually_fire():
    """A guard nobody has seen fail is a guard nobody knows works."""
    assert missing_node_complaint("true", None), "the guard never complains"
    assert missing_node_complaint("true", "/usr/bin/node") is None
    assert missing_node_complaint(None, None) is None, (
        "a developer with no Node installed would fail this suite locally"
    )


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def settings():
    """The live config dict, restored afterwards.

    config.get() returns the process-wide mapping, so a test that writes to it
    and does not put it back changes what every later test sees.
    """
    cfg = config.get()
    before = dict(cfg)
    try:
        yield cfg
    finally:
        cfg.clear()
        cfg.update(before)


# ── Reading the bar out of the served page ───────────────────────────────

class _Cells(HTMLParser):
    """Collect the bar's cells, each with its text and whether it has a lamp.

    Parsed rather than regexed because the question — does *this* cell contain
    a lamp — is a nesting question, and a regex answering it would be answering
    a different one.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells: list[dict] = []
        self._depth = 0
        self._current: dict | None = None
        self.in_bar = False
        self._bar_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if a.get("id") == "statusbar":
            self.in_bar = True
            self._bar_depth = 0
        if not self.in_bar:
            return
        self._bar_depth += 1
        if "statusbar-cell" in classes:
            self._current = {
                "id": a.get("id", ""),
                "classes": classes,
                "lamp": False,
                "text": "",
                "depth": self._bar_depth,
            }
            self.cells.append(self._current)
            return
        if self._current is not None and "lamp" in classes:
            self._current["lamp"] = True

    def handle_endtag(self, tag):
        if not self.in_bar:
            return
        if self._current is not None and self._bar_depth == self._current["depth"]:
            self._current = None
        self._bar_depth -= 1
        if self._bar_depth <= 0:
            self.in_bar = False

    def handle_data(self, data):
        if self._current is not None:
            self._current["text"] += data


def bar_cells(html: str) -> list[dict]:
    p = _Cells()
    p.feed(html)
    cells = p.cells
    assert cells, "no status bar cells were found, so nothing below proves anything"
    return cells


def bar_html(html: str) -> str:
    start = html.index('<div class="statusbar"')
    end = html.index("<!-- Dialogs live outside", start)
    return html[start:end]


# ── Which cells carry a lamp ─────────────────────────────────────────────

POLLED = {"INFERENCE", "NODES", "RUNNING", "QUEUED"}
AT_LOAD = {"MODE", "LOCK", "TRACING", "EVIDENCE"}


def _label(cell: dict) -> str:
    return cell["text"].split()[0].strip() if cell["text"].split() else ""


def test_the_bar_has_exactly_the_eight_cells_the_design_names(client):
    labels = {_label(c) for c in bar_cells(client.get("/dashboard").text)}
    assert labels == POLLED | AT_LOAD, labels


def test_every_polled_cell_carries_a_lamp(client):
    for cell in bar_cells(client.get("/dashboard").text):
        if _label(cell) in POLLED:
            assert cell["lamp"], (
                f"{_label(cell)} is polled but has no lamp, so a stale value in it "
                "would look exactly like a current one"
            )


def test_every_at_load_cell_lacks_a_lamp(client):
    """A cell with no lamp is making no claim about this second.

    mode, lock, tracing and evidence are all fixed for the life of the process,
    so a lamp beside one would be a freshness claim about a constant — and the
    coordinator lock in particular can never be observed lost by a console that
    is running, because it is held for as long as the process is.
    """
    for cell in bar_cells(client.get("/dashboard").text):
        if _label(cell) in AT_LOAD:
            assert not cell["lamp"], (
                f"{_label(cell)} is read once at load but carries a lamp, which "
                "claims it is current"
            )


def test_the_age_of_the_last_answer_is_in_the_bar_and_in_the_pill(client):
    body = client.get("/dashboard").text
    assert 'id="statusbar-age-text"' in body, "the bar has no age cell"
    assert 'id="statuspill-age"' in body, "the collapsed bar drops the age"
    js = DASHBOARD_JS.read_text(encoding="utf-8")
    # Unconditional: not inside a branch that some state could skip.
    assert "setText('statusbar-age-text', d.ageText);" in js
    assert "pillAge.textContent = d.ageText;" in js


# ── Shape, not colour ────────────────────────────────────────────────────

LAMP_RULE = re.compile(r"^\.lamp\.is-(\w[\w-]*)\s*\{([^}]*)\}", re.M)


def test_not_heard_back_is_the_only_hollow_lamp(client):
    """Filled means a fresh answer, hollow means none, and colour only says
    which answer. Give `is-unknown` a fill and the state stops surviving
    greyscale, video compression, and a reader who does not see green."""
    css = DASHBOARD_CSS.read_text(encoding="utf-8")
    rules = dict(LAMP_RULE.findall(css))
    assert set(rules) >= {"ok", "warn", "bad", "unknown"}, rules.keys()

    hollow = rules["unknown"]
    assert "background: transparent" in hollow, (
        "the not-heard-back lamp has a fill, so it is only distinguishable by hue"
    )
    for state in ("ok", "warn", "bad"):
        assert "transparent" not in rules[state], (
            f"the {state} lamp is hollow, so it cannot be told from not-heard-back"
        )

    # And every lamp is a square: the shape difference is fill, not silhouette.
    base = css[css.index("\n.lamp {"):]
    base = base[: base.index("}")]
    assert "border-radius" not in base, "a round lamp is a different vocabulary"


def test_the_lamp_states_the_js_paints_all_exist_in_the_css():
    """A class the script sets and the stylesheet has never heard of renders as
    an unstyled square, which reads as `ok` in dark mode."""
    js = DASHBOARD_JS.read_text(encoding="utf-8")
    css = DASHBOARD_CSS.read_text(encoding="utf-8")
    painted = set(re.findall(r"return '(is-\w+)';", js))
    assert painted, "toneClass no longer returns literals; this check is inert"
    for cls in painted:
        assert f".lamp.{cls}" in css, f"{cls} is painted but not styled"


# ── The three value traps ────────────────────────────────────────────────

def test_the_model_name_comes_from_config_not_from_whatever_ollama_answered(
    client, settings
):
    """/health.models is everything installed on the host and /status.json.model
    is whichever tag came back first. Neither is the model this coordinator will
    use, and on a host with several pulled they are routinely a different name.
    """
    settings["model"] = "sentinel-config-model:9b"
    body = client.get("/dashboard").text
    assert "sentinel-config-model:9b" in bar_html(body), (
        "the bar does not show the configured model"
    )

    # And the script never fills that cell from a poll, by either wrong route.
    js = DASHBOARD_JS.read_text(encoding="utf-8")
    for wrong in ("cell-model", "models[0]", "health.models"):
        if wrong == "cell-model":
            assert "cell-model" not in js, (
                "the model cell is written from the client, so it is no longer "
                "the at-load config value"
            )
        else:
            assert f"$('cell-inference-v').textContent = {wrong}" not in js


def test_running_and_queued_are_jobs_not_the_subtask_queue():
    js = DASHBOARD_JS.read_text(encoding="utf-8")
    assert "statusText.running = String(met.jobs_running);" in js
    assert "statusText.queued = String(met.jobs_queued);" in js
    # tasks_pending has its own tile, labelled as itself, and appears nowhere
    # near the two cells that would be wrong to fill with it.
    running_block = js[js.index("statusText.running"): js.index("statusText.running") + 400]
    assert "tasks_pending" not in running_block


def test_the_subtask_queue_and_the_job_queue_are_different_numbers(client):
    """The trap is only a trap because the two disagree. Rather than assert that
    they might, make them: one queued subtask and no queued job."""
    import server_state as state

    state.task_queue.clear()
    state.jobs.clear()
    state.task_queue.append({"id": "t1", "title": "a subtask"})
    try:
        health = client.get("/health").json()
        metrics = client.get("/metrics").json()
        assert health["tasks_pending"] == 1
        assert metrics["jobs_queued"] == 0
        assert health["tasks_pending"] != metrics["jobs_queued"], (
            "the two counts agree here, so this scenario cannot show the trap"
        )
    finally:
        state.task_queue.clear()


@pytest.mark.parametrize(
    "enabled,export,bridge,expected",
    [
        (False, False, False, "off"),
        (False, True, True, "off"),      # export without propagation is still off
        (True, False, False, "propagating"),
        (True, True, False, "propagating"),   # the SDK is absent, so nothing leaves
        (True, True, True, "exporting"),
    ],
)
def test_tracing_has_three_states_not_two(
    settings, monkeypatch, enabled, export, bridge, expected
):
    """`tracing_enabled` alone would report `exporting` for a deployment with
    only opentelemetry-api installed — which arrives on its own as a transitive
    dependency of mcp and records nothing. The SDK is the condition."""
    import tracing

    settings["tracing_enabled"] = enabled
    settings["tracing_export"] = export
    monkeypatch.setattr(tracing, "_otel_bridge", lambda: object() if bridge else None)
    assert dashboard.tracing_state() == expected


def test_the_tracing_cell_renders_the_state_and_not_a_boolean(client, settings):
    settings["tracing_enabled"] = True
    settings["tracing_export"] = False
    bar = bar_html(client.get("/dashboard").text)
    assert "propagating" in bar
    for boolean in ("true", "True", "false", "False"):
        assert f">{boolean}<" not in bar, "the tracing cell rendered a boolean"


def test_evidence_renders_the_configured_mode(client, settings):
    settings["capability_evidence_mode"] = "shadow"
    assert "shadow" in bar_html(client.get("/dashboard").text)


def test_the_at_load_values_are_escaped_before_they_reach_the_page(settings):
    """Slot values are HTML and are escaped by the caller — dashboard.py, here.
    config.json is operator-controlled rather than hostile, but a slot that
    interpolates unescaped is a slot that will one day carry something else."""
    settings["model"] = '<script>alert(1)</script>'
    slots = dashboard.status_bar_at_load()
    assert "<script>" not in slots["STATUS_MODEL_NAME"]
    assert "&lt;script&gt;" in slots["STATUS_MODEL_NAME"]


# ── What the bar refuses to show ─────────────────────────────────────────

UTILIZATION = re.compile(
    r"\b(cpu|gpu|ram|memory|load average|utili[sz]ation)\b|\d+\s*%", re.I
)


def test_the_bar_renders_no_utilization_figure(client):
    """A node's capability descriptor claims CPU, memory and GPU at
    registration. Nothing samples them afterwards and no endpoint serves a
    sample, so a load line here would be a drawing rather than a measurement.
    """
    bar = bar_html(client.get("/dashboard").text)
    visible = re.sub(r"<!--.*?-->", " ", bar, flags=re.S)
    visible = re.sub(r"<[^>]+>", " ", visible)
    found = UTILIZATION.search(visible)
    assert not found, f"the bar shows a utilization figure: {found.group(0)!r}"


def test_the_rails_inference_line_was_deleted_not_duplicated(client):
    """One poller, one rendering. The rail carried the only indicator whose
    disagreement with the others was on screen at all times."""
    body = client.get("/dashboard").text
    for gone in ('id="nav-status"', 'id="nav-model"', 'class="nav-foot"'):
        assert gone not in body, f"{gone} is still in the rail beside the bar"
    css = DASHBOARD_CSS.read_text(encoding="utf-8")
    assert ".nav-foot {" not in css, "the deleted rail line still has styles"
    js = DASHBOARD_JS.read_text(encoding="utf-8")
    assert "nav-status" not in js and "nav-model" not in js


def test_there_is_one_banner_region_rather_than_two(client):
    body = client.get("/dashboard").text
    assert 'id="banner-failopen"' not in body
    assert 'id="banner-degraded"' not in body
    assert body.count('class="banner"') == 1, "more than one banner region"


def test_the_four_states_that_earn_words_are_the_only_four():
    js = DASHBOARD_JS.read_text(encoding="utf-8")
    block = js[js.index("const BANNERS = {"): js.index("/* A poll that never returns")]
    keys = set(re.findall(r"^  (\w+): \{", block, re.M))
    assert keys == {"gate", "commit", "inference", "silent"}, keys


# ── The verdict chip ─────────────────────────────────────────────────────
#
# runVerdict lives in _dashboard.js, which touches the DOM at load and so
# cannot simply be required. The function is sliced out by name and evaluated
# on its own; the slice asserts its own anchors, so a rename fails here loudly
# rather than quietly testing nothing.

VERDICT_START = "const VERDICTS = {"
VERDICT_END = "\n}\n"


def _verdict_module() -> str:
    js = DASHBOARD_JS.read_text(encoding="utf-8")
    assert VERDICT_START in js, "the verdict table was renamed; this slice is inert"
    start = js.index(VERDICT_START)
    tail = js.index("function verdictChip(", start)
    end = js.index(VERDICT_END, tail) + len(VERDICT_END)
    body = js[start:end]
    assert "function runVerdict(" in body, "runVerdict fell outside the slice"
    return body + "\nmodule.exports = {runVerdict, verdictChip, VERDICTS};\n"


def verdicts(cases: list[tuple]) -> list[str]:
    """Run (rating, precheck_error) pairs through the shipped function."""
    driver = (
        _verdict_module()
        + "const CASES = JSON.parse(process.env.MYCELIUM_VERDICT_CASES);\n"
        + "process.stdout.write(JSON.stringify(CASES.map("
        + "c => [runVerdict(c[0], c[1]), verdictChip(c[0], c[1])])));\n"
    )
    out = subprocess.run(
        [NODE, "-e", driver],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "MYCELIUM_VERDICT_CASES": json.dumps(cases)},
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_verdict_has_five_values_and_no_sixth():
    cases = [
        ("PASS", None),
        ("NEEDS_WORK", None),
        ("FAIL", None),
        ("PASS", "validator runner did not start"),
        ("?", None),
    ]
    got = [v for v, _ in verdicts(cases)]
    assert got == ["PASS", "NEEDS_WORK", "FAIL", "UNCHECKED", "NO_VERDICT"]
    assert len(set(got)) == 5


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_unchecked_never_reads_as_pass():
    """An empty problem list beside a precheck error means "not checked", not
    "checked clean". PASS claims the reviewer passed it *and* the mechanical
    check found no defects, so a run missing the second half cannot wear it."""
    cases = [
        ("PASS", "runner failure"),
        ("PASS", "starved validator"),
        ("PASS", ""),          # falsy: no precheck error, so PASS is honest
        ("PASS", None),
    ]
    got = [v for v, _ in verdicts(cases)]
    assert got == ["UNCHECKED", "UNCHECKED", "PASS", "PASS"]

    chips = [chip for _, chip in verdicts([("PASS", "runner failure")])]
    assert "UNCHECKED" in chips[0]
    assert "PASS" not in chips[0], "the UNCHECKED chip still contains the word PASS"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_named_negative_verdict_is_never_softened_into_unchecked():
    """Worst wins, as everywhere else in this model. A precheck error must not
    replace a FAIL with something that reads as less bad than it is."""
    got = [v for v, _ in verdicts([("FAIL", "runner failure"),
                                   ("NEEDS_WORK", "runner failure")])]
    assert got == ["FAIL", "NEEDS_WORK"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_an_unrecorded_rating_says_so_instead_of_rendering_nothing():
    """The badge this replaced returned an empty string for "?", so a run with
    no rating was indistinguishable from one nobody had looked at."""
    got = [v for v, _ in verdicts([("?", None), (None, None), ("", None),
                                   ("SOMETHING_ELSE", None)])]
    assert got == ["NO_VERDICT"] * 4


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_verdict_never_renders_a_percentage():
    every = verdicts([("PASS", None), ("NEEDS_WORK", None), ("FAIL", None),
                      ("PASS", "e"), ("?", None)])
    for _, chip in every:
        assert "%" not in chip, f"a verdict chip carries a percentage: {chip!r}"


def test_the_list_endpoints_carry_the_field_the_chip_needs(client):
    """Without it a Runs or Gallery card would render PASS over a run whose own
    page says UNCHECKED — the two views disagreeing about one run."""
    import server_state as state

    run = state.OUTPUT_DIR / "20260907_120000"
    run.mkdir(parents=True, exist_ok=True)
    (run / "full_log.json").write_text(json.dumps({
        "task": "A run whose mechanical check never finished",
        "timestamp": "20260907_120000",
        "plan": [{"id": 1, "title": "one", "depends_on": []}],
        "results": {"1": "x"}, "review": "PASS", "rating": "PASS",
        "code_files": [], "code_problems": [],
        "code_precheck_error": "validator runner did not start",
        "mode": "local", "project_id": "",
    }), encoding="utf-8")

    listed = client.get("/history").json()["runs"]
    mine = next(r for r in listed if r["timestamp"] == "20260907_120000")
    assert mine["rating"] == "PASS"
    assert mine["code_precheck_error"] == "validator runner did not start", (
        "/history cannot tell PASS from UNCHECKED"
    )

    cards = client.get("/gallery").json()["cards"]
    card = next(c for c in cards if c["timestamp"] == "20260907_120000")
    assert card["code_precheck_error"] == "validator runner did not start"

    detail = client.get("/history/20260907_120000").json()
    assert detail["code_precheck_error"] == "validator runner did not start"
    assert detail["code_problems"] == [], (
        "a record carrying both channels was constructed, which validators.py forbids"
    )


def test_a_count_that_stops_being_current_is_held_and_greyed_not_blanked():
    """The silence banner says "every count on screen is the last one it gave",
    which is only true if the counts are still on screen.

    Blanking them would also make QUEUED read as empty, and an empty queue is a
    claim that the swarm is idle — which nobody polling a silent coordinator is
    in a position to make.
    """
    js = DASHBOARD_JS.read_text(encoding="utf-8")
    block = js[js.index("const served = d.answering"):]
    block = block[: block.index("/* The Overview tile")]
    assert "statusText.nodes)" in block, "the held value is not passed through"
    assert ": null" not in block, (
        "a cell is passed null while a last-known value exists, so it blanks"
    )
    css = DASHBOARD_CSS.read_text(encoding="utf-8")
    assert ".statusbar-cell.is-stale .statusbar-v { color: var(--text-faint); }" in css


def test_run_detail_never_shows_a_problem_list_beside_a_precheck_error():
    """execution/validators.py refuses to construct a record carrying both a
    runner failure and code problems. The UI is where that separation could be
    quietly re-merged, so the branch is exclusive rather than additive.

    The rule moved with the markup: the modal no longer builds its own layout,
    and run_detail.py now renders the deliverable panel for the console and
    /run/{id} alike. Same rule, one place instead of two.
    """
    source = (
        Path(__file__).resolve().parent.parent / "run_detail.py"
    ).read_text(encoding="utf-8")
    block = source[source.index('if ctx["precheck_error"]:'):]
    block = block[: block.index("prose_html =")]
    assert 'elif ctx["problems"]:' in block, (
        "problems and the precheck error are rendered independently, so a record "
        "could show both"
    )
    assert block.index('if ctx["precheck_error"]:') < block.index('elif ctx["problems"]:'), (
        "an empty problem list printed first would read as 'checked, clean'"
    )
