"""A node that outlives its own registry entry must be able to get back in.

The failure this pins, found Aug 13 while checking what happens when Jett shuts
his laptop:

1. `node.py` long-polls `/tasks/next`, which keeps `last_seen` fresh.
2. The laptop sleeps. Nothing reaches the server for more than `_NODE_TIMEOUT`
   (90 s), so the janitor evicts the node and reclaims its work — correct so far.
3. The laptop wakes. Its old process-local session is rejected explicitly.
4. `node.py` automatically registers again and resumes polling with a new
   server-issued token; an arbitrary session-less poll cannot create a node.

Closing a laptop lid is the single most likely thing to happen to a volunteer
node, and on camera it shows as an empty swarm while work is running.
"""


import pytest
from fastapi.testclient import TestClient

import server_state as state
from server import app
from tests._node_session_helpers import age_node_record, enable_auto_node_sessions


@pytest.fixture
def client(monkeypatch):
    state.nodes.clear()
    state.task_queue.clear()
    state.task_inflight.clear()
    monkeypatch.setattr(state, "_LONG_POLL_TIMEOUT", 0.2)
    with TestClient(app) as c:
        yield enable_auto_node_sessions(c)
    state.nodes.clear()
    state.task_queue.clear()


def _register(client, node_id="laptop"):
    return client.post("/nodes/register", json={
        "node_id": node_id, "model": "qwen3.5:4b", "platform": "Windows",
        "machine": "AMD64", "hostname": node_id, "cpu_count": 8,
        "ram_gb": 8.0, "gpu": None, "capabilities": [],
    })


def _sleep_the_laptop(node_id="laptop"):
    """Simulate a lid closed for longer than the staleness threshold."""
    age_node_record(state.nodes[node_id], state._NODE_TIMEOUT + 30)
    state._cleanup_pass()


def test_eviction_itself_is_correct(client):
    """The janitor should drop a node that has genuinely gone away."""
    _register(client)
    assert client.get("/nodes").json()["count"] == 1
    _sleep_the_laptop()
    assert client.get("/nodes").json()["count"] == 0


def test_in_flight_work_is_reclaimed_when_a_node_disappears(client):
    """A closed lid must not swallow the task it was holding."""
    _register(client)
    state.task_inflight["t1"] = {"task_id": "t1", "prompt": "x", "assigned_to": "laptop"}
    _sleep_the_laptop()
    assert "t1" not in state.task_inflight
    assert any(t["task_id"] == "t1" for t in state.task_queue), "task was not re-queued"


def test_woken_node_is_told_to_register_again(client):
    """An evicted laptop cannot silently recreate itself by polling."""
    _register(client)
    _sleep_the_laptop()
    assert client.get("/nodes").json()["count"] == 0

    # The lid opens. The old session is invalid and the worker gets an explicit
    # machine-readable instruction to register again.
    resp = client.get("/tasks/next", params={"node_id": "laptop"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["action"] == "register_again"
    assert client.get("/nodes").json()["count"] == 0

    _register(client)
    count = client.get("/nodes").json()["count"]
    assert count == 1, (
        "a worker that follows the register_again response did not recover"
    )


def test_a_readmitted_node_can_receive_work(client):
    """Being listed is not enough — it has to actually get tasks again."""
    _register(client)
    _sleep_the_laptop()
    assert client.get("/tasks/next", params={"node_id": "laptop"}).status_code == 401
    _register(client)

    state.task_queue.append({"task_id": "t2", "prompt": "build something", "system": ""})
    resp = client.get("/tasks/next", params={"node_id": "laptop"})
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "t2"


def test_late_unbound_result_does_not_readmit_evicted_node(client):
    """Rejected output has no authority to mutate node liveness."""
    _register(client)
    state.task_inflight["t3"] = {"task_id": "t3", "prompt": "x", "assigned_to": "laptop"}
    _sleep_the_laptop()
    assert client.get("/nodes").json()["count"] == 0

    response = client.post("/tasks/t3/result", json={
        "node_id": "laptop", "output": "done", "elapsed_seconds": 1.0,
    })
    assert response.status_code == 401
    assert client.get("/nodes").json()["count"] == 0
