"""Joining must require a human to agree.

Mycelium is being posted in agent-native communities. The failure this guards
against is an agent reading "join the swarm", running join.py unattended, and
committing someone else's machine to downloading a 2.5 GB model and running
strangers' workloads at full CPU. That is the machine owner's decision.

The important case is the *unattended* one: no terminal means nobody consented,
so it must refuse rather than assume.
"""

import builtins
import sys
from pathlib import Path

import join
import pytest


def test_yes_flag_skips_the_prompt(capsys):
    """--yes exists for people scripting their own machine."""
    assert join.confirm_consent("https://example:8000", True) is True


def test_refuses_when_there_is_no_terminal(monkeypatch, capsys):
    """THE ONE THAT MATTERS. No tty means no human, so no consent."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("prompted for input with no terminal attached")

    monkeypatch.setattr(builtins, "input", _boom)

    assert join.confirm_consent("https://example:8000", False) is False
    out = capsys.readouterr().out
    assert "nobody can consent" in out.lower()
    assert "--yes" in out, "should say how a deliberate script proceeds"


def test_explains_the_cost_before_asking(monkeypatch, capsys):
    """Consent is only meaningful if the costs were stated first."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *a: "yes")

    join.confirm_consent("https://example:8000", False)
    out = capsys.readouterr().out.lower()
    for claim in ("cpu", "gb", "other people", "enrollment identity"):
        assert claim in out, f"consent screen never mentions {claim!r}"
    assert "not do" in out, "should state what it will not do, not only what it will"


def test_typing_yes_proceeds(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *a: "yes")
    assert join.confirm_consent("https://example:8000", False) is True


def test_anything_else_cancels(monkeypatch):
    """Enter, 'n', or a stray word must all mean no."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    for answer in ("", "n", "no", "sure", "ok"):
        monkeypatch.setattr(builtins, "input", lambda *a, _r=answer: _r)
        expected = answer in ("y", "yes")
        assert join.confirm_consent("https://example:8000", False) is expected, answer


def test_interrupting_the_prompt_cancels(monkeypatch):
    """Ctrl+C at the prompt must not be read as agreement."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", _interrupt)
    assert join.confirm_consent("https://example:8000", False) is False


def test_consent_runs_before_anything_is_installed():
    """Order matters: the prompt is worthless after the model is downloaded."""
    src = (join.__file__ and open(join.__file__, encoding="utf-8").read()) or ""
    assert src.index("confirm_consent(server, args.yes)") < src.index("await ensure_ollama()"), (
        "consent must be requested before ensure_ollama() pulls the model"
    )


@pytest.mark.asyncio
async def test_join_requires_an_explicit_coordinator_before_any_install(
    monkeypatch,
):
    monkeypatch.setattr(sys, "argv", ["join.py"])
    with pytest.raises(SystemExit) as raised:
        await join.main()
    assert raised.value.code == 2


# ── There is no longer a one-liner, and that is the point ──
#
# `install.sh` and `install.ps1` advertised `curl … | bash` and `irm … | iex`
# in their own headers. That form has no step at which the person running it
# can read what they are about to run: the download and the execution are the
# same command, and whatever answers the URL at that moment is what executes.
# For a project whose entire pitch is "lend me your computer", that was the
# wrong default, and no amount of care inside the script fixed the shape of it.
#
# Both were deleted on 2026-09-05. The replacement is `git clone`, which fetches
# the same code and leaves it sitting on disk to be read first, followed by
# `python worker_installer.py`.
#
# These tests keep them gone. Deleting a file is easy; the failure mode is
# somebody re-adding the convenience later, and a one-liner is exactly the kind
# of convenience that gets re-added.

REPO_ROOT = Path(__file__).resolve().parent.parent

_PIPE_INTO_A_SHELL = ("| bash", "|bash", "| sh", "|sh", "| iex", "|iex")

#: Files whose job is to record that this used to exist, or to forbid it.
_HISTORY = {"test_join_consent.py", "SPRINT_PHASE2.md", "SPRINT_AUG2026.md"}


def test_the_curl_pipe_bash_installers_are_gone():
    for name in ("install.sh", "install.ps1"):
        assert not (REPO_ROOT / name).exists(), (
            f"{name} is back. Piping a download into a shell gives the person "
            "running it no point at which to read the thing they are running."
        )


def _tracked_text_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {
            ".md",
            ".py",
            ".sh",
            ".ps1",
            ".yml",
            ".yaml",
            ".html",
            ".txt",
        }:
            continue
        if set(path.parts) & {
            ".git",
            ".claude",
            "__pycache__",
            "node_modules",
            "output",
            ".venv",
            ".pytest_cache",
            ".ruff_cache",
            ".hypothesis",
        }:
            continue
        if path.name in _HISTORY:
            continue
        yield path


def test_nothing_advertises_piping_this_project_into_a_shell():
    """Repo-wide. A one-liner re-added in a doc is a one-liner people run."""

    offenders: list[str] = []
    for path in _tracked_text_files():
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            if "distributed-orchestrator" not in line:
                continue
            if any(pipe in line.lower() for pipe in _PIPE_INTO_A_SHELL):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not offenders, (
        "these lines pipe this project into a shell: " + "; ".join(offenders)
    )


def test_the_documented_join_is_a_clone_somebody_can_read_first():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    joining = readme[readme.index("## Worker nodes"):]
    assert "git clone https://github.com/Jwrightsman/distributed-orchestrator" in joining
    assert "python worker_installer.py" in joining
