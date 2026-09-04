"""One span per worker-boundary request, and the header that continues it.

Kept out of `tracing.py` deliberately: a worker imports that module to read and
write two headers, and it must not drag a web framework onto a contributor's
machine to do it. This half runs only inside the coordinator.

Only the worker boundary is traced. Tracing the dashboard, the artifact reads,
or the static routes would buy nothing for the question ROADMAP section 6 asks -
following one job end to end across machines - and every span is memory.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

import tracing

#: (method, path predicate) -> span name. Matched in order.
_WORKER_BOUNDARY_SPANS: tuple[tuple[str, str, str], ...] = (
    ("GET", "/tasks/next", "mycelium.worker.task_handout"),
    ("POST", "/result", "mycelium.worker.result_submission"),
    ("POST", "/stream", "mycelium.worker.token_batch"),
    ("POST", "/tokens", "mycelium.worker.token_batch"),
    ("POST", "/heartbeat", "mycelium.worker.heartbeat"),
    ("POST", "/drain", "mycelium.worker.drain"),
    ("POST", "/nodes/register", "mycelium.worker.registration"),
)


def span_name_for(method: str, path: str) -> str | None:
    """The span this request belongs in, or ``None`` to leave it untraced."""
    for wanted_method, marker, name in _WORKER_BOUNDARY_SPANS:
        if method != wanted_method:
            continue
        if marker.startswith("/tasks/") or marker.startswith("/nodes/"):
            if path == marker:
                return name
        elif path.endswith(marker) and (
            path.startswith("/tasks/") or path.startswith("/nodes/")
        ):
            return name
    return None


class TraceContextMiddleware(BaseHTTPMiddleware):
    """Open a span for a worker-boundary request and hand its ID back.

    A worker that sends no `traceparent` is not penalised: the coordinator mints
    one, so an operator can still follow their own incident. A worker that sends
    a malformed one is treated exactly as one that sent none - the value is
    dropped, nothing about admission changes, and it is never written into the
    response, because reflecting it would put a stranger's string in the
    coordinator's own output.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not tracing.propagation_enabled():
            return await call_next(request)
        name = span_name_for(request.method, request.url.path)
        if name is None:
            return await call_next(request)

        parent = tracing.context_from_headers(request.headers)
        with tracing.span(name, parent=parent) as handle:
            setattr(request.state, tracing.REQUEST_STATE_ATTRIBUTE, handle)
            response = await call_next(request)
            tracing.annotate(handle, http_status=response.status_code)
            if response.status_code >= 500:
                handle.set_status("error")
            for header, value in tracing.response_headers(handle).items():
                response.headers[header] = value
            return response
