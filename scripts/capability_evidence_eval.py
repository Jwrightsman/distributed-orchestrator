"""Deterministic offline evaluation of the Theme 2C shadow policy.

The fixture contains synthetic aggregate evidence and held-out binary outcomes.
This script compares the existing claim-only choice with the production pure
shadow evaluator. It never contacts a coordinator, worker, model, or network.

Run from any directory:

    python scripts/capability_evidence_eval.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = REPOSITORY_ROOT / "tests" / "fixtures" / "capability_evidence_eval_v1.json"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from capability_evidence import (  # noqa: E402
    BinaryAggregate,
    EligibleShadowCandidate,
    EvidenceScope,
    ScopeAggregate,
    evaluate_shadow_preference,
    future_active_experiment_eligibility,
)

REPORT_VERSION = "2"
FIXTURE_VERSION = "1"
MAX_CASES = 100
MAX_CANDIDATES = 16
ALLOWED_TASK_CLASSES = frozenset({"dag_subtask", "candidate"})
ALLOWED_EXCLUDED_CAUSES = frozenset(
    {
        "caller_cancellation",
        "coordinator_shutdown",
        "coordinator_persistence_failure",
    }
)
REQUIRED_SCENARIOS = frozenset(
    {
        "sufficient_evidence",
        "contract_floor_tiebreak",
        "cold_start",
        "descriptor_reset",
        "model_reset",
        "task_class_reset",
        "few_failures",
        "excluded_non_node_causes",
        "hard_ineligible_exclusion",
    }
)


class FixtureError(ValueError):
    """The synthetic fixture is malformed or violates an evaluation invariant."""


def _object(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise FixtureError(f"{field} must be an object with string keys")
    return value


def _keys(
    value: dict[str, Any],
    *,
    field: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing:
        raise FixtureError(f"{field} is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        raise FixtureError(f"{field} has unknown keys: {', '.join(sorted(unknown))}")


def _text(value: object, *, field: str, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise FixtureError(f"{field} must be 1-{maximum} printable characters without outer whitespace")
    return value


def _integer(value: object, *, field: str, minimum: int = 0, maximum: int = 10_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise FixtureError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _boolean(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise FixtureError(f"{field} must be a boolean")
    return value


def _number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FixtureError(f"{field} must be a finite non-negative number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise FixtureError(f"{field} must be a finite non-negative number")
    return parsed


def _digest(domain: str, value: str) -> str:
    return hashlib.sha256(f"{domain}\0{value}".encode()).hexdigest()


def _scope(raw: object, *, field: str) -> EvidenceScope:
    data = _object(raw, field=field)
    _keys(
        data,
        field=field,
        required={
            "enrollment_id",
            "descriptor_seed",
            "model_name",
            "model_digest_seed",
            "task_class",
        },
    )
    task_class = _text(data["task_class"], field=f"{field}.task_class")
    if task_class not in ALLOWED_TASK_CLASSES:
        raise FixtureError(f"{field}.task_class must be dag_subtask or candidate")
    descriptor_seed = _text(data["descriptor_seed"], field=f"{field}.descriptor_seed")
    raw_model_digest_seed = data["model_digest_seed"]
    model_digest_seed = (
        None
        if raw_model_digest_seed is None
        else _text(raw_model_digest_seed, field=f"{field}.model_digest_seed")
    )
    return EvidenceScope(
        enrollment_id=_text(data["enrollment_id"], field=f"{field}.enrollment_id"),
        descriptor_version="1",
        descriptor_hash=_digest("synthetic-descriptor-v1", descriptor_seed),
        executor_kind="ollama",
        executor_version="synthetic-v1",
        worker_protocol_version="1",
        model_provider="ollama",
        model_name=_text(data["model_name"], field=f"{field}.model_name"),
        model_digest=(
            f"sha256:{_digest('synthetic-model-v1', model_digest_seed)}"
            if model_digest_seed is not None
            else None
        ),
        model_variant=None,
        task_class=task_class,  # type: ignore[arg-type]
        evidence_role="production",
    )


def _count_pair(
    raw: object,
    *,
    field: str,
    positive_name: str,
) -> tuple[int, int]:
    data = _object(raw, field=field)
    _keys(data, field=field, required={"samples", positive_name})
    samples = _integer(data["samples"], field=f"{field}.samples")
    positives = _integer(data[positive_name], field=f"{field}.{positive_name}")
    if positives > samples:
        raise FixtureError(f"{field}.{positive_name} cannot exceed samples")
    return samples, positives


def _binary(samples: int, positives: int) -> BinaryAggregate:
    if samples == 0:
        return BinaryAggregate(0, 0, 0, None, None, None)
    rate = positives / samples
    z = 1.959963984540054
    denominator = 1 + z * z / samples
    center = (rate + z * z / (2 * samples)) / denominator
    margin = (
        z
        * math.sqrt(rate * (1 - rate) / samples + z * z / (4 * samples * samples))
        / denominator
    )
    return BinaryAggregate(
        sample_count=samples,
        positive_count=positives,
        negative_count=samples - positives,
        rate=rate,
        wilson_low=max(0.0, center - margin),
        wilson_high=min(1.0, center + margin),
    )


def _aggregate(
    raw: object,
    *,
    scope: EvidenceScope,
    minimum_samples: int,
    field: str,
) -> ScopeAggregate:
    data = _object(raw, field=field)
    _keys(data, field=field, required={"deadline", "contract_floor"})
    deadline_samples, deadline_successes = _count_pair(
        data["deadline"], field=f"{field}.deadline", positive_name="successes"
    )
    contract_samples, contract_passes = _count_pair(
        data["contract_floor"],
        field=f"{field}.contract_floor",
        positive_name="passes",
    )
    return ScopeAggregate(
        scope=scope,
        observation_count=deadline_samples + contract_samples,
        settlement_count=deadline_successes,
        settled_output_count=deadline_successes,
        settled_worker_error_count=0,
        settled_empty_output_count=0,
        deadline_completion=_binary(deadline_samples, deadline_successes),
        contract_floor=_binary(contract_samples, contract_passes),
        sampled_agreement=_binary(0, 0),
        lease_expiration_count=deadline_samples - deadline_successes,
        worker_disconnect_count=0,
        latency_sample_count=0,
        recent_median_latency_seconds=None,
        throughput_sample_count=0,
        recent_median_output_bytes_per_second=None,
        minimum_samples=minimum_samples,
        insufficient_evidence=deadline_samples < minimum_samples,
    )


def _labels(raw: object, *, field: str) -> dict[str, bool]:
    data = _object(raw, field=field)
    _keys(data, field=field, required={"deadline_success", "contract_floor_pass"})
    return {
        "deadline_success": _boolean(
            data["deadline_success"], field=f"{field}.deadline_success"
        ),
        "contract_floor_pass": _boolean(
            data["contract_floor_pass"], field=f"{field}.contract_floor_pass"
        ),
    }


def _scope_changes_only(
    prior: EvidenceScope,
    current: EvidenceScope,
    *,
    dimension: str,
    field: str,
) -> None:
    descriptor_changed = prior.descriptor_hash != current.descriptor_hash
    model_changed = (
        prior.model_provider,
        prior.model_name,
        prior.model_digest,
        prior.model_variant,
    ) != (
        current.model_provider,
        current.model_name,
        current.model_digest,
        current.model_variant,
    )
    task_class_changed = prior.task_class != current.task_class
    observed = {
        "descriptor_hash": descriptor_changed,
        "model": model_changed,
        "task_class": task_class_changed,
    }
    if dimension not in observed:
        raise FixtureError(f"{field}.changed_dimension is invalid")
    if not observed[dimension] or any(
        changed for name, changed in observed.items() if name != dimension
    ):
        raise FixtureError(f"{field} must change only {dimension}")
    if prior.enrollment_id != current.enrollment_id:
        raise FixtureError(f"{field} cannot change enrollment_id")
    if prior.scope_key == current.scope_key:
        raise FixtureError(f"{field} did not create a new evidence scope")


def _prior_scope_samples(
    raw: object,
    *,
    current_scope: EvidenceScope,
    minimum_samples: int,
    field: str,
) -> int:
    data = _object(raw, field=field)
    _keys(data, field=field, required={"changed_dimension", "scope", "evidence"})
    dimension = _text(data["changed_dimension"], field=f"{field}.changed_dimension")
    prior_scope = _scope(data["scope"], field=f"{field}.scope")
    _scope_changes_only(prior_scope, current_scope, dimension=dimension, field=field)
    prior = _aggregate(
        data["evidence"],
        scope=prior_scope,
        minimum_samples=minimum_samples,
        field=f"{field}.evidence",
    )
    return (
        prior.deadline_completion.sample_count
        + prior.contract_floor.sample_count
    )


def _candidate(
    raw: object,
    *,
    minimum_samples: int,
    field: str,
) -> dict[str, Any]:
    data = _object(raw, field=field)
    _keys(
        data,
        field=field,
        required={"candidate_id", "hard_eligible", "scope", "evidence", "labels"},
        optional={"excluded_causes", "prior_scope_evidence"},
    )
    candidate_id = _text(data["candidate_id"], field=f"{field}.candidate_id")
    hard_eligible = _boolean(data["hard_eligible"], field=f"{field}.hard_eligible")
    scope = _scope(data["scope"], field=f"{field}.scope")
    aggregate = _aggregate(
        data["evidence"],
        scope=scope,
        minimum_samples=minimum_samples,
        field=f"{field}.evidence",
    )
    excluded_causes_raw = data.get("excluded_causes", [])
    if not isinstance(excluded_causes_raw, list):
        raise FixtureError(f"{field}.excluded_causes must be an array")
    excluded_causes: list[str] = []
    for index, raw_cause in enumerate(excluded_causes_raw):
        cause = _text(raw_cause, field=f"{field}.excluded_causes[{index}]")
        if cause not in ALLOWED_EXCLUDED_CAUSES:
            raise FixtureError(f"{field}.excluded_causes[{index}] is not an excluded cause")
        excluded_causes.append(cause)
    if len(set(excluded_causes)) != len(excluded_causes):
        raise FixtureError(f"{field}.excluded_causes must be unique")
    ignored_prior_scope_samples = 0
    if "prior_scope_evidence" in data:
        ignored_prior_scope_samples = _prior_scope_samples(
            data["prior_scope_evidence"],
            current_scope=scope,
            minimum_samples=minimum_samples,
            field=f"{field}.prior_scope_evidence",
        )
    return {
        "candidate_id": candidate_id,
        "hard_eligible": hard_eligible,
        "aggregate": aggregate,
        "future_active_experiment_eligibility": (
            future_active_experiment_eligibility(scope).as_dict()
        ),
        "labels": _labels(data["labels"], field=f"{field}.labels"),
        "excluded_causes": tuple(excluded_causes),
        "ignored_prior_scope_samples": ignored_prior_scope_samples,
    }


def _expected(raw: object, *, field: str) -> dict[str, Any]:
    data = _object(raw, field=field)
    _keys(
        data,
        field=field,
        required={
            "eligible_candidate_ids",
            "outcome",
            "preferred_candidate_id",
            "rationale_code",
        },
    )
    raw_ids = data["eligible_candidate_ids"]
    if not isinstance(raw_ids, list):
        raise FixtureError(f"{field}.eligible_candidate_ids must be an array")
    eligible_ids = [
        _text(value, field=f"{field}.eligible_candidate_ids[{index}]")
        for index, value in enumerate(raw_ids)
    ]
    preferred = data["preferred_candidate_id"]
    if preferred is not None:
        preferred = _text(preferred, field=f"{field}.preferred_candidate_id")
    outcome = _text(data["outcome"], field=f"{field}.outcome")
    if outcome not in {"same", "different", "no_preference"}:
        raise FixtureError(f"{field}.outcome is invalid")
    return {
        "eligible_candidate_ids": sorted(eligible_ids),
        "outcome": outcome,
        "preferred_candidate_id": preferred,
        "rationale_code": _text(
            data["rationale_code"], field=f"{field}.rationale_code"
        ),
    }


def _case_report(
    raw: object,
    *,
    index: int,
    minimum_samples: int,
    decision_at: float,
) -> dict[str, Any]:
    field = f"cases[{index}]"
    data = _object(raw, field=field)
    _keys(
        data,
        field=field,
        required={"case_id", "scenario", "actual_candidate_id", "candidates", "expected"},
    )
    case_id = _text(data["case_id"], field=f"{field}.case_id")
    scenario = _text(data["scenario"], field=f"{field}.scenario")
    actual_candidate_id = _text(
        data["actual_candidate_id"], field=f"{field}.actual_candidate_id"
    )
    raw_candidates = data["candidates"]
    if not isinstance(raw_candidates, list) or not 1 <= len(raw_candidates) <= MAX_CANDIDATES:
        raise FixtureError(f"{field}.candidates must contain 1-{MAX_CANDIDATES} items")
    candidates = [
        _candidate(item, minimum_samples=minimum_samples, field=f"{field}.candidates[{item_index}]")
        for item_index, item in enumerate(raw_candidates)
    ]
    by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise FixtureError(f"{field}.candidate_id values must be unique")
    actual = by_id.get(actual_candidate_id)
    if actual is None or actual["hard_eligible"] is not True:
        raise FixtureError(f"{field}.actual_candidate_id must be hard eligible")
    expected = _expected(data["expected"], field=f"{field}.expected")
    eligible_records = sorted(
        (candidate for candidate in candidates if candidate["hard_eligible"] is True),
        key=lambda candidate: candidate["candidate_id"],
    )
    eligible_ids = [candidate["candidate_id"] for candidate in eligible_records]
    if eligible_ids != expected["eligible_candidate_ids"]:
        raise FixtureError(f"{field}.expected eligible candidates do not match hard eligibility")
    production_candidates = tuple(
        EligibleShadowCandidate(candidate["candidate_id"], candidate["aggregate"])
        for candidate in eligible_records
    )
    evaluate_kwargs = {
        "actual_attempt_id": f"synthetic-attempt-{case_id}",
        "actual_candidate_id": actual_candidate_id,
        "minimum_samples": minimum_samples,
        "decision_at": decision_at + index,
    }
    evaluation = evaluate_shadow_preference(
        candidates=production_candidates,
        **evaluate_kwargs,
    )
    reordered = evaluate_shadow_preference(
        candidates=tuple(reversed(production_candidates)),
        **evaluate_kwargs,
    )
    if evaluation != reordered:
        raise FixtureError(f"{field} is not invariant to candidate order")
    actual_result = {
        "outcome": evaluation.outcome,
        "preferred_candidate_id": evaluation.preferred_candidate_id,
        "rationale_code": evaluation.rationale_code,
    }
    expected_result = {
        "outcome": expected["outcome"],
        "preferred_candidate_id": expected["preferred_candidate_id"],
        "rationale_code": expected["rationale_code"],
    }
    if actual_result != expected_result:
        raise FixtureError(
            f"{field} shadow invariant mismatch: expected {expected_result}, got {actual_result}"
        )
    shadow_fallback_id = evaluation.preferred_candidate_id or actual_candidate_id
    shadow_fallback = by_id[shadow_fallback_id]
    hard_ineligible_ids = sorted(
        candidate["candidate_id"]
        for candidate in candidates
        if candidate["hard_eligible"] is False
    )
    excluded_count = sum(len(candidate["excluded_causes"]) for candidate in candidates)
    ignored_prior_scope_samples = sum(
        candidate["ignored_prior_scope_samples"] for candidate in candidates
    )
    future_active_diagnostics = {
        candidate["candidate_id"]: candidate[
            "future_active_experiment_eligibility"
        ]
        for candidate in eligible_records
    }
    return {
        "case_id": case_id,
        "scenario": scenario,
        "claim_only_candidate_id": actual_candidate_id,
        "eligible_candidate_ids": eligible_ids,
        "hard_ineligible_candidate_ids": hard_ineligible_ids,
        "excluded_non_node_fault_count": excluded_count,
        "ignored_prior_scope_samples": ignored_prior_scope_samples,
        "future_active_experiment_eligibility": {
            "by_candidate": future_active_diagnostics,
            "meaning": "identity_prerequisite_only_not_reputation_or_trust",
        },
        "shadow": {
            "candidate_set_digest": evaluation.candidate_set_digest,
            "decision_id": evaluation.decision_id,
            "outcome": evaluation.outcome,
            "preferred_candidate_id": evaluation.preferred_candidate_id,
            "rationale_code": evaluation.rationale_code,
            "with_claim_fallback_candidate_id": shadow_fallback_id,
        },
        "labeled_outcomes": {
            "claim_only": actual["labels"],
            "shadow_preference": (
                by_id[evaluation.preferred_candidate_id]["labels"]
                if evaluation.preferred_candidate_id is not None
                else None
            ),
            "shadow_with_claim_fallback": shadow_fallback["labels"],
        },
    }


def _policy_metrics(cases: list[dict[str, Any]], label: str) -> dict[str, Any]:
    deadline_successes = sum(
        int(case["labeled_outcomes"][label]["deadline_success"]) for case in cases
    )
    contract_passes = sum(
        int(case["labeled_outcomes"][label]["contract_floor_pass"]) for case in cases
    )
    count = len(cases)
    return {
        "case_count": count,
        "contract_floor_pass_count": contract_passes,
        "contract_floor_pass_rate": (
            round(contract_passes / count, 6) if count else None
        ),
        "deadline_success_count": deadline_successes,
        "deadline_success_rate": (
            round(deadline_successes / count, 6) if count else None
        ),
    }


def evaluate_fixture(raw: object) -> dict[str, Any]:
    fixture = _object(raw, field="fixture")
    _keys(
        fixture,
        field="fixture",
        required={"fixture_version", "minimum_samples", "decision_at", "cases"},
    )
    if fixture["fixture_version"] != FIXTURE_VERSION:
        raise FixtureError(f"fixture_version must be {FIXTURE_VERSION}")
    minimum_samples = _integer(
        fixture["minimum_samples"], field="minimum_samples", minimum=1
    )
    decision_at = _number(fixture["decision_at"], field="decision_at")
    raw_cases = fixture["cases"]
    if not isinstance(raw_cases, list) or not 1 <= len(raw_cases) <= MAX_CASES:
        raise FixtureError(f"cases must contain 1-{MAX_CASES} items")
    cases = [
        _case_report(
            item,
            index=index,
            minimum_samples=minimum_samples,
            decision_at=decision_at,
        )
        for index, item in enumerate(raw_cases)
    ]
    case_ids = [case["case_id"] for case in cases]
    if len(set(case_ids)) != len(case_ids):
        raise FixtureError("case_id values must be unique")
    scenarios = {case["scenario"] for case in cases}
    missing_scenarios = REQUIRED_SCENARIOS - scenarios
    if missing_scenarios:
        raise FixtureError(
            "fixture is missing scenarios: " + ", ".join(sorted(missing_scenarios))
        )
    baseline = _policy_metrics(cases, "claim_only")
    shadow = _policy_metrics(cases, "shadow_with_claim_fallback")
    preference_cases = [
        case
        for case in cases
        if case["labeled_outcomes"]["shadow_preference"] is not None
    ]
    comparable_baseline = _policy_metrics(preference_cases, "claim_only")
    shadow_preference = _policy_metrics(preference_cases, "shadow_preference")
    outcome_counts = {name: 0 for name in ("same", "different", "no_preference")}
    for case in cases:
        outcome_counts[case["shadow"]["outcome"]] += 1
    future_active_diagnostics = [
        diagnostic
        for case in cases
        for diagnostic in case["future_active_experiment_eligibility"][
            "by_candidate"
        ].values()
    ]
    future_active_reason_counts: dict[str, int] = {}
    for diagnostic in future_active_diagnostics:
        for reason in diagnostic["blocking_reasons"]:
            future_active_reason_counts[reason] = (
                future_active_reason_counts.get(reason, 0) + 1
            )
    return {
        "report_version": REPORT_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "minimum_samples": minimum_samples,
        "summary": {
            "case_count": len(cases),
            "claim_only": baseline,
            "shadow_with_claim_fallback": shadow,
            "delta": {
                "contract_floor_pass_count": (
                    shadow["contract_floor_pass_count"]
                    - baseline["contract_floor_pass_count"]
                ),
                "deadline_success_count": (
                    shadow["deadline_success_count"]
                    - baseline["deadline_success_count"]
                ),
            },
            "preference_evaluable_comparison": {
                "claim_only": comparable_baseline,
                "delta": {
                    "contract_floor_pass_count": (
                        shadow_preference["contract_floor_pass_count"]
                        - comparable_baseline["contract_floor_pass_count"]
                    ),
                    "deadline_success_count": (
                        shadow_preference["deadline_success_count"]
                        - comparable_baseline["deadline_success_count"]
                    ),
                },
                "shadow_preference": shadow_preference,
            },
            "shadow_outcomes": outcome_counts,
            "future_active_experiment_identity": {
                "eligible_scope_count": sum(
                    int(item["eligible_for_future_active_experiment"])
                    for item in future_active_diagnostics
                ),
                "blocked_scope_count": sum(
                    int(not item["eligible_for_future_active_experiment"])
                    for item in future_active_diagnostics
                ),
                "blocking_reason_counts": dict(
                    sorted(future_active_reason_counts.items())
                ),
                "meaning": "necessary_identity_prerequisite_not_promotion",
            },
            "hard_ineligible_candidates_excluded": sum(
                len(case["hard_ineligible_candidate_ids"]) for case in cases
            ),
            "excluded_non_node_fault_count": sum(
                case["excluded_non_node_fault_count"] for case in cases
            ),
            "ignored_prior_scope_samples": sum(
                case["ignored_prior_scope_samples"] for case in cases
            ),
            "invariant_failures": 0,
        },
        "cases": cases,
    }


def load_fixture(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FixtureError(f"cannot read fixture: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FixtureError(
            f"fixture JSON is invalid at line {exc.lineno}, column {exc.colno}"
        ) from exc
    return _object(raw, field="fixture")


def render_report(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args(argv)
    try:
        fixture = load_fixture(args.fixture)
        first = evaluate_fixture(fixture)
        second = evaluate_fixture(fixture)
        rendered = render_report(first)
        if rendered != render_report(second):
            raise FixtureError("evaluation output is not deterministic")
    except (AssertionError, FixtureError, TypeError, ValueError) as exc:
        print(f"capability evidence evaluation failed: {exc}", file=sys.stderr)
        return 2
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
