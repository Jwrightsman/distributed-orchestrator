"""Thin wrapper around the Ollama HTTP API."""

import json as _json
import re

import httpx

from config import get as get_config


def _cfg():
    c = get_config()
    return c["ollama_url"], c["model"], c["timeout"]


OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen3.5:4b"


def _sync_globals():
    """Update module globals from config."""
    global OLLAMA_URL, DEFAULT_MODEL
    c = get_config()
    OLLAMA_URL = c["ollama_url"]
    DEFAULT_MODEL = c["model"]

_sync_globals()


async def check_ollama() -> dict:
    """Check if Ollama is running and return available models."""
    _sync_globals()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            return {"ok": True, "models": models}
    except (httpx.ConnectError, httpx.TimeoutException):
        return {"ok": False, "models": [], "error": "Ollama is not running. Start it with: ollama serve"}
    except Exception as e:
        return {"ok": False, "models": [], "error": str(e)}


# Capabilities per model (from /api/show), fetched once per model per process.
# Used to decide whether to send "think": false — thinking models (qwen3.5 etc.)
# otherwise burn unbounded hidden reasoning tokens, which is unusable on CPU.
_capabilities_cache: dict[str, list[str]] = {}


async def _model_capabilities(client: httpx.AsyncClient, model: str) -> list[str]:
    if model not in _capabilities_cache:
        try:
            resp = await client.post(f"{OLLAMA_URL}/api/show", json={"model": model})
            resp.raise_for_status()
            _capabilities_cache[model] = resp.json().get("capabilities") or []
        except Exception:
            _capabilities_cache[model] = []
    return _capabilities_cache[model]


_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_THINK_ORPHAN = re.compile(r"</?think\b[^>]*>", re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Remove reasoning markup that leaked into a response.

    Even with think=false, reasoning models emit stray <think> tags — and an
    orphan closing tag drags the reasoning that preceded it into the
    deliverable (observed: a reviewer response whose assembled output opened
    with draft haiku lines followed by '</think>'). Anything before an orphan
    close is reasoning, so it goes too.
    """
    text = _THINK_BLOCK.sub("", text)

    # Orphan close with no open: everything before it was reasoning
    orphan_close = re.search(r"</think\s*>", text, re.IGNORECASE)
    if orphan_close and not re.search(r"<think\b", text[: orphan_close.start()], re.IGNORECASE):
        text = text[orphan_close.end():]

    return _THINK_ORPHAN.sub("", text).strip()


async def _apply_think(client: httpx.AsyncClient, payload: dict) -> None:
    """Disable hidden reasoning on thinking-capable models (config: "think")."""
    if get_config().get("think", False):
        return  # user opted in to thinking — leave the model's default alone
    if "thinking" in await _model_capabilities(client, payload["model"]):
        payload["think"] = False


async def auto_detect_model() -> str | None:
    """Pick the best available model from what Ollama has pulled.

    Preference order (best to worst for orchestration tasks, Aug 2026):
      qwen3.5 > gemma4 > phi4-mini > qwen3 > gemma3:4b > gemma3:1b
    """
    status = await check_ollama()
    if not status["ok"]:
        return None

    models = status["models"]
    preference = ["qwen3.5", "gemma4", "phi4-mini", "qwen3", "gemma3:4b", "gemma3:1b"]

    for pref in preference:
        for m in models:
            if pref in m:
                return m

    # Fall back to whatever's available
    return models[0] if models else None


async def _generate_provider(prompt: str, system: str, cfg: dict) -> str:
    """Call an OpenAI-compatible API (xai, openai, groq, together, etc.)."""
    base_url = cfg.get("provider_base_url") or "https://api.openai.com/v1"
    api_key = cfg["provider_api_key"]
    model = cfg["provider_model"]
    timeout = cfg.get("timeout", 600)

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages}

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def generate(
    prompt: str,
    system: str = "",
    model: str | None = None,
    role: str | None = None,
    format: dict | str | None = None,
) -> str:
    """Send a prompt to Ollama (or an external provider) and return the response.

    Pass role="planner" or role="reviewer" to enable provider routing when
    a provider is configured in config.json.

    Pass format= a JSON schema dict (or "json") to have Ollama constrain the
    output to that schema (structured outputs). External providers ignore it —
    callers must keep a text-parsing fallback for that path.
    """
    _sync_globals()
    cfg = get_config()

    # Route to external provider if configured and this role uses it.
    # Providers don't get the format schema — caller's parsing fallback applies.
    provider = cfg.get("provider")
    if (
        provider
        and cfg.get("provider_api_key")
        and cfg.get("provider_model")
        and role
        and role in cfg.get("provider_roles", [])
    ):
        return await _generate_provider(prompt, system, cfg)

    # Default: Ollama
    if model is None:
        model = DEFAULT_MODEL

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": cfg.get("context_tokens", 8192)},
    }
    if system:
        payload["system"] = system
    if format is not None:
        payload["format"] = format

    _, _, timeout = _cfg()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await _apply_think(client, payload)
            resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            if resp.status_code == 400 and format is not None:
                # Ollama rejected the schema (older version or unsupported model) —
                # retry unconstrained; the caller's text-parsing fallback handles it.
                payload.pop("format")
                resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            resp.raise_for_status()
            return strip_thinking(resp.json()["response"])
    except httpx.TimeoutException as e:
        # str(e) is often empty — make the failure explain itself
        raise RuntimeError(
            f"Model call timed out after {timeout}s (model={model}). On slow CPU "
            f"hardware, raise \"timeout\" in config.json."
        ) from e
    except httpx.ConnectError as e:
        # httpx says "All connection attempts failed", which tells a user nothing.
        # This is the failure that shows up on a dashboard mid-demo.
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_URL}. Is it running? "
            f"Start it with: ollama serve"
        ) from e
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise RuntimeError(
                f"Model \"{model}\" is not installed. Pull it with: ollama pull {model}"
            ) from e
        raise


async def generate_stream(prompt: str, system: str = "", model: str | None = None, role: str | None = None):
    """Stream tokens from Ollama one at a time (async generator).

    Yields individual token strings as they arrive. If an external provider is
    configured for this role, falls back to a single yield of the full response
    (providers don't expose Ollama's streaming protocol).
    """
    _sync_globals()
    cfg = get_config()

    # External provider — no token streaming, yield the full response as one chunk
    provider = cfg.get("provider")
    if (
        provider
        and cfg.get("provider_api_key")
        and cfg.get("provider_model")
        and role
        and role in cfg.get("provider_roles", [])
    ):
        result = await _generate_provider(prompt, system, cfg)
        yield result
        return

    if model is None:
        model = DEFAULT_MODEL

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {"num_ctx": cfg.get("context_tokens", 8192)},
    }
    if system:
        payload["system"] = system

    _, _, timeout = _cfg()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await _apply_think(client, payload)
            async with client.stream("POST", f"{OLLAMA_URL}/api/generate", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    token = chunk.get("response", "")
                    if token:
                        yield token
                    if chunk.get("done"):
                        break
    except httpx.TimeoutException as e:
        raise RuntimeError(
            f"Model stream stalled past {timeout}s (model={model}). The Ollama "
            f"runner may have died mid-generation, or the machine slept."
        ) from e
    except httpx.ConnectError as e:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_URL}. Is it running? "
            f"Start it with: ollama serve"
        ) from e
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise RuntimeError(
                f"Model \"{model}\" is not installed. Pull it with: ollama pull {model}"
            ) from e
        raise
