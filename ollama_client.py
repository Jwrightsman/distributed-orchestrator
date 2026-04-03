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


async def generate(prompt: str, system: str = "", model: str | None = None) -> str:
    """Send a prompt to Ollama and return the full response text."""
    _sync_globals()
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
