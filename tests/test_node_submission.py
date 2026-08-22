"""Worker CLI must report authoritative result-submission failures honestly."""

from __future__ import annotations

import pytest

import node


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, task, *, reject_result=False, fail_result=False):
        self.task = task
        self.reject_result = reject_result
        self.fail_result = fail_result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, *args, **kwargs):
        return _Response(200, self.task)

    async def post(self, url, *args, **kwargs):
        if url.endswith("/stream"):
            return _Response(200, {"ok": True})
        if self.fail_result:
            raise RuntimeError("result endpoint offline")
        if self.reject_result:
            return _Response(403, {"detail": "attempt is cancelled"})
        return _Response(200, {"status": "accepted", "credits_earned": 0})


def _task():
    return {
        "task_id": "task-1",
        "title": "Build",
        "prompt": "prompt",
        "system": "system",
        "attempt_id": "attempt-1",
        "nonce": "nonce-1",
        "contract_version": "1",
        "execution_id": "e" * 32,
        "execution_unit_id": "candidate-1",
        "execution_unit_kind": "candidate",
    }


@pytest.mark.asyncio
async def test_rejected_result_submission_is_not_reported_as_done(monkeypatch):
    client = _Client(_task(), reject_result=True)
    monkeypatch.setattr(node.httpx, "AsyncClient", lambda **kwargs: client)

    async def generated(*args, **kwargs):
        yield "complete output"

    monkeypatch.setattr(node, "generate_stream", generated)
    messages = []
    monkeypatch.setattr(node.console, "print", lambda value="": messages.append(str(value)))
    session = {"tasks": 0, "credits": 0}

    completed = await node.poll_and_execute("http://server", "worker", session)

    assert completed is None
    assert session == {"tasks": 0, "credits": 0}
    rendered = "\n".join(messages)
    assert "FAILED" in rendered
    assert "rejected result" in rendered
    assert "DONE" not in rendered


@pytest.mark.asyncio
async def test_failed_error_report_does_not_hide_generation_exception(monkeypatch):
    client = _Client(_task(), fail_result=True)
    monkeypatch.setattr(node.httpx, "AsyncClient", lambda **kwargs: client)

    async def generated(*args, **kwargs):
        raise ValueError("model exploded")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(node, "generate_stream", generated)
    messages = []
    monkeypatch.setattr(node.console, "print", lambda value="": messages.append(str(value)))

    completed = await node.poll_and_execute(
        "http://server",
        "worker",
        {"tasks": 0, "credits": 0},
    )

    assert completed is None
    rendered = "\n".join(messages)
    assert "model exploded" in rendered
    assert "result endpoint offline" in rendered
