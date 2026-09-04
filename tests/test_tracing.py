"""Trace context across the worker boundary: what it carries and what it cannot.

The claims under test, in the order the ADR makes them:

* off is off - no header read, none written, no span built
* the optional dependency is optional, and `import server` proves it
* propagation and export are separate, and export is never a condition of joining
* attributes are an allowlist, enforced by Python rather than by a scanner
* nothing carrying content can reach a span, an event, or a log
* high-cardinality identifiers belong in spans and in no metric label
* trace context changes no routing, admission, settlement, credit, or terminal state
* a worker-supplied `traceparent` is untrusted, bounded, and never echoed back
"""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import config as config_module
import tracing
import tracing_middleware
from tests.protocol_harness import (
    ADMISSION_SECRET,
    CREDENTIALS,
    NODE_LABELS,
    REQUESTER_HOSTS,
    TASK_TEXTS,
    WORKER_OUTPUTS,
    CoordinatorHarness,
)

#: The tests below scan the source tree. `conftest.isolated_cwd` chdirs every
#: test into an empty temp directory, so a relative glob finds nothing at all
#: and every scan built on one passes without looking at a single file - the
#: exact failure ROADMAP section 2 warns about. Resolved from `__file__`, and
#: every scan asserts it actually read something.
REPO_ROOT = Path(__file__).resolve().parent.parent


def _source_files() -> list[Path]:
    found = sorted(REPO_ROOT.glob("*.py")) + sorted(REPO_ROOT.glob("execution/*.py"))
    assert len(found) > 20, f"the source scan found only {len(found)} files"
    return found


VALID_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
VALID_SPAN_ID = "00f067aa0ba902b7"
VALID_TRACEPARENT = f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01"


@pytest.fixture
def harness(tmp_path):
    root = Path(tempfile.mkdtemp(prefix="tracing-", dir=tmp_path))
    built = CoordinatorHarness(root / "state")
    try:
        yield built
    finally:
        built.close()
        tracing.recorder.clear()
        shutil.rmtree(root, ignore_errors=True)


def _enrol(built: CoordinatorHarness, label: str = NODE_LABELS[0]) -> None:
    response = built.register(label, CREDENTIALS[0], "bootstrap")
    assert response.status_code == 200, response.text


def _queue_one(built: CoordinatorHarness, task_id: str = "t0") -> str:
    response = built.submit_execution(
        host=REQUESTER_HOSTS[0], task=TASK_TEXTS[0], idempotency_key=None
    )
    assert response.status_code == 202, response.text
    execution_id = response.json()["execution_id"]
    built.enqueue_unit(task_id, execution_id=execution_id, unit_id=f"candidate-{task_id}")
    return execution_id


# ── the dependency really is optional ────────────────────────────────


def test_the_sdk_is_absent_here_so_the_no_sdk_claims_are_being_tested():
    """Verify the negative result before trusting it (ROADMAP section 2).

    Every "works without the SDK" assertion below is vacuous if the SDK happens
    to be installed, so the precondition is stated rather than assumed.

    This is also how the API-versus-SDK distinction was found. `opentelemetry`
    imports fine in this environment - `mcp` depends on `opentelemetry-api` -
    while `opentelemetry.sdk` does not exist, and an API-only install returns a
    tracer that records nothing. A bridge accepting the API alone would have
    reported `exporting` while exporting nothing at all.
    """
    assert importlib.util.find_spec("opentelemetry.sdk") is None, (
        "the OpenTelemetry SDK is installed in this environment, so the tests "
        "asserting that the disabled and propagating paths work without it are "
        "no longer testing that"
    )


def test_server_imports_with_the_optional_dependency_absent():
    import server

    assert server.app is not None


def test_the_coordinator_runs_where_opentelemetry_cannot_be_imported():
    """The stronger form, since `opentelemetry-api` is present here anyway.

    A subprocess that refuses every `opentelemetry` import is the only honest
    way to answer "does this work on a clean machine?" from a machine that is
    not clean. See tests/no_opentelemetry_probe.py.
    """
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests" / "no_opentelemetry_probe.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    assert "ok" in completed.stdout


def test_no_module_imports_opentelemetry_at_module_scope():
    """A top-level import would make the extra required rather than optional.

    Parsed rather than grepped: a substring search for "opentelemetry" matches
    this file, every docstring that names it, and the guarded import inside the
    bridge - which is the Theme 4A lesson about probes that match themselves.
    """
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module scope only, not nested function bodies
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(name.split(".")[0] == "opentelemetry" for name in names):
                offenders.append(str(path))
    assert offenders == [], f"opentelemetry imported at module scope in {offenders}"


def test_export_stays_off_when_only_the_api_is_installed(monkeypatch):
    """An API-only install must not be mistaken for a working exporter."""
    tracing.reset_bridge_for_tests()
    monkeypatch.setattr(
        tracing, "get_config", lambda: {"tracing_enabled": True, "tracing_export": True}
    )
    assert tracing.propagation_enabled() is True
    assert tracing.export_enabled() is False, (
        "export claimed to be on with no SDK installed"
    )
    tracing.reset_bridge_for_tests()


def test_export_defaults_off_and_is_independent_of_propagation():
    defaults = config_module.DEFAULTS
    assert defaults["tracing_enabled"] is False
    assert defaults["tracing_export"] is False
    assert defaults["tracing_endpoint"] == ""


def test_joining_never_requires_export(monkeypatch, harness):
    """Export is the contributor's choice, so a node joins with it off."""
    monkeypatch.setattr(tracing, "get_config", lambda: harness.settings)
    harness.enable_tracing(export=False)
    _enrol(harness)
    execution_id = _queue_one(harness)
    handout = harness.poll(NODE_LABELS[0])
    assert handout is not None, "a node could not take work with export disabled"
    body = harness.result_body(handout)
    assert harness.submit(handout.task_id, body, label=NODE_LABELS[0]).status_code == 200
    assert execution_id


# ── off is off ───────────────────────────────────────────────────────


def test_disabled_tracing_reads_no_header_writes_none_and_builds_no_span(harness):
    _enrol(harness)
    _queue_one(harness)
    tracing.recorder.clear()
    response = harness.client.get(
        "/tasks/next",
        params={"node_id": NODE_LABELS[0]},
        headers={**harness.headers(NODE_LABELS[0]), "traceparent": VALID_TRACEPARENT},
    )
    assert response.status_code == 200
    assert "traceparent" not in response.headers
    assert tracing.recorder.recent() == []
    assert tracing.context_from_headers({"traceparent": VALID_TRACEPARENT}) is None


def test_disabled_span_yields_a_shared_no_op():
    with tracing.span("x") as first, tracing.span("y") as second:
        assert first is second, "the disabled path allocated a span object"
        assert first.headers() == {}
        first.set_status("error")
    assert tracing.recorder.recent() == []


def test_disabled_subprocess_environment_is_empty():
    assert tracing.subprocess_environment(
        tracing.TraceContext(VALID_TRACE_ID, VALID_SPAN_ID)
    ) == {}


# ── parsing untrusted input ──────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        b"00-" + b"a" * 32,
        "not-a-traceparent",
        "00-" + "z" * 32 + "-" + "b" * 16 + "-01",
        "00-" + "0" * 32 + "-" + "b" * 16 + "-01",
        "00-" + "a" * 32 + "-" + "0" * 16 + "-01",
        "ff-" + "a" * 32 + "-" + "b" * 16 + "-01",
        "00-" + "a" * 32 + "-" + "b" * 16 + "-01-extra",
        "00-" + "a" * 32 + "-" + "b" * 16 + "-01" + "x" * 400,
        "00-" + "A" * 32 + "-" + "b" * 16 + "-01",
    ],
)
def test_malformed_trace_context_is_refused(value):
    assert tracing.parse_trace_context(value) is None


def test_a_future_version_with_extra_fields_is_still_usable():
    parsed = tracing.parse_trace_context(f"01-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01-xyz")
    assert parsed is not None and parsed.trace_id == VALID_TRACE_ID


@pytest.mark.parametrize(
    "value",
    ["", "no-equals-sign", "x" * 600, ",".join(f"k{i}=v" for i in range(40)), "K=v"],
)
def test_malformed_tracestate_is_dropped_without_losing_the_traceparent(value):
    parsed = tracing.parse_trace_context(VALID_TRACEPARENT, value)
    assert parsed is not None, "a bad tracestate invalidated a good traceparent"
    assert parsed.state is None


def test_valid_tracestate_survives():
    parsed = tracing.parse_trace_context(VALID_TRACEPARENT, "vendor=1,other=2")
    assert parsed is not None and parsed.state == "vendor=1,other=2"


# ── the allowlist ────────────────────────────────────────────────────


def test_the_allowlist_is_derived_from_the_signature_not_a_copy_of_it():
    """The constant and the enforcement cannot drift apart.

    `ATTRIBUTE_ALLOWLIST` is built by calling `attributes`, so this checks the
    derivation actually reaches every keyword-only parameter rather than
    agreeing with a hand-maintained list.
    """
    signature = set((tracing.attributes.__kwdefaults__ or {}).keys())
    assert signature, "attributes() has no keyword-only parameters to derive from"
    derived = {key.removeprefix("mycelium.") for key in tracing.ATTRIBUTE_ALLOWLIST}
    assert derived == signature
    assert all(key.startswith("mycelium.") for key in tracing.ATTRIBUTE_ALLOWLIST)


@pytest.mark.parametrize(
    "forbidden",
    [
        "prompt",
        "output",
        "artifact",
        "schema",
        "credential",
        "session_token",
        "nonce",
        "idempotency_key",
        "error_message",
        "hostname",
        "requester_key",
        "viewer_key",
    ],
)
def test_the_allowlist_has_no_slot_for_content(forbidden):
    assert f"mycelium.{forbidden}" not in tracing.ATTRIBUTE_ALLOWLIST
    with pytest.raises(TypeError):
        tracing.attributes(**{forbidden: "x"})


def test_a_raw_attribute_dict_outside_the_allowlist_is_refused(monkeypatch):
    monkeypatch.setattr(tracing, "get_config", lambda: {"tracing_enabled": True})
    with pytest.raises(tracing.TracingAttributeError) as caught:
        with tracing.span("x", attributes={"mycelium.prompt": "secret text"}):
            pass
    assert "mycelium.prompt" in str(caught.value)
    assert "secret text" not in str(caught.value), (
        "the rejection reported the value it was rejecting"
    )


def test_annotating_with_an_unknown_key_is_a_type_error(monkeypatch):
    monkeypatch.setattr(tracing, "get_config", lambda: {"tracing_enabled": True})
    with tracing.span("x") as handle:
        with pytest.raises(TypeError):
            tracing.annotate(handle, prompt="secret text")


def test_attribute_values_are_bounded():
    built = tracing.attributes(execution_id="e" * 4096)
    assert len(built["mycelium.execution_id"]) == tracing.MAX_ATTRIBUTE_VALUE_LENGTH


def test_every_annotate_call_site_uses_only_allowlisted_names():
    """No code path can set a key outside the allowlist.

    `attributes()` already makes an unknown key a TypeError at runtime, so this
    is the static half: every keyword actually written at a call site is one the
    signature accepts, which is checked by parsing rather than by matching text.
    """
    signature = set((tracing.attributes.__kwdefaults__ or {}).keys())
    checked = 0
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None)
            if name not in {"annotate", "annotate_request", "attributes"}:
                continue
            supplied = {kw.arg for kw in node.keywords if kw.arg}
            checked += 1
            unknown = supplied - signature - {"request", "handle"}
            assert not unknown, f"{path} sets span attributes outside the allowlist: {unknown}"
    assert checked >= 4, "the call-site scan found nothing, so it proves nothing"


# ── spans on the enabled path ────────────────────────────────────────


def test_the_enabled_path_emits_a_span_with_the_expected_structure(harness):
    harness.enable_tracing()
    _enrol(harness)
    execution_id = _queue_one(harness)
    handout = harness.poll(NODE_LABELS[0])
    assert handout is not None

    handout_spans = [s for s in harness.spans() if s.name == "mycelium.worker.task_handout"]
    assert handout_spans, f"no handout span; got {[s.name for s in harness.spans()]}"
    span = handout_spans[-1]
    assert len(span.trace_id) == 32 and len(span.span_id) == 16
    assert span.ended_at is not None and span.ended_at >= span.started_at
    assert span.status == "ok"
    assert span.attributes["mycelium.execution_id"] == execution_id
    assert span.attributes["mycelium.attempt_id"] == handout.attempt_id
    assert span.attributes["mycelium.node_label"] == NODE_LABELS[0]
    assert span.attributes["mycelium.http_status"] == "200"


def test_context_survives_coordinator_worker_coordinator(harness):
    harness.enable_tracing()
    _enrol(harness)
    _queue_one(harness)
    handout = harness.poll(NODE_LABELS[0])
    assert handout is not None
    assert harness.worker_trace_headers[NODE_LABELS[0]]["traceparent"]

    body = harness.result_body(handout)
    assert harness.submit(handout.task_id, body, label=NODE_LABELS[0]).status_code == 200

    by_name = {}
    for span in harness.spans():
        by_name.setdefault(span.name, []).append(span)
    handout_span = by_name["mycelium.worker.task_handout"][-1]
    result_span = by_name["mycelium.worker.result_submission"][-1]
    assert result_span.trace_id == handout_span.trace_id, (
        "the result submission started a new trace instead of continuing the handout's"
    )
    assert result_span.parent_span_id == handout_span.span_id, (
        "the result span did not name the handout span as its parent"
    )


def test_a_worker_that_ignores_the_headers_still_works_unchanged(harness):
    """Propagation is offered, never required."""
    harness.enable_tracing()
    _enrol(harness)
    _queue_one(harness)
    handout = harness.poll(NODE_LABELS[0])
    assert handout is not None
    harness.worker_trace_headers.clear()  # a worker that never looked

    body = harness.result_body(handout)
    response = harness.submit(handout.task_id, body, label=NODE_LABELS[0])
    assert response.status_code == 200
    result_spans = [s for s in harness.spans() if s.name == "mycelium.worker.result_submission"]
    assert result_spans, "a silent worker's request was not traced at all"
    assert len(result_spans[-1].trace_id) == 32, "no trace id was minted for it"


def test_a_malformed_traceparent_changes_nothing_and_is_never_echoed(harness):
    harness.enable_tracing()
    _enrol(harness)
    _queue_one(harness)
    poison = "00-" + "z" * 32 + "-oh-no-" + "!" * 64
    response = harness.client.get(
        "/tasks/next",
        params={"node_id": NODE_LABELS[0]},
        headers={**harness.headers(NODE_LABELS[0]), "traceparent": poison},
    )
    assert response.status_code == 200, "a malformed header affected admission"
    assert response.json().get("attempt_id"), "a malformed header cost the worker its work"
    echoed = response.headers.get("traceparent", "")
    assert poison not in echoed
    assert "z" * 32 not in json.dumps(dict(response.headers))
    assert len(echoed) == 55, "the response did not carry a freshly minted context"


def test_an_oversized_traceparent_is_bounded_and_harmless(harness):
    harness.enable_tracing()
    _enrol(harness)
    _queue_one(harness)
    response = harness.client.get(
        "/tasks/next",
        params={"node_id": NODE_LABELS[0]},
        headers={
            **harness.headers(NODE_LABELS[0]),
            "traceparent": VALID_TRACEPARENT + "x" * 4096,
        },
    )
    assert response.status_code == 200
    assert VALID_TRACE_ID not in response.headers.get("traceparent", "")


# ── containment ──────────────────────────────────────────────────────


def test_trace_context_changes_no_settlement_credit_or_terminal_outcome():
    """The same sequence, traced and untraced, settles identically.

    Takes no `harness` fixture: one coordinator owns a state directory at a
    time (ADR 0006), so the two runs are built and torn down in sequence.
    """

    def run(traced: bool) -> dict:
        root = Path(tempfile.mkdtemp(prefix="compare-", dir=Path.cwd()))
        built = CoordinatorHarness(root / "state")
        try:
            if traced:
                built.enable_tracing()
            _enrol(built)
            _queue_one(built)
            handout = built.poll(NODE_LABELS[0])
            assert handout is not None
            accepted = built.submit(
                handout.task_id, built.result_body(handout), label=NODE_LABELS[0]
            )
            rejected = built.submit(
                handout.task_id,
                built.result_body(handout, output=WORKER_OUTPUTS[1]),
                label=NODE_LABELS[0],
            )
            return {
                "accepted": accepted.status_code,
                "rejected": rejected.status_code,
                "receipts": len(built.durable_receipts()),
                "credits": len(built.durable_credits()),
                "attempt_states": sorted(
                    row["state"] for row in built.durable_attempts().values()
                ),
            }
        finally:
            built.close()
            shutil.rmtree(root, ignore_errors=True)

    untraced = run(False)
    traced = run(True)
    assert traced == untraced, f"tracing changed the outcome: {untraced} -> {traced}"


def test_no_forbidden_value_reaches_a_span_across_a_full_execution(harness):
    harness.enable_tracing()
    _enrol(harness)
    _queue_one(harness)
    handout = harness.poll(NODE_LABELS[0])
    assert handout is not None
    harness.submit(handout.task_id, harness.result_body(handout), label=NODE_LABELS[0])
    harness.submit(
        handout.task_id,
        harness.result_body(handout, nonce="campaign-not-the-nonce"),
        label=NODE_LABELS[0],
    )

    forbidden = (
        (ADMISSION_SECRET, "the admission secret"),
        (CREDENTIALS[0], "an enrollment credential"),
        (handout.nonce, "an attempt nonce"),
        (harness.session_tokens[NODE_LABELS[0]], "a session token"),
        (TASK_TEXTS[0], "prompt text"),
        (WORKER_OUTPUTS[0], "output text"),
    )
    spans = harness.spans()
    assert spans, "nothing was traced, so this proves nothing"
    rendered = json.dumps([span.attributes for span in spans])
    for value, kind in forbidden:
        assert value not in rendered, f"a span carried {kind}"
    for span in spans:
        outside = set(span.attributes) - tracing.ATTRIBUTE_ALLOWLIST
        assert not outside, f"span {span.name} carried keys outside the allowlist: {outside}"


def test_tracing_adds_nothing_to_the_event_stream_or_the_logs(harness):
    harness.enable_tracing()
    _enrol(harness)
    _queue_one(harness)
    handout = harness.poll(NODE_LABELS[0])
    assert handout is not None
    harness.submit(handout.task_id, harness.result_body(handout), label=NODE_LABELS[0])

    span = [s for s in harness.spans() if s.name == "mycelium.worker.task_handout"][-1]
    events = harness.client.get("/events").json()
    blob = json.dumps(events) + "\n".join(harness.log_lines())
    assert span.trace_id not in blob, (
        "a trace id reached the event stream or the logs; spans and events are "
        "joined on the identifiers they already share, not by writing one into "
        "the other"
    )
    assert harness.scan_for_secrets() == []


# ── cardinality ──────────────────────────────────────────────────────


def test_high_cardinality_identifiers_are_in_spans_and_in_no_metric_label(harness):
    harness.enable_tracing()
    _enrol(harness)
    execution_id = _queue_one(harness)
    handout = harness.poll(NODE_LABELS[0])
    assert handout is not None

    span = [s for s in harness.spans() if s.name == "mycelium.worker.task_handout"][-1]
    unbounded = {execution_id, handout.attempt_id, handout.task_id}
    assert unbounded <= set(span.attributes.values()), (
        "the identifiers that make a trace useful are missing from the span"
    )

    metrics = harness.client.get("/metrics").json()
    rendered = json.dumps(metrics)
    for identifier in unbounded:
        assert identifier not in rendered, (
            "a high-cardinality identifier reached /metrics: it would become a "
            "label with unbounded values"
        )
    assert all(not key.startswith("mycelium.") for key in metrics), (
        "a span attribute key leaked into the metrics surface"
    )


def test_the_metrics_surface_is_unchanged(harness):
    """Nothing about the existing telemetry is removed or reshaped here."""
    harness.enable_tracing()
    _enrol(harness)
    metrics = harness.client.get("/metrics").json()
    assert set(metrics) == {
        "orchestrator_id",
        "orchestrator_credits",
        "tasks_completed_total",
        "tasks_in_queue",
        "tasks_inflight",
        "nodes_online",
        "nodes_blacklisted",
        "jobs_running",
        "jobs_queued",
        "avg_task_latency_seconds",
    }


# ── the middleware's own boundary ────────────────────────────────────


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("GET", "/tasks/next", "mycelium.worker.task_handout"),
        ("POST", "/tasks/t0/result", "mycelium.worker.result_submission"),
        ("POST", "/tasks/t0/stream", "mycelium.worker.token_batch"),
        ("POST", "/tasks/t0/tokens", "mycelium.worker.token_batch"),
        ("POST", "/nodes/n0/heartbeat", "mycelium.worker.heartbeat"),
        ("POST", "/nodes/n0/drain", "mycelium.worker.drain"),
        ("POST", "/nodes/register", "mycelium.worker.registration"),
        ("GET", "/dashboard", None),
        ("GET", "/metrics", None),
        ("GET", "/v1/executions/abc", None),
        ("POST", "/pitch", None),
        ("GET", "/tasks/t0/result", None),
    ],
)
def test_only_the_worker_boundary_is_traced(method, path, expected):
    assert tracing_middleware.span_name_for(method, path) == expected


def test_the_validator_subprocess_receives_context_as_environment_not_payload(
    monkeypatch, tmp_path
):
    """ADR 0013 keeps the control message minimal; this adds nothing to it."""
    import execution.validator_process as validator_process
    from execution.validator_protocol import ValidatorRunnerRequestV2

    monkeypatch.setattr(tracing, "get_config", lambda: {"tracing_enabled": True})
    with tracing.span("mycelium.validator.run") as handle:
        environment = validator_process._sanitized_environment(tmp_path)
    assert environment["TRACEPARENT"].startswith("00-")
    assert handle.record.trace_id in environment["TRACEPARENT"]
    assert set(ValidatorRunnerRequestV2.model_fields) == {
        "protocol_version",
        "validator_name",
        "validator_version",
        "output_reference",
        "contract",
        "staged_files",
        "limits",
    }, "the validator control message gained a field; it was supposed to gain none"


def test_the_child_reads_back_what_the_parent_wrote(monkeypatch):
    monkeypatch.setenv("TRACEPARENT", VALID_TRACEPARENT)
    monkeypatch.setenv("TRACESTATE", "vendor=1")
    inherited = tracing.context_from_environment()
    assert inherited is not None
    assert inherited.trace_id == VALID_TRACE_ID
    assert inherited.state == "vendor=1"


def test_a_poisoned_environment_variable_is_refused(monkeypatch):
    monkeypatch.setenv("TRACEPARENT", "$(rm -rf /)")
    assert tracing.context_from_environment() is None
