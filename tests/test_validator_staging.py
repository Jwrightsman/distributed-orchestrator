"""Validator inputs are copied into a bounded candidate-local stage."""

from __future__ import annotations

import hashlib
import os
import socket
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from execution.artifacts import ArtifactEntryV1
from execution.validator_staging import (
    StagingLimits,
    ValidatorStagingAborted,
    ValidatorStagingIntegrityError,
    ValidatorStagingLimitError,
    ValidatorStagingSecurityError,
    stage_validator_files,
    validate_validator_file_names,
)


def _entry(root: Path, path: Path, *, size: int | None = None, digest: str | None = None):
    payload = path.read_bytes()
    return ArtifactEntryV1(
        relative_path=path.relative_to(root).as_posix(),
        media_type="text/x-python",
        size_bytes=len(payload) if size is None else size,
        sha256=hashlib.sha256(payload).hexdigest() if digest is None else digest,
        role="deliverable",
        source_candidate_id="candidate-1",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _tree(tmp_path: Path):
    root = tmp_path / "artifacts" / ("a" * 32)
    subtree = root / "candidate_1" / "code"
    subtree.mkdir(parents=True)
    return root, subtree


def _stage(
    tmp_path: Path,
    root: Path,
    subtree: Path,
    files,
    *,
    entries=None,
    limits=None,
    name="stage",
):
    destination = tmp_path / name
    relative = stage_validator_files(
        authoritative_root=root,
        authoritative_subtree=subtree,
        selected_files=files,
        staging_root=destination,
        validated_entries=entries,
        limits=limits,
    )
    return destination, relative


def test_normal_files_are_copied_with_only_normalized_relative_paths(tmp_path):
    root, subtree = _tree(tmp_path)
    first = subtree / "main.py"
    second = subtree / "nested" / "page.html"
    second.parent.mkdir()
    first.write_text("print('safe')\n", encoding="utf-8")
    second.write_text("<!doctype html><html></html>\n", encoding="utf-8")

    stage, relative = _stage(
        tmp_path,
        root,
        subtree,
        [first, Path("nested/page.html")],
        entries=[_entry(root, first), _entry(root, second)],
    )

    assert relative == ("main.py", "nested/page.html")
    assert (stage / "main.py").read_bytes() == first.read_bytes()
    assert (stage / "nested" / "page.html").read_bytes() == second.read_bytes()
    assert all(not Path(value).is_absolute() for value in relative)
    if os.name == "posix":
        assert stat.S_IMODE(stage.stat().st_mode) == 0o700
        assert stat.S_IMODE((stage / "main.py").stat().st_mode) == 0o600


def test_stream_copy_does_not_create_hard_links(tmp_path):
    root, subtree = _tree(tmp_path)
    source = subtree / "main.py"
    source.write_bytes(b"baseline")

    stage, _ = _stage(tmp_path, root, subtree, [source], entries=[_entry(root, source)])
    staged = stage / "main.py"

    source.write_bytes(b"changed!")
    assert staged.read_bytes() == b"baseline"
    assert os.path.samefile(source, staged) is False


def test_metadata_name_validation_uses_shared_selection_without_reading_bytes(
    tmp_path,
    monkeypatch,
):
    root, subtree = _tree(tmp_path)
    source = subtree / "nested" / "large.txt"
    source.parent.mkdir()
    source.write_bytes(b"x" * 32)

    def unexpected_open(*_args, **_kwargs):
        raise AssertionError("metadata-only validation opened artifact content")

    monkeypatch.setattr("execution.validator_staging._open_source", unexpected_open)
    relative = validate_validator_file_names(
        authoritative_root=root,
        authoritative_subtree=subtree,
        selected_files=[source],
        validated_entries=[_entry(root, source, size=999, digest="0" * 64)],
    )

    assert relative == ("nested/large.txt",)


def test_metadata_name_validation_rejects_missing_snapshot_entry(tmp_path):
    root, subtree = _tree(tmp_path)
    selected = subtree / "main.py"
    selected.write_text("selected", encoding="utf-8")
    other = root / "candidate_2" / "code" / "main.py"
    other.parent.mkdir(parents=True)
    other.write_text("other", encoding="utf-8")

    with pytest.raises(ValidatorStagingIntegrityError, match="absent"):
        validate_validator_file_names(
            authoritative_root=root,
            authoritative_subtree=subtree,
            selected_files=[selected],
            validated_entries=[_entry(root, other)],
        )


def test_metadata_name_validation_can_abort_during_selection(tmp_path):
    root, subtree = _tree(tmp_path)
    files = [subtree / f"{index}.txt" for index in range(3)]
    for path in files:
        path.write_text("x", encoding="utf-8")
    checks = 0

    def abort_reason():
        nonlocal checks
        checks += 1
        return "validator_cancelled" if checks >= 3 else None

    with pytest.raises(ValidatorStagingAborted) as raised:
        validate_validator_file_names(
            authoritative_root=root,
            authoritative_subtree=subtree,
            selected_files=files,
            abort_reason=abort_reason,
        )

    assert raised.value.reason == "validator_cancelled"


@pytest.mark.parametrize("selected", ["../secret.py", "nested/../../secret.py"])
def test_traversal_is_rejected_and_partial_stage_is_absent(tmp_path, selected):
    root, subtree = _tree(tmp_path)
    (root / "candidate_1" / "secret.py").write_text("secret", encoding="utf-8")

    with pytest.raises(ValidatorStagingSecurityError):
        validate_validator_file_names(
            authoritative_root=root,
            authoritative_subtree=subtree,
            selected_files=[selected],
        )

    with pytest.raises(ValidatorStagingSecurityError):
        _stage(tmp_path, root, subtree, [selected])

    assert not (tmp_path / "stage").exists()


def test_absolute_file_outside_subtree_is_rejected(tmp_path):
    root, subtree = _tree(tmp_path)
    outside = root / "candidate_2" / "code" / "other.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("other", encoding="utf-8")

    with pytest.raises(ValidatorStagingSecurityError, match="escaped"):
        _stage(tmp_path, root, subtree, [outside])

    assert not (tmp_path / "stage").exists()


def test_symlink_file_is_rejected(tmp_path):
    root, subtree = _tree(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("outside", encoding="utf-8")
    link = subtree / "linked.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(ValidatorStagingSecurityError, match="symlink|reparse"):
        validate_validator_file_names(
            authoritative_root=root,
            authoritative_subtree=subtree,
            selected_files=[link],
        )

    with pytest.raises(ValidatorStagingSecurityError, match="symlink|reparse"):
        _stage(tmp_path, root, subtree, [link])


def test_symlink_directory_is_rejected(tmp_path):
    root, subtree = _tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "linked.py").write_text("outside", encoding="utf-8")
    link = subtree / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(ValidatorStagingSecurityError, match="symlink|reparse"):
        _stage(tmp_path, root, subtree, [link / "linked.py"])


@pytest.mark.skipif(os.name != "nt", reason="Windows junction coverage")
def test_windows_junction_is_rejected(tmp_path):
    import _winapi

    root, subtree = _tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "linked.py").write_text("outside", encoding="utf-8")
    junction = subtree / "junction"
    _winapi.CreateJunction(str(outside), str(junction))

    with pytest.raises(ValidatorStagingSecurityError, match="symlink|reparse"):
        _stage(tmp_path, root, subtree, [junction / "linked.py"])


@pytest.mark.skipif(os.name != "posix", reason="POSIX special-file coverage")
def test_fifo_is_rejected_without_opening_it(tmp_path):
    root, subtree = _tree(tmp_path)
    fifo = subtree / "input.pipe"
    os.mkfifo(fifo)

    with pytest.raises(ValidatorStagingSecurityError, match="regular file"):
        _stage(tmp_path, root, subtree, [fifo])


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "AF_UNIX"),
    reason="Unix-domain socket coverage",
)
def test_socket_is_rejected():
    # Linux limits AF_UNIX addresses to roughly 108 bytes. GitHub Actions uses
    # a deliberately long pytest workspace, so build this fixture directly
    # below the system temp root while retaining the full authoritative tree.
    with tempfile.TemporaryDirectory(prefix="mv-sock-") as temporary:
        short_root = Path(temporary)
        root, subtree = _tree(short_root)
        socket_path = subtree / "validator.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(socket_path))
            with pytest.raises(ValidatorStagingSecurityError, match="regular file"):
                _stage(short_root, root, subtree, [socket_path])
        finally:
            listener.close()


def test_file_count_limit_is_enforced_before_stage_creation(tmp_path):
    root, subtree = _tree(tmp_path)
    files = [subtree / "one.py", subtree / "two.py"]
    for path in files:
        path.write_text("x", encoding="utf-8")

    with pytest.raises(ValidatorStagingLimitError, match="file-count"):
        _stage(tmp_path, root, subtree, files, limits=StagingLimits(max_files=1))

    assert not (tmp_path / "stage").exists()


def test_per_file_limit_cleans_partial_stage(tmp_path):
    root, subtree = _tree(tmp_path)
    source = subtree / "large.py"
    source.write_bytes(b"12345")

    with pytest.raises(ValidatorStagingLimitError, match="per-file"):
        _stage(
            tmp_path,
            root,
            subtree,
            [source],
            limits=StagingLimits(max_file_bytes=4),
        )

    assert not (tmp_path / "stage").exists()


def test_aggregate_limit_cleans_files_already_copied(tmp_path):
    root, subtree = _tree(tmp_path)
    first = subtree / "one.py"
    second = subtree / "two.py"
    first.write_bytes(b"123")
    second.write_bytes(b"456")

    with pytest.raises(ValidatorStagingLimitError, match="aggregate"):
        _stage(
            tmp_path,
            root,
            subtree,
            [first, second],
            limits=StagingLimits(max_aggregate_bytes=5),
        )

    assert not (tmp_path / "stage").exists()


def test_cancellation_during_copy_removes_the_partial_stage(tmp_path):
    root, subtree = _tree(tmp_path)
    source = subtree / "main.py"
    source.write_bytes(b"x" * (2 * 1024 * 1024))
    checks = 0

    def abort_reason():
        nonlocal checks
        checks += 1
        return "validator_cancelled" if checks >= 6 else None

    with pytest.raises(ValidatorStagingAborted) as raised:
        stage_validator_files(
            authoritative_root=root,
            authoritative_subtree=subtree,
            selected_files=[source],
            staging_root=tmp_path / "stage",
            abort_reason=abort_reason,
        )

    assert raised.value.reason == "validator_cancelled"
    assert not (tmp_path / "stage").exists()


def test_relative_path_limit_is_enforced(tmp_path):
    root, subtree = _tree(tmp_path)
    source = subtree / "long-name.py"
    source.write_text("x", encoding="utf-8")

    with pytest.raises(ValidatorStagingLimitError, match="relative-path"):
        _stage(
            tmp_path,
            root,
            subtree,
            [source],
            limits=StagingLimits(max_relative_path_length=5),
        )


def test_snapshot_must_contain_the_selected_candidate_file(tmp_path):
    root, subtree = _tree(tmp_path)
    selected = subtree / "main.py"
    selected.write_text("selected", encoding="utf-8")
    other = root / "candidate_2" / "code" / "main.py"
    other.parent.mkdir(parents=True)
    other.write_text("other", encoding="utf-8")

    with pytest.raises(ValidatorStagingIntegrityError, match="absent"):
        _stage(tmp_path, root, subtree, [selected], entries=[_entry(root, other)])

    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize(
    "entry_override",
    [
        {"size": 999},
        {"digest": "0" * 64},
    ],
)
def test_snapshot_size_or_hash_mismatch_cleans_partial_stage(tmp_path, entry_override):
    root, subtree = _tree(tmp_path)
    selected = subtree / "main.py"
    selected.write_text("selected", encoding="utf-8")
    claim = _entry(root, selected, **entry_override)

    with pytest.raises(ValidatorStagingIntegrityError, match="differs"):
        _stage(tmp_path, root, subtree, [selected], entries=[claim])

    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_files", 0),
        ("max_files", True),
        ("max_file_bytes", 50 * 1024 * 1024 + 1),
        ("max_aggregate_bytes", 100 * 1024 * 1024 + 1),
        ("max_relative_path_length", 501),
    ],
)
def test_staging_limits_are_strictly_bounded(field, value):
    with pytest.raises(ValueError, match=field):
        StagingLimits(**{field: value})


def test_existing_destination_is_not_reused_or_removed(tmp_path):
    root, subtree = _tree(tmp_path)
    source = subtree / "main.py"
    source.write_text("safe", encoding="utf-8")
    destination = tmp_path / "stage"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ValidatorStagingSecurityError, match="must not already exist"):
        _stage(tmp_path, root, subtree, [source])

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_successful_empty_selection_creates_an_empty_restrictive_stage(tmp_path):
    root, subtree = _tree(tmp_path)

    stage, relative = _stage(tmp_path, root, subtree, [])

    assert relative == ()
    assert stage.is_dir()
    assert list(stage.iterdir()) == []


def test_module_imports_on_supported_python():
    assert sys.version_info >= (3, 11)
