"""Tests for ollama_client helpers — thinking suppression and model detection."""

import pytest

import ollama_client
from ollama_client import _apply_think


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    """Stands in for httpx.AsyncClient — serves canned /api/show responses."""

    def __init__(self, capabilities):
        self._capabilities = capabilities

    async def post(self, url, json=None):
        assert url.endswith("/api/show")
        return _FakeResponse({"capabilities": self._capabilities})


@pytest.fixture(autouse=True)
def clear_caps_cache():
    ollama_client._capabilities_cache.clear()
    yield
    ollama_client._capabilities_cache.clear()


@pytest.mark.asyncio
async def test_think_disabled_for_thinking_models():
    payload = {"model": "qwen3.5:4b", "prompt": "hi"}
    await _apply_think(_FakeClient(["completion", "thinking"]), payload)
    assert payload["think"] is False


@pytest.mark.asyncio
async def test_think_untouched_for_plain_models():
    payload = {"model": "gemma3:4b", "prompt": "hi"}
    await _apply_think(_FakeClient(["completion"]), payload)
    assert "think" not in payload


@pytest.mark.asyncio
async def test_think_respects_config_opt_in(monkeypatch):
    import config

    cfg = config.DEFAULTS.copy()
    cfg["think"] = True
    monkeypatch.setattr(ollama_client, "get_config", lambda: cfg)
    payload = {"model": "qwen3.5:4b", "prompt": "hi"}
    await _apply_think(_FakeClient(["completion", "thinking"]), payload)
    assert "think" not in payload  # opted in — model's own default stands


@pytest.mark.asyncio
async def test_capabilities_cached_once():
    calls = []

    class CountingClient(_FakeClient):
        async def post(self, url, json=None):
            calls.append(url)
            return await super().post(url, json)

    client = CountingClient(["completion", "thinking"])
    await _apply_think(client, {"model": "m", "prompt": "a"})
    await _apply_think(client, {"model": "m", "prompt": "b"})
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_auto_detect_prefers_ladder_order(monkeypatch):
    async def fake_check():
        return {"ok": True, "models": ["gemma3:1b", "gemma3:4b", "qwen3.5:4b", "gemma4:e4b"]}

    monkeypatch.setattr(ollama_client, "check_ollama", fake_check)
    assert await ollama_client.auto_detect_model() == "qwen3.5:4b"


@pytest.mark.asyncio
async def test_auto_detect_falls_through_ladder(monkeypatch):
    async def fake_check():
        return {"ok": True, "models": ["gemma3:1b", "phi4-mini:latest"]}

    monkeypatch.setattr(ollama_client, "check_ollama", fake_check)
    assert await ollama_client.auto_detect_model() == "phi4-mini:latest"


@pytest.mark.asyncio
async def test_auto_detect_none_when_ollama_down(monkeypatch):
    async def fake_check():
        return {"ok": False, "models": [], "error": "down"}

    monkeypatch.setattr(ollama_client, "check_ollama", fake_check)
    assert await ollama_client.auto_detect_model() is None
