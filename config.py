"""
Configuration for the orchestrator.

Change settings here instead of digging through code.
"""

import json
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_FILE = Path(os.environ.get("MYCELIUM_CONFIG_FILE", "config.json"))
TRUSTED_ALPHA_MARKER = ".mycelium-trusted-alpha"
VALID_DEPLOYMENT_MODES = frozenset({"local", "trusted_alpha"})
VALID_NODE_ENROLLMENT_MODES = frozenset({"compat", "required"})
MIN_STATIC_CREDENTIAL_LENGTH = 32
GENERATED_SECRET_BYTES = 32

_LOG = logging.getLogger("mycelium.config")


class ConfigError(RuntimeError):
    """Configuration could not be loaded without losing operator intent."""


@dataclass(frozen=True)
class DeploymentConfigUpdate:
    """Secret-free summary of a trusted-alpha configuration migration."""

    path: Path
    generated_authorities: tuple[str, ...]
    preserved_authorities: tuple[str, ...]


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

    # "local" preserves the original single-machine development behavior.
    # "trusted_alpha" opts into strict startup validation and the deployment
    # safety contract documented in docs/DEPLOY.md.
    "deployment_mode": "local",
    "bind_host": "127.0.0.1",

    # Reverse proxies are not trusted by default. Operators terminating TLS at
    # a proxy must explicitly enable both settings and restrict proxy access.
    "https_enabled": False,
    # Trusted-alpha bearer credentials must travel through HTTPS or an
    # authenticated private overlay.  This boolean records the operator's
    # explicit overlay boundary; it is not network enforcement by the app.
    "private_overlay": False,
    "trust_proxy_headers": False,

    # Shared initial-enrollment admission secret. Enrolled workers use their
    # private credential to return and their issued session for normal work.
    # Legacy compatibility sessions still send this secret on normal requests.
    "node_secret": "",

    # "compat" permits explicitly unenrolled legacy sessions for loopback
    # development. Trusted-alpha preflight requires "required" so the shared
    # node secret is bootstrap admission rather than durable worker identity.
    "node_enrollment_mode": "compat",

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
    "public_pitch_acknowledged": False,

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


def _config_path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else CONFIG_FILE


def deployment_marker_path(path: Path | str | None = None) -> Path:
    """Return the out-of-band marker used to fail closed on damaged config."""
    return _config_path(path).parent / TRUSTED_ALPHA_MARKER


def trusted_alpha_expected(path: Path | str | None = None) -> bool:
    """Whether this installation has explicitly opted into strict startup."""
    requested_mode = os.environ.get("MYCELIUM_DEPLOYMENT_MODE", "").strip().lower()
    return requested_mode == "trusted_alpha" or deployment_marker_path(path).is_file()


def read_overrides(
    path: Path | str | None = None,
    *,
    require_exists: bool = False,
) -> dict[str, Any]:
    """Read the operator-owned JSON object without applying defaults."""
    config_path = _config_path(path)
    if not config_path.exists():
        if require_exists:
            raise ConfigError(f"configuration file is missing: {config_path}")
        return {}

    try:
        parsed = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"configuration JSON is invalid at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"configuration file cannot be read: {config_path}") from exc

    if not isinstance(parsed, dict):
        raise ConfigError("configuration JSON must contain one object")
    return parsed


def load(
    path: Path | str | None = None,
    *,
    strict: bool | None = None,
) -> dict[str, Any]:
    """Load configuration, failing closed for trusted-alpha installations.

    Local development retains the historical compatibility behavior: malformed
    configuration is ignored with a prominent warning. The deployment marker
    exists so a trusted-alpha installation cannot silently fall back to open
    local defaults if its JSON is later truncated or damaged.
    """
    config_path = _config_path(path)
    expected = trusted_alpha_expected(config_path)
    try:
        overrides = read_overrides(config_path, require_exists=expected)
    except ConfigError:
        if strict is True or (strict is None and expected):
            raise
        _LOG.warning(
            "Ignoring malformed local configuration at %s; local defaults are active",
            config_path,
        )
        overrides = {}

    configured_mode = overrides.get("deployment_mode", DEFAULTS["deployment_mode"])
    if strict is None:
        effective_strict = expected or configured_mode == "trusted_alpha"
    else:
        effective_strict = strict
    valid_mode = (
        isinstance(configured_mode, str) and configured_mode in VALID_DEPLOYMENT_MODES
    )
    if not valid_mode:
        if effective_strict:
            raise ConfigError(
                "deployment_mode must be one of: "
                + ", ".join(sorted(VALID_DEPLOYMENT_MODES))
            )
        _LOG.warning("Unknown deployment_mode; using local compatibility mode")
        overrides["deployment_mode"] = "local"

    configured_enrollment_mode = overrides.get(
        "node_enrollment_mode", DEFAULTS["node_enrollment_mode"]
    )
    valid_enrollment_mode = (
        isinstance(configured_enrollment_mode, str)
        and configured_enrollment_mode in VALID_NODE_ENROLLMENT_MODES
    )
    if not valid_enrollment_mode:
        if effective_strict:
            raise ConfigError(
                "node_enrollment_mode must be one of: "
                + ", ".join(sorted(VALID_NODE_ENROLLMENT_MODES))
            )
        _LOG.warning("Unknown node_enrollment_mode; using local compatibility mode")
        overrides["node_enrollment_mode"] = "compat"

    config = DEFAULTS.copy()
    config.update(overrides)
    if expected:
        config["deployment_mode"] = "trusted_alpha"
    elif configured_mode == "trusted_alpha":
        # Remember the operator's strict intent out of band. If config.json is
        # truncated later, the next start can still fail closed instead of
        # mistaking it for a local-development installation.
        _atomic_write_text(deployment_marker_path(config_path), "trusted_alpha\n")
    return config


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Atomically replace a small operator-owned file with private permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, mode)
        except OSError:
            # Windows ACLs are not represented by POSIX mode bits. The file is
            # still created for the current user and atomically replaced.
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def save(config: dict[str, Any], path: Path | str | None = None) -> None:
    """Atomically save configuration without printing credential values."""
    config_path = _config_path(path)
    _atomic_write_text(config_path, json.dumps(config, indent=2) + "\n")


def credential_meets_policy(value: object) -> bool:
    """Return whether a static authority has the trusted-alpha minimum size."""
    return isinstance(value, str) and len(value.strip()) >= MIN_STATIC_CREDENTIAL_LENGTH


def _new_credential(disallowed: set[str]) -> str:
    while True:
        candidate = secrets.token_urlsafe(GENERATED_SECRET_BYTES)
        if candidate not in disallowed:
            return candidate


def ensure_trusted_alpha_config(
    path: Path | str | None = None,
    *,
    model: str | None = None,
    ollama_url: str | None = None,
    private_overlay: bool | None = None,
) -> DeploymentConfigUpdate:
    """Create or safely upgrade a trusted-alpha configuration.

    Valid, independent credentials are preserved so repeat deployments do not
    disconnect operators or workers. Missing, short, or duplicate authorities
    are replaced. The returned summary intentionally contains names, never
    values. Transport protection is never inferred: callers must explicitly
    assert a private authenticated overlay or configure HTTPS separately.
    """
    config_path = _config_path(path)
    overrides = read_overrides(config_path) if config_path.exists() else {}
    config = DEFAULTS.copy()
    config.update(overrides)
    config["deployment_mode"] = "trusted_alpha"
    config["node_enrollment_mode"] = "required"
    if private_overlay is not None and not bool(config.get("https_enabled", False)):
        config["private_overlay"] = bool(private_overlay)
    if "bind_host" not in overrides:
        config["bind_host"] = "0.0.0.0"
    if model:
        config["model"] = model
    if ollama_url and "ollama_url" not in overrides:
        config["ollama_url"] = ollama_url

    generated: list[str] = []
    preserved: list[str] = []
    used: set[str] = set()
    for authority in ("node_secret", "pitch_key", "viewer_key"):
        current = config.get(authority)
        if credential_meets_policy(current) and current not in used:
            value = str(current).strip()
            preserved.append(authority)
        else:
            value = _new_credential(used)
            generated.append(authority)
        config[authority] = value
        used.add(value)

    save(config, config_path)
    _atomic_write_text(deployment_marker_path(config_path), "trusted_alpha\n")
    return DeploymentConfigUpdate(
        path=config_path,
        generated_authorities=tuple(generated),
        preserved_authorities=tuple(preserved),
    )


def get() -> dict[str, Any]:
    """Get current config (load once, cache)."""
    if not hasattr(get, "_cache"):
        get._cache = load()
    return get._cache
