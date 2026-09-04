"""Run the eval harness's controls and print what they found.

    python scripts/eval_controls.py
    python scripts/eval_controls.py --seed-pairs 30

No Ollama, no network, no live inference. The artifacts are produced by the
deterministic stub in `evals/controls.py`; what is under test is the harness,
not the model.

Read the output in this order:

1. **Positive controls.** A deliberately degraded arm has to come out worse. If
   these do not clear alpha, the instrument cannot see a broken arm and
   certainly cannot see a prompt change — stop and fix that before believing
   anything else in the eval directory.
2. **What a weaker instrument would have said.** The same artifacts judged by
   parse-and-run alone. The gap between the two is the value of output-level
   grading, measured rather than asserted.
3. **Negative controls.** Identical configurations must not differ. Reported as
   a false-positive rate over many seed pairs rather than a single pass,
   because one non-significant draw is equally consistent with a correct noise
   model and a lucky seed.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "evals"))

import controls  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the eval harness controls.")
    ap.add_argument("--seed-pairs", type=int, default=20,
                    help="how many identical-configuration pairs the negative control runs")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=20260903, help="base seed")
    args = ap.parse_args()

    items = controls.load_fixture_corpus()
    print(f"Fixture corpus: {len(items)} items, stubbed model, no inference.")
    print(f"alpha = {args.alpha}\n")

    started = time.time()
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="eval_controls_") as workdir:
        workdir = Path(workdir)

        for arm in ("truncated", "shuffled"):
            outcome = controls.positive_control(items, arm, workdir, args.seed, args.alpha)
            print("=" * 68)
            print(f"POSITIVE CONTROL - degraded arm: {arm}")
            print("=" * 68)
            print(f"default arm passed {outcome['baseline_passes']}/{outcome['n']}, "
                  f"{arm} arm passed {outcome['degraded_passes']}/{outcome['n']}")
            print()
            print(outcome["report"])
            print()
            print(f"  DETECTED: {outcome['detected']}")
            if not outcome["detected"]:
                failures.append(f"positive control ({arm}) did not detect the degradation")

            print()
            print("  The same artifacts, graded by parse-and-run alone:")
            print()
            for line in outcome["weak_report"].splitlines():
                print("  " + line)
            print()
            print(f"  weaker instrument detected it: {outcome['weak_detected']}")
            print()

        pairs = [(args.seed + i, args.seed + i + 1) for i in range(args.seed_pairs)]
        rate = controls.negative_control_false_positive_rate(
            items, workdir, pairs, alpha=args.alpha
        )
        single = controls.negative_control(items, workdir, pairs[0][0], pairs[0][1], args.alpha)

        print("=" * 68)
        print("NEGATIVE CONTROL - the same configuration, twice")
        print("=" * 68)
        print(single["report"])
        print()
        print(f"  DIFFERS: {single['differs']}")
        if single["differs"]:
            failures.append("negative control reported a difference between identical runs")
        print()
        print(f"  Over {rate['trials']} identical-configuration pairs, "
              f"{len(rate['flagged'])} came out significant at alpha={rate['alpha']} "
              f"({rate['rate']:.0%}, 95% CI {rate['ci95'][0]:.0%}-{rate['ci95'][1]:.0%}).")
        print(f"  Expected under a correct noise model: about {rate['alpha']:.0%}.")
        if rate["flagged"]:
            print(f"  Flagged pairs: {rate['flagged']}")
        # The gate is on the interval, not the point estimate: with a handful of
        # trials the observed rate is itself noisy, and a rate whose interval
        # covers alpha is not evidence the noise model is wrong.
        if rate["ci95"][0] > rate["alpha"]:
            failures.append(
                f"negative control false-positive rate {rate['rate']:.0%} is above alpha "
                f"with the whole interval clear of it — the noise model is wrong"
            )

    print()
    print("=" * 68)
    print(f"Elapsed {time.time() - started:.0f}s")
    if failures:
        for line in failures:
            print(f"FAIL: {line}")
        return 1
    print("All controls behaved as required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
