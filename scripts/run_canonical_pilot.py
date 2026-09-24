"""Run the canonical direct / direct-with-repair / DAG pilot for real, on this machine.

This is the live activation the pilot plan kept deliberately absent until an
owner approved it (docs/experiments/2026-09-13-canonical-pilot-plan.md). It:

* observes the real runtime identity from the local Ollama (version, binary
  hash, model digest and quantisation) instead of accepting typed-in values;
* freezes a rubric and a blinding map before the first call and binds their
  hashes into the manifest;
* grades each candidate with `evals/grading.py`, which RUNS the generated
  Python script: a scratch directory, a scrubbed environment and a timeout.
  That is a speed bump, not a sandbox, which is why `--i-approve-running-
  generated-code` must be typed;
* runs all 36 cells once, in the frozen order, and stops at the plan's budget
  (10 hours of model time plus 1 hour of checking at most) or the first
  operational failure.

Local placement only. No worker, enrollment, or network listener is involved,
and nothing from production state is read or written: the service lives in a
disposable workspace inside the output directory.

    py scripts/run_canonical_pilot.py --owner "Jett Wrightsman" \
        --i-approve-running-generated-code --output C:\\mycelium-pilot\\run1
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import corpus, grading  # noqa: E402
from evals.canonical_pilot import (  # noqa: E402
    ITEM_IDS, SETTINGS, CanonicalPilot, PreflightError, digest, planned_cells,
    prepare_manifest, validate_activation,
)

MODEL = "qwen3.5:4b"
ROOT = Path(__file__).resolve().parents[1]
RUBRIC = """Acceptance rubric, canonical development pilot v1.

The acceptance judgment for every cell is the mechanical checker in
evals/grading.py at the grader version bound in the manifest: the generated
script must parse, be Python, contain the item's keywords, run to completion on
the item's fixture inputs, and pass the item's declared output checks.
No human acceptance review is part of this run. Any later human review is a
separate, labelled judgment and does not replace these booleans.
"""


def _sha256_file(path, _cache={}):
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _cache:
        h = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                h.update(block)
        _cache[key] = h.hexdigest()
    return _cache[key]


def _fixture_digest():
    files = {}
    for sub in ("inputs", "schemas"):
        for p in sorted((ROOT / "evals" / "fixtures" / sub).rglob("*")):
            if p.is_file():
                files[p.relative_to(ROOT).as_posix()] = _sha256_file(p)
    return digest(files)


def _hardware_id():
    ram = None
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            ram = round(status.ullTotalPhys / 2**30)
    except (AttributeError, OSError):
        try:
            ram = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30)
        except (ValueError, OSError, AttributeError):
            ram = None
    parts = [platform.node(), platform.machine(), platform.processor() or "cpu-unknown",
             f"{ram}GiB" if ram else "ram-unknown", f"{os.cpu_count()}cpu"]
    return " / ".join(parts)


class Observer:
    """Re-observes the runtime before every call; nothing here is typed in."""

    def __init__(self, url, owner, isolation, rubric_sha, blinding_sha):
        self.url = url.rstrip("/")
        self.fixed = {"owner_approval_id": owner, "isolation_approval_id": isolation,
                      "rubric_sha256": rubric_sha, "blinding_map_sha256": blinding_sha,
                      "hardware_id": _hardware_id(), "os": platform.platform()}

    def __call__(self):
        with httpx.Client(timeout=10) as client:
            version = client.get(f"{self.url}/api/version").json()["version"]
            models = client.get(f"{self.url}/api/tags").json()["models"]
        model = next((m for m in models if m["name"] == MODEL), None)
        if model is None:
            raise PreflightError(f"{MODEL} is not installed in the local Ollama")
        binary = shutil.which("ollama")
        if not binary:
            raise PreflightError("ollama binary is not on PATH; its hash cannot be observed")
        env = {k: v for k, v in sorted(os.environ.items()) if k.startswith("OLLAMA_")}
        return {**self.fixed,
                "model_digest": model["digest"],
                "quantization": model.get("details", {}).get("quantization_level") or "",
                "runtime_version": version,
                "runtime_binary_sha256": _sha256_file(Path(binary)),
                "provider_settings_sha256": digest({"url": self.url, "settings": SETTINGS,
                                                    "ollama_env": env}),
                "acceptance_fixture_sha256": _fixture_digest()}

    def warm_cold(self):
        try:
            loaded = httpx.get(f"{self.url}/api/ps", timeout=5).json().get("models", [])
        except (httpx.HTTPError, ValueError):
            return "unknown"
        return "warm" if any(m.get("name") == MODEL for m in loaded) else "cold"


def make_checker(scratch):
    from extract import extract_code_files

    items = {item.id: item for item in corpus.load_corpus()}

    async def checker(item_id, candidate, *, deadline_monotonic):
        item = items[item_id]
        target = Path(tempfile.mkdtemp(prefix=f"{item_id}-", dir=scratch))
        files = [str(p) for p in extract_code_files(candidate, target)]
        timeout = max(1, min(grading.EXEC_TIMEOUT, int(deadline_monotonic - time.monotonic())))
        result = await asyncio.to_thread(grading.grade, item, files, timeout)
        failed = set(result.failed_checks) | set(result.ungraded_checks)
        if result.passed:
            return True, "requirement"
        if not files or failed & {"artifact_kind"}:
            return False, "artifact"
        if failed & {"parses"}:
            return False, "syntax"
        return False, "requirement"

    return checker


def summarise(directory):
    cells = [json.loads(line) for line in (directory / "pilot-events.jsonl").open(encoding="utf-8")
             if '"cell_result"' in line]
    by_arm = {}
    for row in cells:
        if row.get("event") != "cell_result":
            continue
        arm = row["cell"]["arm"]
        stats = by_arm.setdefault(arm, {"accepted": 0, "rejected": 0, "other": 0, "seconds": 0.0,
                                        "repair_forms": {}})
        key = row["outcome"] if row["outcome"] in ("accepted", "rejected") else "other"
        stats[key] += 1
        stats["seconds"] += sum((c.get("inference_seconds") or 0) for c in row.get("unit_costs", []))
        if row.get("repair_form"):
            stats["repair_forms"][row["repair_form"]] = stats["repair_forms"].get(row["repair_form"], 0) + 1
    summary = {"planned_per_arm": len(ITEM_IDS), "cells_finished": len(cells), "arms": by_arm,
               "note": "Development pilot, one replicate, one machine. It picks a direction; "
                       "it does not prove a general effect."}
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("\n=== Result (accepted out of 12 planned per arm) ===")
    for arm in ("direct", "direct-with-repair", "dag"):
        s = by_arm.get(arm, {"accepted": 0, "rejected": 0, "other": 0, "seconds": 0.0})
        print(f"  {arm:20s} accepted {s['accepted']:2d}   rejected {s['rejected']:2d}   "
              f"did not finish {s['other']:2d}   model time {s['seconds'] / 60:6.1f} min")
    print(f"Saved in {directory}")
    return summary


async def run(args):
    import config
    from execution.artifacts import ArtifactStore
    from execution.persistence import ExecutionStore
    from execution.service import ExecutionService
    import ollama_client

    out = args.output.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty. Pick a new folder; a pilot is never resumed.")
    out.mkdir(parents=True, exist_ok=True)
    try:
        out.relative_to(ROOT)
        raise SystemExit("Put the output folder outside the repository; the pilot hashes the repo.")
    except ValueError:
        pass

    (out / "rubric.txt").write_text(RUBRIC, encoding="utf-8")
    rng = random.Random()
    blinding = {f"{c['item_id']}|{c['arm']}": f"A{rng.randrange(16**8):08x}" for c in planned_cells()}
    (out / "blinding-map.json").write_text(json.dumps(blinding, indent=2, sort_keys=True) + "\n",
                                           encoding="utf-8")
    observer = Observer(args.ollama_url, f"owner:{args.owner}",
                        f"isolation:scratch-dir+scrubbed-env+timeout approved by {args.owner}",
                        _sha256_file(out / "rubric.txt"), _sha256_file(out / "blinding-map.json"))

    observed = observer()
    manifest = prepare_manifest(args.study_id, observed)
    validate_activation(manifest, observed)
    print(f"Model {MODEL} {observed['model_digest'][:12]}  Ollama {observed['runtime_version']}  "
          f"on {observed['hardware_id']}")
    print(f"{args.smoke or 36} cells, stops by itself after 10 hours of model time. Output: {out}\n")

    workspace = out / "workspace"
    workspace.mkdir()
    scratch = out / "graded"
    scratch.mkdir()
    previous = Path.cwd()
    os.chdir(workspace)
    try:
        settings = dict(config.DEFAULTS)
        settings.update(model=MODEL, provider=None, ollama_url=args.ollama_url,
                        temperature=SETTINGS["temperature"], seed=SETTINGS["seed"],
                        context_tokens=SETTINGS["context_tokens"])
        with patch.object(config.get, "_cache", settings, create=True), \
                patch.dict(ollama_client._capabilities_cache, clear=True):
            ollama_client._sync_globals()
            db = workspace / "execution.db"
            service = ExecutionService(store=ExecutionStore(db),
                                       artifacts=ArtifactStore(db, allowed_roots=[workspace]))
            service._emit = lambda *a: None
            pilot = CanonicalPilot(service, manifest, observer, make_checker(scratch), out,
                                   warm_cold=observer.warm_cold)
            schedule = manifest["schedule"][:args.smoke] if args.smoke else manifest["schedule"]
            started = time.monotonic()
            for index, cell in enumerate(schedule, 1):
                print(f"[{index:2d}/36] {cell['item_id']:28s} {cell['arm']:20s}", end=" ", flush=True)
                try:
                    outcome = await pilot.run_cell(cell)
                except Exception as exc:  # noqa: BLE001 - reported, then the pilot stops
                    print(f"STOPPED: {type(exc).__name__}: {exc}")
                    break
                elapsed = (time.monotonic() - started) / 60
                print(f"{outcome}  ({elapsed:.0f} min so far)")
                if pilot.ledger.stopped:
                    print(f"Stopped by the plan's own rule: {pilot.ledger.stopped}")
                    break
    finally:
        os.chdir(previous)
        gc.collect()
    return summarise(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True,
                        help="new, empty folder outside the repository")
    parser.add_argument("--owner", help="the machine owner approving this run")
    parser.add_argument("--i-approve-running-generated-code", action="store_true",
                        help="required: the checker runs each generated script")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--study-id", default="canonical-development-pilot-v1")
    parser.add_argument("--smoke", type=int, metavar="N",
                        help="check the setup: run only the first N cells as a study labelled "
                             "SMOKE-NOT-A-RESULT, never reported")
    parser.add_argument("--summary-only", action="store_true",
                        help="re-print the result of an existing output folder")
    args = parser.parse_args()
    if args.summary_only:
        summarise(args.output.resolve())
        return
    if args.smoke:
        args.study_id = "SMOKE-NOT-A-RESULT"
    if not args.owner:
        raise SystemExit("Name the machine owner approving this run with --owner.")
    if not args.i_approve_running_generated_code:
        raise SystemExit("The checker runs each generated Python script (scratch folder, scrubbed "
                         "environment, timeout; not a sandbox). Add "
                         "--i-approve-running-generated-code to accept that.")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
