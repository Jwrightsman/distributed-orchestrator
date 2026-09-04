"""Grading rules: what an item is allowed to pass on.

The three properties worth having:

1. **Grading a code artifact requires running it.** Not parsing it, not
   grepping it. A file containing the string `setInterval` says nothing about
   whether the loop it starts does anything, and that reasoning has produced a
   confident wrong answer twice in this project's history.
2. **Grading is deterministic.** The same artifact graded twice gives the same
   verdict, or two runs of one study are not comparable with each other.
3. **Ungraded is not failed.** A check that could not run is recorded as such,
   because "the browser was missing" and "the page is broken" are different
   facts and only one of them is a measurement.

No Ollama, no network. Playwright is optional: the browser-backed checks are
skipped when it is absent, and the point of the skip is that the harness says
so rather than quietly scoring a page it never loaded.
"""

import json
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))
import corpus as corpus_mod  # noqa: E402
import grading  # noqa: E402


def make_item(**over):
    base = {
        "id": "t-1",
        "category": "algorithm",
        "taxonomy": "algorithmic_kernel",
        "task": "print something",
        "origin": "taxonomy",
        "split": "development",
        "expect": {"artifact": "python", "keywords": []},
    }
    base.update(over)
    return corpus_mod.CorpusItem(
        id=base["id"], category=base["category"], taxonomy=base["taxonomy"],
        task=base["task"], origin=base["origin"], split=base["split"],
        expect=base["expect"],
    )


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


# -- a code artifact has to run ---------------------------------------------

def test_a_working_script_passes(tmp_path):
    path = write(tmp_path, "ok.py", "print('MARKER-OK')\n")
    item = make_item(expect={"artifact": "python", "keywords": ["marker"],
                             "checks": [{"kind": "stdout_contains",
                                         "substrings": ["MARKER-OK"]}]})
    result = grading.grade(item, [path])
    assert result.graded and result.passed


def test_code_that_parses_but_crashes_fails(tmp_path):
    """Syntactically perfect, and it does not run. That is a failure."""
    path = write(tmp_path, "boom.py", "raise ValueError('nope')\n")
    item = make_item()
    result = grading.grade(item, [path])
    assert result.graded
    assert not result.passed
    assert "runs" in result.failed_checks
    assert "parses" not in result.failed_checks


def test_code_that_runs_but_answers_the_wrong_question_fails(tmp_path):
    """The failure a parse-and-run instrument cannot see.

    This is the shuffled positive control in miniature: valid output, cleanly
    executed, for a different question. Only the output check catches it.
    """
    path = write(tmp_path, "wrong.py", "print('something else entirely')\n")
    item = make_item(expect={
        "artifact": "python",
        "keywords": [],
        "checks": [{"kind": "stdout_contains", "substrings": ["MARKER-OK"]}],
    })
    result = grading.grade(item, [path])
    assert result.graded and not result.passed
    assert result.failed_checks == ["stdout_contains"]

    weak = [c for c in result.checks if c.kind in ("parses", "runs")]
    assert weak and all(c.passed for c in weak), (
        "the point of this test is that parse-and-run alone calls this a pass"
    )


def test_a_deliberately_broken_artifact_is_detected(tmp_path):
    path = write(tmp_path, "broken.py", "def f(:\n    pass\n")
    result = grading.grade(make_item(), [path])
    assert result.graded and not result.passed
    assert "parses" in result.failed_checks


def test_truncated_output_is_detected(tmp_path):
    """What the truncating positive control does to an artifact."""
    good = 'print("MARKER-OK, and then a good deal more output after it")\n'
    path = write(tmp_path, "cut.py", good[: int(len(good) * 0.45)])
    result = grading.grade(make_item(), [path])
    assert result.graded and not result.passed
    assert "parses" in result.failed_checks


def test_a_truncation_that_lands_on_valid_syntax_survives_parse_and_run(tmp_path):
    """Found by this test, and worth keeping as a statement about the checks.

    `def report():` followed by a bare `return` is valid Python that exits
    cleanly, because nothing ever calls it. Truncation is only reliably caught
    by a check that looks at the output — which is the same lesson as the
    shuffled control, arrived at from the other direction.
    """
    good = "def report():\n    return 'MARKER-OK'\n\n\nprint(report())\n"
    path = write(tmp_path, "lucky.py", good[: int(len(good) * 0.45)])

    bare = grading.grade(make_item(), [path])
    assert bare.graded and bare.passed, "the premise of this test is that it slips through"

    with_output_check = grading.grade(
        make_item(expect={
            "artifact": "python", "keywords": [],
            "checks": [{"kind": "stdout_contains", "substrings": ["MARKER-OK"]}],
        }),
        [path],
    )
    assert with_output_check.graded and not with_output_check.passed


def test_no_files_at_all_is_graded_as_a_failure_not_skipped():
    result = grading.grade(make_item(), [])
    assert result.graded, "producing nothing is a result, not an unrun check"
    assert not result.passed


def test_wrong_artifact_kind_is_caught(tmp_path):
    path = write(tmp_path, "game.py", "print('hi')\n")
    item = make_item(expect={"artifact": "html", "keywords": []})
    result = grading.grade(item, [path])
    assert not result.passed
    assert "artifact_kind" in result.failed_checks


# -- determinism ------------------------------------------------------------

def test_grading_the_same_artifact_twice_gives_the_same_verdict(tmp_path):
    path = write(tmp_path, "same.py", "print('MARKER-OK')\n")
    item = make_item(expect={"artifact": "python", "keywords": ["marker"],
                             "checks": [{"kind": "stdout_contains",
                                         "substrings": ["MARKER-OK"]}]})
    first = grading.grade(item, [path])
    second = grading.grade(item, [path])
    assert first.as_record() == second.as_record()
    assert first.deterministic


def test_determinism_holds_for_a_failing_artifact_too(tmp_path):
    path = write(tmp_path, "bad.py", "import definitely_not_a_real_package_xyz\n")
    item = make_item()
    assert grading.grade(item, [path]).as_record() == grading.grade(item, [path]).as_record()


def test_the_run_cache_does_not_leak_between_gradings(tmp_path):
    """An edited artifact must be re-run, not answered from a stale cache."""
    path = tmp_path / "edit.py"
    item = make_item(expect={"artifact": "python", "keywords": [],
                             "checks": [{"kind": "stdout_contains",
                                         "substrings": ["MARKER-OK"]}]})
    path.write_text("print('MARKER-OK')\n", encoding="utf-8")
    assert grading.grade(item, [str(path)]).passed
    path.write_text("print('changed my mind')\n", encoding="utf-8")
    assert not grading.grade(item, [str(path)]).passed


# -- ungraded is not failed -------------------------------------------------

def test_an_unknown_check_kind_leaves_the_item_ungraded(tmp_path):
    path = write(tmp_path, "ok.py", "print('hi')\n")
    item = make_item(expect={"artifact": "python", "keywords": [],
                             "checks": [{"kind": "vibes"}]})
    result = grading.grade(item, [path])
    assert not result.graded
    assert "vibes" in result.ungraded_checks
    assert not result.passed


def test_a_missing_fixture_leaves_the_item_ungraded(tmp_path):
    path = write(tmp_path, "reads.py", "print(open('nowhere.csv').read())\n")
    item = make_item(expect={
        "artifact": "python", "keywords": [],
        "checks": [{"kind": "stdout_contains", "substrings": ["x"],
                    "inputs": ["not-a-real-fixture.csv"]}],
    })
    result = grading.grade(item, [path])
    assert not result.graded
    assert "stdout_contains" in result.ungraded_checks


def test_a_missing_schema_leaves_the_item_ungraded(tmp_path):
    path = write(tmp_path, "j.py", "print('{}')\n")
    item = make_item(expect={
        "artifact": "python", "keywords": [],
        "checks": [{"kind": "stdout_json_schema", "schema": "no_such_schema"}],
    })
    result = grading.grade(item, [path])
    assert not result.graded


def test_ungraded_is_reported_separately_from_failed(tmp_path):
    path = write(tmp_path, "b.py", "raise SystemExit(1)\n")
    item = make_item(expect={"artifact": "python", "keywords": [],
                             "checks": [{"kind": "vibes"}]})
    result = grading.grade(item, [path])
    assert result.failed_checks and result.ungraded_checks
    assert set(result.failed_checks).isdisjoint(result.ungraded_checks)


# -- schema-backed grading --------------------------------------------------

def test_json_output_is_validated_against_the_committed_schema(tmp_path):
    path = write(
        tmp_path, "j.py",
        "import json\nprint(json.dumps([{'name': 'a'}, {'name': 'b'}]))\n",
    )
    item = make_item(expect={
        "artifact": "python", "keywords": [],
        "checks": [{"kind": "stdout_json_schema", "schema": "record_array"}],
    })
    assert grading.grade(item, [path]).passed


def test_an_empty_array_does_not_satisfy_the_record_schema(tmp_path):
    """The vacuous pass this instrument exists to stop.

    A script that extracts nothing and prints `[]` is valid JSON of the right
    outer type. `record_array` sets minItems to 1 for exactly this reason.
    """
    path = write(tmp_path, "empty.py", "print('[]')\n")
    item = make_item(expect={
        "artifact": "python", "keywords": [],
        "checks": [{"kind": "stdout_json_schema", "schema": "record_array"}],
    })
    result = grading.grade(item, [path])
    assert result.graded and not result.passed


def test_output_that_is_not_json_fails_the_schema_check(tmp_path):
    path = write(tmp_path, "prose.py", "print('I would rather write an essay')\n")
    item = make_item(expect={
        "artifact": "python", "keywords": [],
        "checks": [{"kind": "stdout_json_schema", "schema": "record_array"}],
    })
    result = grading.grade(item, [path])
    assert result.graded and not result.passed


def test_json_is_found_even_with_a_banner_printed_first(tmp_path):
    path = write(
        tmp_path, "chatty.py",
        "print('Analysing...')\nprint('[{\"name\": \"a\"}]')\n",
    )
    item = make_item(expect={
        "artifact": "python", "keywords": [],
        "checks": [{"kind": "stdout_json_schema", "schema": "record_array"}],
    })
    assert grading.grade(item, [path]).passed


def test_every_committed_schema_is_valid_json_schema():
    import jsonschema

    schemas = sorted((EVALS / "fixtures" / "schemas").glob("*.json"))
    assert schemas, "no schemas were found — the check below would be vacuous"
    for path in schemas:
        schema = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)


# -- fixture inputs ---------------------------------------------------------

def test_a_declared_fixture_is_present_in_the_working_directory(tmp_path):
    path = write(
        tmp_path, "reads.py",
        "import csv\n"
        "with open('sales.csv', encoding='utf-8') as fh:\n"
        "    rows = list(csv.DictReader(fh))\n"
        "print(len(rows))\n",
    )
    item = make_item(expect={
        "artifact": "python", "keywords": [],
        "checks": [{"kind": "stdout_contains", "substrings": ["8"],
                    "inputs": ["sales.csv"]}],
    })
    result = grading.grade(item, [path])
    assert result.graded and result.passed


def test_the_execution_check_sees_the_fixtures_too(tmp_path):
    """Otherwise a correct program that reads its input scores as broken."""
    path = write(tmp_path, "needs.py", "print(open('sales.csv', encoding='utf-8').read()[:5])\n")
    item = make_item(expect={
        "artifact": "python", "keywords": [],
        "checks": [{"kind": "stdout_contains", "substrings": ["region"],
                    "inputs": ["sales.csv"]}],
    })
    result = grading.grade(item, [path])
    runs = [c for c in result.checks if c.kind == "runs"][0]
    assert runs.passed, runs.detail


# -- the grader version travels with the verdict ----------------------------

def test_the_grader_version_is_recorded_on_every_verdict(tmp_path):
    path = write(tmp_path, "ok.py", "print('hi')\n")
    record = grading.grade(make_item(), [path]).as_record()
    assert record["grader_version"] == grading.GRADER_VERSION
    assert record["grader_version"], "an unversioned grader makes two runs incomparable"


# -- HTML behaviour, when a browser is available ----------------------------

def _has_playwright() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _has_playwright(), reason="playwright is not installed")
def test_a_blank_canvas_fails_the_behaviour_check(tmp_path):
    """The failure `no console errors` calls a pass.

    Eight of the ten Snake runs behind the published 2/10 loaded cleanly, threw
    no JavaScript errors, and never drew anything.
    """
    path = write(
        tmp_path, "blank.html",
        "<!DOCTYPE html><html><body><canvas id='c' width='100' height='100'></canvas>"
        "<script>const c = document.getElementById('c');</script></body></html>",
    )
    item = make_item(expect={
        "artifact": "html", "keywords": [],
        "checks": [{"kind": "html_behaviour", "canvas_drawn": True}],
    })
    result = grading.grade(item, [path])
    assert result.graded and not result.passed


@pytest.mark.skipif(not _has_playwright(), reason="playwright is not installed")
def test_a_canvas_that_is_drawn_on_passes(tmp_path):
    path = write(
        tmp_path, "drawn.html",
        "<!DOCTYPE html><html><body><canvas id='c' width='100' height='100'></canvas>"
        "<script>const x = document.getElementById('c').getContext('2d');"
        "x.fillStyle = '#0f0'; x.fillRect(0, 0, 50, 50);</script></body></html>",
    )
    item = make_item(expect={
        "artifact": "html", "keywords": [],
        "checks": [{"kind": "html_behaviour", "canvas_drawn": True}],
    })
    result = grading.grade(item, [path])
    assert result.graded and result.passed, result.checks


@pytest.mark.skipif(not _has_playwright(), reason="playwright is not installed")
def test_the_behaviour_check_declares_itself_non_deterministic(tmp_path):
    path = write(tmp_path, "p.html", "<!DOCTYPE html><html><body><p>hi</p></body></html>")
    item = make_item(expect={
        "artifact": "html", "keywords": [],
        "checks": [{"kind": "html_behaviour", "text_present": ["hi"]}],
    })
    result = grading.grade(item, [path])
    behaviour = [c for c in result.checks if c.kind == "html_behaviour"][0]
    assert behaviour.deterministic is False
    assert result.deterministic is False


@pytest.mark.skipif(not _has_playwright(), reason="playwright is not installed")
def test_text_inside_a_hidden_overlay_does_not_count_as_visible(tmp_path):
    """One of the three defects that made the showcase checker call games broken.

    An `<h2>GAME OVER</h2>` inside a `display:none` overlay reports itself
    visible if you read the element's own computed style. The whole ancestor
    chain is what decides.
    """
    path = write(
        tmp_path, "overlay.html",
        "<!DOCTYPE html><html><body><p>playing</p>"
        "<div style='display:none'><h2>GAME OVER</h2></div></body></html>",
    )
    item = make_item(expect={
        "artifact": "html", "keywords": [],
        "checks": [{"kind": "html_behaviour", "forbidden_text": ["GAME OVER"],
                    "text_present": ["playing"]}],
    })
    result = grading.grade(item, [path])
    assert result.graded and result.passed, result.checks


@pytest.mark.skipif(not _has_playwright(), reason="playwright is not installed")
def test_forbidden_text_that_is_actually_visible_fails(tmp_path):
    path = write(
        tmp_path, "dead.html",
        "<!DOCTYPE html><html><body><h2>GAME OVER</h2></body></html>",
    )
    item = make_item(expect={
        "artifact": "html", "keywords": [],
        "checks": [{"kind": "html_behaviour", "forbidden_text": ["GAME OVER"]}],
    })
    result = grading.grade(item, [path])
    assert result.graded and not result.passed


@pytest.mark.skipif(not _has_playwright(), reason="playwright is not installed")
def test_fixed_position_text_still_counts_as_visible(tmp_path):
    """`offsetParent` is null for a position:fixed element, which is not hidden."""
    path = write(
        tmp_path, "fixed.html",
        "<!DOCTYPE html><html><body>"
        "<div style='position:fixed;top:0;left:0'>Mon</div></body></html>",
    )
    item = make_item(expect={
        "artifact": "html", "keywords": [],
        "checks": [{"kind": "html_behaviour", "text_present": ["Mon"]}],
    })
    result = grading.grade(item, [path])
    assert result.graded and result.passed, result.checks
