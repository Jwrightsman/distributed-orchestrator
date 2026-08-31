"""Copy validator inputs into a bounded, path-safe staging directory.

The validator runner must never receive the coordinator's live artifact paths.
This module narrows a registered/validated subtree to an explicit file list and
copies those bytes into a fresh caller-owned directory.  It deliberately does
not create hard links, preserve source metadata, or return host paths.

The caller remains responsible for deleting a successful stage after the child
process has terminated.  A failed staging attempt removes every partial copy.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterable, Iterator, Sequence

from execution.artifacts import ArtifactEntryV1, ArtifactSecurityError, normalize_relative_path


_COPY_CHUNK_BYTES = 1024 * 1024
_HARD_MAX_FILES = 100
_HARD_MAX_FILE_BYTES = 50 * 1024 * 1024
_HARD_MAX_AGGREGATE_BYTES = 100 * 1024 * 1024
_HARD_MAX_RELATIVE_PATH_LENGTH = 500


class ValidatorStagingError(RuntimeError):
    """Base error whose message does not expose an absolute host path."""


class ValidatorStagingSecurityError(ValidatorStagingError):
    """A source or destination path violated the staging boundary."""


class ValidatorStagingLimitError(ValidatorStagingError):
    """The selected input set exceeded a staging limit."""


class ValidatorStagingIntegrityError(ValidatorStagingSecurityError):
    """Live source bytes did not match the authoritative entry snapshot."""


class ValidatorStagingCleanupError(ValidatorStagingError):
    """A partial or completed validator stage could not be removed."""


class ValidatorStagingAborted(ValidatorStagingError):
    """Staging stopped because the owning execution was cancelled or expired."""

    def __init__(self, reason: str) -> None:
        if reason not in {"validator_cancelled", "validator_timeout"}:
            raise ValueError("unsupported validator staging abort reason")
        self.reason = reason
        super().__init__(reason)


def _raise_if_aborted(abort_reason: Callable[[], str | None] | None) -> None:
    if abort_reason is None:
        return
    reason = abort_reason()
    if reason is not None:
        raise ValidatorStagingAborted(reason)


@dataclass(frozen=True)
class StagingLimits:
    """Strict limits for one validator input stage.

    The hard ceilings match the canonical artifact registry ceilings.  Runner
    configuration may choose smaller values, but cannot turn this helper into an
    unbounded copy operation.
    """

    max_files: int = 20
    max_file_bytes: int = 10 * 1024 * 1024
    max_aggregate_bytes: int = 10 * 1024 * 1024
    max_relative_path_length: int = 200

    def __post_init__(self) -> None:
        bounds = (
            ("max_files", self.max_files, _HARD_MAX_FILES),
            ("max_file_bytes", self.max_file_bytes, _HARD_MAX_FILE_BYTES),
            ("max_aggregate_bytes", self.max_aggregate_bytes, _HARD_MAX_AGGREGATE_BYTES),
            (
                "max_relative_path_length",
                self.max_relative_path_length,
                _HARD_MAX_RELATIVE_PATH_LENGTH,
            ),
        )
        for name, value, maximum in bounds:
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")


@dataclass(frozen=True)
class _SelectedSource:
    source_path: Path
    staged_relative_path: str
    root_relative_path: str
    source_stat: os.stat_result
    claim: ArtifactEntryV1 | None


def _is_reparse_or_link(path: Path, status: os.stat_result) -> bool:
    if stat.S_ISLNK(status.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if reparse_flag and getattr(status, "st_file_attributes", 0) & reparse_flag:
        return True
    if os.name == "nt" and hasattr(path, "is_junction"):
        try:
            return path.is_junction()
        except OSError:
            return True
    return False


def _safe_lstat(path: Path, *, kind: str) -> os.stat_result:
    try:
        status = path.lstat()
    except OSError as exc:
        raise ValidatorStagingSecurityError(f"{kind} is unavailable") from exc
    if _is_reparse_or_link(path, status):
        raise ValidatorStagingSecurityError(f"{kind} cannot be a symlink or reparse point")
    return status


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _validate_root(root: str | Path) -> tuple[Path, os.stat_result]:
    candidate = Path(root)
    status = _safe_lstat(candidate, kind="authoritative root")
    if not stat.S_ISDIR(status.st_mode):
        raise ValidatorStagingSecurityError("authoritative root must be a directory")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValidatorStagingSecurityError("authoritative root is unavailable") from exc
    resolved_status = _safe_lstat(resolved, kind="authoritative root")
    if not stat.S_ISDIR(resolved_status.st_mode):
        raise ValidatorStagingSecurityError("authoritative root must be a directory")
    return resolved, resolved_status


def _portable_relative(path: Path, base: Path, *, kind: str) -> str:
    try:
        relative = path.relative_to(base).as_posix()
    except ValueError as exc:
        raise ValidatorStagingSecurityError(f"{kind} escaped its authoritative boundary") from exc
    if not relative:
        raise ValidatorStagingSecurityError(f"{kind} must name a file below its boundary")
    try:
        return normalize_relative_path(relative)
    except ArtifactSecurityError as exc:
        raise ValidatorStagingSecurityError(f"{kind} is not a safe relative path") from exc


def _validate_directory_chain(base: Path, directory: Path, *, kind: str) -> os.stat_result:
    try:
        relative = directory.relative_to(base)
    except ValueError as exc:
        raise ValidatorStagingSecurityError(f"{kind} escaped the authoritative root") from exc

    current = base
    status = _safe_lstat(current, kind="authoritative root")
    if not stat.S_ISDIR(status.st_mode):
        raise ValidatorStagingSecurityError("authoritative root must be a directory")
    for part in relative.parts:
        current = current / part
        status = _safe_lstat(current, kind=kind)
        if not stat.S_ISDIR(status.st_mode):
            raise ValidatorStagingSecurityError(f"{kind} must be a directory")
    return status


def _resolve_subtree(root: Path, subtree: str | Path | None) -> tuple[Path, os.stat_result]:
    if subtree is None:
        return root, _validate_directory_chain(root, root, kind="authoritative subtree")

    raw = Path(subtree)
    lexical = raw.absolute() if raw.is_absolute() else root / raw
    if lexical == root:
        return root, _validate_directory_chain(root, root, kind="authoritative subtree")
    relative = _portable_relative(lexical, root, kind="authoritative subtree")
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    status = _validate_directory_chain(root, candidate, kind="authoritative subtree")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValidatorStagingSecurityError("authoritative subtree is unavailable") from exc
    if not resolved.is_relative_to(root):
        raise ValidatorStagingSecurityError("authoritative subtree escaped the authoritative root")
    return resolved, status


def _validate_source_chain(subtree: Path, source: Path) -> os.stat_result:
    try:
        relative = source.relative_to(subtree)
    except ValueError as exc:
        raise ValidatorStagingSecurityError("selected input escaped the authoritative subtree") from exc

    current = subtree
    for part in relative.parts[:-1]:
        current = current / part
        status = _safe_lstat(current, kind="selected input directory")
        if not stat.S_ISDIR(status.st_mode):
            raise ValidatorStagingSecurityError("selected input parent must be a directory")
    status = _safe_lstat(source, kind="selected input")
    if not stat.S_ISREG(status.st_mode):
        raise ValidatorStagingSecurityError("selected input must be a regular file")
    return status


def _claim_map(entries: Iterable[ArtifactEntryV1] | None) -> dict[str, ArtifactEntryV1] | None:
    if entries is None:
        return None
    claims: dict[str, ArtifactEntryV1] = {}
    portable_seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, ArtifactEntryV1):
            raise TypeError("validated_entries must contain ArtifactEntryV1 values")
        try:
            relative_path = normalize_relative_path(entry.relative_path)
        except ArtifactSecurityError as exc:
            raise ValidatorStagingSecurityError("authoritative entry has an unsafe path") from exc
        portable_key = relative_path.casefold()
        if portable_key in portable_seen:
            raise ValidatorStagingSecurityError("authoritative entries contain duplicate paths")
        portable_seen.add(portable_key)
        claims[relative_path] = entry
    return claims


def _select_sources(
    *,
    root: Path,
    subtree: Path,
    selected_files: Sequence[str | Path],
    claims: dict[str, ArtifactEntryV1] | None,
    limits: StagingLimits,
) -> list[_SelectedSource]:
    if len(selected_files) > limits.max_files:
        raise ValidatorStagingLimitError("validator staging file-count limit exceeded")

    selected: list[_SelectedSource] = []
    portable_seen: set[str] = set()
    for raw_value in selected_files:
        raw = Path(raw_value)
        lexical = raw.absolute() if raw.is_absolute() else subtree / raw
        staged_relative = _portable_relative(lexical, subtree, kind="selected input")
        if len(staged_relative) > limits.max_relative_path_length:
            raise ValidatorStagingLimitError("validator staging relative-path limit exceeded")
        portable_key = staged_relative.casefold()
        if portable_key in portable_seen:
            raise ValidatorStagingSecurityError("selected inputs contain duplicate paths")
        portable_seen.add(portable_key)

        source = subtree.joinpath(*PurePosixPath(staged_relative).parts)
        source_stat = _validate_source_chain(subtree, source)
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise ValidatorStagingSecurityError("selected input is unavailable") from exc
        if not resolved.is_relative_to(subtree):
            raise ValidatorStagingSecurityError("selected input escaped the authoritative subtree")
        root_relative = _portable_relative(resolved, root, kind="selected input")
        claim = None
        if claims is not None:
            claim = claims.get(root_relative)
            if claim is None:
                raise ValidatorStagingIntegrityError(
                    "selected input is absent from the authoritative entry snapshot"
                )
        selected.append(
            _SelectedSource(
                source_path=resolved,
                staged_relative_path=staged_relative,
                root_relative_path=root_relative,
                source_stat=source_stat,
                claim=claim,
            )
        )
    return selected


_SECURE_DIRFD_OPEN = (
    os.name == "posix"
    and os.open in getattr(os, "supports_dir_fd", set())
    and bool(getattr(os, "O_NOFOLLOW", 0))
    and bool(getattr(os, "O_DIRECTORY", 0))
)


@contextmanager
def _open_source(
    subtree: Path,
    subtree_stat: os.stat_result,
    selected: _SelectedSource,
) -> Iterator[BinaryIO]:
    read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    if _SECURE_DIRFD_OPEN:
        directory_flags = (
            read_flags
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0)
        )
        file_flags = read_flags | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        directory_fd: int | None = None
        file_fd: int | None = None
        try:
            directory_fd = os.open(subtree, directory_flags)
            opened_subtree = os.fstat(directory_fd)
            if not stat.S_ISDIR(opened_subtree.st_mode) or not _same_file_identity(
                subtree_stat, opened_subtree
            ):
                raise ValidatorStagingSecurityError("authoritative subtree identity changed")

            parts = PurePosixPath(selected.staged_relative_path).parts
            for part in parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                opened_directory = os.fstat(next_fd)
                if not stat.S_ISDIR(opened_directory.st_mode):
                    os.close(next_fd)
                    raise ValidatorStagingSecurityError("selected input parent must be a directory")
                os.close(directory_fd)
                directory_fd = next_fd

            file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
            opened_file = os.fstat(file_fd)
            if not stat.S_ISREG(opened_file.st_mode):
                raise ValidatorStagingSecurityError("selected input must be a regular file")
            if not _same_file_identity(selected.source_stat, opened_file):
                raise ValidatorStagingSecurityError("selected input identity changed")
            handle = os.fdopen(file_fd, "rb", closefd=True)
            file_fd = None
            try:
                yield handle
            finally:
                handle.close()
        except OSError as exc:
            raise ValidatorStagingSecurityError("selected input could not be opened safely") from exc
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)
        return

    # Windows does not expose O_NOFOLLOW/openat through the standard library.
    # Reparse components were rejected above; fstat identity and regular-file
    # checks make the remaining race best effort and are documented as such.
    file_fd = None
    try:
        file_fd = os.open(selected.source_path, read_flags)
        opened_file = os.fstat(file_fd)
        if not stat.S_ISREG(opened_file.st_mode):
            raise ValidatorStagingSecurityError("selected input must be a regular file")
        if not _same_file_identity(selected.source_stat, opened_file):
            raise ValidatorStagingSecurityError("selected input identity changed")
        handle = os.fdopen(file_fd, "rb", closefd=True)
        file_fd = None
        try:
            yield handle
        finally:
            handle.close()
    except OSError as exc:
        raise ValidatorStagingSecurityError("selected input could not be opened safely") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)


def _create_staging_root(value: str | Path) -> Path:
    requested = Path(value)
    if not requested.name or requested.name in {".", ".."}:
        raise ValidatorStagingSecurityError("staging root must name a fresh directory")
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError as exc:
        raise ValidatorStagingSecurityError("staging root parent is unavailable") from exc
    parent_status = _safe_lstat(parent, kind="staging root parent")
    if not stat.S_ISDIR(parent_status.st_mode):
        raise ValidatorStagingSecurityError("staging root parent must be a directory")
    staging_root = parent / requested.name
    try:
        staging_root.mkdir(mode=0o700)
        os.chmod(staging_root, 0o700)
    except FileExistsError as exc:
        raise ValidatorStagingSecurityError("staging root must not already exist") from exc
    except OSError as exc:
        raise ValidatorStagingSecurityError("staging root could not be created") from exc
    return staging_root


def _create_destination_parent(staging_root: Path, relative_path: str) -> Path:
    current = staging_root
    for part in PurePosixPath(relative_path).parts[:-1]:
        current = current / part
        try:
            current.mkdir(mode=0o700)
            os.chmod(current, 0o700)
        except FileExistsError:
            status = _safe_lstat(current, kind="staging directory")
            if not stat.S_ISDIR(status.st_mode):
                raise ValidatorStagingSecurityError("staging parent must be a directory")
        except OSError as exc:
            raise ValidatorStagingSecurityError("staging directory could not be created") from exc
    return current


def _copy_one(
    *,
    source: BinaryIO,
    selected: _SelectedSource,
    destination: Path,
    aggregate_before: int,
    limits: StagingLimits,
    abort_reason: Callable[[], str | None] | None,
) -> tuple[int, str]:
    _raise_if_aborted(abort_reason)
    initial = os.fstat(source.fileno())
    if not stat.S_ISREG(initial.st_mode):
        raise ValidatorStagingSecurityError("selected input must be a regular file")
    if initial.st_size > limits.max_file_bytes:
        raise ValidatorStagingLimitError("validator staging per-file byte limit exceeded")
    if aggregate_before + initial.st_size > limits.max_aggregate_bytes:
        raise ValidatorStagingLimitError("validator staging aggregate-byte limit exceeded")

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    destination_fd: int | None = None
    digest = hashlib.sha256()
    copied = 0
    try:
        destination_fd = os.open(destination, flags, 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(destination_fd, 0o600)
        with os.fdopen(destination_fd, "wb", closefd=True) as target:
            destination_fd = None
            while True:
                _raise_if_aborted(abort_reason)
                chunk = source.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                next_file_size = copied + len(chunk)
                next_aggregate = aggregate_before + next_file_size
                if next_file_size > limits.max_file_bytes:
                    raise ValidatorStagingLimitError(
                        "validator staging per-file byte limit exceeded"
                    )
                if next_aggregate > limits.max_aggregate_bytes:
                    raise ValidatorStagingLimitError(
                        "validator staging aggregate-byte limit exceeded"
                    )
                target.write(chunk)
                digest.update(chunk)
                copied = next_file_size
    except FileExistsError as exc:
        raise ValidatorStagingSecurityError("staging destination already exists") from exc
    except OSError as exc:
        raise ValidatorStagingSecurityError("selected input could not be copied") from exc
    finally:
        if destination_fd is not None:
            os.close(destination_fd)

    final = os.fstat(source.fileno())
    stable_fields = ("st_size", "st_mtime_ns", "st_ctime_ns")
    if (
        not _same_file_identity(initial, final)
        or copied != initial.st_size
        or any(getattr(initial, name, None) != getattr(final, name, None) for name in stable_fields)
    ):
        raise ValidatorStagingIntegrityError("selected input changed while it was staged")

    hexdigest = digest.hexdigest()
    if selected.claim is not None and (
        copied != selected.claim.size_bytes or hexdigest != selected.claim.sha256
    ):
        raise ValidatorStagingIntegrityError(
            "selected input differs from the authoritative entry snapshot"
        )
    return copied, hexdigest


def _remove_partial_staging(staging_root: Path) -> None:
    try:
        status = staging_root.lstat()
    except FileNotFoundError:
        return
    if _is_reparse_or_link(staging_root, status):
        try:
            staging_root.unlink()
        except IsADirectoryError:
            staging_root.rmdir()
        return
    shutil.rmtree(staging_root)


def stage_validator_files(
    *,
    authoritative_root: str | Path,
    authoritative_subtree: str | Path | None,
    selected_files: Sequence[str | Path],
    staging_root: str | Path,
    limits: StagingLimits | None = None,
    validated_entries: Iterable[ArtifactEntryV1] | None = None,
    abort_reason: Callable[[], str | None] | None = None,
) -> tuple[str, ...]:
    """Copy selected regular files into a fresh bounded validator stage.

    ``authoritative_subtree`` may be relative to ``authoritative_root`` or an
    absolute path beneath it.  Relative selected files are interpreted beneath
    that subtree.  Absolute selected files are accepted only for compatibility
    with current materializers and must resolve inside the same subtree.

    When ``validated_entries`` is supplied, every selected file must have an
    exact root-relative entry and its copied size/hash must match that snapshot.
    The successful return value contains only normalized paths relative to the
    staging root; it never contains a coordinator filesystem path.

    The destination must not already exist.  The caller owns successful-stage
    cleanup after the validator process is reaped.  On any exception this
    function removes the partially created destination before re-raising.
    """

    effective_limits = limits or StagingLimits()
    if not isinstance(effective_limits, StagingLimits):
        raise TypeError("limits must be a StagingLimits instance")
    if isinstance(selected_files, (str, bytes)) or not isinstance(selected_files, Sequence):
        raise TypeError("selected_files must be a sequence of paths")

    _raise_if_aborted(abort_reason)

    root, _ = _validate_root(authoritative_root)
    subtree, subtree_stat = _resolve_subtree(root, authoritative_subtree)
    claims = _claim_map(validated_entries)
    selected = _select_sources(
        root=root,
        subtree=subtree,
        selected_files=selected_files,
        claims=claims,
        limits=effective_limits,
    )

    _raise_if_aborted(abort_reason)

    stage = _create_staging_root(staging_root)
    try:
        aggregate = 0
        staged_paths: list[str] = []
        for item in selected:
            _raise_if_aborted(abort_reason)
            destination_parent = _create_destination_parent(stage, item.staged_relative_path)
            destination = destination_parent / PurePosixPath(item.staged_relative_path).name
            with _open_source(subtree, subtree_stat, item) as source:
                copied, _ = _copy_one(
                    source=source,
                    selected=item,
                    destination=destination,
                    aggregate_before=aggregate,
                    limits=effective_limits,
                    abort_reason=abort_reason,
                )
            aggregate += copied
            staged_paths.append(item.staged_relative_path)
        return tuple(staged_paths)
    except BaseException as exc:
        try:
            _remove_partial_staging(stage)
        except OSError as cleanup_error:
            raise ValidatorStagingCleanupError(
                "partial validator staging cleanup failed"
            ) from cleanup_error
        raise exc
