"""Tests for plan() with a mocked generate() — no Ollama involved."""

import json

import pytest

import orchestrator

VALID_PLAN = [
    {"id": 1, "title": "Design", "prompt": "Design the thing in full detail.", "depends_on": []},
    {"id": 2, "title": "Build", "prompt": "Build the thing from the design.", "depends_on": [1]},
    {"id": 3, "title": "Document", "prompt": "Write the docs for the thing.", "depends_on": [2]},
]


def _fake_generate(responses):
    """Return an async generate() stand-in that pops canned responses in order."""
    queue = list(responses)
    calls = []

    async def fake(prompt, system="", model=None, role=None, format=None):
        calls.append({"prompt": prompt, "system": system, "role": role, "format": format})
        return queue.pop(0)

    fake.calls = calls
    return fake


@pytest.mark.asyncio
async def test_plan_parses_clean_json(monkeypatch):
    fake = _fake_generate([json.dumps(VALID_PLAN)])
    monkeypatch.setattr(orchestrator, "generate", fake)
    subtasks = await orchestrator.plan("build a thing", max_retries=3)
    assert [st["id"] for st in subtasks] == [1, 2, 3]
    # The planner call must request schema-enforced output (structured outputs)
    assert fake.calls[0]["format"] == orchestrator.PLANNER_FORMAT
    assert fake.calls[0]["role"] == "planner"


@pytest.mark.asyncio
async def test_plan_retries_on_garbage_then_succeeds(monkeypatch):
    fake = _fake_generate(["utter nonsense, no json here", json.dumps(VALID_PLAN)])
    monkeypatch.setattr(orchestrator, "generate", fake)
    subtasks = await orchestrator.plan("build a thing", max_retries=3)
    assert len(subtasks) == 3
    assert len(fake.calls) == 2
    # Retry prompt must tell the model what went wrong
    assert "could not be parsed" in fake.calls[1]["prompt"]


@pytest.mark.asyncio
async def test_plan_exhausts_retries_and_raises(monkeypatch):
    fake = _fake_generate(["nope", "still nope", "nope again"])
    monkeypatch.setattr(orchestrator, "generate", fake)
    with pytest.raises(ValueError, match="failed after 3 attempts"):
        await orchestrator.plan("build a thing", max_retries=3)


@pytest.mark.asyncio
async def test_plan_includes_memory_context(monkeypatch):
    fake = _fake_generate([json.dumps(VALID_PLAN)])
    monkeypatch.setattr(orchestrator, "generate", fake)
    await orchestrator.plan("iterate on it", max_retries=1, memory_context="Previously built: a widget")
    assert "Previously built: a widget" in fake.calls[0]["system"]


def test_planner_format_schema_shape():
    """The schema handed to Ollama must describe the exact subtask shape."""
    schema = orchestrator.PLANNER_FORMAT
    assert schema["type"] == "array"
    item = schema["items"]
    assert set(item["required"]) == {"id", "title", "prompt", "depends_on"}
    assert item["properties"]["depends_on"]["type"] == "array"
