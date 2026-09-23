"""Ctrl+C on `python node.py` must stop cleanly and sign off with the coordinator.

Found on real machines on 2026-09-22 at a515dd6: one Ctrl+C printed a
~60-line traceback ending in CancelledError and KeyboardInterrupt, no session
summary, and no `POST /nodes/{id}/leave` — the node stayed "online" for about
105 s. Since Python 3.11 `asyncio.run` delivers Ctrl+C by cancelling the main
task, so the old `except KeyboardInterrupt` inside the loop never ran.

These tests raise a real SIGINT inside `asyncio.run`, so they exercise the
same path the terminal does rather than a hand-thrown exception.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import time

import httpx
import pytest

import node
from node_capabilities import NodeCapabilityDescriptorV1

COORDINATOR = "https://coordinator.example"
NODE_ID = "laptop-ab12"
TOKEN = "session-token-under-test"


def _descriptor() -> NodeCapabilityDescriptorV1:
    return NodeCapabilityDescriptorV1.model_validate(
        {
            "executor": {"kind": "ollama", "worker_protocol_version": "1"},
            "models": [{"provider": "ollama", "name": "qwen3.5:4b"}],
            "hardware": {"architecture": "x86_64", "logical_cpu_count": 4},
            "features": [],
            "limits": {
                "max_concurrent_execution_units": 1,
                "max_output_bytes": 1_048_576,
            },
            "isolation": {"kind": "none"},
        }
    )


@pytest.fixture
def worker(monkeypatch, tmp_path):
    """A worker that registers without a network and then idles in its loop.

    `leave_handler` answers `POST /nodes/{id}/leave`; `requests` records what
    was sent. `on_poll` runs once the loop is live, which is where a person
    would press Ctrl+C.
    """

    state: dict = {
        "requests": [],
        "leave_handler": lambda request: httpx.Response(200, json={"ok": True}),
        "on_poll": lambda: signal.raise_signal(signal.SIGINT),
    }

    async def _ollama_ok():
        return {"ok": True, "models": ["qwen3.5:4b"]}

    async def _descriptor_stub(**_kwargs):
        return _descriptor()

    async def _register(*_args, **_kwargs):
        return {"session_token": TOKEN, "session_id": "s-1", "node_id": NODE_ID}

    def _apply(session, registration, **_kwargs):
        session["session_token"] = registration["session_token"]
        session["tasks"] = 3
        session["credits"] = 42
        return registration["node_id"]

    async def _poll(*_args, **_kwargs):
        state["on_poll"]()
        await asyncio.sleep(3600)

    async def _transport(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        result = state["leave_handler"](request)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(node, "check_ollama", _ollama_ok)
    monkeypatch.setattr(node, "build_stock_capability_descriptor", _descriptor_stub)
    monkeypatch.setattr(node, "register", _register)
    monkeypatch.setattr(node, "_apply_registration", _apply)
    monkeypatch.setattr(node, "poll_and_execute", _poll)
    monkeypatch.setattr(node.httpx, "AsyncClient", _client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "node.py",
            "--server",
            COORDINATOR,
            "--identity-file",
            str(tmp_path / "identity.json"),
            "--node-id",
            NODE_ID,
        ],
    )
    return state


def test_ctrl_c_prints_the_summary_signs_off_and_exits_0(worker, capsys):
    started = time.monotonic()
    code = node.run()
    elapsed = time.monotonic() - started

    assert code == 0
    assert elapsed < 5
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert "Session Summary" in out
    assert NODE_ID in out and "42" in out
    assert "Signed off" in out

    [leave] = worker["requests"]
    assert leave.method == "POST"
    assert leave.url.path == f"/nodes/{NODE_ID}/leave"
    assert leave.headers["X-Node-Session"] == TOKEN
    assert json.loads(leave.content) == {"revoke_enrollment": False}, (
        "stopping must never retire the membership"
    )


def test_ctrl_c_in_the_worker_loop_does_not_escape_asyncio_run(worker):
    """`asyncio.run` itself returns 0; no KeyboardInterrupt comes out of it."""

    try:
        code = asyncio.run(node.main())
    except KeyboardInterrupt:
        # Caught here so a regression fails this test instead of ending the
        # whole pytest session as if someone had pressed Ctrl+C at it.
        pytest.fail("Ctrl+C escaped asyncio.run as KeyboardInterrupt")
    assert code == 0
    assert len(worker["requests"]) == 1


def test_an_unreachable_coordinator_does_not_hold_up_shutdown(worker, monkeypatch, capsys):
    monkeypatch.setattr(node, "_LEAVE_TIMEOUT_SECONDS", 0.3)

    async def _never_answers(_request):
        await asyncio.sleep(30)
        return httpx.Response(200)

    worker["leave_handler"] = _never_answers

    started = time.monotonic()
    code = node.run()
    elapsed = time.monotonic() - started

    assert code == 0
    assert elapsed < 5, f"shutdown waited {elapsed:.1f}s on a silent coordinator"
    out = capsys.readouterr().out
    assert "Session Summary" in out
    assert "could not be reached" in out


def test_a_refused_connection_still_exits_0(worker, capsys):
    def _refused(request):
        raise httpx.ConnectError("connection refused", request=request)

    worker["leave_handler"] = _refused

    assert node.run() == 0
    assert "could not be reached" in capsys.readouterr().out


def test_a_second_ctrl_c_during_sign_off_exits_at_once(worker, monkeypatch, capsys):
    monkeypatch.setattr(node, "_LEAVE_TIMEOUT_SECONDS", 30.0)

    async def _press_again(_request):
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(30)
        return httpx.Response(200)

    worker["leave_handler"] = _press_again

    started = time.monotonic()
    code = node.run()
    elapsed = time.monotonic() - started

    assert code == 0
    assert elapsed < 5, f"a second Ctrl+C still waited {elapsed:.1f}s"
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert "Session Summary" in out
    assert "Signed off" not in out


def test_ctrl_c_before_registration_finishes_sends_nothing(worker, monkeypatch, capsys):
    async def _interrupted_register(*_args, **_kwargs):
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(3600)

    monkeypatch.setattr(node, "register", _interrupted_register)

    assert node.run() == 0
    assert worker["requests"] == []
    out = capsys.readouterr().out
    assert "Traceback" not in out
