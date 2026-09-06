"""The history scan has to find a planted secret, and ignore what isn't one.

A scanner that reports nothing is indistinguishable from a scanner that is
broken, so the first test here plants a real generated credential in a real
git repository's history, deletes it in a later commit, and asserts the scan
still finds it. The rest are the false positives that an earlier version of
this rule actually produced against this repository -- they are kept because
each one made the scan useless in a different way, and precision is what
decides whether an operator keeps running it.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.secret_history_scan import (  # noqa: E402
    entropy_per_character,
    looks_random,
    scan_repository,
    scan_text,
)


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def throwaway_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, name: str, body: str, message: str) -> None:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)


# -- It finds what is actually there -----------------------------------------


def test_a_committed_authority_is_found_after_it_is_deleted(throwaway_repo: Path):
    """Deleting the line in a later commit does not remove the blob."""

    leaked = secrets.token_urlsafe(32)
    _commit(
        throwaway_repo,
        "config.json",
        '{\n  "node_secret": "' + leaked + '"\n}\n',
        "oops",
    )
    _commit(throwaway_repo, "config.json", '{\n  "node_secret": ""\n}\n', "remove it")

    findings = scan_repository(throwaway_repo)

    assert findings, "the deleted blob is still in the object database"
    assert any(finding.rule == "mycelium_authority" for finding in findings)
    assert all(leaked not in finding.note for finding in findings)
    assert all(leaked not in finding.path for finding in findings)


def test_the_working_tree_being_clean_is_not_the_question(throwaway_repo: Path):
    """The file is gone entirely and the credential is still findable."""

    leaked = secrets.token_urlsafe(32)
    _commit(throwaway_repo, "secrets.env", f"NODE_SECRET={leaked}\n", "add")
    _git(throwaway_repo, "rm", "secrets.env")
    _git(throwaway_repo, "commit", "-m", "delete the file")

    assert not (throwaway_repo / "secrets.env").exists()
    assert scan_repository(throwaway_repo)


def test_a_private_key_block_is_found(throwaway_repo: Path):
    _commit(
        throwaway_repo,
        "id_rsa",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----\n",
        "add",
    )

    assert any(
        finding.rule == "private_key_block" for finding in scan_repository(throwaway_repo)
    )


def test_a_clean_repository_reports_nothing(throwaway_repo: Path):
    _commit(throwaway_repo, "README.md", "# nothing to see\n", "add")

    assert scan_repository(throwaway_repo) == []


# -- It ignores what is not ---------------------------------------------------
#
# Each of these was reported by a real earlier version of the rule, against
# this repository's own history.


@pytest.mark.parametrize(
    "line",
    [
        # `keyHandlers` supplied the word "key"; a worktree name supplied the
        # entropy. This was ten hits on one file of benchmark results.
        '{"detail": {"canvas": true, "keyHandlers": false, '
        '"html": "worktrees/aug-2026-sprint-execution-6d9ed6/index.html"}}',
        # An identifier on the right of an assignment is not a value.
        "            credential_version=normalize_credential_version(raw_version),",
        "                subject_key=DEADLINE_COMPLETION_SUBJECT,",
        # Illustrative values in documentation.
        '  "session_token": "plaintext-returned-only-to-the-worker",',
        '  "node_secret": "<independent-random-node-authority-at-least-32-chars>",',
        '  "viewer_key": "your-viewer-key-here",',
        '  "pitch_key": "changeme-please",',
    ],
)
def test_known_false_positives_stay_quiet(line):
    assert list(scan_text(line, "some/path.py", "abc123")) == []


@pytest.mark.parametrize(
    "line",
    [
        '  "node_secret": "{value}"',
        "NODE_SECRET={value}",
        'api_key = "{value}"',
        '  "enrollment_credential": "{value}"',
    ],
)
def test_a_real_generated_credential_is_reported_in_every_spelling(line):
    findings = list(
        scan_text(line.format(value=secrets.token_urlsafe(32)), "config.json", "abc123")
    )

    assert findings, f"missed a credential in: {line}"


# -- The shape discriminator --------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "normalize_credential_version",
        "DEADLINE_COMPLETION_SUBJECT",
        "plaintext-returned-only-to-the-worker",
        "aug-2026-sprint-execution-6d9ed6",  # digits, but no case mixing
        "supported_worker_protocol_versions",
    ],
)
def test_words_do_not_look_random(value):
    assert looks_random(value) is False


@pytest.mark.parametrize(
    "value",
    [
        "a3f5c8e19b7d4a2f6c0e8b1d3a5f7c9e",  # hex
        "Xk3Jq9Lm2Pv5Rt8Wy1Zb4Nc7Df0Gh6Js",  # mixed case and digits
        "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo=",  # base64
    ],
)
def test_generated_tokens_look_random(value):
    assert looks_random(value) is True


def test_generated_credentials_always_look_random():
    """The generator this project actually uses, sampled rather than argued."""

    assert all(looks_random(secrets.token_urlsafe(32)) for _ in range(200))


def test_entropy_separates_prose_from_a_token():
    prose = entropy_per_character("the quick brown fox jumps over the lazy dog")
    token = entropy_per_character(secrets.token_urlsafe(32))

    assert token > prose


# -- It never prints a value --------------------------------------------------


def test_no_finding_carries_the_secret_it_found(throwaway_repo: Path):
    leaked = secrets.token_urlsafe(32)
    _commit(throwaway_repo, "config.json", f'{{"node_secret": "{leaked}"}}', "add")

    findings = scan_repository(throwaway_repo)
    rendered = " ".join(
        f"{f.rule} {f.path} {f.line} {f.blob} {f.note}" for f in findings
    )

    assert findings
    assert leaked not in rendered
