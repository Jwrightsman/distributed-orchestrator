"""Server-authoritative worker attempts and accepted-result receipts.

The worker queue is intentionally still process-local. Attempt credentials,
terminal attempt state, exact-replay responses, contribution settlement, and
accepted receipts are durable because those are integrity boundaries rather
than scheduling conveniences.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ledger import ensure_contribution_schema, insert_contribution_in_transaction
from node_capabilities import (
    canonical_requirement_binding,
    ensure_node_capability_snapshot_schema,
)
from node_enrollments import ensure_node_enrollment_schema
from sqlite_store import connect as sqlite_connect
from sqlite_store import connection, migration_lock


AttemptState = Literal[
    "active",
    "settled",
    "expired",
    "reclaimed",
    "cancelled",
    "superseded",
    "interrupted",
]

_TERMINAL_STATES = {
    "settled",
    "expired",
    "reclaimed",
    "cancelled",
    "superseded",
    "interrupted",
}
_MAX_QUARANTINE_ROWS = 500
_MAX_QUARANTINE_PREVIEW_BYTES = 4096
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 10_485_760
MAX_ERROR_BYTES = 2048
# The reference worker flushes roughly every 20 model tokens.  This ceiling is
# high enough for the maximum 10 MiB output at that cadence while still bounding a
# malicious attempt independently of its byte and event-rate limits.
MAX_STREAM_BATCHES = 250_000
MAX_STREAM_BATCHES_PER_WINDOW = 120
STREAM_RATE_WINDOW_SECONDS = 1.0


class AttemptConflict(RuntimeError):
    """The server tried to issue an attempt that violates durable uniqueness."""


class AttemptRejected(RuntimeError):
    """A submission cannot settle the authoritative attempt."""

    def __init__(self, reason: str, *, state: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.state = state


class WorkerPayloadLimitExceeded(AttemptRejected):
    """A worker-controlled payload exceeded its server-issued byte limit."""

    def __init__(self, field: str, *, limit: int, observed: int):
        self.field = field
        self.limit = int(limit)
        self.observed = int(observed)
        code = "output_limit_exceeded" if field == "output" else f"{field}_limit_exceeded"
        self.code = code
        super().__init__(
            f"{field} is {observed} UTF-8 bytes; server limit is {limit} bytes",
            state="cancelled",
        )


class ReceiptBindingError(RuntimeError):
    """A durable receipt does not belong to the dispatch wait consuming it."""


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    task_id: str
    execution_id: str | None
    execution_unit_id: str | None
    execution_unit_kind: str | None
    assigned_node_id: str
    assigned_enrollment_id: str | None
    assigned_credential_version: int | None
    assigned_session_id: str | None
    assigned_descriptor_version: str | None
    assigned_descriptor_hash: str | None
    requirement_version: str | None
    requirement_digest: str | None
    contract_version: str | None
    nonce_digest: str
    state: str
    issued_at: float
    lease_expires_at: float
    max_output_bytes: int
    streamed_bytes: int
    stream_batch_count: int
    first_stream_at: float | None
    last_stream_at: float | None
    stream_closed: int
    stream_limit_event_emitted: int
    stream_rate_window_started_at: float | None
    stream_rate_window_batch_count: int
    settled_at: float | None = None
    result_hash: str | None = None
    response_json: str | None = None
    reason: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AttemptRecord:
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})


@dataclass(frozen=True)
class AcceptedResultReceipt:
    attempt_id: str
    task_id: str
    execution_id: str | None
    execution_unit_id: str | None
    execution_unit_kind: str | None
    assigned_node_id: str
    assigned_enrollment_id: str | None
    assigned_descriptor_version: str | None
    assigned_descriptor_hash: str | None
    requirement_version: str | None
    requirement_digest: str | None
    contract_version: str | None
    result_hash: str
    accepted_at: float
    output: str | None
    error: str | None
    elapsed_seconds: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AcceptedResultReceipt:
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})

    def as_legacy_result(self, *, trace_id: str = "") -> dict[str, Any]:
        """Compatibility projection; never an authority for dispatch."""
        return {
            "task_id": self.task_id,
            "node_id": self.assigned_node_id,
            "enrollment_id": self.assigned_enrollment_id,
            "capability_descriptor_version": self.assigned_descriptor_version,
            "capability_descriptor_hash": self.assigned_descriptor_hash,
            "resource_requirement_version": self.requirement_version,
            "resource_requirement_digest": self.requirement_digest,
            "output": self.output,
            "error": self.error,
            "elapsed_seconds": self.elapsed_seconds,
            "completed_at": self.accepted_at,
            "trace_id": trace_id,
            "attempt_id": self.attempt_id,
            "contract_version": self.contract_version,
            "execution_id": self.execution_id,
            "execution_unit_id": self.execution_unit_id,
            "execution_unit_kind": self.execution_unit_kind,
            "result_hash": self.result_hash,
        }


@dataclass(frozen=True)
class SettlementOutcome:
    receipt: AcceptedResultReceipt
    response: dict[str, Any]
    replayed: bool


@dataclass(frozen=True)
class StreamBatchOutcome:
    accepted: bool
    attempt_id: str
    max_output_bytes: int
    streamed_bytes: int
    stream_batch_count: int
    error_code: str | None = None
    detail: str | None = None
    emit_limit_event: bool = False


def nonce_digest(nonce: str) -> str:
    """One-way digest for a high-entropy server nonce."""
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def _validate_versioned_digest(
    version: str | None,
    digest: str | None,
    *,
    label: str,
) -> tuple[str | None, str | None]:
    if (version is None) != (digest is None):
        raise ValueError(f"{label} version and digest must be supplied together")
    if version is None:
        return None, None
    normalized_version = str(version).strip()
    normalized_digest = str(digest).strip().lower()
    if (
        not normalized_version
        or len(normalized_version) > 16
        or any(
            ord(character) < 33 or ord(character) > 126
            for character in normalized_version
        )
    ):
        raise ValueError(f"{label} version must be 1-16 printable ASCII characters")
    if len(normalized_digest) != 64 or any(
        character not in "0123456789abcdef" for character in normalized_digest
    ):
        raise ValueError(f"{label} digest must be lowercase SHA-256")
    return normalized_version, normalized_digest


def canonical_result_hash(
    *,
    task_id: str,
    node_id: str,
    output: str | None,
    error: str | None,
    elapsed_seconds: float,
    contract_version: str | None,
    attempt_id: str | None,
    execution_id: str | None,
    execution_unit_id: str | None,
    execution_unit_kind: str | None,
) -> str:
    """Hash the normalized non-secret submission for exact replay checks."""
    payload = {
        "attempt_id": attempt_id,
        "contract_version": contract_version,
        "elapsed_seconds": elapsed_seconds,
        "error": error,
        "execution_id": execution_id,
        "execution_unit_id": execution_unit_id,
        "execution_unit_kind": execution_unit_kind,
        "node_id": node_id,
        "output": output,
        "task_id": task_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AttemptStore:
    """SQLite authority for attempt issuance and exactly-once settlement."""

    def __init__(self, path: str | Path = "events.db"):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        return sqlite_connect(self.path, row_factory=sqlite3.Row)

    @staticmethod
    def _migrate_connection(con: sqlite3.Connection) -> None:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id             TEXT PRIMARY KEY,
                task_id                TEXT NOT NULL,
                execution_id           TEXT,
                execution_unit_id      TEXT,
                execution_unit_kind    TEXT,
                assigned_node_id       TEXT NOT NULL,
                assigned_enrollment_id TEXT,
                assigned_credential_version INTEGER,
                assigned_session_id    TEXT,
                assigned_descriptor_version TEXT,
                assigned_descriptor_hash TEXT,
                requirement_version     TEXT,
                requirement_digest      TEXT,
                contract_version       TEXT,
                nonce_digest           TEXT NOT NULL,
                state                  TEXT NOT NULL CHECK(state IN (
                    'active', 'settled', 'expired', 'reclaimed', 'cancelled',
                    'superseded', 'interrupted'
                )),
                issued_at              REAL NOT NULL,
                lease_expires_at       REAL NOT NULL,
                max_output_bytes       INTEGER NOT NULL DEFAULT 1048576,
                streamed_bytes         INTEGER NOT NULL DEFAULT 0,
                stream_batch_count     INTEGER NOT NULL DEFAULT 0,
                first_stream_at        REAL,
                last_stream_at         REAL,
                stream_closed          INTEGER NOT NULL DEFAULT 0,
                stream_limit_event_emitted INTEGER NOT NULL DEFAULT 0,
                stream_rate_window_started_at REAL,
                stream_rate_window_batch_count INTEGER NOT NULL DEFAULT 0,
                settled_at             REAL,
                result_hash            TEXT,
                response_json          TEXT,
                reason                 TEXT
            )
            """
        )
        attempt_columns = {
            str(row[1]) for row in con.execute("PRAGMA table_info(attempts)").fetchall()
        }
        for name, declaration in {
            "assigned_enrollment_id": "TEXT",
            "assigned_credential_version": "INTEGER",
            "assigned_session_id": "TEXT",
            "assigned_descriptor_version": "TEXT",
            "assigned_descriptor_hash": "TEXT",
            "requirement_version": "TEXT",
            "requirement_digest": "TEXT",
            "max_output_bytes": "INTEGER NOT NULL DEFAULT 1048576",
            "streamed_bytes": "INTEGER NOT NULL DEFAULT 0",
            "stream_batch_count": "INTEGER NOT NULL DEFAULT 0",
            "first_stream_at": "REAL",
            "last_stream_at": "REAL",
            "stream_closed": "INTEGER NOT NULL DEFAULT 0",
            "stream_limit_event_emitted": "INTEGER NOT NULL DEFAULT 0",
            "stream_rate_window_started_at": "REAL",
            "stream_rate_window_batch_count": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in attempt_columns:
                con.execute(f"ALTER TABLE attempts ADD COLUMN {name} {declaration}")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_one_active_task "
            "ON attempts(task_id) WHERE state = 'active'"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempts_execution_state "
            "ON attempts(execution_id, state)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempts_enrollment_state "
            "ON attempts(assigned_enrollment_id, state) "
            "WHERE assigned_enrollment_id IS NOT NULL"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS accepted_result_receipts (
                attempt_id             TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
                task_id                TEXT NOT NULL UNIQUE,
                execution_id           TEXT,
                execution_unit_id      TEXT,
                execution_unit_kind    TEXT,
                assigned_node_id       TEXT NOT NULL,
                assigned_enrollment_id TEXT,
                assigned_descriptor_version TEXT,
                assigned_descriptor_hash TEXT,
                requirement_version    TEXT,
                requirement_digest     TEXT,
                contract_version       TEXT,
                result_hash            TEXT NOT NULL,
                accepted_at            REAL NOT NULL,
                output                 TEXT,
                error                  TEXT,
                elapsed_seconds        REAL NOT NULL
            )
            """
        )
        receipt_columns = {
            str(row[1])
            for row in con.execute(
                "PRAGMA table_info(accepted_result_receipts)"
            ).fetchall()
        }
        for name in (
            "assigned_enrollment_id",
            "assigned_descriptor_version",
            "assigned_descriptor_hash",
            "requirement_version",
            "requirement_digest",
        ):
            if name not in receipt_columns:
                con.execute(
                    f"ALTER TABLE accepted_result_receipts ADD COLUMN {name} TEXT"
                )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_receipts_execution_unit "
            "ON accepted_result_receipts(execution_id, execution_unit_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_receipts_enrollment "
            "ON accepted_result_receipts(assigned_enrollment_id) "
            "WHERE assigned_enrollment_id IS NOT NULL"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS result_quarantine (
                quarantine_id          TEXT PRIMARY KEY,
                task_id                TEXT NOT NULL,
                claimed_attempt_id     TEXT,
                claimed_node_id        TEXT NOT NULL,
                claimed_enrollment_id  TEXT,
                claimed_execution_id   TEXT,
                claimed_unit_id        TEXT,
                claimed_unit_kind      TEXT,
                claimed_contract_version TEXT,
                reason                 TEXT NOT NULL,
                output_sha256          TEXT,
                output_preview         TEXT,
                error                  TEXT,
                received_at            REAL NOT NULL
            )
            """
        )
        quarantine_columns = {
            str(row[1])
            for row in con.execute("PRAGMA table_info(result_quarantine)").fetchall()
        }
        if "claimed_enrollment_id" not in quarantine_columns:
            con.execute(
                "ALTER TABLE result_quarantine ADD COLUMN claimed_enrollment_id TEXT"
            )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_quarantine_received "
            "ON result_quarantine(received_at)"
        )
        ensure_node_enrollment_schema(con)
        ensure_node_capability_snapshot_schema(con)
        ensure_contribution_schema(con)

    def _ensure_schema(self, con: sqlite3.Connection) -> None:
        """Serialize idempotent additive migrations across store objects."""

        with migration_lock(self.path):
            self._migrate_connection(con)

    def migrate(self) -> None:
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            self._ensure_schema(con)
            con.commit()

    @staticmethod
    def _active_enrollment_matches(
        con: sqlite3.Connection,
        *,
        enrollment_id: str,
        node_id: str,
        credential_version: int,
    ) -> bool:
        """Check durable enrollment authority inside the caller's transaction."""

        row = con.execute(
            """
            SELECT node_id, status, credential_version
            FROM node_enrollments
            WHERE enrollment_id = ?
            """,
            (enrollment_id,),
        ).fetchone()
        return bool(
            row is not None
            and hmac.compare_digest(str(row["node_id"]), str(node_id))
            and str(row["status"]) == "active"
            and int(row["credential_version"]) == int(credential_version)
        )

    @staticmethod
    def _capability_snapshot_matches(
        con: sqlite3.Connection,
        *,
        enrollment_id: str,
        descriptor_version: str,
        descriptor_hash: str,
    ) -> bool:
        row = con.execute(
            """
            SELECT descriptor_version
            FROM node_capability_snapshots
            WHERE enrollment_id = ? AND descriptor_hash = ?
            """,
            (enrollment_id, descriptor_hash),
        ).fetchone()
        return bool(
            row is not None
            and hmac.compare_digest(
                str(row["descriptor_version"]), descriptor_version
            )
        )

    def issue(
        self,
        task: dict[str, Any],
        *,
        assigned_node_id: str,
        attempt_id: str,
        nonce: str,
        issued_at: float,
        lease_expires_at: float,
        assigned_session_id: str | None = None,
        assigned_enrollment_id: str | None = None,
        assigned_credential_version: int | None = None,
        assigned_descriptor_version: str | None = None,
        assigned_descriptor_hash: str | None = None,
        requirement_version: str | None = None,
        requirement_digest: str | None = None,
    ) -> AttemptRecord:
        if not attempt_id or not nonce:
            raise ValueError("attempt id and nonce are required")
        if lease_expires_at <= issued_at:
            raise ValueError("attempt lease must expire after issuance")
        if assigned_enrollment_id is None:
            if assigned_credential_version is not None:
                raise ValueError(
                    "legacy attempt cannot have an enrollment credential version"
                )
        else:
            if not assigned_session_id:
                raise ValueError("enrolled attempt requires an assigned session")
            if assigned_credential_version is None or int(assigned_credential_version) < 1:
                raise ValueError(
                    "enrolled attempt requires a positive credential version"
                )
        assigned_descriptor_version, assigned_descriptor_hash = (
            _validate_versioned_digest(
                assigned_descriptor_version,
                assigned_descriptor_hash,
                label="capability descriptor",
            )
        )
        if assigned_enrollment_id is not None and assigned_descriptor_hash is None:
            raise ValueError(
                "enrolled attempt requires a capability descriptor snapshot binding"
            )
        expected_requirement_version, expected_requirement_digest = (
            canonical_requirement_binding(
                task.get("resource_requirements"), task.get("requires", [])
            )
        )
        for supplied, expected, label in (
            (
                task.get("requirement_version"),
                expected_requirement_version,
                "task requirement version",
            ),
            (
                task.get("requirement_digest"),
                expected_requirement_digest,
                "task requirement digest",
            ),
            (requirement_version, expected_requirement_version, "requirement version"),
            (requirement_digest, expected_requirement_digest, "requirement digest"),
        ):
            if supplied is not None and str(supplied) != expected:
                raise ValueError(f"{label} does not match canonical task requirements")
        requirement_version = expected_requirement_version
        requirement_digest = expected_requirement_digest
        task["requirement_version"] = requirement_version
        task["requirement_digest"] = requirement_digest
        try:
            max_output_bytes = int(
                task.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("max_output_bytes must be an integer") from exc
        if not 1 <= max_output_bytes <= MAX_OUTPUT_BYTES:
            raise ValueError(
                f"max_output_bytes must be between 1 and {MAX_OUTPUT_BYTES}"
            )
        # The returned handout and the durable attempt must carry the exact same
        # server-issued value.  A worker submission never supplies this budget.
        task["max_output_bytes"] = max_output_bytes
        if task.get("contract_version") == "1":
            missing = [
                name
                for name in ("execution_id", "execution_unit_id", "execution_unit_kind")
                if not task.get(name)
            ]
            if missing:
                raise ValueError(f"v1 task is missing server binding fields: {', '.join(missing)}")
        values = (
            attempt_id,
            str(task["task_id"]),
            task.get("execution_id"),
            task.get("execution_unit_id"),
            task.get("execution_unit_kind"),
            assigned_node_id,
            assigned_enrollment_id,
            assigned_credential_version,
            assigned_session_id,
            assigned_descriptor_version,
            assigned_descriptor_hash,
            requirement_version,
            requirement_digest,
            task.get("contract_version"),
            nonce_digest(nonce),
            issued_at,
            lease_expires_at,
            max_output_bytes,
        )
        try:
            with self._lock, connection(
                self.path, row_factory=sqlite3.Row
            ) as con:
                self._ensure_schema(con)
                con.execute("BEGIN IMMEDIATE")
                if assigned_enrollment_id is not None and not self._active_enrollment_matches(
                    con,
                    enrollment_id=assigned_enrollment_id,
                    node_id=assigned_node_id,
                    credential_version=int(assigned_credential_version),
                ):
                    raise AttemptConflict(
                        "assigned enrollment is revoked, missing, or no longer current"
                    )
                if (
                    assigned_enrollment_id is not None
                    and not self._capability_snapshot_matches(
                        con,
                        enrollment_id=assigned_enrollment_id,
                        descriptor_version=str(assigned_descriptor_version),
                        descriptor_hash=assigned_descriptor_hash,
                    )
                ):
                    raise AttemptConflict(
                        "assigned capability descriptor snapshot is missing or inconsistent"
                    )
                con.execute(
                    """
                    INSERT INTO attempts (
                        attempt_id, task_id, execution_id, execution_unit_id,
                        execution_unit_kind, assigned_node_id,
                        assigned_enrollment_id, assigned_credential_version,
                        assigned_session_id, assigned_descriptor_version,
                        assigned_descriptor_hash, requirement_version,
                        requirement_digest, contract_version, nonce_digest,
                        state, issued_at, lease_expires_at, max_output_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'active', ?, ?, ?)
                    """,
                    values,
                )
                con.commit()
        except sqlite3.IntegrityError as exc:
            raise AttemptConflict(f"could not issue a unique active attempt for task {task['task_id']}") from exc
        record = self.get(attempt_id)
        if record is None:  # pragma: no cover - defensive after committed insert
            raise RuntimeError("attempt disappeared after issuance")
        return record

    def adopt_inflight(self, task: dict[str, Any]) -> AttemptRecord | None:
        """Durably adopt a complete pre-migration in-memory handout.

        This supports a rolling trusted-alpha upgrade and tests that construct a
        server-owned in-flight task directly. Unbound tasks are never adopted.
        """
        attempt_id = task.get("attempt_id")
        nonce = task.get("nonce")
        assigned = task.get("assigned_to")
        expires = task.get("lease_expires_at")
        if not (attempt_id and nonce and assigned and expires):
            return None
        existing = self.get(str(attempt_id))
        if existing:
            return existing
        issued = float(task.get("assigned_at") or min(time.time(), float(expires) - 0.001))
        return self.issue(
            task,
            assigned_node_id=str(assigned),
            attempt_id=str(attempt_id),
            nonce=str(nonce),
            issued_at=issued,
            lease_expires_at=float(expires),
            assigned_session_id=task.get("assigned_session_id"),
            assigned_enrollment_id=task.get("assigned_enrollment_id"),
            assigned_credential_version=task.get("assigned_credential_version"),
            assigned_descriptor_version=task.get("assigned_descriptor_version"),
            assigned_descriptor_hash=task.get("assigned_descriptor_hash"),
            requirement_version=task.get("requirement_version"),
            requirement_digest=task.get("requirement_digest"),
        )

    def get(self, attempt_id: str) -> AttemptRecord | None:
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return AttemptRecord.from_row(row) if row else None

    def active_for_task(self, task_id: str) -> AttemptRecord | None:
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute(
                "SELECT * FROM attempts WHERE task_id = ? AND state = 'active'",
                (task_id,),
            ).fetchone()
        return AttemptRecord.from_row(row) if row else None

    @staticmethod
    def _validate_binding(
        record: AttemptRecord,
        *,
        task_id: str,
        node_id: str,
        contract_version: str | None,
        attempt_id: str | None,
        nonce: str | None,
        execution_id: str | None,
        execution_unit_id: str | None,
        execution_unit_kind: str | None,
        session_id: str | None = None,
        enrollment_id: str | None = None,
        credential_version: int | None = None,
        allow_enrolled_replay_session: bool = False,
    ) -> None:
        if task_id != record.task_id:
            raise AttemptRejected("attempt belongs to another task", state=record.state)
        if node_id != record.assigned_node_id:
            raise AttemptRejected("submitting node is not the assigned node", state=record.state)
        if record.assigned_enrollment_id is None:
            if enrollment_id is not None or credential_version is not None:
                raise AttemptRejected(
                    "enrolled session cannot claim a legacy attempt",
                    state=record.state,
                )
        else:
            if not enrollment_id or not hmac.compare_digest(
                str(enrollment_id), str(record.assigned_enrollment_id)
            ):
                raise AttemptRejected(
                    "enrollment does not own this attempt", state=record.state
                )
            if credential_version is None:
                raise AttemptRejected(
                    "missing enrollment credential version", state=record.state
                )
            if not allow_enrolled_replay_session and (
                record.assigned_credential_version is None
                or int(credential_version) != int(record.assigned_credential_version)
            ):
                raise AttemptRejected(
                    "enrollment credential version does not own this attempt",
                    state=record.state,
                )
        if record.assigned_session_id is not None and not allow_enrolled_replay_session:
            if not session_id:
                raise AttemptRejected("missing assigned node session", state=record.state)
            if not hmac.compare_digest(
                str(session_id), str(record.assigned_session_id)
            ):
                raise AttemptRejected(
                    "node session does not own this attempt", state=record.state
                )
        elif allow_enrolled_replay_session and not session_id:
            raise AttemptRejected(
                "missing current node session for replay", state=record.state
            )
        if record.contract_version == "1":
            if not contract_version:
                raise AttemptRejected("missing contract version", state=record.state)
            if not hmac.compare_digest(str(contract_version), "1"):
                raise AttemptRejected("contract version does not match", state=record.state)
        if not attempt_id:
            raise AttemptRejected("missing attempt id", state=record.state)
        if not hmac.compare_digest(str(attempt_id), record.attempt_id):
            raise AttemptRejected("attempt id does not match", state=record.state)
        if not nonce:
            raise AttemptRejected("missing attempt nonce", state=record.state)
        if not hmac.compare_digest(nonce_digest(str(nonce)), record.nonce_digest):
            raise AttemptRejected("attempt nonce does not match", state=record.state)

        if record.contract_version == "1":
            for supplied, expected, label in (
                (execution_id, record.execution_id, "execution id"),
                (execution_unit_id, record.execution_unit_id, "execution unit id"),
                (execution_unit_kind, record.execution_unit_kind, "execution unit kind"),
            ):
                if not supplied:
                    raise AttemptRejected(f"missing {label}", state=record.state)
                if not expected or not hmac.compare_digest(str(supplied), str(expected)):
                    raise AttemptRejected(f"{label} does not match", state=record.state)
        elif contract_version not in (None, "", record.contract_version):
            raise AttemptRejected("contract version does not match", state=record.state)

    def settle(
        self,
        *,
        task_id: str,
        node_id: str,
        output: str | None,
        error: str | None,
        elapsed_seconds: float,
        contract_version: str | None,
        attempt_id: str | None,
        nonce: str | None,
        execution_id: str | None,
        execution_unit_id: str | None,
        execution_unit_kind: str | None,
        session_id: str | None = None,
        enrollment_id: str | None = None,
        credential_version: int | None = None,
        now: float | None = None,
    ) -> SettlementOutcome:
        """Atomically settle one active attempt or replay its exact response."""
        accepted_at = now if now is not None else time.time()
        result_hash = canonical_result_hash(
            task_id=task_id,
            node_id=node_id,
            output=output,
            error=error,
            elapsed_seconds=elapsed_seconds,
            contract_version=contract_version,
            attempt_id=attempt_id,
            execution_id=execution_id,
            execution_unit_id=execution_unit_id,
            execution_unit_kind=execution_unit_kind,
        )

        with self._lock:
            con = self._connect()
            try:
                self._ensure_schema(con)
                con.execute("BEGIN IMMEDIATE")
                # The active attempt for the task is authoritative even when a
                # submitter supplies an older/different attempt identifier.
                row = con.execute(
                    "SELECT * FROM attempts WHERE task_id = ? AND state = 'active'",
                    (task_id,),
                ).fetchone()
                if row is None and attempt_id:
                    row = con.execute(
                        "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
                    ).fetchone()
                if row is None:
                    raise AttemptRejected("no active server-issued attempt")
                record = AttemptRecord.from_row(row)

                if record.assigned_enrollment_id is not None:
                    if enrollment_id is None or credential_version is None:
                        raise AttemptRejected(
                            "missing assigned enrollment authority", state=record.state
                        )
                    if not self._active_enrollment_matches(
                        con,
                        enrollment_id=str(enrollment_id),
                        node_id=node_id,
                        credential_version=int(credential_version),
                    ):
                        raise AttemptRejected(
                            "enrollment is revoked, missing, or no longer current",
                            state=record.state,
                        )

                self._validate_binding(
                    record,
                    task_id=task_id,
                    node_id=node_id,
                    contract_version=contract_version,
                    attempt_id=attempt_id,
                    nonce=nonce,
                    execution_id=execution_id,
                    execution_unit_id=execution_unit_id,
                    execution_unit_kind=execution_unit_kind,
                    session_id=session_id,
                    enrollment_id=enrollment_id,
                    credential_version=credential_version,
                    allow_enrolled_replay_session=(
                        record.state == "settled"
                        and record.assigned_enrollment_id is not None
                    ),
                )

                if record.state == "settled":
                    if not record.result_hash or not hmac.compare_digest(record.result_hash, result_hash):
                        raise AttemptRejected(
                            "settled attempt replay payload does not match",
                            state=record.state,
                        )
                    receipt_row = con.execute(
                        "SELECT * FROM accepted_result_receipts WHERE attempt_id = ?",
                        (record.attempt_id,),
                    ).fetchone()
                    if receipt_row is None or not record.response_json:
                        raise RuntimeError("settled attempt is missing its durable receipt")
                    response = json.loads(record.response_json)
                    con.commit()
                    return SettlementOutcome(
                        receipt=AcceptedResultReceipt.from_row(receipt_row),
                        response=response,
                        replayed=True,
                    )

                if record.state != "active":
                    reason = (
                        "lease expired"
                        if record.state == "expired"
                        else f"attempt is {record.state}, not active"
                    )
                    raise AttemptRejected(
                        reason,
                        state=record.state,
                    )
                if accepted_at > record.lease_expires_at:
                    con.execute(
                        """
                        UPDATE attempts
                        SET state = 'expired', settled_at = ?,
                            reason = 'lease expired', stream_closed = 1
                        WHERE attempt_id = ? AND state = 'active'
                        """,
                        (accepted_at, record.attempt_id),
                    )
                    con.commit()
                    raise AttemptRejected("lease expired", state="expired")

                output_bytes = (
                    len(output.encode("utf-8")) if output is not None else 0
                )
                error_bytes = len(error.encode("utf-8")) if error is not None else 0
                exceeded: tuple[str, int, int] | None = None
                if output_bytes > record.max_output_bytes:
                    exceeded = ("output", record.max_output_bytes, output_bytes)
                elif error_bytes > MAX_ERROR_BYTES:
                    exceeded = ("error", MAX_ERROR_BYTES, error_bytes)
                if exceeded is not None:
                    field, limit, observed = exceeded
                    reason = (
                        f"{field} payload exceeded server byte limit "
                        f"({observed}>{limit})"
                    )
                    con.execute(
                        """
                        UPDATE attempts
                        SET state = 'cancelled', settled_at = ?, reason = ?,
                            stream_closed = 1
                        WHERE attempt_id = ? AND state = 'active'
                        """,
                        (accepted_at, reason, record.attempt_id),
                    )
                    con.commit()
                    raise WorkerPayloadLimitExceeded(
                        field, limit=limit, observed=observed
                    )

                points = 5 if output and not error else 0
                response = {"status": "accepted", "credits_earned": points}
                response_json = json.dumps(response, separators=(",", ":"), sort_keys=True)
                updated = con.execute(
                    """
                    UPDATE attempts
                    SET state = 'settled', settled_at = ?, result_hash = ?,
                        response_json = ?, reason = NULL, stream_closed = 1
                    WHERE attempt_id = ? AND state = 'active'
                    """,
                    (accepted_at, result_hash, response_json, record.attempt_id),
                )
                if updated.rowcount != 1:
                    raise AttemptRejected("attempt was settled concurrently")
                con.execute(
                    """
                    INSERT INTO accepted_result_receipts (
                        attempt_id, task_id, execution_id, execution_unit_id,
                        execution_unit_kind, assigned_node_id,
                        assigned_enrollment_id, assigned_descriptor_version,
                        assigned_descriptor_hash, requirement_version,
                        requirement_digest, contract_version,
                        result_hash, accepted_at, output, error, elapsed_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.attempt_id,
                        record.task_id,
                        record.execution_id,
                        record.execution_unit_id,
                        record.execution_unit_kind,
                        record.assigned_node_id,
                        record.assigned_enrollment_id,
                        record.assigned_descriptor_version,
                        record.assigned_descriptor_hash,
                        record.requirement_version,
                        record.requirement_digest,
                        record.contract_version,
                        result_hash,
                        accepted_at,
                        output,
                        error,
                        elapsed_seconds,
                    ),
                )
                insert_contribution_in_transaction(
                    con,
                    contribution_id=f"attempt:{record.attempt_id}",
                    contributor=record.assigned_node_id,
                    contribution_type="compute",
                    points=points,
                    task=record.task_id,
                    details=(
                        "Compute contribution for an accepted bound attempt; "
                        "this does not imply candidate selection or validated correctness."
                    ),
                    basis="compute_contribution",
                    attempt_id=record.attempt_id,
                    enrollment_id=record.assigned_enrollment_id,
                    node_id=record.assigned_node_id,
                    session_id=record.assigned_session_id,
                    created_at=accepted_at,
                )
                con.commit()
                receipt_row = con.execute(
                    "SELECT * FROM accepted_result_receipts WHERE attempt_id = ?",
                    (record.attempt_id,),
                ).fetchone()
                if receipt_row is None:  # pragma: no cover - same committed transaction
                    raise RuntimeError("accepted receipt disappeared after settlement")
                return SettlementOutcome(
                    receipt=AcceptedResultReceipt.from_row(receipt_row),
                    response=response,
                    replayed=False,
                )
            except Exception:
                if con.in_transaction:
                    con.rollback()
                raise
            finally:
                con.close()

    def record_stream_batch(
        self,
        *,
        task_id: str,
        node_id: str,
        tokens: str,
        contract_version: str | None,
        attempt_id: str | None,
        nonce: str | None,
        execution_id: str | None,
        execution_unit_id: str | None,
        execution_unit_kind: str | None,
        session_id: str | None = None,
        enrollment_id: str | None = None,
        credential_version: int | None = None,
        now: float | None = None,
    ) -> StreamBatchOutcome:
        """Atomically validate and account for one worker token batch.

        Stream accounting is durable with the attempt so per-request checks can
        never reset the byte or event budget.  Limit breaches terminally cancel
        the attempt before a later result can be mistaken for a normal success.
        """

        streamed_at = time.time() if now is None else float(now)
        batch_bytes = len(tokens.encode("utf-8"))
        with self._lock:
            con = self._connect()
            try:
                self._ensure_schema(con)
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    "SELECT * FROM attempts WHERE task_id = ? AND state = 'active'",
                    (task_id,),
                ).fetchone()
                if row is None and attempt_id:
                    row = con.execute(
                        "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
                    ).fetchone()
                if row is None:
                    raise AttemptRejected("no active server-issued attempt")
                record = AttemptRecord.from_row(row)
                if record.assigned_enrollment_id is not None:
                    if enrollment_id is None or credential_version is None:
                        raise AttemptRejected(
                            "missing assigned enrollment authority", state=record.state
                        )
                    if not self._active_enrollment_matches(
                        con,
                        enrollment_id=str(enrollment_id),
                        node_id=node_id,
                        credential_version=int(credential_version),
                    ):
                        raise AttemptRejected(
                            "enrollment is revoked, missing, or no longer current",
                            state=record.state,
                        )
                self._validate_binding(
                    record,
                    task_id=task_id,
                    node_id=node_id,
                    contract_version=contract_version,
                    attempt_id=attempt_id,
                    nonce=nonce,
                    execution_id=execution_id,
                    execution_unit_id=execution_unit_id,
                    execution_unit_kind=execution_unit_kind,
                    session_id=session_id,
                    enrollment_id=enrollment_id,
                    credential_version=credential_version,
                )
                if record.state != "active":
                    reason = (
                        "lease expired"
                        if record.state == "expired"
                        else f"attempt is {record.state}, not active"
                    )
                    raise AttemptRejected(reason, state=record.state)
                if streamed_at > record.lease_expires_at:
                    con.execute(
                        """
                        UPDATE attempts
                        SET state = 'expired', settled_at = ?,
                            reason = 'lease expired', stream_closed = 1
                        WHERE attempt_id = ? AND state = 'active'
                        """,
                        (streamed_at, record.attempt_id),
                    )
                    con.commit()
                    raise AttemptRejected("lease expired", state="expired")
                if record.stream_closed:
                    raise AttemptRejected("attempt stream is closed", state=record.state)

                next_bytes = int(record.streamed_bytes) + batch_bytes
                next_batch_count = int(record.stream_batch_count) + 1
                window_started = record.stream_rate_window_started_at
                if (
                    window_started is None
                    or streamed_at - float(window_started) >= STREAM_RATE_WINDOW_SECONDS
                ):
                    window_started = streamed_at
                    window_count = 1
                else:
                    window_count = int(record.stream_rate_window_batch_count) + 1

                error_code: str | None = None
                detail: str | None = None
                if next_bytes > record.max_output_bytes:
                    error_code = "output_limit_exceeded"
                    detail = (
                        f"cumulative stream would be {next_bytes} UTF-8 bytes; "
                        f"server limit is {record.max_output_bytes} bytes"
                    )
                elif next_batch_count > MAX_STREAM_BATCHES:
                    error_code = "stream_batch_limit_exceeded"
                    detail = f"stream batch limit is {MAX_STREAM_BATCHES}"
                elif window_count > MAX_STREAM_BATCHES_PER_WINDOW:
                    error_code = "stream_rate_limit_exceeded"
                    detail = (
                        f"stream event-rate limit is "
                        f"{MAX_STREAM_BATCHES_PER_WINDOW} batches per "
                        f"{STREAM_RATE_WINDOW_SECONDS:g} second"
                    )

                if error_code is not None:
                    emit_limit_event = not bool(record.stream_limit_event_emitted)
                    changed = con.execute(
                        """
                        UPDATE attempts
                        SET state = 'cancelled', settled_at = ?, reason = ?,
                            stream_closed = 1, stream_limit_event_emitted = 1
                        WHERE attempt_id = ? AND state = 'active'
                        """,
                        (streamed_at, detail, record.attempt_id),
                    ).rowcount
                    if changed != 1:
                        raise AttemptRejected("attempt changed while streaming")
                    con.commit()
                    return StreamBatchOutcome(
                        accepted=False,
                        attempt_id=record.attempt_id,
                        max_output_bytes=record.max_output_bytes,
                        streamed_bytes=int(record.streamed_bytes),
                        stream_batch_count=int(record.stream_batch_count),
                        error_code=error_code,
                        detail=detail,
                        emit_limit_event=emit_limit_event,
                    )

                changed = con.execute(
                    """
                    UPDATE attempts
                    SET streamed_bytes = ?, stream_batch_count = ?,
                        first_stream_at = COALESCE(first_stream_at, ?),
                        last_stream_at = ?,
                        stream_rate_window_started_at = ?,
                        stream_rate_window_batch_count = ?
                    WHERE attempt_id = ? AND state = 'active' AND stream_closed = 0
                    """,
                    (
                        next_bytes,
                        next_batch_count,
                        streamed_at,
                        streamed_at,
                        window_started,
                        window_count,
                        record.attempt_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise AttemptRejected("attempt changed while streaming")
                con.commit()
                return StreamBatchOutcome(
                    accepted=True,
                    attempt_id=record.attempt_id,
                    max_output_bytes=record.max_output_bytes,
                    streamed_bytes=next_bytes,
                    stream_batch_count=next_batch_count,
                )
            except Exception:
                if con.in_transaction:
                    con.rollback()
                raise
            finally:
                con.close()

    def get_receipt_for_task(self, task_id: str) -> AcceptedResultReceipt | None:
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute(
                "SELECT * FROM accepted_result_receipts WHERE task_id = ?", (task_id,)
            ).fetchone()
        return AcceptedResultReceipt.from_row(row) if row else None

    def count_attempts(self, task_id: str) -> int:
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute(
                "SELECT COUNT(*) FROM attempts WHERE task_id = ?", (task_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def lifetime_contribution_summary(
        self,
        node_id: str,
        enrollment_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, int | float]:
        """Read counters without treating a reusable label as durable identity."""

        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            if enrollment_id is not None:
                predicate = "enrollment_id = ?"
                parameters = (enrollment_id,)
            elif session_id is not None:
                predicate = "enrollment_id IS NULL AND session_id = ?"
                parameters = (session_id,)
            else:
                # Historical rows have neither attribution field. Do not merge
                # them into a newly enrolled or newly session-scoped claimant.
                predicate = (
                    "enrollment_id IS NULL AND session_id IS NULL "
                    "AND contributor = ?"
                )
                parameters = (node_id,)
            row = con.execute(
                f"""
                SELECT COUNT(*) AS tasks, COALESCE(SUM(points), 0) AS points
                FROM contributions
                WHERE {predicate} AND contribution_type = 'compute'
                """,
                parameters,
            ).fetchone()
        return {
            "lifetime_tasks_completed": int(row["tasks"] if row else 0),
            "lifetime_contribution_points": float(row["points"] if row else 0),
        }

    def reclaim_enrollment(
        self,
        enrollment_id: str,
        reason: str,
        *,
        now: float | None = None,
    ) -> list[str]:
        """Atomically reclaim every active lease owned by one enrollment."""

        if not enrollment_id:
            raise ValueError("enrollment_id is required")
        reclaimed_at = time.time() if now is None else float(now)
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            self._ensure_schema(con)
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                """
                SELECT task_id
                FROM attempts
                WHERE assigned_enrollment_id = ? AND state = 'active'
                ORDER BY task_id
                """,
                (enrollment_id,),
            ).fetchall()
            con.execute(
                """
                UPDATE attempts
                SET state = 'reclaimed', settled_at = ?, reason = ?,
                    stream_closed = 1
                WHERE assigned_enrollment_id = ? AND state = 'active'
                """,
                (reclaimed_at, reason, enrollment_id),
            )
            con.commit()
        return [str(row["task_id"]) for row in rows]

    def execution_attempt_summary(self, execution_id: str) -> dict[str, int]:
        """Return durable attempt/retry counts for terminal telemetry."""
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            rows = con.execute(
                "SELECT task_id, state FROM attempts WHERE execution_id = ?",
                (execution_id,),
            ).fetchall()
        attempts_by_task: dict[str, int] = {}
        for row in rows:
            task_id = str(row["task_id"])
            attempts_by_task[task_id] = attempts_by_task.get(task_id, 0) + 1
        repeated_attempts = sum(
            max(0, count - 1) for count in attempts_by_task.values()
        )
        return {
            "attempt_count": len(rows),
            "unit_count": len(attempts_by_task),
            "retry_count": repeated_attempts,
            "reassignment_count": repeated_attempts,
        }

    def transition_active(
        self,
        *,
        state: AttemptState,
        reason: str,
        task_id: str | None = None,
        attempt_id: str | None = None,
        now: float | None = None,
    ) -> bool:
        if state == "active" or state not in _TERMINAL_STATES:
            raise ValueError("transition target must be a terminal attempt state")
        if not task_id and not attempt_id:
            raise ValueError("task_id or attempt_id is required")
        column, value = ("attempt_id", attempt_id) if attempt_id else ("task_id", task_id)
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            self._ensure_schema(con)
            con.execute("BEGIN IMMEDIATE")
            changed = con.execute(
                f"UPDATE attempts SET state = ?, settled_at = ?, reason = ?, "
                f"stream_closed = 1 "
                f"WHERE {column} = ? AND state = 'active'",
                (state, now if now is not None else time.time(), reason, value),
            ).rowcount
            con.commit()
        return changed == 1

    def cancel_execution(self, execution_id: str, reason: str) -> list[str]:
        """Cancel every active attempt and return its task identifiers."""
        now = time.time()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            self._ensure_schema(con)
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT task_id FROM attempts WHERE execution_id = ? AND state = 'active'",
                (execution_id,),
            ).fetchall()
            con.execute(
                """
                UPDATE attempts SET state = 'cancelled', settled_at = ?, reason = ?,
                    stream_closed = 1
                WHERE execution_id = ? AND state = 'active'
                """,
                (now, reason, execution_id),
            )
            con.commit()
        return [str(row["task_id"]) for row in rows]

    def expire_due(self, now: float | None = None) -> int:
        """Durably close active leases whose wall-clock deadline has elapsed."""
        cutoff = now if now is not None else time.time()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            self._ensure_schema(con)
            con.execute("BEGIN IMMEDIATE")
            changed = con.execute(
                """
                UPDATE attempts
                SET state = 'expired', settled_at = ?, reason = 'lease expired',
                    stream_closed = 1
                WHERE state = 'active' AND lease_expires_at < ?
                """,
                (cutoff, cutoff),
            ).rowcount
            con.commit()
        return int(changed)

    def interrupt_active(self, reason: str) -> int:
        """Fail closed after restart because the queue itself is process-local."""
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            self._ensure_schema(con)
            con.execute("BEGIN IMMEDIATE")
            changed = con.execute(
                """
                UPDATE attempts SET state = 'interrupted', settled_at = ?, reason = ?,
                    stream_closed = 1
                WHERE state = 'active'
                """,
                (time.time(), reason),
            ).rowcount
            con.commit()
        return int(changed)

    @staticmethod
    def _bounded_preview(value: str | None, max_bytes: int = _MAX_QUARANTINE_PREVIEW_BYTES) -> str | None:
        if value is None:
            return None
        # ``ignore`` only affects a code point split by the byte boundary.  It
        # keeps the stored UTF-8 representation at or below the promised cap;
        # replacement characters could expand it beyond the cap.
        raw = value.encode("utf-8")[:max_bytes]
        return raw.decode("utf-8", errors="ignore")

    def quarantine(
        self,
        *,
        task_id: str,
        claimed_attempt_id: str | None,
        claimed_node_id: str,
        claimed_enrollment_id: str | None = None,
        claimed_execution_id: str | None,
        claimed_unit_id: str | None,
        claimed_unit_kind: str | None,
        claimed_contract_version: str | None,
        output: str | None,
        error: str | None,
        reason: str,
    ) -> str:
        """Retain bounded diagnostics outside the operational receipt channel."""
        quarantine_id = uuid.uuid4().hex
        output_hash = (
            hashlib.sha256(output.encode("utf-8")).hexdigest()
            if output is not None
            else None
        )
        received_at = time.time()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            self._ensure_schema(con)
            con.execute(
                """
                INSERT INTO result_quarantine (
                    quarantine_id, task_id, claimed_attempt_id, claimed_node_id,
                    claimed_enrollment_id,
                    claimed_execution_id, claimed_unit_id, claimed_unit_kind,
                    claimed_contract_version, reason, output_sha256,
                    output_preview, error, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quarantine_id,
                    task_id,
                    claimed_attempt_id,
                    claimed_node_id,
                    claimed_enrollment_id,
                    claimed_execution_id,
                    claimed_unit_id,
                    claimed_unit_kind,
                    claimed_contract_version,
                    self._bounded_preview(reason, 500) or "result rejected",
                    output_hash,
                    self._bounded_preview(output),
                    self._bounded_preview(error, MAX_ERROR_BYTES),
                    received_at,
                ),
            )
            con.execute(
                """
                DELETE FROM result_quarantine
                WHERE quarantine_id NOT IN (
                    SELECT quarantine_id FROM result_quarantine
                    ORDER BY received_at DESC, quarantine_id DESC LIMIT ?
                )
                """,
                (_MAX_QUARANTINE_ROWS,),
            )
            con.commit()
        return quarantine_id

    def quarantine_count(self) -> int:
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute("SELECT COUNT(*) FROM result_quarantine").fetchone()
        return int(row[0]) if row else 0


class AcceptedResultBroker:
    """Only attempt-bound durable receipts may wake a dispatcher."""

    def __init__(self, store: AttemptStore, max_cached: int = 5000):
        self.store = store
        self.max_cached = max_cached
        self._lock = threading.RLock()
        self._receipts: OrderedDict[str, AcceptedResultReceipt] = OrderedDict()

    def publish(self, receipt: AcceptedResultReceipt) -> None:
        with self._lock:
            self._receipts[receipt.task_id] = receipt
            self._receipts.move_to_end(receipt.task_id)
            while len(self._receipts) > self.max_cached:
                self._receipts.popitem(last=False)

    def get_matching(
        self,
        *,
        task_id: str,
        execution_id: str,
        execution_unit_id: str,
        execution_unit_kind: str,
        contract_version: str = "1",
    ) -> AcceptedResultReceipt | None:
        with self._lock:
            receipt = self._receipts.get(task_id)
        if receipt is None:
            # Durable fallback closes the commit-before-publish crash window.
            receipt = self.store.get_receipt_for_task(task_id)
            if receipt is not None:
                self.publish(receipt)
        if receipt is None:
            return None
        expected = {
            "task_id": task_id,
            "execution_id": execution_id,
            "execution_unit_id": execution_unit_id,
            "execution_unit_kind": execution_unit_kind,
            "contract_version": contract_version,
        }
        for field, value in expected.items():
            if getattr(receipt, field) != value:
                raise ReceiptBindingError(
                    f"accepted receipt {field} does not match the dispatch wait"
                )
        return receipt

    def clear(self) -> None:
        with self._lock:
            self._receipts.clear()
