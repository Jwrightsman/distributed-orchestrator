"""Strategy abstraction, registry, and deterministic protocol-v1 selector."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from execution.contracts import (
    DagOptionsV1,
    EnsembleOptionsV1,
    ExecutionRequestV1,
    SelectedStrategyV1,
    StrategyOptionsV1,
)

SELECTOR_VERSION_V1 = "conservative-v1"


@dataclass
class StrategyOutcome:
    status: str
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


_DETERMINISTIC_VALIDATORS = {
    "structured_json",
    "json_schema",
    "file_manifest",
    "code_parse",
    "artifact_extraction",
}


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

        if isinstance(options, EnsembleOptionsV1) and options.candidates == 1:
            return StrategySelection(
                "ensemble",
                options,
                "Selected ensemble with one candidate because candidates=1 is a direct execution request.",
            )

        contract = request.output_contract
        validator_names = {v.name for v in request.verification.validators}
        if contract:
            validator_names.update(v.name for v in contract.validators)
        has_deterministic = bool(validator_names.intersection(_DETERMINISTIC_VALIDATORS))
        if contract and contract.kind == "single_artifact" and has_deterministic:
            selected_options = options if isinstance(options, EnsembleOptionsV1) else EnsembleOptionsV1()
            return StrategySelection(
                "ensemble",
                selected_options,
                "Selected ensemble because a single-artifact output contract and deterministic validator were supplied.",
            )

        selected_options = options if isinstance(options, DagOptionsV1) else DagOptionsV1()
        return StrategySelection(
            "dag",
            selected_options,
            "Selected DAG because no single-artifact output contract with a deterministic validator was supplied.",
        )
