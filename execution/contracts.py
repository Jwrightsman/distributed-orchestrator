"""Bounded Pydantic contracts for execution protocol version 1."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from node_capabilities import NodeResourceRequirementsV1

StrategyNameV1 = Literal["auto", "dag", "ensemble", "direct"]
SelectedStrategyV1 = Literal["dag", "ensemble"]
PlacementV1 = Literal["auto", "local", "distributed"]
SelectedPlacementV1 = Literal["local", "distributed"]
ObservedPlacementV1 = Literal["none", "local", "distributed", "mixed"]
ConfidentialityV1 = Literal[
    "local_only",
    "trusted_guild",
    "approved_nodes",
    "public",
]
LifecycleStatusV1 = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]
CompatibilityStatusV1 = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "unverified",
    "cancelled",
    "interrupted",
]
ValidationOutcomeV1 = Literal["passed", "failed", "partial", "not_run"]
AssuranceLevelV1 = Literal["unverified", "structural", "deterministic", "model_judged"]
RequirementSourceV1 = Literal["contract_floor", "explicit"]
ValidationAggregationV1 = Literal["all", "any"]

MAX_ARTIFACTS_V1 = 20
SUPPORTED_JSON_SCHEMA_DRAFT_V1 = "https://json-schema.org/draft/2020-12/schema"


def normalize_manifest_path(value: str) -> str:
    """Return one portable relative manifest path or raise ``ValueError``.

    Protocol paths always use POSIX separators. Backslashes are rejected rather
    than silently rewritten so a manifest cannot mean one thing on Windows and
    another on a contributor node. Dot segments and empty segments are also
    rejected instead of being collapsed, which makes duplicate detection
    deterministic at request time.
    """

    value = value.strip()
    if not value or len(value) > 200:
        raise ValueError("required file paths must be 1-200 characters")
    if "\x00" in value:
        raise ValueError("required file paths cannot contain NUL bytes")
    if "\\" in value:
        raise ValueError("required file paths must use '/' separators, not backslashes")
    if value.startswith("/") or PureWindowsPath(value).drive or re.match(r"^[A-Za-z]:", value):
        raise ValueError("required file paths must be relative and cannot include a drive")
    if ":" in value:
        raise ValueError("required file paths cannot contain ':'")

    parts = value.split("/")
    if any(part == "" for part in parts):
        raise ValueError("required file paths cannot contain empty path segments")
    if any(part == "." for part in parts):
        raise ValueError("required file paths cannot contain '.' segments")
    if any(part == ".." for part in parts):
        raise ValueError("required file paths cannot contain '..' segments")

    normalized = PurePosixPath(*parts).as_posix()
    if normalized in ("", ".") or normalized.startswith("../"):
        raise ValueError("required file paths must be normalized relative paths")
    return normalized


class ProtocolModel(BaseModel):
    """Strict base model so misspelled protocol fields fail loudly."""

    model_config = ConfigDict(extra="forbid")


class DagOptionsV1(ProtocolModel):
    kind: Literal["dag"] = "dag"
    maximum_subtasks: int = Field(default=5, ge=1, le=5)
    review_enabled: bool = True
    revision_enabled: bool = True


class EnsembleOptionsV1(ProtocolModel):
    kind: Literal["ensemble"] = "ensemble"
    candidates: int = Field(default=3, ge=1, le=5)
    concurrency: int = Field(default=1, ge=1, le=5)
    selection_policy: Literal["validated_score", "first_valid"] = "validated_score"

    @model_validator(mode="after")
    def concurrency_not_above_candidates(self):
        if self.concurrency > self.candidates:
            raise ValueError("ensemble concurrency cannot exceed candidate count")
        return self


StrategyOptionsV1 = Annotated[
    DagOptionsV1 | EnsembleOptionsV1,
    Field(discriminator="kind"),
]


class ValidatorSpecV1(ProtocolModel):
    name: Literal[
        "nonempty",
        "structured_json",
        "json_schema",
        "file_manifest",
        "code_parse",
        "artifact_extraction",
        "artifact_contract",
    ]
    required: bool = True
    minimum_score: float | None = Field(default=None, ge=0.0, le=1.0)


class OutputContractV1(ProtocolModel):
    kind: Literal["text", "single_artifact", "structured_json", "file_manifest", "code"] = "text"
    artifact_count: int = Field(default=1, ge=1, le=MAX_ARTIFACTS_V1)
    format: str | None = Field(default=None, max_length=64)
    required_files: list[str] = Field(default_factory=list, max_length=50)
    json_schema: dict[str, Any] | None = None
    validators: list[ValidatorSpecV1] = Field(default_factory=list, max_length=8)

    @model_validator(mode="before")
    @classmethod
    def infer_manifest_artifact_count(cls, data: Any):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        required = data.get("required_files")
        if data.get("kind") == "file_manifest" and required and "artifact_count" not in data:
            data["artifact_count"] = len(required)
        return data

    @field_validator("required_files")
    @classmethod
    def bounded_safe_file_names(cls, values: list[str]) -> list[str]:
        clean = [normalize_manifest_path(value) for value in values]
        portable_keys = [value.casefold() for value in clean]
        if len(set(portable_keys)) != len(portable_keys):
            raise ValueError("required file paths must be unique after normalization")
        return clean

    @field_validator("format")
    @classmethod
    def normalize_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower().removeprefix(".")
        if not value or not re.fullmatch(r"[a-z0-9][a-z0-9+._-]{0,63}", value):
            raise ValueError("format must be a portable 1-64 character identifier")
        return value

    @field_validator("json_schema")
    @classmethod
    def bounded_schema(cls, value: dict[str, Any] | None):
        if value is None:
            return None
        try:
            raw = json.dumps(value, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"json_schema must contain JSON-compatible values: {exc}") from exc
        if len(raw.encode("utf-8")) > 16_384:
            raise ValueError("json_schema must be 16384 bytes or fewer")
        declared_draft = value.get("$schema")
        if declared_draft and str(declared_draft).rstrip("#") != SUPPORTED_JSON_SCHEMA_DRAFT_V1:
            raise ValueError(
                "json_schema must use the supported JSON Schema draft 2020-12"
            )
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as exc:
            raise ValueError(f"json_schema is malformed: {exc.message}") from exc
        return value

    @model_validator(mode="after")
    def coherent_contract(self):
        if self.kind == "single_artifact" and self.artifact_count != 1:
            raise ValueError("single_artifact contracts require artifact_count=1")
        if self.kind == "file_manifest" and not self.required_files:
            raise ValueError("file_manifest contracts require at least one required file")
        if self.kind == "file_manifest" and self.artifact_count != len(self.required_files):
            raise ValueError("file_manifest artifact_count must equal the exact required_files count")
        if self.kind == "structured_json" and self.format not in (None, "json"):
            raise ValueError("structured_json contracts use format='json'")
        return self


class VerificationPolicyV1(ProtocolModel):
    """Validator aggregation policy.

    Contract-floor validators always use AND semantics. ``require_all`` only
    controls explicitly requested *required* validators: true requires all of
    them, while false requires at least one. Optional validators never decide
    acceptance.
    """

    validators: list[ValidatorSpecV1] = Field(default_factory=list, max_length=8)
    allow_unverified_fallback: bool = True
    require_all: bool = True


class ExecutionRequirementsV1(ProtocolModel):
    required_capabilities: list[str] = Field(default_factory=list, max_length=16)
    approved_node_ids: list[str] = Field(default_factory=list, max_length=32)
    allow_local_fallback: bool = True
    resource_requirements: NodeResourceRequirementsV1 | None = None

    @field_validator("required_capabilities", "approved_node_ids")
    @classmethod
    def bounded_identifiers(cls, values: list[str]) -> list[str]:
        clean = []
        for value in values:
            value = value.strip()
            if not value or len(value) > 128:
                raise ValueError("capability and node identifiers must be 1-128 characters")
            clean.append(value)
        if len(set(clean)) != len(clean):
            raise ValueError("capability and node identifiers must be unique")
        return clean


class ExecutionRequestV1(ProtocolModel):
    protocol_version: Literal["1"] = "1"
    task: str = Field(min_length=1, max_length=1000)
    project_id: str | None = Field(default=None, max_length=128)

    strategy: StrategyNameV1 = "auto"
    strategy_options: StrategyOptionsV1 | None = None
    placement: PlacementV1 = "local"
    remote_dispatch_consent: bool = False

    requirements: ExecutionRequirementsV1 = Field(default_factory=ExecutionRequirementsV1)
    output_contract: OutputContractV1 | None = None
    verification: VerificationPolicyV1 = Field(default_factory=VerificationPolicyV1)

    confidentiality: ConfidentialityV1 = "local_only"
    timeout_seconds: int = Field(default=1800, ge=1, le=7200)
    max_output_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    network_policy: Literal["disabled", "restricted", "allowed"] = "disabled"

    @model_validator(mode="before")
    @classmethod
    def infer_strategy_option_discriminator(cls, data: Any):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        raw = data.get("strategy_options")
        if isinstance(raw, dict) and "kind" not in raw:
            raw = dict(raw)
            strategy = data.get("strategy", "auto")
            ensemble_fields = {"candidates", "concurrency", "selection_policy"}
            dag_fields = {"maximum_subtasks", "review_enabled", "revision_enabled"}
            if strategy in ("ensemble", "direct") or ensemble_fields.intersection(raw):
                raw["kind"] = "ensemble"
            elif strategy == "dag" or dag_fields.intersection(raw):
                raw["kind"] = "dag"
            data["strategy_options"] = raw
        return data

    @field_validator("task")
    @classmethod
    def task_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task cannot be empty")
        return value

    @field_validator("project_id")
    @classmethod
    def project_id_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("project_id cannot be blank")
        return value

    @model_validator(mode="after")
    def strategy_and_placement_are_coherent(self):
        options = self.strategy_options
        if self.strategy == "dag" and options is not None and not isinstance(options, DagOptionsV1):
            raise ValueError("strategy='dag' requires DagOptionsV1")
        if self.strategy in ("ensemble", "direct") and options is not None and not isinstance(
            options, EnsembleOptionsV1
        ):
            raise ValueError(f"strategy='{self.strategy}' requires EnsembleOptionsV1")
        if self.strategy == "direct" and isinstance(options, EnsembleOptionsV1) and options.candidates != 1:
            raise ValueError("strategy='direct' requires candidates=1")
        if self.confidentiality == "local_only" and self.placement == "distributed":
            raise ValueError("confidentiality='local_only' cannot use placement='distributed'")
        if self.confidentiality == "approved_nodes" and not self.requirements.approved_node_ids:
            raise ValueError("confidentiality='approved_nodes' requires approved_node_ids")
        remote_capable = self.placement in ("auto", "distributed") and self.confidentiality != "local_only"
        if remote_capable and not self.remote_dispatch_consent:
            raise ValueError(
                "remote-capable placement requires remote_dispatch_consent=true"
            )
        if self.remote_dispatch_consent and not remote_capable:
            raise ValueError(
                "remote_dispatch_consent=true requires remote-capable placement and confidentiality"
            )
        return self


class ValidationSummaryV1(ProtocolModel):
    outcome: ValidationOutcomeV1 = "not_run"
    assurance_level: AssuranceLevelV1 = "unverified"
    checks_run: list[str] = Field(default_factory=list, max_length=32)
    checks_passed: list[str] = Field(default_factory=list, max_length=32)
    checks_failed: list[str] = Field(default_factory=list, max_length=32)
    checks_not_run: list[str] = Field(default_factory=list, max_length=32)
    proves_behavioral_correctness: bool = False
    explanation: str = Field(
        default="No validation checks were run; the result is unverified.",
        max_length=1000,
    )


class ValidationEvidenceV1(ProtocolModel):
    validator_name: str = Field(min_length=1, max_length=64)
    validator_version: str = Field(min_length=1, max_length=32)
    status: Literal["passed", "failed", "skipped", "error"]
    assurance_level: AssuranceLevelV1 = "unverified"
    proves_behavioral_correctness: bool = False
    requirement_source: RequirementSourceV1 = "explicit"
    required: bool = True
    aggregation: ValidationAggregationV1 = "all"
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    failure_reason: str | None = Field(default=None, max_length=500)
    duration_ms: int = Field(default=0, ge=0)


class ExecutionUnitSummaryV1(ProtocolModel):
    unit_id: str = Field(min_length=1, max_length=128)
    kind: Literal["dag_subtask", "candidate"]
    title: str = Field(min_length=1, max_length=200)
    depends_on: list[str] = Field(default_factory=list, max_length=10)
    status: Literal["queued", "running", "completed", "failed", "cancelled"] = "queued"
    placement: SelectedPlacementV1 | None = None
    node_id: str | None = Field(default=None, max_length=128)
    enrollment_id: str | None = Field(default=None, max_length=64)
    capability_descriptor_version: str | None = Field(default=None, max_length=16)
    capability_descriptor_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    attempt_id: str | None = Field(default=None, max_length=64)
    selected_model_provider: str | None = Field(default=None, max_length=32)
    selected_model_name: str | None = Field(default=None, max_length=128)
    selected_model_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    evidence_role: Literal["production", "sampled_comparison"] | None = None
    attempt_count: int = Field(default=0, ge=0, le=20)
    duration_ms: int = Field(default=0, ge=0)
    fallback_reason: str | None = Field(default=None, max_length=500)


class CandidateSummaryV1(ProtocolModel):
    candidate_id: str = Field(min_length=1, max_length=128)
    status: Literal["completed", "failed", "rejected", "selected", "unverified"]
    output_bytes: int = Field(default=0, ge=0)
    output_preview: str = Field(default="", max_length=500)
    produced_files: list[str] = Field(default_factory=list, max_length=50)
    error: str | None = Field(default=None, max_length=500)
    placement: SelectedPlacementV1 | None = None
    node_id: str | None = Field(default=None, max_length=128)
    enrollment_id: str | None = Field(default=None, max_length=64)
    capability_descriptor_version: str | None = Field(default=None, max_length=16)
    capability_descriptor_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    attempt_id: str | None = Field(default=None, max_length=64)
    selected_model_provider: str | None = Field(default=None, max_length=32)
    selected_model_name: str | None = Field(default=None, max_length=128)
    selected_model_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    evidence_role: Literal["production", "sampled_comparison"] | None = None
    generation_duration_ms: int = Field(default=0, ge=0)
    validation_duration_ms: int = Field(default=0, ge=0)
    validation: list[ValidationEvidenceV1] = Field(default_factory=list, max_length=16)
    validation_outcome: ValidationOutcomeV1 = "not_run"
    assurance_level: AssuranceLevelV1 = "unverified"
    validation_summary: ValidationSummaryV1 = Field(default_factory=ValidationSummaryV1)
    failure_stage: Literal[
        "generation",
        "directory_creation",
        "materialization",
        "artifact_extraction",
        "validation",
        "manifest_creation",
    ] | None = None


class StructuredErrorV1(ProtocolModel):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    unit_id: str | None = Field(default=None, max_length=128)
    retryable: bool = False


class ExecutionResultV1(ProtocolModel):
    execution_id: str = Field(min_length=16, max_length=64)
    job_id: str | None = Field(default=None, max_length=128)
    protocol_version: Literal["1"] = "1"
    # ``status`` is the protocol-v1 compatibility projection. Canonical code
    # must make lifecycle decisions from ``lifecycle_status`` instead.
    status: CompatibilityStatusV1
    lifecycle_status: LifecycleStatusV1 = "queued"
    validation_outcome: ValidationOutcomeV1 = "not_run"
    assurance_level: AssuranceLevelV1 = "unverified"
    validation_summary: ValidationSummaryV1 = Field(default_factory=ValidationSummaryV1)
    task: str = Field(min_length=1, max_length=1000)
    project_id: str | None = Field(default=None, max_length=128)

    strategy_requested: StrategyNameV1
    strategy_selected: SelectedStrategyV1
    strategy_version: str = Field(min_length=1, max_length=32)
    strategy_options: dict[str, Any] = Field(default_factory=dict)
    selector_reason: str = Field(min_length=1, max_length=500)
    selector_version: str = Field(min_length=1, max_length=32)

    placement_requested: PlacementV1
    placement_planned: SelectedPlacementV1 | None = None
    # Historical name retained for clients; it projects the planned placement.
    placement_selected: SelectedPlacementV1 | None = None
    placement_observed: ObservedPlacementV1 = "none"
    observed_placements: list[SelectedPlacementV1] = Field(default_factory=list, max_length=2)
    units_local: int = Field(default=0, ge=0)
    units_distributed: int = Field(default=0, ge=0)
    fallback_count: int = Field(default=0, ge=0)
    attempt_count: int = Field(default=0, ge=0)
    reassignment_count: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    remote_dispatch_consent: bool = False
    fallback_reason: str | None = Field(default=None, max_length=1000)

    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    deadline_at: str | None = None
    cancellation_requested: bool = False
    cancellation_requested_at: str | None = None
    cancellation_reason: str | None = Field(default=None, max_length=1000)
    cancelled_at: str | None = None
    interruption_reason: str | None = Field(default=None, max_length=1000)
    coordinator_restart_marker: str | None = Field(default=None, max_length=128)
    interrupted_at: str | None = None
    retryable: bool = False

    execution_units: list[ExecutionUnitSummaryV1] = Field(default_factory=list, max_length=50)
    candidates: list[CandidateSummaryV1] = Field(default_factory=list, max_length=5)
    winning_candidate: str | None = Field(default=None, max_length=128)
    winner_selection_explanation: str | None = Field(default=None, max_length=1000)
    validation_evidence: list[ValidationEvidenceV1] = Field(default_factory=list, max_length=32)
    review_metadata: dict[str, Any] = Field(default_factory=dict)
    revision_metadata: dict[str, Any] = Field(default_factory=dict)
    produced_files: list[str] = Field(default_factory=list, max_length=100)
    primary_deliverables: list[str] = Field(default_factory=list, max_length=100)
    artifact_manifest_url: str | None = Field(default=None, max_length=500)
    audit_manifest_url: str | None = Field(default=None, max_length=500)
    sealed_manifest_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    artifact_integrity_mode: Literal[
        "none",
        "active",
        "sealed",
        "legacy_live",
        "invalid",
    ] = "none"
    posthoc_verification_status: Literal[
        "disabled",
        "not_requested",
        "pending",
        "running",
        "completed",
        "failed",
    ] = "not_requested"
    posthoc_verification_started_at: str | None = None
    posthoc_verification_completed_at: str | None = None
    posthoc_agreement: bool | None = None
    posthoc_reason: str | None = Field(default=None, max_length=500)
    output_reference: str | None = Field(default=None, max_length=500)
    output_preview: str = Field(default="", max_length=1000)
    participating_nodes: list[str] = Field(default_factory=list, max_length=32)
    credit_records: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    errors: list[StructuredErrorV1] = Field(default_factory=list, max_length=20)
    telemetry: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def derive_lifecycle_compatibility_projection(cls, data: Any):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        status = data.get("status")
        lifecycle = data.get("lifecycle_status")
        if lifecycle is None and status is not None:
            data["lifecycle_status"] = "completed" if status == "unverified" else status
        elif status is None and lifecycle is not None:
            data["status"] = lifecycle
        return data

    @field_validator("observed_placements")
    @classmethod
    def unique_observed_placements(cls, values: list[SelectedPlacementV1]):
        if len(set(values)) != len(values):
            raise ValueError("observed_placements must not contain duplicates")
        return values

    @model_validator(mode="after")
    def lifecycle_projection_is_truthful(self):
        allowed: dict[str, set[str]] = {
            "queued": {"queued"},
            "running": {"running"},
            "completed": {"completed", "unverified"},
            "failed": {"failed"},
            # Older clients understand interrupted work as failed; both
            # projections are accepted while lifecycle remains explicit.
            "interrupted": {"interrupted", "failed"},
            "cancelled": {"cancelled", "failed"},
        }
        if self.status not in allowed[self.lifecycle_status]:
            raise ValueError(
                f"status={self.status!r} is not a valid compatibility projection "
                f"for lifecycle_status={self.lifecycle_status!r}"
            )
        return self
