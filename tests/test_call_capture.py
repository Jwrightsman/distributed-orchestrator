"""Runtime instrumentation sees HTTP attempts and terminal stream counters."""

import asyncio
import json

import httpx
import pytest

import config
import ollama_client
from execution.call_capture import CallCollector, capture_calls
from execution.experiment_scope import single_attempt_calls


@pytest.fixture
def mocked_runtime(monkeypatch):
    original = httpx.AsyncClient
    def install(handler):
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: original(*a, **kw, transport=httpx.MockTransport(handler)))
        monkeypatch.setattr(ollama_client, "_capabilities_cache", {"qwen3.5:4b": []})
    return install


def test_empty_capture_is_not_evidence_of_complete_cost():
    assert not CallCollector("fixture").cost_capture_complete


@pytest.mark.asyncio
async def test_terminal_stream_counters_including_output_and_thinking(mocked_runtime):
    final = {"done": True, "response": "", "prompt_eval_count": 7, "eval_count": 9,
             "total_duration": 2000000000}
    mocked_runtime(lambda request: httpx.Response(200, content=(json.dumps({"response": "authored"}) + "\n" + json.dumps(final)).encode()))
    collector = CallCollector("fixture", warm_cold="warm")
    with capture_calls(collector, role="builder"):
        assert [token async for token in ollama_client.generate_stream("public")] == ["authored"]
    assert collector.cost_capture_complete
    assert collector.records[0]["output_tokens"] == 9
    assert collector.records[0]["inference_seconds"] == 2


@pytest.mark.asyncio
async def test_abandoned_stream_is_not_reported_complete(mocked_runtime):
    mocked_runtime(lambda request: httpx.Response(200, content=b'{"response":"authored"}\n'))
    collector = CallCollector("fixture", warm_cold="warm")
    with capture_calls(collector):
        assert [token async for token in ollama_client.generate_stream("public")] == ["authored"]
    assert collector.records[0]["outcome"] == "abandoned"
    assert collector.dispatch_blocked and not collector.cost_capture_complete


@pytest.mark.asyncio
async def test_schema_retry_disabled_in_scoped_capture(mocked_runtime):
    attempts = []
    def handler(request):
        attempts.append(request)
        return httpx.Response(400, json={"error": "fixture unsupported schema"})
    mocked_runtime(handler)
    collector = CallCollector("fixture")
    with capture_calls(collector):
        with pytest.raises(httpx.HTTPStatusError):
            await ollama_client.generate("public", role="planner", format={"type": "array"})
    assert len(attempts) == len(collector.records) == 1
    assert collector.records[0]["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_provider_usage_keeps_inference_time_unknown(mocked_runtime, monkeypatch):
    monkeypatch.setitem(config.get(), "provider", "authored")
    monkeypatch.setitem(config.get(), "provider_api_key", "authored-not-a-secret")
    monkeypatch.setitem(config.get(), "provider_model", "authored")
    monkeypatch.setitem(config.get(), "provider_roles", ["planner"])
    mocked_runtime(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "authored"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 6}}))
    collector = CallCollector("fixture")
    with capture_calls(collector):
        assert await ollama_client.generate("public", role="planner") == "authored"
    assert collector.records[0]["input_tokens"] == 5
    assert collector.records[0]["output_tokens"] == 6
    assert collector.records[0]["inference_seconds"] is None
    assert not collector.cost_capture_complete


@pytest.mark.asyncio
async def test_pipeline_retries_are_scoped_and_not_production_defaults(monkeypatch):
    import orchestrator
    count = 0
    async def bad(*args, **kwargs):
        nonlocal count
        count += 1
        return "invalid"
    monkeypatch.setattr(orchestrator, "generate", bad)
    with single_attempt_calls():
        with pytest.raises(ValueError):
            await orchestrator.plan("public", max_retries=3)
    assert count == 1
    with pytest.raises(ValueError):
        await orchestrator.plan("public", max_retries=3)
    assert count == 4


@pytest.mark.asyncio
async def test_call_timeout_is_unknown_termination(mocked_runtime):
    async def handler(request):
        await asyncio.Event().wait()
    mocked_runtime(handler)
    def budget(record, payload, provider):
        record["timeout_seconds"] = .01
    collector = CallCollector("fixture", before_dispatch=budget)
    with capture_calls(collector):
        with pytest.raises(TimeoutError):
            await ollama_client.generate("public")
    assert collector.reconciled and collector.dispatch_blocked
    assert collector.records[0]["termination_confirmed"] is False


@pytest.mark.asyncio
async def test_malformed_provider_usage_is_unknown_without_losing_reconciliation():
    collector = CallCollector("fixture")
    record = await collector.start({}, "external")
    await collector.finish(record, {"usage": ["malformed"]}, confirmed=True)
    assert collector.reconciled and collector.dispatch_blocked
    assert record["input_tokens"] is None


@pytest.mark.asyncio
async def test_terminal_chunk_cost_survives_consumer_abandoning_output(mocked_runtime):
    mocked_runtime(lambda request: httpx.Response(200, content=b'{"done":true,"response":"authored","prompt_eval_count":1,"eval_count":2,"total_duration":3}\n'))
    collector = CallCollector("fixture", warm_cold=lambda: "warm")
    with capture_calls(collector):
        stream = ollama_client.generate_stream("public")
        assert await anext(stream) == "authored"
        await stream.aclose()
    assert collector.cost_capture_complete
    assert collector.records[0]["warm_cold"] == "warm"
