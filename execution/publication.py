"""Fail-closed publication boundary for legacy run-directory views.

Current DAG runs are materialized before their canonical terminal snapshot is
committed. Legacy routes must therefore treat ``output/<timestamp>`` as staged
storage, not as publication authority. Unmarked runs without an execution
identifier predate the canonical service and retain historical behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from execution.artifacts import (
    ArtifactError,
    ArtifactManifestV1,
    ArtifactStore,
    normalize_relative_path,
)
from execution.service import get_execution_service

_TERMINAL_LIFECYCLES = {"completed", "failed", "cancelled", "interrupted"}
_CURRENT_PUBLICATION_BOUNDARY = "canonical_terminal_v1"


class LegacyRunNotPublished(RuntimeError):
    """A materialized run has not crossed its durable publication boundary."""


@dataclass(frozen=True)
class LegacyRunPublication:
    """Authorization context for one already-parsed legacy run log."""

    run_dir: Path
    execution_id: str | None = None
    artifacts: ArtifactStore | None = None
    manifest: ArtifactManifestV1 | None = None
    integrity_mode: Literal["historical", "legacy_live", "sealed"] = "historical"

    @property
    def historical(self) -> bool:
        return self.integrity_mode == "historical"

    @property
    def sealed(self) -> bool:
        return self.integrity_mode == "sealed"


def require_legacy_run_publication(
    run_dir: Path,
    log: dict,
) -> LegacyRunPublication:
    """Authorize a legacy run only after its canonical terminal commit.

    Artifact-root registration is checked before mutable log fields. A sealed
    root always requires its matching durable hash. Only genuinely unregistered
    pre-canonical runs and unmarked legacy-live roots retain live-file behavior.
    Any unavailable or corrupt authority fails closed without disclosing which
    check failed.
    """

    raw_execution_id = log.get("execution_id")
    marked_current = log.get("publication_boundary") == _CURRENT_PUBLICATION_BOUNDARY

    try:
        service = get_execution_service()
        artifacts = service.artifacts
        binding = artifacts.binding_for_root(run_dir)

        if binding is not None:
            if raw_execution_id != binding.execution_id:
                raise LegacyRunNotPublished
            durable = service.store.get(binding.execution_id)
            if durable is None or durable.lifecycle_status not in _TERMINAL_LIFECYCLES:
                raise LegacyRunNotPublished
            if binding.active or binding.manifest_state in {"active", "invalid"}:
                raise LegacyRunNotPublished

            if binding.manifest_state == "sealed":
                expected_hash = durable.sealed_manifest_hash
                if not expected_hash:
                    raise LegacyRunNotPublished
                manifest = artifacts.get_manifest(binding.execution_id)
                if (
                    manifest.integrity_mode != "sealed"
                    or manifest.manifest_hash != expected_hash
                ):
                    raise LegacyRunNotPublished
                log_path, _ = artifacts.resolve_entry(
                    binding.execution_id,
                    "full_log.json",
                )
                if log_path != (run_dir / "full_log.json").resolve():
                    raise LegacyRunNotPublished
                return LegacyRunPublication(
                    run_dir=run_dir,
                    execution_id=binding.execution_id,
                    artifacts=artifacts,
                    manifest=manifest,
                    integrity_mode="sealed",
                )

            if (
                binding.manifest_state != "legacy_live"
                or marked_current
                or durable.coordinator_restart_marker
                or durable.sealed_manifest_hash
            ):
                raise LegacyRunNotPublished
            return LegacyRunPublication(
                run_dir=run_dir,
                execution_id=binding.execution_id,
                integrity_mode="legacy_live",
            )

        if marked_current:
            raise LegacyRunNotPublished
        if raw_execution_id in (None, ""):
            return LegacyRunPublication(run_dir=run_dir)
        if not isinstance(raw_execution_id, str):
            raise LegacyRunNotPublished
        durable = service.store.get(raw_execution_id)
        if (
            durable is None
            or durable.lifecycle_status not in _TERMINAL_LIFECYCLES
            or durable.coordinator_restart_marker
            or durable.sealed_manifest_hash
        ):
            raise LegacyRunNotPublished
        return LegacyRunPublication(
            run_dir=run_dir,
            execution_id=raw_execution_id,
            integrity_mode="legacy_live",
        )
    except LegacyRunNotPublished:
        raise
    except (ArtifactError, OSError, TypeError, ValueError) as exc:
        raise LegacyRunNotPublished from exc


def published_file(
    publication: LegacyRunPublication,
    relative_path: str,
) -> Path | None:
    """Resolve one file inside an authorized run, checking sealed integrity."""

    try:
        normalized = normalize_relative_path(relative_path)
        if not publication.sealed:
            candidate = publication.run_dir.joinpath(*Path(normalized).parts)
            resolved = candidate.resolve()
            if not resolved.is_relative_to(publication.run_dir.resolve()):
                raise LegacyRunNotPublished
            return resolved if resolved.is_file() else None

        assert publication.execution_id is not None
        assert publication.artifacts is not None
        assert publication.manifest is not None
        if not any(
            entry.relative_path == normalized
            for entry in publication.manifest.entries
        ):
            return None
        path, _ = publication.artifacts.resolve_entry(
            publication.execution_id,
            normalized,
        )
        return path
    except LegacyRunNotPublished:
        raise
    except (ArtifactError, OSError, TypeError, ValueError) as exc:
        raise LegacyRunNotPublished from exc


def published_paths(
    publication: LegacyRunPublication,
    prefix: str,
) -> list[str]:
    """List authorized manifest paths below a directory-like prefix."""

    normalized_prefix = normalize_relative_path(prefix).rstrip("/") + "/"
    if not publication.sealed:
        directory = publication.run_dir.joinpath(*Path(normalized_prefix).parts)
        if not directory.is_dir():
            return []
        return [
            path.relative_to(publication.run_dir).as_posix()
            for path in sorted(directory.iterdir())
            if path.is_file()
        ]

    assert publication.manifest is not None
    return sorted(
        entry.relative_path
        for entry in publication.manifest.entries
        if entry.relative_path.startswith(normalized_prefix)
        and "/" not in entry.relative_path[len(normalized_prefix) :]
    )
