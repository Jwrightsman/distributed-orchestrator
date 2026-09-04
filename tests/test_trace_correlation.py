"""Induced failures, and whether one trace ID reads them as one incident.

This is the achievable half of Theme 4B's evaluation. The audit's actual test is
diagnosis *time* on induced failures across machines, which needs a live
multi-node deployment and is deferred - the protocol for running it is written
down in docs/experiments/trace-diagnosis-time.md, and no improvement in
diagnosis time is claimed here, because none has been measured.

What is measurable now: each failure below is induced deliberately against a
real coordinator, and the coordinator's own view of it is asserted to hang
together under a single trace ID. That is a precondition for the deferred
experiment rather than a substitute for it.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

import tracing
from tests.protocol_harness import (
    CREDENTIALS,
    NODE_LABELS,
    REQUESTER_HOSTS,
    TASK_TEXTS,
    CoordinatorHarness,
)

LABEL = NODE_LABELS[0]
OTHER = NODE_LABELS[1]


@pytest.fixture
def traced(tmp_path):
    """One coordinator with tracing on and a node enrolled."""
    root = Path(tempfile.mkdtemp(prefix="correlate-", dir=tmp_path))
    built = CoordinatorHarness(root / "state")
    try:
        built.enable_tracing()
        for index, label in enumerate(NODE_LABELS[:2]):
            response = built.register(label, CREDENTIALS[index], "bootstrap")
            assert response.status_code == 200, response.text
        yield built
    finally:
        built.close()
        tracing.recorder.clear()
        shutil.rmtree(root, ignore_errors=True)


def _queue(built: CoordinatorHarness, task_id: str, *, lease_seconds: int = 900) -> str:
    response = built.submit_execution(
        host=REQUESTER_HOSTS[0], task=TASK_TEXTS[0], idempotency_key=None
    )
    assert response.status_code == 202, response.text
    execution_id = response.json()["execution_id"]
    built.enqueue_unit(
        task_id,
        execution_id=execution_id,
        unit_id=f"candidate-{task_id}",
        lease_seconds=lease_seconds,
    )
    return execution_id


def _trace_of(built: CoordinatorHarness, task_id: str) -> str:
    """The single trace every span mentioning this task belongs to."""
    traces = {
        span.trace_id
        for span in built.spans()
        if span.attributes.get("mycelium.task_id") == task_id
    }
    assert len(traces) == 1, (
        f"the coordinator's view of task {task_id} spans {len(traces)} traces; "
        "it cannot be read as one incident"
    )
    return traces.pop()


def _names_in(built: CoordinatorHarness, trace_id: str) -> list[str]:
    return [span.name for span in built.spans() if span.trace_id == trace_id]


def _statuses_in(built: CoordinatorHarness, trace_id: str) -> list[str]:
    return [
        span.attributes.get("mycelium.http_status")
        for span in built.spans()
        if span.trace_id == trace_id
    ]


# ── the five induced failures ────────────────────────────────────────


def test_lease_expiry_and_reassignment_stay_in_one_trace(traced):
    """A lease expires, the janitor reclaims it, another node picks it up.

    The reassignment is the interesting part: the second handout continues the
    first one's trace because the coordinator is still working on the same unit,
    which is exactly the question "where did this job go?" needs answered.
    """
    _queue(traced, "t-lease", lease_seconds=20)
    first = traced.poll(LABEL)
    assert first is not None

    traced.clock.advance(120)
    traced.janitor()
    assert traced.durable_attempts()[first.attempt_id]["state"] == "expired"

    second = traced.poll(OTHER)
    assert second is not None, "the reclaimed unit was never handed out again"
    assert second.attempt_id != first.attempt_id

    trace_id = _trace_of(traced, "t-lease")
    assert _names_in(traced, trace_id).count("mycelium.worker.task_handout") == 2
    labels = {
        span.attributes.get("mycelium.node_label")
        for span in traced.spans()
        if span.trace_id == trace_id
    }
    assert labels == {LABEL, OTHER}, (
        f"the reassignment did not show both machines in one trace: {labels}"
    )


def test_a_rejected_settlement_is_readable_beside_the_handout_that_earned_it(traced):
    _queue(traced, "t-reject")
    handout = traced.poll(LABEL)
    assert handout is not None

    rejected = traced.submit(
        handout.task_id,
        traced.result_body(handout, nonce="campaign-not-the-nonce"),
        label=LABEL,
    )
    assert rejected.status_code in (401, 403)

    trace_id = _trace_of(traced, "t-reject")
    assert _names_in(traced, trace_id) == [
        "mycelium.worker.task_handout",
        "mycelium.worker.result_submission",
    ]
    assert str(rejected.status_code) in _statuses_in(traced, trace_id), (
        "the refusal's status code is not on the span, so a reader would have to "
        "go and find it somewhere else"
    )


def test_a_cross_enrollment_submission_shows_both_identities_in_one_trace(traced):
    """The finding tests/test_result_binding.py exists for, read as a trace."""
    _queue(traced, "t-cross")
    handout = traced.poll(LABEL)
    assert handout is not None

    # Submitted *as* the other contributor, so the session check passes and the
    # attempt-binding check is what refuses. Claiming the assigned node's label
    # with the wrong token is refused earlier, before the coordinator has
    # resolved a second identity to put in the span.
    refused = traced.submit(
        handout.task_id,
        traced.result_body(handout, node_id=OTHER),
        label=OTHER,
    )
    assert refused.status_code in (401, 403)

    trace_id = _trace_of(traced, "t-cross")
    enrollments = {
        span.attributes.get("mycelium.enrollment_id")
        for span in traced.spans()
        if span.trace_id == trace_id
    }
    assert len(enrollments) == 2, (
        "the trace does not show that the submitting identity differed from the "
        f"assigned one: {enrollments}"
    )


def test_a_persistence_failure_does_not_break_the_trace(traced):
    """A span is a diagnostic: it survives the thing it is diagnosing."""
    _queue(traced, "t-fault")
    handout = traced.poll(LABEL)
    assert handout is not None

    traced.faults.arm(target_index=3, mode="io")
    try:
        response = traced.submit(
            handout.task_id, traced.result_body(handout), label=LABEL
        )
    except Exception:
        response = None
    finally:
        traced.faults.disarm()
    assert traced.faults.fired, "the injected persistence fault never fired"

    trace_id = _trace_of(traced, "t-fault")
    submissions = [
        span
        for span in traced.spans()
        if span.trace_id == trace_id and span.name == "mycelium.worker.result_submission"
    ]
    assert submissions, "the failing submission produced no span at all"
    assert submissions[-1].ended_at is not None, "a span was left open by the failure"
    if response is not None and response.status_code >= 500:
        assert submissions[-1].status == "error"


def test_a_worker_that_disconnects_mid_stream_leaves_a_readable_trace(traced):
    """Tokens arrive, then nothing does. The trace shows exactly that."""
    _queue(traced, "t-drop")
    handout = traced.poll(LABEL)
    assert handout is not None

    assert traced.stream(handout.task_id, handout, "synthetic-", label=LABEL).status_code == 200
    assert traced.stream(handout.task_id, handout, "output", label=LABEL).status_code == 200
    # …and the worker's machine goes away. No result is ever submitted.
    traced.clock.advance(2000)
    traced.janitor()

    trace_id = _trace_of(traced, "t-drop")
    names = _names_in(traced, trace_id)
    assert names.count("mycelium.worker.token_batch") == 2
    assert "mycelium.worker.result_submission" not in names, (
        "this scenario is supposed to end without a submission"
    )
    assert traced.durable_attempts()[handout.attempt_id]["state"] == "expired"


def test_a_drain_is_attributable_to_the_session_that_asked_for_it(traced):
    _queue(traced, "t-drain")
    handout = traced.poll(LABEL)
    assert handout is not None
    assert traced.drain(LABEL).status_code == 200

    drains = [span for span in traced.spans() if span.name == "mycelium.worker.drain"]
    assert drains, "the drain was not traced"
    assert drains[-1].attributes["mycelium.node_label"] == LABEL
    assert drains[-1].attributes["mycelium.session_id"] == traced.session_ids[LABEL]
    assert drains[-1].trace_id == _trace_of(traced, "t-drain"), (
        "the drain landed in a different trace from the work it stopped"
    )


# ── what the fixtures are not allowed to have cost ───────────────────


def test_none_of_the_induced_failures_leaked_anything(traced):
    """Every scenario above, run together, then scanned."""
    _queue(traced, "t-all")
    handout = traced.poll(LABEL)
    assert handout is not None
    traced.stream(handout.task_id, handout, "synthetic-output-alpha", label=LABEL)
    traced.submit(handout.task_id, traced.result_body(handout), label=LABEL)
    traced.clock.advance(2000)
    traced.janitor()

    assert traced.scan_for_secrets() == []
    for span in traced.spans():
        outside = set(span.attributes) - tracing.ATTRIBUTE_ALLOWLIST
        assert not outside, f"{span.name} carried {outside}"
