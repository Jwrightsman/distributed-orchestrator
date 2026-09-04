"""W3C trace context across the coordinator/worker boundary.

A cross-machine timeout, reassignment, or settlement incident currently has to
be reconstructed from custom events on one side of the boundary. A trace ID that
survives coordinator -> worker -> coordinator lets one incident be read as one
thing.

Three states, and the difference between them matters:

``off`` (the default)
    ``tracing_enabled`` is false. Every function here is a no-op: no headers are
    read, none are written, no span is built, and nothing is allocated per
    request. This is what a deployment that never opts in runs.

``propagating``
    ``tracing_enabled`` is true and no OpenTelemetry SDK is installed. Trace
    context is accepted, validated, minted when absent, and handed onward, and
    spans are recorded in-process. Nothing leaves the machine. This is useful on
    its own: the coordinator's own records can be correlated by trace ID.

``exporting``
    ``tracing_enabled`` and ``tracing_export`` are both true *and* the
    OpenTelemetry **SDK** is importable. Spans additionally reach a collector
    the operator configured.

    The SDK, not the API. ``opentelemetry-api`` arrives on its own as a
    transitive dependency of ``mcp``, and on its own it hands back a
    non-recording tracer - so a deployment with only the API installed would
    have reported that it was exporting while sending nothing anywhere. That
    was found by a test asserting its own precondition rather than by anyone
    noticing an empty collector.

**Propagation and export are separate decisions on purpose.** A worker runs on a
machine somebody else owns. Accepting a trace ID and handing it back costs that
contributor nothing and is not telemetry. Exporting that machine's spans to
somebody else's collector *is* telemetry leaving a stranger's machine, so it is
a separate switch, off by default, and never a condition of joining. See
ROADMAP section 2, "Contributor rights are not negotiable".

**Attributes are an allowlist, not a scan.** :func:`attributes` takes
keyword-only parameters, so a key outside the set is a ``TypeError`` from Python
itself rather than something a scanner has to notice. Finding F8 is the reason:
a probe that searched text for short needles reported leaks that were not there,
and a denylist for span attributes would be the same mistake with the polarity
flipped. Nothing carrying content - a prompt, an output, an artifact, a schema,
a credential, a session token, an attempt nonce, an idempotency key, worker
error text, or a node-supplied hostname - has a slot to go in.

Trace context is **diagnostic only**. Nothing here is read by routing,
eligibility, admission, settlement, credit, validation, or terminal state, and a
worker-supplied ``traceparent`` is untrusted input: bounded, validated, and
never echoed back.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from config import get as get_config

logger = logging.getLogger("mycelium.tracing")

# -- the wire format --------------------------------------------------
#
# https://www.w3.org/TR/trace-context/. Version 00 is 55 characters:
# "00-" + 32 hex trace id + "-" + 16 hex span id + "-" + 2 hex flags.

TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"
TRACE_CONTEXT_HEADERS = (TRACEPARENT_HEADER, TRACESTATE_HEADER)

MAX_TRACEPARENT_BYTES = 256
MAX_TRACESTATE_BYTES = 512
MAX_TRACESTATE_MEMBERS = 32
MAX_ATTRIBUTE_VALUE_LENGTH = 128

_VERSION_00 = "00"
_INVALID_VERSION = "ff"
_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16

_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})"
    r"-(?P<trace_id>[0-9a-f]{32})"
    r"-(?P<parent_id>[0-9a-f]{16})"
    r"-(?P<flags>[0-9a-f]{2})"
    r"(?P<tail>-.*)?$"
)
# Deliberately conservative: printable ASCII without the characters that would
# let a value break out of a log line or a header.
_TRACESTATE_MEMBER_RE = re.compile(r"^[a-z0-9_\-*/@]{1,256}=[ -~]{0,256}$")

SAMPLED_FLAG = 0x01


class TracingAttributeError(ValueError):
    """A span attribute key outside the allowlist reached the boundary."""


@dataclass(frozen=True)
class TraceContext:
    """One validated W3C trace context.

    ``parent_id`` is the span this process is a child of when the context
    arrived on a request, or this process's own span when it was minted here.
    """

    trace_id: str
    parent_id: str
    flags: str = "01"
    state: str | None = None

    @property
    def sampled(self) -> bool:
        return bool(int(self.flags, 16) & SAMPLED_FLAG)

    def traceparent(self) -> str:
        return f"{_VERSION_00}-{self.trace_id}-{self.parent_id}-{self.flags}"

    def headers(self) -> dict[str, str]:
        """The two headers to put on an outbound request."""
        outbound = {TRACEPARENT_HEADER: self.traceparent()}
        if self.state:
            outbound[TRACESTATE_HEADER] = self.state
        return outbound

    def child(self) -> "TraceContext":
        """A context naming a fresh span in the same trace."""
        return TraceContext(
            trace_id=self.trace_id,
            parent_id=new_span_id(),
            flags=self.flags,
            state=self.state,
        )


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def parse_tracestate(value: object) -> str | None:
    """Validate a ``tracestate``, or discard it.

    Per the specification an unparsable ``tracestate`` is dropped while a valid
    ``traceparent`` is still honoured, so this returns ``None`` rather than
    invalidating the whole context.
    """
    if not isinstance(value, str) or not value:
        return None
    if len(value.encode("utf-8", "replace")) > MAX_TRACESTATE_BYTES:
        return None
    members = [member.strip() for member in value.split(",") if member.strip()]
    if not members or len(members) > MAX_TRACESTATE_MEMBERS:
        return None
    if not all(_TRACESTATE_MEMBER_RE.match(member) for member in members):
        return None
    return ",".join(members)


def parse_trace_context(
    traceparent: object, tracestate: object = None
) -> TraceContext | None:
    """Validate untrusted trace context. Returns ``None`` for anything invalid.

    Never raises and never reflects its input: a caller that supplied rubbish is
    told nothing about it beyond the request proceeding untraced, because an
    error body that echoed the value back would be a worker-controlled string in
    the coordinator's own response.
    """
    if not isinstance(traceparent, str) or not traceparent:
        return None
    if len(traceparent.encode("utf-8", "replace")) > MAX_TRACEPARENT_BYTES:
        return None
    match = _TRACEPARENT_RE.match(traceparent)
    if match is None:
        return None
    version = match.group("version")
    if version == _INVALID_VERSION:
        return None
    # A future version may append fields; version 00 may not.
    if version == _VERSION_00 and match.group("tail"):
        return None
    trace_id = match.group("trace_id")
    parent_id = match.group("parent_id")
    if trace_id == _ZERO_TRACE_ID or parent_id == _ZERO_SPAN_ID:
        return None
    return TraceContext(
        trace_id=trace_id,
        parent_id=parent_id,
        flags=match.group("flags"),
        state=parse_tracestate(tracestate),
    )


def context_from_headers(headers: Mapping[str, str] | None) -> TraceContext | None:
    """Read trace context off an inbound request, when tracing is enabled."""
    if headers is None or not propagation_enabled():
        return None
    return parse_trace_context(
        headers.get(TRACEPARENT_HEADER), headers.get(TRACESTATE_HEADER)
    )


def inbound_context(headers: Mapping[str, str] | None) -> TraceContext:
    """The context to trace a request under, minting one when none arrived.

    A worker that ignores the headers entirely still gets a trace, because the
    coordinator mints its own. Propagation is worth having on the coordinator's
    side alone; a contributor is never required to participate for the operator
    to be able to read their own incident.
    """
    return context_from_headers(headers) or TraceContext(new_trace_id(), new_span_id())


# -- the attribute allowlist ------------------------------------------


def attributes(
    *,
    execution_id: str | None = None,
    unit_id: str | None = None,
    unit_kind: str | None = None,
    task_id: str | None = None,
    attempt_id: str | None = None,
    receipt_id: str | None = None,
    enrollment_id: str | None = None,
    session_id: str | None = None,
    node_label: str | None = None,
    placement: str | None = None,
    strategy: str | None = None,
    lifecycle_status: str | None = None,
    validation_outcome: str | None = None,
    settlement_outcome: str | None = None,
    terminal_cause: str | None = None,
    worker_protocol_version: str | None = None,
    descriptor_version: str | None = None,
    descriptor_hash: str | None = None,
    model_provider: str | None = None,
    model_name: str | None = None,
    model_digest: str | None = None,
    validator_name: str | None = None,
    validator_version: str | None = None,
    error_code: str | None = None,
    http_status: object = None,
) -> dict[str, str]:
    """Build a span's attributes. Unknown keys are a ``TypeError``.

    This is the allowlist's enforcement, not a description of it. Every
    parameter is keyword-only and every one of them is an identifier, a bounded
    enum, or a version - never content.

    Two absences are deliberate. There is no ``hostname``: a node-supplied
    hostname is the finding recorded as F2 in Theme 4A, and ``node_label`` is
    the identity the rest of the system already displays (ADR 0016, seam 2) -
    display metadata, never a trust key. And there is no ``error_message``:
    ``error_code`` is a fixed code the coordinator chose, whereas worker error
    text is a string a stranger's machine wrote.

    Values are stringified and bounded, because an identifier that arrived
    malformed is still an identifier this must not widen.
    """
    supplied: dict[str, object] = {
        "mycelium.execution_id": execution_id,
        "mycelium.unit_id": unit_id,
        "mycelium.unit_kind": unit_kind,
        "mycelium.task_id": task_id,
        "mycelium.attempt_id": attempt_id,
        "mycelium.receipt_id": receipt_id,
        "mycelium.enrollment_id": enrollment_id,
        "mycelium.session_id": session_id,
        "mycelium.node_label": node_label,
        "mycelium.placement": placement,
        "mycelium.strategy": strategy,
        "mycelium.lifecycle_status": lifecycle_status,
        "mycelium.validation_outcome": validation_outcome,
        "mycelium.settlement_outcome": settlement_outcome,
        "mycelium.terminal_cause": terminal_cause,
        "mycelium.worker_protocol_version": worker_protocol_version,
        "mycelium.descriptor_version": descriptor_version,
        "mycelium.descriptor_hash": descriptor_hash,
        "mycelium.model_provider": model_provider,
        "mycelium.model_name": model_name,
        "mycelium.model_digest": model_digest,
        "mycelium.validator_name": validator_name,
        "mycelium.validator_version": validator_version,
        "mycelium.error_code": error_code,
        "mycelium.http_status": http_status,
    }
    return {
        key: str(value)[:MAX_ATTRIBUTE_VALUE_LENGTH]
        for key, value in supplied.items()
        if value is not None
    }


#: Every key a span may carry. Derived from :func:`attributes` by calling it, so
#: the two cannot drift: the function is the enforcement and this is the
#: statement of it. A test asserts that the derivation actually reaches the
#: signature rather than agreeing with a hand-written copy of it.
ATTRIBUTE_ALLOWLIST: frozenset[str] = frozenset(
    attributes(**{name: "x" for name in (attributes.__kwdefaults__ or {})})
)


def validated_attributes(supplied: Mapping[str, Any] | None) -> dict[str, str]:
    """Reject any attribute key outside the allowlist at the boundary."""
    if not supplied:
        return {}
    unknown = sorted(set(supplied) - ATTRIBUTE_ALLOWLIST)
    if unknown:
        # The keys are named because a key is a schema, not a value; no value is
        # reported, because a value is exactly what must not reach a log.
        raise TracingAttributeError(
            f"span attribute keys outside the allowlist: {', '.join(unknown)}"
        )
    return {
        key: str(value)[:MAX_ATTRIBUTE_VALUE_LENGTH]
        for key, value in supplied.items()
        if value is not None
    }


# -- spans ------------------------------------------------------------


@dataclass
class SpanRecord:
    """One finished span. Content-free by construction."""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    attributes: dict[str, str] = field(default_factory=dict)
    started_at: float = 0.0
    ended_at: float | None = None
    status: str = "ok"

    def traceparent(self) -> str:
        return f"{_VERSION_00}-{self.trace_id}-{self.span_id}-01"


class _SpanRecorder:
    """A bounded in-process ring of finished spans.

    It exists so the enabled path is testable without installing an SDK, and so
    an operator running in ``propagating`` mode still gets something. Bounded
    because ROADMAP section 6 lists unbounded coordinator memory as a launch
    risk, and a telemetry buffer is the classic way to acquire one. There is no
    endpoint that serves it: adding a route would be a new public surface, and
    this is a diagnostic, not an API.
    """

    def __init__(self, capacity: int = 512) -> None:
        self._spans: deque[SpanRecord] = deque(maxlen=capacity)

    def record(self, span: SpanRecord) -> None:
        self._spans.append(span)

    def recent(self) -> list[SpanRecord]:
        return list(self._spans)

    def clear(self) -> None:
        self._spans.clear()


recorder = _SpanRecorder()

#: The span this task is currently inside. A context variable rather than a
#: threaded parameter, because the one place that needs it downstream - the
#: validator subprocess spawn - is reached through `asyncio.to_thread`, which
#: copies the calling context. No signature in the validator path changes.
_CURRENT_CONTEXT: ContextVar[TraceContext | None] = ContextVar(
    "mycelium_trace_context", default=None
)


def current_context() -> TraceContext | None:
    """The trace context of the work this task is doing, if any."""
    return _CURRENT_CONTEXT.get()


class _UnitTraces:
    """Which trace a unit of work belongs to, across reassignment.

    A lease expires, the janitor reclaims the unit, and another machine takes
    it. Without this the second handout starts a fresh trace, so the
    coordinator's own view of one unit is split across two - and "where did
    this job go?" is precisely the question ROADMAP section 6 wants a trace to
    answer. It was found by a test asserting the reassignment case rather than
    assuming it worked.

    Process-local and bounded by eviction rather than by cleanup: a map that has
    to be emptied at five call sites acquires a leak the first time one of them
    is missed, and ROADMAP section 6 already lists unbounded coordinator memory
    as a launch risk. A restart empties it, which is correct - a new epoch is a
    new trace. Nothing durable, and nothing on the wire.
    """

    def __init__(self, capacity: int = 4096) -> None:
        self._capacity = capacity
        self._traces: OrderedDict[str, TraceContext] = OrderedDict()

    def remember(self, task_id: str, context: TraceContext | None) -> None:
        if not task_id or context is None:
            return
        self._traces[task_id] = context
        self._traces.move_to_end(task_id)
        while len(self._traces) > self._capacity:
            self._traces.popitem(last=False)

    def recall(self, task_id: str) -> TraceContext | None:
        return self._traces.get(task_id) if task_id else None

    def clear(self) -> None:
        self._traces.clear()


unit_traces = _UnitTraces()


def adopt(handle: Any, parent: TraceContext | None) -> None:
    """Move an open span into a trace that already existed.

    Legitimate because nothing has been recorded or exported yet - a span is
    written when it closes. The coordinator learns which unit a handout request
    is for only after it has picked one, which is after the span opened.
    """
    record = getattr(handle, "record", None)
    if record is None or parent is None:
        return
    record.trace_id = parent.trace_id
    record.parent_span_id = parent.parent_id
    moved = TraceContext(
        trace_id=parent.trace_id,
        parent_id=record.span_id,
        flags=parent.flags,
        state=parent.state,
    )
    handle.context = moved
    _CURRENT_CONTEXT.set(moved)


def continue_unit_trace(request: Any, task_id: str) -> None:
    """Keep one unit of work in one trace, however often it is reassigned."""
    handle = getattr(getattr(request, "state", None), REQUEST_STATE_ATTRIBUTE, None)
    if handle is None:
        return
    established = unit_traces.recall(task_id)
    if established is not None:
        adopt(handle, established)
    else:
        unit_traces.remember(task_id, getattr(handle, "context", None))


class _NullSpan:
    """What every call site gets when tracing is off."""

    __slots__ = ()

    context: TraceContext | None = None
    record: SpanRecord | None = None

    def set_status(self, status: str) -> None:
        return None

    def headers(self) -> dict[str, str]:
        return {}


_NULL_SPAN = _NullSpan()


class _LiveSpan:
    __slots__ = ("record", "context")

    def __init__(self, record: SpanRecord, context: TraceContext) -> None:
        self.record = record
        self.context = context

    def set_status(self, status: str) -> None:
        self.record.status = status

    def headers(self) -> dict[str, str]:
        return self.context.headers()


def propagation_enabled() -> bool:
    """Whether trace context is accepted, minted, and handed onward."""
    try:
        return bool(get_config().get("tracing_enabled", False))
    except Exception:  # pragma: no cover - configuration must never break a request
        return False


def export_enabled() -> bool:
    """Whether finished spans additionally leave this machine."""
    if not propagation_enabled():
        return False
    try:
        if not bool(get_config().get("tracing_export", False)):
            return False
    except Exception:  # pragma: no cover
        return False
    return _otel_bridge() is not None


@contextmanager
def span(
    name: str,
    *,
    parent: TraceContext | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    """Record one span, or do nothing at all.

    When tracing is off this yields a shared null object and allocates nothing
    per call. When it is on, a failure anywhere in here is contained: a
    diagnostic must never fail a request that would otherwise have succeeded.
    The one exception is an attribute key outside the allowlist, which is a
    programming error rather than a runtime condition and is raised so a test
    can see it.
    """
    if not propagation_enabled():
        yield _NULL_SPAN
        return

    checked = validated_attributes(attributes)
    try:
        context = (parent or TraceContext(new_trace_id(), new_span_id())).child()
        record = SpanRecord(
            name=name,
            trace_id=context.trace_id,
            span_id=context.parent_id,
            parent_span_id=parent.parent_id if parent else None,
            attributes=checked,
            started_at=time.monotonic(),
        )
    except Exception:  # pragma: no cover - containment
        yield _NULL_SPAN
        return

    live = _LiveSpan(record, context)
    token = _CURRENT_CONTEXT.set(context)
    try:
        yield live
    except Exception:
        record.status = "error"
        raise
    finally:
        _CURRENT_CONTEXT.reset(token)
        record.ended_at = time.monotonic()
        try:
            recorder.record(record)
            if export_enabled():
                bridge = _otel_bridge()
                if bridge is not None:
                    bridge.export(record)
        except Exception as exc:  # pragma: no cover - containment
            logger.debug("span not recorded error_type=%s", type(exc).__name__)


#: Where the active span is parked on a request, so a handler can add the
#: identifiers it learns without threading a parameter through every signature.
REQUEST_STATE_ATTRIBUTE = "mycelium_span"


def annotate(handle: Any, **named: Any) -> None:
    """Add allowlisted identifiers to a span that is already open.

    Goes through :func:`attributes`, so a key outside the allowlist is a
    ``TypeError`` raised from this call rather than an attribute that quietly
    reaches a span.
    """
    record = getattr(handle, "record", None)
    if record is None:
        return
    record.attributes.update(attributes(**named))


def annotate_request(request: Any, **named: Any) -> None:
    """Annotate the span the middleware opened for this request, if any.

    Duck-typed on purpose: this module stays free of any web framework so a
    worker can import it without pulling a server dependency onto a
    contributor's machine.
    """
    state = getattr(request, "state", None)
    handle = getattr(state, REQUEST_STATE_ATTRIBUTE, None)
    if handle is None:
        return
    annotate(handle, **named)


def response_headers(handle: Any) -> dict[str, str]:
    """The trace headers to hand a worker alongside its work."""
    getter = getattr(handle, "headers", None)
    return getter() if callable(getter) else {}


def worker_echo_headers(received: Mapping[str, str] | None) -> dict[str, str]:
    """What a worker sends back, given what arrived with its work.

    Unconditional, and not gated on the worker's own configuration, because it
    is not telemetry: the coordinator minted this trace ID, and handing it back
    is how the coordinator reads its own incident. A worker that never enables
    anything still returns it. If the coordinator sent nothing - tracing off, or
    an older coordinator - there is nothing to echo and this is empty.

    The value is re-validated on the way out rather than copied, so a malformed
    header cannot be laundered through a worker into the coordinator's next
    request.
    """
    if not received:
        return {}
    context = parse_trace_context(
        received.get(TRACEPARENT_HEADER), received.get(TRACESTATE_HEADER)
    )
    return context.headers() if context is not None else {}


def subprocess_environment(context: TraceContext | None = None) -> dict[str, str]:
    """Trace context for a child process, as environment rather than payload.

    The validator runner's control message is deliberately minimal (ADR 0013),
    and its environment is already a strict allowlist. Two variables put nothing
    at all in the control message, which is a stricter reading of the constraint
    than adding two fields to it would have been.

    Defaults to the context of the work in progress, falling back to this
    process's own inherited context so a nested spawn still joins the trace.
    """
    context = context or current_context() or context_from_environment()
    if context is None or not propagation_enabled():
        return {}
    environment = {"TRACEPARENT": context.traceparent()}
    if context.state:
        environment["TRACESTATE"] = context.state
    return environment


def context_from_environment() -> TraceContext | None:
    """The other side of :func:`subprocess_environment`, for a child process."""
    return parse_trace_context(
        os.environ.get("TRACEPARENT"), os.environ.get("TRACESTATE")
    )


# -- the optional OpenTelemetry bridge --------------------------------
#
# Never imported at module scope. `python -c "import server"` must succeed on a
# machine with nothing installed, and an import that is only attempted when an
# operator has switched export on cannot break that.

_BRIDGE: Any = None
_BRIDGE_ATTEMPTED = False


class _OtelBridge:
    """Hands finished spans to an installed OpenTelemetry SDK."""

    def __init__(self, tracer: Any) -> None:
        self._tracer = tracer

    def export(self, record: SpanRecord) -> None:
        span = self._tracer.start_span(record.name)
        try:
            for key, value in record.attributes.items():
                span.set_attribute(key, value)
        finally:
            span.end()


def _otel_bridge() -> Any:
    global _BRIDGE, _BRIDGE_ATTEMPTED
    if _BRIDGE_ATTEMPTED:
        return _BRIDGE
    _BRIDGE_ATTEMPTED = True
    try:
        # The SDK is required, not merely the API. `opentelemetry-api` is
        # already present here as a transitive dependency of `mcp`, and by
        # itself it returns a tracer that records nothing - so accepting it
        # would mean reporting `exporting` while exporting nothing.
        import opentelemetry.sdk.trace  # type: ignore  # noqa: F401
        from opentelemetry import trace as otel_trace  # type: ignore

        _BRIDGE = _OtelBridge(otel_trace.get_tracer("mycelium"))
    except Exception:
        # The optional extra is not installed, or the SDK raised while being set
        # up. Either way this stays in `propagating` mode; nothing about the
        # request path changes and nothing is logged per request.
        _BRIDGE = None
    return _BRIDGE


def reset_bridge_for_tests() -> None:
    """Forget a cached bridge decision. Test-only."""
    global _BRIDGE, _BRIDGE_ATTEMPTED
    _BRIDGE = None
    _BRIDGE_ATTEMPTED = False
