"""Archive a known-good pipeline run as fallback demo material.

Live inference on CPU is variable. If the swarm produces something weak while
the camera is rolling, the recovery is to show a real run captured earlier —
not to re-roll on camera and hope.

`output/` is gitignored and gets pruned by the disk cap, so good runs disappear.
This copies one out into `docs/demo-assets/<name>/`, which is committed, along
with a manifest recording exactly what produced it.

    python scripts/capture_demo_asset.py --run latest --name snake-game
    python scripts/capture_demo_asset.py --run output/20260806_101500 --name expense-tracker
    python scripts/capture_demo_asset.py --list

**Everything captured is a real run.** The manifest records the model, the
prompt set, the rating and the mechanical checks, so nothing here can be
mistaken for something the swarm did not actually produce. A run whose code
fails `check_code_files` is refused unless you pass --force, because "known
good" has to mean something.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from extract import check_code_files  # noqa: E402
from execution.publication import (  # noqa: E402
    LegacyRunNotPublished,
    published_file,
    published_paths,
    require_legacy_run_publication,
)

ASSETS_DIR = REPO_ROOT / "docs" / "demo-assets"
OUTPUT_DIR = REPO_ROOT / "output"


def resolve_run(spec: str) -> Path:
    if spec == "latest":
        if not OUTPUT_DIR.is_dir():
            raise SystemExit(f"No output directory at {OUTPUT_DIR} — nothing to capture.")
        runs = sorted((d for d in OUTPUT_DIR.iterdir() if d.is_dir()), reverse=True)
        if not runs:
            raise SystemExit("No runs in output/ yet. Pitch something first.")
        return runs[0]
    run = Path(spec)
    if not run.is_absolute():
        run = REPO_ROOT / run
    if not run.is_dir():
        raise SystemExit(f"Not a run directory: {run}")
    return run


def read_rating(run: Path, publication=None) -> str:
    review = (
        published_file(publication, "review.md")
        if publication is not None
        else run / "review.md"
    )
    if review is not None and review.exists():
        for line in review.read_text(errors="replace").splitlines():
            if line.strip() in ("PASS", "NEEDS_WORK", "FAIL"):
                return line.strip()
    return "?"


def capture(run: Path, name: str, force: bool, note: str) -> int:
    log_path = run / "full_log.json"
    try:
        if not log_path.is_file():
            raise LegacyRunNotPublished
        log = json.loads(log_path.read_text(errors="replace"))
        publication = require_legacy_run_publication(run, log)
        code_files = [
            path
            for relative_path in published_paths(publication, "code")
            if (path := published_file(publication, relative_path)) is not None
        ]
        supplemental = {
            artifact: path
            for artifact in ("output.md", "review.md", "plan.json")
            if (path := published_file(publication, artifact)) is not None
        }
        if publication.sealed:
            assert publication.manifest is not None
            transcript_names = sorted(
                entry.relative_path
                for entry in publication.manifest.entries
                if "/" not in entry.relative_path
                and entry.relative_path.startswith("builder_")
                and entry.relative_path.endswith(".md")
            )
        else:
            transcript_names = sorted(path.name for path in run.glob("builder_*.md"))
        transcripts = [
            path
            for relative_path in transcript_names
            if (path := published_file(publication, relative_path)) is not None
        ]
        rating = log.get("rating") or read_rating(run, publication)
    except (json.JSONDecodeError, LegacyRunNotPublished, OSError):
        print(
            "Refusing to capture — the run has not crossed its durable "
            "publication boundary."
        )
        return 1

    problems = check_code_files([str(f) for f in code_files])

    if problems and not force:
        print("Refusing to capture — the extracted code has problems:")
        for p in problems:
            print(f"  - {p}")
        print("\nThis is meant to be fallback material you can show without checking it "
              "first.\nCapture anyway with --force if you know why it is fine.")
        return 1
    if not code_files:
        print("Refusing to capture — this run produced no code files.")
        return 1

    dest = ASSETS_DIR / name
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "code").mkdir(parents=True)

    for f in code_files:
        shutil.copy2(f, dest / "code" / f.name)
    for artifact, src in supplemental.items():
        if src is not None:
            shutil.copy2(src, dest / artifact)

    # Builder transcripts — the "several machines really did work on this" proof
    if transcripts:
        (dest / "transcript").mkdir()
        for t in transcripts:
            shutil.copy2(t, dest / "transcript" / t.name)

    try:
        from config import get as get_config

        model = get_config().get("model", "?")
    except Exception:
        model = "?"
    try:
        import orchestrator

        prompt_set = orchestrator.active_prompt_set().name
    except Exception:
        prompt_set = "?"

    manifest = {
        "name": name,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_run": run.name,
        "task": log.get("task", "(unknown — full_log.json missing)"),
        "model": log.get("model", model),
        "prompt_set": prompt_set,
        "rating": rating,
        "subtask_count": len(log.get("plan", []) or []),
        "code_files": [f.name for f in code_files],
        "mechanical_check": "clean" if not problems else f"{len(problems)} problem(s)",
        "problems": problems,
        "captured_with_force": bool(problems and force),
        "note": note,
        "provenance": "Real pipeline run, captured verbatim. Not edited by hand.",
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))

    readme = [
        f"# Demo asset — {name}",
        "",
        f"**Task:** {manifest['task']}",
        "",
        f"- Real run `{run.name}`, captured {manifest['captured_at']}",
        f"- Model `{manifest['model']}` · prompt set `{manifest['prompt_set']}` "
        f"· rating {manifest['rating']} · {manifest['subtask_count']} subtasks",
        f"- Mechanical check: {manifest['mechanical_check']}",
        "",
        "Captured verbatim from a real pipeline run — nothing here was written or "
        "edited by hand. Use it on camera if live inference misbehaves.",
        "",
        "## Files",
        "",
    ]
    readme += [f"- `code/{f.name}`" for f in code_files]
    if transcripts:
        readme += ["", f"`transcript/` holds the {len(transcripts)} builder outputs this was "
                       "assembled from."]
    if note:
        readme += ["", f"**Note:** {note}"]
    (dest / "README.md").write_text("\n".join(readme) + "\n")

    print(f"Captured {run.name} -> {dest.relative_to(REPO_ROOT)}")
    print(f"  task:    {manifest['task'][:70]}")
    print(f"  rating:  {manifest['rating']} · checks: {manifest['mechanical_check']}")
    print(f"  files:   {', '.join(manifest['code_files'])}")
    print("\nCommit it so it survives — output/ is gitignored and gets pruned.")
    return 0


def list_assets() -> int:
    if not ASSETS_DIR.is_dir():
        print("No demo assets captured yet.")
        print("After a good run:  python scripts/capture_demo_asset.py --run latest --name my-demo")
        return 0
    entries = sorted(d for d in ASSETS_DIR.iterdir() if d.is_dir())
    if not entries:
        print("No demo assets captured yet.")
        return 0
    print(f"{len(entries)} captured asset(s):\n")
    for d in entries:
        try:
            m = json.loads((d / "manifest.json").read_text())
        except (OSError, json.JSONDecodeError):
            print(f"  {d.name}  (unreadable manifest)")
            continue
        print(f"  {d.name}")
        print(f"    task:   {m.get('task', '?')[:66]}")
        print(f"    run:    {m.get('source_run')} · {m.get('model')} · "
              f"{m.get('prompt_set')} · {m.get('rating')} · {m.get('mechanical_check')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", help="run directory, or 'latest'")
    ap.add_argument("--name", help="short name for the captured asset, e.g. snake-game")
    ap.add_argument("--note", default="", help="anything worth remembering about this take")
    ap.add_argument("--force", action="store_true", help="capture even if code checks fail")
    ap.add_argument("--list", action="store_true", help="list captured assets")
    args = ap.parse_args()

    if args.list:
        return list_assets()
    if not args.run or not args.name:
        ap.error("--run and --name are required (or use --list)")
    return capture(resolve_run(args.run), args.name, args.force, args.note)


if __name__ == "__main__":
    raise SystemExit(main())
