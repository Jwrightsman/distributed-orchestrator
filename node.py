"""
Worker node — runs on any machine that wants to contribute compute.

Connects to the orchestrator server, picks up tasks, runs them locally
via Ollama, and sends results back. This is what turns a laptop into
a node in the network.

Usage:
    python node.py --server http://ORCHESTRATOR_IP:8000
    python node.py --server http://192.168.1.50:8000
"""

import argparse
import asyncio
import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field, ValidationError, field_validator
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from config import get as get_config
import tracing
from node_capabilities import (
    CapabilityProtocolModel,
    ExecutorDescriptorV1,
    GpuDescriptorV1,
    HardwareDescriptorV1,
    IsolationDescriptorV1,
    ModelDescriptorV1,
    NodeCapabilityDescriptorV1,
    NodeLimitDescriptorV1,
    capability_descriptor_digest,
)
from ollama_client import generate_stream, check_ollama, DEFAULT_MODEL
from worker_identity import (
    WorkerIdentity,
    WorkerIdentityError,
    default_identity_file,
    load_worker_identity,
    load_or_create_worker_identity,
    normalize_coordinator,
    normalize_enrollment_id,
    normalize_worker_node_id,
    persist_learned_enrollment,
)

console = Console()

_MAX_WORKER_OUTPUT_BYTES = 10_485_760
_MAX_WORKER_ERROR_BYTES = 2048
_MAX_CAPABILITY_OVERRIDE_BYTES = 65_536
_GPU_PROBE_TIMEOUT_SECONDS = 3


class WorkerCapabilityOverrides(CapabilityProtocolModel):
    """Strict operator overrides layered over best-effort local detection.

    These remain claims.  They are useful when a platform cannot expose a
    value safely, but they do not turn registration into measurement or
    attestation.  Model digests are intentionally absent: the stock worker
    advertises one only when Ollama actually supplies it.
    """

    hardware: HardwareDescriptorV1 | None = None
    features: list[str] | None = Field(default=None, max_length=32)
    executor_version: str | None = Field(default=None, max_length=64)
    model_context_tokens: int | None = Field(default=None, ge=1, le=16_777_216)
    model_variant: str | None = Field(default=None, max_length=64)
    max_context_tokens: int | None = Field(default=None, ge=1, le=16_777_216)

    @field_validator("executor_version", "model_variant")
    @classmethod
    def bounded_override_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized
        ):
            raise ValueError("capability override text must be nonblank and printable")
        return normalized


class WorkerCapabilityConfigurationError(RuntimeError):
    """A local operator override cannot produce a safe stock-worker claim."""


class NodeSessionRejected(RuntimeError):
    """The coordinator requires this worker to register for a new session."""


class NodeRegistrationRejected(RuntimeError):
    """The coordinator rejected enrollment or registration without echoing secrets."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str = "node_registration_rejected",
        action: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.action = action

    @property
    def retryable(self) -> bool:
        return self.status_code == 429 or self.status_code >= 500


def _bounded_detected_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        return None
    return normalized


def _bounded_positive_int(value: object, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # local OS/runtime data; reject non-integral floats below
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return parsed if 1 <= parsed <= maximum else None


def _memory_from_sysconf() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    bounded_pages = _bounded_positive_int(pages, 2**52)
    bounded_page_size = _bounded_positive_int(page_size, 2**30)
    if bounded_pages is None or bounded_page_size is None:
        return None
    return _bounded_positive_int(bounded_pages * bounded_page_size, 2**60)


def _memory_from_proc() -> int | None:
    try:
        with Path("/proc/meminfo").open("r", encoding="ascii", errors="strict") as handle:
            for _index, line in zip(range(64), handle, strict=False):
                if not line.startswith("MemTotal:"):
                    continue
                fields = line.split()
                if len(fields) != 3 or fields[2].casefold() != "kb":
                    return None
                kibibytes = _bounded_positive_int(fields[1], 2**50)
                return (
                    _bounded_positive_int(kibibytes * 1024, 2**60)
                    if kibibytes is not None
                    else None
                )
    except (OSError, UnicodeError):
        return None
    return None


def _memory_from_windows() -> int | None:
    if platform.system() != "Windows":
        return None
    try:
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return _bounded_positive_int(status.ullTotalPhys, 2**60)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _memory_from_macos_sysctl() -> int | None:
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=_GPU_PROBE_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (FileNotFoundError, OSError, UnicodeError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or len(result.stdout) > 64:
        return None
    return _bounded_positive_int(result.stdout.strip(), 2**60)


def _total_memory_bytes() -> int | None:
    """Return total physical memory without requiring a production dependency."""

    for detector in (
        _memory_from_sysconf,
        _memory_from_windows,
        _memory_from_proc,
        _memory_from_macos_sysctl,
    ):
        if (detected := detector()) is not None:
            return detected
    return None


def _detect_nvidia_gpus() -> list[GpuDescriptorV1] | None:
    """Best-effort bounded NVIDIA claims; unavailable tooling means unknown."""

    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=_GPU_PROBE_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (FileNotFoundError, OSError, UnicodeError, subprocess.SubprocessError):
        return None
    # Parsing is bounded even if a surprising local utility emits extra data.
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > 16_384:
        return None
    claims: list[GpuDescriptorV1] = []
    seen_claims: set[tuple[str, str, int | None]] = set()
    for line in result.stdout.splitlines()[:8]:
        fields = [field.strip() for field in line.split(",", maxsplit=1)]
        if not fields:
            continue
        model = _bounded_detected_text(fields[0], 128)
        if model is None:
            continue
        memory_bytes = None
        if len(fields) == 2:
            memory_mib = _bounded_positive_int(fields[1], 2**40)
            if memory_mib is not None:
                memory_bytes = _bounded_positive_int(memory_mib * 1024**2, 2**60)
        try:
            claim_key = ("nvidia", model, memory_bytes)
            # The v1 descriptor is a set of bounded capability claims, not an
            # inventory.  Identical boards collapse to one claim instead of
            # making a common dual-GPU host fail strict descriptor validation.
            if claim_key not in seen_claims:
                claims.append(
                    GpuDescriptorV1(
                        vendor="nvidia",
                        model=model,
                        memory_bytes=memory_bytes,
                    )
                )
                seen_claims.add(claim_key)
        except ValidationError:
            continue
    return claims or None


def _detected_hardware_descriptor() -> HardwareDescriptorV1:
    return HardwareDescriptorV1(
        architecture=_bounded_detected_text(platform.machine(), 64),
        logical_cpu_count=_bounded_positive_int(os.cpu_count(), 4096),
        total_memory_bytes=_total_memory_bytes(),
        gpus=_detect_nvidia_gpus(),
    )


def _hardware_info(descriptor: NodeCapabilityDescriptorV1 | None = None) -> dict:
    """Compatibility projection for coordinators that still read flat fields."""

    hardware = descriptor.hardware if descriptor is not None else _detected_hardware_descriptor()
    gpus = list(hardware.gpus or [])
    return {
        "cpu_count": hardware.logical_cpu_count,
        "ram_gb": (
            round(hardware.total_memory_bytes / 1024**3, 1)
            if hardware.total_memory_bytes is not None
            else None
        ),
        "gpu": gpus[0].model if gpus else None,
    }


def _merge_override_objects(base: dict[str, Any], newer: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in newer.items():
        if key == "hardware" and isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _read_capability_override_file(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_CAPABILITY_OVERRIDE_BYTES:
            raise WorkerCapabilityConfigurationError(
                f"capability override file exceeds {_MAX_CAPABILITY_OVERRIDE_BYTES} bytes"
            )
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except WorkerCapabilityConfigurationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerCapabilityConfigurationError(
            f"capability override file cannot be read as JSON: {path}"
        ) from exc
    if not isinstance(parsed, dict):
        raise WorkerCapabilityConfigurationError("capability override JSON must be one object")
    return parsed


def _load_capability_overrides(
    config_value: object,
    override_file: Path | None,
) -> WorkerCapabilityOverrides:
    if config_value is None:
        merged: dict[str, Any] = {}
    elif isinstance(config_value, dict):
        merged = dict(config_value)
    else:
        raise WorkerCapabilityConfigurationError(
            "worker_capability_overrides in config.json must be an object"
        )
    if override_file is not None:
        merged = _merge_override_objects(merged, _read_capability_override_file(override_file))
    try:
        return WorkerCapabilityOverrides.model_validate(merged)
    except ValidationError as exc:
        raise WorkerCapabilityConfigurationError(
            f"invalid worker capability overrides: {exc}"
        ) from exc


async def _detect_ollama_metadata(
    model: str,
    ollama_url: str,
) -> tuple[str | None, str | None, str | None]:
    """Return runtime version, exact model digest, and quantization when supplied."""

    executor_version = None
    model_digest = None
    model_variant = None
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            try:
                version_response = await client.get(f"{ollama_url}/api/version")
                if version_response.status_code == 200:
                    executor_version = _bounded_detected_text(
                        version_response.json().get("version"), 64
                    )
            except Exception:
                executor_version = None
            try:
                tags_response = await client.get(f"{ollama_url}/api/tags")
                if tags_response.status_code == 200:
                    records = tags_response.json().get("models", [])
                    exact = [
                        record
                        for record in records[:256]
                        if isinstance(record, dict)
                        and model in {record.get("name"), record.get("model")}
                    ] if isinstance(records, list) else []
                    if len(exact) == 1:
                        candidate = exact[0]
                        raw_digest = candidate.get("digest")
                        if isinstance(raw_digest, str):
                            try:
                                model_digest = ModelDescriptorV1(
                                    provider="ollama",
                                    name=model,
                                    digest=raw_digest,
                                ).digest
                            except ValidationError:
                                model_digest = None
                        details = candidate.get("details")
                        if isinstance(details, dict):
                            model_variant = _bounded_detected_text(
                                details.get("quantization_level"), 64
                            )
            except Exception:
                model_digest = None
                model_variant = None
    except Exception:
        pass
    return executor_version, model_digest, model_variant


async def build_stock_capability_descriptor(
    *,
    model: str,
    context_tokens: object,
    ollama_url: str,
    config_overrides: object = None,
    override_file: Path | None = None,
) -> NodeCapabilityDescriptorV1:
    """Build one immutable process-session claim from detection plus overrides."""

    # The legacy registration projection has a 96-character model bound; keep
    # the typed and flat representations one atomic, server-valid claim.
    normalized_model = _bounded_detected_text(model, 96)
    if normalized_model is None:
        raise WorkerCapabilityConfigurationError(
            "configured model must be 1-96 printable characters"
        )
    overrides = _load_capability_overrides(config_overrides, override_file)
    hardware = _detected_hardware_descriptor()
    if overrides.hardware is not None:
        update = {
            name: getattr(overrides.hardware, name)
            for name in overrides.hardware.model_fields_set
        }
        try:
            hardware = HardwareDescriptorV1.model_validate(
                {**hardware.model_dump(mode="python"), **update}
            )
        except ValidationError as exc:
            raise WorkerCapabilityConfigurationError(
                f"capability hardware is invalid after overrides: {exc}"
            ) from exc

    executor_version, model_digest, detected_variant = await _detect_ollama_metadata(
        normalized_model,
        ollama_url.rstrip("/"),
    )
    configured_context = _bounded_positive_int(context_tokens, 16_777_216)
    model_context = overrides.model_context_tokens or configured_context
    maximum_context = overrides.max_context_tokens or configured_context
    try:
        return NodeCapabilityDescriptorV1(
            descriptor_version="1",
            executor=ExecutorDescriptorV1(
                kind="ollama",
                version=overrides.executor_version or executor_version,
                worker_protocol_version="1",
            ),
            models=[
                ModelDescriptorV1(
                    provider="ollama",
                    name=normalized_model,
                    digest=model_digest,
                    context_tokens=model_context,
                    variant=overrides.model_variant or detected_variant,
                )
            ],
            hardware=hardware,
            features=overrides.features or [],
            limits=NodeLimitDescriptorV1(
                max_concurrent_execution_units=1,
                max_output_bytes=_MAX_WORKER_OUTPUT_BYTES,
                max_context_tokens=maximum_context,
            ),
            isolation=IsolationDescriptorV1(kind="none"),
        )
    except ValidationError as exc:
        raise WorkerCapabilityConfigurationError(
            f"capability descriptor is invalid after overrides: {exc}"
        ) from exc


def _auth_headers(secret: str, session_token: str = "") -> dict:
    """Build admission and per-node session headers without logging either."""

    headers = {"X-Node-Secret": secret} if secret else {}
    if session_token:
        headers["X-Node-Session"] = session_token
    return headers


def _registration_headers(
    *,
    enrollment_action: str | None,
    secret: str,
    session_token: str,
) -> dict[str, str]:
    # Returning enrollment authentication replaces the deployment-wide
    # bootstrap secret.  A legacy registration has no action and retains the
    # old admission header for local compatibility only.
    bootstrap_secret = secret if enrollment_action in {None, "bootstrap"} else ""
    return _auth_headers(bootstrap_secret, session_token)


def _session_rejected(response) -> bool:
    if response.status_code not in {401, 403, 426}:
        return False
    try:
        detail = response.json().get("detail", {})
    except Exception:
        detail = {}
    return isinstance(detail, dict) and detail.get("code") in {
        "node_session_rejected",
        "node_registration_required",
        "node_enrollment_revoked",
        "node_enrollment_credential_rotated",
        "enrollment_revoked",
        "enrollment_credential_rotated",
    }


def _bounded_registration_error(
    response,
    *,
    sensitive_values: tuple[str, ...],
) -> NodeRegistrationRejected:
    code = "node_registration_rejected"
    action = ""
    message = f"coordinator rejected node registration (HTTP {response.status_code})"
    try:
        detail = response.json().get("detail", {})
    except Exception:
        detail = {}
    secrets_to_redact = sorted(
        {value for value in sensitive_values if value},
        key=len,
        reverse=True,
    )

    def redacted(value: str) -> str:
        for secret_value in secrets_to_redact:
            value = value.replace(secret_value, "<redacted>")
        return value

    if isinstance(detail, dict):
        raw_code = detail.get("code")
        raw_action = detail.get("action")
        raw_message = detail.get("message") or detail.get("reason")
        if isinstance(raw_code, str) and raw_code:
            code = redacted(raw_code)[:96]
        if isinstance(raw_action, str) and raw_action:
            action = redacted(raw_action)[:160]
        if isinstance(raw_message, str) and raw_message:
            message = redacted(raw_message)[:512]
    elif isinstance(detail, str) and detail:
        message = redacted(detail)[:512]
    rendered = f"{code}: {message}"
    if action:
        rendered += f" (action: {action})"
    return NodeRegistrationRejected(
        rendered,
        status_code=int(response.status_code),
        code=code,
        action=action,
    )


def _bounded_utf8(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


async def register(
    server: str,
    node_id: str,
    secret: str = "",
    capabilities: list[str] | None = None,
    capability_descriptor: NodeCapabilityDescriptorV1 | None = None,
    model: str = DEFAULT_MODEL,
    session_token: str = "",
    enrollment_action: str | None = None,
    enrollment_credential: str = "",
) -> dict:
    """Register or idempotently refresh this node's server-issued session."""
    if capability_descriptor is None:
        config = get_config()
        configured_context = _bounded_positive_int(
            config.get("context_tokens"), 16_777_216
        )
        capability_descriptor = NodeCapabilityDescriptorV1(
            descriptor_version="1",
            executor=ExecutorDescriptorV1(
                kind="ollama", worker_protocol_version="1"
            ),
            models=[
                ModelDescriptorV1(
                    provider="ollama",
                    name=model,
                    context_tokens=configured_context,
                )
            ],
            hardware=_detected_hardware_descriptor(),
            features=[],
            limits=NodeLimitDescriptorV1(
                max_concurrent_execution_units=1,
                max_output_bytes=_MAX_WORKER_OUTPUT_BYTES,
                max_context_tokens=configured_context,
            ),
            isolation=IsolationDescriptorV1(kind="none"),
        )
    hw = _hardware_info(capability_descriptor)

    # Auto-detect capabilities if not provided
    caps = list(capabilities) if capabilities else []
    if not caps:
        if hw.get("gpu"):
            caps.append("gpu")
        if (hw.get("ram_gb") or 0) >= 16:
            caps.append("large-context")

    info = {
        "node_id": node_id,
        "model": model,
        "platform": platform.system(),
        "machine": capability_descriptor.hardware.architecture or platform.machine(),
        "hostname": platform.node(),
        "capabilities": caps,
        "capability_descriptor": capability_descriptor.model_dump(mode="json"),
        **hw,
    }
    if enrollment_action is not None:
        if enrollment_action not in {"bootstrap", "returning"}:
            raise ValueError("enrollment_action must be bootstrap or returning")
        info["enrollment_action"] = enrollment_action
        info["enrollment_credential"] = enrollment_credential
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        resp = await client.post(
            f"{server}/nodes/register",
            json=info,
            headers=_registration_headers(
                enrollment_action=enrollment_action,
                secret=secret,
                session_token=session_token,
            ),
        )
        if resp.status_code >= 400:
            raise _bounded_registration_error(
                resp,
                sensitive_values=(secret, enrollment_credential, session_token),
            )
        return resp.json()


def _apply_registration(
    session: dict,
    registration: dict,
    *,
    identity: WorkerIdentity | None = None,
    identity_file: Path | str | None = None,
    expected_descriptor: NodeCapabilityDescriptorV1 | None = None,
) -> str:
    """Install a registration grant and return the normalized node id."""

    token = registration.get("session_token")
    session_id = registration.get("session_id")
    node_id = registration.get("node_id")
    if not token or not session_id or not node_id:
        raise RuntimeError("orchestrator registration did not issue a node session")
    normalized_node_id = normalize_worker_node_id(str(node_id))
    if identity is not None and normalized_node_id != identity.node_id:
        raise WorkerIdentityError(
            "coordinator registration returned a different node_id for this identity"
        )
    enrollment_confirmation = registration.get("enrolled")
    if identity is not None:
        expected_action = (
            "returning" if identity.enrollment_id is not None else "bootstrap"
        )
        if (
            enrollment_confirmation is not True
            or registration.get("enrollment_action") != expected_action
        ):
            raise WorkerIdentityError(
                "coordinator did not confirm the requested durable enrollment; "
                "upgrade the coordinator instead of using a legacy session"
            )
    if expected_descriptor is not None:
        expected_hash = capability_descriptor_digest(expected_descriptor)
        if (
            registration.get("capability_descriptor_version")
            != expected_descriptor.descriptor_version
            or registration.get("capability_descriptor_hash") != expected_hash
        ):
            raise WorkerIdentityError(
                "coordinator did not confirm the advertised capability descriptor; "
                "upgrade the coordinator instead of using untyped registration"
            )
        session["capability_descriptor_version"] = (
            expected_descriptor.descriptor_version
        )
        session["capability_descriptor_hash"] = expected_hash
    enrolled = enrollment_confirmation is True
    updated_identity = identity
    if enrolled:
        enrollment_id = normalize_enrollment_id(registration.get("enrollment_id"))
        credential_version = registration.get("credential_version")
        if identity is None or identity_file is None:
            raise WorkerIdentityError(
                "enrolled registration cannot be installed without its identity file"
            )
        if identity.enrollment_id is None:
            updated_identity = persist_learned_enrollment(
                identity_file,
                identity,
                enrollment_id,
                credential_version,
            )
        elif identity.enrollment_id != enrollment_id:
            raise WorkerIdentityError(
                "coordinator returned a different enrollment_id for this identity"
            )
        elif identity.credential_version != credential_version:
            raise WorkerIdentityError(
                "coordinator returned a different credential_version for this identity"
            )
        session["enrollment_id"] = enrollment_id
        session["credential_version"] = credential_version
    else:
        # Explicit legacy sessions exist only for local compatibility.  They
        # continue using node_secret on normal requests and never masquerade as
        # durable enrollment identity.
        session["enrollment_id"] = None
        session["credential_version"] = None
    session["session_token"] = str(token)
    session["session_id"] = str(session_id)
    session["session_expires_at"] = registration.get("session_expires_at")
    session["enrolled"] = enrolled
    if updated_identity is not None:
        session["worker_identity"] = updated_identity
    return normalized_node_id


# Reconnect backoff. Measured WAN numbers (scripts/wan_bench.py, Indiana ->
# Germany) leave every timeout in this file with huge headroom — registration is
# 218 ms against a 10 s timeout — so latency is not what needed hardening. The
# retry *pattern* did.
#
# The old loop slept a flat 10 s forever. That is fine for one node and wrong for
# a network: when the orchestrator restarts, every connected node drops at the
# same instant and then retries in lockstep, hammering a box that is still
# booting. A redeploy is exactly this event, and the launch plan targets 3-5
# external nodes to start.
_RECONNECT_BASE_S = 2.0
_RECONNECT_CAP_S = 60.0


def reconnect_delay(attempt: int, rng=None) -> float:
    """Seconds to wait before retry number `attempt` (0-based).

    Exponential to a cap, with +/-25% jitter so simultaneous drops do not stay
    synchronised. Jitter is the part that actually breaks the thundering herd —
    without it, backoff just moves every node's retry to the same later instant.

    The result is clamped to the cap *after* jitter, so the cap means what it
    says. Applying jitter last let it reach 75 s against a stated 60 s cap.
    """
    import random as _random

    rng = rng or _random
    # Clamp the exponent, not just the result: a node left running through a
    # long outage reaches attempt counts where 2**attempt is an integer big
    # enough to raise OverflowError on the float multiply. Found by a test
    # asserting the cap holds for attempt=5000 — roughly three days of retries,
    # which is an ordinary laptop left open over a weekend.
    raw = min(_RECONNECT_CAP_S, _RECONNECT_BASE_S * (2 ** min(max(0, attempt), 30)))
    return min(_RECONNECT_CAP_S, raw * (0.75 + rng.random() * 0.5))


def _execution_model_for_task(
    task: dict,
    configured_model: str | None,
    capability_descriptor: NodeCapabilityDescriptorV1 | None,
) -> str | None:
    """Validate a server model binding against this process's fixed claim."""

    raw_binding = task.get("selected_model")
    if raw_binding is None:
        # Compatibility with task handouts from coordinators that predate model
        # binding, including descriptor-less legacy sessions.
        return configured_model
    if capability_descriptor is None:
        raise ValueError(
            "server selected a model but this worker has no advertised descriptor"
        )
    selected = ModelDescriptorV1.model_validate(raw_binding)
    if not any(
        advertised.provider == selected.provider
        and advertised.name == selected.name
        and advertised.digest == selected.digest
        for advertised in capability_descriptor.models
    ):
        raise ValueError(
            "server-selected model is not in this worker's immutable "
            "capability descriptor"
        )
    return selected.name


async def poll_and_execute(
    server: str,
    node_id: str,
    session: dict,
    secret: str = "",
    model: str | None = None,
    capability_descriptor: NodeCapabilityDescriptorV1 | None = None,
) -> str | None:
    """Poll the orchestrator for tasks, execute them, return task_id or None.

    The server long-polls up to 25s before returning 204, so this call
    blocks for up to ~25s when there's no work — no tight polling loop needed.
    """
    session_token = str(session.get("session_token", ""))
    legacy_secret = secret if not bool(session.get("enrolled", False)) else ""
    async with httpx.AsyncClient(timeout=600, trust_env=False) as client:
        resp = await client.get(
            f"{server}/tasks/next",
            params={"node_id": node_id},
            headers=_auth_headers(legacy_secret, session_token),
        )
        if _session_rejected(resp):
            raise NodeSessionRejected("coordinator rejected the node session")
        if resp.status_code == 204:
            return None  # No work available (long-poll timed out)
        if resp.status_code == 429:
            # Circuit breaker tripped — back off for the indicated duration
            retry_after = resp.json().get("retry_after", 60)
            console.print(f"[yellow]Circuit breaker open — sitting out {retry_after}s[/yellow]")
            await asyncio.sleep(retry_after)
            return None

        resp.raise_for_status()
        task = resp.json()
        # Whatever trace this handout belongs to, this worker's later requests
        # belong to the same one. Two headers, echoed back after revalidation;
        # nothing about this machine is sent, and nothing is enabled locally.
        trace_headers = tracing.worker_echo_headers(getattr(resp, "headers", None))

        task_id = task["task_id"]
        title = task.get("title", "unnamed")
        prompt = task["prompt"]
        system = task.get("system", "")
        try:
            max_output_bytes = int(task.get("max_output_bytes", 1_048_576))
        except (TypeError, ValueError):
            max_output_bytes = 1_048_576
        max_output_bytes = min(
            _MAX_WORKER_OUTPUT_BYTES, max(1, max_output_bytes)
        )

        console.print(Panel(
            f"[dim]{task_id}[/dim]",
            title=f"[bold yellow]TASK[/bold yellow]  {title}",
            border_style="yellow",
        ))

        start = time.time()
        try:
            execution_model = _execution_model_for_task(
                task, model, capability_descriptor
            )
            # Stream tokens from Ollama, batch them, and relay to the orchestrator
            # so the dashboard shows live output from remote workers.
            _BATCH_TOKENS = 20      # flush after this many tokens …
            _BATCH_INTERVAL = 0.3   # … or after this many seconds, whichever comes first

            collected: list[str] = []
            collected_bytes = 0
            batch: list[str] = []
            last_flush = time.time()
            limit_error: str | None = None

            async def _flush_batch(client_: httpx.AsyncClient, batch_: list[str]):
                if not batch_:
                    return None
                text = "".join(batch_)
                try:
                    stream_response = await client_.post(
                        f"{server}/tasks/{task_id}/tokens",
                        json={
                            "node_id": node_id,
                            "tokens": text,
                            "contract_version": task.get("contract_version"),
                            "attempt_id": task.get("attempt_id"),
                            "nonce": task.get("nonce"),
                            "execution_id": task.get("execution_id"),
                            "execution_unit_id": task.get("execution_unit_id"),
                            "execution_unit_kind": task.get("execution_unit_kind"),
                        },
                        headers={
                            **_auth_headers(legacy_secret, session_token),
                            **trace_headers,
                        },
                    )
                    if _session_rejected(stream_response):
                        raise NodeSessionRejected(
                            "coordinator rejected the node session while streaming"
                        )
                    if stream_response.status_code in (413, 429):
                        try:
                            payload = stream_response.json()
                        except Exception:
                            payload = {}
                        return str(
                            payload.get("error") or "stream_limit_exceeded"
                        )
                except NodeSessionRejected:
                    raise
                except Exception:
                    # Live projection remains best-effort for network failures.
                    # Session and limit outcomes are handled explicitly above.
                    pass
                return None

            async with httpx.AsyncClient(timeout=600, trust_env=False) as stream_client:
                async for token in generate_stream(
                    prompt, system=system, model=execution_model
                ):
                    token_bytes = len(token.encode("utf-8"))
                    if collected_bytes + token_bytes > max_output_bytes:
                        limit_error = "output_limit_exceeded"
                        break
                    collected.append(token)
                    collected_bytes += token_bytes
                    batch.append(token)
                    now_t = time.time()
                    if len(batch) >= _BATCH_TOKENS or (now_t - last_flush) >= _BATCH_INTERVAL:
                        relay_error = await _flush_batch(stream_client, batch)
                        batch = []
                        last_flush = now_t
                        if relay_error:
                            limit_error = relay_error
                            break
                # flush any remaining tokens
                if batch:
                    relay_error = await _flush_batch(stream_client, batch)
                    if relay_error:
                        limit_error = relay_error

            result = None if limit_error else "".join(collected)
            elapsed = time.time() - start
            result_error = None
            if limit_error:
                result_error = _bounded_utf8(
                    f"{limit_error}: generation stopped at the server-issued "
                    f"max_output_bytes={max_output_bytes}",
                    _MAX_WORKER_ERROR_BYTES,
                )

            submit_resp = await client.post(
                f"{server}/tasks/{task_id}/result",
                json={
                    "node_id": node_id,
                    "output": result,
                    "error": result_error,
                    "elapsed_seconds": elapsed,
                    # Issued with this task. An unbound result is rejected and
                    # quarantined; it can never satisfy operational execution.
                    "attempt_id": task.get("attempt_id"),
                    "nonce": task.get("nonce"),
                    "contract_version": task.get("contract_version"),
                    "execution_id": task.get("execution_id"),
                    "execution_unit_id": task.get("execution_unit_id"),
                    "execution_unit_kind": task.get("execution_unit_kind"),
                },
                headers={
                    **_auth_headers(legacy_secret, session_token),
                    **trace_headers,
                },
            )
            if _session_rejected(submit_resp):
                raise NodeSessionRejected(
                    "coordinator rejected the node session while submitting a result"
                )
            if submit_resp.status_code != 200:
                try:
                    detail = submit_resp.json().get("detail", submit_resp.text)
                except Exception:
                    detail = submit_resp.text
                console.print(
                    f"[red bold]FAILED[/red bold]  {title}: "
                    f"orchestrator rejected result ({submit_resp.status_code}): {detail}\n"
                )
                return None
            credits = submit_resp.json().get("credits_earned", 0)

            session["tasks"] += 1
            session["credits"] += credits

            if limit_error:
                console.print(
                    f"[red bold]FAILED[/red bold]  {title}: "
                    f"{limit_error} (limit {max_output_bytes} UTF-8 bytes)\n"
                )
                return None

            console.print(
                f"[bold green]DONE[/bold green]  {title} "
                f"[dim]({elapsed:.0f}s)[/dim]  "
                    f"[bold yellow]+{credits} contribution points[/bold yellow]  "
                    f"[dim]session total: {session['credits']} points[/dim]"
            )
            console.print()
            return task_id

        except NodeSessionRejected:
            raise
        except Exception as original_error:
            report_problem = ""
            try:
                error_response = await client.post(
                    f"{server}/tasks/{task_id}/result",
                    json={
                        "node_id": node_id,
                        "attempt_id": task.get("attempt_id"),
                        "nonce": task.get("nonce"),
                        "contract_version": task.get("contract_version"),
                        "execution_id": task.get("execution_id"),
                        "execution_unit_id": task.get("execution_unit_id"),
                        "execution_unit_kind": task.get("execution_unit_kind"),
                        "output": None,
                        "error": _bounded_utf8(
                            str(original_error), _MAX_WORKER_ERROR_BYTES
                        ),
                        "elapsed_seconds": time.time() - start,
                    },
                    headers=_auth_headers(legacy_secret, session_token),
                )
                if _session_rejected(error_response):
                    raise NodeSessionRejected(
                        "coordinator rejected the node session while reporting an error"
                    )
                if error_response.status_code != 200:
                    report_problem = (
                        f"; error report was rejected with HTTP "
                        f"{error_response.status_code}"
                    )
            except NodeSessionRejected:
                raise
            except Exception as report_error:
                report_problem = (
                    f"; error report also failed: "
                    f"{type(report_error).__name__}: {report_error}"
                )
            console.print(
                f"[red bold]FAILED[/red bold]  {title}: "
                f"{original_error}{report_problem}\n"
            )
            return None


async def main():
    parser = argparse.ArgumentParser(description="Join the network as a worker node")
    parser.add_argument("--server", required=True, help="Orchestrator URL (e.g. http://192.168.1.50:8000)")
    parser.add_argument("--node-id", default=None, help="Custom node ID (defaults to hostname)")
    parser.add_argument(
        "--identity-file",
        type=Path,
        help=(
            "Private durable worker identity JSON (default: a coordinator-hashed "
            "file in the current user's Mycelium configuration directory)"
        ),
    )
    parser.add_argument(
        "--secret",
        default="",
        help=(
            "Shared network-admission secret (node_secret); registration then "
            "issues a process-local node session"
        ),
    )
    parser.add_argument(
        "--model",
        help=(
            "Ollama model used for both execution and the capability claim "
            "(default: config.json model)"
        ),
    )
    parser.add_argument(
        "--capability-overrides",
        type=Path,
        help=(
            "Strict bounded JSON claim overrides layered over local detection; "
            "model digests still come only from Ollama"
        ),
    )
    parser.add_argument("--capabilities", default="", help="Comma-separated capability tags, e.g. 'gpu,large-context' (auto-detected if omitted)")
    args = parser.parse_args()

    config = get_config()
    selected_model = str(args.model or config.get("model") or DEFAULT_MODEL)

    try:
        node_id = normalize_worker_node_id(args.node_id or platform.node())
        server = normalize_coordinator(args.server)
        identity_file = (
            args.identity_file.expanduser()
            if args.identity_file is not None
            else default_identity_file(server)
        )
    except WorkerIdentityError as exc:
        console.print(f"[red bold]Invalid worker identity configuration:[/red bold] {exc}")
        return
    secret = args.secret
    capabilities = [c.strip() for c in args.capabilities.split(",") if c.strip()] if args.capabilities else None

    # Pre-flight: check local Ollama
    status = await check_ollama()
    if not status["ok"]:
        console.print(f"[red bold]ERROR:[/red bold] {status['error']}")
        return

    try:
        capability_descriptor = await build_stock_capability_descriptor(
            model=selected_model,
            context_tokens=config.get("context_tokens"),
            ollama_url=str(config.get("ollama_url") or "http://localhost:11434"),
            config_overrides=config.get("worker_capability_overrides"),
            override_file=(
                args.capability_overrides.expanduser()
                if args.capability_overrides is not None
                else None
            ),
        )
    except WorkerCapabilityConfigurationError as exc:
        console.print(
            f"[red bold]Invalid capability configuration:[/red bold] {exc}"
        )
        return
    descriptor_hash = capability_descriptor_digest(capability_descriptor)

    try:
        # A new credential is durably written before the bootstrap request. If
        # the response is lost, the same credential makes the retry converge on
        # the enrollment already committed by the coordinator.
        identity = load_or_create_worker_identity(
            identity_file,
            coordinator=server,
            node_id=node_id,
        )
    except WorkerIdentityError as exc:
        console.print(f"[red bold]Worker identity unavailable:[/red bold] {exc}")
        return

    _caps_display = ", ".join(capabilities) if capabilities else "[dim]auto-detect[/dim]"
    console.print(Panel(
        f"[bold]Node ID:[/bold]      {node_id}\n"
        f"[bold]Model:[/bold]        {selected_model}\n"
        f"[bold]Capabilities:[/bold] {_caps_display}\n"
        f"[bold]Descriptor:[/bold]   v{capability_descriptor.descriptor_version} {descriptor_hash[:12]}\n"
        f"[bold]Orchestrator:[/bold] {server}\n"
        f"[bold]Identity file:[/bold] {identity_file}",
        title="[bold cyan]Distributed AI Node[/bold cyan]",
        border_style="cyan",
    ))

    # Registration returns an ephemeral server-issued bearer session.  Keep it
    # only in memory; coordinator or worker restart intentionally requires a new
    # registration.
    session = {
        "tasks": 0,
        "credits": 0,
        "session_token": "",
        "session_id": None,
        "session_expires_at": None,
        "enrollment_id": identity.enrollment_id,
        "enrolled": False,
        "worker_identity": identity,
        "capability_descriptor_version": capability_descriptor.descriptor_version,
        "capability_descriptor_hash": descriptor_hash,
    }
    try:
        enrollment_action = (
            "returning" if identity.enrollment_id is not None else "bootstrap"
        )
        reg = await register(
            server,
            node_id,
            secret=secret,
            capabilities=capabilities,
            capability_descriptor=capability_descriptor,
            model=selected_model,
            enrollment_action=enrollment_action,
            enrollment_credential=identity.enrollment_credential,
        )
        node_id = _apply_registration(
            session,
            reg,
            identity=identity,
            identity_file=identity_file,
            expected_descriptor=capability_descriptor,
        )
        identity = session["worker_identity"]
        # After registration the server may have added auto-detected caps (model:<name>, etc.)
        # Show what was actually registered so the user knows what the node is offering.
        registered_caps = reg.get("capabilities", capabilities or [])
        visible_caps = [c for c in (registered_caps or []) if not c.startswith("model:")]
        caps_str = ", ".join(visible_caps) if visible_caps else "none"
        console.print(f"[green]Connected.[/green] {reg.get('message', '')}")
        console.print(f"[dim]Capabilities: {caps_str}[/dim]\n")
    except (NodeRegistrationRejected, WorkerIdentityError) as e:
        console.print(f"[red bold]Could not register with orchestrator at {server}[/red bold]")
        console.print(f"[dim]{e}[/dim]")
        return
    except Exception as e:
        console.print(f"[red bold]Could not connect to orchestrator at {server}[/red bold]")
        console.print(f"[dim]{e}[/dim]")
        console.print("\nMake sure the orchestrator is running:")
        console.print("  [dim]python -m uvicorn server:app --host 0.0.0.0 --port 8000[/dim]")
        return

    console.print("[dim]Waiting for tasks... (Ctrl+C to stop)[/dim]\n")

    registered = True
    attempt = 0  # consecutive connection failures, drives the backoff

    while True:
        try:
            # Re-register if we lost and regained connection
            if not registered:
                # Rotation handoff replaces the private identity file while
                # the old process-local session is being invalidated. Reload
                # before returning registration so a safely installed bundle
                # takes effect without reusing cached credential material.
                identity = load_worker_identity(
                    identity_file,
                    coordinator=server,
                    node_id=node_id,
                )
                session["worker_identity"] = identity
                enrollment_action = (
                    "returning"
                    if identity.enrollment_id is not None
                    else "bootstrap"
                )
                reg = await register(
                    server,
                    node_id,
                    secret=secret,
                    capabilities=capabilities,
                    capability_descriptor=capability_descriptor,
                    model=selected_model,
                    session_token=str(session.get("session_token", "")),
                    enrollment_action=enrollment_action,
                    enrollment_credential=identity.enrollment_credential,
                )
                node_id = _apply_registration(
                    session,
                    reg,
                    identity=identity,
                    identity_file=identity_file,
                    expected_descriptor=capability_descriptor,
                )
                console.print(f"[green]Reconnected.[/green] {reg.get('message', '')}\n")
                registered = True

            # Server long-polls up to 25s — this call already blocks while waiting.
            # No sleep needed between polls; just loop immediately.
            await poll_and_execute(
                server,
                node_id,
                session,
                secret=secret,
                model=selected_model,
                capability_descriptor=capability_descriptor,
            )

            attempt = 0  # a clean poll means the link is healthy again

        except httpx.ConnectError:
            if registered:
                console.print("[yellow]Lost connection to orchestrator. Retrying...[/yellow]")
            registered = False
            delay = reconnect_delay(attempt)
            attempt += 1
            console.print(f"[dim]Reconnecting in {delay:.0f}s...[/dim]")
            await asyncio.sleep(delay)

        except NodeSessionRejected:
            if registered:
                console.print(
                    "[yellow]Node session expired or was invalidated; "
                    "registering again...[/yellow]"
                )
            registered = False
            # No backoff is needed for an explicit authentication response.  The
            # next loop idempotently refreshes or obtains a replacement token.
            attempt = 0

        except NodeRegistrationRejected as exc:
            if not exc.retryable:
                console.print(f"[red bold]Enrollment rejected:[/red bold] {exc}")
                break
            delay = reconnect_delay(attempt)
            attempt += 1
            console.print(
                f"[yellow]Registration temporarily unavailable:[/yellow] {exc}. "
                f"Retrying in {delay:.0f}s..."
            )
            await asyncio.sleep(delay)

        except WorkerIdentityError as exc:
            console.print(f"[red bold]Worker identity rejected:[/red bold] {exc}")
            break

        except KeyboardInterrupt:
            _print_session_summary(node_id, session)
            break

        except Exception as e:
            delay = reconnect_delay(attempt)
            attempt += 1
            console.print(f"[red]Error:[/red] {e}. Retrying in {delay:.0f}s...")
            await asyncio.sleep(delay)


def _print_session_summary(node_id: str, session: dict):
    table = Table(title="Session Summary", box=box.SIMPLE, border_style="dim")
    table.add_column("Node")
    table.add_column("Tasks Completed", justify="center")
    table.add_column("Contribution Points", justify="right", style="yellow")
    table.add_row(node_id, str(session["tasks"]), str(session["credits"]))
    console.print()
    console.print(table)
    console.print("[dim]Thanks for contributing to the network.[/dim]\n")


if __name__ == "__main__":
    asyncio.run(main())
