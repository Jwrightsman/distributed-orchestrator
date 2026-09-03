"""Process-local, server-issued sessions for admitted worker nodes.

``node_secret`` authorizes initial durable enrollment in trusted alpha; it is
not a worker identity. This module issues a short-lived session after durable
enrollment authentication and binds it to the immutable enrollment, current
credential version, and normalized display label. Local compatibility sessions
remain explicitly unenrolled.

Only a SHA-256 digest of the high-entropy session token is retained.  Sessions
are intentionally process-local: a coordinator restart invalidates every token
and workers must register again.  This is collision resistance for a trusted
guild, not durable or cryptographic node identity.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


NODE_ID_MAX_LENGTH = 64
SESSION_TOKEN_MAX_LENGTH = 512
DEFAULT_SESSION_TTL_SECONDS = 24 * 60 * 60
DEFAULT_STALE_AFTER_SECONDS = 90

_NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")
_MISSING_DIGEST = "0" * 64


class InvalidNodeId(ValueError):
    """A claimed node label cannot be normalized safely."""


class DuplicateNodeSession(RuntimeError):
    """A different live session already owns a normalized node label."""


class NodeSessionDescriptorConflict(RuntimeError):
    """A live session attempted to replace its immutable capability claim."""


class InvalidNodeSession(RuntimeError):
    """A worker request did not present the current live session token."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def normalize_node_id(value: str) -> str:
    """Return the canonical trusted-alpha node label or raise ``ValueError``.

    Node labels appear in URLs, logs, attempt bindings, and contribution rows.
    Restricting them to a compact ASCII alphabet avoids visually distinct labels
    normalizing to the same durable contributor and keeps every use bounded.
    """

    normalized = str(value).strip().casefold()
    if not normalized:
        raise InvalidNodeId("node_id cannot be empty")
    if len(normalized) > NODE_ID_MAX_LENGTH:
        raise InvalidNodeId(
            f"node_id must be {NODE_ID_MAX_LENGTH} characters or fewer"
        )
    if not _NODE_ID_RE.fullmatch(normalized):
        raise InvalidNodeId(
            "node_id may contain only ASCII letters, numbers, '.', '_', ':', and '-'"
        )
    return normalized


def idle_seconds(
    record: "NodeSessionRecord",
    *,
    wall_now: float,
    monotonic_now: float,
) -> float:
    """How long since this session was last heard from.

    Staleness is a duration, not a deadline. Reading it from the monotonic
    source means a coordinator whose wall clock jumps forward does not conclude
    that every connected worker went silent while it was not looking.
    """

    if record.last_seen_monotonic is None:
        return wall_now - record.last_seen
    return monotonic_now - record.last_seen_monotonic


def token_digest(token: str) -> str:
    """One-way digest for a random session bearer token."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


@dataclass
class NodeSessionRecord:
    node_id: str
    session_id: str
    token_digest: str = field(repr=False)
    session_started_at: float
    last_seen: float
    expires_at: float
    # Heartbeat recency is an elapsed duration, so it is measured on a source a
    # wall-clock correction cannot move. ``last_seen`` above stays on the wall
    # clock because it is what operators are shown. ``None`` means a record
    # predating this field, which falls back to the wall clock exactly as before.
    last_seen_monotonic: float | None = None
    enrollment_id: str | None = None
    credential_version: int | None = None
    capability_descriptor_version: str | None = None
    capability_descriptor_hash: str | None = None

    def public_metadata(self) -> dict[str, str | float]:
        """Return non-secret metadata safe for registration and node views."""

        return {
            "node_id": self.node_id,
            "session_id": self.session_id,
            "enrollment_id": self.enrollment_id,
            "credential_version": self.credential_version,
            "capability_descriptor_version": self.capability_descriptor_version,
            "capability_descriptor_hash": self.capability_descriptor_hash,
            "session_started_at": _iso_timestamp(self.session_started_at),
            "session_expires_at": _iso_timestamp(self.expires_at),
            "last_seen": self.last_seen,
        }


@dataclass(frozen=True)
class NodeSessionGrant:
    record: NodeSessionRecord
    session_token: str = field(repr=False)
    idempotent: bool
    replaced_session_id: str | None = None


class NodeSessionRegistry:
    """Thread-safe process-local authority for worker session claims."""

    def __init__(
        self,
        *,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    ):
        if session_ttl_seconds <= 0 or stale_after_seconds <= 0:
            raise ValueError("session TTL and stale timeout must be positive")
        self.session_ttl_seconds = float(session_ttl_seconds)
        self.stale_after_seconds = float(stale_after_seconds)
        self._sessions: dict[str, NodeSessionRecord] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _token_matches(record: NodeSessionRecord | None, token: str | None) -> bool:
        # Hash even a missing/oversized token and compare against a fixed-width
        # value so the valid-token path always uses constant-time comparison.
        supplied = str(token or "")
        if len(supplied) > SESSION_TOKEN_MAX_LENGTH:
            supplied = supplied[:SESSION_TOKEN_MAX_LENGTH]
        supplied_digest = token_digest(supplied)
        expected_digest = record.token_digest if record is not None else _MISSING_DIGEST
        return hmac.compare_digest(supplied_digest, expected_digest)

    def register(
        self,
        node_id: str,
        *,
        enrollment_id: str | None = None,
        credential_version: int | None = None,
        capability_descriptor_version: str | None = None,
        capability_descriptor_hash: str | None = None,
        presented_token: str | None = None,
        before_grant: Callable[[], None] | None = None,
        now: float | None = None,
        monotonic_now: float | None = None,
    ) -> NodeSessionGrant:
        """Create, reclaim, or idempotently refresh one normalized claim.

        A different legacy claimant cannot replace a live, recently-seen
        session. A caller already authenticated as the same durable enrollment
        may replace its earlier process incarnation immediately, which makes a
        lost bootstrap response or worker-process restart recoverable. A claim
        otherwise becomes reclaimable after absolute expiry or the documented
        staleness interval. If the exact live token and enrollment binding are
        presented, registration is idempotent and the server echoes that token;
        it never needs to retain the plaintext itself. ``before_grant`` runs
        under the registry lock only after conflicts have been rejected and
        before the session is refreshed or installed, so durable claim storage
        can fail closed without recording a rejected mutation.
        """

        normalized = normalize_node_id(node_id)
        normalized_enrollment = str(enrollment_id).strip().lower() if enrollment_id else None
        if normalized_enrollment is None and credential_version is not None:
            raise ValueError("an unenrolled session cannot have a credential version")
        if normalized_enrollment is not None:
            if len(normalized_enrollment) > 64:
                raise ValueError("enrollment_id must be 64 characters or fewer")
            try:
                normalized_version = int(credential_version)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "an enrolled session requires a positive credential version"
                ) from exc
            if normalized_version < 1:
                raise ValueError(
                    "an enrolled session requires a positive credential version"
                )
        else:
            normalized_version = None
        if (capability_descriptor_version is None) != (
            capability_descriptor_hash is None
        ):
            raise ValueError(
                "capability descriptor version and hash must be supplied together"
            )
        if capability_descriptor_version is not None:
            normalized_descriptor_version = str(
                capability_descriptor_version
            ).strip()
            normalized_descriptor_hash = str(capability_descriptor_hash).strip().lower()
            if (
                not normalized_descriptor_version
                or len(normalized_descriptor_version) > 16
                or any(
                    ord(character) < 33 or ord(character) > 126
                    for character in normalized_descriptor_version
                )
            ):
                raise ValueError(
                    "capability descriptor version must be 1-16 printable ASCII characters"
                )
            if len(normalized_descriptor_hash) != 64 or any(
                character not in "0123456789abcdef"
                for character in normalized_descriptor_hash
            ):
                raise ValueError("capability descriptor hash must be lowercase SHA-256")
        else:
            normalized_descriptor_version = None
            normalized_descriptor_hash = None
        current_time = time.time() if now is None else float(now)
        monotonic_time = (
            time.monotonic() if monotonic_now is None else float(monotonic_now)
        )
        with self._lock:
            existing = self._sessions.get(normalized)
            authority_binding_matches = bool(
                existing is not None
                and existing.enrollment_id == normalized_enrollment
                and existing.credential_version == normalized_version
            )
            descriptor_binding_matches = bool(
                existing is not None
                and existing.capability_descriptor_version
                == normalized_descriptor_version
                and existing.capability_descriptor_hash == normalized_descriptor_hash
            )
            current_token_matches = bool(
                existing is not None
                and current_time < existing.expires_at
                and authority_binding_matches
                and self._token_matches(existing, presented_token)
            )
            if current_token_matches and not descriptor_binding_matches:
                raise NodeSessionDescriptorConflict(
                    "capability descriptor is immutable for the current node session; "
                    "drain or establish a new session"
                )
            if (
                current_token_matches
                and descriptor_binding_matches
            ):
                if before_grant is not None:
                    before_grant()
                existing.last_seen = current_time
                existing.last_seen_monotonic = monotonic_time
                return NodeSessionGrant(
                    record=existing,
                    session_token=str(presented_token),
                    idempotent=True,
                )

            claim_is_active = bool(
                existing is not None
                # The absolute session expiry stays on the wall clock; only the
                # heartbeat-recency half of this decision is a duration.
                and current_time < existing.expires_at
                and idle_seconds(
                    existing,
                    wall_now=current_time,
                    monotonic_now=monotonic_time,
                )
                <= self.stale_after_seconds
            )
            authorized_durable_replacement = bool(
                existing is not None
                and normalized_enrollment is not None
                and existing.enrollment_id in (None, normalized_enrollment)
            )
            if claim_is_active and not authorized_durable_replacement:
                raise DuplicateNodeSession(
                    f"node_id '{normalized}' already has an active session"
                )

            if before_grant is not None:
                before_grant()
            plaintext = secrets.token_urlsafe(32)
            replacement = existing.session_id if existing is not None else None
            record = NodeSessionRecord(
                node_id=normalized,
                session_id=secrets.token_hex(16),
                token_digest=token_digest(plaintext),
                session_started_at=current_time,
                last_seen=current_time,
                last_seen_monotonic=monotonic_time,
                expires_at=current_time + self.session_ttl_seconds,
                enrollment_id=normalized_enrollment,
                credential_version=normalized_version,
                capability_descriptor_version=normalized_descriptor_version,
                capability_descriptor_hash=normalized_descriptor_hash,
            )
            self._sessions[normalized] = record
            return NodeSessionGrant(
                record=record,
                session_token=plaintext,
                idempotent=False,
                replaced_session_id=replacement,
            )

    def authenticate(
        self,
        node_id: str,
        token: str | None,
        *,
        now: float | None = None,
        monotonic_now: float | None = None,
        touch: bool = True,
    ) -> NodeSessionRecord:
        """Validate one bearer token and return its current session binding."""

        try:
            normalized = normalize_node_id(node_id)
        except InvalidNodeId as exc:
            raise InvalidNodeSession(str(exc)) from exc
        current_time = time.time() if now is None else float(now)
        with self._lock:
            record = self._sessions.get(normalized)
            matches = self._token_matches(record, token)
            if record is None or not token or not matches:
                raise InvalidNodeSession("invalid or missing node session")
            if current_time >= record.expires_at:
                raise InvalidNodeSession("node session expired; register again")
            if touch:
                record.last_seen = current_time
                record.last_seen_monotonic = (
                    time.monotonic() if monotonic_now is None else float(monotonic_now)
                )
            return record

    def invalidate_node(
        self,
        node_id: str,
        *,
        session_id: str | None = None,
    ) -> bool:
        """Invalidate the current claim, optionally only if its id matches."""

        try:
            normalized = normalize_node_id(node_id)
        except InvalidNodeId:
            return False
        with self._lock:
            record = self._sessions.get(normalized)
            if record is None:
                return False
            if session_id is not None and not hmac.compare_digest(
                record.session_id, str(session_id)
            ):
                return False
            self._sessions.pop(normalized, None)
            return True

    def current(self, node_id: str) -> NodeSessionRecord | None:
        try:
            normalized = normalize_node_id(node_id)
        except InvalidNodeId:
            return None
        with self._lock:
            return self._sessions.get(normalized)

    def invalidate_enrollment(self, enrollment_id: str) -> list[NodeSessionRecord]:
        """Invalidate every live session bound to one durable enrollment."""

        normalized = str(enrollment_id or "").strip().lower()
        if not normalized:
            return []
        removed: list[NodeSessionRecord] = []
        with self._lock:
            for node_id, record in list(self._sessions.items()):
                if record.enrollment_id == normalized:
                    removed.append(record)
                    self._sessions.pop(node_id, None)
        return removed

    def reset(self) -> None:
        """Invalidate every session, as happens on coordinator restart."""

        with self._lock:
            self._sessions.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
