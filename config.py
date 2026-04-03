"""
Configuration for the orchestrator.

Change settings here instead of digging through code.
"""

import json
from pathlib import Path

CONFIG_FILE = Path("config.json")

DEFAULTS = {
    # Model to use for inference. Must be pulled in Ollama.
    # Good options by RAM:
    #   8GB:  gemma3:4b (fast, decent quality)
    #   16GB: gemma4 (slower, much better quality)
    #   16GB: qwen3:8b (good balance)
    "model": "gemma3:4b",

    # Ollama server URL
    "ollama_url": "http://localhost:11434",

    # Timeout for a single inference call (seconds)
    # CPU-only: 600s is safe. GPU: 120s is plenty.
    "timeout": 600,

    # Max planner retries when model returns bad JSON
    "planner_retries": 3,

    # Server port
    "port": 8000,
}


def load() -> dict:
    """Load config from config.json, falling back to defaults."""
    config = DEFAULTS.copy()
    if CONFIG_FILE.exists():
        try:
            overrides = json.loads(CONFIG_FILE.read_text())
            config.update(overrides)
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save(config: dict):
    """Save config to config.json."""
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def get() -> dict:
    """Get current config (load once, cache)."""
    if not hasattr(get, "_cache"):
        get._cache = load()
    return get._cache
