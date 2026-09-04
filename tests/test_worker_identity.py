from __future__ import annotations

import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import worker_identity as identities


TEST_CREDENTIAL = "a" * 43
TEST_ENROLLMENT_ID = "11111111111141118111111111111111"


def _create(path: Path, coordinator: str = "https://Example.COM:443/"):
    return identities.create_worker_identity(
        path,
        coordinator=coordinator,
        node_id=" Worker-A ",
        credential_factory=lambda: TEST_CREDENTIAL,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM:443/", "https://example.com"),
        ("http://EXAMPLE.com:80", "http://example.com"),
        ("http://example.com:8000/", "http://example.com:8000"),
        ("http://[2001:0db8::1]:8000", "http://[2001:db8::1]:8000"),
    ],
)
def test_coordinator_normalization_is_stable(raw, expected):
    assert identities.normalize_coordinator(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://example.com",
        "https://user:password@example.com",
        "https://example.com/path",
        "https://example.com?credential=value",
        "https://example.com#fragment",
        "https://exa mple.com",
    ],
)
def test_ambiguous_or_unsafe_coordinator_urls_are_rejected(raw):
    with pytest.raises(identities.WorkerIdentityError):
        identities.normalize_coordinator(raw)


def test_default_filename_hashes_coordinator_and_never_contains_url(tmp_path):
    first = identities.default_identity_file(
        "https://worker.example:8443", config_dir=tmp_path
    )
    same = identities.default_identity_file(
        "HTTPS://WORKER.EXAMPLE:8443/", config_dir=tmp_path
    )
    other = identities.default_identity_file(
        "https://other.example:8443", config_dir=tmp_path
    )

    assert first == same
    assert first != other
    assert first.parent == tmp_path / "nodes"
    assert len(first.stem) == 64
    assert "worker" not in first.name
    assert "example" not in first.name


def test_new_identity_is_written_before_use_and_secret_is_repr_safe(tmp_path):
    path = tmp_path / "private" / "node.json"
    identity = _create(path)

    assert path.is_file()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == {
        "version": 1,
        "coordinator": "https://example.com",
        "node_id": "worker-a",
        "enrollment_id": None,
        "credential_version": None,
        "enrollment_credential": TEST_CREDENTIAL,
    }
    assert TEST_CREDENTIAL not in repr(identity)


def test_initial_identity_creation_uses_atomic_replace(tmp_path, monkeypatch):
    target = tmp_path / "identity.json"
    replacements: list[tuple[Path, Path, bool]] = []
    real_replace = identities.os.replace

    def observed_replace(source, destination):
        replacements.append((Path(source), Path(destination), Path(source).is_file()))
        return real_replace(source, destination)

    monkeypatch.setattr(identities.os, "replace", observed_replace)

    _create(target)

    assert len(replacements) == 1
    temporary, destination, existed_before_replace = replacements[0]
    assert temporary.parent == target.parent
    assert temporary != target
    assert destination == target
    assert existed_before_replace is True


# A deadlock guard, not a performance budget, and deliberately far too large
# to double as one. Two first starts settle in ~3ms on an idle machine and
# ~230ms at the 95th percentile under a heavy fsync storm, but each start
# fsyncs both the identity file and its directory inside an exclusive flock,
# and fsync latency is set by what the disk is doing rather than by how fast
# the machine is. A budget tight enough to measure that is measuring the disk;
# reaching this one means the two starts are not going to finish at all.
CONCURRENT_START_DEADLOCK_GUARD_SECONDS = 120


def test_concurrent_first_start_converges_on_one_durable_identity(tmp_path):
    target = tmp_path / "identity.json"
    barrier = threading.Barrier(2)

    def start(credential: str):
        # Bounded so a worker that never runs surfaces as a broken barrier in
        # the thread that did run, rather than parking the suite forever.
        barrier.wait(timeout=CONCURRENT_START_DEADLOCK_GUARD_SECONDS)
        return identities.load_or_create_worker_identity(
            target,
            coordinator="https://example.com",
            node_id="worker-a",
            credential_factory=lambda: credential,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(start, "a" * 43),
            pool.submit(start, "b" * 43),
        ]
        try:
            # Twice the barrier's guard, so the barrier always gets to explain
            # a stall first instead of the two racing to report it.
            results = [
                future.result(timeout=2 * CONCURRENT_START_DEADLOCK_GUARD_SECONDS)
                for future in futures
            ]
        except threading.BrokenBarrierError:
            pytest.fail(
                "the two first starts never overlapped: one never reached the "
                f"barrier within {CONCURRENT_START_DEADLOCK_GUARD_SECONDS}s, so "
                "nothing here was a concurrent creation. This is a scheduling "
                "failure, not a convergence failure."
            )
        except TimeoutError:
            pytest.fail(
                "a first start never returned within "
                f"{2 * CONCURRENT_START_DEADLOCK_GUARD_SECONDS}s. The creation "
                "lock deadlocked; the credentials below were never compared, "
                "so this says nothing about whether they converge."
            )

    stored = identities.load_worker_identity(target)
    assert results[0].enrollment_credential == results[1].enrollment_credential
    assert stored.enrollment_credential == results[0].enrollment_credential
    assert stored.enrollment_credential in {"a" * 43, "b" * 43}


def test_learned_enrollment_is_atomic_and_mismatch_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "node.json"
    identity = _create(path)
    original = path.read_bytes()
    real_replace = identities.os.replace
    calls: list[tuple[Path, Path]] = []

    def record_replace(source, destination):
        calls.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(identities.os, "replace", record_replace)
    learned = identities.persist_learned_enrollment(
        path, identity, TEST_ENROLLMENT_ID, 1
    )

    assert calls and calls[-1][0].parent == path.parent
    assert calls[-1][1] == path
    assert learned.enrollment_id == TEST_ENROLLMENT_ID
    assert path.read_bytes() != original
    with pytest.raises(identities.WorkerIdentityError, match="different enrollment"):
        identities.persist_learned_enrollment(
            path,
            learned,
            "22222222222242228222222222222222",
            1,
        )
    assert json.loads(path.read_text(encoding="utf-8"))["enrollment_id"] == TEST_ENROLLMENT_ID


def test_failed_atomic_replace_preserves_existing_identity(tmp_path, monkeypatch):
    path = tmp_path / "node.json"
    identity = _create(path)
    original = path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(identities.os, "replace", fail_replace)
    with pytest.raises(identities.WorkerIdentityError, match="cannot be written"):
        identities.persist_learned_enrollment(path, identity, TEST_ENROLLMENT_ID, 1)

    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".node.json.*.tmp"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode checks do not model Windows ACLs")
def test_identity_permissions_are_private_and_permissive_file_fails_closed(tmp_path):
    path = tmp_path / "private" / "node.json"
    _create(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    os.chmod(path, 0o644)
    with pytest.raises(identities.WorkerIdentityError, match="mode 0600"):
        identities.load_worker_identity(
            path,
            coordinator="https://example.com",
            node_id="worker-a",
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: "{broken",
        lambda value: value.replace('"version": 1', '"version": 2'),
        lambda value: value.replace(TEST_CREDENTIAL, "short"),
        lambda value: value.replace(
            '"node_id": "worker-a"', '"node_id": "worker-a", "extra": true'
        ),
        lambda value: value.replace(
            '"node_id": "worker-a"',
            '"node_id": "worker-a", "node_id": "worker-b"',
        ),
    ],
)
def test_malformed_identity_files_fail_closed_without_secret_in_error(tmp_path, mutation):
    path = tmp_path / "node.json"
    _create(path)
    path.write_text(mutation(path.read_text(encoding="utf-8")), encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o600)

    with pytest.raises(identities.WorkerIdentityError) as raised:
        identities.load_worker_identity(path)
    assert TEST_CREDENTIAL not in str(raised.value)


def test_wrong_coordinator_or_node_never_rebinds_an_existing_identity(tmp_path):
    path = tmp_path / "node.json"
    _create(path)

    with pytest.raises(identities.WorkerIdentityError, match="different coordinator"):
        identities.load_worker_identity(
            path,
            coordinator="https://other.example",
            node_id="worker-a",
        )
    with pytest.raises(identities.WorkerIdentityError, match="different node_id"):
        identities.load_worker_identity(
            path,
            coordinator="https://example.com",
            node_id="worker-b",
        )
