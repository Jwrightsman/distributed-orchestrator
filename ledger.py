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
from dataclasses import dataclass
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


# The accounting policy, named and owned by the module that owns accounting.
# It used to be a bare literal inside the settlement transaction, which put a
# policy decision inside an integrity boundary and made "what is a point worth"
# a question you answered by reading AttemptStore.settle.
#
# The policy itself is unchanged and deliberately dull: an accepted, attempt-bound
# worker result with non-empty output and no worker error is worth
# COMPUTE_CONTRIBUTION_POINTS. It is not money, not a token, not a claim that the
# candidate was selected, and not a claim that the output is correct.
COMPUTE_CONTRIBUTION_POINTS = 5

# ── Tamper-evident chain ─────────────────────────────────────────────
#
# Each entry carries the digest of the one before it, so an edit to any entry
# breaks every link after it and verification reports where.
#
# This makes the ledger **tamper-evident, not tamper-proof.** An operator with
# database access can rewrite every entry *and* every link, and this mechanism
# will report a clean chain. There is no consensus here, no external anchor, and
# no third party attesting to anything. What it detects is accidental
# corruption, a partial edit, and casual modification - which is worth having,
# and is not the same thing as verifiable compute. See ADR 0017.
#
# A linear chain rather than a Merkle tree: a tree buys efficient inclusion
# proofs, which matter only when proving membership to a third party without
# handing over the whole log. Nobody needs that yet.
LEDGER_CHAIN_VERSION = "1"
_CHAIN_DOMAIN = "mycelium.ledger-chain.v1"

# The chain's fixed starting point. Entries written before the chain existed have
# no link and are not retrofitted with one; see `genesis_unchained_entries`.
LEDGER_CHAIN_GENESIS_DIGEST = hashlib.sha256(
    f"{_CHAIN_DOMAIN}:genesis".encode("ascii")
).hexdigest()

# The columns the digest covers. Anything not listed here can change without
# breaking the chain, which is why the list is explicit rather than "every
# column": a future additive column must be a deliberate decision, not a silent
# invalidation of every existing link.
_CHAINED_FIELDS = (
    "contribution_id",
    "contributor",
    "contribution_type",
    "points",
    "task",
    "details",
    "basis",
    "points_are_monetary",
    "attempt_id",
    "enrollment_id",
    "node_id",
    "session_id",
    "created_at",
)


def chain_entry_digest(
    *, entry_index: int, previous_digest: str, content: dict[str, Any]
) -> str:
    """The digest of one ledger entry, bound to its position and predecessor."""

    material = json.dumps(
        {
            "content": {key: content.get(key) for key in _CHAINED_FIELDS},
            "entry_index": int(entry_index),
            "previous_digest": previous_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(
        _CHAIN_DOMAIN.encode("ascii") + b"\0" + material.encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class LedgerChainVerification:
    """What a chain walk found. Content-free by construction."""

    ok: bool
    chained_entries: int
    genesis_unchained_entries: int
    break_at_index: int | None = None
    break_entry_id: str | None = None
    expected_digest: str | None = None
    observed_digest: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "chained_entries": self.chained_entries,
            "genesis_unchained_entries": self.genesis_unchained_entries,
            "break_at_index": self.break_at_index,
            "break_entry_id": self.break_entry_id,
            "expected_digest": self.expected_digest,
            "observed_digest": self.observed_digest,
            "reason": self.reason,
            "establishes": (
                "that no entry was changed without also recomputing every link "
                "after it. It is tamper evidence, not tamper proofing, and it is "
                "not proof that any recorded work happened or was correct."
            ),
        }


def _chain_head(con: sqlite3.Connection) -> tuple[int, str]:
    """The current chain tip, or the genesis boundary if there is none."""

    row = con.execute(
        "SELECT entry_index, entry_digest FROM contributions "
        "WHERE entry_index IS NOT NULL ORDER BY entry_index DESC LIMIT 1"
    ).fetchone()
    if row is None or row[0] is None:
        return -1, LEDGER_CHAIN_GENESIS_DIGEST
    return int(row[0]), str(row[1])


def verify_ledger_chain(db_path: str | Path | None = None) -> LedgerChainVerification:
    """Walk the chain and report the first break, with enough to act on.

    Reports an index, an entry ID, and two digests. No secrets, prompts, outputs,
    or artifact contents can appear here, because none of them are in the chained
    columns.
    """

    path = LEDGER_DB_FILE if db_path is None else Path(db_path)
    with sqlite_connect(path, row_factory=sqlite3.Row) as con:
        ensure_contribution_schema(con)
        unchained = int(
            con.execute(
                "SELECT COUNT(*) AS n FROM contributions WHERE entry_index IS NULL"
            ).fetchone()["n"]
        )
        rows = con.execute(
            "SELECT * FROM contributions WHERE entry_index IS NOT NULL "
            "ORDER BY entry_index"
        ).fetchall()

    previous = LEDGER_CHAIN_GENESIS_DIGEST
    for position, row in enumerate(rows):
        entry_index = int(row["entry_index"])
        entry_id = str(row["contribution_id"])
        if entry_index != position:
            return LedgerChainVerification(
                ok=False,
                chained_entries=len(rows),
                genesis_unchained_entries=unchained,
                break_at_index=position,
                break_entry_id=entry_id,
                expected_digest=None,
                observed_digest=None,
                reason=(
                    f"entry index is {entry_index} where {position} was expected; "
                    "the chain has a gap or a duplicate"
                ),
            )
        stored_previous = str(row["previous_digest"] or "")
        if stored_previous != previous:
            return LedgerChainVerification(
                ok=False,
                chained_entries=len(rows),
                genesis_unchained_entries=unchained,
                break_at_index=entry_index,
                break_entry_id=entry_id,
                expected_digest=previous,
                observed_digest=stored_previous or None,
                reason="entry does not link to the previous entry's digest",
            )
        recomputed = chain_entry_digest(
            entry_index=entry_index,
            previous_digest=previous,
            content={key: row[key] for key in _CHAINED_FIELDS},
        )
        stored_digest = str(row["entry_digest"] or "")
        if recomputed != stored_digest:
            return LedgerChainVerification(
                ok=False,
                chained_entries=len(rows),
                genesis_unchained_entries=unchained,
                break_at_index=entry_index,
                break_entry_id=entry_id,
                expected_digest=recomputed,
                observed_digest=stored_digest or None,
                reason="entry content does not match its recorded digest",
            )
        previous = stored_digest
    return LedgerChainVerification(
        ok=True,
        chained_entries=len(rows),
        genesis_unchained_entries=unchained,
    )



def compute_contribution_points(*, output: str | None, error: str | None) -> int:
    """Points for one accepted compute contribution.

    A pure function of the settled result. It reads no evidence, no history, and
    no reputation, because none of those may influence what work is worth.
    """

    return COMPUTE_CONTRIBUTION_POINTS if output and not error else 0


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
            enrollment_id      TEXT,
            node_id            TEXT,
            session_id         TEXT,
            created_at         REAL NOT NULL
        )
        """
    )
    existing = {
        str(row[1]) for row in con.execute("PRAGMA table_info(contributions)").fetchall()
    }
    for name in ("enrollment_id", "node_id", "session_id"):
        if name not in existing:
            con.execute(f"ALTER TABLE contributions ADD COLUMN {name} TEXT")
    # Chain columns are additive and nullable. Rows written before the chain
    # existed keep NULL and are the genesis boundary; history is never rewritten
    # to fabricate links it did not have.
    if "entry_index" not in existing:
        con.execute("ALTER TABLE contributions ADD COLUMN entry_index INTEGER")
    if "previous_digest" not in existing:
        con.execute("ALTER TABLE contributions ADD COLUMN previous_digest TEXT")
    if "entry_digest" not in existing:
        con.execute("ALTER TABLE contributions ADD COLUMN entry_digest TEXT")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_contributions_entry_index "
        "ON contributions(entry_index) WHERE entry_index IS NOT NULL"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_contributions_contributor_created "
        "ON contributions(contributor, created_at)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_contributions_enrollment_created "
        "ON contributions(enrollment_id, created_at) "
        "WHERE enrollment_id IS NOT NULL"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_contributions_session_created "
        "ON contributions(session_id, created_at) "
        "WHERE enrollment_id IS NULL AND session_id IS NOT NULL"
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
    enrollment_id: str | None = None,
    node_id: str | None = None,
    session_id: str | None = None,
    created_at: float | None = None,
) -> bool:
    """Insert once using the caller's transaction.

    ``attempt_id`` is unique so an accepted worker attempt can never earn the
    same compute-contribution points twice, including after coordinator restart.
    """
    ensure_contribution_schema(con)
    safe_task = _safe_task_label(contribution_type, task)
    moment = created_at if created_at is not None else time.time()
    content = {
        "contribution_id": contribution_id,
        "contributor": contributor,
        "contribution_type": contribution_type,
        "points": float(points),
        "task": safe_task,
        "details": "",
        "basis": basis,
        "points_are_monetary": 0,
        "attempt_id": attempt_id,
        "enrollment_id": enrollment_id,
        "node_id": node_id,
        "session_id": session_id,
        "created_at": moment,
    }
    # The link is computed and written inside the caller's transaction, which for
    # a settlement is the same BEGIN IMMEDIATE that writes the receipt. Nothing
    # about settlement atomicity is relaxed to fit the chain in: the chain rides
    # the transaction that already exists.
    head_index, head_digest = _chain_head(con)
    entry_index = head_index + 1
    entry_digest = chain_entry_digest(
        entry_index=entry_index, previous_digest=head_digest, content=content
    )
    cursor = con.execute(
        """
        INSERT OR IGNORE INTO contributions (
            contribution_id, contributor, contribution_type, points, task,
            details, basis, points_are_monetary, attempt_id, enrollment_id,
            node_id, session_id, created_at, entry_index, previous_digest,
            entry_digest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
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
            enrollment_id,
            node_id,
            session_id,
            moment,
            entry_index,
            head_digest,
            entry_digest,
        ),
    )
    # An ignored insert consumed no index: a replayed settlement or an ambiguous
    # commit that retries resolves to the one existing entry, never a second one
    # and never a gap.
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
            enrollment_id=entry.get("enrollment_id"),
            node_id=entry.get("node_id"),
            session_id=entry.get("session_id"),
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
        # These fields are deliberately present even for historical rows. Null
        # means attribution was never recorded; it must not be inferred later
        # from a reusable display label.
        "enrollment_id": row["enrollment_id"],
        "node_id": row["node_id"],
        "session_id": row["session_id"],
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
    enrollment_id: str | None = None,
    node_id: str | None = None,
    session_id: str | None = None,
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
                enrollment_id=enrollment_id,
                node_id=node_id,
                session_id=session_id,
            )
            con.commit()
            entries = _query_entries(con)
        _save(entries)


def get_standings() -> list[dict]:
    """Aggregate points by durable enrollment or explicit legacy incarnation.

    Historical rows predate both fields and remain grouped by their old
    contributor label. Newly accepted legacy-session work is scoped to its
    process incarnation, so a later claimant of the same label cannot inherit
    it. Enrolled work is always grouped by immutable ``enrollment_id``.
    """
    entries = _load()
    contributors: dict[str, dict] = {}
    for entry in entries:
        enrollment_id = entry.get("enrollment_id")
        session_id = entry.get("session_id")
        if enrollment_id:
            contributor_id = f"enrollment:{enrollment_id}"
            attribution = "enrollment"
        elif session_id:
            contributor_id = f"legacy-session:{session_id}"
            attribution = "legacy_session"
        else:
            contributor_id = f"historical-node:{entry['contributor']}"
            attribution = "historical_node"
        contributor = contributors.setdefault(
            contributor_id,
            {
                "contributor": entry["contributor"],
                "enrollment_id": enrollment_id,
                "node_id": entry.get("node_id"),
                "session_id": session_id if not enrollment_id else None,
                "attribution": attribution,
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
