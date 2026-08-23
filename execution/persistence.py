"""SQLite persistence for canonical executions.

The worker queue remains process-local in protocol v1. This store makes the
canonical request, strategy decision, placement metadata, and final normalized
result durable without claiming scheduler durability.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from execution.contracts import ExecutionRequestV1, ExecutionResultV1
from sqlite_store import connection, migration_lock


class ExecutionStore:
    def __init__(self, path: str | Path = "events.db"):
        self.path = Path(path)
        self._lock = threading.RLock()

    def migrate(self) -> None:
        """Create the v1 execution table idempotently, preserving old tables."""
        with self._lock, migration_lock(self.path), connection(self.path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id       TEXT PRIMARY KEY,
                    job_id             TEXT,
                    protocol_version   TEXT NOT NULL,
                    request_json       TEXT NOT NULL,
                    strategy_requested TEXT NOT NULL,
                    strategy_selected  TEXT NOT NULL,
                    strategy_version   TEXT NOT NULL,
                    strategy_options   TEXT NOT NULL,
                    selector_reason    TEXT NOT NULL,
                    selector_version   TEXT NOT NULL,
                    placement_requested TEXT NOT NULL,
                    placement_selected TEXT,
                    fallback_reason    TEXT,
                    status             TEXT NOT NULL,
                    created_at         TEXT NOT NULL,
                    started_at         TEXT,
                    completed_at       TEXT,
                    result_json        TEXT NOT NULL,
                    candidate_summaries TEXT NOT NULL,
                    validation_summaries TEXT NOT NULL,
                    error_json         TEXT NOT NULL,
                    lifecycle_status   TEXT,
                    validation_outcome TEXT,
                    assurance_level    TEXT,
                    interruption_reason TEXT,
                    coordinator_restart_marker TEXT,
                    interrupted_at     TEXT,
                    retryable          INTEGER NOT NULL DEFAULT 0,
                    cancellation_requested INTEGER NOT NULL DEFAULT 0,
                    remote_dispatch_consent INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # SQLite has no ``ADD COLUMN IF NOT EXISTS``. Inspecting the table
            # keeps this migration safe for databases created by protocol v1.
            existing = {row[1] for row in con.execute("PRAGMA table_info(executions)")}
            additions = {
                "lifecycle_status": "TEXT",
                "validation_outcome": "TEXT",
                "assurance_level": "TEXT",
                "interruption_reason": "TEXT",
                "coordinator_restart_marker": "TEXT",
                "interrupted_at": "TEXT",
                "retryable": "INTEGER NOT NULL DEFAULT 0",
                "cancellation_requested": "INTEGER NOT NULL DEFAULT 0",
                "remote_dispatch_consent": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, declaration in additions.items():
                if name not in existing:
                    con.execute(f"ALTER TABLE executions ADD COLUMN {name} {declaration}")
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_executions_job_id ON executions(job_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_executions_created_at ON executions(created_at)"
            )
            con.commit()

    @staticmethod
    def _bounded_json(value: Any, limit: int = 262_144) -> str:
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
        if len(raw.encode("utf-8")) > limit:
            raise ValueError(f"persisted execution JSON exceeds {limit} bytes")
        return raw

    def save(self, request: ExecutionRequestV1, result: ExecutionResultV1) -> None:
        self.migrate()
        request_json = self._bounded_json(request.model_dump(mode="json"), 65_536)
        result_json = self._bounded_json(result.model_dump(mode="json"))
        with self._lock, connection(self.path) as con:
            con.execute(
                """
                INSERT INTO executions (
                    execution_id, job_id, protocol_version, request_json,
                    strategy_requested, strategy_selected, strategy_version,
                    strategy_options, selector_reason, selector_version,
                    placement_requested, placement_selected, fallback_reason,
                    status, created_at, started_at, completed_at, result_json,
                    candidate_summaries, validation_summaries, error_json,
                    lifecycle_status, validation_outcome, assurance_level,
                    interruption_reason, coordinator_restart_marker,
                    interrupted_at, retryable, cancellation_requested,
                    remote_dispatch_consent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                    job_id=excluded.job_id,
                    request_json=excluded.request_json,
                    strategy_requested=excluded.strategy_requested,
                    strategy_selected=excluded.strategy_selected,
                    strategy_version=excluded.strategy_version,
                    strategy_options=excluded.strategy_options,
                    selector_reason=excluded.selector_reason,
                    selector_version=excluded.selector_version,
                    placement_requested=excluded.placement_requested,
                    placement_selected=excluded.placement_selected,
                    fallback_reason=excluded.fallback_reason,
                    status=excluded.status,
                    started_at=excluded.started_at,
                    completed_at=excluded.completed_at,
                    result_json=excluded.result_json,
                    candidate_summaries=excluded.candidate_summaries,
                    validation_summaries=excluded.validation_summaries,
                    error_json=excluded.error_json,
                    lifecycle_status=excluded.lifecycle_status,
                    validation_outcome=excluded.validation_outcome,
                    assurance_level=excluded.assurance_level,
                    interruption_reason=excluded.interruption_reason,
                    coordinator_restart_marker=excluded.coordinator_restart_marker,
                    interrupted_at=excluded.interrupted_at,
                    retryable=excluded.retryable,
                    cancellation_requested=excluded.cancellation_requested,
                    remote_dispatch_consent=excluded.remote_dispatch_consent
                """,
                (
                    result.execution_id,
                    result.job_id,
                    result.protocol_version,
                    request_json,
                    result.strategy_requested,
                    result.strategy_selected,
                    result.strategy_version,
                    self._bounded_json(result.strategy_options, 16_384),
                    result.selector_reason,
                    result.selector_version,
                    result.placement_requested,
                    result.placement_selected,
                    result.fallback_reason,
                    result.status,
                    result.created_at,
                    result.started_at,
                    result.completed_at,
                    result_json,
                    self._bounded_json([c.model_dump(mode="json") for c in result.candidates], 65_536),
                    self._bounded_json(
                        [v.model_dump(mode="json") for v in result.validation_evidence], 65_536
                    ),
                    self._bounded_json([e.model_dump(mode="json") for e in result.errors], 16_384),
                    getattr(result, "lifecycle_status", result.status),
                    getattr(result, "validation_outcome", "not_run"),
                    getattr(result, "assurance_level", "unverified"),
                    getattr(result, "interruption_reason", None),
                    getattr(result, "coordinator_restart_marker", None),
                    getattr(result, "interrupted_at", None),
                    int(bool(getattr(result, "retryable", False))),
                    int(bool(getattr(result, "cancellation_requested", False))),
                    int(bool(getattr(result, "remote_dispatch_consent", False))),
                ),
            )
            con.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def reconcile_nonterminal(self, restart_marker: str | None = None) -> list[str]:
        """Truthfully interrupt persisted work that this process cannot resume.

        The task queue intentionally remains process-local. Therefore a fresh
        coordinator has no resumable coroutine for a row left queued/running.
        The update is transactional and idempotent; already terminal rows and
        rows marked by a previous reconciliation are untouched.
        """
        self.migrate()
        marker = restart_marker or uuid.uuid4().hex
        interrupted_at = self._now()
        changed: list[str] = []
        with self._lock, connection(self.path) as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                """
                SELECT execution_id, result_json
                FROM executions
                WHERE COALESCE(lifecycle_status, status) IN ('queued', 'running')
                """
            ).fetchall()
            for execution_id, raw_result in rows:
                try:
                    payload = json.loads(raw_result)
                except (TypeError, json.JSONDecodeError):
                    payload = {"execution_id": execution_id}
                payload["status"] = "failed"  # compatibility projection
                payload["lifecycle_status"] = "interrupted"
                payload["interruption_reason"] = "coordinator restarted without resumable in-process work"
                payload["coordinator_restart_marker"] = marker
                payload["interrupted_at"] = interrupted_at
                payload["completed_at"] = payload.get("completed_at") or interrupted_at
                payload["retryable"] = True
                errors = list(payload.get("errors") or [])
                if not any(item.get("code") == "coordinator_restarted" for item in errors if isinstance(item, dict)):
                    errors.append(
                        {
                            "code": "coordinator_restarted",
                            "message": "Execution was interrupted by a coordinator restart.",
                            "retryable": True,
                        }
                    )
                payload["errors"] = errors[:20]
                con.execute(
                    """
                    UPDATE executions
                    SET status='failed', lifecycle_status='interrupted',
                        interruption_reason=?, coordinator_restart_marker=?,
                        interrupted_at=?, retryable=1, completed_at=?,
                        result_json=?, error_json=?
                    WHERE execution_id=?
                      AND COALESCE(lifecycle_status, status) IN ('queued', 'running')
                    """,
                    (
                        payload["interruption_reason"],
                        marker,
                        interrupted_at,
                        payload["completed_at"],
                        self._bounded_json(payload),
                        self._bounded_json(errors, 16_384),
                        execution_id,
                    ),
                )
                changed.append(execution_id)
            con.commit()
        return changed

    def get(self, execution_id: str) -> ExecutionResultV1 | None:
        self.migrate()
        with self._lock, connection(self.path) as con:
            row = con.execute(
                "SELECT result_json FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        if not row:
            return None
        return ExecutionResultV1.model_validate_json(row[0])

    def get_by_job_id(self, job_id: str) -> ExecutionResultV1 | None:
        self.migrate()
        with self._lock, connection(self.path) as con:
            row = con.execute(
                "SELECT result_json FROM executions WHERE job_id = ? ORDER BY created_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        return ExecutionResultV1.model_validate_json(row[0])

    def raw_record(self, execution_id: str) -> dict[str, Any] | None:
        """Return the stored row for diagnostics and migration tests."""
        self.migrate()
        with self._lock, connection(self.path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return dict(row) if row else None
