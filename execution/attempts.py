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


class AttemptConflict(RuntimeError):
    """The server tried to issue an attempt that violates durable uniqueness."""


class AttemptRejected(RuntimeError):
    """A submission cannot settle the authoritative attempt."""

    def __init__(self, reason: str, *, state: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.state = state


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
    contract_version: str | None
    nonce_digest: str
    state: str
    issued_at: float
    lease_expires_at: float
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


def nonce_digest(nonce: str) -> str:
    """One-way digest for a high-entropy server nonce."""
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


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
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 30000")
        con.execute("PRAGMA foreign_keys = ON")
        return con

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
                contract_version       TEXT,
                nonce_digest           TEXT NOT NULL,
                state                  TEXT NOT NULL CHECK(state IN (
                    'active', 'settled', 'expired', 'reclaimed', 'cancelled',
                    'superseded', 'interrupted'
                )),
                issued_at              REAL NOT NULL,
                lease_expires_at       REAL NOT NULL,
                settled_at             REAL,
                result_hash            TEXT,
                response_json          TEXT,
                reason                 TEXT
            )
            """
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_one_active_task "
            "ON attempts(task_id) WHERE state = 'active'"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempts_execution_state "
            "ON attempts(execution_id, state)"
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
                contract_version       TEXT,
                result_hash            TEXT NOT NULL,
                accepted_at            REAL NOT NULL,
                output                 TEXT,
                error                  TEXT,
                elapsed_seconds        REAL NOT NULL
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_receipts_execution_unit "
            "ON accepted_result_receipts(execution_id, execution_unit_id)"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS result_quarantine (
                quarantine_id          TEXT PRIMARY KEY,
                task_id                TEXT NOT NULL,
                claimed_attempt_id     TEXT,
                claimed_node_id        TEXT NOT NULL,
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
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_quarantine_received "
            "ON result_quarantine(received_at)"
        )
        ensure_contribution_schema(con)

    def migrate(self) -> None:
        with self._lock, self._connect() as con:
            self._migrate_connection(con)
            con.commit()

    def issue(
        self,
        task: dict[str, Any],
        *,
        assigned_node_id: str,
        attempt_id: str,
        nonce: str,
        issued_at: float,
        lease_expires_at: float,
    ) -> AttemptRecord:
        if not attempt_id or not nonce:
            raise ValueError("attempt id and nonce are required")
        if lease_expires_at <= issued_at:
            raise ValueError("attempt lease must expire after issuance")
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
            task.get("contract_version"),
            nonce_digest(nonce),
            issued_at,
            lease_expires_at,
        )
        try:
            with self._lock, self._connect() as con:
                self._migrate_connection(con)
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    """
                    INSERT INTO attempts (
                        attempt_id, task_id, execution_id, execution_unit_id,
                        execution_unit_kind, assigned_node_id, contract_version,
                        nonce_digest, state, issued_at, lease_expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
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
        )

    def get(self, attempt_id: str) -> AttemptRecord | None:
        self.migrate()
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return AttemptRecord.from_row(row) if row else None

    def active_for_task(self, task_id: str) -> AttemptRecord | None:
        self.migrate()
        with self._lock, self._connect() as con:
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
    ) -> None:
        if task_id != record.task_id:
            raise AttemptRejected("attempt belongs to another task", state=record.state)
        if node_id != record.assigned_node_id:
            raise AttemptRejected("submitting node is not the assigned node", state=record.state)
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
                self._migrate_connection(con)
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
                        SET state = 'expired', settled_at = ?, reason = 'lease expired'
                        WHERE attempt_id = ? AND state = 'active'
                        """,
                        (accepted_at, record.attempt_id),
                    )
                    con.commit()
                    raise AttemptRejected("lease expired", state="expired")

                points = 5 if output and not error else 0
                response = {"status": "accepted", "credits_earned": points}
                response_json = json.dumps(response, separators=(",", ":"), sort_keys=True)
                updated = con.execute(
                    """
                    UPDATE attempts
                    SET state = 'settled', settled_at = ?, result_hash = ?,
                        response_json = ?, reason = NULL
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
                        execution_unit_kind, assigned_node_id, contract_version,
                        result_hash, accepted_at, output, error, elapsed_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.attempt_id,
                        record.task_id,
                        record.execution_id,
                        record.execution_unit_id,
                        record.execution_unit_kind,
                        record.assigned_node_id,
                        record.contract_version,
                        result_hash,
                        accepted_at,
                        output,
                        error,
                        elapsed_seconds,
                    ),
                )
                if points:
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

    def get_receipt_for_task(self, task_id: str) -> AcceptedResultReceipt | None:
        self.migrate()
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM accepted_result_receipts WHERE task_id = ?", (task_id,)
            ).fetchone()
        return AcceptedResultReceipt.from_row(row) if row else None

    def count_attempts(self, task_id: str) -> int:
        self.migrate()
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) FROM attempts WHERE task_id = ?", (task_id,)
            ).fetchone()
        return int(row[0]) if row else 0

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
        with self._lock, self._connect() as con:
            self._migrate_connection(con)
            con.execute("BEGIN IMMEDIATE")
            changed = con.execute(
                f"UPDATE attempts SET state = ?, settled_at = ?, reason = ? "
                f"WHERE {column} = ? AND state = 'active'",
                (state, now if now is not None else time.time(), reason, value),
            ).rowcount
            con.commit()
        return changed == 1

    def cancel_execution(self, execution_id: str, reason: str) -> list[str]:
        """Cancel every active attempt and return its task identifiers."""
        now = time.time()
        with self._lock, self._connect() as con:
            self._migrate_connection(con)
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT task_id FROM attempts WHERE execution_id = ? AND state = 'active'",
                (execution_id,),
            ).fetchall()
            con.execute(
                """
                UPDATE attempts SET state = 'cancelled', settled_at = ?, reason = ?
                WHERE execution_id = ? AND state = 'active'
                """,
                (now, reason, execution_id),
            )
            con.commit()
        return [str(row["task_id"]) for row in rows]

    def expire_due(self, now: float | None = None) -> int:
        """Durably close active leases whose wall-clock deadline has elapsed."""
        cutoff = now if now is not None else time.time()
        with self._lock, self._connect() as con:
            self._migrate_connection(con)
            con.execute("BEGIN IMMEDIATE")
            changed = con.execute(
                """
                UPDATE attempts
                SET state = 'expired', settled_at = ?, reason = 'lease expired'
                WHERE state = 'active' AND lease_expires_at < ?
                """,
                (cutoff, cutoff),
            ).rowcount
            con.commit()
        return int(changed)

    def interrupt_active(self, reason: str) -> int:
        """Fail closed after restart because the queue itself is process-local."""
        with self._lock, self._connect() as con:
            self._migrate_connection(con)
            con.execute("BEGIN IMMEDIATE")
            changed = con.execute(
                """
                UPDATE attempts SET state = 'interrupted', settled_at = ?, reason = ?
                WHERE state = 'active'
                """,
                (time.time(), reason),
            ).rowcount
            con.commit()
        return int(changed)

    @staticmethod
    def _bounded_preview(output: str | None) -> str | None:
        if output is None:
            return None
        raw = output.encode("utf-8")[:_MAX_QUARANTINE_PREVIEW_BYTES]
        return raw.decode("utf-8", errors="replace")

    def quarantine(
        self,
        *,
        task_id: str,
        claimed_attempt_id: str | None,
        claimed_node_id: str,
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
        output_hash = hashlib.sha256(output.encode("utf-8")).hexdigest() if output is not None else None
        received_at = time.time()
        with self._lock, self._connect() as con:
            self._migrate_connection(con)
            con.execute(
                """
                INSERT INTO result_quarantine (
                    quarantine_id, task_id, claimed_attempt_id, claimed_node_id,
                    claimed_execution_id, claimed_unit_id, claimed_unit_kind,
                    claimed_contract_version, reason, output_sha256,
                    output_preview, error, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quarantine_id,
                    task_id,
                    claimed_attempt_id,
                    claimed_node_id,
                    claimed_execution_id,
                    claimed_unit_id,
                    claimed_unit_kind,
                    claimed_contract_version,
                    reason,
                    output_hash,
                    self._bounded_preview(output),
                    error,
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
        with self._lock, self._connect() as con:
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
