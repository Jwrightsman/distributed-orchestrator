"""A node that outlives its own registry entry must be able to get back in.

The failure this pins, found Aug 13 while checking what happens when Jett shuts
his laptop:

1. `node.py` long-polls `/tasks/next`, which keeps `last_seen` fresh.
2. The laptop sleeps. Nothing reaches the server for more than `_NODE_TIMEOUT`
   (90 s), so the janitor evicts the node and reclaims its work — correct so far.
3. The laptop wakes. The long-poll resumes and **succeeds**, so `node.py` never
   sees a `ConnectError` and never re-registers.
4. `/tasks/next` only refreshes `last_seen` `if node_id in nodes` — and it is
   not. So the node polls forever, absent from `/nodes`, invisible on the
   dashboard, and receiving no work, because both pitch paths route to nodes
   only when the registry is non-empty.

Closing a laptop lid is the single most likely thing to happen to a volunteer
node, and on camera it shows as an empty swarm while work is running.
"""

import time

import pytest
from fastapi.testclient import TestClient

import server_state as state
from server import app


@pytest.fixture
def client(monkeypatch):
    state.nodes.clear()
    state.task_queue.clear()
    state.task_inflight.clear()
    monkeypatch.setattr(state, "_LONG_POLL_TIMEOUT", 0.2)
    with TestClient(app) as c:
        yield c
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
    state.nodes[node_id]["last_seen"] = time.time() - (state._NODE_TIMEOUT + 30)
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


def test_node_reappears_after_waking_up(client):
    """THE BUG. A woken laptop polls successfully but stays invisible."""
    _register(client)
    _sleep_the_laptop()
    assert client.get("/nodes").json()["count"] == 0

    # The lid opens. node.py's long poll resumes and succeeds — no error, so it
    # never re-registers on its own.
    resp = client.get("/tasks/next", params={"node_id": "laptop"})
    assert resp.status_code in (200, 204)

    count = client.get("/nodes").json()["count"]
    assert count == 1, (
        "a polling node is still missing from the registry, so the dashboard "
        "shows an empty swarm and neither pitch path will route work to it"
    )


def test_a_readmitted_node_can_receive_work(client):
    """Being listed is not enough — it has to actually get tasks again."""
    _register(client)
    _sleep_the_laptop()
    client.get("/tasks/next", params={"node_id": "laptop"})  # readmit

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
    assert response.status_code == 403
    assert client.get("/nodes").json()["count"] == 0
