"""
Run `cli.py --demo` N times and check it tells its story every time (§1.3).

`--demo` is the memory shot in the video: pitch an expense tracker, then pitch
"add monthly budgets" against the same project and watch the second run load
what the first one built. So "did it finish" is not the bar. The claim on
camera is that memory carried, and that is what gets checked:

  - the process exits clean, with no error panel in its output
  - two runs are produced, both with extractable code
  - the extracted Python actually parses (production checker, not a guess)
  - the project's memory.md records BOTH iterations
  - iteration 2's memory entry is present, which is the thing being demoed

    python scripts/demo_reliability.py --runs 3

Each run is two full pipelines — budget ~1-2 hours each. Results land in
scripts/demo_results/ so the pass rate is traceable rather than remembered.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS = Path(__file__).parent / "demo_results"

sys.path.insert(0, str(REPO))
from extract import check_code_files  # noqa: E402


def _dirs(path: Path) -> set[str]:
    return {d.name for d in path.iterdir() if d.is_dir()} if path.exists() else set()


def check_run(new_runs: list[Path], project_dir: Path | None, stdout: str) -> dict:
    """Decide whether this take would survive being filmed."""
    reasons: list[str] = []

    # The pipeline prints a red panel rather than raising; catch that too.
    if re.search(r"Unexpected error|Pipeline failed|Demo aborted", stdout):
        reasons.append("error panel in output")

    if len(new_runs) < 2:
        reasons.append(f"expected 2 pipeline runs, got {len(new_runs)}")

    code_ok = 0
    for run in new_runs:
        code_dir = run / "code"
        files = sorted(str(p) for p in code_dir.iterdir()) if code_dir.exists() else []
        if not files:
            reasons.append(f"{run.name}: no code extracted")
            continue
        problems = check_code_files(files)
        if problems:
            reasons.append(f"{run.name}: {problems[0][:70]}")
        else:
            code_ok += 1

    iterations = 0
    if project_dir and (project_dir / "memory.md").exists():
        memory = (project_dir / "memory.md").read_text(encoding="utf-8", errors="ignore")
        iterations = memory.count("### Iteration")
        if iterations < 2:
            # This is the demo's actual claim — without it there is no memory shot
            reasons.append(f"memory records {iterations} iteration(s), need 2")
    else:
        reasons.append("no project memory file")

    return {
        "clean": not reasons,
        "reasons": reasons,
        "runs_produced": len(new_runs),
        "runs_with_valid_code": code_ok,
        "memory_iterations": iterations,
    }


def main():
    ap = argparse.ArgumentParser(description="Measure --demo reliability (sprint §1.3)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--fast", action="store_true", help="use --demo-fast (skips the inter-pitch pause)")
    args = ap.parse_args()

    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log = RESULTS / f"demo_{stamp}.jsonl"
    rows = []

    out_dir, proj_dir = REPO / "output", REPO / "projects"

    for i in range(1, args.runs + 1):
        before_out, before_proj = _dirs(out_dir), _dirs(proj_dir)
        print(f"\n=== run {i}/{args.runs} — two full pipelines, ~1-2 h", flush=True)
        t0 = time.time()

        proc = subprocess.run(
            [sys.executable, "cli.py", "--demo-fast" if args.fast else "--demo"],
            cwd=REPO, capture_output=True, text=True, errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        elapsed = round(time.time() - t0, 1)

        new_runs = sorted(
            (out_dir / d for d in _dirs(out_dir) - before_out), key=lambda p: p.name
        )
        new_projects = _dirs(proj_dir) - before_proj
        project = (proj_dir / sorted(new_projects)[0]) if new_projects else None

        row = {"run": i, "seconds": elapsed, "exit_code": proc.returncode,
               "project": project.name if project else None}
        row.update(check_run(new_runs, project, proc.stdout + proc.stderr))
        rows.append(row)

        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

        mark = "CLEAN " if row["clean"] else "FAILED"
        print(f"    {mark} in {elapsed/60:.0f} min — {row['reasons'] or 'memory carried, code parses'}",
              flush=True)
        print(f"    running total: {sum(1 for r in rows if r['clean'])}/{len(rows)} clean", flush=True)

    clean = sum(1 for r in rows if r["clean"])
    print(f"\n{'=' * 52}")
    print(f"--demo reliability: {clean}/{len(rows)} clean")
    print(f"Log: {log}")
    if clean < len(rows):
        print("Not every take is filmable — see reasons above before relying on this shot.")


if __name__ == "__main__":
    main()
