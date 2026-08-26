"""Deterministic offline evaluation coverage for Theme 2C shadow evidence."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import capability_evidence_eval as evaluation

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPOSITORY_ROOT / "scripts" / "capability_evidence_eval.py"


def _fixture() -> dict:
    return evaluation.load_fixture()


def _by_scenario(report: dict) -> dict[str, dict]:
    return {case["scenario"]: case for case in report["cases"]}


def test_report_is_deterministic_and_covers_required_scenarios():
    fixture = _fixture()

    first = evaluation.evaluate_fixture(fixture)
    second = evaluation.evaluate_fixture(copy.deepcopy(fixture))

    assert evaluation.render_report(first) == evaluation.render_report(second)
    assert {case["scenario"] for case in first["cases"]} >= evaluation.REQUIRED_SCENARIOS
    assert first["summary"] == {
        "case_count": 10,
        "claim_only": {
            "case_count": 10,
            "contract_floor_pass_count": 6,
            "contract_floor_pass_rate": 0.6,
            "deadline_success_count": 7,
            "deadline_success_rate": 0.7,
        },
        "delta": {
            "contract_floor_pass_count": 3,
            "deadline_success_count": 2,
        },
        "excluded_non_node_fault_count": 3,
        "hard_ineligible_candidates_excluded": 1,
        "ignored_prior_scope_samples": 120,
        "invariant_failures": 0,
        "preference_evaluable_comparison": {
            "claim_only": {
                "case_count": 6,
                "contract_floor_pass_count": 2,
                "contract_floor_pass_rate": 0.333333,
                "deadline_success_count": 3,
                "deadline_success_rate": 0.5,
            },
            "delta": {
                "contract_floor_pass_count": 3,
                "deadline_success_count": 2,
            },
            "shadow_preference": {
                "case_count": 6,
                "contract_floor_pass_count": 5,
                "contract_floor_pass_rate": 0.833333,
                "deadline_success_count": 5,
                "deadline_success_rate": 0.833333,
            },
        },
        "shadow_outcomes": {"different": 5, "no_preference": 4, "same": 1},
        "shadow_with_claim_fallback": {
            "case_count": 10,
            "contract_floor_pass_count": 9,
            "contract_floor_pass_rate": 0.9,
            "deadline_success_count": 9,
            "deadline_success_rate": 0.9,
        },
    }


def test_cold_start_and_scope_changes_never_inherit_prior_evidence():
    cases = _by_scenario(evaluation.evaluate_fixture(_fixture()))

    cold_start = cases["cold_start"]
    assert cold_start["shadow"]["outcome"] == "no_preference"
    assert cold_start["shadow"]["with_claim_fallback_candidate_id"] == "cold-fresh"

    for scenario in ("descriptor_reset", "model_reset", "task_class_reset"):
        case = cases[scenario]
        assert case["ignored_prior_scope_samples"] == 40
        assert case["shadow"]["outcome"] == "no_preference"
        assert case["shadow"]["rationale_code"] == "insufficient_deadline_evidence"
        assert case["shadow"]["preferred_candidate_id"] is None


def test_few_failures_exclusions_and_held_out_regression_are_visible():
    cases = _by_scenario(evaluation.evaluate_fixture(_fixture()))

    assert cases["few_failures"]["shadow"]["preferred_candidate_id"] == "few-observed"
    excluded = cases["excluded_non_node_causes"]
    assert excluded["excluded_non_node_fault_count"] == 3
    assert excluded["shadow"]["outcome"] == "same"

    shifted = cases["distribution_shift"]
    assert shifted["labeled_outcomes"]["claim_only"] == {
        "deadline_success": True,
        "contract_floor_pass": True,
    }
    assert shifted["labeled_outcomes"]["shadow_with_claim_fallback"] == {
        "deadline_success": False,
        "contract_floor_pass": False,
    }


def test_hard_ineligible_candidates_are_filtered_before_production_evaluation(monkeypatch):
    production_evaluator = evaluation.evaluate_shadow_preference
    observed_candidate_sets: list[set[str]] = []

    def checked_evaluator(**kwargs):
        candidate_ids = {candidate.candidate_id for candidate in kwargs["candidates"]}
        assert all(candidate.hard_eligible is True for candidate in kwargs["candidates"])
        assert "ineligible-super" not in candidate_ids
        observed_candidate_sets.append(candidate_ids)
        return production_evaluator(**kwargs)

    monkeypatch.setattr(evaluation, "evaluate_shadow_preference", checked_evaluator)

    report = evaluation.evaluate_fixture(_fixture())

    hard_filter = _by_scenario(report)["hard_ineligible_exclusion"]
    assert hard_filter["hard_ineligible_candidate_ids"] == ["ineligible-super"]
    assert hard_filter["eligible_candidate_ids"] == ["eligible-a", "eligible-b"]
    assert hard_filter["shadow"]["preferred_candidate_id"] == "eligible-b"
    assert len(observed_candidate_sets) == 2 * len(report["cases"])


def test_cli_output_is_stable_and_independent_of_working_directory(tmp_path):
    command = [sys.executable, str(SCRIPT)]

    first = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, check=False)
    second = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, check=False)

    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    assert json.loads(first.stdout) == evaluation.evaluate_fixture(_fixture())


def test_cli_does_not_fail_when_shadow_underperforms(tmp_path):
    fixture = _fixture()
    for case in fixture["cases"]:
        for candidate in case["candidates"]:
            candidate["labels"] = {
                "deadline_success": False,
                "contract_floor_pass": False,
            }
        actual = next(
            candidate
            for candidate in case["candidates"]
            if candidate["candidate_id"] == case["actual_candidate_id"]
        )
        actual["labels"] = {
            "deadline_success": True,
            "contract_floor_pass": True,
        }
    fixture_path = tmp_path / "shadow-underperforms.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--fixture", str(fixture_path)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    report = json.loads(completed.stdout)
    assert report["summary"]["delta"]["deadline_success_count"] < 0
    assert report["summary"]["delta"]["contract_floor_pass_count"] < 0
    assert report["summary"]["invariant_failures"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda fixture: fixture.update(minimum_samples=0),
        lambda fixture: fixture["cases"][0]["expected"].update(outcome="same"),
        lambda fixture: fixture["cases"][0]["candidates"][0].update(hard_eligible="yes"),
    ],
)
def test_invalid_fixture_or_invariant_returns_nonzero(tmp_path, mutation):
    fixture = _fixture()
    mutation(fixture)
    fixture_path = tmp_path / "invalid.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--fixture", str(fixture_path)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.startswith("capability evidence evaluation failed:")
