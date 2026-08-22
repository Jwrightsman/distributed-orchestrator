"""Strategy abstraction, registry, and deterministic protocol-v1 selector."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from execution.contracts import (
    AssuranceLevelV1,
    CompatibilityStatusV1,
    DagOptionsV1,
    EnsembleOptionsV1,
    ExecutionRequestV1,
    LifecycleStatusV1,
    SelectedStrategyV1,
    StrategyOptionsV1,
    ValidationOutcomeV1,
    ValidationSummaryV1,
)

SELECTOR_VERSION_V1 = "conservative-v2"


@dataclass
class StrategyOutcome:
    # ``status`` remains as a temporary compatibility input for strategy
    # adapters. Canonical lifecycle logic consumes ``lifecycle_status``.
    status: CompatibilityStatusV1 | None = None
    lifecycle_status: LifecycleStatusV1 = "completed"
    validation_outcome: ValidationOutcomeV1 = "not_run"
    assurance_level: AssuranceLevelV1 = "unverified"
    validation_summary: ValidationSummaryV1 = field(default_factory=ValidationSummaryV1)
    legacy_payload: dict[str, Any] = field(default_factory=dict)
    execution_units: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    validation_evidence: list[dict[str, Any]] = field(default_factory=list)
    winning_candidate: str | None = None
    winner_selection_explanation: str | None = None
    produced_files: list[str] = field(default_factory=list)
    output_reference: str | None = None
    output_preview: str = ""
    review_metadata: dict[str, Any] = field(default_factory=dict)
    revision_metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    telemetry: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status is not None:
            self.lifecycle_status = "completed" if self.status == "unverified" else self.status
        else:
            self.status = self.lifecycle_status


class ExecutionStrategy(ABC):
    identifier: SelectedStrategyV1
    version: str

    @abstractmethod
    async def execute(self, request: ExecutionRequestV1, options: StrategyOptionsV1, context) -> StrategyOutcome:
        """Compile, dispatch, validate, and reduce one strategy run."""


class StrategyRegistry:
    def __init__(self):
        self._strategies: dict[str, ExecutionStrategy] = {}

    def register(self, strategy: ExecutionStrategy) -> None:
        if strategy.identifier in self._strategies:
            raise ValueError(f"strategy already registered: {strategy.identifier}")
        self._strategies[strategy.identifier] = strategy

    def get(self, identifier: str) -> ExecutionStrategy:
        try:
            return self._strategies[identifier]
        except KeyError as exc:
            known = ", ".join(sorted(self._strategies))
            raise ValueError(f"unknown strategy '{identifier}'; supported strategies: {known}") from exc

    def identifiers(self) -> tuple[str, ...]:
        return tuple(sorted(self._strategies))


@dataclass(frozen=True)
class StrategySelection:
    selected: SelectedStrategyV1
    options: StrategyOptionsV1
    reason: str
    selector_version: str = SELECTOR_VERSION_V1


_DETERMINISTIC_CONTRACT_VALIDATORS = {"json_schema"}


class StrategySelector:
    """Conservative, deterministic selector. It never calls a model."""

    def select(self, request: ExecutionRequestV1) -> StrategySelection:
        requested = request.strategy
        options = request.strategy_options

        if requested == "dag":
            selected_options = options if isinstance(options, DagOptionsV1) else DagOptionsV1()
            return StrategySelection(
                "dag",
                selected_options,
                "Selected DAG because the caller explicitly requested strategy='dag'.",
            )

        if requested == "ensemble":
            selected_options = options if isinstance(options, EnsembleOptionsV1) else EnsembleOptionsV1()
            return StrategySelection(
                "ensemble",
                selected_options,
                "Selected ensemble because the caller explicitly requested strategy='ensemble'.",
            )

        if requested == "direct":
            supplied = options if isinstance(options, EnsembleOptionsV1) else EnsembleOptionsV1(candidates=1)
            selected_options = EnsembleOptionsV1(
                candidates=1,
                concurrency=1,
                selection_policy=supplied.selection_policy,
            )
            return StrategySelection(
                "ensemble",
                selected_options,
                "Normalized direct to ensemble with one complete candidate.",
            )

        if isinstance(options, EnsembleOptionsV1):
            if options.candidates == 1:
                reason = "Selected ensemble with one candidate because candidates=1 is a direct execution request."
            else:
                reason = "Selected ensemble because the caller supplied explicit ensemble strategy options."
            return StrategySelection("ensemble", options, reason)

        contract = request.output_contract
        validator_names = {v.name for v in request.verification.validators}
        if contract:
            validator_names.update(v.name for v in contract.validators)
            if contract.json_schema is not None:
                validator_names.add("json_schema")
        has_deterministic_contract_check = bool(
            validator_names.intersection(_DETERMINISTIC_CONTRACT_VALIDATORS)
        )
        if contract and has_deterministic_contract_check:
            selected_options = options if isinstance(options, EnsembleOptionsV1) else EnsembleOptionsV1()
            return StrategySelection(
                "ensemble",
                selected_options,
                "Selected ensemble because candidates can be compared using deterministic JSON Schema "
                "contract conformance. This does not establish behavioral correctness.",
            )

        selected_options = options if isinstance(options, DagOptionsV1) else DagOptionsV1()
        return StrategySelection(
            "dag",
            selected_options,
            "Selected DAG because no deterministic contract-conformance check suitable for candidate "
            "comparison was supplied; extraction, parsing, and manifest checks are structural only.",
        )
