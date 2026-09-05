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


# ── The advertised one-liner has to survive its own delivery mechanism ──
#
# `curl … | bash -s -- URL` gives bash the downloaded script as *stdin*, so
# everything install.sh exec's inherits a pipe. join.py then sees no terminal
# and — correctly, by the rule above — refuses. The result was an installer
# that checked Python, installed Ollama, cloned the repo, installed the deps,
# and stopped dead on "Not running in a terminal, so nobody can consent."
# Reproduced by piping into bash before the fix.
#
# The fix is to hand over on /dev/tty, which is the controlling terminal
# whatever stdin was redirected to — so a human still types "yes". The wrong
# fix, which these tests exist to block, is passing --yes from the installer:
# that consents on the machine owner's behalf, which is the entire thing the
# gate prevents.

def _install_sh() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "install.sh").read_text(encoding="utf-8")


def _install_ps1() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "install.ps1").read_text(
        encoding="utf-8"
    )


def test_installers_require_explicit_origin_before_mutating_the_machine():
    shell = _install_sh()
    powershell = _install_ps1()

    assert "An explicit coordinator origin is required" in shell
    assert shell.index("An explicit coordinator origin is required") < shell.index(
        "# 1. Python"
    )
    assert "SWARM_SERVER is required for durable enrollment" in powershell
    assert powershell.index(
        "SWARM_SERVER is required for durable enrollment"
    ) < powershell.index("# 1. Python")


def test_installer_hands_over_on_the_terminal_not_the_pipe():
    src = _install_sh()
    assert "join.py \"$@\" < /dev/tty" in src, (
        "install.sh must reconnect stdin to /dev/tty before exec'ing join.py, "
        "or the one-line install can never reach a consent prompt"
    )


def test_installer_never_consents_on_the_owners_behalf():
    # Comments may discuss --yes (one warns against exactly this); code may not.
    code = [ln for ln in _install_sh().splitlines() if not ln.lstrip().startswith("#")]
    offenders = [ln for ln in code if "--yes" in ln]
    assert not offenders, (
        f"install.sh must not pass --yes: that answers the consent prompt for "
        f"someone who never saw it. Offending lines: {offenders}"
    )


def test_installer_still_refuses_when_there_is_genuinely_no_terminal():
    """CI, containers, unattended shells: the fallback must reach plain join.py."""
    src = _install_sh()
    tail = src[src.index("# 5. Join the network"):]
    assert 'exec "$PY" join.py "$@"\n' in tail, (
        "there must be a non-tty fallback branch, so a genuinely unattended "
        "install still hits join.py's refusal instead of failing obscurely"
    )
