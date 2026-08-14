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


def test_yes_flag_skips_the_prompt(capsys):
    """--yes exists for people scripting their own machine."""
    assert join.confirm_consent("http://example:8000", True) is True


def test_refuses_when_there_is_no_terminal(monkeypatch, capsys):
    """THE ONE THAT MATTERS. No tty means no human, so no consent."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("prompted for input with no terminal attached")

    monkeypatch.setattr(builtins, "input", _boom)

    assert join.confirm_consent("http://example:8000", False) is False
    out = capsys.readouterr().out
    assert "nobody can consent" in out.lower()
    assert "--yes" in out, "should say how a deliberate script proceeds"


def test_explains_the_cost_before_asking(monkeypatch, capsys):
    """Consent is only meaningful if the costs were stated first."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *a: "yes")

    join.confirm_consent("http://example:8000", False)
    out = capsys.readouterr().out.lower()
    for claim in ("cpu", "gb", "other people"):
        assert claim in out, f"consent screen never mentions {claim!r}"
    assert "not do" in out, "should state what it will not do, not only what it will"


def test_typing_yes_proceeds(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *a: "yes")
    assert join.confirm_consent("http://example:8000", False) is True


def test_anything_else_cancels(monkeypatch):
    """Enter, 'n', or a stray word must all mean no."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    for answer in ("", "n", "no", "sure", "ok"):
        monkeypatch.setattr(builtins, "input", lambda *a, _r=answer: _r)
        expected = answer in ("y", "yes")
        assert join.confirm_consent("http://example:8000", False) is expected, answer


def test_interrupting_the_prompt_cancels(monkeypatch):
    """Ctrl+C at the prompt must not be read as agreement."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", _interrupt)
    assert join.confirm_consent("http://example:8000", False) is False


def test_consent_runs_before_anything_is_installed():
    """Order matters: the prompt is worthless after the model is downloaded."""
    src = (join.__file__ and open(join.__file__, encoding="utf-8").read()) or ""
    assert src.index("confirm_consent(server, args.yes)") < src.index("await ensure_ollama()"), (
        "consent must be requested before ensure_ollama() pulls the model"
    )
