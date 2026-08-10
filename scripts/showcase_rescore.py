"""
Re-score a finished showcase run from its saved artifacts.

The same lesson `evals/rescore.py` exists for: a run costs hours of inference,
a scoring bug costs seconds to fix. Every row in a showcase log records the
HTML file it produced, and those files stay in `output/`, so any change to the
checks can be applied to work already done instead of paying for it again.

    python scripts/showcase_rescore.py                        # newest log
    python scripts/showcase_rescore.py --log scripts/showcase_results/showcase_X.jsonl
    python scripts/showcase_rescore.py --candidate chart      # only these rows

The original log is preserved as `<name>.pre-rescore` and rewritten in place,
so the file the summary reads from is always the current scoring.

Needs a real browser, like the original run did. Do not run it while a pipeline
or eval is using the CPU.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS = Path(__file__).parent / "showcase_results"

sys.path.insert(0, str(REPO))
from showcase import get as get_candidate  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from showcase_reliability import check_artifact  # noqa: E402


def newest_log() -> Path | None:
    logs = sorted(RESULTS.glob("showcase_*.jsonl"))
    return logs[-1] if logs else None


def main():
    ap = argparse.ArgumentParser(description="Re-score a showcase run from saved artifacts")
    ap.add_argument("--log", default=None, help="path to a showcase_*.jsonl (default: newest)")
    ap.add_argument("--candidate", default=None, help="only re-score these ids (comma-separated)")
    ap.add_argument("--dry-run", action="store_true", help="print changes, don't rewrite the log")
    args = ap.parse_args()

    log = Path(args.log) if args.log else newest_log()
    if not log or not log.exists():
        print("No showcase log found.")
        return 1

    only = {c.strip() for c in args.candidate.split(",")} if args.candidate else None
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]

    changed = 0
    skipped = 0
    for row in rows:
        if only and row.get("candidate") not in only:
            continue
        html = row.get("html")
        if not html or not Path(html).exists():
            skipped += 1
            continue  # artifact gone — leave the original verdict alone
        cand = get_candidate(row["candidate"])
        before = row.get("ok")
        fresh = check_artifact(Path(html), cand)
        row.update(fresh)
        if before != fresh["ok"]:
            changed += 1
            print(f"  {row['candidate']:10} run {row.get('run')}: ok {before} -> {fresh['ok']}  {fresh['reasons']}")

    if not args.dry_run:
        backup = log.with_suffix(log.suffix + ".pre-rescore")
        if not backup.exists():
            shutil.copy2(log, backup)
        with log.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    print(f"\n{changed} verdict(s) changed, {skipped} row(s) skipped (artifact missing).")
    for cid in sorted({r["candidate"] for r in rows}):
        got = [r for r in rows if r["candidate"] == cid]
        ok = sum(1 for r in got if r.get("ok"))
        print(f"{cid:10} {ok}/{len(got)}")
    if not args.dry_run:
        print(f"Rewrote {log} (original kept as .pre-rescore)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
