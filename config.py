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
    # Good options by RAM (Aug 2026):
    #   8GB:  qwen3.5:4b (~2.5GB — best quality/size for CPU-only)
    #   8GB:  gemma3:4b or phi4-mini (fallbacks)
    #   16GB: gemma4 e2b/e4b (slower, better quality)
    "model": "qwen3.5:4b",

    # Ollama server URL
    "ollama_url": "http://localhost:11434",

    # Thinking models (qwen3.5 etc.) generate hidden reasoning before answering.
    # Off by default: on CPU-only nodes it multiplies latency several-fold.
    # Set true to allow thinking (only affects models that support it).
    "think": False,

    # Context window in tokens. Ollama defaults to 4096, which silently
    # truncates large deliverables mid-sentence AND starves the reviewer of the
    # builder output it is supposed to merge. Measured on an 8GB CPU machine
    # with qwen3.5:4b: 4096 -> 5.8GB, 8192 -> 5.9GB, 16384 -> 6.2GB.
    # The reviewer's per-builder budget scales from this automatically.
    # Drop to 8192 if you are tight on RAM; raise it if you have plenty.
    "context_tokens": 16384,

    # Timeout for a single inference call (seconds)
    # CPU-only: 1200s — qwen3.5's reviewer calls (big prompt + long assembled
    # output) can exceed 600s on 8GB CPU machines. GPU: 120s is plenty.
    "timeout": 1200,

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

    # Shared key for pitch authentication.
    # Set this to a non-empty string to require X-Pitch-Key: <value> on
    # /pitch, /pitch/async, and /pitch/distributed. Required before exposing
    # the server to the internet — otherwise anyone can burn your compute.
    # Leave empty ("") to allow open pitching (default — trusted networks only).
    "pitch_key": "",

    # Cap on total size of the output/ directory, in megabytes.
    # When exceeded, the oldest runs are deleted until back under the cap.
    # Set 0 to disable pruning.
    "output_max_mb": 500,

    # Public pitch page (/try): lets anyone submit a task from a browser with
    # NO key — hard-limited to 2 pitches/hour per IP, 3 concurrent public jobs,
    # 300-char tasks, and a basic content filter. Off by default; understand
    # the abuse risk (docs/DEPLOY.md) before enabling on a public server.
    "public_pitch": False,

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
