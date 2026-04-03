"""Thin wrapper around the Ollama HTTP API."""

import httpx

OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma3:4b"


async def check_ollama() -> dict:
    """Check if Ollama is running and return available models."""
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


async def generate(prompt: str, system: str = "", model: str = DEFAULT_MODEL) -> str:
    """Send a prompt to Ollama and return the full response text."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if system:
        payload["system"] = system

    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        resp.raise_for_status()
        return resp.json()["response"]
