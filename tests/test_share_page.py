"""/s/{token}, the share card, and the Try page that links to it.

What is asserted is what a later edit could quietly undo:

* the card is reachable by its token alone, on a server with a viewer key;
* it shows nothing the redacted JSON view does not: no private node id, no
  project id, no server path, whatever the execution record carries;
* a bad, expired or revoked token all answer the same 404;
* the token never reaches a log line, in either of its two path forms;
* the Try page states the public endpoint's real profile, checked against the
  literals public_pitch sets, and reads progress from the public share link
  rather than from routes a visitor cannot open.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import access_control
import routes_access
import routes_share_page
import routes_try
from access_control import ViewerAccessMiddleware, is_public_or_separately_authenticated
from execution.artifacts import ArtifactStore
from execution.sharing import CreateExecutionShareV1, ShareStore
from tests.test_shares import EXECUTION_ID, _execution

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def share_page(tmp_path, monkeypatch):
    cfg = {"viewer_key": "viewer-secret", "viewer_session_ttl_seconds": 3600, "viewer_cookie_secure": False}
    monkeypatch.setattr(access_control, "get_config", lambda: cfg)
    monkeypatch.setattr(routes_access, "get_config", lambda: cfg)
    shares = ShareStore(str(tmp_path / "api.db"))
    storage = tmp_path / "storage"
    root = storage / EXECUTION_ID
    root.mkdir(parents=True)
    (root / "safe.txt").write_bytes(b"safe")
    artifacts = ArtifactStore(tmp_path / "api.db", allowed_roots=[storage])
    artifacts.register_root(EXECUTION_ID, root)
    record = _execution()
    record.update({"lifecycle_status": "completed", "validation_outcome": "passed",
                   "assurance_level": "structural"})

    class FakeService:
        def get(self, execution_id):
            return record if execution_id == EXECUTION_ID else None

    monkeypatch.setattr(routes_access, "get_share_store", lambda: shares)
    monkeypatch.setattr(routes_access, "get_artifact_store", lambda: artifacts)
    monkeypatch.setattr(routes_access, "get_execution_service", lambda: FakeService())
    app = FastAPI()
    app.add_middleware(ViewerAccessMiddleware)
    app.include_router(routes_access.router)
    app.include_router(routes_share_page.router)
    return TestClient(app), shares


def test_the_card_opens_by_token_alone_and_shows_only_the_redacted_view(share_page):
    client, shares = share_page
    token = shares.create(EXECUTION_ID, CreateExecutionShareV1(
        allow_artifact_download=True, redact_node_identity=True)).token
    page = client.get(f"/s/{token}")
    assert page.status_code == 200, page.text[:200]
    body = page.text
    assert "Build the deliberately shared result" in body
    assert "COMPLETED · CHECKS PASSED" in body
    assert "Structure checked" in body
    for private in ("private-node-id", "private-project-id", "C:/server/private", "job_private",
                    "private-hostname", "private-attempt"):
        assert private not in body, private
    assert "not shown on this link" in body
    for header, value in (("cache-control", "no-store"), ("referrer-policy", "no-referrer")):
        assert page.headers[header] == value


def test_every_dead_token_answers_the_same_404(share_page):
    client, shares = share_page
    expired = shares.create(EXECUTION_ID, CreateExecutionShareV1(expires_in_seconds=60),
                            now=datetime.now(timezone.utc) - timedelta(minutes=2))
    revoked = shares.create(EXECUTION_ID, CreateExecutionShareV1())
    assert shares.revoke(EXECUTION_ID, revoked.share_id)
    answers = [client.get(f"/s/{t}") for t in ("not-a-token", expired.token, revoked.token)]
    assert {a.status_code for a in answers} == {404}
    assert len({a.text for a in answers}) == 1, "a dead token's kind is distinguishable"


def test_the_card_is_public_and_its_token_never_logged():
    assert is_public_or_separately_authenticated("GET", "/s/anything")
    assert not is_public_or_separately_authenticated("POST", "/s/anything")
    token = "top-secret-capability-token"
    assert token not in routes_access.redact_share_token_path(f"/s/{token}")
    assert routes_access.redact_share_token_path("/status/s/x") == "/status/s/x"


def test_the_try_profile_is_the_one_public_pitch_sets():
    src = (ROOT / "routes_pitch.py").read_text(encoding="utf-8")
    body = src[src.index("async def public_pitch("):]
    body = body[: body.index("\n@router", 1) if "\n@router" in body[1:] else len(body)]
    words = dict((k, v) for v, k in routes_try.PUBLIC_PROFILE)
    assert 'strategy="direct"' in body and "candidates=1" in body and words["NO SPLITTING"] == "1 attempt"
    assert 'placement="local"' in body and words["NEVER SENT OUT"] == "this machine"
    assert "timeout_seconds=120" in body and words["HARD DEADLINE"] == "120s"
    assert "max_output_bytes=65_536" in body and words["OUTPUT CAP"] == "64 KB"
    assert "expires_in_seconds=3600" in body and "redact_node_identity=True" in body


def test_the_try_page_reads_only_what_a_visitor_may_open():
    html = (ROOT / "templates" / "try.html").read_text(encoding="utf-8")
    script = html[html.index("<script>"):]
    assert not re.search(r"fetch\(`/(jobs|history)/", script), "the page polls a private route"
    assert "fetch(shareUrl)" in script
    for phrase in ("planner splits", "Planning", "volunteer"):
        assert phrase not in html, phrase
