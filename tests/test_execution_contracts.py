"""Protocol-v1 request bounds, option discrimination, and selector policy."""

import pytest
from pydantic import ValidationError

from execution.contracts import (
    DagOptionsV1,
    EnsembleOptionsV1,
    ExecutionRequestV1,
)
from execution.registry import StrategySelector
from execution.persistence import ExecutionStore
from execution.service import ExecutionService


def test_valid_v1_request_uses_bounded_defaults():
    request = ExecutionRequestV1(task="Build something", project_id="example")
    assert request.protocol_version == "1"
    assert request.strategy == "auto"
    assert request.placement == "local"
    assert request.confidentiality == "local_only"
    assert request.remote_dispatch_consent is False
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
    assert "structural only" in selection.reason


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


def test_auto_does_not_treat_code_parse_as_correctness_evidence():
    request = ExecutionRequestV1(
        task="x",
        output_contract={
            "kind": "single_artifact",
            "validators": [{"name": "code_parse"}],
        },
    )
    selection = StrategySelector().select(request)
    assert selection.selected == "dag"
    assert selection.selector_version == "conservative-v2"
    assert "structural only" in selection.reason


def test_auto_can_use_deterministic_schema_conformance_without_claiming_correctness():
    request = ExecutionRequestV1(
        task="x",
        output_contract={
            "kind": "structured_json",
            "json_schema": {"type": "object"},
        },
    )
    selection = StrategySelector().select(request)
    assert selection.selected == "ensemble"
    assert "contract conformance" in selection.reason
    assert "does not establish behavioral correctness" in selection.reason


def test_ambiguous_auto_defaults_to_dag_with_reason():
    selection = StrategySelector().select(ExecutionRequestV1(task="x"))
    assert selection.selected == "dag"
    assert selection.reason
    assert selection.selector_version


def test_auto_with_explicit_ensemble_options_selects_ensemble():
    request = ExecutionRequestV1(
        task="x",
        strategy="auto",
        strategy_options={"candidates": 3, "concurrency": 2},
    )
    selection = StrategySelector().select(request)
    assert selection.selected == "ensemble"
    assert selection.options.candidates == 3
    assert "explicit ensemble strategy options" in selection.reason


def test_remote_capable_canonical_request_requires_explicit_consent():
    with pytest.raises(ValidationError, match="remote_dispatch_consent"):
        ExecutionRequestV1(
            task="x",
            placement="auto",
            confidentiality="trusted_guild",
        )

    request = ExecutionRequestV1(
        task="x",
        placement="distributed",
        confidentiality="trusted_guild",
        remote_dispatch_consent=True,
    )
    assert request.remote_dispatch_consent is True


def test_remote_consent_cannot_be_recorded_for_a_local_only_request():
    with pytest.raises(ValidationError, match="remote_dispatch_consent"):
        ExecutionRequestV1(task="x", remote_dispatch_consent=True)


def test_trusted_alpha_reports_sampled_posthoc_verification_as_disabled(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "execution.service.get_config",
        lambda: {"deployment_mode": "trusted_alpha"},
    )
    service = ExecutionService(store=ExecutionStore(tmp_path / "events.db"))
    result = service._new_result(
        ExecutionRequestV1(task="x"),
        "a" * 32,
        None,
        "queued",
    )
    assert result.posthoc_verification_status == "disabled"
    assert "durable post-hoc semantics" in result.posthoc_reason
    assert result.validation_outcome == "not_run"
    assert result.assurance_level == "unverified"
