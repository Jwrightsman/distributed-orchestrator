"""Private, durable identity files for the stock Mycelium worker.

Enrollment credentials are bearer secrets.  This module deliberately keeps
them out of reprs and errors, binds an identity file to one normalized
coordinator URL and node label, refuses any origin that would carry them in
clear text (see `worker_transport`), and writes the file atomically with
owner-only POSIX permissions.  Windows has no portable stdlib API for auditing ACLs, so
the same atomic write is used there and permissions are best effort.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from node_sessions import InvalidNodeId, normalize_node_id
from worker_transport import InsecureTransportError, require_secure_transport


IDENTITY_VERSION = 1
MAX_IDENTITY_BYTES = 16 * 1024
ENROLLMENT_CREDENTIAL_BYTES = 32
MIN_ENROLLMENT_CREDENTIAL_LENGTH = 32
MAX_ENROLLMENT_CREDENTIAL_LENGTH = 256

_IDENTITY_FIELDS = frozenset(
    {
        "version",
        "coordinator",
        "node_id",
        "enrollment_id",
        "credential_version",
        "enrollment_credential",
    }
)
_CREDENTIAL_RE = re.compile(
    rf"^[A-Za-z0-9_-]{{{MIN_ENROLLMENT_CREDENTIAL_LENGTH},"
    rf"{MAX_ENROLLMENT_CREDENTIAL_LENGTH}}}$"
)
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class WorkerIdentityError(RuntimeError):
    """A worker identity cannot be used without weakening its safety boundary."""


@dataclass(frozen=True)
class WorkerIdentity:
    """Secret-safe worker enrollment material for exactly one coordinator."""

    version: int
    coordinator: str
    node_id: str
    enrollment_id: str | None
    credential_version: int | None
    enrollment_credential: str = field(repr=False)

    def with_enrollment(
        self, enrollment_id: str, credential_version: object
    ) -> "WorkerIdentity":
        return replace(
            self,
            enrollment_id=normalize_enrollment_id(enrollment_id),
            credential_version=normalize_credential_version(credential_version),
        )

    def as_json(self) -> dict[str, object]:
        return {
            "version": self.version,
            "coordinator": self.coordinator,
            "node_id": self.node_id,
            "enrollment_id": self.enrollment_id,
            "credential_version": self.credential_version,
            "enrollment_credential": self.enrollment_credential,
        }


def normalize_coordinator(value: str) -> str:
    """Return a stable HTTP(S) coordinator origin or fail closed.

    Mycelium worker routes are origin-relative, so paths are not meaningful.
    Userinfo, query strings, and fragments are rejected to keep bearer
    credentials away from ambiguous or accidentally logged URL components.
    """

    if not isinstance(value, str) or not value.strip():
        raise WorkerIdentityError("coordinator URL cannot be empty")
    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise WorkerIdentityError("coordinator URL is invalid") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise WorkerIdentityError("coordinator URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise WorkerIdentityError("coordinator URL must not contain user information")
    if parsed.query or parsed.fragment:
        raise WorkerIdentityError("coordinator URL must not contain a query or fragment")
    if parsed.path not in {"", "/"}:
        raise WorkerIdentityError("coordinator URL must not contain a path")
    hostname = parsed.hostname
    if not hostname:
        raise WorkerIdentityError("coordinator URL must contain a host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise WorkerIdentityError("coordinator URL has an invalid port") from exc

    if "%" in hostname:
        raise WorkerIdentityError("scoped coordinator addresses are not supported")
    bracketed = False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if len(host) > 253 or not host:
            raise WorkerIdentityError("coordinator hostname is invalid")
        if not all(_DNS_LABEL_RE.fullmatch(label) for label in host.split(".")):
            raise WorkerIdentityError("coordinator hostname is invalid")
    else:
        host = address.compressed.lower()
        bracketed = address.version == 6

    if port is not None and not 1 <= port <= 65535:
        raise WorkerIdentityError("coordinator URL has an invalid port")

    # The transport gate lives here, and only here, because this is the single
    # function every worker path already funnels through: the join flow, the
    # installer, node.py's --server, the enrollment admin tool, and the
    # validation of an identity file that was written earlier. Putting the
    # check anywhere else would leave one of those as a way around it.
    try:
        require_secure_transport(scheme, host)
    except InsecureTransportError as exc:
        raise WorkerIdentityError(str(exc)) from exc

    default_port = 80 if scheme == "http" else 443
    port_suffix = "" if port in {None, default_port} else f":{port}"
    rendered_host = f"[{host}]" if bracketed else host
    return f"{scheme}://{rendered_host}{port_suffix}"


def normalize_enrollment_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkerIdentityError("enrollment_id must be a UUID")
    try:
        parsed = uuid.UUID(value.strip())
    except (ValueError, AttributeError) as exc:
        raise WorkerIdentityError("enrollment_id must be a UUID") from exc
    # The coordinator's repository-consistent UUID form is lowercase hex.
    return parsed.hex


def normalize_credential_version(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise WorkerIdentityError("credential_version must be a positive integer")
    return value


def validate_worker_credential(value: object) -> str:
    if not isinstance(value, str) or not _CREDENTIAL_RE.fullmatch(value):
        raise WorkerIdentityError(
            "enrollment credential in identity file is invalid; restore or rotate the identity"
        )
    return value


def normalize_worker_node_id(value: str) -> str:
    try:
        return normalize_node_id(value)
    except (InvalidNodeId, ValueError) as exc:
        raise WorkerIdentityError(str(exc)) from exc


def _coordinator_digest(coordinator: str) -> str:
    payload = b"mycelium-worker-coordinator-v1\0" + coordinator.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def user_config_directory(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    os_name: str | None = None,
    platform_name: str | None = None,
) -> Path:
    """Return a user-owned Mycelium configuration directory."""

    environment = os.environ if environ is None else environ
    override = environment.get("MYCELIUM_WORKER_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    user_home = Path.home() if home is None else Path(home)
    effective_os = os.name if os_name is None else os_name
    effective_platform = sys.platform if platform_name is None else platform_name
    if effective_os == "nt":
        appdata = environment.get("APPDATA", "").strip()
        root = Path(appdata) if appdata else user_home / "AppData" / "Roaming"
        return root / "Mycelium"
    if effective_platform == "darwin":
        return user_home / "Library" / "Application Support" / "Mycelium"
    xdg = environment.get("XDG_CONFIG_HOME", "").strip()
    root = Path(xdg) if xdg else user_home / ".config"
    return root / "mycelium"


def default_identity_file(
    coordinator: str,
    *,
    config_dir: Path | str | None = None,
) -> Path:
    normalized = normalize_coordinator(coordinator)
    root = Path(config_dir) if config_dir is not None else user_config_directory()
    return root.expanduser() / "nodes" / f"{_coordinator_digest(normalized)}.json"


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise WorkerIdentityError("worker identity JSON contains duplicate fields")
        value[key] = item
    return value


def _validate_identity_payload(
    payload: object,
    *,
    coordinator: str | None = None,
    node_id: str | None = None,
) -> WorkerIdentity:
    if not isinstance(payload, dict) or set(payload) != _IDENTITY_FIELDS:
        raise WorkerIdentityError("worker identity JSON has an unexpected structure")
    if payload.get("version") != IDENTITY_VERSION:
        raise WorkerIdentityError(
            f"unsupported worker identity version; expected {IDENTITY_VERSION}"
        )
    stored_coordinator = normalize_coordinator(str(payload.get("coordinator", "")))
    if payload.get("coordinator") != stored_coordinator:
        raise WorkerIdentityError("worker identity coordinator is not normalized")
    stored_node_id = normalize_worker_node_id(str(payload.get("node_id", "")))
    if payload.get("node_id") != stored_node_id:
        raise WorkerIdentityError("worker identity node_id is not normalized")
    raw_enrollment_id = payload.get("enrollment_id")
    enrollment_id = (
        None if raw_enrollment_id is None else normalize_enrollment_id(raw_enrollment_id)
    )
    if raw_enrollment_id is not None and raw_enrollment_id != enrollment_id:
        raise WorkerIdentityError("worker identity enrollment_id is not normalized")
    raw_credential_version = payload.get("credential_version")
    if enrollment_id is None:
        if raw_credential_version is not None:
            raise WorkerIdentityError(
                "unenrolled worker identity cannot have a credential_version"
            )
        credential_version = None
    else:
        credential_version = normalize_credential_version(raw_credential_version)
    credential = validate_worker_credential(payload.get("enrollment_credential"))

    if coordinator is not None and stored_coordinator != normalize_coordinator(coordinator):
        raise WorkerIdentityError("worker identity belongs to a different coordinator")
    if node_id is not None and stored_node_id != normalize_worker_node_id(node_id):
        raise WorkerIdentityError("worker identity belongs to a different node_id")
    return WorkerIdentity(
        version=IDENTITY_VERSION,
        coordinator=stored_coordinator,
        node_id=stored_node_id,
        enrollment_id=enrollment_id,
        credential_version=credential_version,
        enrollment_credential=credential,
    )


def _check_open_file(path: Path, opened: os.stat_result) -> None:
    if not stat.S_ISREG(opened.st_mode):
        raise WorkerIdentityError(f"worker identity is not a regular file: {path}")
    if opened.st_size > MAX_IDENTITY_BYTES:
        raise WorkerIdentityError("worker identity file is too large")
    if os.name == "posix":
        if stat.S_IMODE(opened.st_mode) & 0o077:
            raise WorkerIdentityError(
                "worker identity file is accessible by group or other users; "
                "set POSIX mode 0600"
            )
        getuid = getattr(os, "getuid", None)
        if getuid is not None and opened.st_uid != getuid():
            raise WorkerIdentityError("worker identity file is not owned by this user")


def load_worker_identity(
    path: Path | str,
    *,
    coordinator: str | None = None,
    node_id: str | None = None,
) -> WorkerIdentity:
    identity_path = Path(path).expanduser()
    try:
        path_stat = identity_path.lstat()
    except FileNotFoundError:
        raise WorkerIdentityError(f"worker identity file does not exist: {identity_path}")
    except OSError as exc:
        raise WorkerIdentityError(f"worker identity file cannot be inspected: {identity_path}") from exc
    if stat.S_ISLNK(path_stat.st_mode):
        raise WorkerIdentityError("worker identity file must not be a symbolic link")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(identity_path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            _check_open_file(identity_path, opened)
            raw = handle.read(MAX_IDENTITY_BYTES + 1)
    except WorkerIdentityError:
        raise
    except OSError as exc:
        raise WorkerIdentityError(f"worker identity file cannot be read: {identity_path}") from exc
    if len(raw) > MAX_IDENTITY_BYTES:
        raise WorkerIdentityError("worker identity file is too large")
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except WorkerIdentityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise WorkerIdentityError("worker identity file is not valid UTF-8 JSON") from exc
    return _validate_identity_payload(payload, coordinator=coordinator, node_id=node_id)


def _ensure_parent(path: Path) -> None:
    parent = path.parent
    existed = parent.exists()
    if existed:
        try:
            parent_stat = parent.lstat()
        except OSError as exc:
            raise WorkerIdentityError("worker identity directory cannot be inspected") from exc
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise WorkerIdentityError("worker identity parent must be a real directory")
        return
    try:
        parent.mkdir(parents=True, mode=0o700, exist_ok=False)
        if os.name == "posix":
            os.chmod(parent, 0o700)
    except FileExistsError:
        # Another same-user worker may have created the coordinator-scoped
        # directory between the check and mkdir. Validate that object below.
        pass
    except OSError as exc:
        raise WorkerIdentityError("worker identity directory cannot be created") from exc
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise WorkerIdentityError("worker identity directory cannot be inspected") from exc
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise WorkerIdentityError("worker identity parent must be a real directory")


@contextmanager
def _identity_creation_lock(identity_path: Path) -> Iterator[None]:
    """Serialize first creation across processes without a stale PID lock."""

    _ensure_parent(identity_path)
    lock_path = identity_path.parent / f".{identity_path.name}.lock"
    if lock_path.is_symlink():
        raise WorkerIdentityError("worker identity creation lock must not be a symlink")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    handle = None
    locked = False
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        descriptor = -1
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise WorkerIdentityError(
                "worker identity creation lock must be a regular file"
            )
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locked = True
        yield
    except WorkerIdentityError:
        raise
    except OSError as exc:
        raise WorkerIdentityError(
            "worker identity creation lock cannot be acquired"
        ) from exc
    finally:
        if handle is not None:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    # Closing releases a kernel-held advisory lock even if an
                    # explicit unlock reports an operating-system error.
                    pass
            handle.close()
        elif descriptor >= 0:
            os.close(descriptor)


def write_worker_identity(path: Path | str, identity: WorkerIdentity) -> Path:
    """Atomically replace one identity file without ever rendering its secret."""

    validated = _validate_identity_payload(identity.as_json())
    identity_path = Path(path).expanduser()
    _ensure_parent(identity_path)
    if identity_path.exists() or identity_path.is_symlink():
        try:
            current = identity_path.lstat()
        except OSError as exc:
            raise WorkerIdentityError("worker identity target cannot be inspected") from exc
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise WorkerIdentityError("worker identity target must be a regular non-symlink file")

    payload = (json.dumps(validated.as_json(), indent=2) + "\n").encode("utf-8")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=identity_path.parent,
            prefix=f".{identity_path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            # Windows ACLs are not representable through portable POSIX bits.
            pass
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, identity_path)
        temporary = None
        try:
            os.chmod(identity_path, 0o600)
        except OSError:
            pass
        if os.name == "posix":
            directory_descriptor = os.open(identity_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except WorkerIdentityError:
        raise
    except OSError as exc:
        raise WorkerIdentityError(f"worker identity file cannot be written: {identity_path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return identity_path


def write_new_worker_identity(path: Path | str, identity: WorkerIdentity) -> Path:
    """Atomically create one identity file and never replace an existing one."""

    identity_path = Path(path).expanduser()
    with _identity_creation_lock(identity_path):
        if identity_path.exists() or identity_path.is_symlink():
            raise WorkerIdentityError(
                f"worker identity file already exists: {identity_path}"
            )
        return write_worker_identity(identity_path, identity)


def create_worker_identity(
    path: Path | str,
    *,
    coordinator: str,
    node_id: str,
    credential_factory: Callable[[], str] | None = None,
) -> WorkerIdentity:
    identity_path = Path(path).expanduser()
    with _identity_creation_lock(identity_path):
        if identity_path.exists() or identity_path.is_symlink():
            raise WorkerIdentityError(
                f"worker identity file already exists: {identity_path}"
            )
        factory = credential_factory or (
            lambda: secrets.token_urlsafe(ENROLLMENT_CREDENTIAL_BYTES)
        )
        identity = WorkerIdentity(
            version=IDENTITY_VERSION,
            coordinator=normalize_coordinator(coordinator),
            node_id=normalize_worker_node_id(node_id),
            enrollment_id=None,
            credential_version=None,
            enrollment_credential=validate_worker_credential(factory()),
        )
        write_worker_identity(identity_path, identity)
        return identity


def load_or_create_worker_identity(
    path: Path | str,
    *,
    coordinator: str,
    node_id: str,
    credential_factory: Callable[[], str] | None = None,
) -> WorkerIdentity:
    identity_path = Path(path).expanduser()
    with _identity_creation_lock(identity_path):
        if identity_path.exists() or identity_path.is_symlink():
            return load_worker_identity(
                identity_path,
                coordinator=coordinator,
                node_id=node_id,
            )
        factory = credential_factory or (
            lambda: secrets.token_urlsafe(ENROLLMENT_CREDENTIAL_BYTES)
        )
        identity = WorkerIdentity(
            version=IDENTITY_VERSION,
            coordinator=normalize_coordinator(coordinator),
            node_id=normalize_worker_node_id(node_id),
            enrollment_id=None,
            credential_version=None,
            enrollment_credential=validate_worker_credential(factory()),
        )
        write_worker_identity(identity_path, identity)
        return identity


def persist_learned_enrollment(
    path: Path | str,
    identity: WorkerIdentity,
    enrollment_id: object,
    credential_version: object,
) -> WorkerIdentity:
    learned = normalize_enrollment_id(enrollment_id)
    if identity.enrollment_id is not None and identity.enrollment_id != learned:
        raise WorkerIdentityError(
            "coordinator returned a different enrollment_id for this identity"
        )
    learned_version = normalize_credential_version(credential_version)
    if (
        identity.credential_version is not None
        and identity.credential_version != learned_version
    ):
        raise WorkerIdentityError(
            "coordinator returned a different credential_version for this identity"
        )
    updated = identity.with_enrollment(learned, learned_version)
    write_worker_identity(path, updated)
    return updated
