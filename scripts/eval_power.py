"""What can this eval set actually see, and what would it cost to see less?

    python scripts/eval_power.py
    python scripts/eval_power.py --delta 0.15 --power 0.8
    python scripts/eval_power.py --no-grid          # just the measured curve

Estimates the discordant pair rate from the committed eval runs, then prints
the power curve: the smallest true difference detectable at each corpus size,
and what a run of that size costs in wall-clock hours at the measured per-item
rate. Every number in `docs/eval-methodology.md`'s power section comes from
this script, so the document can be checked rather than believed.

The discordant rate is the quantity that decides everything here. For a paired
binary design, McNemar's power depends on the fraction of items that *change
outcome* between two runs, not on the total item count directly — an item both
arms pass and an item both arms fail contribute nothing. The best estimate this
project has is the one pair of runs made with an identical configuration
(prompt set v3, Aug 8 against Aug 11), which is also the noise floor already
published in `evals/README.md`.

**The curve is a function of that rate, so the script prints it as one.** After
the measured curve comes a second table: detectable effect across the same
corpus sizes crossed with a range of hypothetical noise floors, because
delta is proportional to sqrt(psi / n) and halving psi is therefore worth
exactly what doubling n is worth. Unpinned sampling is a plausible and
removable contributor to psi that nobody has measured;
`docs/experiments/noise-floor-under-pinned-sampling.md` is the design that
would measure it, and this table is what its answer turns into.

**Every cell in that second table computed at a rate other than the measured
one is marked with a `*` and called a projection.** The default output stays
pinned to psi = 0.643 so nothing here reads as though a better floor had been
observed; `evals/stats.py::format_psi_cell` refuses to render a projected cell
without its marker rather than trusting whoever writes the next renderer.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from itertools import combinations
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "evals"))

import stats  # noqa: E402

RESULTS = REPO_ROOT / "evals" / "results"
DEFAULT_SIZES = [28, 40, 50, 60, 80, 100, 120, 160, 200, 300]


def corpus_size() -> int | None:
    """How many items the corpus actually holds, so the grid can mark that row.

    None when the corpus cannot be read. The grid then marks no row rather than
    guessing at 100, which would be a number nobody checked.
    """
    try:
        import corpus as corpus_mod

        return len(corpus_mod.load_corpus())
    except Exception:
        return None


def load_runs() -> dict[str, dict]:
    """Every committed run, as {run_id: {meta, outcomes, seconds}}.

    Outcomes are recomputed mechanically from the stored per-item fields rather
    than read from `success`, so the judge gate can be included or excluded and
    the difference reported. It turns out not to matter, which is itself worth
    knowing.
    """
    runs: dict[str, dict] = {}
    if not RESULTS.is_dir():
        return runs
    for directory in sorted(RESULTS.iterdir()):
        log = directory / "results.jsonl"
        if not directory.is_dir() or not log.exists():
            continue
        outcomes: dict[str, bool] = {}
        judged: dict[str, bool] = {}
        seconds: list[float] = []
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            mechanical = bool(
                record.get("extracted")
                and record.get("parses")
                and record.get("executes")
                and record.get("artifact_match")
                and record.get("keywords_ok")
            )
            score = record.get("judge_score")
            outcomes[record["id"]] = mechanical
            judged[record["id"]] = mechanical and score is not None and score >= 4
            if record.get("seconds") is not None:
                seconds.append(float(record["seconds"]))
        if not outcomes:
            continue
        meta = {}
        summary = directory / "summary.json"
        if summary.exists():
            blob = json.loads(summary.read_text(encoding="utf-8"))
            meta = blob.get("meta", blob)
        runs[directory.name] = {
            "meta": meta,
            "outcomes": outcomes,
            "judged": judged,
            "seconds": seconds,
        }
    return runs


def main() -> int:
    ap = argparse.ArgumentParser(description="Power analysis for the eval corpus.")
    ap.add_argument("--delta", type=float, default=0.15,
                    help="target effect, as a proportion of the corpus (default 0.15)")
    ap.add_argument("--power", type=float, default=0.80)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--sizes", type=int, nargs="*", default=DEFAULT_SIZES)
    ap.add_argument("--rate", type=float, default=None,
                    help="override the discordant rate instead of estimating it")
    ap.add_argument("--psi", type=float, nargs="*", default=None,
                    help="hypothetical noise floors for the projection grid "
                         "(default: 0.5 0.4 0.32 0.25, alongside the measured rate)")
    ap.add_argument("--no-grid", action="store_true",
                    help="print only the measured curve, without the projection grid")
    args = ap.parse_args()

    runs = load_runs()
    if not runs and args.rate is None:
        print("ERROR: no committed eval runs to estimate the discordant rate from, and")
        print("       no --rate given. Refusing to invent one.")
        return 1

    per_item_minutes = None
    if runs:
        print(f"Committed runs: {len(runs)}")
        for run_id, run in sorted(runs.items()):
            passes = sum(1 for v in run["outcomes"].values() if v)
            print(f"  {run_id}  prompt set {run['meta'].get('prompt_set', '?'):<3} "
                  f"{passes}/{len(run['outcomes'])} mechanical")
        print()

        all_seconds = [s for run in runs.values() for s in run["seconds"]]
        if all_seconds:
            per_item_minutes = statistics.mean(all_seconds) / 60
            print(f"Measured cost: mean {per_item_minutes:.1f} min per item, "
                  f"median {statistics.median(all_seconds) / 60:.1f} min "
                  f"(n = {len(all_seconds)} item runs)")
            print()

        print("Discordance between every pair of committed runs:")
        same_config: list[tuple[str, str, dict]] = []
        for a, b in combinations(sorted(runs), 2):
            result = stats.discordance(runs[a]["outcomes"], runs[b]["outcomes"])
            set_a = runs[a]["meta"].get("prompt_set")
            set_b = runs[b]["meta"].get("prompt_set")
            marker = ""
            if set_a and set_a == set_b:
                marker = "   <- SAME CONFIGURATION: this is the noise floor"
                same_config.append((a, b, result))
            print(f"  {a} vs {b}: {result['discordant']}/{result['n']} "
                  f"= {result['rate']:.3f}{marker}")
        print()

        if args.rate is not None:
            rate = args.rate
            source = "given on the command line"
        elif same_config:
            rate = statistics.mean(r["rate"] for _, _, r in same_config)
            example = same_config[0][2]
            source = (
                f"the {len(same_config)} identical-configuration pair(s): "
                f"{example['discordant']}/{example['n']}, "
                f"95% CI {example['ci95'][0]:.2f}-{example['ci95'][1]:.2f}"
            )
        else:
            print("ERROR: no two committed runs share a configuration, so the discordant")
            print("       rate cannot be estimated without confounding it with the change")
            print("       between prompt sets. Run one prompt set twice, or pass --rate.")
            return 1
    else:
        rate = args.rate
        source = "given on the command line"

    print(f"Discordant pair rate used: {rate:.3f}  ({source})")
    print()

    print(f"Power curve  (alpha = {args.alpha}, power = {args.power:.0%}, "
          f"one-sided McNemar exact)")
    print()
    header = f"{'n':>5}  {'detectable effect':>18}  {'= items':>8}  {'power at ' + f'{args.delta:.0%}':>14}"
    if per_item_minutes:
        header += f"  {'one run':>9}  {'a comparison':>13}"
    print(header)
    print("-" * len(header))
    for row in stats.power_curve(args.sizes, rate, args.delta, args.alpha, args.power):
        mde = "not reachable" if row["mde"] is None else f"{row['mde']:.1%}"
        items = "-" if row["mde_items"] is None else f"{row['mde_items']:.1f}"
        line = f"{row['n']:>5}  {mde:>18}  {items:>8}  {row['power_at_delta']:>14.2f}"
        if per_item_minutes:
            hours = row["n"] * per_item_minutes / 60
            line += f"  {hours:>7.0f} h  {2 * hours:>11.0f} h"
        print(line)
    print()

    if not args.no_grid:
        projected = tuple(args.psi) if args.psi else stats.PROJECTED_RATES
        # The measured rate leads, and is the only unmarked column. Anything
        # else is what the instrument would see at a floor nobody has observed.
        grid_rates = [rate] + [p for p in projected if p != rate]
        cells = stats.psi_grid(
            args.sizes, grid_rates, args.delta, rate, args.alpha, args.power
        )
        print(stats.render_psi_grid(
            cells,
            args.delta,
            measured_rate=rate,
            per_item_minutes=per_item_minutes,
            corpus_n=corpus_size(),
            power=args.power,
        ))
        print()

    needed = stats.required_n(args.delta, rate, args.alpha, args.power)
    if needed is None:
        print(f"A {args.delta:.0%} difference is NOT detectable at {args.power:.0%} power at any")
        print("corpus size this script will consider. That is the answer, not a missing number.")
    else:
        print(f"Detecting a {args.delta:.0%} difference at {args.power:.0%} power needs "
              f"n = {needed} items.")
        if per_item_minutes:
            hours = needed * per_item_minutes / 60
            print(f"At the measured {per_item_minutes:.1f} min per item that is {hours:.0f} h for "
                  f"one run and {2 * hours:.0f} h for a comparison.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
