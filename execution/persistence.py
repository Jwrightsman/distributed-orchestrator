"""SQLite persistence for canonical executions.

The worker queue remains process-local in protocol v1. This store makes the
canonical request, strategy decision, placement metadata, and final normalized
result durable without claiming scheduler durability.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from execution.contracts import ExecutionRequestV1, ExecutionResultV1


class ExecutionStore:
    def __init__(self, path: str | Path = "events.db"):
        self.path = Path(path)
        self._lock = threading.RLock()

    def migrate(self) -> None:
        """Create the v1 execution table idempotently, preserving old tables."""
        with self._lock, sqlite3.connect(self.path) as con:
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
                    error_json         TEXT NOT NULL
                )
                """
            )
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
        with self._lock, sqlite3.connect(self.path) as con:
            con.execute(
                """
                INSERT INTO executions (
                    execution_id, job_id, protocol_version, request_json,
                    strategy_requested, strategy_selected, strategy_version,
                    strategy_options, selector_reason, selector_version,
                    placement_requested, placement_selected, fallback_reason,
                    status, created_at, started_at, completed_at, result_json,
                    candidate_summaries, validation_summaries, error_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    error_json=excluded.error_json
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
                ),
            )
            con.commit()

    def get(self, execution_id: str) -> ExecutionResultV1 | None:
        self.migrate()
        with self._lock, sqlite3.connect(self.path) as con:
            row = con.execute(
                "SELECT result_json FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        if not row:
            return None
        return ExecutionResultV1.model_validate_json(row[0])

    def get_by_job_id(self, job_id: str) -> ExecutionResultV1 | None:
        self.migrate()
        with self._lock, sqlite3.connect(self.path) as con:
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
        with self._lock, sqlite3.connect(self.path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return dict(row) if row else None
