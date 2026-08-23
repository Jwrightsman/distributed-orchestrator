"""Chaos tests — the failure modes that would show up on camera.

SPRINT_PHASE2 §2. Every scenario here is something a real volunteer network
does routinely: laptops sleep, wifi drops, models return garbage, two workers
race for the same task. The bar is not "handled gracefully" in the abstract —
it is that the orchestrator keeps serving, the work is not lost, and the
operator sees a clear message instead of a stack trace.

No Ollama and no network: the worker protocol is exercised through the real
FastAPI app in-process.
"""

import time

import pytest
from fastapi.testclient import TestClient

import routes_events
import routes_pitch
import server
import server_state
from tests._node_session_helpers import enable_auto_node_sessions


def _creds(task: dict) -> dict:
    """The attempt credentials a real node echoes back when submitting.

    Results are bound to the node the task was issued to, so a submission
    without these is recorded but never settled for credit.
    """
    task = task or {}
    return {"attempt_id": task.get("attempt_id"), "nonce": task.get("nonce")}


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
    ):
        d.clear()
    server.task_queue.clear()
    server.pipeline_events.clear()
    server_state._init_db()
    yield


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield enable_auto_node_sessions(c)


def register(client, node_id="node-a", model="qwen3.5:4b"):
    resp = client.post(
        "/nodes/register",
        json={
            "node_id": node_id,
            "model": model,
            "platform": "Linux",
            "machine": "x86_64",
            "hostname": node_id,
            "cpu_count": 4,
            "ram_gb": 8.0,
            "gpu": None,
            "capabilities": [],
        },
    )
    assert resp.status_code == 200
    return resp


def queue_task(task_id="t1", title="Build a thing"):
    server.task_queue.append(
        {
            "task_id": task_id,
            "title": title,
            "prompt": "do the thing",
            "system": "you are a builder",
            "trace_id": "trace-1",
            "job_id": "job-1",
            "subtask_id": 1,
            "requires": [],
        }
    )


# ── A node disappears mid-task ──────────────────────────────────────────────

def test_task_is_reclaimed_when_node_goes_silent(client):
    """Laptop closes mid-build. The task must go back in the queue, not vanish."""
    register(client, "ghost")
    queue_task("t-ghost")

    task = client.get("/tasks/next", params={"node_id": "ghost"}).json()
    assert task["task_id"] == "t-ghost"
    assert server.task_inflight["t-ghost"]["assigned_to"] == "ghost"
    assert not server.task_queue

    # The node stops sending heartbeats and ages past the staleness cutoff.
    server.nodes["ghost"]["last_seen"] = time.time() - (server_state._NODE_TIMEOUT + 10)
    server_state._cleanup_pass()

    assert "ghost" not in server.nodes
    assert "t-ghost" not in server.task_inflight
    assert [t["task_id"] for t in server.task_queue] == ["t-ghost"]
    # The reclaimed task must be assignable again — no stale assignment left on it
    assert "assigned_to" not in server.task_queue[0]


def test_reclaimed_task_can_be_picked_up_by_another_node(client):
    register(client, "dead")
    register(client, "alive")
    queue_task("t-move")

    client.get("/tasks/next", params={"node_id": "dead"})
    server.nodes["dead"]["last_seen"] = time.time() - (server_state._NODE_TIMEOUT + 10)
    server_state._cleanup_pass()

    task = client.get("/tasks/next", params={"node_id": "alive"}).json()
    assert task["task_id"] == "t-move"
    assert server.task_inflight["t-move"]["assigned_to"] == "alive"


def test_healthy_node_is_not_reclaimed(client):
    register(client, "steady")
    queue_task("t-keep")
    client.get("/tasks/next", params={"node_id": "steady"})

    server_state._cleanup_pass()

    assert "steady" in server.nodes
    assert "t-keep" in server.task_inflight


def test_a_node_streaming_tokens_is_not_evicted_mid_build(client):
    """A build longer than the staleness cutoff must not lose its own node.

    Found in a dress rehearsal, not by this suite: only /tasks/next and
    /tasks/{id}/result refreshed last_seen, so a node went "silent" for the
    whole of a long build even though it was posting a token batch every
    0.3 s. The janitor evicted it, reclaimed the subtask it was building, and
    put it back in a queue with no nodes left in it. The dashboard showed
    0 nodes and the run stalled, while the node's terminal showed it working.
    """
    register(client, "builder")
    queue_task("t-long")
    task = client.get("/tasks/next", params={"node_id": "builder"}).json()

    # A build that outlives the cutoff: age the node past it, exactly as a
    # slow subtask does when nothing else refreshes last_seen.
    server.nodes["builder"]["last_seen"] = time.time() - (server_state._NODE_TIMEOUT + 10)

    # …but the node is plainly alive: it is streaming tokens for that task.
    resp = client.post("/tasks/t-long/stream",
                       json={"node_id": "builder", "tokens": "def dedupe(", **_creds(task)})
    assert resp.status_code == 200

    server_state._cleanup_pass()

    assert "builder" in server.nodes, "a node streaming tokens was evicted"
    assert "t-long" in server.task_inflight, "its in-flight task was reclaimed under it"
    assert not server.task_queue


def test_streaming_for_an_unknown_task_does_not_refresh_a_node(client):
    """Only the holder of an active lease may refresh via task streaming."""
    register(client, "chatty")
    server.nodes["chatty"]["last_seen"] = time.time() - (server_state._NODE_TIMEOUT + 10)

    client.post("/tasks/does-not-exist/stream",
                json={"node_id": "chatty", "tokens": "x"})
    server_state._cleanup_pass()

    assert "chatty" not in server.nodes


# ── A node returns malformed, empty, or refusing output ─────────────────────

@pytest.mark.parametrize(
    "payload",
    [
        {"output": "", "error": None},
        {"output": None, "error": "model crashed"},
        {"output": "", "error": "timeout"},
    ],
)
def test_bad_results_are_accepted_and_counted_as_failures(client, payload):
    """A bad result must be recorded, not rejected — the caller needs to see it."""
    register(client, "flaky")
    queue_task("t-bad")
    task = client.get("/tasks/next", params={"node_id": "flaky"}).json()

    resp = client.post(
        "/tasks/t-bad/result",
        json={"node_id": "flaky", "elapsed_seconds": 1.0, **payload, **_creds(task)},
    )

    assert resp.status_code == 200
    assert resp.json()["credits_earned"] == 0
    assert server.node_failure_count["flaky"] == 1
    assert "t-bad" in server.task_results


def test_refusal_text_still_counts_as_a_completed_task(client):
    """A model that says "I can't help with that" returns output, not an error.

    It must not be silently treated as a failure at this layer — the pipeline's
    refusal detection handles the content. What matters here is that the
    orchestrator does not lose the result.
    """
    register(client, "refuser")
    queue_task("t-refuse")
    task = client.get("/tasks/next", params={"node_id": "refuser"}).json()

    resp = client.post(
        "/tasks/t-refuse/result",
        json={
            "node_id": "refuser",
            **_creds(task),
            "output": "I'm sorry, but I cannot assist with that request.",
            "error": None,
            "elapsed_seconds": 2.0,
        },
    )

    assert resp.status_code == 200
    assert server.task_results["t-refuse"]["output"].startswith("I'm sorry")


def test_result_for_unknown_task_is_quarantined_not_published(client):
    """A stale task id is rejected without entering operational execution."""
    register(client, "zombie")

    resp = client.post(
        "/tasks/does-not-exist/result",
        json={"node_id": "zombie", "output": "late work", "error": None, "elapsed_seconds": 1.0},
    )

    assert resp.status_code == 403
    assert "does-not-exist" not in server.task_results
    assert server_state.attempt_store.quarantine_count() == 1


def test_stream_from_stale_task_is_rejected_not_fatal(client):
    register(client, "streamer")

    resp = client.post(
        "/tasks/gone/stream",
        json={"node_id": "streamer", "tokens": "hello"},
    )

    assert resp.status_code == 403
    assert "no active server-issued attempt" in resp.json()["detail"]


# ── Circuit breaker opens and recovers ──────────────────────────────────────

def test_circuit_breaker_opens_after_repeated_failures(client):
    register(client, "broken")

    for i in range(server_state._FAILURE_THRESHOLD):
        queue_task(f"t-fail-{i}")
        task = client.get("/tasks/next", params={"node_id": "broken"}).json()
        client.post(
            f"/tasks/t-fail-{i}/result",
            json={"node_id": "broken", "output": "", "error": "boom",
                  "elapsed_seconds": 0.1, **_creds(task)},
        )

    assert "broken" in server.node_blacklist

    resp = client.get("/tasks/next", params={"node_id": "broken"})
    assert resp.status_code == 429
    assert resp.json()["error"] == "circuit_open"
    assert resp.json()["retry_after"] > 0


def test_circuit_breaker_recovers_after_the_blacklist_expires(client):
    register(client, "recovering")
    queue_task("t-later")

    server.node_blacklist["recovering"] = time.time() - 1  # expired a second ago
    server.node_failure_count["recovering"] = server_state._FAILURE_THRESHOLD

    task = client.get("/tasks/next", params={"node_id": "recovering"}).json()

    assert task["task_id"] == "t-later"
    assert "recovering" not in server.node_blacklist
    assert server.node_failure_count["recovering"] == 0


def test_one_success_clears_the_failure_streak(client):
    register(client, "wobbly")
    server.node_failure_count["wobbly"] = server_state._FAILURE_THRESHOLD - 1

    queue_task("t-good")
    task = client.get("/tasks/next", params={"node_id": "wobbly"}).json()
    client.post(
        "/tasks/t-good/result",
        json={"node_id": "wobbly", "output": "a real answer", "error": None,
              "elapsed_seconds": 3.0, **_creds(task)},
    )

    assert server.node_failure_count["wobbly"] == 0
    assert "wobbly" not in server.node_blacklist


def test_blacklisting_one_node_does_not_affect_others(client):
    register(client, "bad")
    register(client, "good")
    server.node_blacklist["bad"] = time.time() + 60
    queue_task("t-shared")

    assert client.get("/tasks/next", params={"node_id": "bad"}).status_code == 429
    assert client.get("/tasks/next", params={"node_id": "good"}).json()["task_id"] == "t-shared"


# ── Two nodes racing for the same task ──────────────────────────────────────

def test_a_task_is_handed_to_exactly_one_node(client, monkeypatch):
    """Two workers polling at once must not both get the same task."""
    # The loser long-polls for the full timeout before giving up; shorten it so
    # the suite does not sit for 25 seconds proving it.
    monkeypatch.setattr(server_state, "_LONG_POLL_TIMEOUT", 0.5)
    register(client, "racer-1")
    register(client, "racer-2")
    queue_task("t-solo")

    first = client.get("/tasks/next", params={"node_id": "racer-1"}).json()
    second = client.get("/tasks/next", params={"node_id": "racer-2"})

    assert first["task_id"] == "t-solo"
    # Nothing left to hand out — the second poll long-polls and returns empty
    assert second.status_code == 204
    assert len(server.task_inflight) == 1


def test_duplicate_result_submissions_do_not_double_pay(client):
    """A node that retries its submission must not earn credits twice."""
    register(client, "double")
    queue_task("t-dup")
    task = client.get("/tasks/next", params={"node_id": "double"}).json()

    body = {"node_id": "double", "output": "work", "error": None,
            "elapsed_seconds": 1.0, **_creds(task)}
    first = client.post("/tasks/t-dup/result", json=body)
    second = client.post("/tasks/t-dup/result", json=body)

    assert first.json()["credits_earned"] == 5
    # Settlement is idempotent: the retry replays the ORIGINAL outcome rather
    # than reporting zero, because a node that retried after a dropped
    # connection did earn those credits and should be told so. What must not
    # happen is a second payment.
    assert second.json() == first.json()
    assert server.nodes["double"]["credits_earned"] == 5


# ── The orchestrator survives its dependencies ──────────────────────────────

def test_health_reports_ollama_down_without_crashing(client, monkeypatch):
    """Ollama restarting mid-pipeline must not take /health down.

    Points the health check at a port nothing listens on, rather than assuming
    the developer's machine has Ollama stopped — that assumption made this test
    pass in CI and fail on the one machine that actually runs the pipeline.
    """
    monkeypatch.setattr(routes_events, "OLLAMA_URL", "http://127.0.0.1:9")  # discard port

    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ollama"] == "unavailable"
    assert body["status"] == "degraded"
    # Node bookkeeping still answers while the model backend is gone
    assert "nodes_online" in body and "tasks_pending" in body


def test_pipeline_failure_returns_a_message_not_a_stack_trace(client, monkeypatch):
    """A planner blow-up must reach the user as an explanation."""
    async def exploding_pipeline(*a, **k):
        raise ValueError("Planner failed after 3 attempts: Subtask 1 is missing a title")

    monkeypatch.setattr(routes_pitch, "run_pipeline", exploding_pipeline)

    resp = client.post("/pitch", json={"task": "build something"})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "Planner failed" in detail
    assert "Traceback" not in detail


def test_unexpected_error_is_json_not_a_traceback(monkeypatch):
    """Anything unhandled still leaves the API contract intact."""
    async def exploding_pipeline(*a, **k):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(routes_pitch, "run_pipeline", exploding_pipeline)

    # Let the app's own handler answer instead of re-raising into the test.
    with TestClient(server.app, raise_server_exceptions=False) as c:
        resp = c.post("/pitch", json={"task": "build something"})

    assert resp.status_code == 500
    assert "Traceback" not in resp.text
    # This assertion used to read resp.json()["error"] == "Internal server
    # error", which was the response shape of a handler that also echoed
    # str(exc). Pinning that shape is why a real leak survived: the test was
    # asserting the contract of the wrong handler. Assert the message is gone.
    assert "something nobody predicted" not in resp.text
    assert resp.json() == {"detail": "internal server error"}


def test_async_job_records_failure_instead_of_hanging(client, monkeypatch):
    """A job whose pipeline dies must end up 'failed', never stuck 'running'."""
    async def exploding_pipeline(*a, **k):
        raise RuntimeError("node fell over")

    monkeypatch.setattr(routes_pitch, "run_pipeline", exploding_pipeline)

    job_id = client.post("/pitch/async", json={"task": "build something"}).json()["job_id"]

    deadline = time.time() + 10
    while time.time() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] in ("complete", "failed"):
            break
        time.sleep(0.1)

    assert job["status"] == "failed"
    assert "node fell over" in (job["error"] or "")


# ── Queue and state hygiene under churn ─────────────────────────────────────

def test_cleanup_pass_is_safe_to_run_repeatedly_on_empty_state():
    for _ in range(5):
        server_state._cleanup_pass()

    assert server.nodes == {}
    assert server.task_queue == []


def test_old_task_results_are_pruned(client):
    server.task_results["ancient"] = {
        "task_id": "ancient",
        "node_id": "n",
        "output": "x",
        "completed_at": time.time() - (server_state._RESULT_TTL + 60),
    }
    server.task_results["fresh"] = {
        "task_id": "fresh",
        "node_id": "n",
        "output": "x",
        "completed_at": time.time(),
    }

    server_state._cleanup_pass()

    assert "ancient" not in server.task_results
    assert "fresh" in server.task_results


def test_many_nodes_churning_does_not_lose_queued_work(client):
    """20 nodes register, take work, and half of them vanish."""
    for i in range(20):
        register(client, f"churn-{i}")
    for i in range(20):
        queue_task(f"t-churn-{i}")

    for i in range(20):
        client.get("/tasks/next", params={"node_id": f"churn-{i}"})
    assert len(server.task_inflight) == 20

    for i in range(0, 20, 2):
        server.nodes[f"churn-{i}"]["last_seen"] = time.time() - (server_state._NODE_TIMEOUT + 10)
    server_state._cleanup_pass()

    # Nothing lost: every task is either still in flight or back in the queue.
    assert len(server.task_inflight) + len(server.task_queue) == 20
    assert len(server.task_queue) == 10
