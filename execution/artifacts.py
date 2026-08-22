"""Durable, path-safe artifact manifests and streaming download preparation.

The execution service may write DAG output under ``output/`` and ensemble output
under ``execution_artifacts/``.  ``ArtifactStore`` is the common authority over
both: it records the internal root, publishes only normalized relative paths,
and re-checks confinement and symlinks every time a file is opened.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field

from config import get as get_config


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactEntryV1(ArtifactModel):
    relative_path: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=1, max_length=200)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_candidate_id: str | None = Field(default=None, max_length=128)
    source_execution_unit_id: str | None = Field(default=None, max_length=128)
    created_at: str


class ArtifactManifestV1(ArtifactModel):
    protocol_version: str = "1"
    execution_id: str = Field(min_length=1, max_length=128)
    created_at: str
    file_count: int = Field(ge=0)
    aggregate_size_bytes: int = Field(ge=0)
    entries: list[ArtifactEntryV1]


class ArtifactError(RuntimeError):
    """Base class safe for routes to map without exposing server paths."""


class ArtifactNotFound(ArtifactError):
    pass


class ArtifactSecurityError(ArtifactError):
    pass


class ArtifactLimitError(ArtifactError):
    pass


@dataclass(frozen=True)
class ArtifactSource:
    candidate_id: str | None = None
    execution_unit_id: str | None = None


@dataclass(frozen=True)
class PreparedArtifactArchive:
    path: Path
    download_name: str
    size_bytes: int


def _utc_iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() if timestamp is None else timestamp,
        timezone.utc,
    ).isoformat()


def normalize_relative_path(value: str) -> str:
    """Return one canonical POSIX relative path or reject it.

    URL decoding is deliberately repeated a small bounded number of times so
    encoded and double-encoded traversal cannot become meaningful at a later
    layer.  Literal backslashes and colons are rejected to keep the same rules
    on POSIX and Windows and to avoid NTFS alternate data streams.
    """
    if not isinstance(value, str) or not value or len(value) > 500 or "\x00" in value:
        raise ArtifactSecurityError("invalid artifact path")
    decoded = value
    for _ in range(3):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    if not decoded or "\\" in decoded or ":" in decoded or "\ufffd" in decoded:
        raise ArtifactSecurityError("invalid artifact path")
    if decoded.startswith(("/", "//")) or PureWindowsPath(decoded).is_absolute():
        raise ArtifactSecurityError("artifact paths must be relative")
    path = PurePosixPath(decoded)
    if any(part in ("", ".", "..") for part in path.parts):
        raise ArtifactSecurityError("artifact path traversal is not allowed")
    normalized = path.as_posix()
    if normalized != decoded:
        raise ArtifactSecurityError("artifact path is not normalized")
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_int(value: Any, fallback: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= minimum else fallback


class ArtifactStore:
    """SQLite-backed registry for execution artifact roots and manifests."""

    def __init__(
        self,
        db_path: str | Path = "events.db",
        *,
        allowed_roots: list[str | Path] | None = None,
        max_files: int | None = None,
        max_file_bytes: int | None = None,
        max_aggregate_bytes: int | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        roots = allowed_roots or [Path("output"), Path("execution_artifacts")]
        self.allowed_roots = tuple(Path(root).resolve() for root in roots)
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_aggregate_bytes = max_aggregate_bytes
        self._lock = threading.RLock()

    def _limits(self) -> tuple[int, int, int]:
        cfg = get_config()
        max_files = self._max_files or _safe_int(cfg.get("artifact_max_files"), 100)
        max_file = self._max_file_bytes or _safe_int(
            cfg.get("artifact_max_file_bytes"), 50 * 1024 * 1024
        )
        max_total = self._max_aggregate_bytes or _safe_int(
            cfg.get("artifact_max_aggregate_bytes"), 100 * 1024 * 1024
        )
        return max_files, max_file, max_total

    def migrate(self) -> None:
        with self._lock, sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS artifact_roots (
                    execution_id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL,
                    strategy TEXT,
                    active INTEGER NOT NULL DEFAULT 0,
                    manifest_prefix TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS artifact_entries (
                    execution_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    source_candidate_id TEXT,
                    source_execution_unit_id TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (execution_id, relative_path),
                    FOREIGN KEY (execution_id) REFERENCES artifact_roots(execution_id)
                        ON DELETE CASCADE
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifact_roots_updated ON artifact_roots(updated_at)"
            )
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_roots_path ON artifact_roots(root_path)"
            )
            root_columns = {
                row[1] for row in con.execute("PRAGMA table_info(artifact_roots)")
            }
            if "manifest_prefix" not in root_columns:
                con.execute("ALTER TABLE artifact_roots ADD COLUMN manifest_prefix TEXT")
            con.commit()

    @staticmethod
    def _validate_execution_id(execution_id: str) -> str:
        if not isinstance(execution_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", execution_id):
            raise ArtifactSecurityError("invalid execution identifier")
        return execution_id

    def _allowed_root_for(self, resolved: Path) -> Path:
        for allowed in self.allowed_roots:
            if resolved != allowed and resolved.is_relative_to(allowed):
                return allowed
        raise ArtifactSecurityError("artifact root is outside configured storage")

    @staticmethod
    def _reject_symlink_components(path: Path, base: Path) -> None:
        current = base
        if current.is_symlink():
            raise ArtifactSecurityError("symlink artifact roots are not allowed")
        relative = path.relative_to(base)
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ArtifactSecurityError("symlink artifacts are not allowed")

    def _validated_existing_root(self, root: str | Path) -> Path:
        candidate = Path(root)
        if not candidate.exists() or not candidate.is_dir() or candidate.is_symlink():
            raise ArtifactSecurityError("artifact root must be an existing non-symlink directory")
        lexical = candidate.absolute()
        lexical_base = next(
            (
                allowed
                for allowed in self.allowed_roots
                if lexical != allowed and lexical.is_relative_to(allowed)
            ),
            None,
        )
        if lexical_base is None:
            raise ArtifactSecurityError("artifact root is outside configured storage")
        self._reject_symlink_components(lexical, lexical_base)
        resolved = candidate.resolve(strict=True)
        allowed = self._allowed_root_for(resolved)
        self._reject_symlink_components(resolved, allowed)
        return resolved

    def register_root(
        self,
        execution_id: str,
        root: str | Path,
        *,
        strategy: str | None = None,
        active: bool = False,
    ) -> None:
        """Register an internal root without exposing it in any API model."""
        execution_id = self._validate_execution_id(execution_id)
        resolved = self._validated_existing_root(root)
        now = _utc_iso()
        self.migrate()
        with self._lock, sqlite3.connect(self.db_path) as con:
            existing = con.execute(
                "SELECT root_path FROM artifact_roots WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if existing and Path(existing[0]) != resolved:
                raise ArtifactSecurityError("an execution cannot change artifact roots")
            try:
                con.execute(
                    """
                    INSERT INTO artifact_roots
                        (execution_id, root_path, strategy, active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(execution_id) DO UPDATE SET
                        strategy=COALESCE(excluded.strategy, artifact_roots.strategy),
                        active=excluded.active,
                        updated_at=excluded.updated_at
                    """,
                    (execution_id, str(resolved), strategy, int(active), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ArtifactSecurityError(
                    "an artifact root cannot belong to multiple executions"
                ) from exc
            con.commit()

    def set_active(self, execution_id: str, active: bool) -> None:
        execution_id = self._validate_execution_id(execution_id)
        self.migrate()
        with self._lock, sqlite3.connect(self.db_path) as con:
            cursor = con.execute(
                "UPDATE artifact_roots SET active = ?, updated_at = ? WHERE execution_id = ?",
                (int(active), _utc_iso(), execution_id),
            )
            con.commit()
        if cursor.rowcount == 0:
            raise ArtifactNotFound("artifact root is not registered")

    def _validated_subtree(self, root: Path, prefix: str) -> tuple[str, Path]:
        normalized = normalize_relative_path(prefix)
        candidate = root.joinpath(*PurePosixPath(normalized).parts)
        self._reject_symlink_components(candidate, root)
        if candidate.is_symlink():
            raise ArtifactSecurityError("symlink artifact directories are not allowed")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_dir():
            raise ArtifactSecurityError("artifact subtree escaped its registered root")
        return normalized, resolved

    def set_manifest_prefix(self, execution_id: str, prefix: str) -> None:
        """Publish only one validated subtree of an execution artifact root."""
        execution_id = self._validate_execution_id(execution_id)
        root, _, _ = self._root_record(execution_id)
        normalized, _ = self._validated_subtree(root, prefix)
        with self._lock, sqlite3.connect(self.db_path) as con:
            cursor = con.execute(
                "UPDATE artifact_roots SET manifest_prefix = ?, updated_at = ? "
                "WHERE execution_id = ?",
                (normalized, _utc_iso(), execution_id),
            )
            con.commit()
        if cursor.rowcount == 0:
            raise ArtifactNotFound("artifact root is not registered")

    def active_root_paths(self) -> set[Path]:
        """Internal resolved roots that retention code must never prune."""
        self.migrate()
        with self._lock, sqlite3.connect(self.db_path) as con:
            rows = con.execute(
                "SELECT root_path FROM artifact_roots WHERE active = 1"
            ).fetchall()
        active: set[Path] = set()
        for (root_path,) in rows:
            try:
                active.add(self._validated_existing_root(root_path))
            except ArtifactError:
                # A missing/corrupt root is not converted into a broader path;
                # it remains absent until reconciliation can inspect the record.
                continue
        return active

    def _root_record(self, execution_id: str) -> tuple[Path, str, bool]:
        execution_id = self._validate_execution_id(execution_id)
        self.migrate()
        with self._lock, sqlite3.connect(self.db_path) as con:
            row = con.execute(
                "SELECT root_path, created_at, active FROM artifact_roots WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        if not row:
            raise ArtifactNotFound("artifacts are not registered for this execution")
        root = self._validated_existing_root(row[0])
        return root, str(row[1]), bool(row[2])

    @staticmethod
    def _infer_source(relative_path: str) -> ArtifactSource:
        first = PurePosixPath(relative_path).parts[0]
        match = re.fullmatch(r"candidate_(\d+)", first)
        return ArtifactSource(candidate_id=f"candidate-{match.group(1)}") if match else ArtifactSource()

    def _scan_entries(
        self,
        root: Path,
        scan_root: Path,
        *,
        sources: Mapping[str, ArtifactSource] | None = None,
    ) -> tuple[list[ArtifactEntryV1], int]:
        """Inspect one root/subtree without trusting directory contents."""
        max_files, max_file_bytes, max_total_bytes = self._limits()
        entries: list[ArtifactEntryV1] = []
        aggregate = 0
        normalized_seen: set[str] = set()

        for directory, directory_names, file_names in os.walk(scan_root, followlinks=False):
            directory_path = Path(directory)
            for name in list(directory_names):
                if (directory_path / name).is_symlink():
                    raise ArtifactSecurityError("symlink artifacts are not allowed")
            for name in sorted(file_names):
                path = directory_path / name
                if path.is_symlink():
                    raise ArtifactSecurityError("symlink artifacts are not allowed")
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    raise ArtifactSecurityError("artifact escaped its registered root")
                self._reject_symlink_components(resolved, root)
                relative_path = normalize_relative_path(resolved.relative_to(root).as_posix())
                collision_key = relative_path.casefold()
                if collision_key in normalized_seen:
                    raise ArtifactSecurityError("duplicate normalized artifact paths")
                normalized_seen.add(collision_key)
                stat = resolved.stat()
                if stat.st_size > max_file_bytes:
                    raise ArtifactLimitError("an artifact exceeds the per-file byte limit")
                aggregate += stat.st_size
                if len(entries) + 1 > max_files:
                    raise ArtifactLimitError("artifact file-count limit exceeded")
                if aggregate > max_total_bytes:
                    raise ArtifactLimitError("artifact aggregate-byte limit exceeded")
                media_type = mimetypes.guess_type(relative_path)[0] or "application/octet-stream"
                source = (sources or {}).get(relative_path) or self._infer_source(relative_path)
                entries.append(
                    ArtifactEntryV1(
                        relative_path=relative_path,
                        media_type=media_type,
                        size_bytes=stat.st_size,
                        sha256=_sha256_file(resolved),
                        source_candidate_id=source.candidate_id,
                        source_execution_unit_id=source.execution_unit_id,
                        created_at=_utc_iso(stat.st_mtime),
                    )
                )

        entries.sort(key=lambda item: item.relative_path)
        return entries, aggregate

    def validate_subtree(self, execution_id: str, prefix: str) -> list[ArtifactEntryV1]:
        """Validate a candidate subtree without publishing it as the manifest."""
        root, _, _ = self._root_record(execution_id)
        _, scan_root = self._validated_subtree(root, prefix)
        entries, _ = self._scan_entries(root, scan_root)
        return entries

    def refresh_manifest(
        self,
        execution_id: str,
        *,
        sources: Mapping[str, ArtifactSource] | None = None,
    ) -> ArtifactManifestV1:
        """Rebuild a manifest from disk after enforcing all configured quotas."""
        root, manifest_created_at, _ = self._root_record(execution_id)
        with self._lock, sqlite3.connect(self.db_path) as con:
            prefix_row = con.execute(
                "SELECT manifest_prefix FROM artifact_roots WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        prefix = prefix_row[0] if prefix_row else None
        scan_root = root
        if prefix:
            _, scan_root = self._validated_subtree(root, prefix)
        entries, aggregate = self._scan_entries(root, scan_root, sources=sources)
        with self._lock, sqlite3.connect(self.db_path) as con:
            con.execute("DELETE FROM artifact_entries WHERE execution_id = ?", (execution_id,))
            con.executemany(
                """
                INSERT INTO artifact_entries
                    (execution_id, relative_path, media_type, size_bytes, sha256,
                     source_candidate_id, source_execution_unit_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        execution_id,
                        item.relative_path,
                        item.media_type,
                        item.size_bytes,
                        item.sha256,
                        item.source_candidate_id,
                        item.source_execution_unit_id,
                        item.created_at,
                    )
                    for item in entries
                ],
            )
            con.execute(
                "UPDATE artifact_roots SET updated_at = ? WHERE execution_id = ?",
                (_utc_iso(), execution_id),
            )
            con.commit()
        return ArtifactManifestV1(
            execution_id=execution_id,
            created_at=manifest_created_at,
            file_count=len(entries),
            aggregate_size_bytes=aggregate,
            entries=entries,
        )

    def resolve_entry(self, execution_id: str, relative_path: str) -> tuple[Path, ArtifactEntryV1]:
        normalized = normalize_relative_path(relative_path)
        manifest = self.refresh_manifest(execution_id)
        entry = next((item for item in manifest.entries if item.relative_path == normalized), None)
        if entry is None:
            raise ArtifactNotFound("artifact was not found")
        root, _, _ = self._root_record(execution_id)
        candidate = root.joinpath(*PurePosixPath(normalized).parts)
        self._reject_symlink_components(candidate, root)
        if candidate.is_symlink():
            raise ArtifactSecurityError("symlink artifacts are not allowed")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ArtifactSecurityError("artifact escaped its registered root")
        if _sha256_file(resolved) != entry.sha256:
            raise ArtifactSecurityError("artifact changed while it was being resolved")
        return resolved, entry

    def prepare_archive(
        self,
        execution_id: str,
        *,
        relative_paths: set[str] | None = None,
    ) -> PreparedArtifactArchive:
        """Write a bounded ZIP to a temporary file for FileResponse streaming."""
        manifest = self.refresh_manifest(execution_id)
        if relative_paths is not None:
            normalized_paths = {normalize_relative_path(path) for path in relative_paths}
            manifest.entries = [
                entry for entry in manifest.entries if entry.relative_path in normalized_paths
            ]
            manifest.file_count = len(manifest.entries)
            manifest.aggregate_size_bytes = sum(entry.size_bytes for entry in manifest.entries)
        descriptor, temp_name = tempfile.mkstemp(prefix="mycelium-artifacts-", suffix=".zip")
        os.close(descriptor)
        archive_path = Path(temp_name)
        try:
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for entry in manifest.entries:
                    path, resolved_entry = self.resolve_entry(execution_id, entry.relative_path)
                    if resolved_entry.sha256 != entry.sha256:
                        raise ArtifactSecurityError("artifact changed during archive creation")
                    archive.write(path, arcname=entry.relative_path)
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise
        return PreparedArtifactArchive(
            path=archive_path,
            download_name=f"execution_{execution_id}_artifacts.zip",
            size_bytes=archive_path.stat().st_size,
        )

    def prune(
        self,
        *,
        active_execution_ids: set[str] | None = None,
        retention_seconds: int | None = None,
        max_total_bytes: int | None = None,
        now: float | None = None,
    ) -> list[str]:
        """Delete expired/oldest registered terminal roots within safe bases.

        Both the durable ``active`` bit and the caller's current active set are
        honored.  The registered root is revalidated before deletion and an
        allowed storage base itself can never be a target.
        """
        self.migrate()
        cfg = get_config()
        retention = _safe_int(
            cfg.get("artifact_retention_seconds") if retention_seconds is None else retention_seconds,
            7 * 24 * 3600,
        )
        if max_total_bytes is None:
            max_mb = _safe_int(cfg.get("execution_artifacts_max_mb"), 500)
            byte_cap = max_mb * 1024 * 1024
        else:
            byte_cap = max(0, int(max_total_bytes))
        current = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
        active_ids = active_execution_ids or set()

        with self._lock, sqlite3.connect(self.db_path) as con:
            rows = con.execute(
                "SELECT execution_id, root_path, active, updated_at FROM artifact_roots ORDER BY updated_at"
            ).fetchall()

        candidates: list[tuple[str, Path, int, float]] = []
        total = 0
        for execution_id, root_path, active, updated_at in rows:
            try:
                root = self._validated_existing_root(root_path)
                size = sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())
                age = (current - datetime.fromisoformat(updated_at)).total_seconds()
            except (ArtifactError, OSError, ValueError):
                continue
            total += size
            if bool(active) or execution_id in active_ids:
                continue
            candidates.append((execution_id, root, size, age))

        deleted: list[str] = []
        for execution_id, root, size, age in candidates:
            if age < retention and (byte_cap == 0 or total <= byte_cap):
                continue
            # Validation above guarantees root is a strict child of an allowed
            # base.  Re-check immediately before the destructive operation.
            self._validated_existing_root(root)
            shutil.rmtree(root)
            total -= size
            deleted.append(execution_id)
            with self._lock, sqlite3.connect(self.db_path) as con:
                con.execute("DELETE FROM artifact_entries WHERE execution_id = ?", (execution_id,))
                con.execute("DELETE FROM artifact_roots WHERE execution_id = ?", (execution_id,))
                con.commit()
        return deleted


_ARTIFACT_STORE: ArtifactStore | None = None


def get_artifact_store() -> ArtifactStore:
    global _ARTIFACT_STORE
    if _ARTIFACT_STORE is None:
        _ARTIFACT_STORE = ArtifactStore()
    return _ARTIFACT_STORE
