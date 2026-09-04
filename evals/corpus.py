"""The prompt corpus: schema, digests, difficulty bands, and the locked split.

`evals/prompts.json` used to be a flat list of tasks. It now carries four extra
things per item, and each one exists to stop a specific way of fooling
yourself:

**`taxonomy`** — which category of work Mycelium claims to do, from the list in
`docs/eval-methodology.md`. Items are written *from* the taxonomy. A corpus
grown by adding prompts that resemble things the system currently fails
measures whether those particular failures were fixed, not capability.

**`origin`** — `taxonomy` or `observed_failure`. An item that did come from a
failure log is marked, and `confirmatory_items()` refuses to return it. The
28 original items are all marked `legacy` because they predate the
distinction and have been iterated against for four prompt-set versions; they
are development-set material regardless of how they were written.

**`band`** — `floor` (roughly 0-20% pass), `discriminating` (20-80%), or
`ceiling` (80-100%), from running the item k times. Power in a paired design
comes only from items where the arms can differ: an item every arm passes and
an item every arm fails both contribute exactly nothing to McNemar's test.
Banding is therefore a power intervention, not bookkeeping. `band` is null
until an item has been run; `evals/stats.py` explains what that costs.

**`split`** — `development` or `confirmatory`. The confirmatory assignment is
frozen in `evals/split.lock.json` with a digest, and
`tests/test_eval_corpus.py` fails if it changes. That is the whole point: a
held-out set you can redraw is not held out, and this project has already
kept one prompt set alive on a subgroup that looked good after the fact.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

EVALS_DIR = Path(__file__).resolve().parent
PROMPTS_FILE = EVALS_DIR / "prompts.json"
SPLIT_LOCK_FILE = EVALS_DIR / "split.lock.json"

BANDS = ("floor", "discriminating", "ceiling")
SPLITS = ("development", "confirmatory")
ORIGINS = ("taxonomy", "observed_failure", "legacy")

# The categories of work this project claims, from ROADMAP section 4's
# narrowed workload claim plus the shapes the original corpus already covered.
# New items are written from this list; the list is not written from failures.
TAXONOMY = (
    "structured_extraction",
    "data_transformation",
    "test_generation",
    "static_analysis",
    "synthetic_data",
    "batch_classification",
    "interactive_artifact",
    "service_endpoint",
    "algorithmic_kernel",
    "underspecified_request",
)

# Band edges. Stated as constants because a band that moves is a band that can
# be moved to suit a result.
FLOOR_MAX = 0.20
CEILING_MIN = 0.80


class CorpusError(ValueError):
    """Raised for anything structurally wrong with the corpus on disk."""


@dataclass(frozen=True)
class CorpusItem:
    id: str
    category: str
    taxonomy: str
    task: str
    origin: str
    split: str
    expect: dict[str, Any]
    band: dict[str, Any] | None = None

    @property
    def band_label(self) -> str | None:
        return None if self.band is None else str(self.band.get("label"))

    @property
    def artifact(self) -> str:
        return str(self.expect.get("artifact", "any"))

    @property
    def checks(self) -> list[dict[str, Any]]:
        return list(self.expect.get("checks", []))


def classify_band(passes: int, trials: int) -> str:
    """floor / discriminating / ceiling from a pass count.

    Reproducible for a fixed set of outcomes, which a test asserts — the band
    is an input to which items carry the study, so it cannot depend on when it
    was computed or on how the caller feels about the item.
    """
    if trials <= 0:
        raise CorpusError("cannot band an item that was never run")
    if not 0 <= passes <= trials:
        raise CorpusError(f"impossible band evidence: {passes} passes in {trials} trials")
    rate = passes / trials
    if rate <= FLOOR_MAX:
        return "floor"
    if rate >= CEILING_MIN:
        return "ceiling"
    return "discriminating"


def _digest_ids(ids: Iterable[str]) -> str:
    """SHA-256 over the sorted id list, newline separated.

    Order-independent on purpose: reordering `prompts.json` is a cosmetic edit
    and should not read as tampering, whereas adding, removing or renaming a
    confirmatory item is exactly what the digest is there to catch.
    """
    blob = "\n".join(sorted(ids)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def corpus_digest(items: Iterable[CorpusItem]) -> str:
    """Digest over every item's id *and* task text.

    Broader than the split digest: this one changes when a prompt is reworded,
    because a reworded prompt is a different measurement even under the same
    id. Recorded on every run so two results can be checked for having graded
    the same thing.
    """
    payload = "\n".join(f"{item.id}\x1f{item.task}" for item in sorted(items, key=lambda i: i.id))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate(item: dict[str, Any], seen: set[str]) -> CorpusItem:
    for field in ("id", "category", "taxonomy", "task", "origin", "split", "expect"):
        if field not in item:
            raise CorpusError(f"item {item.get('id', '?')} is missing {field!r}")
    item_id = str(item["id"])
    if item_id in seen:
        raise CorpusError(f"duplicate item id {item_id!r}")
    seen.add(item_id)
    if item["taxonomy"] not in TAXONOMY:
        raise CorpusError(f"{item_id}: unknown taxonomy {item['taxonomy']!r}")
    if item["split"] not in SPLITS:
        raise CorpusError(f"{item_id}: unknown split {item['split']!r}")
    if item["origin"] not in ORIGINS:
        raise CorpusError(f"{item_id}: unknown origin {item['origin']!r}")
    if not str(item["task"]).strip():
        raise CorpusError(f"{item_id}: empty task")
    expect = item["expect"]
    if expect.get("artifact", "any") not in ("python", "html", "any"):
        raise CorpusError(f"{item_id}: unknown expected artifact {expect.get('artifact')!r}")
    band = item.get("band")
    if band is not None:
        label = classify_band(int(band["passes"]), int(band["trials"]))
        if label != band.get("label"):
            raise CorpusError(
                f"{item_id}: band label {band.get('label')!r} does not follow from "
                f"{band['passes']}/{band['trials']} (should be {label!r})"
            )
    # An item written from a failure log can never carry a confirmatory result.
    if item["origin"] == "observed_failure" and item["split"] == "confirmatory":
        raise CorpusError(
            f"{item_id}: items derived from an observed failure are development-only"
        )
    if item["origin"] == "legacy" and item["split"] == "confirmatory":
        raise CorpusError(
            f"{item_id}: the original 28 items have been iterated against and cannot "
            "be part of the confirmatory set"
        )
    return CorpusItem(
        id=item_id,
        category=str(item["category"]),
        taxonomy=str(item["taxonomy"]),
        task=str(item["task"]),
        origin=str(item["origin"]),
        split=str(item["split"]),
        expect=dict(expect),
        band=None if band is None else dict(band),
    )


def load_corpus(path: Path | None = None) -> list[CorpusItem]:
    """Read and validate the corpus. Never returns an empty list.

    The empty-corpus guard is here rather than in each caller because "the
    harness read nothing and reported success" is the failure this repository
    keeps producing: four test files have passed on zero inputs.
    """
    path = path or PROMPTS_FILE
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = data.get("prompts")
    if not isinstance(raw, list) or not raw:
        raise CorpusError(f"{path} holds no prompts")
    seen: set[str] = set()
    items = [_validate(entry, seen) for entry in raw]
    if not items:
        raise CorpusError(f"{path} produced no items after validation")
    return items


def corpus_version(path: Path | None = None) -> str:
    path = path or PROMPTS_FILE
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return str(data.get("version", "?"))


def development_items(items: Iterable[CorpusItem]) -> list[CorpusItem]:
    return [i for i in items if i.split == "development"]


def confirmatory_items(items: Iterable[CorpusItem]) -> list[CorpusItem]:
    """The locked set, refusing anything traceable to an observed failure."""
    chosen = [i for i in items if i.split == "confirmatory"]
    tainted = [i.id for i in chosen if i.origin != "taxonomy"]
    if tainted:
        raise CorpusError(
            "confirmatory set contains items not written from the taxonomy: "
            + ", ".join(sorted(tainted))
        )
    if not chosen:
        raise CorpusError("confirmatory set is empty")
    return chosen


def band_distribution(items: Iterable[CorpusItem]) -> dict[str, int]:
    """Counts per band, with unbanded items counted rather than dropped."""
    counts = {band: 0 for band in BANDS}
    counts["unbanded"] = 0
    for item in items:
        label = item.band_label
        counts[label if label in counts else "unbanded"] += 1
    return counts


def read_split_lock(path: Path | None = None) -> dict[str, Any]:
    return json.loads(Path(path or SPLIT_LOCK_FILE).read_text(encoding="utf-8"))


def check_split_lock(
    items: Iterable[CorpusItem], lock: dict[str, Any] | None = None
) -> list[str]:
    """Return the reasons the corpus no longer matches its committed split.

    Empty list means the lock holds. A list of reasons is a hard failure, not
    a warning: a confirmatory set that drifted is a confirmatory set that can
    be redrawn to suit whatever came out.
    """
    lock = lock or read_split_lock()
    items = list(items)
    if not items:
        return ["corpus is empty — nothing to check the lock against"]

    problems: list[str] = []
    locked_ids = set(lock.get("confirmatory_ids", []))
    if not locked_ids:
        return ["split lock records no confirmatory ids"]

    current_ids = {i.id for i in items if i.split == "confirmatory"}
    added = sorted(current_ids - locked_ids)
    removed = sorted(locked_ids - current_ids)
    if added:
        problems.append(f"items added to the confirmatory set since it was locked: {added}")
    if removed:
        problems.append(f"items removed from the confirmatory set since it was locked: {removed}")

    digest = _digest_ids(current_ids)
    if digest != lock.get("confirmatory_digest"):
        problems.append(
            f"confirmatory digest is {digest}, lock records {lock.get('confirmatory_digest')}"
        )

    missing = sorted(locked_ids - {i.id for i in items})
    if missing:
        problems.append(f"locked confirmatory items no longer in the corpus: {missing}")
    return problems


def build_split_lock(items: Iterable[CorpusItem]) -> dict[str, Any]:
    """The lock file's contents for the corpus as it currently stands.

    Used to write the lock once, and by the test to recompute what the lock
    should say. There is deliberately no CLI that rewrites it: relocking is an
    edit somebody has to make and defend in a diff.
    """
    items = list(items)
    confirmatory = sorted(i.id for i in items if i.split == "confirmatory")
    return {
        "confirmatory_ids": confirmatory,
        "confirmatory_digest": _digest_ids(confirmatory),
        "corpus_digest": corpus_digest(items),
        "item_count": len(items),
    }
