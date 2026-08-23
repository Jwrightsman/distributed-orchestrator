"""Create a consistent, checksummed Mycelium state backup.

The coordinator may keep ``events.db`` in WAL mode while this command runs.
Opening or copying the database and its sidecars as ordinary files can produce
a snapshot from different points in time, so this module always uses SQLite's
online backup API for the database.  Other state is copied into a private
staging directory before the ZIP is assembled.

Process-local scheduler queues, active node sessions, and in-flight work are
not durable state and therefore cannot be included.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO


BACKUP_FORMAT = "mycelium-state-backup"
BACKUP_FORMAT_VERSION = 1
MANIFEST_NAME = "backup-manifest.json"
BUILD_METADATA_PATH = "metadata/build.json"
STATE_PREFIX = "state"
DATABASE_NAME = "events.db"
CONFIG_NAME = "config.json"
LEDGER_NAME = "ledger.json"
STATE_DIRECTORIES = ("projects", "output", "execution_artifacts")
STATE_FILES = (DATABASE_NAME, CONFIG_NAME, LEDGER_NAME)
SQLITE_SIDECARS = (
    f"{DATABASE_NAME}-wal",
    f"{DATABASE_NAME}-shm",
    f"{DATABASE_NAME}-journal",
)

_APP_ROOT = Path(__file__).resolve().parent.parent
_COPY_CHUNK_BYTES = 1024 * 1024
_STABLE_COPY_ATTEMPTS = 3
_SQLITE_BACKUP_DEADLINE_SECONDS = 60.0


class BackupError(RuntimeError):
    """A backup could not be completed without risking an invalid archive."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_mode(path_stat: os.stat_result) -> int:
    return stat.S_IMODE(path_stat.st_mode) & 0o777


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    try:
        return os.path.samestat(left, right)
    except (AttributeError, OSError):
        return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _copy_stream(source: BinaryIO, destination: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(_COPY_CHUNK_BYTES):
        destination.write(chunk)
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _copy_stable_file(source: Path, destination: Path) -> dict:
    """Copy one regular file, retrying if it changes during the read."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_reason = "the file changed while it was read"

    for _attempt in range(_STABLE_COPY_ATTEMPTS):
        try:
            before = source.lstat()
        except OSError as exc:
            raise BackupError(f"cannot read backup source {source}: {exc}") from exc
        if stat.S_ISLNK(before.st_mode):
            raise BackupError(f"refusing to archive symlink: {source}")
        if not stat.S_ISREG(before.st_mode):
            raise BackupError(f"backup source is not a regular file: {source}")

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".copy", dir=destination.parent
        )
        temporary = Path(temporary_name)
        source_fd: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            source_fd = os.open(source, flags)
            with os.fdopen(source_fd, "rb") as source_handle:
                source_fd = None
                with os.fdopen(descriptor, "wb") as destination_handle:
                    descriptor = -1
                    opened = os.fstat(source_handle.fileno())
                    digest, size = _copy_stream(source_handle, destination_handle)
                    finished = os.fstat(source_handle.fileno())
                    destination_handle.flush()
                    os.fsync(destination_handle.fileno())

            after = source.lstat()
            stable = (
                _same_file(before, opened)
                and _same_file(opened, finished)
                and _same_file(finished, after)
                and finished.st_size == size
                and before.st_size == after.st_size
                and before.st_mtime_ns == after.st_mtime_ns
                and opened.st_mtime_ns == finished.st_mtime_ns
                and opened.st_ctime_ns == finished.st_ctime_ns
            )
            if not stable:
                last_reason = "the file changed while it was read"
                time.sleep(0.01)
                continue

            mode = _safe_mode(opened)
            os.chmod(temporary, mode)
            os.utime(temporary, ns=(opened.st_atime_ns, opened.st_mtime_ns))
            os.replace(temporary, destination)
            return {
                "kind": "file",
                "size_bytes": size,
                "sha256": digest,
                "mode": mode,
                "mtime_ns": opened.st_mtime_ns,
            }
        except FileNotFoundError:
            last_reason = "the file disappeared while it was read"
            time.sleep(0.01)
        except OSError as exc:
            last_reason = str(exc)
            time.sleep(0.01)
        finally:
            if source_fd is not None:
                os.close(source_fd)
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    raise BackupError(f"could not take a stable copy of {source}: {last_reason}")


def _snapshot_sqlite(source: Path, destination: Path) -> dict:
    """Use SQLite's online backup API to produce one transactionally consistent DB."""
    try:
        before = source.lstat()
    except OSError as exc:
        raise BackupError(f"SQLite database not found at {source}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise BackupError(f"SQLite database must be a regular non-symlink file: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() - started > _SQLITE_BACKUP_DEADLINE_SECONDS:
            raise BackupError("timed out waiting for a consistent SQLite snapshot")

    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30.0)) as live:
            live.execute("PRAGMA busy_timeout = 30000")
            with closing(sqlite3.connect(destination, timeout=30.0)) as snapshot:
                live.backup(snapshot, pages=1024, progress=progress, sleep=0.05)
                result = snapshot.execute("PRAGMA integrity_check").fetchone()
                if result is None or result[0] != "ok":
                    raise BackupError("SQLite rejected the completed backup snapshot")
    except BackupError:
        destination.unlink(missing_ok=True)
        raise
    except sqlite3.Error as exc:
        destination.unlink(missing_ok=True)
        raise BackupError(f"SQLite backup failed: {exc}") from exc

    try:
        after = source.lstat()
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise BackupError("SQLite database was replaced during backup") from exc
    if not _same_file(before, after):
        destination.unlink(missing_ok=True)
        raise BackupError("SQLite database was replaced during backup")

    # The database contains prompts, results, and access metadata.  A restored
    # coordinator must also be able to write it, regardless of an overly broad
    # or read-only source mode.
    mode = 0o600
    os.chmod(destination, mode)
    digest, size = _sha256_file(destination)
    snapshot_stat = destination.stat()
    return {
        "kind": "file",
        "size_bytes": size,
        "sha256": digest,
        "mode": mode,
        "mtime_ns": snapshot_stat.st_mtime_ns,
    }


def _directory_entry(path: Path) -> dict:
    path_stat = path.lstat()
    if stat.S_ISLNK(path_stat.st_mode):
        raise BackupError(f"refusing to archive symlink: {path}")
    if not stat.S_ISDIR(path_stat.st_mode):
        raise BackupError(f"backup source is not a directory: {path}")
    return {
        "kind": "directory",
        "mode": _safe_mode(path_stat),
        "mtime_ns": path_stat.st_mtime_ns,
    }


def _snapshot_directory(
    source: Path,
    destination: Path,
    archive_path: PurePosixPath,
    entries: list[dict],
) -> None:
    """Copy a directory tree without following or silently skipping links."""
    destination.mkdir(parents=True, exist_ok=True)
    if source.exists() or source.is_symlink():
        root_metadata = _directory_entry(source)
    else:
        root_metadata = {"kind": "directory", "mode": 0o700, "mtime_ns": 0}
    entries.append({"path": archive_path.as_posix(), **root_metadata})

    if not source.exists():
        return

    def visit(current_source: Path, current_destination: Path, relative: PurePosixPath) -> None:
        try:
            children = sorted(os.scandir(current_source), key=lambda item: item.name)
        except OSError as exc:
            raise BackupError(f"cannot enumerate backup source {current_source}: {exc}") from exc

        for child in children:
            child_source = current_source / child.name
            child_destination = current_destination / child.name
            child_relative = relative / child.name
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise BackupError(f"cannot inspect backup source {child_source}: {exc}") from exc
            if stat.S_ISLNK(child_stat.st_mode):
                raise BackupError(f"refusing to archive symlink: {child_source}")
            if stat.S_ISDIR(child_stat.st_mode):
                child_destination.mkdir()
                entries.append(
                    {
                        "path": child_relative.as_posix(),
                        "kind": "directory",
                        "mode": _safe_mode(child_stat),
                        "mtime_ns": child_stat.st_mtime_ns,
                    }
                )
                visit(child_source, child_destination, child_relative)
            elif stat.S_ISREG(child_stat.st_mode):
                metadata = _copy_stable_file(child_source, child_destination)
                entries.append({"path": child_relative.as_posix(), **metadata})
            else:
                raise BackupError(f"refusing to archive special file: {child_source}")

    visit(source, destination, archive_path)


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_APP_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip().lower()
    if len(commit) == 40 and all(character in "0123456789abcdef" for character in commit):
        return commit
    return None


def _build_fingerprint() -> str | None:
    try:
        sys.path.insert(0, str(_APP_ROOT))
        from build_info import BUILD  # noqa: PLC0415
    except Exception:
        return None
    return BUILD


def _write_json_staged(path: Path, value: dict, *, mode: int = 0o600) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(payload)
    os.chmod(path, mode)
    path_stat = path.stat()
    return {
        "kind": "file",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "mode": mode,
        "mtime_ns": path_stat.st_mtime_ns,
    }


def _zip_info(entry: dict) -> zipfile.ZipInfo:
    name = entry["path"] + ("/" if entry["kind"] == "directory" else "")
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = int(entry.get("mode", 0o700 if entry["kind"] == "directory" else 0o600))
    type_bits = stat.S_IFDIR if entry["kind"] == "directory" else stat.S_IFREG
    info.external_attr = ((type_bits | mode) << 16) | (0x10 if entry["kind"] == "directory" else 0)
    return info


def _write_archive(archive: Path, staging: Path, entries: list[dict], manifest: dict) -> None:
    with zipfile.ZipFile(
        archive,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as bundle:
        for entry in sorted(entries, key=lambda item: item["path"]):
            info = _zip_info(entry)
            if entry["kind"] == "directory":
                bundle.writestr(info, b"")
                continue
            source = staging.joinpath(*PurePosixPath(entry["path"]).parts)
            with source.open("rb") as source_handle, bundle.open(info, "w", force_zip64=True) as target:
                shutil.copyfileobj(source_handle, target, length=_COPY_CHUNK_BYTES)

        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        manifest_entry = {
            "path": MANIFEST_NAME,
            "kind": "file",
            "mode": 0o600,
        }
        bundle.writestr(_zip_info(manifest_entry), manifest_bytes)


def _archive_target(destination: str | Path, created_at: str) -> Path:
    requested = Path(destination).expanduser()
    if requested.exists():
        if not requested.is_dir():
            raise BackupError(f"backup destination already exists: {requested}")
        directory = requested
        stamp = datetime.fromisoformat(created_at).strftime("%Y%m%dT%H%M%SZ")
        target = directory / f"mycelium-backup-{stamp}.zip"
    elif requested.suffix.lower() == ".zip":
        requested.parent.mkdir(parents=True, exist_ok=True)
        target = requested
    else:
        requested.mkdir(parents=True, exist_ok=False)
        stamp = datetime.fromisoformat(created_at).strftime("%Y%m%dT%H%M%SZ")
        target = requested / f"mycelium-backup-{stamp}.zip"

    if target.exists():
        raise BackupError(f"backup destination already exists: {target}")
    return target.resolve()


def _reject_destination_inside_state(target: Path, state_dir: Path) -> None:
    for directory_name in STATE_DIRECTORIES:
        source_root = (state_dir / directory_name).resolve()
        try:
            target.relative_to(source_root)
        except ValueError:
            continue
        raise BackupError(f"backup destination cannot be inside {directory_name}/")


def _reserve_and_publish(temporary: Path, target: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except FileExistsError as exc:
        raise BackupError(f"backup destination already exists: {target}") from exc
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        target.unlink(missing_ok=True)
        raise


def create_backup(destination: str | Path, *, state_dir: str | Path = ".") -> Path:
    """Create a recovery archive and return its absolute path."""
    state_root = Path(state_dir).expanduser().resolve()
    if not state_root.is_dir():
        raise BackupError(f"state directory does not exist: {state_root}")

    database = state_root / DATABASE_NAME
    created_at = _utc_now()
    target = _archive_target(destination, created_at)
    _reject_destination_inside_state(target, state_root)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary_archive = Path(temporary_name)
    try:
        with tempfile.TemporaryDirectory(prefix="mycelium-backup-stage-") as staging_name:
            staging = Path(staging_name)
            entries: list[dict] = []

            database_destination = staging / STATE_PREFIX / DATABASE_NAME
            database_metadata = _snapshot_sqlite(database, database_destination)
            entries.append({"path": f"{STATE_PREFIX}/{DATABASE_NAME}", **database_metadata})

            for file_name in (CONFIG_NAME, LEDGER_NAME):
                source = state_root / file_name
                if source.exists() or source.is_symlink():
                    destination_path = staging / STATE_PREFIX / file_name
                    metadata = _copy_stable_file(source, destination_path)
                    metadata["mode"] = 0o600
                    os.chmod(destination_path, 0o600)
                    entries.append({"path": f"{STATE_PREFIX}/{file_name}", **metadata})

            for directory_name in STATE_DIRECTORIES:
                _snapshot_directory(
                    state_root / directory_name,
                    staging / STATE_PREFIX / directory_name,
                    PurePosixPath(STATE_PREFIX, directory_name),
                    entries,
                )

            build_metadata = {
                "product": "Mycelium",
                "backup_format": BACKUP_FORMAT,
                "backup_format_version": BACKUP_FORMAT_VERSION,
                "created_at": created_at,
                "build_fingerprint": _build_fingerprint(),
                "git_commit": _git_commit(),
                "python_version": sys.version.split()[0],
            }
            metadata_entry = _write_json_staged(
                staging.joinpath(*PurePosixPath(BUILD_METADATA_PATH).parts),
                build_metadata,
            )
            entries.append({"path": BUILD_METADATA_PATH, **metadata_entry})

            entries.sort(key=lambda item: item["path"])
            checksums = {
                entry["path"]: entry["sha256"]
                for entry in entries
                if entry["kind"] == "file"
            }
            manifest = {
                "format": BACKUP_FORMAT,
                "format_version": BACKUP_FORMAT_VERSION,
                "created_at": created_at,
                "entries": entries,
                "checksums": checksums,
                "state": {
                    "database": f"{STATE_PREFIX}/{DATABASE_NAME}",
                    "configuration": (
                        f"{STATE_PREFIX}/{CONFIG_NAME}"
                        if (state_root / CONFIG_NAME).exists()
                        else None
                    ),
                    "compatibility_ledger": (
                        f"{STATE_PREFIX}/{LEDGER_NAME}"
                        if (state_root / LEDGER_NAME).exists()
                        else None
                    ),
                    "projects": f"{STATE_PREFIX}/projects",
                    "output": f"{STATE_PREFIX}/output",
                    "execution_artifacts": f"{STATE_PREFIX}/execution_artifacts",
                    "build_metadata": BUILD_METADATA_PATH,
                },
                "not_included": [
                    "process-local scheduler queues",
                    "in-flight work",
                    "process-local node sessions",
                ],
            }
            _write_archive(temporary_archive, staging, entries, manifest)

        _reserve_and_publish(temporary_archive, target)
    except Exception:
        temporary_archive.unlink(missing_ok=True)
        raise

    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a consistent, checksummed backup of Mycelium state."
    )
    parser.add_argument("--destination", required=True, help="ZIP path or backup directory")
    parser.add_argument(
        "--state-dir",
        default=".",
        help="Mycelium state directory (default: current directory)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        archive = create_backup(args.destination, state_dir=args.state_dir)
    except (BackupError, OSError, zipfile.BadZipFile) as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1

    config_included = "yes" if (Path(args.state_dir) / CONFIG_NAME).exists() else "no"
    print(f"Backup created: {archive}")
    print("SQLite snapshot: consistent online backup of events.db")
    print(f"Configuration included: {config_included} (values not displayed)")
    print("Not recoverable: process-local queues, in-flight work, or node sessions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
