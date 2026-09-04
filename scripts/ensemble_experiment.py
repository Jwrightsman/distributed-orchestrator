"""Does ensemble beat decomposition on a tightly-coupled artifact?

    python scripts/ensemble_experiment.py --candidate snake --trials 12

The claim under test, from the August 2026 external review: the pipeline's
weakness on coupled artifacts is *architectural*, not just model weakness.
Decomposition asks blind agents to agree on shared interfaces; a chart (10/10)
has almost nothing to agree about and a Snake game (2/10) has almost everything.
If that is right, having one node write the whole game — and running several
nodes independently, then keeping whichever result passes — should beat 2/10.

**What this measures, precisely.** Each trial is one model call producing a
complete artifact, scored by the same browser checks that produced the 2/10
(`showcase_reliability.check_artifact`). That gives `p`, the single-shot rate
for the ensemble architecture. Ensemble-of-N is then the chance that at least
one of N independent candidates passes, reported two ways: the closed form
1-(1-p)^N, and an empirical estimate by resampling the observed trials, which
does not assume independence holds.

**The comparison is p against the 2/10 decomposition baseline**, by Fisher's
exact test. Everything after that is arithmetic on p, so if p is not
distinguishable from 0.2 the honest report is "inconclusive" — printed in those
words, with the number of trials that would be needed. This project has thrown
away two prompt sets that looked good inside the noise; it is not going to
promote an architecture the same way.
"""

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ensemble  # noqa: E402
import showcase  # noqa: E402
from showcase_reliability import check_artifact  # noqa: E402

# The published decomposition result this is measured against. Both numbers are
# committed: scripts/showcase_results/showcase_20260808_162106.jsonl, re-scored
# Aug 15 after three bugs were fixed in the checker and still 2/10.
BASELINE = {"snake": (2, 10), "chart": (10, 10), "clock": (3, 4), "particles": (3, 4)}


# Fisher's exact test and the Wilson interval used to be defined here and,
# separately, in evals/compare.py. Two implementations of the same statistic is
# how two callers end up quietly disagreeing about what a p-value meant, so both
# now come from one place. The behaviour is unchanged: the self-check below
# still reproduces this project's published 10/10-vs-2/10 result, p = 0.00036.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
from stats import fisher_exact_greater, wilson  # noqa: E402


def ensemble_rate_empirical(outcomes: list[bool], n: int, draws: int = 20000) -> float:
    """P(at least one pass in a group of n), by resampling the observed trials.

    Sampling with replacement from the observed outcomes rather than assuming
    independence analytically. With a small number of trials the two agree; the
    point of showing both is that a gap between them is a signal the trials are
    not as independent as the closed form assumes.
    """
    if not outcomes:
        return 0.0
    rng = random.Random(20260815)
    hits = sum(
        1 for _ in range(draws)
        if any(rng.choice(outcomes) for _ in range(n))
    )
    return hits / draws


def min_trials_for_significance(baseline_k: int, baseline_n: int, true_p: float) -> int | None:
    """How many trials before an effect of this size could reach p<0.05.

    Returns None when no number of trials would do it — which is not a rare
    edge case, it is the normal answer for a moderate effect, and it is the
    most useful thing this script prints.

    Fisher's test is limited by the smaller sample, and **the baseline is only
    10 runs**. Against 2/10, a true rate of 0.5 asymptotes near p=0.06 no
    matter how many new trials are run: 5/10 -> 0.175, 10/20 -> 0.117,
    20/40 -> 0.086, 50/100 -> 0.067. More ensemble trials cannot fix
    uncertainty that lives in the baseline.

    So a moderate win is not provable by running this script harder. It needs
    the *decomposition* baseline extended too, at ~50 minutes per run. Only a
    large effect — roughly 8/10 or better — clears p<0.05 against the baseline
    as it stands.
    """
    for n in range(4, 400):
        k = round(true_p * n)
        if fisher_exact_greater(k, n - k, baseline_k, baseline_n - baseline_k) < 0.05:
            return n
    return None


def score_artifact(html: Path, cand) -> dict:
    """Run the browser check off the event loop.

    `check_artifact` uses Playwright's **sync** API, which refuses to run inside
    a thread that already has a running asyncio loop — and generation is async,
    so calling it straight from the per-trial callback raised
    "It looks like you are using Playwright Sync API inside the asyncio loop"
    on every single trial. A fresh worker thread has no loop of its own, which
    is all the sync API is asking for.

    Blocking the loop here is deliberate: generation is sequential by design on
    this hardware, so there is nothing else for the loop to be doing.
    """
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(check_artifact, html, cand).result()


def score_saved_run(out_root: Path, cand) -> list[dict]:
    """Re-score candidates already on disk, without regenerating them.

    Generation is hours and scoring is seconds, so a scoring bug must never
    cost the generation. Every candidate is written to
    `<run>/candidate_N/code/`, which makes the expensive half replayable.
    """
    rows = []
    for cdir in sorted(out_root.glob("candidate_*"),
                       key=lambda p: int(p.name.split("_")[1])):
        idx = int(cdir.name.split("_")[1])
        html = next(iter(cdir.glob("code/*.html")), None)
        if html is None:
            rows.append({"trial": idx, "ok": False, "reasons": ["no html extracted"],
                         "html": None})
            continue
        verdict = score_artifact(html, cand)
        rows.append({"trial": idx, "ok": bool(verdict["ok"]),
                     "reasons": verdict.get("reasons", []), "html": str(html)})
    return rows


def baseline_trials_needed(true_p: float, baseline_p: float, cap: int = 300) -> int | None:
    """How big BOTH samples must be for this effect to clear p<0.05.

    Assumes the baseline is re-measured at the same size as the experiment,
    which is the honest way to compare two architectures.
    """
    for n in range(4, cap):
        k, bk = round(true_p * n), round(baseline_p * n)
        if fisher_exact_greater(k, n - k, bk, n - bk) < 0.05:
            return n
    return None


def report(outcomes: list[bool], cand, elapsed: float, out_root: Path,
           ensemble_n: int = 3) -> None:
    """Print the verdict and write summary.json. Shared by both entry paths."""
    k, n = sum(outcomes), len(outcomes)
    lo, hi = wilson(k, n)
    b_k, b_n = BASELINE.get(cand.id, (2, 10))
    p_value = fisher_exact_greater(k, n - k, b_k, b_n - b_k)
    pass

    print(f"\n{'=' * 64}", flush=True)
    print(f"SINGLE-SHOT (ensemble architecture): {k}/{n} = {k/n:.0%}"
          f"   95% CI {lo:.0%}-{hi:.0%}", flush=True)
    print(f"DECOMPOSITION baseline             : {b_k}/{b_n} = {b_k/b_n:.0%}", flush=True)
    print(f"Fisher exact, one-sided            : p = {p_value:.4f}", flush=True)
    print(f"Mean seconds per candidate         : {elapsed/max(n,1):.0f}s", flush=True)

    if p_value < 0.05:
        print("\nVERDICT: ensemble's single-shot rate beats decomposition (p < 0.05).", flush=True)
    else:
        rate = k / n if n else 0
        need = min_trials_for_significance(b_k, b_n, rate)
        print("\nVERDICT: INCONCLUSIVE. This difference is not distinguishable from "
              "the baseline\n         at this number of trials.", flush=True)
        if need:
            print(f"         About {need} trials here would reach p<0.05, baseline unchanged.", flush=True)
        else:
            both = baseline_trials_needed(rate, b_k / b_n)
            print("         MORE TRIALS HERE CANNOT FIX IT. Fisher is limited by the", flush=True)
            print(f"         smaller sample, and the baseline is only {b_n} runs, so an", flush=True)
            print("         effect this size stays above p=0.05 however long this runs.", flush=True)
            if both:
                print(f"         Both arms would need about {both} runs each. The decomposition", flush=True)
                print("         side is ~50 min per run, which is the expensive half.", flush=True)
        print("         Do not promote ensemble on this result.", flush=True)

    print(f"\nENSEMBLE-OF-N (at least one candidate passes), from p = {k/n:.2f}:", flush=True)
    for grp in sorted({2, 3, ensemble_n, 5}):
        closed = 1 - (1 - k / n) ** grp if n else 0
        emp = ensemble_rate_empirical(outcomes, grp)
        print(f"  N={grp}: closed form {closed:.0%}   resampled {emp:.0%}"
              f"   (cost: {grp} model calls)", flush=True)
    print("\nThose rows are arithmetic on the single-shot rate, not separate", flush=True)
    print("measurements. If the verdict above is inconclusive, so are they.", flush=True)

    (out_root / "summary.json").write_text(json.dumps({
        "candidate": cand.id, "trials": n, "passes": k,
        "single_shot_rate": k / n if n else 0, "ci95": [lo, hi],
        "baseline": [b_k, b_n], "fisher_p_one_sided": p_value,
        "seconds_total": elapsed, "outcomes": outcomes,
    }, indent=2), encoding="utf-8")
    print(f"\nwrote {out_root/'summary.json'}", flush=True)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate", default="snake", help="showcase id (default: snake)")
    ap.add_argument("--trials", type=int, default=12, help="independent candidates to generate")
    ap.add_argument("--ensemble-n", type=int, default=3, help="group size to report")
    ap.add_argument("--out", default=None, help="results directory")
    ap.add_argument("--score-only", default=None, metavar="DIR",
                    help="re-score candidates already generated in DIR; no inference")
    args = ap.parse_args()

    cand = showcase.get(args.candidate)

    if args.score_only:
        root = Path(args.score_only)
        rows = score_saved_run(root, cand)
        outcomes = [r["ok"] for r in rows]
        for r in rows:
            print(f"  trial {r['trial']:>2}: {'PASS' if r['ok'] else 'fail'}  "
                  f"{str(r['reasons'])[:64]}", flush=True)
        (root / "rescored.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        report(outcomes, cand, 0.0, root, args.ensemble_n)
        return 0
    out_root = Path(args.out or f"scripts/ensemble_results/{args.candidate}_"
                                f"{time.strftime('%Y%m%d_%H%M%S')}")
    out_root.mkdir(parents=True, exist_ok=True)
    jsonl = out_root / "trials.jsonl"

    print(f"ENSEMBLE EXPERIMENT — {cand.id} ({cand.title})", flush=True)
    print(f"  {args.trials} independent complete-artifact candidates, one model call each", flush=True)
    print("  scored by the same browser checks that produced the published baseline", flush=True)
    print(f"  baseline (decomposition): {BASELINE.get(cand.id, ('?', '?'))}")
    print(f"  writing to {out_root}\n", flush=True)

    outcomes: list[bool] = []
    started = time.time()

    def record(res: ensemble.CandidateResult):
        html = next((f for f in res.files if f.endswith(".html")), None)
        verdict = {"ok": False, "reasons": ["no html extracted"]}
        if html:
            try:
                verdict = score_artifact(Path(html), cand)
            except Exception as e:
                verdict = {"ok": False, "reasons": [f"check crashed: {e}"]}
        outcomes.append(bool(verdict["ok"]))
        row = {
            "trial": res.index,
            "ok": bool(verdict["ok"]),
            "reasons": verdict.get("reasons", []),
            "seconds": round(res.elapsed_seconds, 1),
            "bytes": len(res.raw_output),
            "extracted": res.extracted,
            "parses": res.parses,
            "error": res.error,
            "html": html,
        }
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        mark = "PASS" if row["ok"] else "fail"
        print(f"  trial {res.index:>2}: {mark}  {int(res.elapsed_seconds):>4}s  "
              f"{str(row['reasons'])[:64]}", flush=True)

    if args.trials < 1:
        ap.error("--trials must be at least 1")
    await ensemble.run_ensemble(cand.pitch, args.trials, out_root, on_candidate=record)

    report(outcomes, cand, time.time() - started, out_root, args.ensemble_n)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
