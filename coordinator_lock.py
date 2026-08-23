"""Cross-platform single-coordinator process invariant.

The lock is an operating-system advisory lock, not a PID-file convention. The
metadata is only for operator diagnostics; lock ownership is decided solely by
the kernel and is released automatically when a process exits.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Mapping, Sequence


class CoordinatorLockError(RuntimeError):
    """The single-coordinator invariant could not be established."""


@dataclass(frozen=True)
class CoordinatorIdentity:
    instance_id: str
    pid: int
    started_at: str
    deployment_mode: str


def default_state_dir() -> Path:
    return Path(os.environ.get("MYCELIUM_STATE_DIR", "."))


def _read_holder(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8")[:4096]
        parsed = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return "holder metadata unavailable"
    if not isinstance(parsed, dict):
        return "holder metadata unavailable"
    instance = str(parsed.get("instance_id", "unknown"))[:64]
    pid = str(parsed.get("pid", "unknown"))[:20]
    return f"instance_id={instance}, pid={pid}"


def _lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class CoordinatorLock:
    """Hold the exclusive coordinator lock for this object's lifetime."""

    def __init__(
        self,
        state_dir: Path | str | None = None,
        *,
        deployment_mode: str = "local",
    ) -> None:
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.path = self.state_dir / ".mycelium-coordinator.lock"
        self.identity = CoordinatorIdentity(
            instance_id=uuid.uuid4().hex,
            pid=os.getpid(),
            started_at=datetime.now(timezone.utc).isoformat(),
            deployment_mode=deployment_mode,
        )
        self._handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> CoordinatorIdentity:
        if self._handle is not None:
            return self.identity
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b", buffering=0)
        except OSError as exc:
            raise CoordinatorLockError(
                f"coordinator lock cannot be opened in state directory: {self.state_dir}"
            ) from exc

        try:
            _lock_file(handle)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            holder = _read_holder(self.path)
            raise CoordinatorLockError(
                "another Mycelium coordinator already owns this state directory "
                f"({holder}); stop it or choose a different MYCELIUM_STATE_DIR"
            ) from exc

        metadata = json.dumps(
            {
                "instance_id": self.identity.instance_id,
                "pid": self.identity.pid,
                "started_at": self.identity.started_at,
                "deployment_mode": self.identity.deployment_mode,
            },
            sort_keys=True,
        ).encode("utf-8")
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(metadata)
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            try:
                _unlock_file(handle)
            finally:
                handle.close()
            raise CoordinatorLockError("coordinator lock metadata cannot be persisted") from exc

        self._handle = handle
        return self.identity

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock_file(handle)
        finally:
            handle.close()

    def __enter__(self) -> CoordinatorLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _positive_worker_count(value: str, source: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise CoordinatorLockError(f"{source} must be an integer") from exc
    if count < 1:
        raise CoordinatorLockError(f"{source} must be at least 1")
    return count


def validate_single_worker(
    environ: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
) -> None:
    """Reject common multi-worker launch configurations before startup."""
    environment = os.environ if environ is None else environ
    arguments = list(sys.argv[1:] if argv is None else argv)
    counts: list[tuple[str, int]] = []
    for variable in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        raw = environment.get(variable, "").strip()
        if raw:
            counts.append((variable, _positive_worker_count(raw, variable)))

    gunicorn_args = environment.get("GUNICORN_CMD_ARGS", "")
    if gunicorn_args:
        arguments.extend(shlex.split(gunicorn_args, posix=os.name != "nt"))
    for index, argument in enumerate(arguments):
        if argument in {"--workers", "-w"} and index + 1 < len(arguments):
            counts.append((argument, _positive_worker_count(arguments[index + 1], argument)))
        elif argument.startswith("--workers="):
            counts.append(
                ("--workers", _positive_worker_count(argument.split("=", 1)[1], "--workers"))
            )

    unsafe = [(source, count) for source, count in counts if count != 1]
    if unsafe:
        details = ", ".join(f"{source}={count}" for source, count in unsafe)
        raise CoordinatorLockError(
            "Mycelium requires exactly one coordinator process per state directory; "
            f"multi-worker launch rejected ({details})"
        )
