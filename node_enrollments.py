"""Durable, independently revocable enrollment identity for worker nodes.

Enrollment credentials are high-entropy bearer secrets.  The coordinator keeps
only a domain-separated SHA-256 digest; public records deliberately exclude that
digest.  Enrollment survives coordinator restart, while the sessions bound to an
enrollment remain process-local in :mod:`node_sessions`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from node_sessions import normalize_node_id
from sqlite_store import connection, migration_lock


ENROLLMENT_CREDENTIAL_MIN_LENGTH = 32
ENROLLMENT_CREDENTIAL_MAX_LENGTH = 512
ENROLLMENT_ID_MAX_LENGTH = 64
REVOCATION_REASON_MAX_LENGTH = 500

_CREDENTIAL_DIGEST_DOMAIN = b"mycelium:node-enrollment-credential:v1\x00"
_MISSING_DIGEST = "0" * 64
_VALID_STATUSES = frozenset({"active", "revoked"})


class NodeEnrollmentError(RuntimeError):
    """Base class for stable enrollment failures safe to map to protocol errors."""

    code = "node_enrollment_error"

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class InvalidEnrollmentCredential(NodeEnrollmentError, ValueError):
    code = "invalid_enrollment_credential"


class EnrollmentLabelConflict(NodeEnrollmentError):
    code = "node_enrollment_label_conflict"


class EnrollmentCredentialConflict(NodeEnrollmentError):
    code = "node_enrollment_credential_conflict"


class EnrollmentAuthenticationFailed(NodeEnrollmentError):
    code = "node_enrollment_authentication_failed"


class EnrollmentRevoked(NodeEnrollmentError):
    code = "node_enrollment_revoked"


class EnrollmentSessionMismatch(NodeEnrollmentError):
    code = "node_enrollment_session_mismatch"


class EnrollmentCredentialRotated(NodeEnrollmentError):
    code = "node_enrollment_credential_rotated"


class EnrollmentRotationConflict(NodeEnrollmentError):
    code = "node_enrollment_rotation_conflict"


class EnrollmentNotFound(NodeEnrollmentError):
    code = "node_enrollment_not_found"


def validate_enrollment_credential(value: str) -> str:
    """Validate a bounded printable bearer credential without transforming it."""

    if not isinstance(value, str):
        raise InvalidEnrollmentCredential("enrollment credential must be text")
    if not ENROLLMENT_CREDENTIAL_MIN_LENGTH <= len(value) <= ENROLLMENT_CREDENTIAL_MAX_LENGTH:
        raise InvalidEnrollmentCredential(
            "enrollment credential must be between "
            f"{ENROLLMENT_CREDENTIAL_MIN_LENGTH} and "
            f"{ENROLLMENT_CREDENTIAL_MAX_LENGTH} characters"
        )
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise InvalidEnrollmentCredential(
            "enrollment credential must contain only printable ASCII without whitespace"
        )
    return value


def new_enrollment_credential() -> str:
    """Return a standard-library generated high-entropy enrollment bearer."""

    return secrets.token_urlsafe(32)


def _credential_digest(credential: str) -> str:
    validated = validate_enrollment_credential(credential)
    return hashlib.sha256(
        _CREDENTIAL_DIGEST_DOMAIN + validated.encode("ascii")
    ).hexdigest()


def _validate_enrollment_id(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized or len(normalized) > ENROLLMENT_ID_MAX_LENGTH:
        raise EnrollmentNotFound("node enrollment was not found")
    return normalized


def _validate_status(value: object) -> str:
    status = str(value)
    if status not in _VALID_STATUSES:
        raise RuntimeError("node enrollment has an invalid durable status")
    return status


def _iso_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class NodeEnrollmentRecord:
    """Secret-free durable enrollment metadata."""

    enrollment_id: str
    node_id: str
    status: str
    credential_version: int
    created_at: float
    rotated_at: float | None
    revoked_at: float | None
    revocation_reason: str | None
    last_registered_at: float | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "NodeEnrollmentRecord":
        return cls(
            enrollment_id=str(row["enrollment_id"]),
            node_id=str(row["node_id"]),
            status=_validate_status(row["status"]),
            credential_version=int(row["credential_version"]),
            created_at=float(row["created_at"]),
            rotated_at=(
                float(row["rotated_at"]) if row["rotated_at"] is not None else None
            ),
            revoked_at=(
                float(row["revoked_at"]) if row["revoked_at"] is not None else None
            ),
            revocation_reason=(
                str(row["revocation_reason"])
                if row["revocation_reason"] is not None
                else None
            ),
            last_registered_at=(
                float(row["last_registered_at"])
                if row["last_registered_at"] is not None
                else None
            ),
        )

    def public_metadata(self) -> dict[str, object]:
        """Return fields safe for worker grants and protected operator views."""

        return {
            "enrollment_id": self.enrollment_id,
            "node_id": self.node_id,
            "status": self.status,
            "credential_version": self.credential_version,
            "created_at": _iso_timestamp(self.created_at),
            "rotated_at": _iso_timestamp(self.rotated_at),
            "revoked_at": _iso_timestamp(self.revoked_at),
            "revocation_reason": self.revocation_reason,
            "last_registered_at": _iso_timestamp(self.last_registered_at),
        }


@dataclass(frozen=True)
class BootstrapEnrollmentResult:
    record: NodeEnrollmentRecord
    created: bool
    idempotent: bool


@dataclass(frozen=True)
class RotationResult:
    record: NodeEnrollmentRecord
    rotated: bool
    idempotent: bool


def ensure_node_enrollment_schema(con: sqlite3.Connection) -> None:
    """Create the additive durable enrollment schema on an existing connection."""

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS node_enrollments (
            enrollment_id       TEXT PRIMARY KEY,
            node_id             TEXT NOT NULL UNIQUE,
            credential_digest   TEXT NOT NULL UNIQUE,
            status              TEXT NOT NULL CHECK(status IN ('active', 'revoked')),
            credential_version  INTEGER NOT NULL DEFAULT 1
                                CHECK(credential_version >= 1),
            created_at          REAL NOT NULL,
            rotated_at          REAL,
            revoked_at          REAL,
            revocation_reason   TEXT,
            last_registered_at  REAL
        )
        """
    )
    columns = {
        str(row[1])
        for row in con.execute("PRAGMA table_info(node_enrollments)").fetchall()
    }
    if "credential_version" not in columns:
        con.execute(
            "ALTER TABLE node_enrollments ADD COLUMN credential_version "
            "INTEGER NOT NULL DEFAULT 1 CHECK(credential_version >= 1)"
        )
    invalid = con.execute(
        "SELECT 1 FROM node_enrollments "
        "WHERE status NOT IN ('active', 'revoked') LIMIT 1"
    ).fetchone()
    if invalid is not None:
        raise RuntimeError("node_enrollments contains an invalid status")


class NodeEnrollmentStore:
    """SQLite authority for enrollment bootstrap, authentication, and revocation."""

    def __init__(self, path: str | Path = "events.db"):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _ensure_schema(self, con: sqlite3.Connection) -> None:
        with migration_lock(self.path):
            ensure_node_enrollment_schema(con)

    def migrate(self) -> None:
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            ensure_node_enrollment_schema(con)
            con.commit()

    @staticmethod
    def _row_by_id(con: sqlite3.Connection, enrollment_id: str) -> sqlite3.Row | None:
        return con.execute(
            "SELECT * FROM node_enrollments WHERE enrollment_id = ?",
            (enrollment_id,),
        ).fetchone()

    @staticmethod
    def _row_by_node(con: sqlite3.Connection, node_id: str) -> sqlite3.Row | None:
        return con.execute(
            "SELECT * FROM node_enrollments WHERE node_id = ?", (node_id,)
        ).fetchone()

    def bootstrap(
        self,
        node_id: str,
        credential: str,
        *,
        candidate_enrollment_id: str | None = None,
        now: float | None = None,
    ) -> BootstrapEnrollmentResult:
        """Atomically create an enrollment or authenticate the exact existing one."""

        normalized = normalize_node_id(node_id)
        digest = _credential_digest(credential)
        candidate = (
            _validate_enrollment_id(candidate_enrollment_id)
            if candidate_enrollment_id is not None
            else uuid.uuid4().hex
        )
        registered_at = time.time() if now is None else float(now)

        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            self._ensure_schema(con)
            con.execute("BEGIN IMMEDIATE")
            try:
                by_node = self._row_by_node(con, normalized)
                if by_node is not None:
                    digest_matches = hmac.compare_digest(
                        digest, str(by_node["credential_digest"])
                    )
                    if str(by_node["status"]) == "revoked":
                        raise EnrollmentRevoked("node enrollment is revoked")
                    if not digest_matches:
                        raise EnrollmentLabelConflict(
                            "node label already belongs to another enrollment"
                        )
                    con.execute(
                        "UPDATE node_enrollments SET last_registered_at = ? "
                        "WHERE enrollment_id = ?",
                        (registered_at, str(by_node["enrollment_id"])),
                    )
                    con.commit()
                    refreshed = self._row_by_id(con, str(by_node["enrollment_id"]))
                    if refreshed is None:  # pragma: no cover - same committed row
                        raise RuntimeError("node enrollment disappeared after bootstrap")
                    return BootstrapEnrollmentResult(
                        NodeEnrollmentRecord.from_row(refreshed),
                        created=False,
                        idempotent=True,
                    )

                by_digest = con.execute(
                    "SELECT enrollment_id FROM node_enrollments "
                    "WHERE credential_digest = ?",
                    (digest,),
                ).fetchone()
                if by_digest is not None:
                    raise EnrollmentCredentialConflict(
                        "enrollment credential already belongs to another node label"
                    )

                con.execute(
                    """
                    INSERT INTO node_enrollments (
                        enrollment_id, node_id, credential_digest, status,
                        credential_version, created_at, last_registered_at
                    ) VALUES (?, ?, ?, 'active', 1, ?, ?)
                    """,
                    (candidate, normalized, digest, registered_at, registered_at),
                )
                con.commit()
                created = self._row_by_id(con, candidate)
                if created is None:  # pragma: no cover - defensive after commit
                    raise RuntimeError("node enrollment disappeared after bootstrap")
                return BootstrapEnrollmentResult(
                    NodeEnrollmentRecord.from_row(created),
                    created=True,
                    idempotent=False,
                )
            except Exception:
                if con.in_transaction:
                    con.rollback()
                raise

    def authenticate(
        self,
        node_id: str,
        credential: str,
        *,
        now: float | None = None,
    ) -> NodeEnrollmentRecord:
        """Authenticate a returning enrollment and update its registration time."""

        normalized = normalize_node_id(node_id)
        digest = _credential_digest(credential)
        registered_at = time.time() if now is None else float(now)
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            self._ensure_schema(con)
            con.execute("BEGIN IMMEDIATE")
            try:
                row = self._row_by_node(con, normalized)
                expected = str(row["credential_digest"]) if row is not None else _MISSING_DIGEST
                matches = hmac.compare_digest(digest, expected)
                if row is None or not matches:
                    raise EnrollmentAuthenticationFailed(
                        "node label or enrollment credential is invalid"
                    )
                if str(row["status"]) == "revoked":
                    raise EnrollmentRevoked("node enrollment is revoked")
                con.execute(
                    "UPDATE node_enrollments SET last_registered_at = ? "
                    "WHERE enrollment_id = ? AND status = 'active'",
                    (registered_at, str(row["enrollment_id"])),
                )
                con.commit()
                refreshed = self._row_by_id(con, str(row["enrollment_id"]))
                if refreshed is None:  # pragma: no cover - same committed row
                    raise RuntimeError("node enrollment disappeared after authentication")
                return NodeEnrollmentRecord.from_row(refreshed)
            except Exception:
                if con.in_transaction:
                    con.rollback()
                raise

    def get(self, enrollment_id: str) -> NodeEnrollmentRecord | None:
        try:
            normalized = _validate_enrollment_id(enrollment_id)
        except EnrollmentNotFound:
            return None
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = self._row_by_id(con, normalized)
        return NodeEnrollmentRecord.from_row(row) if row is not None else None

    def get_by_node(self, node_id: str) -> NodeEnrollmentRecord | None:
        try:
            normalized = normalize_node_id(node_id)
        except ValueError:
            return None
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = self._row_by_node(con, normalized)
        return NodeEnrollmentRecord.from_row(row) if row is not None else None

    def list(self) -> list[NodeEnrollmentRecord]:
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            rows = con.execute(
                "SELECT * FROM node_enrollments ORDER BY created_at, enrollment_id"
            ).fetchall()
        return [NodeEnrollmentRecord.from_row(row) for row in rows]

    def validate_session(
        self,
        enrollment_id: str,
        node_id: str,
        credential_version: int,
    ) -> NodeEnrollmentRecord:
        """Validate the durable binding carried by one process-local session."""

        normalized_id = _validate_enrollment_id(enrollment_id)
        normalized_node = normalize_node_id(node_id)
        try:
            version = int(credential_version)
        except (TypeError, ValueError) as exc:
            raise EnrollmentSessionMismatch(
                "node session has an invalid enrollment binding"
            ) from exc
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = self._row_by_id(con, normalized_id)
        if row is None or not hmac.compare_digest(str(row["node_id"]), normalized_node):
            raise EnrollmentSessionMismatch(
                "node session no longer matches a durable enrollment"
            )
        if str(row["status"]) == "revoked":
            raise EnrollmentRevoked("node enrollment is revoked")
        if int(row["credential_version"]) != version:
            raise EnrollmentCredentialRotated(
                "node enrollment credential was rotated; acquire a new session"
            )
        return NodeEnrollmentRecord.from_row(row)

    def revoke(
        self,
        enrollment_id: str,
        reason: str = "",
        *,
        now: float | None = None,
    ) -> NodeEnrollmentRecord:
        """Durably and idempotently revoke one enrollment without deleting history."""

        normalized = _validate_enrollment_id(enrollment_id)
        bounded_reason = str(reason or "").strip()[:REVOCATION_REASON_MAX_LENGTH]
        if any(ord(character) < 32 for character in bounded_reason):
            raise ValueError("revocation reason cannot contain control characters")
        revoked_at = time.time() if now is None else float(now)
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            self._ensure_schema(con)
            con.execute("BEGIN IMMEDIATE")
            try:
                row = self._row_by_id(con, normalized)
                if row is None:
                    raise EnrollmentNotFound("node enrollment was not found")
                if str(row["status"]) == "active":
                    con.execute(
                        """
                        UPDATE node_enrollments
                        SET status = 'revoked', revoked_at = ?, revocation_reason = ?
                        WHERE enrollment_id = ? AND status = 'active'
                        """,
                        (revoked_at, bounded_reason or None, normalized),
                    )
                con.commit()
                refreshed = self._row_by_id(con, normalized)
                if refreshed is None:  # pragma: no cover
                    raise RuntimeError("node enrollment disappeared after revocation")
                return NodeEnrollmentRecord.from_row(refreshed)
            except Exception:
                if con.in_transaction:
                    con.rollback()
                raise

    def rotate(
        self,
        enrollment_id: str,
        new_credential: str,
        *,
        expected_credential_version: int | None = None,
        now: float | None = None,
    ) -> RotationResult:
        """Atomically rotate a credential; retrying the same new secret is safe."""

        normalized = _validate_enrollment_id(enrollment_id)
        new_digest = _credential_digest(new_credential)
        rotated_at = time.time() if now is None else float(now)
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            self._ensure_schema(con)
            con.execute("BEGIN IMMEDIATE")
            try:
                row = self._row_by_id(con, normalized)
                if row is None:
                    raise EnrollmentNotFound("node enrollment was not found")
                if str(row["status"]) == "revoked":
                    raise EnrollmentRevoked("node enrollment is revoked")
                if hmac.compare_digest(new_digest, str(row["credential_digest"])):
                    con.commit()
                    return RotationResult(
                        NodeEnrollmentRecord.from_row(row),
                        rotated=False,
                        idempotent=True,
                    )
                current_version = int(row["credential_version"])
                if (
                    expected_credential_version is not None
                    and int(expected_credential_version) != current_version
                ):
                    raise EnrollmentRotationConflict(
                        "node enrollment changed since rotation was prepared"
                    )
                duplicate = con.execute(
                    "SELECT enrollment_id FROM node_enrollments "
                    "WHERE credential_digest = ?",
                    (new_digest,),
                ).fetchone()
                if duplicate is not None:
                    raise EnrollmentCredentialConflict(
                        "enrollment credential already belongs to another node"
                    )
                changed = con.execute(
                    """
                    UPDATE node_enrollments
                    SET credential_digest = ?, credential_version = ?, rotated_at = ?
                    WHERE enrollment_id = ? AND status = 'active'
                      AND credential_version = ?
                    """,
                    (
                        new_digest,
                        current_version + 1,
                        rotated_at,
                        normalized,
                        current_version,
                    ),
                ).rowcount
                if changed != 1:
                    raise EnrollmentRotationConflict(
                        "node enrollment changed during credential rotation"
                    )
                con.commit()
                refreshed = self._row_by_id(con, normalized)
                if refreshed is None:  # pragma: no cover
                    raise RuntimeError("node enrollment disappeared after rotation")
                return RotationResult(
                    NodeEnrollmentRecord.from_row(refreshed),
                    rotated=True,
                    idempotent=False,
                )
            except Exception:
                if con.in_transaction:
                    con.rollback()
                raise

    def count(self) -> int:
        self.migrate()
        with self._lock, connection(self.path) as con:
            row = con.execute("SELECT COUNT(*) FROM node_enrollments").fetchone()
        return int(row[0]) if row else 0
