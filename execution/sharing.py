"""Durable, revocable capability tokens for deliberately shared executions."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from execution.artifacts import ArtifactManifestV1


class ShareModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateExecutionShareV1(ShareModel):
    expires_in_seconds: int | None = Field(default=7 * 24 * 3600, ge=60, le=30 * 24 * 3600)
    allow_artifact_download: bool = False
    redact_node_identity: bool = True
    include_candidate_details: bool = False


class CreatedExecutionShareV1(ShareModel):
    share_id: str
    execution_id: str
    token: str
    created_at: str
    expires_at: str | None
    allow_artifact_download: bool
    redact_node_identity: bool
    include_candidate_details: bool


class ExecutionShareRecordV1(ShareModel):
    share_id: str
    execution_id: str
    created_at: str
    expires_at: str | None
    revoked_at: str | None = None
    allow_artifact_download: bool
    redact_node_identity: bool
    include_candidate_details: bool


class PublicValidationSummaryV1(ShareModel):
    validator_name: str
    status: str
    score: float | None = None
    failure_reason: str | None = None


class PublicCandidateSummaryV1(ShareModel):
    candidate_id: str
    status: str
    output_bytes: int = 0
    output_preview: str = ""
    produced_files: list[str] = Field(default_factory=list)
    node_id: str | None = None
    validation: list[PublicValidationSummaryV1] = Field(default_factory=list)


class PublicExecutionShareV1(ShareModel):
    protocol_version: str = "1"
    share_id: str
    execution_id: str
    created_at: str
    expires_at: str | None
    lifecycle_status: str
    status: str
    task: str
    strategy_selected: str
    placement_selected: str | None = None
    validation_outcome: str | None = None
    assurance_level: str | None = None
    output_preview: str = ""
    winning_candidate: str | None = None
    produced_files: list[str] = Field(default_factory=list)
    validation: list[PublicValidationSummaryV1] = Field(default_factory=list)
    candidates: list[PublicCandidateSummaryV1] = Field(default_factory=list)
    participating_nodes: list[str] = Field(default_factory=list)
    artifact_download_allowed: bool = False
    artifacts_url: str | None = None


def _utc_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ShareStore:
    """SQLite share registry.  Only SHA-256 hashes of random tokens are stored."""

    def __init__(self, db_path: str = "events.db") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def migrate(self) -> None:
        with self._lock, sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_shares (
                    share_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    revoked_at TEXT,
                    allow_artifact_download INTEGER NOT NULL DEFAULT 0,
                    redact_node_identity INTEGER NOT NULL DEFAULT 1,
                    include_candidate_details INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_execution_shares_execution "
                "ON execution_shares(execution_id)"
            )
            con.commit()

    @staticmethod
    def _record(row) -> ExecutionShareRecordV1:
        return ExecutionShareRecordV1(
            share_id=row[0],
            execution_id=row[1],
            created_at=row[2],
            expires_at=row[3],
            revoked_at=row[4],
            allow_artifact_download=bool(row[5]),
            redact_node_identity=bool(row[6]),
            include_candidate_details=bool(row[7]),
        )

    def create(
        self,
        execution_id: str,
        options: CreateExecutionShareV1,
        *,
        now: datetime | None = None,
    ) -> CreatedExecutionShareV1:
        self.migrate()
        created = _utc_now(now)
        expires = (
            created + timedelta(seconds=options.expires_in_seconds)
            if options.expires_in_seconds is not None
            else None
        )
        for _ in range(4):
            token = secrets.token_urlsafe(32)
            share_id = f"share_{uuid.uuid4().hex}"
            try:
                with self._lock, sqlite3.connect(self.db_path) as con:
                    con.execute(
                        """
                        INSERT INTO execution_shares
                            (share_id, execution_id, token_hash, created_at, expires_at,
                             revoked_at, allow_artifact_download, redact_node_identity,
                             include_candidate_details)
                        VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
                        """,
                        (
                            share_id,
                            execution_id,
                            _token_hash(token),
                            created.isoformat(),
                            expires.isoformat() if expires else None,
                            int(options.allow_artifact_download),
                            int(options.redact_node_identity),
                            int(options.include_candidate_details),
                        ),
                    )
                    con.commit()
                return CreatedExecutionShareV1(
                    share_id=share_id,
                    execution_id=execution_id,
                    token=token,
                    created_at=created.isoformat(),
                    expires_at=expires.isoformat() if expires else None,
                    allow_artifact_download=options.allow_artifact_download,
                    redact_node_identity=options.redact_node_identity,
                    include_candidate_details=options.include_candidate_details,
                )
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("could not allocate a unique share token")

    def get_active(self, token: str, *, now: datetime | None = None) -> ExecutionShareRecordV1 | None:
        if not isinstance(token, str) or not (32 <= len(token) <= 128):
            return None
        digest = _token_hash(token)
        self.migrate()
        with self._lock, sqlite3.connect(self.db_path) as con:
            row = con.execute(
                """
                SELECT share_id, execution_id, created_at, expires_at, revoked_at,
                       allow_artifact_download, redact_node_identity, include_candidate_details,
                       token_hash
                FROM execution_shares WHERE token_hash = ?
                """,
                (digest,),
            ).fetchone()
        if not row or not secrets.compare_digest(str(row[8]), digest):
            return None
        record = self._record(row)
        if record.revoked_at:
            return None
        if record.expires_at and datetime.fromisoformat(record.expires_at) <= _utc_now(now):
            return None
        return record

    def revoke(
        self,
        execution_id: str,
        share_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        self.migrate()
        revoked = _utc_now(now).isoformat()
        with self._lock, sqlite3.connect(self.db_path) as con:
            cursor = con.execute(
                """
                UPDATE execution_shares SET revoked_at = ?
                WHERE share_id = ? AND execution_id = ? AND revoked_at IS NULL
                """,
                (revoked, share_id, execution_id),
            )
            con.commit()
        return cursor.rowcount == 1

    def token_is_stored_plaintext(self, token: str) -> bool:
        """Diagnostic/test helper proving the bearer token never enters SQLite."""
        self.migrate()
        with self._lock, sqlite3.connect(self.db_path) as con:
            row = con.execute(
                "SELECT 1 FROM execution_shares WHERE token_hash = ?", (token,)
            ).fetchone()
        return row is not None


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError("execution result must be a mapping or Pydantic model")


def _validation_summary(value: Any) -> PublicValidationSummaryV1:
    data = _as_mapping(value)
    status = str(data.get("status", "unknown"))
    return PublicValidationSummaryV1(
        validator_name=str(data.get("validator_name", "unknown")),
        status=status,
        score=data.get("score"),
        # Validator diagnostics may contain an internal filesystem path or an
        # exception string.  A public capability gets the outcome, not that
        # unbounded diagnostic text.
        failure_reason="validator reported failure" if status in ("failed", "error") else None,
    )


def artifact_manifest_for_share(
    manifest: ArtifactManifestV1,
    share: ExecutionShareRecordV1,
    execution: Any,
) -> ArtifactManifestV1:
    """Filter internal run records out of a capability-scoped artifact view."""
    data = _as_mapping(execution)
    winner = data.get("winning_candidate")
    entries = []
    for entry in manifest.entries:
        path = entry.relative_path
        name = path.rsplit("/", 1)[-1]
        internal_run_record = (
            name in {"full_log.json", "plan.json", "review.md"}
            or name.startswith("builder_")
            or "/transcript/" in f"/{path}/"
        )
        if internal_run_record:
            continue
        if name == "candidate.md" and not share.include_candidate_details:
            continue
        if (
            entry.source_candidate_id
            and not share.include_candidate_details
            and winner
            and entry.source_candidate_id != winner
        ):
            continue
        entries.append(entry)
    return ArtifactManifestV1(
        execution_id=manifest.execution_id,
        created_at=manifest.created_at,
        file_count=len(entries),
        aggregate_size_bytes=sum(entry.size_bytes for entry in entries),
        entries=entries,
    )


def redact_execution_for_share(
    execution: Any,
    share: ExecutionShareRecordV1,
    *,
    manifest: ArtifactManifestV1 | None = None,
    token: str | None = None,
) -> PublicExecutionShareV1:
    """Build an allowlist-based public view; private fields never flow through."""
    data = _as_mapping(execution)
    artifact_paths = [entry.relative_path for entry in manifest.entries] if manifest else []
    validation = [_validation_summary(item) for item in data.get("validation_evidence", [])]
    candidates: list[PublicCandidateSummaryV1] = []
    if share.include_candidate_details:
        entries_by_candidate: dict[str, list[str]] = {}
        if manifest:
            for entry in manifest.entries:
                if entry.source_candidate_id:
                    entries_by_candidate.setdefault(entry.source_candidate_id, []).append(entry.relative_path)
        for raw_candidate in data.get("candidates", []):
            candidate = _as_mapping(raw_candidate)
            candidate_id = str(candidate.get("candidate_id", "unknown"))
            candidates.append(
                PublicCandidateSummaryV1(
                    candidate_id=candidate_id,
                    status=str(candidate.get("status", "unknown")),
                    output_bytes=int(candidate.get("output_bytes", 0) or 0),
                    output_preview=str(candidate.get("output_preview", ""))[:500],
                    produced_files=sorted(entries_by_candidate.get(candidate_id, [])),
                    node_id=(
                        None
                        if share.redact_node_identity
                        else candidate.get("node_id")
                    ),
                    validation=[_validation_summary(item) for item in candidate.get("validation", [])],
                )
            )
    lifecycle = str(data.get("lifecycle_status") or data.get("status") or "unknown")
    artifacts_url = f"/v1/shares/{token}/artifacts" if token and share.allow_artifact_download else None
    return PublicExecutionShareV1(
        share_id=share.share_id,
        execution_id=share.execution_id,
        created_at=share.created_at,
        expires_at=share.expires_at,
        lifecycle_status=lifecycle,
        status=str(data.get("status") or lifecycle),
        task=str(data.get("task", "")),
        strategy_selected=str(data.get("strategy_selected", "unknown")),
        placement_selected=data.get("placement_selected"),
        validation_outcome=data.get("validation_outcome"),
        assurance_level=data.get("assurance_level"),
        output_preview=str(data.get("output_preview", ""))[:1000],
        winning_candidate=data.get("winning_candidate"),
        produced_files=sorted(artifact_paths),
        validation=validation,
        candidates=candidates,
        participating_nodes=(
            [] if share.redact_node_identity else [str(item) for item in data.get("participating_nodes", [])]
        ),
        artifact_download_allowed=share.allow_artifact_download,
        artifacts_url=artifacts_url,
    )


_SHARE_STORE: ShareStore | None = None


def get_share_store() -> ShareStore:
    global _SHARE_STORE
    if _SHARE_STORE is None:
        _SHARE_STORE = ShareStore()
    return _SHARE_STORE
