"""Process-local, server-issued sessions for admitted worker nodes.

``node_secret`` admits a machine to the trusted-alpha worker network.  It is a
shared credential and therefore cannot distinguish two workers that both know
it.  This module adds a short-lived server-issued session that binds subsequent
worker requests to one normalized node label.

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
from dataclasses import dataclass
from datetime import datetime, timezone


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


def token_digest(token: str) -> str:
    """One-way digest for a random session bearer token."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


@dataclass
class NodeSessionRecord:
    node_id: str
    session_id: str
    token_digest: str
    session_started_at: float
    last_seen: float
    expires_at: float

    def public_metadata(self) -> dict[str, str | float]:
        """Return non-secret metadata safe for registration and node views."""

        return {
            "node_id": self.node_id,
            "session_id": self.session_id,
            "session_started_at": _iso_timestamp(self.session_started_at),
            "session_expires_at": _iso_timestamp(self.expires_at),
            "last_seen": self.last_seen,
        }


@dataclass(frozen=True)
class NodeSessionGrant:
    record: NodeSessionRecord
    session_token: str
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
        presented_token: str | None = None,
        now: float | None = None,
    ) -> NodeSessionGrant:
        """Create, reclaim, or idempotently refresh one normalized claim.

        A different claimant cannot replace a live, recently-seen session.  A
        claim becomes reclaimable after either its absolute token expiry or the
        documented node-staleness interval.  If the exact live token is
        presented, registration is idempotent and the server echoes that token;
        it never needs to retain the plaintext itself.
        """

        normalized = normalize_node_id(node_id)
        current_time = time.time() if now is None else float(now)
        with self._lock:
            existing = self._sessions.get(normalized)
            if (
                existing is not None
                and current_time < existing.expires_at
                and self._token_matches(existing, presented_token)
            ):
                existing.last_seen = current_time
                return NodeSessionGrant(
                    record=existing,
                    session_token=str(presented_token),
                    idempotent=True,
                )

            claim_is_active = bool(
                existing is not None
                and current_time < existing.expires_at
                and current_time - existing.last_seen <= self.stale_after_seconds
            )
            if claim_is_active:
                raise DuplicateNodeSession(
                    f"node_id '{normalized}' already has an active session"
                )

            plaintext = secrets.token_urlsafe(32)
            replacement = existing.session_id if existing is not None else None
            record = NodeSessionRecord(
                node_id=normalized,
                session_id=secrets.token_hex(16),
                token_digest=token_digest(plaintext),
                session_started_at=current_time,
                last_seen=current_time,
                expires_at=current_time + self.session_ttl_seconds,
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

    def reset(self) -> None:
        """Invalidate every session, as happens on coordinator restart."""

        with self._lock:
            self._sessions.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
