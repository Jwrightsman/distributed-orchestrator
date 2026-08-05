"""Failure messages must be readable by a human, live, on a dashboard.

SPRINT_PHASE2 §2: "Every failure path produces a clear message rather than a
stack trace — assume it happens live." The two failures a real audience
triggers are Ollama not running and the model never being pulled. Both used to
surface raw httpx text ("All connection attempts failed", "Client error '404
Not Found'"), which tells nobody anything.
"""

import httpx
import pytest

import ollama_client


@pytest.fixture
def unreachable_ollama(monkeypatch):
    """Every request raises ConnectError, as if nothing is listening."""
    class DeadClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ConnectError("All connection attempts failed")

        async def get(self, *a, **k):
            raise httpx.ConnectError("All connection attempts failed")

        def stream(self, *a, **k):
            raise httpx.ConnectError("All connection attempts failed")

    monkeypatch.setattr(ollama_client.httpx, "AsyncClient", DeadClient)


@pytest.fixture
def model_missing(monkeypatch):
    """Ollama is up but returns 404 — the model was never pulled."""
    class NotFoundClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            request = httpx.Request("POST", url)
            response = httpx.Response(404, text="model not found", request=request)
            raise httpx.HTTPStatusError("404", request=request, response=response)

        async def get(self, *a, **k):
            raise httpx.ConnectError("no capability probe in tests")

    monkeypatch.setattr(ollama_client.httpx, "AsyncClient", NotFoundClient)


async def _generate():
    return await ollama_client.generate("hello", system="be nice")


@pytest.mark.asyncio
async def test_connection_failure_names_ollama_and_the_fix(unreachable_ollama):
    with pytest.raises(RuntimeError) as exc:
        await _generate()

    message = str(exc.value)
    assert "Could not reach Ollama" in message
    assert "ollama serve" in message
    # The unhelpful httpx wording must not be what the user sees.
    assert message != "All connection attempts failed"


@pytest.mark.asyncio
async def test_missing_model_says_how_to_pull_it(model_missing):
    with pytest.raises(RuntimeError) as exc:
        await _generate()

    message = str(exc.value)
    assert "not installed" in message
    assert "ollama pull" in message


@pytest.mark.asyncio
async def test_stream_connection_failure_is_also_explained(unreachable_ollama):
    with pytest.raises(RuntimeError) as exc:
        async for _ in ollama_client.generate_stream("hello"):
            pass

    assert "Could not reach Ollama" in str(exc.value)


@pytest.mark.asyncio
async def test_error_messages_never_leak_a_traceback(unreachable_ollama):
    """Whatever reaches a dashboard or a job record must be one clean line."""
    with pytest.raises(RuntimeError) as exc:
        await _generate()

    message = str(exc.value)
    assert "Traceback" not in message
    assert "\n" not in message.strip()
