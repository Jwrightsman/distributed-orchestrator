"""Verification wired into the dispatcher — the parts that could break production.

`tests/test_verification.py` covers the scoring module in isolation. This file
covers what wiring it in changed: task routing, the duplicate task's placement,
the /nodes payload, and — most importantly — that all of it is inert at the
default verify_rate of 0.

The bar for every test here is behaviour a real network would hit: a node that
must not grade its own homework, and a node that must never be starved because
it once disagreed.
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

import routes_nodes
import routes_pitch
import server
import server_state
from verification import MIN_SAMPLES_FOR_ROUTING, VerificationPool
from tests._node_session_helpers import enable_auto_node_sessions

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
    return client.post("/nodes/register", json={
        "node_id": node_id, "model": "qwen3.5:4b", "platform": "Linux",
        "machine": "x86_64", "hostname": node_id,
    })


def _degrade(node_id: str, disagreements: int = MIN_SAMPLES_FOR_ROUTING):
    """Give a node a real, routing-eligible record of disagreement."""
    rep = server_state.verification_pool.reputation(node_id)
    for _ in range(disagreements):
        rep.record(False, "test")
    return rep


# ── Off by default ───────────────────────────────────────────────────

def test_verify_rate_defaults_to_zero_and_disables_sampling():
    from config import DEFAULTS

    assert DEFAULTS["verify_rate"] == 0.0
    assert server_state._refresh_verify_rate() == 0.0
    # Even with a large network, nothing gets duplicated.
    assert server_state.verification_pool.should_verify(available_nodes=25) is False


def test_refresh_clamps_nonsense_config(monkeypatch):
    import config

    for raw, expected in [(2.5, 1.0), (-1, 0.0), ("junk", 0.0), (None, 0.0), (0.25, 0.25)]:
        monkeypatch.setattr(config, "get", lambda _raw=raw: {"verify_rate": _raw})
        monkeypatch.setattr(server_state, "get_config", lambda _raw=raw: {"verify_rate": _raw})
        assert server_state._refresh_verify_rate() == expected


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


# ── /nodes surfaces the record ───────────────────────────────────────

def test_nodes_endpoint_exposes_routing_weight(client):
    _register(client, "alpha")
    body = client.get("/nodes").json()

    node = body["nodes"][0]
    assert node["node_id"] == "alpha"
    assert node["routing_weight"] == 1.0        # unmeasured nodes are not suspects
    assert node["verified_samples"] == 0
    assert node["trusted_for_routing"] is False
    assert body["verify_rate"] == 0.0
    # The original node fields must survive the merge — the dashboard reads them.
    assert node["model"] == "qwen3.5:4b"
    assert node["tasks_completed"] == 0


def test_reputation_survives_a_node_disconnecting(client):
    _register(client, "alpha")
    _degrade("alpha")
    server.nodes.clear()          # node drops off the network
    _register(client, "alpha")    # ...and comes back

    node = client.get("/nodes").json()["nodes"][0]
    assert node["verified_samples"] == MIN_SAMPLES_FOR_ROUTING
    assert node["routing_weight"] < 1.0


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


# ── Routing preference: first refusal, never exclusion ───────────────

def test_no_deferral_when_verification_is_off():
    """Every weight is 1.0, so there is nothing to rank — must be a no-op."""
    now = time.time()
    server_state.waiting_nodes.update({"alpha": now, "beta": now})
    assert routes_nodes._should_defer("alpha", now) is False
    assert routes_nodes._should_defer("beta", now) is False


def test_worse_node_defers_to_better_one():
    now = time.time()
    server_state.waiting_nodes.update({"alpha": now, "beta": now})
    _degrade("beta")

    assert routes_nodes._should_defer("beta", now) is True    # worse waits
    assert routes_nodes._should_defer("alpha", now) is False  # better proceeds


def test_worse_node_is_never_starved():
    """After the grace period it takes the work regardless of reputation."""
    now = time.time()
    server_state.waiting_nodes.update({"alpha": now, "beta": now})
    _degrade("beta")

    stale = now - server_state._ROUTING_DEFER - 0.01
    assert routes_nodes._should_defer("beta", stale) is False


def test_lone_worse_node_does_not_defer_to_an_absent_better_one():
    now = time.time()
    _degrade("beta")
    server_state.waiting_nodes.update({"beta": now})           # alpha is not polling
    assert routes_nodes._should_defer("beta", now) is False

    # ...nor to one whose poll went stale.
    server_state.waiting_nodes["alpha"] = now - server_state._WAITING_FRESH - 1
    assert routes_nodes._should_defer("beta", now) is False


def test_deferral_does_not_block_the_only_available_node(client):
    """End to end: a degraded node still gets work when it is alone."""
    _register(client, "beta")
    _degrade("beta")
    server.task_queue.append({"task_id": "build_9", "title": "t", "prompt": "p", "system": "s"})

    got = client.get("/tasks/next", params={"node_id": "beta"})
    assert got.status_code == 200
    assert got.json()["task_id"] == "build_9"


def test_waiting_registry_is_cleared_after_a_poll(client):
    """A leaked entry would make an absent node look like a live contender."""
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
        return {"node_id": "beta", "output": _CODE_B}

    routes_pitch._spawn_comparison(
        "verify_1", "subtask", "job_1", "trace_1", "alpha", _CODE_A, slow_duplicate, pool,
    )
    # The pipeline has already moved on: nothing recorded yet.
    assert pool.reputation("alpha").total == 0

    await asyncio.sleep(0.2)
    assert pool.reputation("alpha").total == 1
    assert pool.reputation("beta").total == 1
    assert pool.reputation("alpha").agreed == 1  # same shape, so they agree


@pytest.mark.asyncio
async def test_disagreement_lowers_both_nodes():
    pool = server_state.verification_pool

    async def prose_duplicate(_tid, _budget):
        return {"node_id": "beta", "output": "I'm sorry, I can't help with that."}

    routes_pitch._spawn_comparison(
        "verify_2", "subtask", "job_1", "trace_1", "alpha", _CODE_A, prose_duplicate, pool,
    )
    await asyncio.sleep(0.1)
    assert pool.reputation("alpha").disagreed == 1
    assert pool.reputation("beta").disagreed == 1


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
    await asyncio.sleep(0.1)
    assert pool.reputation("alpha").total == 0


@pytest.mark.asyncio
async def test_a_broken_spot_check_cannot_fail_the_deliverable():
    pool = server_state.verification_pool

    async def exploding(_tid, _budget):
        raise RuntimeError("collector blew up")

    routes_pitch._spawn_comparison(
        "verify_4", "subtask", "job_1", "trace_1", "alpha", _CODE_A, exploding, pool,
    )
    await asyncio.sleep(0.1)  # must not propagate
    assert pool.reputation("alpha").total == 0
