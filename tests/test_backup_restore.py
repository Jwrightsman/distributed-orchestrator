"""Focused recovery tests for the trusted-alpha state bundle."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import threading
import zipfile
from pathlib import Path

import pytest

from scripts import backup, restore


def _create_state(root: Path, *, include_shadow_health: bool = True) -> Path:
    state = root / "state"
    state.mkdir(parents=True)
    with sqlite3.connect(state / "events.db") as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO records(value) VALUES ('durable')")
    if include_shadow_health:
        with sqlite3.connect(
            state / backup.SHADOW_OPERATIONAL_DATABASE_NAME
        ) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE shadow_health "
                "(id INTEGER PRIMARY KEY, outcome TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO shadow_health(outcome) VALUES ('completed')"
            )

    (state / "config.json").write_text(
        json.dumps(
            {
                "deployment_mode": "trusted_alpha",
                "viewer_key": "viewer-super-secret",
                "pitch_key": "pitch-super-secret",
                "node_secret": "node-super-secret",
            }
        ),
        encoding="utf-8",
    )
    (state / "ledger.json").write_text(
        json.dumps([{"contributor": "node-a", "points": 5}]), encoding="utf-8"
    )
    for directory in backup.STATE_DIRECTORIES:
        (state / directory).mkdir()
    (state / "projects" / "alpha.json").write_text('{"name":"alpha"}', encoding="utf-8")
    (state / "output" / "answer.md").write_text("answer", encoding="utf-8")
    artifact = state / "execution_artifacts" / "execution-a"
    artifact.mkdir()
    (artifact / "result.txt").write_text("artifact", encoding="utf-8")
    return state


def _manifest(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as bundle:
        return json.loads(bundle.read(backup.MANIFEST_NAME))


def _rewrite_member(source: Path, destination: Path, member: str, payload: bytes) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as changed:
        for info in original.infolist():
            data = original.read(info)
            changed.writestr(info, payload if info.filename == member else data)


def test_backup_captures_recovery_state_with_manifest_and_does_not_print_secrets(
    tmp_path, capsys
):
    state = _create_state(tmp_path)
    destination = tmp_path / "snapshot.zip"

    assert backup.main(["--destination", str(destination), "--state-dir", str(state)]) == 0

    output = capsys.readouterr()
    for secret in ("viewer-super-secret", "pitch-super-secret", "node-super-secret"):
        assert secret not in output.out
        assert secret not in output.err

    manifest = _manifest(destination)
    assert manifest["format"] == backup.BACKUP_FORMAT
    assert manifest["format_version"] == backup.BACKUP_FORMAT_VERSION
    entries = {entry["path"]: entry for entry in manifest["entries"]}
    expected = {
        "metadata/build.json",
        "state/events.db",
        "state/capability-shadow-health.db",
        "state/config.json",
        "state/ledger.json",
        "state/projects",
        "state/projects/alpha.json",
        "state/output",
        "state/output/answer.md",
        "state/execution_artifacts",
        "state/execution_artifacts/execution-a",
        "state/execution_artifacts/execution-a/result.txt",
    }
    assert expected <= entries.keys()
    assert manifest["checksums"] == {
        path: entry["sha256"]
        for path, entry in entries.items()
        if entry["kind"] == "file"
    }

    with zipfile.ZipFile(destination) as bundle:
        for path, expected_digest in manifest["checksums"].items():
            assert hashlib.sha256(bundle.read(path)).hexdigest() == expected_digest
        config = json.loads(bundle.read("state/config.json"))
        build = json.loads(bundle.read("metadata/build.json"))
    assert config["viewer_key"] == "viewer-super-secret"
    assert build["product"] == "Mycelium"
    assert "build_fingerprint" in build


def test_backup_uses_a_consistent_live_sqlite_snapshot(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    database = state / "events.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE left_side (generation INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE right_side (generation INTEGER PRIMARY KEY)")

    started = threading.Event()
    stop = threading.Event()
    writer_errors: list[Exception] = []

    def write_transactions() -> None:
        try:
            with sqlite3.connect(database, timeout=10.0) as connection:
                connection.execute("PRAGMA busy_timeout = 10000")
                generation = 0
                while not stop.is_set():
                    generation += 1
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("INSERT INTO left_side VALUES (?)", (generation,))
                    connection.execute("INSERT INTO right_side VALUES (?)", (generation,))
                    connection.commit()
                    started.set()
        except Exception as exc:  # pragma: no cover - assertion below surfaces details
            writer_errors.append(exc)
            started.set()

    writer = threading.Thread(target=write_transactions)
    writer.start()
    assert started.wait(timeout=5)
    try:
        archive = backup.create_backup(tmp_path / "live.zip", state_dir=state)
    finally:
        stop.set()
        writer.join(timeout=10)

    assert not writer.is_alive()
    assert not writer_errors
    with zipfile.ZipFile(archive) as bundle:
        snapshot = tmp_path / "snapshot.db"
        snapshot.write_bytes(bundle.read("state/events.db"))
    with sqlite3.connect(snapshot) as connection:
        left_count = connection.execute("SELECT count(*) FROM left_side").fetchone()[0]
        right_count = connection.execute("SELECT count(*) FROM right_side").fetchone()[0]
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert left_count == right_count
    assert left_count > 0


def test_backup_and_restore_work_before_shadow_health_database_exists(tmp_path):
    state = _create_state(tmp_path / "source", include_shadow_health=False)

    archive = backup.create_backup(tmp_path / "without-health.zip", state_dir=state)
    manifest = _manifest(archive)
    assert manifest["state"]["shadow_operational_health_database"] is None
    assert all(
        entry["path"] != f"state/{backup.SHADOW_OPERATIONAL_DATABASE_NAME}"
        for entry in manifest["entries"]
    )

    target = tmp_path / "restored-without-health"
    restore.restore_backup(archive, state_dir=target)
    assert (target / "events.db").is_file()
    assert not (target / backup.SHADOW_OPERATIONAL_DATABASE_NAME).exists()


def test_restore_accepts_legacy_v1_archive_without_shadow_health_database(
    tmp_path,
    monkeypatch,
):
    state = _create_state(
        tmp_path / "legacy-source",
        include_shadow_health=False,
    )
    monkeypatch.setattr(backup, "BACKUP_FORMAT_VERSION", 1)
    versioned = backup.create_backup(tmp_path / "versioned.zip", state_dir=state)
    legacy = tmp_path / "legacy-v1.zip"
    legacy_manifest = _manifest(versioned)
    legacy_manifest["state"].pop("shadow_operational_health_database")
    _rewrite_member(
        versioned,
        legacy,
        backup.MANIFEST_NAME,
        json.dumps(legacy_manifest, sort_keys=True).encode("utf-8"),
    )

    target = tmp_path / "legacy-target"
    restore.restore_backup(legacy, state_dir=target)
    with sqlite3.connect(target / "events.db") as connection:
        assert connection.execute("SELECT value FROM records").fetchone() == (
            "durable",
        )
    assert not (target / backup.SHADOW_OPERATIONAL_DATABASE_NAME).exists()


def test_restore_refuses_existing_state_then_replaces_it_with_explicit_force(tmp_path):
    source = _create_state(tmp_path / "source")
    archive = backup.create_backup(tmp_path / "snapshot.zip", state_dir=source)
    target = tmp_path / "target"
    target.mkdir()
    old_config = target / "config.json"
    old_config.write_text('{"old":true}', encoding="utf-8")
    stale_wal = target / "events.db-wal"
    stale_wal.write_bytes(b"stale sqlite sidecar")
    stale_shadow_wal = target / "capability-shadow-health.db-wal"
    stale_shadow_wal.write_bytes(b"stale shadow sqlite sidecar")
    (target / "unrelated.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(restore.RestoreError, match="--force"):
        restore.restore_backup(archive, state_dir=target)
    assert json.loads(old_config.read_text(encoding="utf-8")) == {"old": True}

    restored = restore.restore_backup(archive, state_dir=target, force=True)

    assert restored == target.resolve()
    assert json.loads(old_config.read_text(encoding="utf-8"))["viewer_key"] == "viewer-super-secret"
    assert (target / "projects" / "alpha.json").is_file()
    assert (target / "output" / "answer.md").read_text(encoding="utf-8") == "answer"
    assert (target / "execution_artifacts" / "execution-a" / "result.txt").is_file()
    assert (target / "unrelated.txt").read_text(encoding="utf-8") == "keep me"
    assert not stale_wal.exists()
    assert not stale_shadow_wal.exists()
    with sqlite3.connect(target / "events.db") as connection:
        assert connection.execute("SELECT value FROM records").fetchone() == ("durable",)
    with sqlite3.connect(
        target / backup.SHADOW_OPERATIONAL_DATABASE_NAME
    ) as connection:
        assert connection.execute(
            "SELECT outcome FROM shadow_health"
        ).fetchone() == ("completed",)


def test_checksum_failure_is_rejected_before_existing_state_is_touched(tmp_path):
    source = _create_state(tmp_path / "source")
    archive = backup.create_backup(tmp_path / "snapshot.zip", state_dir=source)
    corrupt = tmp_path / "corrupt.zip"
    _rewrite_member(
        archive,
        corrupt,
        "state/config.json",
        b'{"viewer_key":"tampered-but-valid-json"}',
    )
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "config.json"
    sentinel.write_text('{"sentinel":true}', encoding="utf-8")

    with pytest.raises(restore.RestoreError, match="size|checksum"):
        restore.restore_backup(corrupt, state_dir=target, force=True)

    assert json.loads(sentinel.read_text(encoding="utf-8")) == {"sentinel": True}


def test_restore_rolls_back_existing_state_if_an_install_rename_fails(tmp_path, monkeypatch):
    source = _create_state(tmp_path / "source")
    archive = backup.create_backup(tmp_path / "snapshot.zip", state_dir=source)
    target = tmp_path / "target"
    target.mkdir()
    old_config = target / "config.json"
    old_config.write_text('{"old":true}', encoding="utf-8")
    old_output = target / "output"
    old_output.mkdir()
    (old_output / "old.txt").write_text("old output", encoding="utf-8")

    real_replace = restore.os.replace

    def fail_on_staged_output(source_path, destination_path):
        source_value = Path(source_path)
        if source_value.name == "output" and ".mycelium-restore-stage-" in str(source_value):
            raise OSError("simulated install failure")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(restore.os, "replace", fail_on_staged_output)

    with pytest.raises(restore.RestoreError, match="previous state was restored"):
        restore.restore_backup(archive, state_dir=target, force=True)

    assert json.loads(old_config.read_text(encoding="utf-8")) == {"old": True}
    assert (old_output / "old.txt").read_text(encoding="utf-8") == "old output"
    assert not (target / "events.db").exists()


@pytest.mark.parametrize("attack", ["traversal", "encoded_traversal", "symlink"])
def test_restore_rejects_traversal_and_symlink_members(tmp_path, attack):
    state = _create_state(tmp_path / "source")
    archive = backup.create_backup(tmp_path / "snapshot.zip", state_dir=state)
    malicious = tmp_path / f"{attack}.zip"
    malicious.write_bytes(archive.read_bytes())

    with zipfile.ZipFile(malicious, "a") as bundle:
        if attack == "traversal":
            bundle.writestr("../outside.txt", "escape")
        elif attack == "encoded_traversal":
            bundle.writestr("state/output/%252e%252e%252foutside.txt", "escape")
        else:
            info = zipfile.ZipInfo("state/output/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(info, "../../outside.txt")

    target = tmp_path / "restored"
    with pytest.raises(restore.RestoreError, match="traversal|symlink|encoding"):
        restore.restore_backup(malicious, state_dir=target)
    assert not target.exists()
    assert not (tmp_path / "outside.txt").exists()


def test_restore_cli_prints_post_restore_preflight(tmp_path, capsys):
    state = _create_state(tmp_path / "source")
    archive = backup.create_backup(tmp_path / "snapshot.zip", state_dir=state)
    target = tmp_path / "restored"

    assert restore.main([str(archive), "--state-dir", str(target)]) == 0

    output = capsys.readouterr()
    assert "python scripts/preflight.py" in output.out
    assert "process-local" in output.out.lower()
    assert output.err == ""
