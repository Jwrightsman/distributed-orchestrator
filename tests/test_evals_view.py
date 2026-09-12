"""The Evals view, and the score it declines to print.

`docs/design/HANDOFF-DELTA.md` §9 is the design this holds. The archived
console built this view around one quality figure; §4.1 retired the figure, so
the view shows the record and what the instrument can resolve, and totals
nothing. What is asserted here is the part a later edit could quietly undo:

* no percent sign, anywhere in the fragment;
* no row or column total;
* the pass decision is the harness's own `is_success`, called;
* a pass on the load-only check says `loaded`, an undecidable grade says
  `ungraded`, and a pair holding one computes no statistic;
* a paired result prints its table before its p-value;
* absent, empty and recorded are three answers.

The first group runs against the committed record, because the published noise
floor is a fact about those files and a fixture could agree with a wrong
implementation. Every branch after that runs against a fixture written for it.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import evals_view
from access_control import _PUBLIC_EXACT
from evals import corpus as corpus_mod
from evals import scoring, stats
from server import app
from tests.test_console_language import (
    BANNED_PHRASES,
    QUALITY_PERCENTAGE,
    _WORD,
    visible_text,
)
from tests.test_templates import _check_balance

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
NOISE_FLOOR_PAIR = ("20260808_050610", "20260811_052310")


# ── fixtures ─────────────────────────────────────────────────────────


def _result(task_id: str, category: str = "cli_tool", *, passed: bool = True, **fields) -> dict:
    record = {
        "id": task_id,
        "category": category,
        "extracted": True,
        "parses": True,
        "executes": passed,
        "artifact_match": True,
        "keywords_ok": True,
        "exec_outcome": "exited_clean" if passed else "error",
        "judge_score": 5,
    }
    record.update(fields)
    return record


def _run(root: Path, run_id: str, prompt_set: str | None, records: list[dict], *, nested=True):
    directory = root / run_id
    directory.mkdir(parents=True)
    (directory / "results.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    meta = {"run_id": run_id, "prompt_set": prompt_set, "model": "m"}
    # The two oldest committed runs keep their meta at the top level.
    blob = {"meta": meta, "summary": {}} if nested else {"total": len(records), **meta}
    (directory / "summary.json").write_text(json.dumps(blob), encoding="utf-8")


def _fragment(record: dict) -> str:
    return evals_view.render(record)


class _Grid(HTMLParser):
    """The grid's body rows, as (row-header text, [cell texts])."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_grid = self.in_body = self.in_foot = False
        self.rows: list[tuple[str | None, list[str], bool]] = []
        self.head_cells = 0
        self._row = None
        self._cell = None
        self._scope = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and "ev-grid" in (a.get("class") or ""):
            self.in_grid = True
        if not self.in_grid:
            return
        if tag == "tbody":
            self.in_body = True
        elif tag == "tfoot":
            self.in_foot = True
        elif tag == "tr" and self.in_body:
            self._row = {"header": None, "cells": [], "group": "ev-group" in (a.get("class") or "")}
        elif tag in ("th", "td"):
            self._cell = ""
            self._scope = a.get("scope")
            if not self.in_body:
                self.head_cells += 1

    def handle_data(self, data):
        if self._cell is not None:
            self._cell += data

    def handle_endtag(self, tag):
        if not self.in_grid:
            return
        if tag in ("th", "td") and self._cell is not None:
            if self._row is not None:
                if tag == "th" and self._scope == "row":
                    self._row["header"] = self._cell.strip()
                else:
                    self._row["cells"].append(self._cell.strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append((self._row["header"], self._row["cells"], self._row["group"]))
            self._row = None
        elif tag == "tbody":
            self.in_body = False
        elif tag == "table":
            self.in_grid = False


def _grid(fragment: str) -> _Grid:
    parser = _Grid()
    parser.feed(fragment)
    return parser


@pytest.fixture
def every_state(tmp_path):
    """One record reaching the cell vocabulary's every word and both pair outcomes.

    Three v1 runs (three pairs, so nothing may be pooled), a task one run
    lacks, a load-only pass, and a v2 pair holding an ungraded task.
    """
    root = tmp_path / "results"
    loaded = dict(exec_outcome="browser_ok")
    _run(root, "20260101_000000", "v1", [
        _result("t-a"), _result("t-b", passed=False), _result("web-x", "web_app", **loaded),
    ])
    _run(root, "20260102_000000", "v1", [
        _result("t-a", passed=False), _result("t-b"), _result("web-x", "web_app", **loaded),
    ], nested=False)
    _run(root, "20260103_000000", "v1", [_result("t-a"), _result("t-b")])
    _run(root, "20260104_000000", "v2", [_result("t-a"), _result("t-b", keywords_ok=None)])
    _run(root, "20260105_000000", "v2", [_result("t-a"), _result("t-b")])
    return root


# ── the committed record ─────────────────────────────────────────────


@pytest.fixture(scope="module")
def committed():
    record = evals_view.build_record()
    assert record["recorded"] and record["runs"], "the committed eval record did not load"
    return record


def test_the_committed_record_reproduces_the_published_noise_floor(committed):
    """18 of 28, the interval eval-methodology.md §1.3 prints, and 10.8 → 11 tasks."""
    (pair,) = [p for p in committed["pairs"] if (p["a"], p["b"]) == NOISE_FLOOR_PAIR]
    assert pair["computed"]
    assert (pair["n"], pair["discordant"]) == (28, 18)
    assert pair["discordant"] / pair["n"] == stats.MEASURED_DISCORDANT_RATE
    assert [round(x, 2) for x in pair["interval"]] == [0.46, 0.79]
    # Under the mechanical grade. The judged `success` field gives 7/10/8/3;
    # both come to 18, which is eval-methodology.md §1.4's finding.
    assert (pair["both_pass"], pair["a_only"], pair["b_only"], pair["both_fail"]) == (8, 10, 8, 2)
    assert pair["smallest_visible_change"] == 11
    assert len(pair["flipped"]) == 18


def test_the_aug_9_and_aug_10_runs_are_comparisons_not_re_runs(committed):
    """The archived design labelled both as further v3 runs. Delta §9.5."""
    sets = {r["run_id"]: r["prompt_set"] for r in committed["runs"]}
    assert sets["20260809_053327"] == "v4"
    assert sets["20260810_041455"] == "v5"
    assert [(p["a"], p["b"]) for p in committed["pairs"]] == [NOISE_FLOOR_PAIR]


def test_web_snake_reads_loaded_in_every_committed_run(committed):
    """§1.1 at cell resolution: five passes, every one of them on load alone."""
    (snake,) = [t for t in committed["tasks"] if t["id"] == "web-snake"]
    assert set(snake["cells"].values()) == {evals_view.LOADED}
    assert len(snake["cells"]) == len(committed["runs"])


def test_the_committed_lede_counts_the_load_only_passes(committed):
    text = visible_text(_fragment(committed))
    assert "31 of the 75 passes" in text


def test_the_committed_corpus_is_described_not_scored(committed):
    corpus = committed["corpus"]
    assert corpus["readable"] and corpus["split_lock_holds"]
    assert (corpus["items"], corpus["development"], corpus["confirmatory"]) == (100, 64, 36)
    assert corpus["confirmatory_ever_run"] == 0
    assert corpus["never_run"] == 72
    assert corpus["known_suspect_bands"] == 6


# ── what each cell says ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fields", "word"),
    [
        ({}, evals_view.PASS),
        ({"exec_outcome": "browser_ok"}, evals_view.LOADED),
        ({"executes": False, "exec_outcome": "browser_ok"}, evals_view.FAIL),
        ({"extracted": False, "parses": None}, evals_view.FAIL),
        ({"parses": None}, evals_view.UNGRADED),
        ({"keywords_ok": None}, evals_view.UNGRADED),
    ],
)
def test_each_record_prints_the_word_for_what_was_checked(fields, word):
    assert evals_view.cell_grade(_result("t", **fields), scoring.is_success) == word


def test_the_pass_decision_is_the_harness_own_not_a_copy():
    """If `is_success` changes its mind, every cell follows it.

    Both directions, because a re-implementation would agree with the real
    function on every record the real function has ever seen.
    """
    clean = _result("t")
    broken = _result("t", passed=False)
    assert evals_view.cell_grade(clean, lambda r, require_judge: False) == evals_view.UNGRADED
    assert evals_view.cell_grade(broken, lambda r, require_judge: True) == evals_view.PASS

    seen = []
    evals_view.cell_grade(clean, lambda r, require_judge: seen.append(require_judge) or True)
    assert seen == [False], "the judge gate must be off: the judge is exploratory"


def test_a_task_a_run_did_not_include_says_so(every_state):
    record = evals_view.build_record(every_state)
    (web,) = [t for t in record["tasks"] if t["id"] == "web-x"]
    assert web["cells"]["20260103_000000"] == evals_view.NOT_IN_RUN
    assert "not in run" in visible_text(_fragment(record))


def test_the_two_oldest_summary_shapes_both_name_their_prompt_set(every_state):
    record = evals_view.build_record(every_state)
    sets = {r["run_id"]: r["prompt_set"] for r in record["runs"]}
    assert sets["20260102_000000"] == "v1"


# ── the rules the fragment is held to ────────────────────────────────


@pytest.fixture(params=["committed", "every_state", "no_pairs", "tiny_pair"])
def any_fragment(request, tmp_path, every_state):
    if request.param == "committed":
        return _fragment(evals_view.build_record())
    if request.param == "every_state":
        return _fragment(evals_view.build_record(every_state))
    root = tmp_path / request.param
    if request.param == "no_pairs":
        _run(root, "20260101_000000", "v1", [_result("t-a")])
        _run(root, "20260102_000000", "v2", [_result("t-a", passed=False)])
    else:
        _run(root, "20260101_000000", "v1", [_result("a"), _result("b", passed=False)])
        _run(root, "20260102_000000", "v1", [_result("a", passed=False), _result("b")])
    return _fragment(evals_view.build_record(root))


def test_no_percent_sign_renders_anywhere(any_fragment):
    """Stronger than §4.1 and needs no exceptions list. Attributes included."""
    assert "%" not in any_fragment


def test_the_fragment_carries_no_prohibited_language(any_fragment):
    text = visible_text(any_fragment)
    assert not QUALITY_PERCENTAGE.search(text)
    lowered = text.lower()
    for phrase in BANNED_PHRASES:
        assert phrase not in lowered
    for pattern in _WORD.values():
        assert not pattern.search(text)


def test_the_fragment_is_balanced(any_fragment):
    assert _check_balance(any_fragment) == []


def test_nothing_in_the_grid_is_totalled(committed, every_state):
    """Every body row is a category or a task, and every row is as wide as the head.

    A totals row would need a header that is not a task id; a totals column
    would make the head wider than a task's cells can fill.
    """
    for record in (committed, evals_view.build_record(every_state)):
        grid = _grid(_fragment(record))
        task_ids = {t["id"] for t in record["tasks"]}
        categories = {t["category"] for t in record["tasks"]}
        assert not grid.in_foot, "the grid has a <tfoot>"
        computed = [p for p in record["pairs"] if p["computed"]]
        assert grid.head_cells == 1 + len(record["runs"]) + len(computed)
        task_rows = [row for row in grid.rows if not row[2]]
        assert len(task_rows) == len(record["tasks"])
        for header, cells, group in grid.rows:
            if group:
                assert cells and cells[0] in categories
                continue
            assert header in task_ids, f"a grid row is headed {header!r}, which is not a task"
            assert len(cells) == len(record["runs"]) + len(computed)
            allowed = {
                evals_view.PASS, evals_view.LOADED, evals_view.FAIL,
                evals_view.UNGRADED, evals_view.NOT_IN_RUN, "flipped", "same",
            }
            assert set(cells) <= allowed, set(cells) - allowed


def test_every_count_printed_is_a_count_about_the_instrument(committed):
    """The strip and the side panels count the instrument, never the output.

    Checking for each run's pass count by value cannot work: the committed
    record's Aug 6 run passed 11 tasks, and 11 is also the smallest visible
    change, which the strip is required to print. So the rule is stated the
    other way round — every `X of Y` in the text must be one of the facts the
    view is meant to count — and a pass count added anywhere is a stranger.
    """
    text = visible_text(_fragment(committed))
    (pair,) = committed["pairs"]
    corpus = committed["corpus"]
    allowed = {
        (pair["discordant"], pair["n"]),
        (pair["smallest_visible_change"], pair["n"]),
        (corpus["confirmatory_ever_run"], corpus["confirmatory"]),
        (corpus["never_run"], corpus["items"]),
    }
    printed = {(int(x), int(y)) for x, y in re.findall(r"\b(\d+) of (\d+)\b", text)}
    assert printed, "found no counts at all, so this check proves nothing"
    assert printed <= allowed, f"counts that are not about the instrument: {printed - allowed}"
    assert not re.search(r"\b\d+\s*/\s*\d+\b", text), "a ratio is printed"


def test_a_paired_result_prints_its_table_before_its_p_value(committed):
    """`stats.render_paired`'s order: table, counts, interval, then p."""
    fragment = _fragment(committed)
    section = fragment[fragment.index('data-pair="' + ":".join(NOISE_FLOOR_PAIR)):]
    section = section[: section.index("</section>")]
    table = section.index("<table")
    interval = section.index("interval")
    p_value = section.index("one-sided p")
    assert table < interval < p_value
    assert section.index("to clear") > p_value


# ── states ───────────────────────────────────────────────────────────


def test_a_server_without_the_instrument_says_it_carries_no_record(monkeypatch):
    monkeypatch.setattr(evals_view, "_instrument", lambda: None)
    record = evals_view.build_record()
    assert record == {"recorded": False, "runs": []}
    assert "carries no eval record" in visible_text(_fragment(record))


def test_a_missing_results_directory_is_absent_not_empty(tmp_path):
    record = evals_view.build_record(tmp_path / "nowhere")
    assert record["recorded"] is False
    assert "carries no eval record" in visible_text(_fragment(record))


def test_a_directory_with_no_runs_is_empty_not_absent(tmp_path):
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / ".gitkeep").write_text("", encoding="utf-8")
    record = evals_view.build_record(tmp_path / "results")
    assert record["recorded"] is True and record["runs"] == []
    text = visible_text(_fragment(record))
    assert "No eval run has been recorded" in text
    assert "carries no eval record" not in text


def test_no_identical_pair_is_not_estimable_rather_than_zero(tmp_path):
    root = tmp_path / "results"
    _run(root, "20260101_000000", "v1", [_result("t-a")])
    _run(root, "20260102_000000", "v2", [_result("t-a", passed=False)])
    record = evals_view.build_record(root)
    assert record["pairs"] == []
    text = visible_text(_fragment(record))
    assert text.count("not estimable") == 2
    assert "Not estimable" in text
    assert "flipped" not in text.lower().replace("flipped between identical runs", "")


def test_several_pairs_are_neither_picked_nor_pooled(every_state):
    record = evals_view.build_record(every_state)
    v1_pairs = [p for p in record["pairs"] if p["prompt_set"] == "v1"]
    assert len(v1_pairs) == 3
    fragment = _fragment(record)
    strip = fragment[: fragment.index('class="ev-lede"')]
    assert "4 identical pairs" in visible_text(strip)
    assert "per pair" in visible_text(strip)
    assert not re.search(r"\b\d+ of \d+\b", visible_text(strip)), "the strip reported one pair's count"
    assert fragment.count('class="rd-panel ev-pair"') == 4


def test_a_pair_holding_an_ungraded_task_computes_nothing(every_state):
    record = evals_view.build_record(every_state)
    (pair,) = [p for p in record["pairs"] if p["prompt_set"] == "v2"]
    assert pair["computed"] is False
    assert pair["ungraded"] == ["t-b"]
    assert pair["flipped"] == []
    assert "p_one_sided" not in pair
    fragment = _fragment(record)
    section = fragment[fragment.index('data-pair="20260104_000000:20260105_000000"'):]
    section = section[: section.index("</section>")]
    assert "McNemar" not in section and "<table" not in section
    assert "could not be graded" in visible_text(section)


def test_a_lone_ungraded_pair_leaves_the_strip_not_estimable(tmp_path):
    root = tmp_path / "results"
    _run(root, "20260101_000000", "v1", [_result("t", keywords_ok=None)])
    _run(root, "20260102_000000", "v1", [_result("t")])
    text = visible_text(_fragment(evals_view.build_record(root)))
    assert "the identical pair holds an ungraded task" in text
    assert "ungraded" in text


def test_a_pair_too_small_to_see_anything_says_so(tmp_path):
    """Two tasks, both flipped: no split clears alpha and no effect reaches power."""
    root = tmp_path / "results"
    _run(root, "20260101_000000", "v1", [_result("a"), _result("b", passed=False)])
    _run(root, "20260102_000000", "v1", [_result("a", passed=False), _result("b")])
    record = evals_view.build_record(root)
    (pair,) = record["pairs"]
    assert pair["needed_one_way"] is None
    assert pair["smallest_visible_change"] is None
    text = visible_text(_fragment(record))
    assert "no change is noticed four runs in five" in text
    assert "cannot clear 0.05 in any split" in text


class _FakeStats:
    def __init__(self, effect):
        self.effect = effect

    def min_detectable_effect(self, n, rate, power):
        return self.effect


@pytest.mark.parametrize(
    ("effect_items", "expected"),
    [(10.758, 11), (11.0000000001, 11), (10.0, 10), (10.2, 11)],
)
def test_the_smallest_visible_change_rounds_up_but_not_past_a_whole_task(effect_items, expected):
    n = 28
    fake = _FakeStats(effect_items / n)
    assert evals_view.smallest_visible_change(n, 18, fake) == expected


def test_a_drifted_split_lock_is_named(monkeypatch, committed):
    monkeypatch.setattr(corpus_mod, "check_split_lock", lambda items: ["digest moved"])
    corpus = evals_view.corpus_view(corpus_mod, [])
    assert corpus["split_lock_holds"] is False
    text = visible_text(evals_view._corpus_panel(corpus))
    assert "the split lock does not hold" in text


def test_an_unreadable_corpus_says_nothing_about_it(monkeypatch):
    def boom():
        raise corpus_mod.CorpusError("broken")

    monkeypatch.setattr(corpus_mod, "load_corpus", boom)
    corpus = evals_view.corpus_view(corpus_mod, [])
    assert corpus == {"readable": False, "reason": "CorpusError"}
    assert "could not be read" in visible_text(evals_view._corpus_panel(corpus))


def test_the_lede_is_counted_from_the_record_not_written_down(every_state):
    """The committed record says 31 of 75; this one must say what it holds."""
    text = visible_text(_fragment(evals_view.build_record(every_state)))
    assert "2 of the 9 passes" in text


def test_run_labels_add_the_time_only_where_two_days_collide():
    runs = [{"run_id": "20260808_050610"}, {"run_id": "20260811_052310"}, {"run_id": "20260811_231500"}]
    assert evals_view.run_labels(runs) == {
        "20260808_050610": "Aug 8",
        "20260811_052310": "Aug 11 05:23",
        "20260811_231500": "Aug 11 23:15",
    }


# ── the route, its gate, and the console ─────────────────────────────


def test_the_route_serves_the_record_and_its_fragment():
    with TestClient(app) as client:
        response = client.get("/evals")
    assert response.status_code == 200
    body = response.json()
    assert body["recorded"] is True
    assert 'data-evals' in body["evals_html"]
    assert "%" not in json.dumps(body["evals_html"])


def test_the_route_is_neither_public_nor_operator_prefixed():
    """Delta §9.4: viewer-gated by default, and not under /v1/operator/."""
    assert ("GET", "/evals") not in _PUBLIC_EXACT
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/evals" in paths
    assert not any(p.startswith("/v1/operator/") and "eval" in p for p in paths)


def test_the_deployed_image_carries_no_eval_record():
    """Why `recorded: false` exists. If evals/ is ever copied in, §9.4 is stale."""
    copies = [
        line for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if line.strip().upper().startswith(("COPY", "ADD"))
    ]
    assert copies, "read no COPY lines, so this check proves nothing"
    assert not any("evals" in line for line in copies), copies
    assert any(line.split()[1:2] == ["*.py"] for line in copies), (
        "evals_view.py reaches the image through `COPY *.py`; without it the "
        "route would not exist there at all"
    )


def test_the_console_has_an_evals_view_that_asks_when_opened():
    html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    js = (TEMPLATES / "_dashboard.js").read_text(encoding="utf-8")
    assert 'id="tab-evals"' in html and 'data-tab="evals"' in html
    assert 'id="view-evals"' in html and 'id="evals-body"' in html
    assert re.search(r"const TABS = \[[^\]]*'evals'", js)
    assert "if (name === 'evals') loadEvals();" in js
    loader = js[js.index("async function loadEvals()"):]
    loader = loader[: loader.index("\n}\n")]
    assert "data.evals_html" in loader
    # The server formats every number. A loader that formats one can print
    # the rate the view exists not to print.
    assert "%" not in loader and "toFixed" not in loader
    assert "setInterval(loadEvals" not in js
