"""Results must be bound to the node the task was issued to.

The hole this closes, from the August external review: `node_secret` is
*network admission*, not per-node identity — every admitted node presents the
same credential. Result submission trusted the `node_id` in the request body and
located the task by `task_id` alone, so any admitted node could submit a result
attributed to a different node and take its credit and its completion count.

These tests are written from the attacker's side. Each one is a thing someone
holding the shared secret could actually do.

NOT tested here because it is NOT claimed: this does not stop a party who holds
`node_secret` from joining under a name of their choosing. Per-node keypairs
with signed receipts, revocation and rotation are the answer to that, and are
deferred to ROADMAP §5.
"""

import time

import pytest
from fastapi.testclient import TestClient

import server_state as state
from server import app


@pytest.fixture
def client():
    for store in (state.nodes, state.task_queue, state.task_inflight,
                  state.task_results, state.settled_attempts,
                  state.node_failure_count, state.node_blacklist):
        store.clear()
    with TestClient(app) as c:
        yield c


def _register(client, node_id):
    return client.post("/nodes/register", json={
        "node_id": node_id, "model": "m", "platform": "L", "machine": "x",
        "hostname": node_id, "cpu_count": 4, "ram_gb": 8.0, "gpu": None,
        "capabilities": [],
    })


def _claim(client, node_id, task_id="t-1"):
    """Queue a task and let `node_id` claim it, as a real node would."""
    state.task_queue.append({"task_id": task_id, "title": "t", "prompt": "p", "system": ""})
    return client.get("/tasks/next", params={"node_id": node_id}).json()


def _submit(client, task_id, node_id, task=None, **over):
    body = {"node_id": node_id, "output": "work", "error": None, "elapsed_seconds": 1.0}
    if task:
        body["attempt_id"] = task.get("attempt_id")
        body["nonce"] = task.get("nonce")
    body.update(over)
    return client.post(f"/tasks/{task_id}/result", json=body)


# ── The handout itself ───────────────────────────────────────────────

def test_assignment_issues_credentials_distinct_from_the_task_id(client):
    _register(client, "worker")
    task = _claim(client, "worker")
    assert task["attempt_id"] and task["nonce"]
    assert task["attempt_id"] != task["task_id"]
    assert task["nonce"] not in task["task_id"]
    assert len(task["nonce"]) >= 16, "a guessable nonce is not a nonce"


def test_honest_submission_is_paid(client):
    _register(client, "worker")
    task = _claim(client, "worker")
    r = _submit(client, "t-1", "worker", task)
    assert r.status_code == 200
    assert r.json()["credits_earned"] == 5


# ── Attacks ──────────────────────────────────────────────────────────

def test_another_node_cannot_claim_the_credit(client):
    """THE FINDING. `thief` holds the shared secret and submits for `worker`."""
    _register(client, "worker")
    _register(client, "thief")
    task = _claim(client, "worker")

    r = _submit(client, "t-1", "thief", task)   # correct nonce, wrong node

    assert r.status_code == 403, "a different node's submission was accepted"
    assert "not the assigned node" in r.json()["detail"]
    assert state.nodes["thief"].get("credits_earned", 0) == 0
    assert state.nodes["worker"].get("credits_earned", 0) == 0


def test_guessing_the_task_id_is_not_enough(client):
    """task_id appears in events and logs; it must not authorise anything."""
    _register(client, "worker")
    _register(client, "thief")
    _claim(client, "worker")

    r = _submit(client, "t-1", "thief")          # no credentials at all
    assert r.status_code == 403

    r = _submit(client, "t-1", "worker", None, nonce="guessed", attempt_id="guessed")
    assert r.status_code == 403
    assert "nonce" in r.json()["detail"]


def test_replaying_a_settled_attempt_pays_once(client):
    """A captured submission replayed later must not pay a second time."""
    _register(client, "worker")
    task = _claim(client, "worker")

    first = _submit(client, "t-1", "worker", task)
    replay = _submit(client, "t-1", "worker", task)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json(), "a retry should replay its outcome"
    assert state.nodes["worker"]["credits_earned"] == 5, "paid twice"


def test_expired_lease_is_rejected(client):
    """Work returned long after the task was reclaimed and redone elsewhere."""
    _register(client, "worker")
    task = _claim(client, "worker")
    state.task_inflight["t-1"]["lease_expires_at"] = time.time() - 1

    r = _submit(client, "t-1", "worker", task)
    assert r.status_code == 403
    assert "lease expired" in r.json()["detail"]
    assert state.nodes["worker"].get("credits_earned", 0) == 0


def test_rejections_are_logged_not_silent(client):
    """Silently dropping these would hide the behaviour worth knowing about."""
    _register(client, "worker")
    _register(client, "thief")
    task = _claim(client, "worker")
    before = len(state.pipeline_events)

    _submit(client, "t-1", "thief", task)

    new = state.pipeline_events[before:]
    assert any(e.get("type") == "result_rejected" for e in new), "rejection not logged"
    ev = next(e for e in new if e.get("type") == "result_rejected")
    assert ev["claimed_by"] == "thief" and ev["assigned_to"] == "worker"


def test_unverifiable_result_is_recorded_but_not_paid(client):
    """A node on an old build still gets its work kept — it just is not settled.

    Losing a finished deliverable would be worse than not paying for it.
    """
    _register(client, "worker")
    _claim(client, "worker")
    state.task_inflight["t-1"].pop("nonce", None)     # as if never issued one

    r = _submit(client, "t-1", "worker")
    assert r.status_code == 200
    assert r.json()["credits_earned"] == 0
    assert state.task_results["t-1"]["output"] == "work"


def test_ids_are_not_guessable_from_a_timestamp(client):
    """Predictable ids make every attack above easier to attempt."""
    import routes_pitch  # noqa: F401

    a, b = state.nodes, None  # keep the fixture's cleanup honest
    assert a is not b
    r1 = client.post("/pitch/async", json={"task": "one"})
    r2 = client.post("/pitch/async", json={"task": "two"})
    j1, j2 = r1.json()["job_id"], r2.json()["job_id"]
    assert j1 != j2
    for jid in (j1, j2):
        suffix = jid.split("_", 1)[1]
        assert len(suffix) >= 16, "job id is short enough to enumerate"
        assert not suffix.isdigit(), "job id is a timestamp, so it is predictable"
