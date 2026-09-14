"""Summarise only the complete cells of an explicit, frozen study manifest.

Usage: python scripts/eval_study_summary.py STUDY_DIR --paired baseline candidate
STUDY_DIR must contain manifest.json and runs.jsonl. Historical logs without a
predeclared measurement identity cannot establish a confirmatory comparison.
One replicate per item is supported; repeated-item aggregation is not inferred.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "evals"))

import runrecord  # noqa: E402
import stats  # noqa: E402


class IncompleteStudy(RuntimeError):
    pass


def check_complete(records: list[dict], arms: list[str], manifest: dict | None = None) -> None:
    """Require every planned cell, no extra cells, and matching frozen identity."""
    if manifest is None:
        raise IncompleteStudy("an explicit planned study manifest is required")
    try:
        runrecord.validate_manifest(manifest)
    except ValueError as exc:
        raise IncompleteStudy(str(exc)) from exc
    if set(arms) != set(manifest["arms"]):
        raise IncompleteStudy("selected arms must equal the planned arms")
    planned = {(item, arm, rep) for item in manifest["item_ids"]
               for arm in manifest["arms"] for rep in manifest["replicates"]}
    latest = runrecord.latest_per_key(records)
    problems = [f"{key}: never ran" for key in sorted(planned - latest.keys())]
    problems.extend(f"{key}: unplanned cell" for key in sorted(latest.keys() - planned))
    identity = manifest["identity"]
    for key, record in sorted(latest.items()):
        if record.get("graded") is not True or type(record.get("passed")) is not bool:
            problems.append(f"{key}: not graded ({record.get('grading', {}).get('ungraded_checks', [])})")
        observed = {
            "measurement_identity_version": record.get("measurement_identity_version", "1"),
            "measurement_digest": record.get("measurement_digest"),
            "grader_version": record.get("grading", {}).get("grader_version"),
            "model_digest": record.get("model", {}).get("digest"),
        }
        if record.get("study_id") != manifest["study_id"]:
            problems.append(f"{key}: study identity differs from manifest")
        for name, value in observed.items():
            if value != identity[name]:
                problems.append(f"{key}: {name} differs from frozen manifest")
    if problems:
        raise IncompleteStudy("no statistic will be computed:\n  - " + "\n  - ".join(problems))


def outcomes_for(records: list[dict], arm: str) -> dict[str, bool]:
    latest = runrecord.latest_per_key(records)
    outcomes = {}
    replicates = set()
    for key, record in sorted(latest.items()):
        if key[1] != arm:
            continue
        replicates.add(key[2])
        if key[0] in outcomes or len(replicates) > 1:
            raise IncompleteStudy("multi-replicate summaries are unsupported")
        outcomes[key[0]] = bool(record["passed"])
    return outcomes


def _number(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


def _tokens(value) -> bool:
    return (isinstance(value, dict) and set(value) == {"prompt", "completion"}
            and all(type(v) is int and v >= 0 for v in value.values()))


def cost_for(records: list[dict], arm: str) -> dict:
    """Latency and aggregate per-call hardware work have separate denominators.

    Unit rows include every planner, candidate, reviewer, reviser, failed call,
    retry, and cancelled call. Completion is an explicit collector assertion;
    positive comparison claims additionally require each row's timing/tokens.
    Missing measurements suppress totals rather than passing partial sums off
    as complete. Inference seconds are runtime duration, not CPU utilization.
    """
    latest = runrecord.latest_per_key(records)
    rows = [row for key, row in sorted(latest.items()) if key[1] == arm]
    latency = [row["wall_clock_seconds"] for row in rows if _number(row.get("wall_clock_seconds"))]
    units = []
    missing_units = 0
    for row in rows:
        values = row.get("unit_costs")
        valid = (row.get("cost_capture_complete") is True and isinstance(values, list)
                 and bool(values) and all(isinstance(unit, dict) for unit in values))
        if valid:
            ids = [unit.get("unit_id") for unit in values]
            valid = all(isinstance(i, str) and i for i in ids) and len(ids) == len(set(ids))
        if not valid:
            missing_units += 1
            continue
        units.extend(values)
    complete = bool(rows) and not missing_units
    timing_complete = complete and all(_number(u.get("inference_seconds")) for u in units)
    tokens_complete = complete and all(_tokens(u.get("tokens")) for u in units)
    hardware_complete = complete and all(isinstance(u.get("hardware_id"), str) and u["hardware_id"] for u in units)
    return {
        "runs": len(rows),
        "latency_seconds_total": math.fsum(latency) if latency and len(latency) == len(rows) else None,
        "latency_seconds_median": statistics.median(latency) if latency and len(latency) == len(rows) else None,
        "latency_missing": len(rows) - len(latency),
        "hardware_seconds_total": math.fsum(u["inference_seconds"] for u in units) if timing_complete else None,
        "tokens_total": sum(sum(u["tokens"].values()) for u in units) if tokens_complete else None,
        "hardware_ids": sorted({u["hardware_id"] for u in units}) if hardware_complete else [],
        "unit_records": len(units),
        "unit_capture_missing_runs": missing_units,
        "cost_complete": timing_complete and tokens_complete and hardware_complete,
    }


def compare_costs(a: dict, b: dict, policy: dict) -> dict:
    """Apply only a predeclared same-hardware inference-duration policy."""
    result = {"comparable": False, "ratio": None, "within_tolerance": None}
    if policy.get("metric") != "homogeneous_hardware_seconds_v1":
        return {**result, "reason": "budget policy is descriptive only"}
    if not all(c["cost_complete"] and not c["latency_missing"] for c in (a, b)):
        return {**result, "reason": "complete per-unit timing, tokens, hardware and latency were not recorded"}
    if any(c["hardware_ids"] != [policy.get("hardware_id")] for c in (a, b)):
        return {**result, "reason": "unit hardware differs from the declared homogeneous hardware"}
    if not a["hardware_seconds_total"] or not b["hardware_seconds_total"]:
        return {**result, "reason": "positive aggregate hardware time is required"}
    ratio = b["hardware_seconds_total"] / a["hardware_seconds_total"]
    return {"comparable": True, "ratio": ratio,
            "within_tolerance": abs(ratio - 1) <= policy["relative_tolerance"],
            "reason": "same-hardware aggregate inference duration; not hardware-independent compute"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("study_dir")
    ap.add_argument("--arms", nargs="*", default=None)
    ap.add_argument("--paired", nargs=2, metavar=("BASELINE", "CANDIDATE"))
    ap.add_argument("--alpha", type=float, default=0.05)
    args = ap.parse_args()
    try:
        records = runrecord.load_runs(Path(args.study_dir))
        manifest = runrecord.load_manifest(Path(args.study_dir))
        arms = args.arms or manifest["arms"]
        check_complete(records, arms, manifest)
    except (FileNotFoundError, ValueError, IncompleteStudy) as exc:
        print(f"ERROR: REFUSING TO SUMMARISE: {exc}")
        return 1
    print(f"Study: {manifest['study_id']}")
    print(f"  records: {len(records)} ({runrecord.superseded_count(records)} superseded)")
    print("  Same-key supersession uses the final appended record; other record order is immaterial.")
    print(f"  measurement identity: {manifest['identity']}")
    costs = {}
    for arm in sorted(arms):
        outcomes = outcomes_for(records, arm)
        passes = sum(outcomes.values())
        lo, hi = stats.wilson(passes, len(outcomes))
        costs[arm] = cost = cost_for(records, arm)
        print(f"  {arm}: {passes}/{len(outcomes)} (95% CI {lo:.0%}-{hi:.0%})")
        print(f"    elapsed latency seconds: total={cost['latency_seconds_total']}, "
              f"median={cost['latency_seconds_median']}, missing={cost['latency_missing']}")
        print(f"    aggregate inference hardware-seconds={cost['hardware_seconds_total']}, "
              f"tokens={cost['tokens_total']}, unit records={cost['unit_records']}, "
              f"complete={cost['cost_complete']}")
    if args.paired:
        baseline, candidate = args.paired
        if baseline == candidate or any(a not in arms for a in args.paired):
            print("ERROR: paired comparison requires two distinct planned arms")
            return 1
        result = stats.paired_test(outcomes_for(records, baseline),
                                   outcomes_for(records, candidate), alpha=args.alpha)
        print(stats.render_paired(result, label_a=baseline, label_b=candidate))
        cost_result = compare_costs(costs[baseline], costs[candidate], manifest["budget_policy"])
        print(f"  Budget comparison: {cost_result['reason']}")
        if cost_result["comparable"]:
            print(f"  aggregate hardware-time ratio: {cost_result['ratio']:.2f}x; "
                  f"within declared tolerance: {cost_result['within_tolerance']}")
        else:
            print("  The comparable-compute endpoint is not established.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
