"""The worker-protocol compatibility window (ADR 0015).

A distributed node population cannot be upgraded at once, so before external
operators depend on this coordinator a worker needs a defined, repeatable answer
to "what happens when the other side has changed". These tests pin that answer:
the window is advertised without a credential, an unsupported peer is refused
with a code that says which side is stale, the refusal happens before anything
durable exists, and a session established under a supported version is never
re-checked.

Nothing here bumps a version. Where a wider window is needed to exercise the
mechanism, the window is widened for the duration of one test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import server_state as state
import worker_protocol
from tests.protocol_harness import ADMISSION_SECRET, CREDENTIALS, CoordinatorHarness


@pytest.fixture
def harness(tmp_path):
    coordinator = CoordinatorHarness(Path(tmp_path) / "state")
    try:
        yield coordinator
    finally:
        coordinator.close()


def _register(harness, label, credential, *, protocol_version=None, drop_version=False):
    descriptor = harness._descriptor()
    if drop_version:
        descriptor["executor"].pop("worker_protocol_version", None)
    elif protocol_version is not None:
        descriptor["executor"]["worker_protocol_version"] = protocol_version
    payload = {
        **harness._registration(label, credential, "bootstrap"),
        "capability_descriptor": descriptor,
    }
    response = harness.client.post(
        "/nodes/register", json=payload, headers={"X-Node-Secret": ADMISSION_SECRET}
    )
    if response.status_code == 200:
        # Record the grant the way the harness does for its own registrations, so
        # later worker calls in the same test present a real session.
        body = response.json()
        harness.session_tokens[label] = body["session_token"]
        harness.session_ids[label] = body["session_id"]
        harness.minted_secrets.add(body["session_token"])
    return response


def _nothing_durable_was_created(harness, label: str) -> None:
    assert state.enrollment_store.get_by_node(label) is None, (
        "a refused worker left a durable enrollment behind"
    )
    assert state.node_sessions.current(label) is None, (
        "a refused worker was issued a session"
    )
    assert label not in state.nodes


# ── the window this PR does not move ─────────────────────────────────


def test_the_shipped_window_admits_exactly_the_shipped_version():
    """This PR defines the mechanism; it does not exercise it."""
    assert worker_protocol.NODE_PROTOCOL_MIN == 1
    assert worker_protocol.NODE_PROTOCOL_MAX == 1
    assert worker_protocol.supported_versions() == ("1",)
    assert worker_protocol.DEFAULT_WORKER_PROTOCOL_VERSION == "1"
    assert worker_protocol.classify(
        worker_protocol.DEFAULT_WORKER_PROTOCOL_VERSION
    ) == "supported", "the default a descriptor falls back to must be inside the window"


def test_the_stock_worker_registers_with_no_operator_change(harness):
    """The version the shipped worker actually sends, unmodified."""
    import node

    assert node is not None  # the stock worker module exists and is importable
    response = _register(harness, "n0", CREDENTIALS[0])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enrollment_id"]
    assert body["session_token"]
    assert state.node_sessions.current("n0") is not None


def test_a_descriptor_that_omits_the_version_is_still_admitted(harness):
    """An older descriptor that never carried the field has not become
    incompatible; it falls back to the default, which is inside the window."""
    response = _register(harness, "n0", CREDENTIALS[0], drop_version=True)

    assert response.status_code == 200, response.text
    snapshot = state.capability_snapshot_store
    assert snapshot is not None


# ── refusals ─────────────────────────────────────────────────────────


def test_a_worker_below_the_window_is_refused_as_too_old(harness, monkeypatch):
    monkeypatch.setattr(worker_protocol, "NODE_PROTOCOL_MIN", 2)
    monkeypatch.setattr(worker_protocol, "NODE_PROTOCOL_MAX", 3)

    response = _register(harness, "n0", CREDENTIALS[0], protocol_version="1")

    assert response.status_code == 426, response.text
    detail = response.json()["detail"]
    assert detail["code"] == worker_protocol.CODE_TOO_OLD
    assert detail["action"] == worker_protocol.ACTION_UPGRADE_WORKER
    assert detail["node_protocol_min"] == "2"
    assert detail["node_protocol_max"] == "3"
    assert "2" in detail["message"] and "3" in detail["message"]
    assert response.headers["X-Node-Protocol-Min"] == "2"
    _nothing_durable_was_created(harness, "n0")


def test_a_worker_above_the_window_is_refused_as_too_new(harness, monkeypatch):
    monkeypatch.setattr(worker_protocol, "NODE_PROTOCOL_MIN", 1)
    monkeypatch.setattr(worker_protocol, "NODE_PROTOCOL_MAX", 1)

    response = _register(harness, "n0", CREDENTIALS[0], protocol_version="2")

    assert response.status_code == 426, response.text
    detail = response.json()["detail"]
    assert detail["code"] == worker_protocol.CODE_TOO_NEW
    assert detail["action"] == worker_protocol.ACTION_UPGRADE_COORDINATOR
    _nothing_durable_was_created(harness, "n0")


def test_too_old_and_too_new_are_distinguishable_by_code(harness, monkeypatch):
    """An operator running behind needs different advice from one running ahead."""
    monkeypatch.setattr(worker_protocol, "NODE_PROTOCOL_MIN", 2)
    monkeypatch.setattr(worker_protocol, "NODE_PROTOCOL_MAX", 2)

    behind = _register(harness, "n0", CREDENTIALS[0], protocol_version="1")
    ahead = _register(harness, "n1", CREDENTIALS[1], protocol_version="3")

    behind_detail = behind.json()["detail"]
    ahead_detail = ahead.json()["detail"]
    assert behind_detail["code"] != ahead_detail["code"]
    assert behind_detail["action"] != ahead_detail["action"]
    assert behind_detail["action"] == "upgrade_worker"
    assert ahead_detail["action"] == "upgrade_coordinator"


@pytest.mark.parametrize("declared", ["", "one", "1.0", "-1", " 1", "1 ", "01", "9" * 9])
def test_a_malformed_version_is_refused_with_one_stable_code(harness, declared):
    response = _register(harness, "n0", CREDENTIALS[0], protocol_version=declared)

    assert response.status_code == 422, f"{declared!r}: {response.text}"
    detail = response.json()["detail"]
    if isinstance(detail, list):
        # Rejected by the request model's own length bounds before the window is
        # consulted. That is still a refusal before anything durable exists, and
        # it still names no enrollment.
        _nothing_durable_was_created(harness, "n0")
        return
    assert detail["code"] == worker_protocol.CODE_MALFORMED
    # The guarantee is that a malformed declaration is never echoed back as a
    # value. A naive substring check would trip on " 1" appearing inside "1
    # through 1", which is the window, not the sender's input.
    assert "declared_worker_protocol_version" not in detail, (
        "a malformed declaration was reflected back to its sender"
    )
    _nothing_durable_was_created(harness, "n0")


@pytest.mark.parametrize("declared", ["1", "2", "3"])
def test_every_version_inside_the_window_is_admitted(harness, monkeypatch, declared):
    """Both boundary values and the interior, against a widened window."""
    monkeypatch.setattr(worker_protocol, "NODE_PROTOCOL_MIN", 1)
    monkeypatch.setattr(worker_protocol, "NODE_PROTOCOL_MAX", 3)

    response = _register(harness, "n0", CREDENTIALS[0], protocol_version=declared)

    assert response.status_code == 200, response.text
    assert state.node_sessions.current("n0") is not None


# ── checked twice, not per request ───────────────────────────────────


def test_an_established_session_is_not_rechecked_for_its_lifetime(harness, monkeypatch):
    """Version checking is a registration-time gate, not per-request overhead.

    The window is narrowed *after* the session is granted, to a range the session's
    own declared version now falls outside. Every worker operation must keep
    working: a coordinator that revoked live sessions on an upgrade would drop the
    entire fleet's in-flight work the moment it moved its own window.
    """
    assert _register(harness, "n0", CREDENTIALS[0]).status_code == 200
    execution = harness.submit_execution(
        host="10.0.0.1", task="synthetic-task-alpha", idempotency_key=None
    )
    execution_id = execution.json()["execution_id"]
    harness.enqueue_unit("u0", execution_id=execution_id, unit_id="candidate-u0")

    # The coordinator moves on. The live session declared version 1.
    monkeypatch.setattr(worker_protocol, "NODE_PROTOCOL_MIN", 2)
    monkeypatch.setattr(worker_protocol, "NODE_PROTOCOL_MAX", 2)

    handout = harness.poll("n0")
    assert handout is not None, "a live session lost its lease over a window change"
    assert harness.submit("u0", harness.result_body(handout), label="n0").status_code == 200
    assert harness.client.post(
        "/nodes/n0/heartbeat", json={"node_id": "n0"}, headers=harness.headers("n0")
    ).status_code in (200, 404)

    # But a *new* registration at that version is now refused.
    assert _register(harness, "n1", CREDENTIALS[1], protocol_version="1").status_code == 426


def test_the_refusal_happens_before_enrollment_even_on_a_second_attempt(harness, monkeypatch):
    """A refused worker cannot accumulate partial state by retrying."""
    monkeypatch.setattr(worker_protocol, "NODE_PROTOCOL_MIN", 5)
    monkeypatch.setattr(worker_protocol, "NODE_PROTOCOL_MAX", 5)

    for _ in range(3):
        assert _register(harness, "n0", CREDENTIALS[0], protocol_version="1").status_code == 426
    _nothing_durable_was_created(harness, "n0")
    assert state.enrollment_store.count() == 0


# ── the advertised surface ───────────────────────────────────────────


def test_the_window_is_readable_without_a_credential(harness):
    response = harness.client.get("/v1/worker-protocol")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["node_protocol_min"] == "1"
    assert body["node_protocol_max"] == "1"
    assert body["supported_worker_protocol_versions"] == ["1"]
    assert body["server_version"] == worker_protocol.SERVER_VERSION


def test_the_advertised_surface_exposes_versions_and_nothing_else(harness):
    harness.settings["viewer_key"] = "a-configured-viewer-key-long-enough-to-pass"
    response = harness.client.get("/v1/worker-protocol")
    assert response.status_code == 200, "the window must be readable before enrolling"

    body = response.json()
    assert set(body) == {
        "node_protocol_min",
        "node_protocol_max",
        "supported_worker_protocol_versions",
        "server_version",
    }
    rendered = response.text
    for leaked in (
        "build",
        "uptime",
        "nodes_online",
        "deployment_mode",
        "trusted_alpha",
        "ollama",
        "localhost",
        "127.0.0.1",
        "events.db",
        "model",
        "queue",
        "hostname",
    ):
        assert leaked not in rendered, f"the window surface leaked {leaked!r}"


# ── the classifier, directly ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "verdict"),
    [
        ("1", "supported"),
        ("0", "too_old"),
        ("2", "too_new"),
        ("", "malformed"),
        ("01", "malformed"),
        ("1.0", "malformed"),
        (" 1", "malformed"),
        ("v1", "malformed"),
        (None, "malformed"),
        (1, "malformed"),
        (True, "malformed"),
    ],
)
def test_the_classifier_is_strict_about_what_a_version_is(value, verdict):
    assert worker_protocol.classify(value) == verdict


def test_a_refusal_never_reflects_a_malformed_declaration():
    detail = worker_protocol.refusal_detail("malformed", "<script>alert(1)</script>")
    assert "script" not in str(detail)
    assert detail["code"] == worker_protocol.CODE_MALFORMED
