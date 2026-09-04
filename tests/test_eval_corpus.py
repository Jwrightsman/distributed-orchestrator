"""The corpus, its bands, and the held-out split that must not move.

The load-bearing test in this file is `test_locked_split_has_not_changed`. A
confirmatory set that can be redrawn after seeing a result is not a
confirmatory set, and this project has already kept one prompt set alive on a
subgroup that looked good in hindsight. The digest makes redrawing it an edit
somebody has to make in a diff.

Every test here asserts it read a non-empty corpus before asserting anything
about it. Four test files in this repository have passed on zero inputs.
"""

import json
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))
import corpus as corpus_mod  # noqa: E402


@pytest.fixture(scope="module")
def items():
    loaded = corpus_mod.load_corpus()
    assert loaded, "the corpus is empty — every assertion below would be vacuous"
    return loaded


# -- structure --------------------------------------------------------------

def test_corpus_is_non_empty_and_unique(items):
    assert len(items) >= 28
    ids = [i.id for i in items]
    assert len(ids) == len(set(ids))


def test_every_item_declares_a_known_taxonomy(items):
    assert items
    for item in items:
        assert item.taxonomy in corpus_mod.TAXONOMY


def test_the_original_categories_all_survive(items):
    """Historical runs are only comparable if their categories still exist."""
    assert items
    categories = {i.category for i in items}
    for required in ("web_app", "cli_tool", "data_processing", "api", "algorithm", "vague"):
        assert required in categories


def test_loading_an_empty_corpus_raises(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"version": 1, "prompts": []}), encoding="utf-8")
    with pytest.raises(corpus_mod.CorpusError):
        corpus_mod.load_corpus(empty)


def test_a_duplicate_id_is_rejected(tmp_path):
    entry = {
        "id": "dupe", "category": "algorithm", "taxonomy": "algorithmic_kernel",
        "task": "do a thing", "origin": "taxonomy", "split": "development",
        "expect": {"artifact": "python"},
    }
    path = tmp_path / "dupe.json"
    path.write_text(json.dumps({"version": 1, "prompts": [entry, dict(entry)]}), encoding="utf-8")
    with pytest.raises(corpus_mod.CorpusError, match="duplicate"):
        corpus_mod.load_corpus(path)


# -- bands ------------------------------------------------------------------

@pytest.mark.parametrize(
    "passes,trials,expected",
    [
        (0, 5, "floor"),
        (1, 5, "floor"),
        (2, 5, "discriminating"),
        (3, 5, "discriminating"),
        (4, 5, "ceiling"),
        (5, 5, "ceiling"),
        (2, 10, "floor"),
        (3, 10, "discriminating"),
        (8, 10, "ceiling"),
    ],
)
def test_band_classification_is_reproducible(passes, trials, expected):
    assert corpus_mod.classify_band(passes, trials) == expected
    # Same inputs, same answer, every time — the band decides which items carry
    # a study, so it cannot depend on when it was computed.
    assert corpus_mod.classify_band(passes, trials) == expected


def test_the_published_data_points_land_where_they_should():
    """Snake at 2/10 is the floor; the chart at 10/10 is the ceiling."""
    assert corpus_mod.classify_band(2, 10) == "floor"
    assert corpus_mod.classify_band(10, 10) == "ceiling"


def test_banding_something_never_run_is_an_error():
    with pytest.raises(corpus_mod.CorpusError):
        corpus_mod.classify_band(0, 0)
    with pytest.raises(corpus_mod.CorpusError):
        corpus_mod.classify_band(6, 5)


def test_a_band_label_that_does_not_follow_from_its_evidence_is_rejected(tmp_path):
    entry = {
        "id": "liar", "category": "algorithm", "taxonomy": "algorithmic_kernel",
        "task": "do a thing", "origin": "taxonomy", "split": "development",
        "band": {"label": "ceiling", "passes": 1, "trials": 5, "source": "made up"},
        "expect": {"artifact": "python"},
    }
    path = tmp_path / "liar.json"
    path.write_text(json.dumps({"version": 1, "prompts": [entry]}), encoding="utf-8")
    with pytest.raises(corpus_mod.CorpusError, match="does not follow"):
        corpus_mod.load_corpus(path)


def test_band_distribution_counts_unbanded_items_rather_than_dropping_them(items):
    assert items
    distribution = corpus_mod.band_distribution(items)
    assert sum(distribution.values()) == len(items)
    assert "unbanded" in distribution


# -- the held-out split -----------------------------------------------------

def test_locked_split_has_not_changed(items):
    """The whole point of a confirmatory set.

    If this fails, either the confirmatory set was edited — in which case the
    edit and its reason belong in the same diff as a new `evals/split.lock.json`
    — or an item was renamed. Either way, results collected before and after
    the change are not from the same study.
    """
    assert items
    problems = corpus_mod.check_split_lock(items)
    assert problems == [], "\n".join(problems)


def test_the_lock_records_what_the_corpus_actually_says(items):
    assert items
    lock = corpus_mod.read_split_lock()
    rebuilt = corpus_mod.build_split_lock(items)
    assert lock["confirmatory_ids"] == rebuilt["confirmatory_ids"]
    assert lock["confirmatory_digest"] == rebuilt["confirmatory_digest"]
    assert lock["corpus_digest"] == rebuilt["corpus_digest"]
    assert lock["item_count"] == len(items)


def test_adding_an_item_to_the_confirmatory_set_breaks_the_lock(items):
    assert items
    lock = corpus_mod.read_split_lock()
    smuggled = list(items) + [
        corpus_mod.CorpusItem(
            id="smuggled-in", category="algorithm", taxonomy="algorithmic_kernel",
            task="added after the fact", origin="taxonomy", split="confirmatory",
            expect={"artifact": "python"},
        )
    ]
    problems = corpus_mod.check_split_lock(smuggled, lock)
    assert problems
    assert any("added" in p for p in problems)


def test_removing_a_confirmatory_item_breaks_the_lock(items):
    assert items
    lock = corpus_mod.read_split_lock()
    confirmatory = [i for i in items if i.split == "confirmatory"]
    assert confirmatory, "there is no confirmatory set to remove from"
    trimmed = [i for i in items if i.id != confirmatory[0].id]
    problems = corpus_mod.check_split_lock(trimmed, lock)
    assert problems


def test_the_lock_check_reports_an_empty_corpus_rather_than_passing():
    problems = corpus_mod.check_split_lock([])
    assert problems and "empty" in problems[0]


def test_the_confirmatory_set_is_big_enough_to_decide_something(items):
    """Sized from the power analysis, not chosen for roundness.

    `stats.required_n(0.35, 18/28)` is 35 items: the pre-registered
    decomposition study is powered for the ~35 point gap the Aug 15 pilot
    suggests, and nothing smaller.
    """
    assert items
    confirmatory = corpus_mod.confirmatory_items(items)
    assert len(confirmatory) >= 35


# -- origin, and the rule that keeps the confirmatory set honest ------------

def test_no_confirmatory_item_came_from_an_observed_failure(items):
    assert items
    for item in corpus_mod.confirmatory_items(items):
        assert item.origin == "taxonomy", (
            f"{item.id} has origin {item.origin!r}; a corpus grown from a failure log "
            "measures whether those failures were fixed, not capability"
        )


def test_an_observed_failure_item_cannot_be_marked_confirmatory(tmp_path):
    entry = {
        "id": "from-a-failure", "category": "web_app", "taxonomy": "interactive_artifact",
        "task": "the thing that broke last week", "origin": "observed_failure",
        "split": "confirmatory", "expect": {"artifact": "html"},
    }
    path = tmp_path / "tainted.json"
    path.write_text(json.dumps({"version": 1, "prompts": [entry]}), encoding="utf-8")
    with pytest.raises(corpus_mod.CorpusError, match="development-only"):
        corpus_mod.load_corpus(path)


def test_the_original_items_are_development_only(items):
    """They have been iterated against for four prompt-set versions."""
    assert items
    legacy = [i for i in items if i.origin == "legacy"]
    assert len(legacy) == 28
    assert all(i.split == "development" for i in legacy)


# -- digests ----------------------------------------------------------------

def test_corpus_digest_changes_when_a_prompt_is_reworded(items):
    assert items
    original = corpus_mod.corpus_digest(items)
    reworded = list(items[1:]) + [
        corpus_mod.CorpusItem(
            id=items[0].id, category=items[0].category, taxonomy=items[0].taxonomy,
            task=items[0].task + " (but say please)", origin=items[0].origin,
            split=items[0].split, expect=items[0].expect, band=items[0].band,
        )
    ]
    assert corpus_mod.corpus_digest(reworded) != original


def test_corpus_digest_ignores_ordering(items):
    assert items
    assert corpus_mod.corpus_digest(items) == corpus_mod.corpus_digest(list(reversed(items)))


# -- the fixture corpus the controls use ------------------------------------

def test_the_control_fixture_corpus_is_a_valid_corpus():
    fixture = corpus_mod.load_corpus(EVALS / "fixtures" / "control_corpus.json")
    assert len(fixture) >= 8
    assert all(i.expect.get("checks") for i in fixture), (
        "a fixture item with no output-level check cannot demonstrate that the "
        "grader sees anything beyond 'it ran'"
    )


# -- the fixtures every declared check depends on ---------------------------

def test_every_declared_fixture_and_schema_exists(items):
    assert items
    inputs = EVALS / "fixtures" / "inputs"
    schemas = EVALS / "fixtures" / "schemas"
    missing = []
    checked = 0
    for item in items:
        for spec in item.checks:
            checked += 1
            for name in spec.get("inputs", []):
                if not (inputs / name).is_file():
                    missing.append(f"{item.id}: input {name}")
            if spec["kind"] == "stdout_json_schema":
                if not (schemas / f"{spec['schema']}.json").is_file():
                    missing.append(f"{item.id}: schema {spec['schema']}")
    assert checked > 0, "no item declares an output-level check — nothing was verified"
    assert missing == [], missing


# -- the bands that are known-suspect, not merely provisional ---------------

def test_every_html_band_from_the_committed_runs_is_marked_known_suspect():
    """PR #71 finding 1, recorded on the items it reaches rather than in prose.

    Before the grading correction an HTML artifact "executed" if it loaded
    without throwing. `web-snake` bands ceiling at 5/5 under that check and was
    measured at 2/10 under the behavioural checker. Every band derived from the
    five committed runs for an HTML item inherits the weaker check, so each one
    says so in its own record — a band travels further than the document
    explaining it.
    """
    data = json.loads((EVALS / "prompts.json").read_text(encoding="utf-8"))
    prompts = data["prompts"]
    assert prompts, "the corpus is empty — this test read nothing"
    html_banded = [
        entry for entry in prompts
        if entry.get("band") and entry.get("expect", {}).get("artifact") == "html"
    ]
    assert html_banded, "no banded HTML item — this test asserted nothing"
    for entry in html_banded:
        band = entry["band"]
        assert band.get("known_suspect") is True, entry["id"]
        assert "browser_ok" in band.get("caveat", ""), entry["id"]
        assert band.get("grader") == "legacy (pre-correction)", entry["id"]
    # And the marking is confined to the items the defect reaches: a Python
    # item graded by the same runs is provisional, not known-suspect.
    other_banded = [
        entry for entry in prompts
        if entry.get("band") and entry.get("expect", {}).get("artifact") != "html"
    ]
    assert other_banded, "no banded non-HTML item — this test asserted nothing"
    assert not any(entry["band"].get("known_suspect") for entry in other_banded)


def test_every_band_records_which_grader_produced_it():
    """Two numbers from two checkers is how this project got here twice."""
    data = json.loads((EVALS / "prompts.json").read_text(encoding="utf-8"))
    banded = [entry for entry in data["prompts"] if entry.get("band")]
    assert banded, "no banded item — this test read nothing"
    for entry in banded:
        assert entry["band"].get("grader"), entry["id"]


def test_web_app_is_exactly_the_html_family_so_the_marking_covers_it():
    """The caveat is keyed on the artifact; the published figures say `web_app`.

    They coincide today. If they ever stop, the marking follows the defect —
    the HTML execution check — and this test fails so the divergence is a
    decision rather than a silent gap.
    """
    data = json.loads((EVALS / "prompts.json").read_text(encoding="utf-8"))
    banded = [entry for entry in data["prompts"] if entry.get("band")]
    html = {e["id"] for e in banded if e.get("expect", {}).get("artifact") == "html"}
    web_app = {e["id"] for e in banded if e["category"] == "web_app"}
    assert html and web_app
    assert html == web_app
