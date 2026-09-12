"""The replay line's source: `GET /v1/operator/executions/{id}/submission`.

Delta §8.11 recorded this as the console port's one remaining *extend or
drop*, and named the way it goes wrong: reading the fact off a plausible
log key renders a line that is always absent, looks like it works, and starts
lying the moment someone writes that key for another reason.

So the fact is durable and the route serves the durable fact. Three things are
asserted here that prose cannot establish:

1. **Absent and zero are different answers.** A run pitched without an
   idempotency key has no mapping row; `replay_count: 0` would say it was
   submitted under a key and never replayed.
2. **The gate matches the placement.** The route is under `/v1/operator/`,
   refused whole at the public edge, which is what lets the panel render in the
   console and nowhere else.
3. **No digest is served.** The mapping's keys are irreversible but
   correlatable, so none of them may leave the table.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import config
from execution.contracts import ExecutionRequestV1
from execution.idempotency import submission_identity
from execution.service import get_execution_service
from server import app

KEY = "replay-route-key"
SCOPE = "replay-route-requester"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _identity(request: ExecutionRequestV1):
    return submission_identity(
        request,
        idempotency_key=KEY,
        requester_scope_kind="pitch-key",
        requester_scope_value=SCOPE,
    )


def _keyed(times: int = 1) -> str:
    """Commit one keyed submission and replay it `times - 1` times."""

    service = get_execution_service()
    request = ExecutionRequestV1(task="Replay route fixture", strategy="direct")
    identity = _identity(request)
    execution_id = ""
    for _ in range(times):
        record = service.store.create_or_replay_submission(
            request,
            identity,
            lambda: service._new_result(request, uuid.uuid4().hex, None, "queued"),
        )
        execution_id = record.result.execution_id
    return execution_id


def _unkeyed() -> str:
    service = get_execution_service()
    request = ExecutionRequestV1(task="Unkeyed route fixture", strategy="direct")
    queued = service._new_result(request, uuid.uuid4().hex, None, "queued")
    service.store.create(request, queued)
    return queued.execution_id


def _url(execution_id: str) -> str:
    return f"/v1/operator/executions/{execution_id}/submission"


def test_the_route_serves_the_count_and_the_moment(client):
    execution_id = _keyed(times=4)
    response = client.get(_url(execution_id))

    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["execution_id"] == execution_id
    assert body["replay_count"] == 3, "three later pitches were answered by return"
    assert body["last_replayed_at"], "a counted replay has a recorded moment"
    assert body["submitted_at"]


def test_a_submission_never_replayed_serves_a_zero_it_can_stand_behind(client):
    """Zero here is a recorded fact, not a default standing in for silence.

    This execution *was* submitted under a key. Nothing has replayed it, and
    the row that says so is the same row that would have counted a replay.
    """
    execution_id = _keyed(times=1)
    body = client.get(_url(execution_id)).json()

    assert body["replay_count"] == 0
    assert body["last_replayed_at"] is None


def test_an_execution_with_no_keyed_submission_is_a_404_not_a_zero(client):
    """The distinction the provenance envelope's 404 already draws (§8.7).

    A run pitched without an idempotency key has no mapping row at all.
    Answering `replay_count: 0` would state that it was submitted under a key
    and never replayed -- a different and false statement about a different
    thing.
    """
    unkeyed = _unkeyed()
    response = client.get(_url(unkeyed))

    assert response.status_code == 404, response.text[:300]
    assert response.json()["detail"]["code"] == "keyed_submission_not_found"

    missing = client.get(_url("exec_does_not_exist"))
    assert missing.status_code == 404


def test_no_digest_is_served(client):
    """Irreversible is not the same as safe to publish.

    Two executions sharing a `requester_scope_hash` came from one requester,
    and an `idempotency_key_hash` over a low-entropy key is guessable, so the
    served object carries neither -- nor the request digest.
    """
    execution_id = _keyed(times=2)
    raw = client.get(_url(execution_id)).text
    body = client.get(_url(execution_id)).json()

    assert set(body) == {
        "execution_id",
        "submitted_at",
        "replay_count",
        "last_replayed_at",
    }
    request = ExecutionRequestV1(task="Replay route fixture", strategy="direct")
    identity = _identity(request)
    for digest in (
        identity.requester_scope_hash,
        identity.idempotency_key_hash,
        identity.request_hash,
    ):
        assert digest not in raw, "a digest reached the response body"
    assert KEY not in raw and SCOPE not in raw, "the key itself reached the body"


def test_the_route_is_operator_gated(client, monkeypatch):
    execution_id = _keyed(times=2)
    monkeypatch.setitem(config.get(), "viewer_key", "k" * 32)

    refused = client.get(_url(execution_id))
    assert refused.status_code == 401, refused.text[:200]
    assert refused.headers.get("WWW-Authenticate") == "Bearer"

    allowed = client.get(_url(execution_id), headers={"X-Viewer-Key": "k" * 32})
    assert allowed.status_code == 200


def test_the_handler_refuses_on_its_own_and_not_only_via_middleware(monkeypatch):
    """The same gap poisoning found on the chain route.

    Removing `require_viewer` from that handler left the suite green, because
    the middleware refuses the request first. The docstring claims the handler
    refuses too, so the handler is called directly with nothing in front of it.
    """
    from routes_access import execution_submission

    monkeypatch.setitem(config.get(), "viewer_key", "k" * 32)

    class _Uncredentialed:
        headers: dict[str, str] = {}
        cookies: dict[str, str] = {}

    with pytest.raises(HTTPException) as raised:
        asyncio.run(execution_submission(_Uncredentialed(), "exec_whatever"))
    assert raised.value.status_code == 401
    assert raised.value.headers.get("WWW-Authenticate") == "Bearer"


def test_the_operator_prefix_that_places_the_panel_is_refused_at_the_edge():
    """Read off the file that makes the claim, not asserted in prose.

    This route's gate is the reason the replay line renders in the console and
    never on `/run/{id}`. If the prefix stops being refused there, the
    placement argument stops holding and the panel should be reconsidered with
    it -- which is why this fails loudly rather than drifting.
    """
    caddyfile = (
        Path(__file__).resolve().parent.parent / "deploy" / "Caddyfile.public"
    ).read_text(encoding="utf-8")
    assert "/v1/operator/*" in caddyfile, (
        "the operator prefix is no longer refused at the public edge, so the "
        "replay count is reachable with a viewer key alone and the panel's "
        "console-only placement no longer follows from its gate"
    )


def test_the_shareable_run_page_does_not_reach_for_the_replay_count():
    """Placement, enforced where it is decided.

    `routes_run.py` renders `/run/{id}`, the page that travels with a link. It
    passes the envelope and deliberately passes neither the ledger chain nor
    this, because who re-pitched a task is not a fact about the deliverable
    somebody was handed.
    """
    root = Path(__file__).resolve().parent.parent
    server_page = (root / "routes_run.py").read_text(encoding="utf-8")
    assert "submission_replays" not in server_page, (
        "the shareable page now reads the replay count, which is operator-gated"
    )
    assert "submission=" not in server_page, (
        "the shareable page now passes a submission record to the renderer"
    )
