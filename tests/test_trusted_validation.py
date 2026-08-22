"""Trusted-alpha validation floors, artifact contracts, and assurance claims."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from execution.contracts import ExecutionRequestV1, ExecutionResultV1
from execution.registry import StrategySelector
from execution.validators import ValidatorRegistry


def _by_name(evidence):
    return {item.validator_name: item for item in evidence}


def _write(root: Path, relative: str, content: str = "x") -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_malformed_json_schema_is_rejected_at_request_validation():
    with pytest.raises(ValidationError, match="json_schema is malformed"):
        ExecutionRequestV1(
            task="return data",
            output_contract={
                "kind": "structured_json",
                "json_schema": {"type": 7},
            },
        )


def test_contract_floor_cannot_be_removed_by_explicit_validators():
    request = ExecutionRequestV1(
        task="return data",
        output_contract={"kind": "structured_json"},
        verification={"validators": [{"name": "nonempty"}]},
    )
    registry = ValidatorRegistry.default()
    evidence = registry.validate(request, "not json", [])

    assert set(_by_name(evidence)) == {"nonempty", "structured_json"}
    assert _by_name(evidence)["structured_json"].requirement_source == "contract_floor"
    assert registry.accepted(evidence) is False


def test_require_all_false_uses_any_for_explicit_required_checks(tmp_path):
    artifact = _write(tmp_path, "result.txt")
    base = {
        "task": "return anything",
        "verification": {
            "validators": [
                {"name": "artifact_extraction"},
                {"name": "structured_json"},
            ],
        },
    }
    registry = ValidatorRegistry.default()

    require_all = ExecutionRequestV1.model_validate(base)
    any_required = ExecutionRequestV1.model_validate(
        {**base, "verification": {**base["verification"], "require_all": False}}
    )
    all_evidence = registry.validate(require_all, "not json", [artifact])
    any_evidence = registry.validate(any_required, "not json", [artifact])

    assert registry.accepted(all_evidence) is False
    assert registry.accepted(any_evidence) is True
    assert _by_name(any_evidence)["artifact_extraction"].aggregation == "any"


def test_require_all_false_never_weakens_contract_floors(tmp_path):
    artifact = _write(tmp_path, "result.txt")
    request = ExecutionRequestV1(
        task="return data",
        output_contract={"kind": "structured_json"},
        verification={
            "require_all": False,
            "validators": [{"name": "artifact_extraction"}],
        },
    )
    registry = ValidatorRegistry.default()
    evidence = registry.validate(request, "not json", [artifact])

    assert _by_name(evidence)["artifact_extraction"].status == "passed"
    assert _by_name(evidence)["structured_json"].aggregation == "all"
    assert registry.accepted(evidence) is False


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.py",
        "C:/absolute.py",
        "src\\config.py",
        "./config.py",
        "src/../config.py",
        "src//config.py",
    ],
)
def test_manifest_rejects_unsafe_or_nonportable_paths(path):
    with pytest.raises(ValidationError, match="required file paths"):
        ExecutionRequestV1(
            task="build files",
            output_contract={"kind": "file_manifest", "required_files": [path]},
        )


def test_manifest_rejects_duplicate_normalized_paths():
    with pytest.raises(ValidationError, match="unique after normalization"):
        ExecutionRequestV1(
            task="build files",
            output_contract={
                "kind": "file_manifest",
                "required_files": ["src/config.py", " SRC/config.py "],
            },
        )


def test_nested_manifest_paths_are_compared_exactly(tmp_path):
    files = [
        _write(tmp_path, "src/config.py", "VALUE = 1"),
        _write(tmp_path, "tests/config.py", "VALUE = 2"),
    ]
    request = ExecutionRequestV1(
        task="build files",
        output_contract={
            "kind": "file_manifest",
            "required_files": ["src/config.py", "tests/config.py"],
        },
    )
    registry = ValidatorRegistry.default()
    evidence = registry.validate(request, "two files", files, artifact_root=tmp_path)

    manifest = _by_name(evidence)["file_manifest"]
    assert request.output_contract.artifact_count == 2
    assert manifest.status == "passed"
    assert manifest.evidence["available"] == ["src/config.py", "tests/config.py"]
    assert registry.accepted(evidence) is True


def test_basename_collision_does_not_satisfy_nested_manifest(tmp_path):
    files = [
        _write(tmp_path, "src/config.py", "VALUE = 1"),
        _write(tmp_path, "other/config.py", "VALUE = 2"),
    ]
    request = ExecutionRequestV1(
        task="build files",
        output_contract={
            "kind": "file_manifest",
            "required_files": ["src/config.py", "tests/config.py"],
        },
    )
    evidence = ValidatorRegistry.default().validate(
        request,
        "two files",
        files,
        artifact_root=tmp_path,
    )
    manifest = _by_name(evidence)["file_manifest"]

    assert manifest.status == "failed"
    assert manifest.evidence["missing"] == ["tests/config.py"]
    assert manifest.evidence["unexpected"] == ["other/config.py"]


def test_single_artifact_contract_enforces_exact_count(tmp_path):
    files = [
        _write(tmp_path, "one.html", "<!doctype html><html></html>"),
        _write(tmp_path, "two.html", "<!doctype html><html></html>"),
    ]
    request = ExecutionRequestV1(
        task="build html",
        output_contract={"kind": "single_artifact", "format": "html"},
    )
    evidence = ValidatorRegistry.default().validate(
        request,
        "two files",
        files,
        artifact_root=tmp_path,
    )

    artifact_contract = _by_name(evidence)["artifact_contract"]
    assert artifact_contract.status == "failed"
    assert "expected exactly 1" in artifact_contract.failure_reason


def test_artifact_contract_enforces_maximum_count(tmp_path):
    files = [_write(tmp_path, f"file-{index}.txt") for index in range(21)]
    request = ExecutionRequestV1(
        task="build files",
        output_contract={"kind": "code", "artifact_count": 20},
    )
    evidence = ValidatorRegistry.default().validate(
        request,
        "many files",
        files,
        artifact_root=tmp_path,
    )

    artifact_contract = _by_name(evidence)["artifact_contract"]
    assert artifact_contract.status == "failed"
    assert "exceeds maximum 20" in artifact_contract.failure_reason


def test_mechanically_checkable_artifact_format_is_required(tmp_path):
    file_path = _write(tmp_path, "main.py", "print('hello')")
    request = ExecutionRequestV1(
        task="build html",
        output_contract={"kind": "single_artifact", "format": "html"},
    )
    evidence = ValidatorRegistry.default().validate(
        request,
        "one file",
        [file_path],
        artifact_root=tmp_path,
    )

    artifact_contract = _by_name(evidence)["artifact_contract"]
    assert artifact_contract.status == "failed"
    assert artifact_contract.evidence["wrong_format"] == ["main.py"]


def test_code_contract_does_not_claim_an_unsupported_parser_ran(tmp_path):
    file_path = _write(tmp_path, "main.js", "console.log('hello')")
    request = ExecutionRequestV1(
        task="build javascript",
        output_contract={"kind": "code", "format": "javascript"},
    )
    registry = ValidatorRegistry.default()
    evidence = registry.validate(
        request,
        "one file",
        [file_path],
        artifact_root=tmp_path,
    )
    parsed = _by_name(evidence)["code_parse"]
    summary = registry.summarize(evidence)

    assert parsed.status == "failed"
    assert parsed.evidence["checked_files"] == []
    assert parsed.evidence["unsupported_files"] == [file_path]
    assert "code_parse:main.js" in summary.checks_not_run


def test_structural_checks_never_claim_behavioral_correctness():
    registry = ValidatorRegistry.default()
    evidence = registry.validate(ExecutionRequestV1(task="write text"), "complete text", [])
    summary = registry.summarize(evidence)

    assert summary.outcome == "passed"
    assert summary.assurance_level == "structural"
    assert summary.proves_behavioral_correctness is False
    assert "behavioral_correctness" in summary.checks_not_run
    assert all(item.proves_behavioral_correctness is False for item in evidence)


def test_json_schema_is_deterministic_contract_assurance_not_behavioral_correctness():
    request = ExecutionRequestV1(
        task="return data",
        output_contract={
            "kind": "structured_json",
            "json_schema": {
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
            },
        },
    )
    registry = ValidatorRegistry.default()
    evidence = registry.validate(request, '{"answer": 42}', [])
    summary = registry.summarize(evidence)

    assert summary.outcome == "passed"
    assert summary.assurance_level == "deterministic"
    assert summary.proves_behavioral_correctness is False
    assert _by_name(evidence)["json_schema"].evidence["claim"] == "contract_conformance"


def test_empty_json_schema_is_a_valid_enforced_schema():
    request = ExecutionRequestV1(
        task="return data",
        output_contract={"kind": "structured_json", "json_schema": {}},
    )
    evidence = ValidatorRegistry.default().validate(request, "42", [])

    assert _by_name(evidence)["json_schema"].status == "passed"


@pytest.mark.parametrize("validator", ["artifact_extraction", "code_parse", "file_manifest"])
def test_auto_selector_does_not_promote_structural_validator_to_correctness(validator):
    request = ExecutionRequestV1(
        task="build one file",
        output_contract={
            "kind": "single_artifact",
            "validators": [{"name": validator}],
        },
    )
    selection = StrategySelector().select(request)

    assert selection.selected == "dag"
    assert "structural only" in selection.reason


def test_unverified_compatibility_status_has_completed_lifecycle():
    result = ExecutionResultV1(
        execution_id="e" * 32,
        status="unverified",
        task="build it",
        strategy_requested="direct",
        strategy_selected="ensemble",
        strategy_version="1",
        selector_reason="test",
        selector_version="test-v1",
        placement_requested="local",
        created_at="2026-08-21T00:00:00+00:00",
    )

    assert result.status == "unverified"
    assert result.lifecycle_status == "completed"
    assert result.assurance_level == "unverified"
    assert result.validation_outcome == "not_run"
