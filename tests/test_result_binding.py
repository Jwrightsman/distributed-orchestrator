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
from execution.dispatch import Dispatcher


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


def _claim_v1(client, node_id, task_id="t-v1"):
    """Claim a canonical execution unit carrying every v1 binding field."""
    state.task_queue.append({
        "task_id": task_id,
        "title": "candidate",
        "prompt": "complete task",
        "system": "system",
        "contract_version": "1",
        "execution_id": "e" * 32,
        "strategy": "ensemble",
        "execution_unit_id": "candidate-1",
        "execution_unit_kind": "candidate",
    })
    return client.get("/tasks/next", params={"node_id": node_id}).json()


def _v1_body(task, node_id="worker", **over):
    body = {
        "node_id": node_id,
        "output": "complete work",
        "error": None,
        "elapsed_seconds": 1.0,
        "contract_version": "1",
        "attempt_id": task.get("attempt_id"),
        "nonce": task.get("nonce"),
        "execution_id": task.get("execution_id"),
        "execution_unit_id": task.get("execution_unit_id"),
        "execution_unit_kind": task.get("execution_unit_kind"),
    }
    body.update(over)
    return body


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
    assert "attempt" in r.json()["detail"]


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
    state.attempt_store.expire_due(task["lease_expires_at"] + 1)

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


def test_unverifiable_legacy_result_is_quarantined_not_published(client):
    """Old-node output is diagnostic only; it cannot satisfy a dispatcher."""
    _register(client, "worker")
    _claim(client, "worker")
    state.task_inflight["t-1"].pop("nonce", None)     # as if never issued one

    r = _submit(client, "t-1", "worker")
    assert r.status_code == 403
    assert "t-1" not in state.task_results
    assert "t-1" in state.task_inflight
    assert state.attempt_store.quarantine_count() == 1


def test_v1_result_requires_attempt_identifier(client):
    _register(client, "worker")
    task = _claim_v1(client, "worker")
    body = _v1_body(task, attempt_id=None)
    response = client.post("/tasks/t-v1/result", json=body)
    assert response.status_code == 403
    assert "missing attempt id" in response.json()["detail"]


def test_v1_worker_cannot_downgrade_by_omitting_contract_and_bindings(client):
    """Strictness comes from the server-issued attempt, never the submission."""
    _register(client, "worker")
    task = _claim_v1(client, "worker")

    downgraded = {
        "node_id": "worker",
        "output": "plausible but unbound work",
        "error": None,
        "elapsed_seconds": 1.0,
    }
    rejected = client.post("/tasks/t-v1/result", json=downgraded)

    assert rejected.status_code == 403
    assert "missing contract version" in rejected.json()["detail"]
    assert "t-v1" in state.task_inflight, "rejection must not settle the attempt"
    assert "t-v1" not in state.task_results, "unbound output entered the operational channel"
    assert state.nodes["worker"].get("credits_earned", 0) == 0

    legitimate = client.post("/tasks/t-v1/result", json=_v1_body(task))
    assert legitimate.status_code == 200
    assert legitimate.json() == {"status": "accepted", "credits_earned": 5}


def test_attacker_claiming_assigned_node_without_nonce_cannot_settle(client):
    _register(client, "worker")
    task = _claim_v1(client, "worker")
    attacker = _v1_body(task, node_id="worker", nonce=None)

    rejected = client.post("/tasks/t-v1/result", json=attacker)

    assert rejected.status_code == 403
    assert "missing attempt nonce" in rejected.json()["detail"]
    assert "t-v1" in state.task_inflight
    assert "t-v1" not in state.task_results


def test_queued_but_unleased_result_is_quarantined(client):
    _register(client, "worker")
    queued = {
        "task_id": "t-queued",
        "title": "candidate",
        "prompt": "complete task",
        "system": "system",
        "contract_version": "1",
        "execution_id": "e" * 32,
        "execution_unit_id": "candidate-1",
        "execution_unit_kind": "candidate",
    }
    state.task_queue.append(queued)

    response = client.post("/tasks/t-queued/result", json={
        "node_id": "worker",
        "output": "plausible queued-task output",
        "elapsed_seconds": 1,
    })

    assert response.status_code == 403
    assert state.task_queue == [queued]
    assert "t-queued" not in state.task_results
    assert state.attempt_store.quarantine_count() == 1


def test_late_result_after_reclamation_is_rejected(client):
    _register(client, "worker")
    task = _claim_v1(client, "worker")
    state.nodes["worker"]["last_seen"] = time.time() - state._NODE_TIMEOUT - 1
    state._cleanup_pass()

    response = client.post("/tasks/t-v1/result", json=_v1_body(task))

    assert response.status_code == 403
    assert "reclaimed" in response.json()["detail"]
    assert "t-v1" not in state.task_results
    assert any(item["task_id"] == "t-v1" for item in state.task_queue)


def test_late_result_after_cancellation_is_rejected(client):
    _register(client, "worker")
    task = _claim_v1(client, "worker")
    assert Dispatcher._cancel("t-v1", reason="test cancellation")

    response = client.post("/tasks/t-v1/result", json=_v1_body(task))

    assert response.status_code == 403
    assert "cancelled" in response.json()["detail"]
    assert "t-v1" not in state.task_results


def test_changed_replay_payload_is_rejected(client):
    _register(client, "worker")
    task = _claim_v1(client, "worker")
    accepted = client.post("/tasks/t-v1/result", json=_v1_body(task))
    changed = client.post(
        "/tasks/t-v1/result",
        json=_v1_body(task, output="different replay output"),
    )

    assert accepted.status_code == 200
    assert changed.status_code == 403
    assert "replay payload does not match" in changed.json()["detail"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("contract_version", "0", "contract version"),
        ("nonce", "wrong", "nonce"),
        ("execution_id", "wrong", "execution id"),
        ("execution_unit_id", "wrong", "execution unit id"),
        ("execution_unit_kind", "wrong", "execution unit kind"),
    ],
)
def test_v1_result_must_match_execution_unit_binding(client, field, value, message):
    _register(client, "worker")
    task = _claim_v1(client, "worker")
    response = client.post("/tasks/t-v1/result", json=_v1_body(task, **{field: value}))
    assert response.status_code == 403
    assert message in response.json()["detail"]


def test_v1_honest_result_is_idempotent_and_paid_once(client):
    _register(client, "worker")
    task = _claim_v1(client, "worker")
    body = _v1_body(task)
    first = client.post("/tasks/t-v1/result", json=body)
    replay = client.post("/tasks/t-v1/result", json=body)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() == {"status": "accepted", "credits_earned": 5}
    assert state.nodes["worker"]["credits_earned"] == 5


def test_v1_stream_requires_current_node_attempt_nonce_and_unit(client):
    _register(client, "worker")
    _register(client, "thief")
    task = _claim_v1(client, "worker")
    base = {
        "node_id": "worker",
        "tokens": "partial",
        "contract_version": "1",
        "attempt_id": task["attempt_id"],
        "nonce": task["nonce"],
        "execution_id": task["execution_id"],
        "execution_unit_id": task["execution_unit_id"],
        "execution_unit_kind": task["execution_unit_kind"],
    }

    assert client.post("/tasks/t-v1/stream", json=base).json() == {"ok": True}
    for field, value in (
        ("node_id", "thief"),
        ("attempt_id", "wrong"),
        ("nonce", "wrong"),
        ("execution_id", "wrong"),
        ("execution_unit_id", "wrong"),
        ("execution_unit_kind", "wrong"),
    ):
        response = client.post("/tasks/t-v1/stream", json={**base, field: value})
        assert response.status_code == 403


def test_v1_stream_rejects_expired_lease(client):
    _register(client, "worker")
    task = _claim_v1(client, "worker")
    state.task_inflight["t-v1"]["lease_expires_at"] = time.time() - 1
    response = client.post("/tasks/t-v1/stream", json={
        "node_id": "worker",
        "tokens": "late",
        "contract_version": "1",
        "attempt_id": task["attempt_id"],
        "nonce": task["nonce"],
        "execution_id": task["execution_id"],
        "execution_unit_id": task["execution_unit_id"],
        "execution_unit_kind": task["execution_unit_kind"],
    })
    assert response.status_code == 403
    assert "lease expired" in response.json()["detail"]


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
