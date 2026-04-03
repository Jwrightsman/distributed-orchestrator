"""Thin wrapper around the Ollama HTTP API."""

import httpx

OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma3:4b"


async def generate(prompt: str, system: str = "", model: str = DEFAULT_MODEL) -> str:
    """Send a prompt to Ollama and return the full response text."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if system:
        payload["system"] = system

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        resp.raise_for_status()
        return resp.json()["response"]
