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
