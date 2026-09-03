"""Compatibility helpers for pre-session worker route tests.

Older focused tests exercise attempt and scheduling behavior rather than HTTP
session plumbing.  This wrapper behaves like a real worker: it remembers each
registration grant and supplies the matching session for later requests.  New
session-specific tests deliberately do not use it.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def enable_auto_node_sessions(client):
    tokens: dict[str, str] = {}
    original_request = client.request

    def request(method, url, **kwargs):
        payload = kwargs.get("json") or {}
        params = kwargs.get("params") or {}
        node_id = payload.get("node_id") or params.get("node_id")
        headers = dict(kwargs.get("headers") or {})
        if node_id and "X-Node-Session" not in headers:
            token = tokens.get(str(node_id).strip().casefold())
            if token:
                headers["X-Node-Session"] = token
        if headers:
            kwargs["headers"] = headers

        response = original_request(method, url, **kwargs)
        path = urlsplit(str(url)).path
        if path == "/nodes/register" and response.status_code == 200:
            body = response.json()
            tokens[str(body["node_id"])] = str(body["session_token"])
        return response

    client.request = request
    client.node_session_tokens = tokens
    return client


def age_node_session(record, seconds: float) -> None:
    """Backdate a live session so it reads as idle.

    Heartbeat recency is measured on ``time.monotonic()``, so a test that moved
    only ``last_seen`` would be backdating the operator display and nothing the
    coordinator actually decides with.
    """

    record.last_seen -= seconds
    if record.last_seen_monotonic is not None:
        record.last_seen_monotonic -= seconds


def age_node_record(node: dict, seconds: float) -> None:
    """Backdate a process-local node registry entry the same way."""

    if "last_seen" in node:
        node["last_seen"] = float(node["last_seen"]) - seconds
    if node.get("last_seen_monotonic") is not None:
        node["last_seen_monotonic"] = float(node["last_seen_monotonic"]) - seconds
