"""Thin wrapper around the Ollama HTTP API."""

import httpx

from config import get as get_config


def _cfg():
    c = get_config()
    return c["ollama_url"], c["model"], c["timeout"]


# Expose these for other modules that import them
@property
def OLLAMA_URL():
    return get_config()["ollama_url"]

@property
def DEFAULT_MODEL():
    return get_config()["model"]

# Keep module-level names for backwards compat with node.py / server.py
OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma3:4b"


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


async def auto_detect_model() -> str | None:
    """Pick the best available model from what Ollama has pulled.

    Preference order (best to worst for orchestration tasks):
      gemma4 > qwen3:8b > gemma3:4b > anything else
    """
    status = await check_ollama()
    if not status["ok"]:
        return None

    models = status["models"]
    preference = ["gemma4", "qwen3:8b", "gemma3:4b", "gemma3:1b"]

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


async def generate(prompt: str, system: str = "", model: str | None = None, role: str | None = None) -> str:
    """Send a prompt to Ollama (or an external provider) and return the response.

    Pass role="planner" or role="reviewer" to enable provider routing when
    a provider is configured in config.json.
    """
    _sync_globals()
    cfg = get_config()

    # Route to external provider if configured and this role uses it
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
    }
    if system:
        payload["system"] = system

    _, _, timeout = _cfg()
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        resp.raise_for_status()
        return resp.json()["response"]
