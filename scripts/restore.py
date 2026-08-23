"""Validate and restore a Mycelium state backup.

No archive member is written into the live state directory until the complete
archive has passed structural, checksum, JSON, and SQLite integrity checks.
Existing managed state is moved aside before staged files are installed, which
allows rollback if a later rename fails.  The coordinator must be stopped
before using the explicit overwrite flag.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unicodedata
import urllib.parse
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from scripts.backup import (  # noqa: E402
    BACKUP_FORMAT,
    BACKUP_FORMAT_VERSION,
    BUILD_METADATA_PATH,
    CONFIG_NAME,
    DATABASE_NAME,
    LEDGER_NAME,
    MANIFEST_NAME,
    SQLITE_SIDECARS,
    STATE_DIRECTORIES,
    STATE_FILES,
    STATE_PREFIX,
)


_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 1_000_000
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class RestoreError(RuntimeError):
    """An archive is unsafe, invalid, or cannot be installed."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RestoreError("backup JSON contains duplicate object keys")
        value[key] = item
    return value


def _decoded_path_is_unsafe(value: str) -> bool:
    normalized = value.replace("\\", "/")
    if "\x00" in normalized or normalized.startswith(("/", "//")):
        return True
    if PureWindowsPath(normalized).drive:
        return True
    parts = normalized.split("/")
    return any(part in {"", ".", ".."} for part in parts)


def _reject_encoded_traversal(value: str) -> None:
    decoded = value
    for _attempt in range(8):
        try:
            next_value = urllib.parse.unquote(decoded, errors="strict")
        except UnicodeError as exc:
            raise RestoreError("archive member has invalid percent encoding") from exc
        if next_value == decoded:
            return
        if _decoded_path_is_unsafe(next_value):
            raise RestoreError("archive member contains encoded traversal or a path separator")
        decoded = next_value
    if urllib.parse.unquote(decoded) != decoded:
        raise RestoreError("archive member uses excessive nested percent encoding")


def _validate_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise RestoreError("archive contains an invalid member name")
    if "\\" in value or "\x00" in value or value.startswith(("/", "//")):
        raise RestoreError("archive contains an absolute or non-canonical member name")
    if PureWindowsPath(value).drive:
        raise RestoreError("archive contains a drive-qualified member name")

    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RestoreError("archive contains path traversal")
    for part in parts:
        if len(part) > 255 or part[-1:] in {" ", "."}:
            raise RestoreError("archive member is not portable across supported filesystems")
        if any(ord(character) < 32 or ord(character) == 127 for character in part):
            raise RestoreError("archive member contains control characters")
        if ":" in part:
            raise RestoreError("archive member contains a drive or alternate-stream separator")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise RestoreError("archive member uses a reserved filesystem name")

    canonical = PurePosixPath(*parts).as_posix()
    if canonical != value:
        raise RestoreError("archive contains a non-canonical member name")
    _reject_encoded_traversal(value)
    return canonical


def _allowed_layout(path: str, kind: str) -> bool:
    if path == MANIFEST_NAME:
        return kind == "file"
    if path == BUILD_METADATA_PATH:
        return kind == "file"
    if path in {f"{STATE_PREFIX}/{name}" for name in STATE_FILES}:
        return kind == "file"
    for directory_name in STATE_DIRECTORIES:
        root = f"{STATE_PREFIX}/{directory_name}"
        if path == root:
            return kind == "directory"
        if path.startswith(f"{root}/"):
            return True
    return False


def _member_kind(info: zipfile.ZipInfo) -> tuple[str, str]:
    raw_name = info.filename
    directory_marker = raw_name.endswith("/")
    if directory_marker:
        raw_name = raw_name[:-1]
        if raw_name.endswith("/"):
            raise RestoreError("archive contains a non-canonical directory member")
    path = _validate_path(raw_name)

    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type == stat.S_IFLNK:
        raise RestoreError(f"archive contains a symlink: {path}")
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise RestoreError(f"archive contains a special file: {path}")

    kind = "directory" if directory_marker or file_type == stat.S_IFDIR else "file"
    if directory_marker != (kind == "directory"):
        raise RestoreError(f"archive member type is inconsistent: {path}")
    if info.flag_bits & 0x1:
        raise RestoreError("encrypted backup archives are not supported")
    if not _allowed_layout(path, kind):
        raise RestoreError(f"archive contains an unexpected path: {path}")
    return path, kind


def _collision_key(path: str) -> str:
    return "/".join(
        unicodedata.normalize("NFC", part).rstrip(" .").casefold() for part in path.split("/")
    )


def _index_archive(bundle: zipfile.ZipFile) -> dict[str, tuple[zipfile.ZipInfo, str]]:
    infos = bundle.infolist()
    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        raise RestoreError("backup archive contains too many entries")

    indexed: dict[str, tuple[zipfile.ZipInfo, str]] = {}
    collision_keys: set[str] = set()
    for info in infos:
        path, kind = _member_kind(info)
        collision = _collision_key(path)
        if path in indexed or collision in collision_keys:
            raise RestoreError(f"archive contains a duplicate or colliding path: {path}")
        indexed[path] = (info, kind)
        collision_keys.add(collision)

    manifest_record = indexed.get(MANIFEST_NAME)
    if manifest_record is None or manifest_record[1] != "file":
        raise RestoreError("backup manifest is missing")
    if manifest_record[0].file_size > _MAX_MANIFEST_BYTES:
        raise RestoreError("backup manifest is unreasonably large")
    return indexed


def _load_manifest(bundle: zipfile.ZipFile, info: zipfile.ZipInfo) -> dict:
    try:
        payload = bundle.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise RestoreError("backup manifest cannot be read") from exc
    try:
        manifest = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RestoreError("backup manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise RestoreError("backup manifest must be a JSON object")
    if manifest.get("format") != BACKUP_FORMAT:
        raise RestoreError("archive is not a Mycelium state backup")
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise RestoreError("backup format version is not supported")
    return manifest


def _manifest_entries(manifest: dict) -> dict[str, dict]:
    raw_entries = manifest.get("entries")
    raw_checksums = manifest.get("checksums")
    if not isinstance(raw_entries, list) or not isinstance(raw_checksums, dict):
        raise RestoreError("backup manifest is missing entries or checksums")

    entries: dict[str, dict] = {}
    collision_keys: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise RestoreError("backup manifest contains an invalid entry")
        path = _validate_path(raw.get("path"))
        kind = raw.get("kind")
        if (
            not isinstance(kind, str)
            or kind not in {"file", "directory"}
            or not _allowed_layout(path, kind)
        ):
            raise RestoreError(f"backup manifest has an invalid entry type: {path}")
        if path == MANIFEST_NAME:
            raise RestoreError("backup manifest cannot checksum itself")
        collision = _collision_key(path)
        if path in entries or collision in collision_keys:
            raise RestoreError(f"backup manifest contains a duplicate path: {path}")

        mode = raw.get("mode")
        mtime_ns = raw.get("mtime_ns")
        if not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o777:
            raise RestoreError(f"backup manifest has an invalid mode: {path}")
        if (
            not isinstance(mtime_ns, int)
            or isinstance(mtime_ns, bool)
            or not 0 <= mtime_ns <= 2**63 - 1
        ):
            raise RestoreError(f"backup manifest has an invalid timestamp: {path}")

        if kind == "file":
            size = raw.get("size_bytes")
            digest = raw.get("sha256")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise RestoreError(f"backup manifest has an invalid file size: {path}")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise RestoreError(f"backup manifest has an invalid checksum: {path}")
        elif "sha256" in raw or "size_bytes" in raw:
            raise RestoreError(f"directory entry contains file metadata: {path}")

        entries[path] = raw
        collision_keys.add(collision)

    expected_checksums = {
        path: entry["sha256"] for path, entry in entries.items() if entry["kind"] == "file"
    }
    if raw_checksums != expected_checksums:
        raise RestoreError("backup checksum index does not match its entries")
    return entries


def _validate_structure(
    manifest: dict,
    indexed: dict[str, tuple[zipfile.ZipInfo, str]], entries: dict[str, dict]
) -> None:
    required = {
        BUILD_METADATA_PATH: "file",
        f"{STATE_PREFIX}/{DATABASE_NAME}": "file",
        **{f"{STATE_PREFIX}/{name}": "directory" for name in STATE_DIRECTORIES},
    }
    for path, kind in required.items():
        if entries.get(path, {}).get("kind") != kind:
            raise RestoreError(f"backup is missing required entry: {path}")

    archive_paths = set(indexed)
    manifest_paths = set(entries) | {MANIFEST_NAME}
    if archive_paths != manifest_paths:
        raise RestoreError("archive members do not match the backup manifest")

    for path, entry in entries.items():
        info, archive_kind = indexed[path]
        if archive_kind != entry["kind"]:
            raise RestoreError(f"archive member type does not match its manifest: {path}")
        if archive_kind == "file" and info.file_size != entry["size_bytes"]:
            raise RestoreError(f"archive member size does not match its manifest: {path}")
        if archive_kind == "directory" and info.file_size != 0:
            raise RestoreError(f"archive directory contains an unexpected payload: {path}")

    file_paths = {path for path, entry in entries.items() if entry["kind"] == "file"}
    for path in entries:
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in file_paths:
                raise RestoreError("backup manifest places an entry beneath a file")
            if parent not in {"metadata", STATE_PREFIX} and entries.get(parent, {}).get(
                "kind"
            ) != "directory":
                raise RestoreError(f"backup manifest omits a parent directory: {parent}")

    expected_state = {
        "database": f"{STATE_PREFIX}/{DATABASE_NAME}",
        "configuration": (
            f"{STATE_PREFIX}/{CONFIG_NAME}"
            if f"{STATE_PREFIX}/{CONFIG_NAME}" in entries
            else None
        ),
        "compatibility_ledger": (
            f"{STATE_PREFIX}/{LEDGER_NAME}"
            if f"{STATE_PREFIX}/{LEDGER_NAME}" in entries
            else None
        ),
        "projects": f"{STATE_PREFIX}/projects",
        "output": f"{STATE_PREFIX}/output",
        "execution_artifacts": f"{STATE_PREFIX}/execution_artifacts",
        "build_metadata": BUILD_METADATA_PATH,
    }
    if manifest.get("state") != expected_state:
        raise RestoreError("backup manifest state layout is invalid")


def _ensure_staging_capacity(entries: dict[str, dict], staging: Path) -> None:
    declared_size = sum(
        entry["size_bytes"] for entry in entries.values() if entry["kind"] == "file"
    )
    try:
        available = shutil.disk_usage(staging).free
    except OSError:
        return
    if declared_size > available:
        raise RestoreError("insufficient free space to stage the verified backup")


def _extract_and_verify(
    bundle: zipfile.ZipFile,
    indexed: dict[str, tuple[zipfile.ZipInfo, str]],
    entries: dict[str, dict],
    staging: Path,
) -> None:
    directories: list[tuple[Path, dict]] = []
    for path, entry in sorted(entries.items(), key=lambda item: (item[0].count("/"), item[0])):
        destination = staging.joinpath(*PurePosixPath(path).parts)
        if entry["kind"] == "directory":
            destination.mkdir(parents=True, exist_ok=False)
            directories.append((destination, entry))
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        info = indexed[path][0]
        digest = hashlib.sha256()
        size = 0
        try:
            with bundle.open(info, "r") as source, destination.open("xb") as target:
                while chunk := source.read(_COPY_CHUNK_BYTES):
                    size += len(chunk)
                    if size > entry["size_bytes"]:
                        raise RestoreError(f"archive member exceeds its declared size: {path}")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except RestoreError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise RestoreError(f"archive member cannot be extracted: {path}") from exc

        if size != entry["size_bytes"] or digest.hexdigest() != entry["sha256"]:
            raise RestoreError(f"checksum verification failed for {path}")
        os.chmod(destination, entry["mode"])
        os.utime(destination, ns=(entry["mtime_ns"], entry["mtime_ns"]))

    # Apply restrictive or read-only directory modes only after every child has
    # been materialized.  Deepest-first timestamping also avoids a child update
    # immediately changing the restored parent mtime.
    for destination, entry in reversed(directories):
        os.chmod(destination, entry["mode"])
        os.utime(destination, ns=(entry["mtime_ns"], entry["mtime_ns"]))


def _read_json_object(path: Path, label: str, expected_type: type) -> Any:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_keys
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RestoreError(f"restored {label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, expected_type):
        raise RestoreError(f"restored {label} has the wrong JSON structure")
    return value


def _validate_staged_state(staging: Path) -> None:
    build = staging.joinpath(*PurePosixPath(BUILD_METADATA_PATH).parts)
    build_data = _read_json_object(build, "build metadata", dict)
    if (
        build_data.get("product") != "Mycelium"
        or build_data.get("backup_format") != BACKUP_FORMAT
        or build_data.get("backup_format_version") != BACKUP_FORMAT_VERSION
    ):
        raise RestoreError("restored build metadata does not describe this backup format")

    config = staging / STATE_PREFIX / CONFIG_NAME
    if config.exists():
        _read_json_object(config, "configuration", dict)
    ledger = staging / STATE_PREFIX / LEDGER_NAME
    if ledger.exists():
        _read_json_object(ledger, "compatibility ledger", list)

    database = staging / STATE_PREFIX / DATABASE_NAME
    database_uri = f"{database.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(database_uri, uri=True, timeout=30.0)) as connection:
            connection.execute("PRAGMA query_only = ON")
            result = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        raise RestoreError("restored SQLite snapshot cannot be opened") from exc
    if result != [("ok",)]:
        raise RestoreError("restored SQLite snapshot failed its integrity check")
    for name in STATE_FILES:
        sensitive_file = staging / STATE_PREFIX / name
        if sensitive_file.exists():
            os.chmod(sensitive_file, 0o600)
    os.chmod(staging / STATE_PREFIX, 0o700)


def _existing_state(state_root: Path) -> list[Path]:
    return [
        state_root / name
        for name in (*STATE_FILES, *SQLITE_SIDECARS, *STATE_DIRECTORIES)
        if (state_root / name).exists() or (state_root / name).is_symlink()
    ]


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _install_into_existing_root(staged_state: Path, state_root: Path, *, force: bool) -> None:
    existing = _existing_state(state_root)
    if existing and not force:
        names = ", ".join(path.name for path in existing)
        raise RestoreError(
            f"existing Mycelium state would be overwritten ({names}); "
            "stop the coordinator and rerun with --force"
        )

    rollback = Path(tempfile.mkdtemp(prefix=".mycelium-restore-rollback-", dir=state_root.parent))
    moved_existing: list[str] = []
    installed: list[str] = []
    try:
        for path in existing:
            os.replace(path, rollback / path.name)
            moved_existing.append(path.name)

        for name in (*STATE_FILES, *STATE_DIRECTORIES):
            source = staged_state / name
            if source.exists():
                os.replace(source, state_root / name)
                installed.append(name)
    except Exception as exc:
        rollback_errors: list[str] = []
        for name in reversed(installed):
            try:
                _remove_path(state_root / name)
            except OSError:
                rollback_errors.append(name)
        for name in reversed(moved_existing):
            try:
                os.replace(rollback / name, state_root / name)
            except OSError:
                rollback_errors.append(name)
        if rollback_errors:
            raise RestoreError(
                "restore failed and rollback was incomplete for: " + ", ".join(rollback_errors)
            ) from exc
        raise RestoreError("restore failed; the previous state was restored") from exc
    finally:
        shutil.rmtree(rollback, ignore_errors=True)


def _restore_verified_staging(staging: Path, state_root: Path, *, force: bool) -> None:
    staged_state = staging / STATE_PREFIX
    if state_root.exists():
        if state_root.is_symlink() or not state_root.is_dir():
            raise RestoreError("restore target must be a non-symlink directory")
        _install_into_existing_root(staged_state, state_root, force=force)
        return

    state_root.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged_state, state_root)


def restore_backup(
    archive: str | Path,
    *,
    state_dir: str | Path = ".",
    force: bool = False,
) -> Path:
    """Verify an archive completely, then restore its managed state."""
    archive_path = Path(archive).expanduser().resolve()
    if archive_path.is_symlink() or not archive_path.is_file():
        raise RestoreError(f"backup archive does not exist or is not a regular file: {archive_path}")

    requested_root = Path(state_dir).expanduser()
    if requested_root.exists():
        state_root = requested_root.resolve()
    else:
        state_root = requested_root.absolute()
    state_parent = state_root.parent
    state_parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=".mycelium-restore-stage-", dir=state_parent
    ) as staging_name:
        staging = Path(staging_name)
        try:
            with zipfile.ZipFile(archive_path, "r") as bundle:
                indexed = _index_archive(bundle)
                manifest = _load_manifest(bundle, indexed[MANIFEST_NAME][0])
                entries = _manifest_entries(manifest)
                _validate_structure(manifest, indexed, entries)
                _ensure_staging_capacity(entries, staging)
                _extract_and_verify(bundle, indexed, entries, staging)
        except RestoreError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise RestoreError("backup archive is not a valid ZIP file") from exc

        _validate_staged_state(staging)
        _restore_verified_staging(staging, state_root, force=force)

    return state_root


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify and restore a checksummed Mycelium state backup."
    )
    parser.add_argument("archive", help="Mycelium backup ZIP")
    parser.add_argument(
        "--state-dir",
        default=".",
        help="Mycelium state directory (default: current directory)",
    )
    parser.add_argument(
        "--force",
        "--overwrite",
        "--overwrite-existing",
        action="store_true",
        help="replace existing managed state; stop the coordinator first",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        restored = restore_backup(args.archive, state_dir=args.state_dir, force=args.force)
    except (RestoreError, OSError) as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 1

    print(f"Restore complete: {restored}")
    print("Process-local queues, in-flight work, and node sessions were not restored.")
    print("Before starting the coordinator, run from the restored application directory:")
    print("  python scripts/preflight.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
