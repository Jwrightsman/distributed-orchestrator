"""Trusted-alpha viewer authentication and route classification.

Worker admission, pitch submission, and private read access are separate trust
decisions.  This module owns the third one.  A static viewer key is sufficient
for the small trusted alpha, while a signed short-lived cookie lets a browser
use the same authority without storing that key in cookie plaintext.

When no viewer key is configured the gate deliberately fails open for local
development compatibility.  That state is never silent: startup and /health
both report it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from config import get as get_config

VIEWER_COOKIE_NAME = "mycelium_viewer"
_SESSION_VERSION = "v1"
_LOG = logging.getLogger("mycelium.access")

# Routes that are intentionally reachable without a viewer credential.  This
# list is intentionally small and method-aware: a path prefix should not turn a
# future private write endpoint public by accident.
_PUBLIC_EXACT: set[tuple[str, str]] = {
    ("GET", "/"),
    ("GET", "/try"),
    ("GET", "/health"),
    ("GET", "/status.json"),
    ("POST", "/public/pitch"),
    ("POST", "/v1/viewer/session"),
    ("DELETE", "/v1/viewer/session"),
}

_PITCH_AUTH_EXACT: set[tuple[str, str]] = {
    ("POST", "/pitch"),
    ("POST", "/pitch/async"),
    ("POST", "/pitch/distributed"),
    ("POST", "/v1/executions"),
}

_WORKER_RESULT = re.compile(r"^/tasks/[^/]+/(?:result|stream)$")


def _viewer_key() -> str:
    value = get_config().get("viewer_key", "")
    return str(value or "")


def viewer_auth_configured() -> bool:
    """Whether private HTTP and WebSocket routes are currently protected."""
    return bool(_viewer_key())


def viewer_key_matches(supplied: str) -> bool:
    """Constant-time validation for the session exchange endpoint."""
    expected = _viewer_key()
    return bool(expected and supplied and secrets.compare_digest(str(supplied), expected))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def issue_viewer_session(*, now: int | None = None, ttl_seconds: int | None = None) -> tuple[str, int]:
    """Return a signed cookie value and its absolute expiry.

    The configured viewer key is used only as HMAC key material; it is never
    embedded in the cookie.  Rotating viewer_key invalidates every session.
    """
    key = _viewer_key()
    if not key:
        raise RuntimeError("viewer authentication is not configured")
    issued = int(time.time() if now is None else now)
    configured_ttl = get_config().get("viewer_session_ttl_seconds", 8 * 3600)
    try:
        ttl = int(configured_ttl if ttl_seconds is None else ttl_seconds)
    except (TypeError, ValueError):
        ttl = 8 * 3600
    ttl = max(60, min(ttl, 7 * 24 * 3600))
    expires = issued + ttl
    payload = _b64encode(
        json.dumps(
            {"exp": expires, "iat": issued, "nonce": secrets.token_urlsafe(12)},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signed = f"{_SESSION_VERSION}.{payload}"
    signature = _b64encode(hmac.new(key.encode("utf-8"), signed.encode("ascii"), hashlib.sha256).digest())
    return f"{signed}.{signature}", expires


def valid_viewer_session(cookie_value: str, *, now: int | None = None) -> bool:
    """Validate a viewer session without raising or exposing why it failed."""
    key = _viewer_key()
    if not key or not cookie_value:
        return False
    try:
        version, payload, submitted_signature = cookie_value.split(".", 2)
        if version != _SESSION_VERSION:
            return False
        signed = f"{version}.{payload}"
        expected_signature = _b64encode(
            hmac.new(key.encode("utf-8"), signed.encode("ascii"), hashlib.sha256).digest()
        )
        if not secrets.compare_digest(submitted_signature, expected_signature):
            return False
        claims: dict[str, Any] = json.loads(_b64decode(payload))
        current = int(time.time() if now is None else now)
        return int(claims["iat"]) <= current < int(claims["exp"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _bearer_value(headers) -> str:
    authorization = headers.get("Authorization", "")
    scheme, separator, value = authorization.partition(" ")
    if separator and scheme.lower() == "bearer":
        return value.strip()
    return ""


def credentials_are_valid(headers, cookies) -> bool:
    """Check all supported viewer credential transports in constant time."""
    expected = _viewer_key()
    if not expected:
        return True
    candidates = (
        headers.get("X-Viewer-Key", ""),
        _bearer_value(headers),
    )
    for supplied in candidates:
        if supplied and secrets.compare_digest(str(supplied), expected):
            return True
    return valid_viewer_session(str(cookies.get(VIEWER_COOKIE_NAME, "")))


def request_viewer_authorized(request: Request) -> bool:
    return credentials_are_valid(request.headers, request.cookies)


def websocket_viewer_authorized(websocket: WebSocket) -> bool:
    return credentials_are_valid(websocket.headers, websocket.cookies)


def require_viewer(request: Request) -> None:
    """FastAPI dependency for explicitly protected endpoints."""
    if not request_viewer_authorized(request):
        raise HTTPException(
            status_code=401,
            detail="Viewer authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def authorize_viewer_websocket(websocket: WebSocket) -> bool:
    """Authenticate before accepting a WebSocket, closing with 4401 on failure."""
    if websocket_viewer_authorized(websocket):
        return True
    await websocket.close(code=4401, reason="Viewer authentication required")
    return False


def is_public_or_separately_authenticated(method: str, path: str) -> bool:
    """Classify paths exempt from viewer auth.

    Separately authenticated means the route already applies pitch_key or
    node_secret.  It does not mean those routes are unauthenticated.
    """
    method = method.upper()
    if (method, path) in _PUBLIC_EXACT or (method, path) in _PITCH_AUTH_EXACT:
        return True
    if method == "GET" and path.startswith("/static/"):
        return True
    if method == "GET" and path.startswith("/v1/shares/"):
        return True
    if method == "POST" and path == "/nodes/register":
        return True
    if method == "GET" and path == "/tasks/next":
        return True
    if method == "POST" and _WORKER_RESULT.fullmatch(path):
        return True
    return False


class ViewerAccessMiddleware(BaseHTTPMiddleware):
    """Protect every route not present in the deliberate public allowlist."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not viewer_auth_configured():
            return await call_next(request)
        if is_public_or_separately_authenticated(request.method, request.url.path):
            return await call_next(request)
        if request_viewer_authorized(request):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"detail": "Viewer authentication required"},
            headers={"WWW-Authenticate": "Bearer"},
        )


def viewer_health_fields() -> dict[str, Any]:
    protected = viewer_auth_configured()
    warnings = [] if protected else [
        "viewer_key is not configured; task-, result-, project-, and machine-sensitive routes are unprotected"
    ]
    return {"private_routes_protected": protected, "warnings": warnings}


def warn_if_viewer_auth_unconfigured() -> None:
    if not viewer_auth_configured():
        _LOG.warning(
            "viewer_key is not configured; private read routes are unprotected. "
            "Set viewer_key before exposing this server beyond a trusted local development network."
        )
