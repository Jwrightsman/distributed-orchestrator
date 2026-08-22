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
    # truncates large deliverables mid-sentence. 8192 is the measured sweet
    # spot on an 8GB CPU machine with qwen3.5:4b — it fits a self-contained
    # HTML app and the reviewer call still finishes inside the timeout.
    # (RAM: 4096 -> 5.8GB, 8192 -> 5.9GB, 16384 -> 6.2GB. 16384 fits in memory
    # but the reviewer's prompt got large enough to blow past 1200s on CPU.)
    # Raise it only with faster hardware; the reviewer's per-builder budget
    # scales from this automatically.
    "context_tokens": 8192,

    # Timeout for a single inference call (seconds)
    # CPU-only: the reviewer is the long pole — it ingests every builder output
    # and re-emits the whole deliverable. Measured ~15-20 min on an 8GB CPU
    # machine, so 1800s leaves headroom for variance. GPU: 120s is plenty.
    "timeout": 1800,

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

    # Separate read credential for task-, result-, project-, and machine-sensitive
    # routes.  A configured key may be sent as X-Viewer-Key, as an Authorization
    # Bearer token, or exchanged for a short-lived signed HttpOnly cookie through
    # POST /v1/viewer/session.  It is deliberately not node_secret or pitch_key:
    # permission to contribute compute or submit work does not imply permission
    # to read every private run on the coordinator.
    #
    # Empty keeps local-development compatibility, but the server reports this
    # clearly in its startup log and public health response.
    "viewer_key": "",
    "viewer_session_ttl_seconds": 8 * 3600,
    "viewer_cookie_secure": False,

    # Cap on total size of the output/ directory, in megabytes.
    # When exceeded, the oldest runs are deleted until back under the cap.
    # Set 0 to disable pruning.
    "output_max_mb": 500,

    # Canonical artifact API limits.  They apply before a manifest or ZIP is
    # returned, so a generated directory cannot turn one authenticated request
    # into unbounded memory, disk, or network use.
    "artifact_max_files": 100,
    "artifact_max_file_bytes": 50 * 1024 * 1024,
    "artifact_max_aggregate_bytes": 100 * 1024 * 1024,
    "artifact_retention_seconds": 7 * 24 * 3600,
    "execution_artifacts_max_mb": 500,

    # Pitch rate limit, per IP: at most `pitch_rate_max` pitches per
    # `pitch_rate_window` seconds on /pitch, /pitch/async and /pitch/distributed.
    # The default (5 per minute) is right for a public server. Raise it for a
    # soak test or a demo where you pitch several times in quick succession —
    # the 6th pitch inside a minute otherwise comes back 429.
    "pitch_rate_max": 5,
    "pitch_rate_window": 60,

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

    # ── Verification & reputation (optional) ─────────────────────────
    # Fraction of builder tasks that get sent to a SECOND node as well, so the
    # two answers can be compared. This is the only mechanism that can notice a
    # node returning plausible-looking garbage — the circuit breaker only sees
    # nodes that fail outright.
    #
    # Each verified task costs a whole extra inference, so this is sampled, not
    # universal. 0.1 means roughly one task in ten is double-run.
    #
    # 0 (default) disables it completely: no duplicate work, no reputation
    # records, and routing order is unchanged. Verification also switches itself
    # off whenever fewer than two nodes are connected — there is nobody to
    # compare against, and it must never block work on a single-node network.
    "verify_rate": 0.0,
}


def load() -> dict:
    """Load config from config.json, falling back to defaults."""
    config = DEFAULTS.copy()
    if CONFIG_FILE.exists():
        try:
            overrides = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            config.update(overrides)
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save(config: dict):
    """Save config to config.json."""
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def get() -> dict:
    """Get current config (load once, cache)."""
    if not hasattr(get, "_cache"):
        get._cache = load()
    return get._cache
