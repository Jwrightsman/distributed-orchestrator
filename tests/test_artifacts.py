"""Canonical artifact manifests are bounded, relative, and root-confined."""

from __future__ import annotations

import hashlib
import os
import time
import zipfile
from pathlib import Path

import pytest

from execution.artifacts import (
    ArtifactLimitError,
    ArtifactNotFound,
    ArtifactSecurityError,
    ArtifactStore,
    normalize_relative_path,
)


EXECUTION_ID = "a" * 32


def _store(tmp_path, **limits):
    storage = tmp_path / "storage"
    storage.mkdir(parents=True)
    root = storage / EXECUTION_ID
    root.mkdir()
    store = ArtifactStore(
        tmp_path / "artifacts.db",
        allowed_roots=[storage],
        **limits,
    )
    store.register_root(EXECUTION_ID, root, strategy="ensemble")
    return store, root


def test_manifest_uses_relative_paths_media_types_and_sha256(tmp_path):
    store, root = _store(tmp_path)
    nested = root / "candidate_1" / "code"
    nested.mkdir(parents=True)
    payload = b"print('hello')\n"
    (nested / "main.py").write_bytes(payload)

    manifest = store.refresh_manifest(EXECUTION_ID)

    assert manifest.file_count == 1
    assert manifest.aggregate_size_bytes == len(payload)
    entry = manifest.entries[0]
    assert entry.relative_path == "candidate_1/code/main.py"
    assert entry.media_type in ("text/x-python", "text/plain")
    assert entry.sha256 == hashlib.sha256(payload).hexdigest()
    assert entry.source_candidate_id == "candidate-1"
    serialized = str(manifest.model_dump(mode="json"))
    assert str(root) not in serialized
    assert not Path(entry.relative_path).is_absolute()


def test_nested_artifact_resolution_rechecks_hash(tmp_path):
    store, root = _store(tmp_path)
    path = root / "nested" / "folder" / "index.html"
    path.parent.mkdir(parents=True)
    path.write_text("<h1>safe</h1>", encoding="utf-8")

    resolved, entry = store.resolve_entry(EXECUTION_ID, "nested/folder/index.html")

    assert resolved == path.resolve()
    assert entry.media_type == "text/html"
    assert entry.size_bytes == len("<h1>safe</h1>".encode())


@pytest.mark.parametrize(
    "bad",
    [
        "../secret.txt",
        "nested/../../secret.txt",
        "/absolute.txt",
        "C:/windows.txt",
        r"nested\windows.txt",
        "nested//double.txt",
        "nested/./dot.txt",
        "%2e%2e/secret.txt",
        "%252e%252e/secret.txt",
        "%2Fabsolute.txt",
    ],
)
def test_invalid_and_encoded_paths_are_rejected(bad):
    with pytest.raises(ArtifactSecurityError):
        normalize_relative_path(bad)


def test_symlink_is_rejected(tmp_path):
    store, root = _store(tmp_path)
    outside = tmp_path / "private.txt"
    outside.write_text("do not expose", encoding="utf-8")
    link = root / "linked.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable in this environment")

    with pytest.raises(ArtifactSecurityError, match="symlink"):
        store.refresh_manifest(EXECUTION_ID)


def test_cross_execution_path_cannot_be_resolved(tmp_path):
    storage = tmp_path / "storage"
    first = storage / ("a" * 32)
    second = storage / ("b" * 32)
    first.mkdir(parents=True)
    second.mkdir()
    (second / "secret.txt").write_text("private", encoding="utf-8")
    store = ArtifactStore(tmp_path / "artifacts.db", allowed_roots=[storage])
    store.register_root("a" * 32, first)
    store.register_root("b" * 32, second)

    with pytest.raises(ArtifactSecurityError):
        store.resolve_entry("a" * 32, "../" + "b" * 32 + "/secret.txt")
    with pytest.raises(ArtifactNotFound):
        store.resolve_entry("a" * 32, "secret.txt")


def test_file_count_and_aggregate_quotas_are_enforced(tmp_path):
    count_store, count_root = _store(tmp_path / "count", max_files=1)
    (count_root / "one.txt").write_text("1", encoding="utf-8")
    (count_root / "two.txt").write_text("2", encoding="utf-8")
    with pytest.raises(ArtifactLimitError, match="file-count"):
        count_store.refresh_manifest(EXECUTION_ID)

    total_store, total_root = _store(tmp_path / "total", max_aggregate_bytes=4)
    (total_root / "large.txt").write_text("12345", encoding="utf-8")
    with pytest.raises(ArtifactLimitError, match="aggregate"):
        total_store.refresh_manifest(EXECUTION_ID)


def test_archive_is_prepared_on_disk_with_normalized_names(tmp_path):
    store, root = _store(tmp_path)
    (root / "nested").mkdir()
    (root / "nested" / "one.txt").write_text("one", encoding="utf-8")
    (root / "two.json").write_text('{"two":2}', encoding="utf-8")

    prepared = store.prepare_archive(EXECUTION_ID)
    try:
        assert prepared.path.is_file()
        assert prepared.path.parent != root
        with zipfile.ZipFile(prepared.path) as archive:
            assert archive.namelist() == ["nested/one.txt", "two.json"]
            assert archive.read("nested/one.txt") == b"one"
    finally:
        prepared.path.unlink(missing_ok=True)


def test_active_execution_is_never_pruned(tmp_path):
    store, root = _store(tmp_path)
    (root / "result.txt").write_text("result", encoding="utf-8")
    store.set_active(EXECUTION_ID, True)

    assert store.active_root_paths() == {root.resolve()}
    assert store.prune(retention_seconds=1, max_total_bytes=1, now=time.time() + 60) == []
    assert root.exists()

    store.set_active(EXECUTION_ID, False)
    assert store.prune(retention_seconds=1, max_total_bytes=1, now=time.time() + 60) == [EXECUTION_ID]
    assert not root.exists()


def test_one_root_cannot_be_registered_to_two_executions(tmp_path):
    store, root = _store(tmp_path)
    with pytest.raises(ArtifactSecurityError, match="multiple executions"):
        store.register_root("b" * 32, root)


def test_retention_covers_output_and_execution_artifact_roots(tmp_path):
    output_root = tmp_path / "output" / "legacy-run"
    ensemble_root = tmp_path / "execution_artifacts" / ("b" * 32)
    output_root.mkdir(parents=True)
    ensemble_root.mkdir(parents=True)
    (output_root / "output.md").write_text("dag", encoding="utf-8")
    (ensemble_root / "candidate.md").write_text("ensemble", encoding="utf-8")
    store = ArtifactStore(
        tmp_path / "artifacts.db",
        allowed_roots=[tmp_path / "output", tmp_path / "execution_artifacts"],
    )
    store.register_root("a" * 32, output_root, strategy="dag")
    store.register_root("b" * 32, ensemble_root, strategy="ensemble")

    deleted = store.prune(retention_seconds=1, max_total_bytes=1, now=time.time() + 60)

    assert deleted == ["a" * 32, "b" * 32]
    assert not output_root.exists()
    assert not ensemble_root.exists()
