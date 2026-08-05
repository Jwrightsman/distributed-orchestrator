"""The read-only CLI commands must work with Ollama stopped.

`--history`, `--standings` and `--projects` only read local files. They were
gated behind the Ollama pre-flight check for a while, which made a fresh clone
on a machine without a model appear broken at the first command a new user
runs. These tests pin the ordering.
"""

import asyncio
import sys

import pytest

import cli


@pytest.fixture
def ollama_down(monkeypatch):
    """check_ollama reports failure, and blows up if the pipeline is reached."""
    async def _down():
        return {"ok": False, "error": "Ollama is not running. Start it with: ollama serve"}

    monkeypatch.setattr(cli, "check_ollama", _down)

    async def _boom(*a, **k):
        raise AssertionError("run_task must not be reached for read-only flags")

    monkeypatch.setattr(cli, "run_task", _boom)


@pytest.mark.parametrize("flag", ["--history", "--standings", "--projects"])
def test_readonly_flags_work_without_ollama(monkeypatch, ollama_down, flag, capsys):
    monkeypatch.setattr(sys, "argv", ["cli.py", flag])

    asyncio.run(cli.main())  # must not raise SystemExit

    # Something was printed, and it is not the Ollama error.
    out = capsys.readouterr().out
    assert out.strip()
    assert "Ollama is not running" not in out


@pytest.mark.parametrize("flag", ["--demo", "--demo-showcase"])
def test_inference_flags_still_blocked_without_ollama(monkeypatch, ollama_down, flag):
    """The pre-flight check must still guard anything that runs a model."""
    monkeypatch.setattr(sys, "argv", ["cli.py", flag])

    with pytest.raises(SystemExit) as exc:
        asyncio.run(cli.main())
    assert exc.value.code == 1


def test_plain_task_still_blocked_without_ollama(monkeypatch, ollama_down):
    monkeypatch.setattr(sys, "argv", ["cli.py", "build me a thing"])

    with pytest.raises(SystemExit) as exc:
        asyncio.run(cli.main())
    assert exc.value.code == 1
