"""Summarise a completed eval study from its recorded runs.

    python scripts/eval_study_summary.py evals/studies/<study_id>
    python scripts/eval_study_summary.py <dir> --arms decomposition ensemble_5

It computes the pre-registered test and prints the counts and the test
statistic together. **It will not print a p-value on its own**, and it refuses
outright to compute anything when the study is incomplete:

  * an item that no arm ran,
  * an item one arm ran and another did not,
  * or any record whose grading did not finish.

Those are the three shapes of "the instrument reported a result it did not
measure", which is the failure this whole directory exists to prevent. A
partial study is not a smaller study; it is a study whose missing cells are
correlated with something, and you do not know what.

It also reports what each arm actually cost. The primary endpoint of the
pre-registered decomposition study is success **at equal compute**, so a
comparison between arms whose measured cost turned out not to be equal is
reported as exactly that rather than quietly presented as the headline.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "evals"))

import runrecord  # noqa: E402
import stats  # noqa: E402

# How far apart two arms' measured cost may be before "equal compute" stops
# being an honest description of the comparison.
COST_TOLERANCE = 0.25


class IncompleteStudy(RuntimeError):
    pass


def check_complete(records: list[dict], arms: list[str]) -> None:
    """Refuse to summarise a study with a missing or ungraded cell."""
    if not records:
        raise IncompleteStudy("the run log holds no records")

    items = sorted({r["item_id"] for r in records})
    if not items:
        raise IncompleteStudy("the run log names no items")

    latest = runrecord.latest_per_key(records)
    problems: list[str] = []
    for item in items:
        for arm in arms:
            matching = [key for key in latest if key[0] == item and key[1] == arm]
            if not matching:
                problems.append(f"{item}: arm {arm!r} never ran")
                continue
            for key in matching:
                record = latest[key]
                if not record.get("graded"):
                    problems.append(
                        f"{item}: arm {arm!r} replicate {key[2]} was not graded "
                        f"({', '.join(record.get('grading', {}).get('ungraded_checks', [])) or 'reason not recorded'})"
                    )
    if problems:
        raise IncompleteStudy(
            "this study is incomplete, so no statistic will be computed:\n  - "
            + "\n  - ".join(problems)
        )


def outcomes_for(records: list[dict], arm: str) -> dict[str, bool]:
    latest = runrecord.latest_per_key(records)
    return {
        key[0]: bool(record["passed"])
        for key, record in latest.items()
        if key[1] == arm
    }


def cost_for(records: list[dict], arm: str) -> dict:
    latest = runrecord.latest_per_key(records)
    rows = [r for key, r in latest.items() if key[1] == arm]
    seconds = [r["wall_clock_seconds"] for r in rows if r.get("wall_clock_seconds") is not None]
    tokens = [
        sum(r["tokens"].values()) for r in rows if isinstance(r.get("tokens"), dict) and r["tokens"]
    ]
    return {
        "runs": len(rows),
        "seconds_total": sum(seconds) if seconds else None,
        "seconds_median": statistics.median(seconds) if seconds else None,
        "seconds_missing": len(rows) - len(seconds),
        "tokens_total": sum(tokens) if tokens else None,
        "tokens_missing": len(rows) - len(tokens),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Summarise a completed eval study.")
    ap.add_argument("study_dir", help="directory holding runs.jsonl")
    ap.add_argument("--arms", nargs="*", default=None,
                    help="arms to include; defaults to every arm in the log")
    ap.add_argument("--paired", nargs=2, metavar=("BASELINE", "CANDIDATE"),
                    help="run the paired McNemar test between these two arms")
    ap.add_argument("--alpha", type=float, default=0.05)
    args = ap.parse_args()

    try:
        records = runrecord.load_runs(Path(args.study_dir))
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    arms = args.arms or sorted({r["arm"] for r in records})
    if not arms:
        print("ERROR: the run log names no arms")
        return 1

    try:
        check_complete(records, arms)
    except IncompleteStudy as exc:
        print(f"REFUSING TO SUMMARISE: {exc}")
        return 1

    corpus_digests = {r.get("corpus_digest") for r in records}
    model_digests = {r["model"].get("digest") for r in records}
    grader_versions = {r.get("grading", {}).get("grader_version") for r in records}
    superseded = runrecord.superseded_count(records)

    print(f"Study: {args.study_dir}")
    print(f"  records: {len(records)}  ({superseded} superseded by a later run of the same cell)")
    print(f"  arms: {', '.join(arms)}")
    print(f"  corpus digest(s): {', '.join(sorted(d or 'unknown' for d in corpus_digests))}")
    print(f"  grader version(s): {', '.join(sorted(v or 'unknown' for v in grader_versions))}")
    print(f"  model digest(s): {', '.join(sorted(d or 'unknown' for d in model_digests))}")
    if len(corpus_digests) > 1:
        print("  WARNING: arms were run against different corpus versions — not comparable")
    if len(grader_versions) > 1:
        print("  WARNING: arms were graded by different grader versions — not comparable")
    if len(model_digests) > 1:
        print("  WARNING: the model changed during this study, which invalidates it")
    if None in model_digests:
        print("  NOTE: at least one run could not record a model digest, so a mid-study")
        print("        model update cannot be ruled out from these records alone")
    print()

    print("Per arm:")
    costs = {}
    for arm in arms:
        outcomes = outcomes_for(records, arm)
        passes = sum(1 for v in outcomes.values() if v)
        lo, hi = stats.wilson(passes, len(outcomes))
        cost = cost_for(records, arm)
        costs[arm] = cost
        hours = "" if cost["seconds_total"] is None else f"{cost['seconds_total'] / 3600:.1f} h"
        median = "" if cost["seconds_median"] is None else f"{cost['seconds_median'] / 60:.1f} min"
        print(f"  {arm:<20} {passes}/{len(outcomes)} "
              f"({passes / len(outcomes):.0%}, 95% CI {lo:.0%}-{hi:.0%})"
              f"   cost {hours or 'unknown'} total, {median or 'unknown'} median/item")
        if cost["seconds_missing"]:
            print(f"    {cost['seconds_missing']} run(s) recorded no wall clock")
        if cost["tokens_missing"]:
            print(f"    {cost['tokens_missing']} run(s) recorded no token count")
    print()

    if args.paired:
        baseline, candidate = args.paired
        for arm in (baseline, candidate):
            if arm not in arms:
                print(f"ERROR: arm {arm!r} is not in this study")
                return 1
        result = stats.paired_test(
            outcomes_for(records, baseline), outcomes_for(records, candidate), alpha=args.alpha
        )
        print(f"Paired comparison: {baseline} vs {candidate}")
        print()
        print(stats.render_paired(result, label_a=baseline, label_b=candidate))
        print()

        a_cost, b_cost = costs[baseline]["seconds_total"], costs[candidate]["seconds_total"]
        if a_cost and b_cost:
            ratio = b_cost / a_cost
            equal = abs(ratio - 1.0) <= COST_TOLERANCE
            print(f"  measured compute ratio ({candidate} / {baseline}): {ratio:.2f}x")
            if equal:
                print(f"  the arms are within +/-{COST_TOLERANCE:.0%}, so this IS the "
                      "equal-compute comparison")
            else:
                print(f"  the arms are NOT within +/-{COST_TOLERANCE:.0%} of each other, so this "
                      "is an equal-ATTEMPT")
                print("  comparison. The equal-compute endpoint is not established by these runs.")
        else:
            print("  compute cost was not recorded for both arms, so the equal-compute")
            print("  endpoint cannot be evaluated from this study.")
        print()

    print("Every figure above is computed from the records in runs.jsonl. Nothing here")
    print("is an estimate of what the arms would have done on a corpus they did not run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
