"""Versioned worker capability claims and deterministic hard-requirement matching.

Capability descriptors are node-advertised claims.  Persisting or matching a
descriptor does not measure, attest, verify, or otherwise establish that the
claim is true.  Operational evidence is deliberately outside this module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from node_enrollments import ensure_node_enrollment_schema
from sqlite_store import connection, migration_lock


DESCRIPTOR_VERSION_V1 = "1"
RESOURCE_REQUIREMENT_VERSION_V1 = "1"
LEGACY_DESCRIPTOR_HASH = "0" * 64
MAX_DESCRIPTOR_JSON_BYTES = 65_536
MAX_NODE_OUTPUT_BYTES = 10_485_760

ExecutorKindV1 = Literal["ollama"]
GpuVendorV1 = Literal["nvidia", "amd", "intel", "apple", "other"]
IsolationKindV1 = Literal["none", "process", "container", "virtual_machine"]
MatchReasonCodeV1 = Literal[
    "descriptor_missing",
    "executor_mismatch",
    "worker_protocol_mismatch",
    "model_mismatch",
    "model_digest_mismatch",
    "insufficient_cpu",
    "insufficient_memory",
    "gpu_required",
    "gpu_vendor_mismatch",
    "insufficient_gpu_memory",
    "insufficient_context",
    "missing_feature",
    "isolation_mismatch",
    "legacy_capability_missing",
]

_REASON_ORDER: tuple[MatchReasonCodeV1, ...] = (
    "descriptor_missing",
    "executor_mismatch",
    "worker_protocol_mismatch",
    "model_mismatch",
    "model_digest_mismatch",
    "insufficient_cpu",
    "insufficient_memory",
    "gpu_required",
    "gpu_vendor_mismatch",
    "insufficient_gpu_memory",
    "insufficient_context",
    "missing_feature",
    "isolation_mismatch",
    "legacy_capability_missing",
)


class CapabilityProtocolModel(BaseModel):
    """Strict base for bounded capability protocol objects."""

    model_config = ConfigDict(extra="forbid", strict=True)


def _clean_text(value: str, *, field_name: str, maximum: int, lower: bool = False) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must be 1-{maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} cannot contain control characters")
    return normalized.casefold() if lower else normalized


def normalize_model_digest(value: str) -> str:
    """Normalize an actually supplied immutable digest without inventing one."""

    normalized = _clean_text(
        value,
        field_name="model digest",
        maximum=71,
        lower=True,
    )
    if normalized.startswith("sha256:"):
        hexadecimal = normalized[7:]
    else:
        hexadecimal = normalized
    if len(hexadecimal) != 64 or any(
        character not in "0123456789abcdef" for character in hexadecimal
    ):
        raise ValueError("model digest must be a SHA-256 digest")
    return f"sha256:{hexadecimal}"


class ExecutorDescriptorV1(CapabilityProtocolModel):
    kind: ExecutorKindV1
    version: str | None = Field(default=None, max_length=64)
    worker_protocol_version: Literal["1"] = "1"

    @field_validator("version")
    @classmethod
    def bounded_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_text(value, field_name="executor version", maximum=64)


class ModelDescriptorV1(CapabilityProtocolModel):
    provider: Literal["ollama"]
    name: str = Field(min_length=1, max_length=128)
    digest: str | None = Field(default=None, max_length=71)
    context_tokens: int | None = Field(default=None, ge=1, le=16_777_216)
    variant: str | None = Field(default=None, max_length=64)

    @field_validator("name")
    @classmethod
    def bounded_name(cls, value: str) -> str:
        return _clean_text(value, field_name="model name", maximum=128)

    @field_validator("digest")
    @classmethod
    def canonical_digest(cls, value: str | None) -> str | None:
        return normalize_model_digest(value) if value is not None else None

    @field_validator("variant")
    @classmethod
    def bounded_variant(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_text(value, field_name="model variant", maximum=64)


class GpuDescriptorV1(CapabilityProtocolModel):
    vendor: GpuVendorV1
    model: str | None = Field(default=None, max_length=128)
    memory_bytes: int | None = Field(default=None, ge=1, le=2**60)

    @field_validator("model")
    @classmethod
    def bounded_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_text(value, field_name="GPU model", maximum=128)


class HardwareDescriptorV1(CapabilityProtocolModel):
    architecture: str | None = Field(default=None, max_length=64)
    logical_cpu_count: int | None = Field(default=None, ge=1, le=4096)
    total_memory_bytes: int | None = Field(default=None, ge=1, le=2**60)
    gpus: list[GpuDescriptorV1] | None = Field(default=None, max_length=8)

    @field_validator("architecture")
    @classmethod
    def bounded_architecture(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_text(
            value,
            field_name="machine architecture",
            maximum=64,
            lower=True,
        )

    @field_validator("gpus")
    @classmethod
    def canonical_gpu_order(
        cls, values: list[GpuDescriptorV1] | None
    ) -> list[GpuDescriptorV1] | None:
        if values is None:
            return None
        keys = [
            (gpu.vendor, gpu.model or "", gpu.memory_bytes or 0)
            for gpu in values
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("GPU claims must be unique")
        return sorted(
            values,
            key=lambda gpu: (gpu.vendor, gpu.model or "", gpu.memory_bytes or 0),
        )


class NodeLimitDescriptorV1(CapabilityProtocolModel):
    max_concurrent_execution_units: int = Field(ge=1, le=1024)
    max_output_bytes: int = Field(ge=1, le=MAX_NODE_OUTPUT_BYTES)
    max_context_tokens: int | None = Field(default=None, ge=1, le=16_777_216)


class IsolationDescriptorV1(CapabilityProtocolModel):
    kind: IsolationKindV1


class NodeCapabilityDescriptorV1(CapabilityProtocolModel):
    descriptor_version: Literal["1"] = "1"
    executor: ExecutorDescriptorV1
    models: list[ModelDescriptorV1] = Field(min_length=1, max_length=16)
    hardware: HardwareDescriptorV1
    features: list[str] = Field(default_factory=list, max_length=32)
    limits: NodeLimitDescriptorV1
    isolation: IsolationDescriptorV1

    @model_validator(mode="before")
    @classmethod
    def reject_unsupported_version(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("descriptor_version", "1") != "1":
            raise PydanticCustomError(
                "unsupported_capability_descriptor_version",
                "capability descriptor version {version} is not supported",
                {"version": data.get("descriptor_version")},
            )
        return data

    @field_validator("models")
    @classmethod
    def canonical_model_order(
        cls, values: list[ModelDescriptorV1]
    ) -> list[ModelDescriptorV1]:
        keys = [(model.provider, model.name) for model in values]
        if len(set(keys)) != len(keys):
            raise ValueError("model provider/name claims must be unique")
        return sorted(
            values,
            key=lambda model: (
                model.provider,
                model.name,
                model.digest or "",
                model.context_tokens or 0,
                model.variant or "",
            ),
        )

    @field_validator("features")
    @classmethod
    def canonical_features(cls, values: list[str]) -> list[str]:
        normalized = [
            _clean_text(value, field_name="typed feature", maximum=64, lower=True)
            for value in values
        ]
        if len(set(normalized)) != len(normalized):
            raise ValueError("typed features must be unique")
        return sorted(normalized)


class AcceptableModelV1(CapabilityProtocolModel):
    provider: Literal["ollama"]
    name: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def bounded_name(cls, value: str) -> str:
        return _clean_text(value, field_name="acceptable model name", maximum=128)


class NodeResourceRequirementsV1(CapabilityProtocolModel):
    requirement_version: Literal["1"] = "1"
    allowed_executor_kinds: list[ExecutorKindV1] | None = Field(
        default=None, min_length=1, max_length=8
    )
    required_worker_protocol_version: Literal["1"] | None = None
    acceptable_models: list[AcceptableModelV1] | None = Field(
        default=None, min_length=1, max_length=16
    )
    exact_model_digest: str | None = Field(default=None, max_length=71)
    minimum_logical_cpus: int | None = Field(default=None, ge=1, le=4096)
    minimum_memory_bytes: int | None = Field(default=None, ge=1, le=2**60)
    gpu_required: bool | None = None
    allowed_gpu_vendors: list[GpuVendorV1] | None = Field(
        default=None, min_length=1, max_length=8
    )
    minimum_gpu_memory_bytes: int | None = Field(default=None, ge=1, le=2**60)
    minimum_context_tokens: int | None = Field(default=None, ge=1, le=16_777_216)
    required_features: list[str] | None = Field(
        default=None, min_length=1, max_length=32
    )
    allowed_isolation_kinds: list[IsolationKindV1] | None = Field(
        default=None, min_length=1, max_length=8
    )

    @model_validator(mode="before")
    @classmethod
    def reject_unsupported_version(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("requirement_version", "1") != "1":
            raise PydanticCustomError(
                "unsupported_resource_requirement_version",
                "resource requirement version {version} is not supported",
                {"version": data.get("requirement_version")},
            )
        return data

    @field_validator(
        "allowed_executor_kinds",
        "allowed_gpu_vendors",
        "allowed_isolation_kinds",
    )
    @classmethod
    def canonical_allowed_values(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        if len(set(values)) != len(values):
            raise ValueError("allowed requirement values must be unique")
        return sorted(values)

    @field_validator("acceptable_models")
    @classmethod
    def canonical_models(
        cls, values: list[AcceptableModelV1] | None
    ) -> list[AcceptableModelV1] | None:
        if values is None:
            return None
        keys = [(model.provider, model.name) for model in values]
        if len(set(keys)) != len(keys):
            raise ValueError("acceptable models must be unique")
        return sorted(values, key=lambda model: (model.provider, model.name))

    @field_validator("exact_model_digest")
    @classmethod
    def canonical_digest(cls, value: str | None) -> str | None:
        return normalize_model_digest(value) if value is not None else None

    @field_validator("required_features")
    @classmethod
    def canonical_features(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = [
            _clean_text(value, field_name="required feature", maximum=64, lower=True)
            for value in values
        ]
        if len(set(normalized)) != len(normalized):
            raise ValueError("required features must be unique")
        return sorted(normalized)

    @model_validator(mode="after")
    def coherent_gpu_constraints(self) -> NodeResourceRequirementsV1:
        if self.gpu_required is False and (
            self.allowed_gpu_vendors is not None
            or self.minimum_gpu_memory_bytes is not None
        ):
            raise ValueError("GPU constraints conflict with gpu_required=false")
        return self


def has_typed_resource_constraints(
    requirements: NodeResourceRequirementsV1 | dict[str, Any] | None,
) -> bool:
    if requirements is None:
        return False
    parsed = (
        requirements
        if isinstance(requirements, NodeResourceRequirementsV1)
        else NodeResourceRequirementsV1.model_validate(requirements)
    )
    payload = parsed.model_dump(mode="json", exclude_none=True)
    payload.pop("requirement_version", None)
    # ``false`` means that a GPU is not required; it is not a prohibition and
    # therefore adds no hard constraint beyond an omitted field.
    if payload.get("gpu_required") is False:
        payload.pop("gpu_required")
    return bool(payload)


def canonical_descriptor_json(
    descriptor: NodeCapabilityDescriptorV1 | dict[str, Any],
) -> str:
    parsed = (
        descriptor
        if isinstance(descriptor, NodeCapabilityDescriptorV1)
        else NodeCapabilityDescriptorV1.model_validate(descriptor)
    )
    return json.dumps(
        parsed.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def capability_descriptor_digest(
    descriptor: NodeCapabilityDescriptorV1 | dict[str, Any],
) -> str:
    return hashlib.sha256(canonical_descriptor_json(descriptor).encode("utf-8")).hexdigest()


def canonical_requirement_digest(
    resource_requirements: NodeResourceRequirementsV1 | dict[str, Any] | None,
    legacy_required_capabilities: Sequence[str] = (),
) -> str:
    parsed = (
        resource_requirements
        if isinstance(resource_requirements, NodeResourceRequirementsV1)
        else (
            NodeResourceRequirementsV1.model_validate(resource_requirements)
            if resource_requirements is not None
            else None
        )
    )
    # Match request-hash semantics: an omitted block, an explicit empty block,
    # and ``gpu_required=false`` all express the same absence of typed hard
    # constraints and therefore receive one canonical requirement identity.
    typed_payload = (
        parsed.model_dump(mode="json")
        if parsed is not None and has_typed_resource_constraints(parsed)
        else None
    )
    payload = {
        "legacy_required_capabilities": sorted(set(legacy_required_capabilities)),
        "resource_requirements": typed_payload,
    }
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def canonical_requirement_binding(
    resource_requirements: NodeResourceRequirementsV1 | dict[str, Any] | None,
    legacy_required_capabilities: Sequence[str] = (),
) -> tuple[str, str]:
    """Return the server-authoritative version and digest for one requirement set."""

    parsed = (
        resource_requirements
        if isinstance(resource_requirements, NodeResourceRequirementsV1)
        else (
            NodeResourceRequirementsV1.model_validate(resource_requirements)
            if resource_requirements is not None
            else None
        )
    )
    version = parsed.requirement_version if parsed is not None else "1"
    return version, canonical_requirement_digest(parsed, legacy_required_capabilities)


@dataclass(frozen=True)
class CapabilityMatchResultV1:
    eligible: bool
    reason_codes: tuple[MatchReasonCodeV1, ...]
    matched_descriptor_hash: str
    selected_model: ModelDescriptorV1 | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason_codes": list(self.reason_codes),
            "matched_descriptor_hash": self.matched_descriptor_hash,
            "selected_model": (
                {
                    "provider": self.selected_model.provider,
                    "name": self.selected_model.name,
                    "digest": self.selected_model.digest,
                }
                if self.selected_model is not None
                else None
            ),
        }


def _matching_model_candidates(
    requirements: NodeResourceRequirementsV1 | None,
    descriptor: NodeCapabilityDescriptorV1,
) -> tuple[list[ModelDescriptorV1], set[MatchReasonCodeV1]]:
    """Return runnable models and model-specific mismatch reasons.

    This is the single filtering path shared by eligibility and handout model
    selection.  A model must satisfy every model constraint as one coherent
    descriptor entry; claims from different models are never combined.
    """

    reasons: set[MatchReasonCodeV1] = set()
    candidate_models = list(descriptor.models)
    if requirements is None:
        return candidate_models, reasons

    if requirements.acceptable_models is not None:
        allowed_models = {
            (model.provider, model.name) for model in requirements.acceptable_models
        }
        candidate_models = [
            model
            for model in candidate_models
            if (model.provider, model.name) in allowed_models
        ]
        if not candidate_models:
            reasons.add("model_mismatch")

    if requirements.exact_model_digest is not None:
        candidate_models = [
            model
            for model in candidate_models
            if model.digest == requirements.exact_model_digest
        ]
        if not candidate_models:
            reasons.add("model_digest_mismatch")

    if requirements.minimum_context_tokens is not None:
        context_cap = descriptor.limits.max_context_tokens
        candidate_models = [
            model
            for model in candidate_models
            if model.context_tokens is not None
            and model.context_tokens >= requirements.minimum_context_tokens
            and (
                context_cap is None
                or context_cap >= requirements.minimum_context_tokens
            )
        ]
        if not candidate_models:
            reasons.add("insufficient_context")

    return candidate_models, reasons


def select_model_for_requirements(
    resource_requirements: NodeResourceRequirementsV1 | dict[str, Any] | None,
    node_descriptor: NodeCapabilityDescriptorV1 | dict[str, Any],
    *,
    preferred_model_name: str | None = None,
) -> ModelDescriptorV1 | None:
    """Purely select the exact advertised model a qualifying worker should run.

    The configured model is retained when it satisfies every model constraint.
    Otherwise the canonical first matching descriptor entry is selected.  A
    ``None`` result means the descriptor has no model satisfying the request.
    """

    requirements = (
        resource_requirements
        if isinstance(resource_requirements, NodeResourceRequirementsV1)
        else (
            NodeResourceRequirementsV1.model_validate(resource_requirements)
            if resource_requirements is not None
            else None
        )
    )
    descriptor = (
        node_descriptor
        if isinstance(node_descriptor, NodeCapabilityDescriptorV1)
        else NodeCapabilityDescriptorV1.model_validate(node_descriptor)
    )
    candidates, _reasons = _matching_model_candidates(requirements, descriptor)
    if preferred_model_name is not None:
        for candidate in candidates:
            if candidate.provider == "ollama" and candidate.name == preferred_model_name:
                return candidate
    return candidates[0] if candidates else None


def match_node_requirements(
    resource_requirements: NodeResourceRequirementsV1 | dict[str, Any] | None,
    legacy_required_capabilities: Sequence[str],
    node_descriptor: NodeCapabilityDescriptorV1 | dict[str, Any] | None,
    legacy_node_capabilities: Sequence[str],
    *,
    preferred_model_name: str | None = None,
) -> CapabilityMatchResultV1:
    """Pure deterministic hard-constraint matcher used by every routing path."""

    requirements = (
        resource_requirements
        if isinstance(resource_requirements, NodeResourceRequirementsV1)
        else (
            NodeResourceRequirementsV1.model_validate(resource_requirements)
            if resource_requirements is not None
            else None
        )
    )
    descriptor = (
        node_descriptor
        if isinstance(node_descriptor, NodeCapabilityDescriptorV1)
        else (
            NodeCapabilityDescriptorV1.model_validate(node_descriptor)
            if node_descriptor is not None
            else None
        )
    )
    reasons: set[MatchReasonCodeV1] = set()
    typed = has_typed_resource_constraints(requirements)
    descriptor_hash = (
        capability_descriptor_digest(descriptor)
        if descriptor is not None
        else LEGACY_DESCRIPTOR_HASH
    )
    selected_model = (
        select_model_for_requirements(
            requirements,
            descriptor,
            preferred_model_name=preferred_model_name,
        )
        if descriptor is not None
        else None
    )

    if typed and descriptor is None:
        reasons.add("descriptor_missing")
    elif typed and requirements is not None and descriptor is not None:
        if (
            requirements.allowed_executor_kinds is not None
            and descriptor.executor.kind not in requirements.allowed_executor_kinds
        ):
            reasons.add("executor_mismatch")
        if (
            requirements.required_worker_protocol_version is not None
            and descriptor.executor.worker_protocol_version
            != requirements.required_worker_protocol_version
        ):
            reasons.add("worker_protocol_mismatch")

        _candidate_models, model_reasons = _matching_model_candidates(
            requirements, descriptor
        )
        reasons.update(model_reasons)

        hardware = descriptor.hardware
        if requirements.minimum_logical_cpus is not None and (
            hardware.logical_cpu_count is None
            or hardware.logical_cpu_count < requirements.minimum_logical_cpus
        ):
            reasons.add("insufficient_cpu")
        if requirements.minimum_memory_bytes is not None and (
            hardware.total_memory_bytes is None
            or hardware.total_memory_bytes < requirements.minimum_memory_bytes
        ):
            reasons.add("insufficient_memory")

        gpu_constraints = bool(
            requirements.gpu_required
            or requirements.allowed_gpu_vendors is not None
            or requirements.minimum_gpu_memory_bytes is not None
        )
        gpus = list(hardware.gpus or [])
        if gpu_constraints and not gpus:
            reasons.add("gpu_required")
        else:
            matching_gpus = gpus
            if requirements.allowed_gpu_vendors is not None:
                matching_gpus = [
                    gpu
                    for gpu in matching_gpus
                    if gpu.vendor in requirements.allowed_gpu_vendors
                ]
                if gpus and not matching_gpus:
                    reasons.add("gpu_vendor_mismatch")
            if requirements.minimum_gpu_memory_bytes is not None and not any(
                gpu.memory_bytes is not None
                and gpu.memory_bytes >= requirements.minimum_gpu_memory_bytes
                for gpu in matching_gpus
            ):
                reasons.add("insufficient_gpu_memory")

        if requirements.required_features is not None and not set(
            requirements.required_features
        ).issubset(set(descriptor.features)):
            reasons.add("missing_feature")
        if (
            requirements.allowed_isolation_kinds is not None
            and descriptor.isolation.kind not in requirements.allowed_isolation_kinds
        ):
            reasons.add("isolation_mismatch")

    if not set(legacy_required_capabilities).issubset(
        set(legacy_node_capabilities)
    ):
        reasons.add("legacy_capability_missing")

    ordered = tuple(reason for reason in _REASON_ORDER if reason in reasons)
    return CapabilityMatchResultV1(
        eligible=not ordered,
        reason_codes=ordered,
        matched_descriptor_hash=descriptor_hash,
        selected_model=selected_model if not ordered else None,
    )


@dataclass(frozen=True)
class NodeCapabilitySnapshotRecord:
    enrollment_id: str
    descriptor_hash: str
    descriptor_version: str
    descriptor_json: str
    first_seen_at: float
    last_seen_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> NodeCapabilitySnapshotRecord:
        record = cls(
            enrollment_id=str(row["enrollment_id"]),
            descriptor_hash=str(row["descriptor_hash"]),
            descriptor_version=str(row["descriptor_version"]),
            descriptor_json=str(row["descriptor_json"]),
            first_seen_at=float(row["first_seen_at"]),
            last_seen_at=float(row["last_seen_at"]),
        )
        descriptor = NodeCapabilityDescriptorV1.model_validate_json(
            record.descriptor_json
        )
        canonical = canonical_descriptor_json(descriptor)
        digest = capability_descriptor_digest(descriptor)
        if canonical != record.descriptor_json:
            raise RuntimeError("stored capability descriptor JSON is not canonical")
        if descriptor.descriptor_version != record.descriptor_version:
            raise RuntimeError("stored capability descriptor version is inconsistent")
        if digest != record.descriptor_hash:
            raise RuntimeError("stored capability descriptor hash is inconsistent")
        return record

    @property
    def descriptor(self) -> NodeCapabilityDescriptorV1:
        return NodeCapabilityDescriptorV1.model_validate_json(self.descriptor_json)


def ensure_node_capability_snapshot_schema(con: sqlite3.Connection) -> None:
    ensure_node_enrollment_schema(con)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS node_capability_snapshots (
            enrollment_id      TEXT NOT NULL,
            descriptor_hash    TEXT NOT NULL,
            descriptor_version TEXT NOT NULL,
            descriptor_json    TEXT NOT NULL,
            first_seen_at      REAL NOT NULL,
            last_seen_at       REAL NOT NULL,
            PRIMARY KEY (enrollment_id, descriptor_hash),
            FOREIGN KEY (enrollment_id) REFERENCES node_enrollments(enrollment_id)
                ON DELETE RESTRICT
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_capability_snapshots_last_seen "
        "ON node_capability_snapshots(last_seen_at)"
    )


class NodeCapabilitySnapshotStore:
    """Durable immutable snapshots of enrolled-node capability claims."""

    def __init__(self, path: str | Path = "events.db"):
        self.path = Path(path)
        self._lock = threading.RLock()

    def migrate(self) -> None:
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            ensure_node_capability_snapshot_schema(con)
            con.commit()

    @staticmethod
    def _validate_enrollment_id(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not normalized or len(normalized) > 64:
            raise ValueError("enrollment_id must be 1-64 characters")
        if any(ord(character) < 33 or ord(character) > 126 for character in normalized):
            raise ValueError("enrollment_id must be printable ASCII")
        return normalized

    def remember(
        self,
        enrollment_id: str,
        descriptor: NodeCapabilityDescriptorV1 | dict[str, Any],
        *,
        now: float | None = None,
    ) -> NodeCapabilitySnapshotRecord:
        normalized_enrollment = self._validate_enrollment_id(enrollment_id)
        parsed = (
            descriptor
            if isinstance(descriptor, NodeCapabilityDescriptorV1)
            else NodeCapabilityDescriptorV1.model_validate(descriptor)
        )
        canonical = canonical_descriptor_json(parsed)
        if len(canonical.encode("utf-8")) > MAX_DESCRIPTOR_JSON_BYTES:
            raise ValueError("capability descriptor exceeds the storage limit")
        digest = capability_descriptor_digest(parsed)
        observed_at = time.time() if now is None else float(now)

        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            with migration_lock(self.path):
                ensure_node_capability_snapshot_schema(con)
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM node_capability_snapshots "
                "WHERE enrollment_id = ? AND descriptor_hash = ?",
                (normalized_enrollment, digest),
            ).fetchone()
            if existing is not None:
                prior = NodeCapabilitySnapshotRecord.from_row(existing)
                if (
                    prior.descriptor_version != parsed.descriptor_version
                    or prior.descriptor_json != canonical
                ):
                    raise RuntimeError("capability snapshot hash collision or corruption")
                con.execute(
                    "UPDATE node_capability_snapshots SET last_seen_at = ? "
                    "WHERE enrollment_id = ? AND descriptor_hash = ?",
                    (observed_at, normalized_enrollment, digest),
                )
            else:
                con.execute(
                    """
                    INSERT INTO node_capability_snapshots (
                        enrollment_id, descriptor_hash, descriptor_version,
                        descriptor_json, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_enrollment,
                        digest,
                        parsed.descriptor_version,
                        canonical,
                        observed_at,
                        observed_at,
                    ),
                )
            con.commit()
            row = con.execute(
                "SELECT * FROM node_capability_snapshots "
                "WHERE enrollment_id = ? AND descriptor_hash = ?",
                (normalized_enrollment, digest),
            ).fetchone()
        if row is None:  # pragma: no cover - committed row
            raise RuntimeError("capability snapshot disappeared after persistence")
        return NodeCapabilitySnapshotRecord.from_row(row)

    def get(
        self, enrollment_id: str, descriptor_hash: str
    ) -> NodeCapabilitySnapshotRecord | None:
        normalized_enrollment = self._validate_enrollment_id(enrollment_id)
        if len(descriptor_hash) != 64 or any(
            character not in "0123456789abcdef" for character in descriptor_hash
        ):
            return None
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute(
                "SELECT * FROM node_capability_snapshots "
                "WHERE enrollment_id = ? AND descriptor_hash = ?",
                (normalized_enrollment, descriptor_hash),
            ).fetchone()
        return NodeCapabilitySnapshotRecord.from_row(row) if row else None

    def list_for_enrollment(
        self, enrollment_id: str
    ) -> list[NodeCapabilitySnapshotRecord]:
        normalized_enrollment = self._validate_enrollment_id(enrollment_id)
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            rows = con.execute(
                "SELECT * FROM node_capability_snapshots WHERE enrollment_id = ? "
                "ORDER BY first_seen_at, descriptor_hash",
                (normalized_enrollment,),
            ).fetchall()
        return [NodeCapabilitySnapshotRecord.from_row(row) for row in rows]
