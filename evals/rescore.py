"""
Re-score a finished eval run from its saved artifacts — no inference.

A scoring bug should not cost another 20 hours of CPU. Every run records the
`project_dir` it produced, and the generated files are still on disk, so the
mechanical checks (extraction, parsing, execution, artifact kind, keywords) can
all be recomputed against the current scoring code.

The model judgment is NOT recomputed — it is the expensive part and it is not
what changed. Existing judge scores carry over untouched.

    python evals/rescore.py evals/results/20260806_195850
    python evals/rescore.py evals/results/20260806_195850 --dry-run

Writes results.jsonl, summary.json and summary.md in place, keeping a
`.pre-rescore` copy of the original so a re-score is never destructive.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scoring  # noqa: E402


def rescore_row(row: dict, exec_enabled: bool) -> tuple[dict, list[str]]:
    """Recompute mechanical scores for one row. Returns (row, changes)."""
    changes: list[str] = []
    project_dir = row.get("project_dir")
    if not project_dir or not Path(project_dir).exists():
        return row, ["artifacts missing — left as recorded"]

    code_dir = Path(project_dir) / "code"
    code_files = sorted(str(p) for p in code_dir.iterdir()) if code_dir.exists() else []

    before = {k: row.get(k) for k in ("extracted", "parses", "executes", "success", "exec_outcome")}

    row["code_files"] = code_files
    row["extracted"] = bool(code_files)
    parses, problems = scoring.check_parses(code_files) if code_files else (False, [])
    row["parses"] = parses
    row["problems"] = problems

    if exec_enabled and parses:
        result = scoring.execute_artifacts(code_files)
        row["executes"] = result["ok"]
        row["exec_outcome"] = result["outcome"]
        row["exec_detail"] = result["detail"]
    elif not parses:
        row["executes"] = False

    # Judge scores carry over, so a run judged originally stays judged here.
    row["success"] = scoring.is_success(row, require_judge=row.get("judge_score") is not None)

    for k, old in before.items():
        if row.get(k) != old:
            changes.append(f"{k}: {old} -> {row.get(k)}")
    return row, changes


def main():
    ap = argparse.ArgumentParser(description="Re-score an eval run from saved artifacts")
    ap.add_argument("run_dir")
    ap.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    ap.add_argument("--no-exec", action="store_true", help="skip re-executing generated code")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    results = run_dir / "results.jsonl"
    if not results.exists():
        sys.exit(f"No results.jsonl in {run_dir}")

    rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"Re-scoring {len(rows)} rows from {run_dir}\n")

    changed_rows = 0
    for row in rows:
        row, changes = rescore_row(row, exec_enabled=not args.no_exec)
        if changes:
            changed_rows += 1
            print(f"  {row['id']}")
            for c in changes:
                print(f"      {c}")

    passed = sum(1 for r in rows if r.get("success"))
    print(f"\n{changed_rows} row(s) changed")
    print(f"Success rate now {passed}/{len(rows)} ({round(100 * passed / max(len(rows), 1))}%)")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    backup = run_dir / "results.jsonl.pre-rescore"
    if not backup.exists():
        shutil.copy2(results, backup)
        print(f"Original preserved at {backup.name}")

    results.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    # Read the original metadata BEFORE overwriting, so model/prompt-set survive
    meta: dict = {}
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        try:
            prior = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(prior, dict):
                meta = dict(prior.get("meta") or {})
        except json.JSONDecodeError:
            pass
    meta.setdefault("run_id", run_dir.name)
    meta["rescored"] = True  # so a re-scored number is never read as a fresh run

    summary = scoring.summarize(rows)
    summary_path.write_text(json.dumps({**summary, "meta": meta}, indent=2), encoding="utf-8")
    (run_dir / "summary.md").write_text(
        scoring.render_markdown(summary, rows, meta), encoding="utf-8"
    )
    print("Rewrote results.jsonl, summary.json, summary.md")


if __name__ == "__main__":
    main()
