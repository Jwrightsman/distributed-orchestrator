"""Protocol-v1 request bounds, option discrimination, and selector policy."""

import pytest
from pydantic import ValidationError

from execution.contracts import (
    DagOptionsV1,
    EnsembleOptionsV1,
    ExecutionRequestV1,
)
from execution.registry import StrategySelector


def test_valid_v1_request_uses_bounded_defaults():
    request = ExecutionRequestV1(task="Build something", project_id="example")
    assert request.protocol_version == "1"
    assert request.strategy == "auto"
    assert request.placement == "auto"
    assert request.timeout_seconds <= 7200


def test_invalid_protocol_version_fails_loudly():
    with pytest.raises(ValidationError, match="protocol_version"):
        ExecutionRequestV1(protocol_version="2", task="x")


def test_unknown_strategy_is_not_silently_defaulted():
    with pytest.raises(ValidationError, match="strategy"):
        ExecutionRequestV1(task="x", strategy="debate")


@pytest.mark.parametrize("count", [0, 6, 100])
def test_candidate_count_is_bounded_one_through_five(count):
    with pytest.raises(ValidationError, match="candidates"):
        ExecutionRequestV1(
            task="x",
            strategy="ensemble",
            strategy_options={"candidates": count},
        )


def test_strategy_options_are_discriminated_and_inferred_ergonomically():
    dag = ExecutionRequestV1(
        task="x",
        strategy="dag",
        strategy_options={"maximum_subtasks": 3},
    )
    ensemble = ExecutionRequestV1(
        task="x",
        strategy="ensemble",
        strategy_options={"candidates": 2, "concurrency": 2},
    )
    assert isinstance(dag.strategy_options, DagOptionsV1)
    assert isinstance(ensemble.strategy_options, EnsembleOptionsV1)


def test_wrong_option_family_is_rejected():
    with pytest.raises(ValidationError, match="requires DagOptionsV1"):
        ExecutionRequestV1(
            task="x",
            strategy="dag",
            strategy_options={"kind": "ensemble", "candidates": 2},
        )


def test_direct_normalizes_to_ensemble_with_one_candidate():
    request = ExecutionRequestV1(task="x", strategy="direct")
    selection = StrategySelector().select(request)
    assert request.strategy == "direct"
    assert selection.selected == "ensemble"
    assert selection.options.candidates == 1
    assert "Normalized direct" in selection.reason


def test_legacy_shaped_request_preserves_dag_behavior():
    request = ExecutionRequestV1.model_validate({"task": "Build something", "project_id": "example"})
    selection = StrategySelector().select(request)
    assert selection.selected == "dag"
    assert "no single-artifact" in selection.reason


def test_local_only_cannot_explicitly_dispatch_to_nodes():
    with pytest.raises(ValidationError, match="local_only"):
        ExecutionRequestV1(task="x", confidentiality="local_only", placement="distributed")


def test_approved_nodes_requires_a_bounded_allowlist():
    with pytest.raises(ValidationError, match="approved_node_ids"):
        ExecutionRequestV1(task="x", confidentiality="approved_nodes")


def test_explicit_strategy_always_wins():
    request = ExecutionRequestV1(
        task="x",
        strategy="dag",
        output_contract={
            "kind": "single_artifact",
            "validators": [{"name": "code_parse"}],
        },
    )
    assert StrategySelector().select(request).selected == "dag"


def test_auto_selects_ensemble_only_for_explicit_deterministic_contract():
    request = ExecutionRequestV1(
        task="x",
        output_contract={
            "kind": "single_artifact",
            "validators": [{"name": "code_parse"}],
        },
    )
    selection = StrategySelector().select(request)
    assert selection.selected == "ensemble"
    assert selection.selector_version == "conservative-v1"
    assert selection.reason


def test_ambiguous_auto_defaults_to_dag_with_reason():
    selection = StrategySelector().select(ExecutionRequestV1(task="x"))
    assert selection.selected == "dag"
    assert selection.reason
    assert selection.selector_version
