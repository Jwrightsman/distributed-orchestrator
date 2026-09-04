"""
Compare two eval runs and say whether the difference is real.

This exists because the project got it wrong once. Prompt set v5 was kept as a
"special-purpose set" on the strength of web_app going 3/6 -> 5/6, which is
three prompts up and one down out of six — a coin flip. Meanwhile the overall
score had gone *down* by one. Eyeballing two summary.json files is how that
happens, so the eyeballing is now done by something that also does the
arithmetic.

    python evals/compare.py 20260808_050610 20260810_041455
    python evals/compare.py                 # the two most recent runs

What it reports:

  * **Churn** — how many prompts flipped in each direction. This is the number
    that matters and the one a success-rate diff hides. Two runs can both score
    17/28 and disagree on fourteen prompts.
  * **McNemar exact test** on the discordant pairs. The paired test is the right
    one here: the same 28 prompts are run both times, so what carries the
    evidence is which prompts *changed*, not the totals.
  * **A verdict** against the promote-or-delete rule in prompts/v3.py.

If both runs used the SAME prompt set it says so and reports the result as a
**noise floor** instead of a comparison — that is the run-to-run variance of
the harness itself, with no prompt difference to explain any of it. That number
is what every future comparison should be judged against.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The exact tests, the interval estimates and the power arithmetic all live in
# evals/stats.py now. They were duplicated here and in
# scripts/ensemble_experiment.py, which is how two callers end up disagreeing
# about what a p-value meant. Re-exported under their old names because this
# module's public surface is what tests/test_eval_compare.py pins.
from stats import mcnemar_exact_p, min_detectable  # noqa: E402,F401

RESULTS = Path(__file__).parent / "results"


def load_run(run_id: str) -> tuple[dict, dict]:
    """Return (meta, {prompt_id: record}) for a run directory."""
    d = RESULTS / run_id
    if not d.exists():
        raise SystemExit(f"No such run: {d}")

    records = {}
    jsonl = d / "results.jsonl"
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                records[r.get("id")] = r

    meta = {}
    summary = d / "summary.json"
    if summary.exists():
        blob = json.loads(summary.read_text(encoding="utf-8"))
        meta = blob.get("meta", blob)
    return meta, records


def compare(a_records: dict, b_records: dict, ids: list[str] | None = None) -> dict:
    shared = sorted(set(a_records) & set(b_records)) if ids is None else ids
    up = [i for i in shared if not a_records[i].get("success") and b_records[i].get("success")]
    down = [i for i in shared if a_records[i].get("success") and not b_records[i].get("success")]
    return {
        "n": len(shared),
        "a_success": sum(1 for i in shared if a_records[i].get("success")),
        "b_success": sum(1 for i in shared if b_records[i].get("success")),
        "up": up,
        "down": down,
        "churn": len(up) + len(down),
        "net": len(up) - len(down),
        "p": mcnemar_exact_p(len(down), len(up)),
        "p_one_sided": mcnemar_exact_p(len(down), len(up), one_sided=True),
    }


def main():
    ap = argparse.ArgumentParser(description="Compare two eval runs")
    ap.add_argument("run_a", nargs="?", help="baseline run id")
    ap.add_argument("run_b", nargs="?", help="candidate run id")
    ap.add_argument("--alpha", type=float, default=0.05, help="significance threshold")
    args = ap.parse_args()

    if not args.run_a or not args.run_b:
        runs = sorted(d.name for d in RESULTS.iterdir() if d.is_dir() and (d / "results.jsonl").exists())
        if len(runs) < 2:
            raise SystemExit("Need at least two completed runs to compare.")
        args.run_a, args.run_b = runs[-2], runs[-1]
        print(f"(no runs given — comparing the two most recent: {args.run_a} vs {args.run_b})\n")

    meta_a, a = load_run(args.run_a)
    meta_b, b = load_run(args.run_b)
    set_a = meta_a.get("prompt_set", "?")
    set_b = meta_b.get("prompt_set", "?")
    same_set = set_a == set_b and set_a != "?"

    res = compare(a, b)
    if res["n"] == 0:
        raise SystemExit("These runs share no prompt ids.")

    print(f"A  {args.run_a}   prompt set {set_a}   {res['a_success']}/{res['n']}")
    print(f"B  {args.run_b}   prompt set {set_b}   {res['b_success']}/{res['n']}")
    print()
    print(f"  improved : {len(res['up'])}")
    print(f"  regressed: {len(res['down'])}")
    print(f"  CHURN    : {res['churn']} of {res['n']} prompts changed outcome")
    print(f"  net      : {res['net']:+d}")
    print(f"  McNemar exact  one-sided p = {res['p_one_sided']:.4f}   <- the decision test")
    print(f"                 two-sided p = {res['p']:.4f}")
    need = min_detectable(res["churn"], args.alpha)
    if need is not None:
        print(f"  power    : with {res['churn']} prompts changed, {need} of them had to go")
        print(f"             one way to clear alpha={args.alpha}. Got {len(res['up'])} up.")
    else:
        print(f"  power    : {res['churn']} discordant pairs CANNOT reach alpha={args.alpha} "
              "in any split.")
    print()

    if same_set:
        pct = 100 * res["churn"] / res["n"]
        print("=" * 62)
        print(f"SAME PROMPT SET ({set_a}) — this is a NOISE FLOOR, not a comparison.")
        print("=" * 62)
        print(f"Nothing differed between these runs except chance, and {res['churn']} of")
        print(f"{res['n']} prompts ({pct:.0f}%) still changed outcome. The observed")
        print(f"success rates differ by {abs(res['net'])} prompt(s) with no cause.")
        print()
        print(f"USE THIS: treat any future net difference of <= {abs(res['net'])} prompt(s)")
        print("as indistinguishable from noise, and expect roughly this much churn")
        print("in every comparison. Record it in prompts/v3.py.")
        return

    print("Per category (small samples — a category result is never sufficient on its own):")
    cats = sorted({r.get("category", "?") for r in b.values()})
    for cat in cats:
        ids = [i for i in set(a) & set(b) if b[i].get("category") == cat]
        if not ids:
            continue
        c = compare(a, b, ids)
        flag = "" if c["p_one_sided"] < args.alpha else "  (not significant)"
        print(f"  {cat:16} {c['a_success']}/{c['n']} -> {c['b_success']}/{c['n']}"
              f"  net {c['net']:+d}  p={c['p_one_sided']:.2f}{flag}")
    print()
    print("  A category here has at most 6 prompts. Even a clean sweep often cannot")
    print("  reach significance, so a category is a lead to investigate, never a")
    print("  reason to keep a set the overall number rejected.")
    print()

    print("VERDICT, per the rule in prompts/v3.py:")
    if res["net"] > 0 and res["p_one_sided"] < args.alpha:
        print(f"  PROMOTE {set_b}. It beats {set_a} by {res['net']} prompts, "
              f"one-sided p={res['p_one_sided']:.4f}.")
    elif res["net"] > 0:
        print(f"  DELETE {set_b}. It is {res['net']} prompt(s) ahead, but "
              f"one-sided p={res['p_one_sided']:.4f}")
        print("  does not clear the bar — that is indistinguishable from run-to-run")
        print("  variance. Note this is a POWER problem, not evidence of harm: the")
        print("  set may well be better and this eval cannot tell. A category that")
        print("  improved is still not a reason to keep it — that is the exact")
        print("  mistake v5 was kept on. Promote or delete, no middle ground.")
    else:
        print(f"  DELETE {set_b}. It does not move the score ({res['net']:+d}).")
    print()
    print("  Judge this against the harness's own noise floor before believing it.")
    print("  Run the SAME prompt set twice and compare those two runs — this tool")
    print("  reports that case as a noise floor rather than a comparison.")


if __name__ == "__main__":
    main()
