"""Bounded Pydantic contracts for execution protocol version 1."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

StrategyNameV1 = Literal["auto", "dag", "ensemble", "direct"]
SelectedStrategyV1 = Literal["dag", "ensemble"]
PlacementV1 = Literal["auto", "local", "distributed"]
SelectedPlacementV1 = Literal["local", "distributed"]
ConfidentialityV1 = Literal[
    "local_only",
    "trusted_guild",
    "approved_nodes",
    "public",
]


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
    ]
    required: bool = True
    minimum_score: float | None = Field(default=None, ge=0.0, le=1.0)


class OutputContractV1(ProtocolModel):
    kind: Literal["text", "single_artifact", "structured_json", "file_manifest", "code"] = "text"
    artifact_count: int = Field(default=1, ge=1, le=20)
    format: str | None = Field(default=None, max_length=64)
    required_files: list[str] = Field(default_factory=list, max_length=50)
    json_schema: dict[str, Any] | None = None
    validators: list[ValidatorSpecV1] = Field(default_factory=list, max_length=8)

    @field_validator("required_files")
    @classmethod
    def bounded_safe_file_names(cls, values: list[str]) -> list[str]:
        clean: list[str] = []
        for value in values:
            value = value.strip().replace("\\", "/")
            if not value or len(value) > 200:
                raise ValueError("required file names must be 1-200 characters")
            if value.startswith("/") or ".." in value.split("/"):
                raise ValueError("required file names must be relative and cannot contain '..'")
            clean.append(value)
        if len(set(clean)) != len(clean):
            raise ValueError("required file names must be unique")
        return clean

    @field_validator("json_schema")
    @classmethod
    def bounded_schema(cls, value: dict[str, Any] | None):
        if value is not None and len(json.dumps(value, separators=(",", ":"))) > 16_384:
            raise ValueError("json_schema must be 16384 bytes or fewer")
        return value

    @model_validator(mode="after")
    def coherent_contract(self):
        if self.kind == "single_artifact" and self.artifact_count != 1:
            raise ValueError("single_artifact contracts require artifact_count=1")
        if self.kind == "file_manifest" and not self.required_files:
            raise ValueError("file_manifest contracts require at least one required file")
        if self.kind == "structured_json" and self.format not in (None, "json"):
            raise ValueError("structured_json contracts use format='json'")
        return self


class VerificationPolicyV1(ProtocolModel):
    validators: list[ValidatorSpecV1] = Field(default_factory=list, max_length=8)
    allow_unverified_fallback: bool = True
    require_all: bool = True


class ExecutionRequirementsV1(ProtocolModel):
    required_capabilities: list[str] = Field(default_factory=list, max_length=16)
    approved_node_ids: list[str] = Field(default_factory=list, max_length=32)
    allow_local_fallback: bool = True

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
    placement: PlacementV1 = "auto"

    requirements: ExecutionRequirementsV1 = Field(default_factory=ExecutionRequirementsV1)
    output_contract: OutputContractV1 | None = None
    verification: VerificationPolicyV1 = Field(default_factory=VerificationPolicyV1)

    confidentiality: ConfidentialityV1 = "trusted_guild"
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
        return self


class ValidationEvidenceV1(ProtocolModel):
    validator_name: str = Field(min_length=1, max_length=64)
    validator_version: str = Field(min_length=1, max_length=32)
    status: Literal["passed", "failed", "skipped", "error"]
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    failure_reason: str | None = Field(default=None, max_length=500)
    duration_ms: int = Field(default=0, ge=0)


class ExecutionUnitSummaryV1(ProtocolModel):
    unit_id: str = Field(min_length=1, max_length=128)
    kind: Literal["dag_subtask", "candidate"]
    title: str = Field(min_length=1, max_length=200)
    depends_on: list[str] = Field(default_factory=list, max_length=10)
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    placement: SelectedPlacementV1 | None = None
    node_id: str | None = Field(default=None, max_length=128)
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
    generation_duration_ms: int = Field(default=0, ge=0)
    validation_duration_ms: int = Field(default=0, ge=0)
    validation: list[ValidationEvidenceV1] = Field(default_factory=list, max_length=16)


class StructuredErrorV1(ProtocolModel):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    unit_id: str | None = Field(default=None, max_length=128)
    retryable: bool = False


class ExecutionResultV1(ProtocolModel):
    execution_id: str = Field(min_length=16, max_length=64)
    job_id: str | None = Field(default=None, max_length=128)
    protocol_version: Literal["1"] = "1"
    status: Literal["queued", "running", "completed", "failed", "unverified"]
    task: str = Field(min_length=1, max_length=1000)
    project_id: str | None = Field(default=None, max_length=128)

    strategy_requested: StrategyNameV1
    strategy_selected: SelectedStrategyV1
    strategy_version: str = Field(min_length=1, max_length=32)
    strategy_options: dict[str, Any] = Field(default_factory=dict)
    selector_reason: str = Field(min_length=1, max_length=500)
    selector_version: str = Field(min_length=1, max_length=32)

    placement_requested: PlacementV1
    placement_selected: SelectedPlacementV1 | None = None
    fallback_reason: str | None = Field(default=None, max_length=1000)

    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)

    execution_units: list[ExecutionUnitSummaryV1] = Field(default_factory=list, max_length=50)
    candidates: list[CandidateSummaryV1] = Field(default_factory=list, max_length=5)
    winning_candidate: str | None = Field(default=None, max_length=128)
    winner_selection_explanation: str | None = Field(default=None, max_length=1000)
    validation_evidence: list[ValidationEvidenceV1] = Field(default_factory=list, max_length=32)
    review_metadata: dict[str, Any] = Field(default_factory=dict)
    revision_metadata: dict[str, Any] = Field(default_factory=dict)
    produced_files: list[str] = Field(default_factory=list, max_length=100)
    output_reference: str | None = Field(default=None, max_length=500)
    output_preview: str = Field(default="", max_length=1000)
    participating_nodes: list[str] = Field(default_factory=list, max_length=32)
    credit_records: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    errors: list[StructuredErrorV1] = Field(default_factory=list, max_length=20)
    telemetry: dict[str, Any] = Field(default_factory=dict)
