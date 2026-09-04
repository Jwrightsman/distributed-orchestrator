"""Append-only records of what was actually run, and under what.

An eval number is only as good as the record of the conditions that produced
it. This project has twice compared two numbers that turned out to have been
produced by different checkers, and once by different prompt sets, and in both
cases nothing in the stored result said so.

So every run records the facts that would let a later reader tell whether two
results are comparable: the corpus version and digest, the item and its band,
the arm and its strategy configuration, the model's provider, name and digest,
the descriptor hash, temperature and seed, wall-clock and token cost, the
artifact's hash, the grading method, the grader version and the outcome, and a
timestamp.

**Absent facts are recorded as unknown, never inferred.** That convention comes
from the provenance envelope (`provenance.py`, ADR 0017) and it matters here for
the same reason: a run that could not determine the model digest and a run
whose model digest happened to match are different situations, and only one of
them supports a comparison.

**Reuse the envelope where there is one.** A run dispatched through the normal
execution path already has a provenance envelope binding the artifacts to the
identity that produced them; that envelope's digest goes in
`provenance_envelope_digest` and the record does not duplicate its contents. A
run executed locally through `run_pipeline` has no envelope, and says so.

**Append-only.** A re-run never overwrites an earlier one. Comparing an old
record with a new one is how you detect that something moved underneath you —
which is the entire reason ROADMAP section 2 says to publish the number that
makes us look worse.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

RUNS_FILENAME = "runs.jsonl"

UNKNOWN_MODEL_DIGEST = "model_digest"
UNKNOWN_DESCRIPTOR_HASH = "descriptor_hash"
UNKNOWN_TOKENS = "token_cost"
UNKNOWN_PROVENANCE = "provenance_envelope"
UNKNOWN_TEMPERATURE = "model_temperature"
UNKNOWN_SEED = "model_seed"


@dataclass(frozen=True)
class ModelIdentity:
    provider: str
    name: str
    digest: str | None = None
    temperature: float | None = None
    seed: int | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "name": self.name,
            "digest": self.digest,
            "temperature": self.temperature,
            "seed": self.seed,
        }


@dataclass
class RunRecord:
    """One item, run once, under one arm."""

    study_id: str
    run_id: str
    item_id: str
    arm: str
    strategy: str
    strategy_config: dict[str, Any]
    corpus_version: str
    corpus_digest: str
    band: str | None
    model: ModelIdentity
    descriptor_hash: str | None
    wall_clock_seconds: float | None
    tokens: dict[str, int] | None
    artifact_sha256: str | None
    artifact_paths: list[str]
    grading: dict[str, Any]
    graded: bool
    passed: bool
    replicate: int = 0
    provenance_envelope_digest: str | None = None
    judge_score: int | None = None
    unknown_facts: list[str] = field(default_factory=list)
    recorded_at: str = ""
    notes: str = ""

    def as_record(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["model"] = self.model.as_record()
        payload["recorded_at"] = self.recorded_at or datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        payload["unknown_facts"] = sorted(set(self.unknown_facts) | set(self._missing()))
        # Said in the record itself rather than only in the docs, because a
        # record travels further than its documentation.
        payload["judge_score_status"] = (
            "exploratory — never a primary endpoint, gates nothing"
        )
        return payload

    def _missing(self) -> list[str]:
        """Facts this run could not establish.

        `model_temperature` and `model_seed` are on this list far more often
        than they should be: `config.json` has no setting for either, so every
        local run today takes whatever Ollama defaults to and records that it
        does not know. That is a real gap in what a study can pin, and it is
        surfaced here rather than left to be discovered when two arms turn out
        to have run at different temperatures.
        """
        missing = []
        if not self.model.digest:
            missing.append(UNKNOWN_MODEL_DIGEST)
        if self.model.temperature is None:
            missing.append(UNKNOWN_TEMPERATURE)
        if self.model.seed is None:
            missing.append(UNKNOWN_SEED)
        if not self.descriptor_hash:
            missing.append(UNKNOWN_DESCRIPTOR_HASH)
        if not self.tokens:
            missing.append(UNKNOWN_TOKENS)
        if not self.provenance_envelope_digest:
            missing.append(UNKNOWN_PROVENANCE)
        return missing


def artifact_digest(paths: Sequence[str]) -> str | None:
    """SHA-256 over the artifact's files, name and content, in sorted order.

    None when there are no files — which is itself a result and is recorded as
    a failing grade, not as a missing run.
    """
    existing = sorted(p for p in paths if Path(p).is_file())
    if not existing:
        return None
    digest = hashlib.sha256()
    for path in existing:
        digest.update(Path(path).name.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(Path(path).read_bytes())
        digest.update(b"\x1e")
    return digest.hexdigest()


def append_run(study_dir: Path, record: RunRecord) -> Path:
    """Append one record. Never rewrites, never deduplicates.

    Two records for the same (item, arm, replicate) are allowed on purpose: a
    re-run is evidence about the instrument's stability, and silently replacing
    the earlier one throws that evidence away.
    """
    study_dir = Path(study_dir)
    study_dir.mkdir(parents=True, exist_ok=True)
    path = study_dir / RUNS_FILENAME
    line = json.dumps(record.as_record(), sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return path


def load_runs(study_dir: Path) -> list[dict[str, Any]]:
    """Every record in a study, in the order it was written.

    Raises when the file is missing or empty. "No runs" is never quietly
    treated as "a study with no findings".
    """
    path = Path(study_dir) / RUNS_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"no run log at {path}")
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} line {number} is not valid JSON: {exc}") from exc
    if not records:
        raise ValueError(f"{path} holds no runs")
    return records


def latest_per_key(records: Iterable[dict[str, Any]]) -> dict[tuple[str, str, int], dict]:
    """The most recent record for each (item, arm, replicate).

    Used for reporting. The earlier records stay on disk; this only chooses
    which one a summary speaks for, and `superseded_keys` says how many were
    passed over so a re-run is never invisible.
    """
    latest: dict[tuple[str, str, int], dict] = {}
    for record in records:
        key = (record["item_id"], record["arm"], int(record.get("replicate", 0)))
        latest[key] = record
    return latest


def superseded_count(records: Iterable[dict[str, Any]]) -> int:
    records = list(records)
    return len(records) - len(latest_per_key(records))


async def capture_model_identity(provider: str = "ollama") -> ModelIdentity:
    """Best-effort model identity, recording what it could not learn.

    A digest is asked for, not assumed. Ollama reports one per tag; when the
    daemon is not reachable the digest is None and the record says
    `model_digest` is unknown rather than pretending the name is enough.
    A model update mid-study invalidates the study, and the digest is what
    makes that detectable.
    """
    import config

    settings = config.get()
    name = str(settings.get("model", "?"))
    temperature = settings.get("temperature")
    digest = None
    try:
        import httpx

        base = str(settings.get("ollama_url") or "http://localhost:11434").rstrip("/")
        async with httpx.AsyncClient(timeout=5) as client:
            payload = (await client.get(f"{base}/api/tags")).json()
        for entry in payload.get("models", []):
            if entry.get("name") == name or entry.get("model") == name:
                digest = entry.get("digest")
                break
    except Exception:
        digest = None
    return ModelIdentity(
        provider=provider,
        name=name,
        digest=digest,
        temperature=float(temperature) if temperature is not None else None,
        seed=settings.get("seed"),
    )
