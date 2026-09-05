"""Sampled output agreement wired into the dispatcher.

Duplicate sampling remains optional and detached. Process-local comparison
records are private diagnostics and cannot alter eligibility, queue order, or
which polling worker receives an assignment.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import routes_nodes
import routes_pitch
import server
import server_state
from verification import VerificationPool
from tests._node_session_helpers import enable_auto_node_sessions
from tests.deadline_guards import await_condition


async def _comparisons_finished() -> None:
    """Wait for every spawned background comparison to finish.

    These tests used a fixed 0.1-0.2s sleep, which on a loaded machine was long
    enough to be missed. For the two that assert something *was* recorded, a
    missed sleep read as "the background comparison never ran". For the two that
    assert nothing was recorded, it was worse: the assertion passed for the
    wrong reason, because the collector had not run yet. `_spawn_comparison`
    tracks its task, so waiting for that set to drain makes all four exact.
    """

    await await_condition(
        lambda: not routes_pitch._verify_tasks,
        what="the background comparison tasks to finish",
    )


_CODE_A = "```python\nprint('hello world from a')\n```"
_CODE_B = "```python\nprint('hello world from b')\n```"


@pytest.fixture(autouse=True)
def clean_server_state():
    for d in (
        server.nodes,
        server.task_results,
        server.task_inflight,
        server.node_failure_count,
        server.node_blacklist,
        server.jobs,
        server._pitch_timestamps,
        server_state.waiting_nodes,
    ):
        d.clear()
    server.task_queue.clear()
    server.pipeline_events.clear()
    server_state.verification_pool = VerificationPool(verify_rate=0.0)
    server_state._init_db()
    yield
    server_state.verification_pool = VerificationPool(verify_rate=0.0)


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield enable_auto_node_sessions(c)


def _register(client, node_id: str):
    return client.post(
        "/nodes/register",
        json={
            "node_id": node_id,
            "model": "qwen3.5:4b",
            "platform": "Linux",
            "machine": "x86_64",
            "hostname": node_id,
            "enrollment_action": "bootstrap",
            "enrollment_credential": f"verification-{node_id}-" + "x" * 40,
            "capability_descriptor": {
                "executor": {"kind": "ollama", "worker_protocol_version": "1"},
                "models": [{"provider": "ollama", "name": "qwen3.5:4b"}],
                "hardware": {
                    "architecture": "x86_64",
                    "logical_cpu_count": 4,
                    "total_memory_bytes": 8 * 1024**3,
                },
                "features": ["code"],
                "limits": {
                    "max_concurrent_execution_units": 1,
                    "max_output_bytes": 1_048_576,
                },
                "isolation": {"kind": "none"},
            },
        },
    )


def _record_disagreements(node_id: str, disagreements: int = 5):
    """Populate the legacy process-local agreement record."""
    identity_key = routes_nodes._verification_key(node_id)
    assert identity_key is not None
    record = server_state.verification_pool.agreement_record(identity_key)
    for _ in range(disagreements):
        record.record(False, "test")
    return record


# ── Off by default ───────────────────────────────────────────────────

def test_verify_rate_defaults_to_zero_and_disables_sampling():
    from config import DEFAULTS

    assert DEFAULTS["verify_rate"] == 0.0
    assert DEFAULTS["capability_evidence_mode"] == "off"
    assert DEFAULTS["capability_evidence_min_samples"] == 5
    assert server_state._refresh_verify_rate() == 0.0
    # Even with a large network, nothing gets duplicated.
    assert server_state.verification_pool.should_verify(available_nodes=25) is False


def test_refresh_clamps_nonsense_config(monkeypatch):
    import config

    for raw, expected in [(2.5, 1.0), (-1, 0.0), ("junk", 0.0), (None, 0.0), (0.25, 0.25)]:
        monkeypatch.setattr(config, "get", lambda _raw=raw: {"verify_rate": _raw})
        monkeypatch.setattr(server_state, "get_config", lambda _raw=raw: {"verify_rate": _raw})
        assert server_state._refresh_verify_rate() == expected


@pytest.mark.parametrize("evidence_mode", ["off", "shadow"])
def test_capability_evidence_mode_does_not_control_sampling(
    monkeypatch, evidence_mode
):
    monkeypatch.setattr(
        server_state,
        "get_config",
        lambda: {
            "deployment_mode": "local",
            "verify_rate": 0.25,
            "capability_evidence_mode": evidence_mode,
        },
    )

    assert server_state._refresh_verify_rate() == 0.25


def test_trusted_alpha_disables_process_local_sampled_verification(monkeypatch):
    """Detached samples stay off until their post-hoc state is durable."""

    monkeypatch.setattr(
        server_state,
        "get_config",
        lambda: {"deployment_mode": "trusted_alpha", "verify_rate": 1.0},
    )
    server_state.verification_pool.verify_rate = 1.0

    assert server_state._refresh_verify_rate() == 0.0
    assert server_state.verification_pool.should_verify(available_nodes=25) is False


def test_single_node_never_verifies():
    """Nobody to compare against — and it must not block a one-node network."""
    server_state.verification_pool.verify_rate = 1.0
    assert server_state.verification_pool.should_verify(available_nodes=1) is False
    assert server_state.verification_pool.should_verify(available_nodes=2) is True


# /nodes does not publish process-local agreement records

def test_nodes_endpoint_omits_sampled_agreement_data(client):
    _register(client, "alpha")
    _record_disagreements("alpha")
    body = client.get("/nodes").json()

    node = body["nodes"][0]
    assert node["node_id"] == "alpha"
    for forbidden in (
        "routing_weight",
        "trusted_for_routing",
        "verified_samples",
        "agreement_score",
        "sampled_comparisons",
        "agreement_rate",
        "agreements",
        "disagreements",
    ):
        assert forbidden not in node
    assert body["verify_rate"] == 0.0
    # The original node fields must survive the merge — the dashboard reads them.
    assert node["model"] == "qwen3.5:4b"
    assert node["tasks_completed"] == 0


def test_nodes_endpoint_stays_agreement_free_after_reconnect(client):
    _register(client, "alpha")
    _record_disagreements("alpha")
    server.nodes.clear()
    _register(client, "alpha")

    node = client.get("/nodes").json()["nodes"][0]
    assert "sampled_comparisons" not in node
    assert "routing_weight" not in node


# ── The duplicate must not land on the node it is checking ───────────

def test_duplicate_task_is_not_offered_to_the_excluded_node(client, monkeypatch):
    # Otherwise alpha's rejected poll holds the suite open for the full 25s.
    monkeypatch.setattr(server_state, "_LONG_POLL_TIMEOUT", 0.5)
    _register(client, "alpha")
    _register(client, "beta")
    server.task_queue.append({
        "task_id": "verify_1", "title": "t", "prompt": "p", "system": "s",
        "exclude_node": "alpha",
    })

    # alpha is excluded, so its poll times out with the task still queued.
    assert client.get("/tasks/next", params={"node_id": "alpha"}).status_code == 204
    assert len(server.task_queue) == 1

    got = client.get("/tasks/next", params={"node_id": "beta"})
    assert got.status_code == 200
    assert got.json()["task_id"] == "verify_1"


def test_ordinary_task_is_unaffected_by_exclusion_logic(client):
    _register(client, "alpha")
    server.task_queue.append({"task_id": "build_1", "title": "t", "prompt": "p", "system": "s"})

    got = client.get("/tasks/next", params={"node_id": "alpha"})
    assert got.status_code == 200
    assert got.json()["task_id"] == "build_1"


# Sampled agreement never affects assignment

def test_deprecated_deferral_hook_is_always_a_noop(client):
    _register(client, "alpha")
    _record_disagreements("alpha", disagreements=100)

    assert routes_nodes._should_defer("alpha", 0.0) is False


@pytest.mark.parametrize("evidence_mode", ["off", "shadow"])
def test_sampled_agreement_cannot_change_real_assignment(
    client, monkeypatch, evidence_mode
):
    _register(client, "alpha")
    _register(client, "beta")
    alpha_key = routes_nodes._verification_key("alpha")
    assert alpha_key is not None
    for _ in range(20):
        server_state.verification_pool.agreement_record(alpha_key).record(True)
    _record_disagreements("beta", disagreements=20)
    monkeypatch.setattr(
        server_state,
        "get_config",
        lambda: {
            "deployment_mode": "local",
            "verify_rate": 1.0,
            "capability_evidence_mode": evidence_mode,
        },
    )

    def fail_if_consulted(*_args):
        raise AssertionError("sampled agreement was consulted during assignment")

    monkeypatch.setattr(routes_nodes, "_should_defer", fail_if_consulted)
    server.task_queue.append(
        {"task_id": "build_9", "title": "t", "prompt": "p", "system": "s"}
    )

    got = client.get("/tasks/next", params={"node_id": "beta"})

    assert got.status_code == 200
    assert got.json()["task_id"] == "build_9"
    assert got.json()["assigned_to"] == "beta"


def test_waiting_registry_is_cleared_after_a_poll(client):
    """The compatibility poll registry is still cleaned up after each request."""
    _register(client, "alpha")
    server.task_queue.append({"task_id": "build_2", "title": "t", "prompt": "p", "system": "s"})
    client.get("/tasks/next", params={"node_id": "alpha"})
    assert "alpha" not in server_state.waiting_nodes


# ── The comparison itself never delays or breaks the deliverable ─────

@pytest.mark.asyncio
async def test_comparison_records_in_the_background_without_blocking():
    pool = server_state.verification_pool

    async def slow_duplicate(_tid, _budget):
        await asyncio.sleep(0.05)
        return {
            "node_id": "beta",
            "enrollment_id": "enrollment-beta",
            "output": _CODE_B,
        }

    routes_pitch._spawn_comparison(
        "verify_1", "subtask", "job_1", "trace_1", "alpha", _CODE_A,
        slow_duplicate, pool, primary_enrollment_id="enrollment-alpha",
    )
    # The pipeline has already moved on: nothing recorded yet. This one is the
    # subject of the test, and asserts on ordering rather than on elapsed time.
    assert pool.agreement_record("enrollment:enrollment-alpha").total == 0

    await _comparisons_finished()
    assert pool.agreement_record("enrollment:enrollment-alpha").total == 1
    assert pool.agreement_record("enrollment:enrollment-beta").total == 1
    assert pool.agreement_record("enrollment:enrollment-alpha").agreed == 1


@pytest.mark.asyncio
async def test_disagreement_is_recorded_for_both_nodes():
    pool = server_state.verification_pool

    async def prose_duplicate(_tid, _budget):
        return {
            "node_id": "beta",
            "enrollment_id": "enrollment-beta",
            "output": "I'm sorry, I can't help with that.",
        }

    routes_pitch._spawn_comparison(
        "verify_2", "subtask", "job_1", "trace_1", "alpha", _CODE_A,
        prose_duplicate, pool, primary_enrollment_id="enrollment-alpha",
    )
    await _comparisons_finished()
    assert pool.agreement_record("enrollment:enrollment-alpha").disagreed == 1
    assert pool.agreement_record("enrollment:enrollment-beta").disagreed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [
    None,                                        # duplicate never arrived
    {"node_id": "beta", "output": "", "error": "boom"},   # duplicate failed
])
async def test_absent_or_failed_duplicate_records_nothing(outcome):
    pool = server_state.verification_pool

    async def duplicate(_tid, _budget):
        return outcome

    routes_pitch._spawn_comparison(
        "verify_3", "subtask", "job_1", "trace_1", "alpha", _CODE_A, duplicate, pool,
    )
    # Sleeping a tenth of a second here made the assertion vacuous under load:
    # if the collector had not run yet, "nothing was recorded" was true for the
    # wrong reason. Waiting for the task to finish makes it a real assertion.
    await _comparisons_finished()
    assert pool.agreement_records == {}


@pytest.mark.asyncio
async def test_a_broken_spot_check_cannot_fail_the_deliverable():
    pool = server_state.verification_pool

    async def exploding(_tid, _budget):
        raise RuntimeError("collector blew up")

    routes_pitch._spawn_comparison(
        "verify_4", "subtask", "job_1", "trace_1", "alpha", _CODE_A, exploding, pool,
    )
    await _comparisons_finished()  # the exception must not propagate
    assert pool.agreement_records == {}
