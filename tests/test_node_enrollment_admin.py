from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import node_enrollments
from scripts import node_enrollment_admin as admin
from worker_identity import load_worker_identity


OLD_CREDENTIAL_A = "a" * 43
OLD_CREDENTIAL_B = "b" * 43
OLD_CREDENTIAL_C = "c" * 43


def _state(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    store = node_enrollments.NodeEnrollmentStore(state / "events.db")
    store.migrate()
    return state, store


def test_list_is_secret_free_and_contains_only_public_metadata(tmp_path, capsys):
    state, store = _state(tmp_path)
    enrolled = store.bootstrap("node-a", OLD_CREDENTIAL_A, now=10).record

    assert (
        admin.main(["--state-dir", str(state), "list", "--json"])
        == 0
    )

    output = capsys.readouterr()
    listed = json.loads(output.out)
    assert listed[0]["enrollment_id"] == enrolled.enrollment_id
    assert listed[0]["node_id"] == "node-a"
    assert listed[0]["status"] == "active"
    assert "credential_digest" not in output.out
    assert OLD_CREDENTIAL_A not in output.out
    assert OLD_CREDENTIAL_A not in output.err


def test_revoke_is_idempotent_and_does_not_affect_another_enrollment(
    tmp_path, capsys
):
    state, store = _state(tmp_path)
    first = store.bootstrap("node-a", OLD_CREDENTIAL_A, now=10).record
    second = store.bootstrap("node-b", OLD_CREDENTIAL_B, now=10).record
    command = [
        "--state-dir",
        str(state),
        "revoke",
        first.enrollment_id,
        "--reason",
        "operator test",
    ]

    assert admin.main(command) == 0
    assert admin.main(command) == 0

    revoked = store.get(first.enrollment_id)
    assert revoked is not None
    assert revoked.status == "revoked"
    assert revoked.revocation_reason == "operator test"
    with pytest.raises(node_enrollments.EnrollmentRevoked):
        store.authenticate("node-a", OLD_CREDENTIAL_A)
    assert store.authenticate("node-b", OLD_CREDENTIAL_B).enrollment_id == second.enrollment_id
    rendered = capsys.readouterr()
    assert OLD_CREDENTIAL_A not in rendered.out + rendered.err
    assert OLD_CREDENTIAL_B not in rendered.out + rendered.err


def test_rotate_writes_private_identity_without_printing_secret_and_retry_is_safe(
    tmp_path, capsys
):
    state, store = _state(tmp_path)
    enrollment = store.bootstrap("node-a", OLD_CREDENTIAL_A, now=10).record
    identity_output = tmp_path / "handoff" / "node-a.json"
    command = [
        "--state-dir",
        str(state),
        "rotate",
        enrollment.enrollment_id,
        "--coordinator",
        "HTTPS://Coordinator.Example:443/",
        "--identity-output",
        str(identity_output),
    ]

    assert admin.main(command) == 0
    first_output = capsys.readouterr()
    identity = load_worker_identity(
        identity_output,
        coordinator="https://coordinator.example",
        node_id="node-a",
    )
    new_credential = identity.enrollment_credential

    assert identity.enrollment_id == enrollment.enrollment_id
    assert identity.credential_version == 2
    assert new_credential != OLD_CREDENTIAL_A
    assert new_credential not in first_output.out + first_output.err
    with pytest.raises(node_enrollments.EnrollmentAuthenticationFailed):
        store.authenticate("node-a", OLD_CREDENTIAL_A)
    assert store.authenticate("node-a", new_credential).enrollment_id == enrollment.enrollment_id
    if os.name == "posix":
        assert stat.S_IMODE(identity_output.stat().st_mode) == 0o600

    # Existing files are refused by default so the current worker identity
    # cannot be mistaken for a prepared rotation. Explicit resume is the retry
    # path after an ambiguous commit.
    assert admin.main(command) == 1
    refused_output = capsys.readouterr()
    assert new_credential not in refused_output.out + refused_output.err
    assert admin.main([*command, "--resume-existing"]) == 0
    second_output = capsys.readouterr()
    retried = load_worker_identity(identity_output)
    assert retried.enrollment_credential == new_credential
    assert new_credential not in second_output.out + second_output.err

    with sqlite3.connect(state / "events.db") as connection:
        database_bytes = "\n".join(
            str(value)
            for row in connection.execute("SELECT * FROM node_enrollments")
            for value in row
        )
    assert OLD_CREDENTIAL_A not in database_bytes
    assert new_credential not in database_bytes

    # A bundle prepared for v2 cannot be replayed after a later v3 rotation.
    advanced = store.rotate(
        enrollment.enrollment_id,
        OLD_CREDENTIAL_C,
        expected_credential_version=2,
    )
    assert advanced.record.credential_version == 3
    assert admin.main([*command, "--resume-existing"]) == 1
    assert store.authenticate("node-a", OLD_CREDENTIAL_C).credential_version == 3


def test_rotate_rejects_revoked_enrollment_before_writing_identity(tmp_path, capsys):
    state, store = _state(tmp_path)
    enrollment = store.bootstrap("node-a", OLD_CREDENTIAL_A).record
    store.revoke(enrollment.enrollment_id, "retired")
    output = tmp_path / "must-not-exist.json"

    result = admin.main(
        [
            "--state-dir",
            str(state),
            "rotate",
            enrollment.enrollment_id,
            "--coordinator",
            "https://coordinator.example",
            "--identity-output",
            str(output),
        ]
    )

    assert result == 1
    assert not output.exists()
    assert OLD_CREDENTIAL_A not in capsys.readouterr().err


def test_concurrent_rotation_cannot_overwrite_the_committed_identity(tmp_path):
    _state_path, store = _state(tmp_path)
    enrollment = store.bootstrap("node-a", OLD_CREDENTIAL_A).record
    output = tmp_path / "handoff" / "node-a.json"
    barrier = threading.Barrier(2)

    def rotate():
        barrier.wait()
        try:
            admin.rotate_enrollment(
                store,
                enrollment.enrollment_id,
                coordinator="https://coordinator.example",
                identity_output=output,
            )
            return "ok"
        except admin.EnrollmentAdminError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(rotate), pool.submit(rotate)]
        outcomes = [future.result(timeout=10) for future in futures]

    assert sorted(outcomes) == ["ok", "refused"]
    identity = load_worker_identity(output)
    authenticated = store.authenticate("node-a", identity.enrollment_credential)
    assert authenticated.enrollment_id == enrollment.enrollment_id
    assert authenticated.credential_version == identity.credential_version == 2
