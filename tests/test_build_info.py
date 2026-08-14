"""The deploy fingerprint has to actually change when the code changes.

A verification signal that never changes is worse than none: it reports
success on a deploy that did nothing, which is the exact failure it exists to
catch. These tests check the three properties the deploy check depends on —
stable for the same input, different for different input, and agnostic to the
line endings that differ between a Windows checkout and the Linux image.
"""

import hashlib

import pytest
from fastapi.testclient import TestClient

import build_info
import server


@pytest.fixture
def client():
    return TestClient(server.app)


def test_fingerprint_is_stable_across_calls():
    assert build_info.fingerprint() == build_info.fingerprint()


def test_fingerprint_covers_the_files_the_image_ships():
    names = {p.name for p in build_info._source_files()}
    # A change to any of these must move the fingerprint, or the check is blind
    # to exactly the deploys people care about.
    for required in ("server.py", "routes_nodes.py", "routes_pitch.py", "server_state.py"):
        assert required in names, f"{required} is in the image but not fingerprinted"


def test_fingerprint_changes_when_a_source_file_changes(tmp_path, monkeypatch):
    """The property the whole check rests on."""
    (tmp_path / "server.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(build_info, "_APP_DIR", tmp_path)
    before = build_info.fingerprint()

    (tmp_path / "server.py").write_text("x = 2\n", encoding="utf-8")
    after = build_info.fingerprint()

    assert before != after


def test_line_endings_do_not_change_the_fingerprint(tmp_path, monkeypatch):
    """A CRLF checkout and an LF image must agree, or the check cries wolf."""
    monkeypatch.setattr(build_info, "_APP_DIR", tmp_path)

    (tmp_path / "server.py").write_bytes(b"x = 1\ny = 2\n")
    unix = build_info.fingerprint()

    (tmp_path / "server.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    windows = build_info.fingerprint()

    assert unix == windows


def test_a_renamed_file_changes_the_fingerprint(tmp_path, monkeypatch):
    """Paths are hashed too — identical bytes under a new name is a new build."""
    monkeypatch.setattr(build_info, "_APP_DIR", tmp_path)
    (tmp_path / "server.py").write_text("x = 1\n", encoding="utf-8")
    before = build_info.fingerprint()

    (tmp_path / "server.py").unlink()
    (tmp_path / "other.py").write_text("x = 1\n", encoding="utf-8")
    assert build_info.fingerprint() != before


def test_status_json_publishes_the_build(client):
    """This is what scripts/verify_deploy.py reads."""
    body = client.get("/status.json").json()
    assert body["build"] == build_info.BUILD
    assert len(body["build"]) == 12
    assert body["build"] != hashlib.sha256(b"").hexdigest()[:12]


def test_build_is_safe_to_publish(client):
    """/status.json is designed to be pasted in public — no paths in it."""
    build = client.get("/status.json").json()["build"]
    assert build.isalnum()
    for leak in ("/", "\\", "Users", "root", "home"):
        assert leak not in build
