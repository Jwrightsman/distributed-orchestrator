"""The history scan has to find a planted secret, and ignore what isn't one.

A scanner that reports nothing is indistinguishable from a scanner that is
broken, so the first test here plants a real generated credential in a real
git repository's history, deletes it in a later commit, and asserts the scan
still finds it. The rest are the false positives that an earlier version of
this rule actually produced against this repository -- they are kept because
each one made the scan useless in a different way, and precision is what
decides whether an operator keeps running it.

The last section covers orphaned blobs -- the ones `git add` leaves behind when
the commit is amended away. No tree names them, so the path-based ignore rules
had nothing to match on and a test fixture came back as a credential. They are
also the one place where a *recovered* path suppresses a finding, so the tests
that matter most there are the ones asserting it does not.
"""

from __future__ import annotations

import base64
import random
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.secret_history_scan as scanner  # noqa: E402
from scripts.secret_history_scan import (  # noqa: E402
    blob_index,
    candidate_blobs,
    entropy_per_character,
    looks_random,
    main,
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


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


@pytest.fixture
def throwaway_repo(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / "repo")


def _commit(repo: Path, name: str, body: str, message: str) -> None:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
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
        # Real names from this repository, digit-free and mixed-case, so the
        # word test is the only thing standing between them and a report.
        "SSLCertVerificationError",  # an acronym running into a word
        "TestEmptyExtractionIsNotAPass",  # the shortest words measured here
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


def _token_urlsafe_from(generator: random.Random) -> str:
    """What `secrets.token_urlsafe(32)` computes, from a seeded generator.

    The same 32 bytes, the same base64url encoding, the same 43 characters
    out; only the source of entropy differs, so a sample drawn through here
    is the same sample on every run.

    The version of this file that drew from `secrets` instead asserted that
    all of 200 random draws pass a heuristic, which is a probabilistic claim
    wearing a deterministic one's clothes. It failed about one run in nine.
    """

    return base64.urlsafe_b64encode(generator.randbytes(32)).rstrip(b"=").decode(
        "ascii"
    )


#: Real `secrets.token_urlsafe(32)` values that happen to contain no digit.
#: About one credential in 1,600 looks like this, and every one of them was
#: missed until the digit requirement came out of `looks_random` -- a false
#: negative in a security tool, which is the expensive direction to be wrong
#: in. They are frozen here because the shape is what matters, not the draw.
CREDENTIALS_WITHOUT_A_DIGIT = (
    "cQXNADiGYacckPZPUNKHCNkDRTaoCL_BgogJoYxGD-E",
    "CnTzVciGBRfrKtVeJBBTSudWVnDkQyYRUBrNSADiqEM",
    "wgNnZxb-ehkMwgCXRjDjvSsJxgvDTlkiALYq-JaczPI",
    "ZmEWcTJdDKNjPJbBXpmU-leSTF-NpwdPGBbYSySoHEM",
    "WPgwC-RLtEDdlhTQcDmeLbsNN-lFTNLSGEZtlBYxtnw",
    "rTDEXqcuNSlFfoIXBRXi-inTKqZoYOgALXUJxLoZFZo",
)


@pytest.mark.parametrize("value", CREDENTIALS_WITHOUT_A_DIGIT)
def test_a_credential_with_no_digit_in_it_is_still_a_credential(value):
    """The hole the digit requirement left open, pinned shut."""

    assert len(value) == 43
    assert not any(character.isdigit() for character in value)
    assert looks_random(value) is True


def test_the_generator_this_project_issues_credentials_with_is_not_missed():
    """20,000 credentials of exactly the shape this project hands out.

    Fixed, so it passes every run or fails every run -- never one in nine.
    The measured miss rate behind this is about one in a million, taken over
    300,000 digit-free tokens rather than over this sample, which is too
    small to measure a rate that low and is here to catch a regression.
    """

    generator = random.Random(20260906)
    tokens = [_token_urlsafe_from(generator) for _ in range(20_000)]

    assert [token for token in tokens if not looks_random(token)] == []


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


# -- Blobs no commit names ----------------------------------------------------
#
# `git add` writes the blob there and then. Amend the commit, or stage the file
# twice, and the first blob stays in the object database with no tree naming
# it. The scan reported two of those as credentials against this repository:
# they were the weak-secret fixtures in tests/test_deploy_preflight.py, drafted
# during a commit that was later amended, and `^tests/` never got to fire
# because the finding's path was "(unreachable blob)".


def _git_out(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _fixture_module(secret: str, tail: str = "") -> str:
    """A test file shaped like the one that actually caused this."""

    return (
        '''"""The preflight has to report a weak secret without printing it."""

from __future__ import annotations

import json

from scripts.deploy_preflight import run_host_preflight


def test_a_weak_secret_is_reported_without_showing_it(tmp_path):
    configuration = tmp_path / "config.json"
    configuration.write_text(json.dumps({"node_secret": "'''
        + secret
        + '''"}))

    report = run_host_preflight(state_dir=tmp_path, config_path=configuration)

    assert report.failed, "a weak node secret has to fail the preflight"
'''
        + tail
    )


def _orphan_a_draft(repo: Path, name: str, draft: str, final: str) -> str:
    """Stage `draft` at `name`, replace it with `final`, commit only `final`.

    Returns the draft's blob, which is now in the object database with nothing
    naming it: no tree ever held it, the index has moved on, and no reflog
    entry points at a commit that contained it.
    """

    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(draft, encoding="utf-8")
    _git(repo, "add", name)
    orphan = _git_out(repo, "rev-parse", f":{name}")
    (repo / name).write_text(final, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"commit only the final {name}")
    return orphan


def _assert_orphaned(repo: Path, blob: str) -> None:
    """Guard against the test passing because nothing was planted."""

    located = blob_index(repo)
    assert blob in candidate_blobs(repo), "the draft blob is not in the database"
    assert blob not in located.paths, "git still names it; this is not an orphan"
    assert blob not in located.in_history


def test_an_orphaned_draft_of_a_test_fixture_is_not_reported(throwaway_repo: Path):
    """The bug: a fixture credential, orphaned, reported as a real one.

    The blob is a draft of a file under tests/, so `^tests/` should silence it
    exactly as it silences the committed copy. Before the path was recovered
    there was nothing for that rule to match.
    """

    _commit(throwaway_repo, "README.md", "# a repository\n", "first")
    fixture = _fixture_module(secrets.token_urlsafe(32))
    orphan = _orphan_a_draft(
        throwaway_repo,
        "tests/test_deploy_preflight.py",
        fixture,
        fixture + "\n\ndef test_the_preflight_also_checks_the_ssh_configuration():\n    pass\n",
    )

    _assert_orphaned(throwaway_repo, orphan)

    assert scan_repository(throwaway_repo) == []


def test_an_orphaned_draft_of_a_scanned_file_is_still_reported(throwaway_repo: Path):
    """Recovering a path must not become a way of losing findings.

    Same mechanism, but the recovered path is one the scan reads. The credential
    has to survive, and it has to be marked as the local-only thing it is.
    """

    _commit(throwaway_repo, "README.md", "# a repository\n", "first")
    leaked = secrets.token_urlsafe(32)
    draft = _fixture_module(leaked)
    orphan = _orphan_a_draft(
        throwaway_repo,
        "deploy/configure.py",
        draft,
        # What got committed is the placeholder, so the only credential in this
        # repository is the one in the orphan. Anything reported is that blob.
        draft.replace(leaked, "changeme-before-committing"),
    )

    _assert_orphaned(throwaway_repo, orphan)
    findings = scan_repository(throwaway_repo)

    assert [finding.path for finding in findings] == ["deploy/configure.py"]
    assert not findings[0].reachable, "no ref reaches it, so it is local only"
    assert all(leaked not in f"{finding.path} {finding.note}" for finding in findings)


def test_an_orphan_resembling_nothing_keeps_its_finding(throwaway_repo: Path):
    """No path recoverable is the case the ignore rules cannot help with."""

    _commit(throwaway_repo, "README.md", "# a repository\n", "first")
    leaked = secrets.token_urlsafe(32)
    orphan = _orphan_a_draft(
        throwaway_repo,
        "notes.md",
        '{\n  "node_secret": "' + leaked + '"\n}\n',
        "# notes\n\nNothing in this file resembles the draft it replaced.\n",
    )

    _assert_orphaned(throwaway_repo, orphan)
    findings = scan_repository(throwaway_repo)

    assert [finding.path for finding in findings] == ["(unreachable blob)"]
    assert all(not finding.reachable for finding in findings)


def _plant_ambiguity(repo: Path, leaked: str, *, rival: bool) -> str:
    """A credential-bearing orphan, and one or two files it resembles.

    The committed copies hold a placeholder, so the only real credential in the
    repository is in the orphan and any finding is that blob. With `rival` the
    orphan resembles a scanned file exactly as much as the ignored one.
    """

    base = _fixture_module("changeme-before-committing")
    _commit(repo, "README.md", "# a repository\n", "first")
    _commit(
        repo,
        "tests/test_preflight.py",
        base + "\n\ndef test_only_the_test_copy_has_this_line():\n    pass\n",
        "the copy under tests/",
    )
    if rival:
        _commit(
            repo,
            "deploy/preflight.py",
            base + "\n\ndef only_the_deploy_copy_has_this_line():\n    return None\n",
            "the copy that gets scanned",
        )
    return _orphan_a_draft(
        repo,
        "notes/scratch.py",
        base.replace("changeme-before-committing", leaked),
        "# scratch\n\nnothing here resembles the draft it replaced\n",
    )


def test_an_orphan_one_file_matches_inherits_that_path(throwaway_repo: Path):
    """The control for the test below: one clear match, and it is adopted.

    Without this, the next test would pass just as well if the similarity were
    too low to match anything -- which is a different reason for the same
    answer, and would leave the ambiguity rule untested.
    """

    orphan = _plant_ambiguity(throwaway_repo, secrets.token_urlsafe(32), rival=False)

    _assert_orphaned(throwaway_repo, orphan)

    assert scan_repository(throwaway_repo) == []


def test_an_orphan_two_files_match_equally_is_not_silenced(
    tmp_path: Path,
):
    """Ambiguity resolves towards reporting, never towards an ignored path.

    Same orphan as the control, and now it resembles a scanned file exactly as
    much as the ignored one. Adopting the ignored path on a coin toss would
    suppress a real credential, so a match this close adopts neither.
    """

    repo = _init_repo(tmp_path / "contested")
    leaked = secrets.token_urlsafe(32)
    orphan = _plant_ambiguity(repo, leaked, rival=True)

    _assert_orphaned(repo, orphan)
    findings = scan_repository(repo)

    assert [finding.path for finding in findings] == ["(unreachable blob)"]
    assert all(leaked not in f"{finding.path} {finding.note}" for finding in findings)


def test_a_reset_away_commit_is_named_by_the_reflog(throwaway_repo: Path):
    """When git still records the path, use it rather than guessing.

    Nothing in this repository resembles the discarded file, so content
    matching has nothing to work with. The reflog still points at the commit
    that held it, and that names the path outright.
    """

    _commit(throwaway_repo, "README.md", "# a repository\n", "first")
    _commit(
        throwaway_repo,
        "tests/test_credentials.py",
        _fixture_module(secrets.token_urlsafe(32)),
        "a test that was thought better of",
    )
    blob = _git_out(throwaway_repo, "rev-parse", "HEAD:tests/test_credentials.py")
    _git(throwaway_repo, "reset", "--hard", "HEAD~1")

    located = blob_index(throwaway_repo)

    assert located.paths[blob] == "tests/test_credentials.py"
    assert blob not in located.in_history, "a reflog is not pushed and not cloned"
    assert scan_repository(throwaway_repo) == []


def test_a_staged_credential_is_named_and_counted_as_travelling(
    throwaway_repo: Path,
):
    """The index names its blobs, and they are one commit from the remote."""

    _commit(throwaway_repo, "README.md", "# a repository\n", "first")
    leaked = secrets.token_urlsafe(32)
    (throwaway_repo / "config.json").write_text(
        '{\n  "node_secret": "' + leaked + '"\n}\n', encoding="utf-8"
    )
    _git(throwaway_repo, "add", "config.json")

    findings = scan_repository(throwaway_repo)

    assert [finding.path for finding in findings] == ["config.json"]
    assert findings[0].reachable


# -- Severity ------------------------------------------------------------------


def test_only_a_credential_that_travels_fails_the_scan(throwaway_repo: Path):
    """An unreachable object is reported, and does not fail the run.

    A fresh clone does not have these objects at all, so failing over them
    makes the result depend on which machine ran it. It is still printed.
    """

    _commit(throwaway_repo, "README.md", "# a repository\n", "first")
    orphan = _orphan_a_draft(
        throwaway_repo,
        "notes.md",
        '{\n  "node_secret": "' + secrets.token_urlsafe(32) + '"\n}\n',
        "# notes\n\nNothing in this file resembles the draft it replaced.\n",
    )

    _assert_orphaned(throwaway_repo, orphan)

    assert scan_repository(throwaway_repo), "still reported"
    assert main(["--repo", str(throwaway_repo)]) == 0


def test_a_committed_credential_still_fails_the_scan(throwaway_repo: Path):
    """The counterpart: reachable is what the exit status is for."""

    _commit(
        throwaway_repo,
        "config.json",
        '{\n  "node_secret": "' + secrets.token_urlsafe(32) + '"\n}\n',
        "oops",
    )

    findings = scan_repository(throwaway_repo)

    assert all(finding.reachable for finding in findings)
    assert main(["--repo", str(throwaway_repo)]) == 1


def test_giving_up_on_attribution_still_reports_the_blob(
    throwaway_repo: Path, monkeypatch
):
    """The cap on how many orphans to name degrades towards reporting.

    Nothing reaches this cap in a repository anyone has run `git gc` on, so the
    branch would otherwise never execute. What it must not do is drop findings:
    an orphan it declines to name is reported without a path, not skipped.
    """

    monkeypatch.setattr(scanner, "MAX_ATTRIBUTED_BLOBS", 0)
    _commit(throwaway_repo, "README.md", "# a repository\n", "first")
    fixture = _fixture_module(secrets.token_urlsafe(32))
    orphan = _orphan_a_draft(
        throwaway_repo,
        "tests/test_deploy_preflight.py",
        fixture,
        fixture + "\n\ndef test_the_preflight_reads_the_ssh_configuration():\n    pass\n",
    )

    _assert_orphaned(throwaway_repo, orphan)
    findings = scan_repository(throwaway_repo)

    assert [finding.path for finding in findings] == ["(unreachable blob)"]
    assert all(not finding.reachable for finding in findings)
