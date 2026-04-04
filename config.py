"""
Configuration for the orchestrator.

Change settings here instead of digging through code.
"""

import json
from pathlib import Path

CONFIG_FILE = Path("config.json")

DEFAULTS = {
    # ── Local inference (Ollama) ──────────────────────────────────────
    # Model to use for all agents by default.
    # Good options by RAM:
    #   8GB:  gemma3:4b (fast, decent quality, CPU-only safe)
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

    # ── External provider (optional) ─────────────────────────────────
    # Set provider + api_key to route planner/reviewer to a stronger model.
    # Any OpenAI-compatible API works: xai (Grok), openai, together, groq, etc.
    #
    # Example for Grok:
    #   "provider": "xai",
    #   "provider_api_key": "xai-...",
    #   "provider_model": "grok-3-mini",
    #   "provider_base_url": "https://api.x.ai/v1",
    #
    # Example for OpenAI:
    #   "provider": "openai",
    #   "provider_api_key": "sk-...",
    #   "provider_model": "gpt-4o-mini",
    #
    # Leave provider as null to use Ollama for everything.
    "provider": None,              # null = use Ollama only
    "provider_api_key": None,
    "provider_model": None,
    "provider_base_url": None,     # null = auto (OpenAI default)

    # Which agent roles use the external provider (when configured).
    # "planner" and "reviewer" benefit most — builders can stay local.
    "provider_roles": ["planner", "reviewer"],

    # ── Server ───────────────────────────────────────────────────────
    "port": 8000,

    # Shared secret for node authentication.
    # Set this to a non-empty string to require worker nodes to present
    # X-Node-Secret: <value> on /nodes/register, /tasks/next, and /tasks/*/result.
    # Leave empty ("") to allow any node to join (default — trusted networks only).
    "node_secret": "",

    # ── Agent specialization (optional) ──────────────────────────────
    # Route builder tasks to nodes running a specific model.
    # The dispatcher will prefer nodes whose model matches the value set here.
    # If no node has the preferred model, any node can pick up the task (soft routing).
    #
    # Example — route builders to fast 4b nodes, leave planner/reviewer on the
    # local machine where a larger model can run:
    #   "role_model_map": {"builder": "gemma3:4b"}
    #
    # Leave empty {} to route tasks to any available node (default).
    "role_model_map": {},
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
