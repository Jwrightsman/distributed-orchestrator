"""Explicit shares are unguessable, revocable, scoped, and deliberately redacted."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import access_control
import routes_access
from access_control import ViewerAccessMiddleware
from execution.artifacts import ArtifactEntryV1, ArtifactManifestV1, ArtifactStore
from execution.sharing import (
    CreateExecutionShareV1,
    ShareStore,
    artifact_manifest_for_share,
    redact_execution_for_share,
)

EXECUTION_ID = "e" * 32
OTHER_EXECUTION_ID = "o" * 32


def _execution(execution_id=EXECUTION_ID):
    return {
        "execution_id": execution_id,
        "job_id": "job_private",
        "status": "completed",
        "task": "Build the deliberately shared result",
        "project_id": "private-project-id",
        "strategy_selected": "ensemble",
        "placement_selected": "distributed",
        "output_reference": "C:/server/private/execution_artifacts/result.md",
        "output_preview": "The explicitly shared output",
        "winning_candidate": "candidate-1",
        "produced_files": ["C:/server/private/execution_artifacts/index.html"],
        "participating_nodes": ["private-node-id"],
        "credit_records": [{"contributor": "private-hostname"}],
        "telemetry": {"attempt_id": "private-attempt"},
        "validation_evidence": [
            {
                "validator_name": "code_parse",
                "status": "passed",
                "score": 1.0,
                "evidence": {"server_path": "C:/server/private/file"},
            }
        ],
        "candidates": [
            {
                "candidate_id": "candidate-1",
                "status": "selected",
                "output_bytes": 20,
                "output_preview": "candidate output",
                "produced_files": ["C:/server/private/candidate/index.html"],
                "node_id": "private-node-id",
                "validation": [
                    {"validator_name": "code_parse", "status": "passed", "score": 1.0}
                ],
            }
        ],
    }


def _manifest():
    return ArtifactManifestV1(
        execution_id=EXECUTION_ID,
        created_at="2026-08-21T12:00:00+00:00",
        file_count=1,
        aggregate_size_bytes=4,
        entries=[
            ArtifactEntryV1(
                relative_path="candidate_1/code/index.html",
                media_type="text/html",
                size_bytes=4,
                sha256=hashlib.sha256(b"safe").hexdigest(),
                source_candidate_id="candidate-1",
                created_at="2026-08-21T12:00:00+00:00",
            )
        ],
    )


def test_store_keeps_only_token_hash_and_reopens(tmp_path):
    db = tmp_path / "shares.db"
    created = ShareStore(str(db)).create(EXECUTION_ID, CreateExecutionShareV1())

    with sqlite3.connect(db) as con:
        stored = con.execute("SELECT token_hash FROM execution_shares").fetchone()[0]
        all_text = " ".join(str(value) for row in con.execute("SELECT * FROM execution_shares") for value in row)
    assert stored == hashlib.sha256(created.token.encode()).hexdigest()
    assert created.token not in all_text
    reopened = ShareStore(str(db)).get_active(created.token)
    assert reopened is not None
    assert reopened.execution_id == EXECUTION_ID


def test_expiry_and_revocation_are_enforced(tmp_path):
    store = ShareStore(str(tmp_path / "shares.db"))
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    created = store.create(
        EXECUTION_ID,
        CreateExecutionShareV1(expires_in_seconds=60),
        now=start,
    )
    assert store.get_active(created.token, now=start + timedelta(seconds=59)) is not None
    assert store.get_active(created.token, now=start + timedelta(seconds=60)) is None

    revocable = store.create(EXECUTION_ID, CreateExecutionShareV1(), now=start)
    assert store.revoke(EXECUTION_ID, revocable.share_id, now=start + timedelta(seconds=1))
    assert store.get_active(revocable.token, now=start + timedelta(seconds=2)) is None
    assert not store.revoke(OTHER_EXECUTION_ID, revocable.share_id)


def test_operator_can_list_active_metadata_and_revoke_all_without_tokens(tmp_path):
    store = ShareStore(str(tmp_path / "shares.db"))
    first = store.create(EXECUTION_ID, CreateExecutionShareV1())
    second = store.create(
        EXECUTION_ID,
        CreateExecutionShareV1(include_candidate_details=True),
    )
    listed = store.list_active(EXECUTION_ID)
    serialized = str([item.model_dump(mode="json") for item in listed])
    assert {item.share_id for item in listed} == {first.share_id, second.share_id}
    assert first.token not in serialized
    assert second.token not in serialized
    assert store.revoke_all(EXECUTION_ID) == 2
    assert store.list_active(EXECUTION_ID) == []
    assert store.revoke_all(EXECUTION_ID) == 0


def test_public_projection_is_allowlist_based_and_path_free(tmp_path):
    store = ShareStore(str(tmp_path / "shares.db"))
    created = store.create(
        EXECUTION_ID,
        CreateExecutionShareV1(
            include_candidate_details=True,
            redact_node_identity=True,
            allow_artifact_download=True,
        ),
    )
    record = store.get_active(created.token)
    public = redact_execution_for_share(
        _execution(), record, manifest=_manifest(), token=created.token
    ).model_dump(mode="json")
    serialized = str(public)

    assert public["task"] == "Build the deliberately shared result"
    assert public["output_preview"] == "The explicitly shared output"
    assert public["produced_files"] == ["candidate_1/code/index.html"]
    assert public["candidates"][0]["produced_files"] == ["candidate_1/code/index.html"]
    assert public["candidates"][0]["node_id"] is None
    for private in (
        "job_private",
        "private-project-id",
        "C:/server/private",
        "private-node-id",
        "private-hostname",
        "private-attempt",
        "server_path",
    ):
        assert private not in serialized


def test_public_artifact_manifest_excludes_internal_and_unselected_candidates(tmp_path):
    store = ShareStore(str(tmp_path / "shares.db"))
    created = store.create(EXECUTION_ID, CreateExecutionShareV1())
    share = store.get_active(created.token)
    base = _manifest()
    entries = list(base.entries)
    for relative_path, candidate_id in (
        ("full_log.json", None),
        ("builder_1_private.md", None),
        ("candidate_1/candidate.md", "candidate-1"),
        ("candidate_2/code/loser.html", "candidate-2"),
    ):
        entries.append(
            ArtifactEntryV1(
                relative_path=relative_path,
                media_type="text/plain",
                size_bytes=1,
                sha256=hashlib.sha256(b"x").hexdigest(),
                source_candidate_id=candidate_id,
                created_at="2026-08-21T12:00:00+00:00",
            )
        )
    complete = ArtifactManifestV1(
        execution_id=EXECUTION_ID,
        created_at=base.created_at,
        file_count=len(entries),
        aggregate_size_bytes=sum(entry.size_bytes for entry in entries),
        entries=entries,
    )

    public = artifact_manifest_for_share(complete, share, _execution())
    assert [entry.relative_path for entry in public.entries] == [
        "candidate_1/code/index.html"
    ]


def test_public_share_without_a_winner_hides_all_candidate_artifacts(tmp_path):
    store = ShareStore(str(tmp_path / "shares.db"))
    created = store.create(EXECUTION_ID, CreateExecutionShareV1())
    share = store.get_active(created.token)
    execution = _execution()
    execution["winning_candidate"] = None
    base = _manifest()
    second = base.entries[0].model_copy(
        update={
            "relative_path": "candidate_2/code/other.html",
            "source_candidate_id": "candidate-2",
        }
    )
    manifest = ArtifactManifestV1(
        execution_id=EXECUTION_ID,
        created_at=base.created_at,
        file_count=2,
        aggregate_size_bytes=base.entries[0].size_bytes + second.size_bytes,
        entries=[base.entries[0], second],
    )

    public = artifact_manifest_for_share(manifest, share, execution)

    assert public.entries == []
    assert public.file_count == 0


@pytest.fixture
def share_api(tmp_path, monkeypatch):
    cfg = {
        "viewer_key": "viewer-secret",
        "viewer_session_ttl_seconds": 3600,
        "viewer_cookie_secure": False,
    }
    monkeypatch.setattr(access_control, "get_config", lambda: cfg)
    monkeypatch.setattr(routes_access, "get_config", lambda: cfg)

    shares = ShareStore(str(tmp_path / "api.db"))
    storage = tmp_path / "storage"
    root = storage / EXECUTION_ID
    root.mkdir(parents=True)
    (root / "safe.txt").write_bytes(b"safe")
    (root / "plan.json").write_text("{}", encoding="utf-8")
    artifacts = ArtifactStore(tmp_path / "api.db", allowed_roots=[storage])
    artifacts.register_root(EXECUTION_ID, root)

    results = {
        EXECUTION_ID: _execution(),
        OTHER_EXECUTION_ID: _execution(OTHER_EXECUTION_ID),
    }

    class FakeService:
        def get(self, execution_id):
            return results.get(execution_id)

    monkeypatch.setattr(routes_access, "get_share_store", lambda: shares)
    monkeypatch.setattr(routes_access, "get_artifact_store", lambda: artifacts)
    monkeypatch.setattr(routes_access, "get_execution_service", lambda: FakeService())

    app = FastAPI()
    app.add_middleware(ViewerAccessMiddleware)
    app.include_router(routes_access.router)
    return TestClient(app), shares


def test_share_api_is_private_to_create_but_public_by_capability(share_api):
    client, _ = share_api
    body = {
        "allow_artifact_download": True,
        "redact_node_identity": True,
        "include_candidate_details": False,
    }
    assert client.post(f"/v1/executions/{EXECUTION_ID}/shares", json=body).status_code == 401
    created_response = client.post(
        f"/v1/executions/{EXECUTION_ID}/shares",
        json=body,
        headers={"X-Viewer-Key": "viewer-secret"},
    )
    assert created_response.status_code == 201
    token = created_response.json()["token"]

    public = client.get(f"/v1/shares/{token}")
    assert public.status_code == 200
    assert public.json()["execution_id"] == EXECUTION_ID
    assert "project_id" not in public.json()
    assert "output_reference" not in public.json()
    for header, value in (
        ("cache-control", "no-store"),
        ("referrer-policy", "no-referrer"),
        ("x-content-type-options", "nosniff"),
    ):
        assert public.headers[header] == value

    manifest = client.get(f"/v1/shares/{token}/artifacts")
    assert manifest.status_code == 200
    assert manifest.json()["entries"][0]["relative_path"] == "safe.txt"
    downloaded = client.get(f"/v1/shares/{token}/artifacts/safe.txt")
    assert downloaded.content == b"safe"
    assert downloaded.headers["x-content-sha256"] == hashlib.sha256(b"safe").hexdigest()
    assert downloaded.headers["cache-control"] == "no-store"
    assert client.get(f"/v1/shares/{token}/artifacts/%2e%2e%2fsafe.txt").status_code != 200

    # The token is bound to exactly one execution and is not ambient viewer
    # authority for a different canonical execution URL.
    other = client.get(f"/v1/executions/{OTHER_EXECUTION_ID}/artifacts")
    assert other.status_code == 401


def test_private_manifest_defaults_to_deliverables_with_explicit_audit_view(share_api):
    client, _ = share_api
    headers = {"X-Viewer-Key": "viewer-secret"}
    deliverables = client.get(
        f"/v1/executions/{EXECUTION_ID}/artifacts",
        headers=headers,
    )
    audit = client.get(
        f"/v1/executions/{EXECUTION_ID}/artifacts?role=audit",
        headers=headers,
    )
    compatibility = client.get(
        f"/v1/executions/{EXECUTION_ID}/artifacts?role=all",
        headers=headers,
    )
    assert [entry["relative_path"] for entry in deliverables.json()["entries"]] == [
        "safe.txt"
    ]
    assert [entry["relative_path"] for entry in audit.json()["entries"]] == [
        "plan.json"
    ]
    assert compatibility.headers["deprecation"] == "true"
    assert {entry["relative_path"] for entry in compatibility.json()["entries"]} == {
        "plan.json",
        "safe.txt",
    }


def test_revoked_and_expired_share_urls_are_not_disclosed(share_api):
    client, shares = share_api
    expired = shares.create(
        EXECUTION_ID,
        CreateExecutionShareV1(expires_in_seconds=60),
        now=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    expired_response = client.get(f"/v1/shares/{expired.token}")
    assert expired_response.status_code == 404
    assert expired_response.headers["cache-control"] == "no-store"

    active = shares.create(EXECUTION_ID, CreateExecutionShareV1())
    assert shares.revoke(EXECUTION_ID, active.share_id)
    revoked_response = client.get(f"/v1/shares/{active.token}")
    assert revoked_response.status_code == 404
    assert revoked_response.headers["cache-control"] == "no-store"


def test_share_without_artifact_permission_cannot_download(share_api):
    client, shares = share_api
    created = shares.create(
        EXECUTION_ID,
        CreateExecutionShareV1(allow_artifact_download=False),
    )
    assert client.get(f"/v1/shares/{created.token}").status_code == 200
    manifest = client.get(f"/v1/shares/{created.token}/artifacts")
    download = client.get(f"/v1/shares/{created.token}/download")
    assert manifest.status_code == 403
    assert download.status_code == 403
    assert manifest.headers["cache-control"] == "no-store"
    assert download.headers["cache-control"] == "no-store"


def test_share_admin_routes_list_revoke_one_and_revoke_all(share_api):
    client, shares = share_api
    first = shares.create(EXECUTION_ID, CreateExecutionShareV1())
    second = shares.create(EXECUTION_ID, CreateExecutionShareV1())
    headers = {"X-Viewer-Key": "viewer-secret"}

    listed = client.get(f"/v1/executions/{EXECUTION_ID}/shares", headers=headers)
    assert listed.status_code == 200
    assert {item["share_id"] for item in listed.json()} == {first.share_id, second.share_id}
    assert first.token not in listed.text

    revoked = client.delete(
        f"/v1/executions/{EXECUTION_ID}/shares/{first.share_id}",
        headers=headers,
    )
    assert revoked.status_code == 204
    revoked_all = client.delete(
        f"/v1/executions/{EXECUTION_ID}/shares",
        headers=headers,
    )
    assert revoked_all.json() == {"execution_id": EXECUTION_ID, "revoked_count": 1}


def test_share_token_path_redaction_never_returns_the_capability():
    token = "top-secret-capability-token"
    redacted = routes_access.redact_share_token_path(
        f"/v1/shares/{token}/artifacts/code/app.py?download=1"
    )
    assert token not in redacted
    assert redacted == "/v1/shares/<redacted>/artifacts/code/app.py?download=1"
