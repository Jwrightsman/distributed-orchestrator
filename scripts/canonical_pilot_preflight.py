"""Prepare unresolved identities or run authored offline controls; no live mode."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.canonical_pilot import prepare_manifest  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--study-id", default="canonical-development-pilot-v1")
    args = parser.parse_args()
    if args.dry_run:
        from evals.pilot_controls import dry_run
        print(json.dumps(asyncio.run(dry_run(args.output)), indent=2))
        return
    manifest = prepare_manifest(args.study_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print("Prepared only. Activation blockers: " + ", ".join(manifest["activation_blockers"]))


if __name__ == "__main__":
    main()
