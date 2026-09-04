"""What would k runs per item buy, and would it be cheaper than more items?

    python scripts/eval_replicate_power.py
    python scripts/eval_replicate_power.py --delta 0.15 --trials 4000

`docs/eval-methodology.md` §7 names replicates with a continuous endpoint as
the remaining lever after corpus growth, and asserts they are "genuinely more
efficient per unit of inference". This script is the arithmetic for that claim.
`docs/experiments/replicate-endpoint-design.md` is the pre-registration it
supports, and reports what came out.

**Every number here is a projection, not a measurement.** Wilcoxon's power has
no closed form, so it is simulated over a stated generative model, seeded so it
reproduces exactly. The model is the weak point and is printed with the result
rather than buried: each item draws a baseline pass probability from the 28
banded corpus items, the candidate arm gets that plus `delta` clipped at 1, and
each arm runs the item k times independently.

Two things about that model are worth knowing before quoting anything:

* **It assumes runs of one item are independent.** Under independence a
  discordant pair rate is E[2p(1-p)], which cannot exceed 0.5 whatever the item
  mix. The measured floor is psi = 0.643, above that ceiling. So this model is
  *more optimistic* than the measurement, and every power figure here is an
  upper bound on what the real instrument would deliver.
* **A ceiling item absorbs the effect.** Two of the 28 banded items sit at
  5/5, where an additive 15-point improvement has nowhere to go. That is
  realistic — it is why the corpus is banded — and it makes the realised mean
  effect slightly below `delta`.

The comparison the script exists to make is at **matched inference cost**:
k runs on n items is k*n item-runs per arm, the same currency as k=1 on k*n
items. Whether splitting that budget across replicates or across items buys
more resolution is the question, and it is answered by putting the two on the
same row.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "evals"))

import stats  # noqa: E402

PROMPTS = REPO_ROOT / "evals" / "prompts.json"

# One complete candidate, median of the August experiment's 22 trials. The
# cheapest arm, and the one a banding or noise-floor run uses.
DIRECT_MINUTES = 6.1

# The DAG arm, mean over the 140 recorded item-runs in evals/results/. The arm
# a study of the shipping architecture would actually use, and the reason the
# cheap arm is quoted first.
DECOMPOSITION_MINUTES = 29.3

# (k, n) pairs, matching the table in
# docs/experiments/replicate-endpoint-design.md so the document can be checked
# rather than believed. The two sizes that exist are n = 36 (the frozen
# confirmatory set) and n = 100 (the whole corpus); n = 187 is on the list
# because it is what a single-run design needs, and it is 87 prompts that
# nobody has written.
DEFAULT_DESIGNS = [
    (1, 36),
    (2, 36),
    (3, 36),
    (5, 36),
    (10, 36),
    (1, 100),
    (2, 100),
    (3, 100),
    (1, 187),
]

# The n a design can actually be run at today, and why. Printed with the row so
# a table of powers cannot be read without the constraint that decides it.
REACHABLE = {
    36: "the frozen confirmatory set",
    100: "the whole corpus, once",
    187: "NOT REACHABLE - 87 prompts that do not exist",
}


def banded_rates(path: Path = PROMPTS) -> list[float]:
    """Per-item pass rates from the banded corpus items.

    Raises rather than substituting a made-up distribution: a power figure
    computed over an invented item mix is a number about nothing.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    rates = [
        item["band"]["passes"] / item["band"]["trials"]
        for item in data.get("prompts", [])
        if item.get("band")
    ]
    if not rates:
        raise SystemExit(
            f"ERROR: {path} carries no banded items, so there is no measured item "
            "mix to simulate over. Refusing to invent one."
        )
    return rates


def main() -> int:
    ap = argparse.ArgumentParser(description="Power of the replicate endpoint design.")
    ap.add_argument("--delta", type=float, default=0.15,
                    help="target effect in per-item pass rate (default 0.15)")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--trials", type=int, default=3000,
                    help="simulated studies per design (default 3000)")
    ap.add_argument("--seed", type=int, default=20260904)
    ap.add_argument("--minutes", type=float, default=DIRECT_MINUTES,
                    help=f"minutes per item-run (default {DIRECT_MINUTES}, the direct arm)")
    args = ap.parse_args()

    rates = banded_rates()
    implied = sum(2 * p * (1 - p) for p in rates) / len(rates)
    print(f"Item mix: {len(rates)} banded corpus items, "
          f"mean pass rate {statistics.mean(rates):.3f}")
    print(f"  rates: {sorted(rates)}")
    print(f"  discordant rate this mix implies for k=1 under independence: {implied:.3f}")
    print(f"  measured noise floor (psi, one identical-configuration pair): "
          f"{stats.MEASURED_DISCORDANT_RATE:.3f}")
    print("  The measurement is ABOVE what independence permits (max 0.5), so every")
    print("  figure below is optimistic relative to the instrument as measured.")
    print()
    print(f"PROJECTION. alpha = {args.alpha}, delta = {args.delta:.0%}, "
          f"{args.trials} simulated studies at seed {args.seed}.")
    print("Wilcoxon signed-rank, one-sided, exact null. Nothing here was run against a model.")
    print()

    label = f"power@{args.delta:.0%}"
    header = (
        f"{'k':>3}  {'n':>5}  {'runs/arm':>9}  {'direct':>8}  {'decomp':>8}  "
        f"{label:>9}  {'k=1 same cost':>14}  {'better':>7}  reachable today"
    )
    print(header)
    print("-" * len(header))
    for k, n in DEFAULT_DESIGNS:
        runs = k * n
        power = stats.replicate_power(
            n, k, args.delta, rates, trials=args.trials, seed=args.seed, alpha=args.alpha
        )
        # The same inference budget spent entirely on items instead. This is
        # the comparison; a replicate design that only beats a *smaller*
        # single-run study has not shown anything about efficiency.
        matched = stats.replicate_power(
            runs, 1, args.delta, rates, trials=args.trials, seed=args.seed, alpha=args.alpha
        )
        if k == 1:
            verdict = "-"
        elif abs(power - matched) < 0.02:
            verdict = "tie"
        else:
            verdict = "replicates" if power > matched else "items"
        print(f"{k:>3}  {n:>5}  {runs:>9}  {2 * runs * args.minutes / 60:>6.0f} h  "
              f"{2 * runs * DECOMPOSITION_MINUTES / 60:>6.0f} h  {power:>9.3f}  "
              f"{matched:>14.3f}  {verdict:>7}  {REACHABLE.get(n, '?')}")
    print()
    print("Each row spends the same inference two ways: k runs on n items, or one run")
    print("on k*n items. That is the comparison, and it comes out a tie or within about")
    print("two standard errors of one: power depends on n*k, the total run count, and")
    print("barely on how it is split. Read the `better` column with the Monte Carlo")
    print("error below in hand before calling any row a difference.")
    print()
    print("So replicates buy little or nothing per unit of inference. What they buy is")
    print("resolution inside a corpus that cannot grow -- the confirmatory 36 are frozen")
    print("by a digest, and k is the only lever a pre-registered study on that set has.")
    print()
    print("Monte Carlo error at these trial counts is about "
          f"{(0.25 / args.trials) ** 0.5:.3f} on a power near 0.5, so differences")
    print("smaller than about 0.02 are not differences.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
