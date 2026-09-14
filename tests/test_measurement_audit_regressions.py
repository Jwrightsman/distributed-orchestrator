"""Authored controls for study completeness, identity, dependence and cost."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
import corpus  # noqa: E402
import grading  # noqa: E402
import runrecord  # noqa: E402
from scripts import ensemble_experiment as experiment  # noqa: E402
from scripts import eval_study_summary as summary  # noqa: E402


def manifest():
    return {
        "version": "1", "study_id": "planned", "item_ids": ["one", "two"],
        "arms": ["direct", "parallel"], "replicates": [0], "aggregation": "single_replicate",
        "identity": {"measurement_identity_version": "2", "measurement_digest": "fixture-digest",
                     "grader_version": "3", "model_digest": "fixture-model"},
        "budget_policy": {"metric": "homogeneous_hardware_seconds_v1",
                          "hardware_id": "fixture-hardware", "relative_tolerance": .25},
    }


def rows():
    return [{"study_id": "planned", "item_id": item, "arm": arm, "replicate": 0,
             "measurement_identity_version": "2", "measurement_digest": "fixture-digest",
             "grading": {"grader_version": "3"}, "model": {"digest": "fixture-model"},
             "graded": True, "passed": item == "one", "wall_clock_seconds": 10,
             "cost_capture_complete": True,
             "unit_costs": [{"unit_id": f"call-{i}", "inference_seconds": 10,
                             "hardware_id": "fixture-hardware", "tokens": {"prompt": 2, "completion": 3}}
                            for i in range(1 if arm == "direct" else 5)]}
            for item in ["one", "two"] for arm in ["direct", "parallel"]]


def test_truncating_every_arm_is_not_a_complete_study():
    with pytest.raises(summary.IncompleteStudy, match="never ran"):
        summary.check_complete(rows()[:2], manifest()["arms"], manifest())
    with pytest.raises(summary.IncompleteStudy, match="manifest"):
        summary.check_complete(rows(), manifest()["arms"])


def test_record_order_is_irrelevant_except_exact_key_supersession():
    records = rows()
    for ordered in (records, list(reversed(records))):
        summary.check_complete(ordered, manifest()["arms"], manifest())
        assert summary.outcomes_for(ordered, "direct") == {"one": True, "two": False}
        assert summary.cost_for(ordered, "parallel") == summary.cost_for(records, "parallel")
    revised = dict(records[0], passed=False)
    assert summary.outcomes_for([*records, revised], "direct")["one"] is False
    assert summary.outcomes_for([revised, *records], "direct")["one"] is True


def test_multiple_replicates_are_rejected_instead_of_overwritten():
    records = [rows()[0], dict(rows()[0], replicate=1, passed=False)]
    for ordered in (records, list(reversed(records))):
        with pytest.raises(summary.IncompleteStudy, match="multi-replicate"):
            summary.outcomes_for(ordered, "direct")
    planned = manifest()
    planned["replicates"] = [0, 1]
    with pytest.raises(ValueError, match="multi-replicate"):
        runrecord.validate_manifest(planned)


@pytest.mark.parametrize("replicate", [True, "0", 0.5, -1])
def test_replicate_identity_is_never_coerced(replicate):
    with pytest.raises(ValueError, match="replicate"):
        runrecord.latest_per_key([dict(rows()[0], replicate=replicate)])


@pytest.mark.parametrize("field", ["identity", "budget_policy"])
def test_malformed_manifest_blocks_summary(field):
    planned = manifest()
    planned[field] = []
    with pytest.raises(ValueError):
        runrecord.validate_manifest(planned)


def test_frozen_manifest_cannot_be_replaced_or_retrofitted(tmp_path):
    original = manifest()
    runrecord.write_manifest(tmp_path, original)
    changed = deepcopy(original)
    changed["item_ids"] = ["one"]
    with pytest.raises(ValueError, match="already frozen"):
        runrecord.write_manifest(tmp_path, changed)
    assert runrecord.load_manifest(tmp_path) == original
    other = tmp_path / "posthoc"
    other.mkdir()
    (other / "runs.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="after"):
        runrecord.write_manifest(other, original)


def test_measurement_identity_includes_expectations_schema_and_fixture_bytes(tmp_path):
    (tmp_path / "schemas").mkdir()
    (tmp_path / "inputs").mkdir()
    schema = tmp_path / "schemas" / "record.json"
    fixture = tmp_path / "inputs" / "input.txt"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    fixture.write_text("first", encoding="utf-8")
    item = corpus.CorpusItem("fixture", "script", "data_transformation", "authored", "taxonomy",
                             "development", {"artifact": "python", "checks": [
                                 {"kind": "stdout_json_schema", "schema": "record", "inputs": ["input.txt"]}]})
    def digest(value=item, version="3"):
        return corpus.measurement_digest([value], grader_version=version, fixtures_dir=tmp_path)
    first = digest()
    changed = replace(item, expect={"artifact": "python"})
    assert corpus.corpus_digest([item]) == corpus.corpus_digest([changed])  # historical v1 is explicit
    assert digest(changed) != first and digest(version="4") != first
    schema.write_text('{"type":"array"}', encoding="utf-8")
    assert digest() != first
    second = digest()
    fixture.write_text("second", encoding="utf-8")
    assert digest() != second
    fixture.unlink()
    with pytest.raises(corpus.CorpusError, match="missing"):
        digest()


def test_equal_latency_can_hide_five_times_the_hardware_work():
    direct, parallel = (summary.cost_for(rows(), arm) for arm in manifest()["arms"])
    assert direct["latency_seconds_total"] == parallel["latency_seconds_total"] == 20
    comparison = summary.compare_costs(direct, parallel, manifest()["budget_policy"])
    assert comparison["ratio"] == 5 and comparison["within_tolerance"] is False


@pytest.mark.parametrize("missing", ["timing", "tokens", "hardware", "capture", "latency"])
def test_missing_cost_measurement_suppresses_comparison(missing):
    records = rows()
    if missing in ("timing", "tokens", "hardware"):
        key = {"timing": "inference_seconds", "tokens": "tokens", "hardware": "hardware_id"}[missing]
        records[1]["unit_costs"][0][key] = None
    elif missing == "capture":
        records[1]["cost_capture_complete"] = False
    else:
        records[1]["wall_clock_seconds"] = None
    a, b = (summary.cost_for(records, arm) for arm in manifest()["arms"])
    assert summary.compare_costs(a, b, manifest()["budget_policy"])["comparable"] is False
    if missing == "timing":
        assert b["hardware_seconds_total"] is None
    if missing == "latency":
        assert b["latency_seconds_total"] is None


def test_descriptive_policy_never_claims_cost_matching():
    a = summary.cost_for(rows(), "direct")
    assert not summary.compare_costs(a, a, {"metric": "descriptive_only"})["comparable"]


def test_perfectly_correlated_candidates_gain_nothing_and_selection_is_distinct():
    groups = [{"item_id": "fixture", "execution_id": str(i), "selected_candidate_id": "0",
               "candidates": [{"candidate_id": str(j), "passed": passed} for j in range(5)]}
              for i, passed in enumerate([True, False])]
    observed = experiment.grouped_outcomes(groups)
    assert observed["any_pass_rate"] == observed["selected_pass_rate"] == .5
    assert observed["all_outcomes_equal_groups"] == 2
    assert .95 < experiment.ensemble_rate_empirical([True, False], 5) < .99
    assert experiment.grouped_outcomes(list(reversed(groups))) == observed
    groups[1]["candidates"][1]["passed"] = True
    observed = experiment.grouped_outcomes(groups)
    assert observed["any_pass_rate"] == 1 and observed["selected_pass_rate"] == .5
    groups[1].pop("selected_candidate_id")
    assert experiment.grouped_outcomes(groups)["selected_pass_rate"] is None


INTERACTIVE = ["web-snake", "web-pomodoro", "web-todo", "web-calculator",
               "web-memory-game", "web-markdown-preview"]


@pytest.mark.parametrize("item_id", INTERACTIVE)
def test_legacy_behavior_check_accepts_positive_and_rejects_broken_fixture(item_id):
    pytest.importorskip("playwright.sync_api")
    items = {item.id: item for item in corpus.load_corpus()}
    item = items[item_id]
    [spec] = [check for check in item.checks if check["kind"] == "html_behaviour"]
    positive, broken = [ROOT / "evals" / "fixtures" / path for path in spec["fixtures"]]
    good = grading.check_html_behaviour([str(positive)], spec)
    if not good.graded:
        pytest.skip(good.detail)
    bad = grading.check_html_behaviour([str(broken)], spec)
    assert good.passed, good.detail
    assert bad.graded and not bad.passed, bad.detail
    # The deliberately inert fixture still loads under the old browser check.
    assert grading.scoring.execute_html([str(broken)])["outcome"] == "browser_ok"


def test_new_rubric_starts_new_series_without_regrading_historical_rows():
    assert corpus.measurement_series() == "legacy-interactive-v3"
    assert grading.GRADER_VERSION == "3"
    historical_ids = ["20260806_195850", "20260808_050610", "20260809_053327",
                      "20260810_041455", "20260811_052310"]
    historical = [json.loads(line) for path in (
                      ROOT / "evals" / "results" / run_id / "results.jsonl" for run_id in historical_ids)
                  for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert historical and all("primary_pass" not in row for row in historical)
