"""Durable, non-monetary contribution-point ledger.

Contribution points describe work donated to an orchestrator. They are not a
currency, payment, or claim that a candidate was selected or proved correct.
The authoritative trusted-alpha records live in SQLite so concurrent in-process
writers cannot lose one another's append. ``ledger.json`` remains an atomic,
read-only compatibility projection for older tooling and pages.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from sqlite_store import connect as sqlite_connect
from sqlite_store import migration_lock


LEDGER_FILE = Path("ledger.json")
LEDGER_DB_FILE = Path("events.db")

# Retained for compatibility with older tests/callers that reset these names.
# SQLite, not this cache, is authoritative now.
_cache: list[dict] | None = None
_cache_mtime: float = 0.0
_ledger_lock = threading.RLock()

_SAFE_TASK_LABELS = frozenset(
    {
        "pipeline_submission",
        "pipeline_subtask",
        "pipeline_review",
        "compute_contribution",
        "contribution_record",
    }
)


def _safe_task_label(contribution_type: str, requested: str = "") -> str:
    """Reduce free-form contribution metadata to a fixed privacy-safe label."""

    if requested in _SAFE_TASK_LABELS:
        return requested
    if contribution_type == "pitch":
        return "pipeline_submission"
    if contribution_type == "compute":
        return "compute_contribution"
    return "contribution_record"


def ensure_contribution_schema(con: sqlite3.Connection) -> None:
    """Create the additive contribution schema on an existing connection."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS contributions (
            contribution_id   TEXT PRIMARY KEY,
            contributor       TEXT NOT NULL,
            contribution_type TEXT NOT NULL,
            points             REAL NOT NULL,
            task               TEXT NOT NULL,
            details            TEXT NOT NULL,
            basis              TEXT NOT NULL,
            points_are_monetary INTEGER NOT NULL DEFAULT 0 CHECK(points_are_monetary = 0),
            attempt_id         TEXT UNIQUE,
            created_at         REAL NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_contributions_contributor_created "
        "ON contributions(contributor, created_at)"
    )


def insert_contribution_in_transaction(
    con: sqlite3.Connection,
    *,
    contribution_id: str,
    contributor: str,
    contribution_type: str,
    points: float,
    task: str = "",
    details: str = "",
    basis: str,
    attempt_id: str | None = None,
    created_at: float | None = None,
) -> bool:
    """Insert once using the caller's transaction.

    ``attempt_id`` is unique so an accepted worker attempt can never earn the
    same compute-contribution points twice, including after coordinator restart.
    """
    ensure_contribution_schema(con)
    safe_task = _safe_task_label(contribution_type, task)
    cursor = con.execute(
        """
        INSERT OR IGNORE INTO contributions (
            contribution_id, contributor, contribution_type, points, task,
            details, basis, points_are_monetary, attempt_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            contribution_id,
            contributor,
            contribution_type,
            float(points),
            safe_task,
            "",
            basis,
            attempt_id,
            created_at if created_at is not None else time.time(),
        ),
    )
    return cursor.rowcount == 1


def redact_contribution_text_in_transaction(con: sqlite3.Connection) -> None:
    """Idempotently remove historical free-form task and details text."""

    ensure_contribution_schema(con)
    labels = tuple(sorted(_SAFE_TASK_LABELS))
    placeholders = ", ".join("?" for _ in labels)
    con.execute(
        f"""
        UPDATE contributions
        SET task = CASE
                WHEN task IN ({placeholders}) THEN task
                WHEN contribution_type = 'pitch' THEN 'pipeline_submission'
                WHEN contribution_type = 'compute' THEN 'compute_contribution'
                ELSE 'contribution_record'
            END,
            details = ''
        WHERE details <> '' OR task NOT IN ({placeholders})
        """,
        (*labels, *labels),
    )


def _legacy_id(entry: dict[str, Any], index: int) -> str:
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{index}:{canonical}".encode()).hexdigest()
    return f"legacy:{digest}"


def _read_legacy_file() -> list[dict]:
    if not LEDGER_FILE.exists():
        return []
    try:
        value = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return value if isinstance(value, list) else []


def _import_legacy_entries(con: sqlite3.Connection) -> None:
    for index, entry in enumerate(_read_legacy_file()):
        if not isinstance(entry, dict) or not entry.get("contributor"):
            continue
        contribution_type = str(entry.get("type") or "legacy")
        basis = str(
            entry.get("contribution_basis")
            or ("compute_contribution" if contribution_type == "compute" else contribution_type)
        )
        insert_contribution_in_transaction(
            con,
            contribution_id=str(entry.get("contribution_id") or _legacy_id(entry, index)),
            contributor=str(entry["contributor"]),
            contribution_type=contribution_type,
            points=float(entry.get("credits", entry.get("points", 0)) or 0),
            task=str(entry.get("task") or ""),
            details=str(entry.get("details") or ""),
            basis=basis,
            attempt_id=entry.get("attempt_id"),
            created_at=float(entry.get("timestamp", time.time())),
        )


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = LEDGER_DB_FILE if db_path is None else Path(db_path)
    con = sqlite_connect(path, row_factory=sqlite3.Row)
    try:
        with migration_lock(path):
            ensure_contribution_schema(con)
            _import_legacy_entries(con)
            redact_contribution_text_in_transaction(con)
            con.commit()
        return con
    except Exception:
        con.close()
        raise


def _entry_from_row(row: sqlite3.Row) -> dict:
    entry = {
        "contribution_id": row["contribution_id"],
        "contributor": row["contributor"],
        "type": row["contribution_type"],
        # ``credits`` is a compatibility name. New code should use ``points``.
        "credits": row["points"],
        "points": row["points"],
        "task": row["task"],
        "details": row["details"],
        "timestamp": row["created_at"],
        "contribution_basis": row["basis"],
        "points_are_monetary": False,
    }
    if row["attempt_id"]:
        entry["attempt_id"] = row["attempt_id"]
    return entry


def _query_entries(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        "SELECT * FROM contributions ORDER BY created_at, contribution_id"
    ).fetchall()
    return [_entry_from_row(row) for row in rows]


def _save(entries: list[dict]) -> None:
    """Atomically update the legacy JSON projection."""
    global _cache, _cache_mtime
    parent = LEDGER_FILE.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ledger-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, LEDGER_FILE)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
    _cache = entries
    try:
        _cache_mtime = LEDGER_FILE.stat().st_mtime
    except OSError:
        _cache_mtime = 0.0


def sync_compatibility_ledger(*, db_path: str | Path | None = None) -> None:
    """Refresh ``ledger.json`` from the authoritative SQLite records."""
    with _ledger_lock:
        with _connect(db_path) as con:
            entries = _query_entries(con)
        _save(entries)


def _load() -> list[dict]:
    """Load the authoritative contribution history."""
    global _cache
    with _ledger_lock:
        with _connect() as con:
            entries = _query_entries(con)
        _cache = entries
        return entries


def log_contribution(
    contributor_id: str,
    contribution_type: str,
    credits: float = 0,
    task: str = "",
    details: str = "",
    *,
    contribution_id: str | None = None,
    basis: str | None = None,
    attempt_id: str | None = None,
) -> None:
    """Record non-monetary contribution points exactly once.

    ``compute`` means compute was contributed. It does not mean that the
    candidate was accepted, selected, or behaviorally validated.
    """
    contribution_id = contribution_id or f"contribution:{uuid.uuid4().hex}"
    basis = basis or (
        "compute_contribution" if contribution_type == "compute" else contribution_type
    )
    with _ledger_lock:
        with _connect() as con:
            insert_contribution_in_transaction(
                con,
                contribution_id=contribution_id,
                contributor=contributor_id,
                contribution_type=contribution_type,
                points=credits,
                task=task,
                details=details,
                basis=basis,
                attempt_id=attempt_id,
            )
            con.commit()
            entries = _query_entries(con)
        _save(entries)


def get_standings() -> list[dict]:
    """Aggregate non-monetary contribution points by contributor."""
    entries = _load()
    contributors: dict[str, dict] = {}
    for entry in entries:
        contributor_id = entry["contributor"]
        contributor = contributors.setdefault(
            contributor_id,
            {
                "contributor": contributor_id,
                "total_credits": 0,
                "total_points": 0,
                "contributions": 0,
                "compute_tasks": 0,
                "pitches": 0,
                "points_are_monetary": False,
            },
        )
        points = entry.get("points", entry.get("credits", 0))
        contributor["total_credits"] += points
        contributor["total_points"] += points
        contributor["contributions"] += 1
        if entry["type"] == "compute":
            contributor["compute_tasks"] += 1
        elif entry["type"] == "pitch":
            contributor["pitches"] += 1
    return sorted(
        contributors.values(),
        key=lambda value: value["total_points"],
        reverse=True,
    )


def get_history(contributor_id: str | None = None, limit: int = 50) -> list[dict]:
    """Get recent contribution records, optionally filtered by contributor."""
    entries = _load()
    if contributor_id:
        entries = [entry for entry in entries if entry["contributor"] == contributor_id]
    return entries[-limit:]
