"""Band corpus items by difficulty, so the corpus has resolving power.

    python scripts/eval_band_corpus.py --from-results          # show what the committed runs say
    python scripts/eval_band_corpus.py --from-results --write  # record those bands
    python scripts/eval_band_corpus.py --live --trials 5       # run the unbanded items (needs Ollama)

**Why bands.** In a paired design, power comes only from items where the arms
can differ. An item every arm passes and an item every arm fails contribute
exactly nothing to McNemar's test, so a corpus of ceiling and floor items can
be arbitrarily large and still resolve nothing. This project already has one of
each in its published numbers: a labelled bar chart at 10/10 and a playable
Snake game at 2/10.

Bands are `floor` (0-20% pass), `discriminating` (20-80%) and `ceiling`
(80-100%), from `evals/corpus.py`. Some floor and ceiling items are kept
deliberately, to catch a breakthrough and a regression respectively — they are
just not where the power is.

**`--from-results` is provisional, and says so in the record it writes.** The
five committed runs used four different prompt sets, so a per-item rate pooled
across them mixes run-to-run noise with prompt-set differences. It is the best
estimate available without paying for inference, and it is labelled as an
estimate rather than presented as a banding run.

**`--live` is the real thing** and costs what it costs: `trials` runs of the
cheapest arm per item, at roughly 6 minutes a run for a single complete
candidate. It is not run automatically and never by CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "evals"))

import corpus as corpus_mod  # noqa: E402

PROMPTS_FILE = REPO_ROOT / "evals" / "prompts.json"
RESULTS = REPO_ROOT / "evals" / "results"

PROVISIONAL_SOURCE = (
    "provisional: recomputed from the five committed runs in evals/results/, "
    "which used four different prompt sets rather than one fixed arm"
)

# Which grader produced the evidence a band rests on. The five committed runs
# predate the grading correction, so a band computed from them is a band
# computed under the old checker and the record says so rather than leaving a
# reader to work it out from dates.
LEGACY_GRADER = "legacy (pre-correction)"

# The specific defect, on the specific items it reaches. Before the correction,
# an HTML artifact "executed" if it loaded without throwing — `browser_ok`.
# Under that check `web-snake` passed 5 of 5 committed runs; the behavioural
# checker in `scripts/showcase_reliability.py` measured the same artifact at
# 2 of 10. Every band derived from those runs for an HTML item inherits the
# weaker check, which makes it known-suspect rather than merely provisional.
#
# Keyed on the expected artifact rather than the category name, because the
# defect is in the HTML execution check. On the current corpus that is exactly
# the six banded `web_app` items.
PRE_CORRECTION_CAVEAT = (
    "known-suspect: computed under the pre-correction grader, where an HTML "
    "artifact 'executed' if it loaded without throwing (browser_ok). web-snake "
    "bands ceiling at 5/5 under that check and measured 2/10 under the "
    "behavioural checker in scripts/showcase_reliability.py. Re-band with "
    "--live under the current grader before relying on any HTML band."
)


def bands_from_results() -> dict[str, tuple[int, int]]:
    """Per-item (passes, trials) from every committed eval run."""
    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for directory in sorted(RESULTS.iterdir()) if RESULTS.is_dir() else []:
        log = directory / "results.jsonl"
        if not directory.is_dir() or not log.exists():
            continue
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            mechanical = bool(
                record.get("extracted")
                and record.get("parses")
                and record.get("executes")
                and record.get("artifact_match")
                and record.get("keywords_ok")
            )
            entry = tally[record["id"]]
            entry[0] += int(mechanical)
            entry[1] += 1
    return {item_id: (passes, trials) for item_id, (passes, trials) in tally.items()}


async def bands_live(items, trials: int) -> dict[str, tuple[int, int]]:
    """Run each item `trials` times through the cheapest arm and band it.

    The cheapest arm is a single complete candidate — `ensemble.run_ensemble`
    with one candidate, which is the same generator the ensemble strategy uses
    with N=1. Banding deliberately does not use the DAG: the point is to place
    the item on a difficulty scale as cheaply as possible, not to measure an
    architecture.
    """
    import ensemble
    import grading

    tally: dict[str, tuple[int, int]] = {}
    for index, item in enumerate(items, 1):
        passes = 0
        for trial in range(trials):
            started = time.time()
            outdir = REPO_ROOT / "evals" / "banding" / item.id / str(trial)
            outdir.mkdir(parents=True, exist_ok=True)
            candidates = await ensemble.run_ensemble(item.task, 1, outdir)
            files = candidates[0].files if candidates else []
            verdict = grading.grade(item, files)
            passes += int(verdict.passed)
            print(f"  [{index}/{len(items)}] {item.id} trial {trial + 1}/{trials}: "
                  f"{'pass' if verdict.passed else 'fail'} "
                  f"({time.time() - started:.0f}s)", flush=True)
        tally[item.id] = (passes, trials)
    return tally


def apply(
    tally: dict[str, tuple[int, int]],
    source: str,
    write: bool,
    grader: str,
    pre_correction: bool = False,
) -> int:
    data = json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
    prompts = data["prompts"]
    if not prompts:
        print("ERROR: the corpus is empty — nothing to band.")
        return 1

    changed = 0
    distribution: Counter[str] = Counter()
    for entry in prompts:
        evidence = tally.get(entry["id"])
        if evidence is None:
            distribution[entry.get("band", {}).get("label") if entry.get("band") else "unbanded"] += 1
            continue
        passes, trials = evidence
        label = corpus_mod.classify_band(passes, trials)
        distribution[label] += 1
        band = {
            "label": label,
            "passes": passes,
            "trials": trials,
            "source": source,
            "grader": grader,
        }
        if pre_correction and entry.get("expect", {}).get("artifact") == "html":
            band["known_suspect"] = True
            band["caveat"] = PRE_CORRECTION_CAVEAT
        if entry.get("band") != band:
            entry["band"] = band
            changed += 1

    suspect = sum(
        1
        for entry in prompts
        if isinstance(entry.get("band"), dict) and entry["band"].get("known_suspect")
    )
    print()
    print(f"{len(tally)} item(s) banded; {changed} band record(s) would change.")
    print("Resulting distribution:", dict(distribution))
    if suspect:
        print(f"{suspect} band(s) marked known-suspect: HTML items graded before the")
        print("execution check was corrected. Re-band them with --live before use.")
    discriminating = distribution.get("discriminating", 0)
    banded = sum(v for k, v in distribution.items() if k in corpus_mod.BANDS)
    if banded:
        print(f"Discriminating share of banded items: {discriminating / banded:.0%}")
        if discriminating / banded < 0.5:
            print("WARNING: most banded items are at the floor or the ceiling, where a paired")
            print("         test gets no information. The corpus is large but not powerful.")

    if not write:
        print()
        print("Nothing written. Re-run with --write to record these bands.")
        return 0

    PROMPTS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {PROMPTS_FILE}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Band the eval corpus by difficulty.")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-results", action="store_true",
                        help="estimate bands from the committed eval runs (no inference)")
    source.add_argument("--live", action="store_true",
                        help="run the unbanded items against the cheapest arm (needs Ollama)")
    ap.add_argument("--trials", type=int, default=5, help="runs per item for --live")
    ap.add_argument("--write", action="store_true", help="record the bands in evals/prompts.json")
    args = ap.parse_args()

    items = corpus_mod.load_corpus()
    print(f"Corpus: {len(items)} items, "
          f"{sum(1 for i in items if i.band is None)} currently unbanded.")

    if args.from_results:
        tally = bands_from_results()
        if not tally:
            print("ERROR: no committed eval runs to band from.")
            return 1
        known = {i.id for i in items}
        tally = {k: v for k, v in tally.items() if k in known}
        if not tally:
            print("ERROR: no committed run covers any item currently in the corpus.")
            return 1
        return apply(
            tally,
            PROVISIONAL_SOURCE,
            args.write,
            grader=LEGACY_GRADER,
            pre_correction=True,
        )

    unbanded = [i for i in items if i.band is None]
    if not unbanded:
        print("Every item is already banded. Nothing to do.")
        return 0
    if args.trials < 3:
        print("ERROR: fewer than three trials cannot place an item in a band.")
        return 1
    print(f"Banding {len(unbanded)} item(s) at {args.trials} trials each. "
          f"This runs the model and will take hours.")
    tally = asyncio.run(bands_live(unbanded, args.trials))
    import grading

    return apply(
        tally,
        f"live banding run, {args.trials} trials of a single complete candidate",
        args.write,
        grader=f"grading.py v{grading.GRADER_VERSION}",
    )


if __name__ == "__main__":
    raise SystemExit(main())
