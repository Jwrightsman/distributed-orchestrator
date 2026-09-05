"""An invitation code must have a way in that is not an argument vector.

`worker_installer.py` has never accepted one on the command line. `join.py` and
`node.py` did, and only that: `--secret VALUE`, which puts the code where every
other user on the machine can read it with `ps` and where the shell writes it to
a history file. Both were reported as open exposures and both are closed here —
not by removing the argument, which would break setups that already script it,
but by putting two safe doors beside it and making the unsafe one say so.

These tests hold three things:

* the safe doors work, and the code they carry reaches registration;
* the unsafe door still works, and warns in words that name the exposure;
* neither safe door ever puts the code in `sys.argv`, prints it, or writes it.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

import pytest

import join
import node
import worker_secret
from worker_secret import (
    AdmissionSecretError,
    add_admission_secret_arguments,
    resolve_admission_secret,
    resolve_from_args,
)


SECRET = "invitation-code-4Kq9ZfWn2LbTx7Rv"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="worker")
    add_admission_secret_arguments(parser)
    return parser


def _owner_only(path: Path, text: str = SECRET) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


# ── The file door ────────────────────────────────────────────────────────────


def test_a_secret_file_only_you_can_read_is_accepted(tmp_path):
    secret_file = _owner_only(tmp_path / "code.txt")
    assert resolve_admission_secret(secret_file=secret_file) == SECRET


def test_surrounding_whitespace_is_not_part_of_the_code(tmp_path):
    """People press Return at the end of a file. That is not a typo."""

    secret_file = _owner_only(tmp_path / "code.txt", f"  {SECRET}\n\n")
    assert resolve_admission_secret(secret_file=secret_file) == SECRET


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_secret_file_others_can_read_is_refused(tmp_path):
    """A credential in a world-readable file is a credential already disclosed."""

    secret_file = tmp_path / "code.txt"
    secret_file.write_text(SECRET, encoding="utf-8")
    secret_file.chmod(0o644)

    with pytest.raises(AdmissionSecretError) as raised:
        resolve_admission_secret(secret_file=secret_file)
    assert "chmod 600" in str(raised.value)
    assert SECRET not in str(raised.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
def test_a_secret_file_that_is_a_symlink_is_refused(tmp_path):
    real = _owner_only(tmp_path / "code.txt")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    with pytest.raises(AdmissionSecretError) as raised:
        resolve_admission_secret(secret_file=link)
    assert "symbolic link" in str(raised.value)


def test_a_missing_secret_file_says_so_rather_than_joining_without_one(tmp_path):
    with pytest.raises(AdmissionSecretError) as raised:
        resolve_admission_secret(secret_file=tmp_path / "absent.txt")
    assert "does not exist" in str(raised.value)


def test_an_empty_secret_file_is_refused(tmp_path):
    secret_file = _owner_only(tmp_path / "code.txt", "\n  \n")
    with pytest.raises(AdmissionSecretError) as raised:
        resolve_admission_secret(secret_file=secret_file)
    assert "empty" in str(raised.value)


def test_the_file_check_is_the_identity_files_check_not_a_second_one():
    """Two copies of a permission check is how one of them ends up laxer."""

    import inspect

    import worker_identity

    source = inspect.getsource(worker_secret)
    assert "read_owner_only_text" in source
    for reimplemented in ("S_IMODE", "0o077", "geteuid", "O_NOFOLLOW"):
        assert reimplemented not in source, (
            f"worker_secret re-implements {reimplemented}; it must reuse "
            "worker_identity.read_owner_only_text"
        )
    assert callable(worker_identity.read_owner_only_text)


# ── The prompt door ──────────────────────────────────────────────────────────


def test_ask_secret_reads_it_with_the_echo_off(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(worker_secret.getpass, "getpass", lambda *a, **k: f" {SECRET} ")

    assert resolve_admission_secret(ask=True) == SECRET

    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_ask_secret_uses_getpass_rather_than_input(monkeypatch):
    """`input` echoes. That is the whole difference."""

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda *a, **k: pytest.fail("the code was echoed to the screen")
    )
    monkeypatch.setattr(worker_secret.getpass, "getpass", lambda *a, **k: SECRET)
    assert resolve_admission_secret(ask=True) == SECRET


def test_ask_secret_with_nobody_at_the_keyboard_refuses(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        worker_secret.getpass,
        "getpass",
        lambda *a, **k: pytest.fail("prompted with no terminal"),
    )
    with pytest.raises(AdmissionSecretError) as raised:
        resolve_admission_secret(ask=True)
    assert "--secret-file" in str(raised.value), "must say what to do instead"


def test_an_empty_answer_at_the_prompt_is_refused(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(worker_secret.getpass, "getpass", lambda *a, **k: "   ")
    with pytest.raises(AdmissionSecretError):
        resolve_admission_secret(ask=True)


def test_interrupting_the_prompt_is_not_read_as_an_empty_code(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _interrupt(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(worker_secret.getpass, "getpass", _interrupt)
    with pytest.raises(AdmissionSecretError) as raised:
        resolve_admission_secret(ask=True)
    assert "cancelled" in str(raised.value).lower()


# ── The unsafe door, kept but no longer silent ───────────────────────────────


def test_secret_on_the_command_line_still_works(capsys):
    """Deleting it would break setups that already script it."""

    assert resolve_admission_secret(secret=SECRET) == SECRET


def test_secret_on_the_command_line_warns_and_names_the_exposure(capsys):
    """THE ONE THAT MATTERS. 'Insecure' means nothing; 'ps' and history do."""

    resolve_admission_secret(secret=SECRET)
    warning = " ".join(capsys.readouterr().err.split())

    assert "'ps'" in warning, "must name the command that reads it"
    assert "history" in warning, "must say the shell wrote it down"
    assert "--secret-file" in warning, "must point at the alternatives"
    assert "--ask-secret" in warning
    assert SECRET not in warning, "the warning must not repeat the code"


def test_the_warning_goes_to_stderr_so_a_redirected_log_still_shows_it(capsys):
    resolve_admission_secret(secret=SECRET)
    captured = capsys.readouterr()
    assert "--secret" in captured.err
    assert captured.out == ""


def test_a_caller_that_already_warned_does_not_warn_twice(capsys):
    resolve_admission_secret(secret=SECRET, warn=False)
    assert capsys.readouterr().err == ""


def test_the_help_text_says_it_is_visible_to_other_users():
    help_text = _parser().format_help().lower()
    assert "visible to other users on this machine" in " ".join(help_text.split())
    assert "history" in help_text


# ── No source, and the shape of the CLI ──────────────────────────────────────


def test_no_source_means_no_secret_and_no_questions(monkeypatch, capsys):
    """A returning worker needs no code at all, and must not be asked for one."""

    monkeypatch.setattr(
        worker_secret.getpass,
        "getpass",
        lambda *a, **k: pytest.fail("asked for a code that was not needed"),
    )
    assert resolve_admission_secret() == ""
    assert capsys.readouterr().err == ""


def test_the_three_doors_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit):
        _parser().parse_args(["--secret", SECRET, "--ask-secret"])
    with pytest.raises(SystemExit):
        _parser().parse_args(["--secret-file", "x", "--ask-secret"])


@pytest.mark.parametrize("module", [join, node])
def test_both_worker_entry_points_offer_all_three_doors(module):
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "add_admission_secret_arguments(parser)" in source, (
        f"{module.__name__} declares its own secret options instead of sharing one"
    )


@pytest.mark.parametrize("module", [join, node, worker_secret])
def test_no_worker_entry_point_reads_anything_out_of_the_environment(module):
    """An environment variable is inherited by every child process spawned.

    `install.ps1` took the invitation code from `$env:SWARM_SECRET`, which put
    it in the environment of everything the installer went on to start. That
    script is deleted; this keeps the pattern from coming back through the
    Python side, where an `ollama pull` child would inherit it just the same.
    """

    source = Path(module.__file__).read_text(encoding="utf-8").lower()
    for spelling in ("os.environ", "getenv", "putenv", "swarm_secret"):
        assert spelling not in source, (
            f"{module.__name__} touches the environment ({spelling}); a secret "
            "read from or written to it is inherited by every child process"
        )


def test_resolve_from_args_reads_the_namespace_the_adder_builds(tmp_path):
    secret_file = _owner_only(tmp_path / "code.txt")
    args = _parser().parse_args(["--secret-file", str(secret_file)])
    assert resolve_from_args(args) == SECRET

    args = _parser().parse_args([])
    assert resolve_from_args(args) == ""


# ── It reaches registration, and nowhere else ────────────────────────────────


@pytest.mark.asyncio
async def test_a_secret_file_supplies_the_bootstrap_registration(monkeypatch, tmp_path):
    """End to end through node.main: file in, one registration header out."""

    secret_file = _owner_only(tmp_path / "code.txt")
    seen: list[str] = []

    async def healthy_ollama():
        return {"ok": True, "models": [node.DEFAULT_MODEL]}

    async def no_runtime_metadata(*_args, **_kwargs):
        return None, None, None

    async def register(server, node_id, secret="", **kwargs):
        seen.append(secret)
        raise KeyboardInterrupt

    monkeypatch.setattr(node, "check_ollama", healthy_ollama)
    monkeypatch.setattr(node, "_detect_ollama_metadata", no_runtime_metadata)
    monkeypatch.setattr(node, "register", register)
    monkeypatch.setattr(node.console, "print", lambda *a, **k: None)

    with pytest.raises(KeyboardInterrupt):
        await node.main(
            [
                "--server",
                "https://coordinator.example",
                "--node-id",
                "laptop",
                "--identity-file",
                str(tmp_path / "identity.json"),
                "--secret-file",
                str(secret_file),
            ]
        )

    assert seen == [SECRET]


@pytest.mark.asyncio
async def test_an_unreadable_secret_file_stops_before_any_registration(
    monkeypatch, tmp_path, capsys
):
    async def healthy_ollama():
        return {"ok": True, "models": [node.DEFAULT_MODEL]}

    async def register(*_a, **_k):
        pytest.fail("registered without a usable admission secret")

    monkeypatch.setattr(node, "check_ollama", healthy_ollama)
    monkeypatch.setattr(node, "register", register)

    await node.main(
        [
            "--server",
            "https://coordinator.example",
            "--identity-file",
            str(tmp_path / "identity.json"),
            "--secret-file",
            str(tmp_path / "absent.txt"),
        ]
    )
    assert "admission secret" in capsys.readouterr().out.lower()
    assert not (tmp_path / "identity.json").exists(), (
        "a credential must not be written before the code could even be read"
    )


@pytest.mark.asyncio
async def test_join_hands_the_code_to_node_without_it_touching_argv(
    monkeypatch, tmp_path
):
    """join.py used to rewrite sys.argv. Now it passes a function argument."""

    secret_file = _owner_only(tmp_path / "code.txt")
    handed: list[object] = []
    argv_before = list(sys.argv)

    async def fake_node_main(argv=None, *, admission_secret=None):
        handed.append((list(argv or []), admission_secret, list(sys.argv)))

    async def ready():
        return True

    monkeypatch.setattr(join, "ensure_ollama", ready)
    monkeypatch.setattr(join, "confirm_consent", lambda *a, **k: True)
    monkeypatch.setitem(sys.modules, "node", node)
    monkeypatch.setattr(node, "main", fake_node_main)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "join.py",
            "https://coordinator.example",
            "--secret-file",
            str(secret_file),
        ],
    )

    await join.main()

    assert len(handed) == 1
    passed_argv, admission_secret, live_argv = handed[0]
    assert admission_secret == SECRET
    assert SECRET not in passed_argv, "the code must not travel as an argument"
    assert SECRET not in live_argv, "the code must not be written into sys.argv"
    assert live_argv[0] == "join.py", "sys.argv must be left alone"
    sys.argv = argv_before


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_fixture_itself_writes_an_owner_only_file(tmp_path):
    """Guard the guard: a 0644 fixture would make the refusal tests vacuous."""

    secret_file = _owner_only(tmp_path / "code.txt")
    assert stat.S_IMODE(secret_file.stat().st_mode) & 0o077 == 0
