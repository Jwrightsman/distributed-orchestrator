"""Typed capability claims, snapshots, and deterministic hard matching."""

from __future__ import annotations

import json
import sqlite3

import pytest
from pydantic import ValidationError

from node_capabilities import (
    HardwareDescriptorV1,
    NodeCapabilityDescriptorV1,
    NodeCapabilitySnapshotStore,
    NodeResourceRequirementsV1,
    capability_descriptor_digest,
    canonical_descriptor_json,
    canonical_requirement_digest,
    has_typed_resource_constraints,
    match_node_requirements,
    select_model_for_requirements,
)
from node_enrollments import NodeEnrollmentStore, new_enrollment_credential


MODEL_DIGEST = "sha256:" + "a" * 64


def _descriptor(
    *, max_output_bytes: int = 1_048_576, **overrides
) -> NodeCapabilityDescriptorV1:
    payload = {
        "descriptor_version": "1",
        "executor": {
            "kind": "ollama",
            "version": "0.11.4",
            "worker_protocol_version": "1",
        },
        "models": [
            {
                "provider": "ollama",
                "name": "qwen3.5:4b",
                "digest": MODEL_DIGEST,
                "context_tokens": 16_384,
                "variant": "Q4_K_M",
            }
        ],
        "hardware": {
            "architecture": "x86_64",
            "logical_cpu_count": 8,
            "total_memory_bytes": 16 * 1024**3,
            "gpus": [
                {
                    "vendor": "nvidia",
                    "model": "Example GPU",
                    "memory_bytes": 8 * 1024**3,
                }
            ],
        },
        "features": ["code", "streaming"],
        "limits": {
            "max_concurrent_execution_units": 1,
            "max_output_bytes": max_output_bytes,
            "max_context_tokens": 16_384,
        },
        "isolation": {"kind": "none"},
    }
    payload.update(overrides)
    return NodeCapabilityDescriptorV1.model_validate(payload)


def test_descriptor_is_strict_bounded_and_excludes_stable_hardware_ids():
    with pytest.raises(ValidationError):
        _descriptor(serial_number="not-allowed")
    with pytest.raises(ValidationError):
        _descriptor(features=[f"feature-{index}" for index in range(33)])
    with pytest.raises(ValidationError):
        _descriptor(
            limits={
                "max_concurrent_execution_units": 0,
                "max_output_bytes": 1024,
            }
        )
    with pytest.raises(ValidationError):
        HardwareDescriptorV1.model_validate(
            {"logical_cpu_count": "8", "mac_address": "00:00:00:00:00:00"}
        )


def test_unsupported_versions_have_machine_readable_error_codes():
    with pytest.raises(ValidationError) as descriptor_error:
        _descriptor(descriptor_version="2")
    assert descriptor_error.value.errors()[0]["type"] == (
        "unsupported_capability_descriptor_version"
    )

    with pytest.raises(ValidationError) as requirement_error:
        NodeResourceRequirementsV1.model_validate({"requirement_version": "2"})
    assert requirement_error.value.errors()[0]["type"] == (
        "unsupported_resource_requirement_version"
    )


def test_canonical_descriptor_hash_ignores_object_key_and_set_order():
    descriptor = _descriptor()
    reordered = json.loads(canonical_descriptor_json(descriptor))
    reordered = {key: reordered[key] for key in reversed(reordered)}
    reordered["features"] = list(reversed(reordered["features"]))
    assert canonical_descriptor_json(reordered) == canonical_descriptor_json(descriptor)
    assert capability_descriptor_digest(reordered) == capability_descriptor_digest(
        descriptor
    )


def test_descriptor_change_produces_a_distinct_digest():
    original = _descriptor()
    changed = _descriptor(
        hardware={
            **original.hardware.model_dump(mode="json"),
            "logical_cpu_count": 16,
        }
    )
    assert capability_descriptor_digest(changed) != capability_descriptor_digest(
        original
    )


def test_descriptor_rejects_conflicting_claims_for_one_runnable_model_name():
    with pytest.raises(ValidationError, match="provider/name claims must be unique"):
        _descriptor(
            models=[
                {
                    "provider": "ollama",
                    "name": "same:latest",
                    "digest": "a" * 64,
                },
                {
                    "provider": "ollama",
                    "name": "same:latest",
                    "digest": "b" * 64,
                },
            ]
        )


def test_snapshot_persistence_is_idempotent_and_validates_on_read(tmp_path):
    database = tmp_path / "capabilities.db"
    enrollment = NodeEnrollmentStore(database).bootstrap(
        "worker-a", new_enrollment_credential(), now=10
    ).record
    store = NodeCapabilitySnapshotStore(database)
    descriptor = _descriptor()

    first = store.remember(enrollment.enrollment_id, descriptor, now=20)
    second = store.remember(enrollment.enrollment_id, descriptor, now=30)

    assert first.descriptor_hash == second.descriptor_hash
    assert second.first_seen_at == 20
    assert second.last_seen_at == 30
    assert len(store.list_for_enrollment(enrollment.enrollment_id)) == 1
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM node_capability_snapshots"
        ).fetchone()[0] == 1

        con.execute(
            "UPDATE node_capability_snapshots SET descriptor_json = '{}'"
        )
        con.commit()
    with pytest.raises((RuntimeError, ValidationError)):
        store.get(enrollment.enrollment_id, first.descriptor_hash)


def test_new_descriptor_is_an_additional_immutable_snapshot(tmp_path):
    database = tmp_path / "capabilities.db"
    enrollment = NodeEnrollmentStore(database).bootstrap(
        "worker-a", new_enrollment_credential()
    ).record
    store = NodeCapabilitySnapshotStore(database)
    first = _descriptor()
    second = _descriptor(features=["different"])

    store.remember(enrollment.enrollment_id, first)
    store.remember(enrollment.enrollment_id, second)

    snapshots = store.list_for_enrollment(enrollment.enrollment_id)
    assert {item.descriptor_hash for item in snapshots} == {
        capability_descriptor_digest(first),
        capability_descriptor_digest(second),
    }


def test_exact_digest_requires_an_actual_matching_model_digest():
    required = NodeResourceRequirementsV1(
        acceptable_models=[{"provider": "ollama", "name": "qwen3.5:4b"}],
        exact_model_digest=MODEL_DIGEST,
    )
    match = match_node_requirements(required, [], _descriptor(), [])
    assert match.eligible is True

    unknown = _descriptor(
        models=[
            {
                "provider": "ollama",
                "name": "qwen3.5:4b",
                "digest": None,
                "context_tokens": 16_384,
            }
        ]
    )
    mismatch = match_node_requirements(required, [], unknown, [])
    assert mismatch.eligible is False
    assert mismatch.reason_codes == ("model_digest_mismatch",)


def test_model_selection_uses_the_same_exact_digest_and_context_filters_as_matching():
    alternate_digest = "sha256:" + "b" * 64
    descriptor = _descriptor(
        models=[
            {
                "provider": "ollama",
                "name": "configured:latest",
                "digest": MODEL_DIGEST,
                "context_tokens": 16_384,
            },
            {
                "provider": "ollama",
                "name": "alternate:latest",
                "digest": alternate_digest,
                "context_tokens": 32_768,
            },
        ],
        limits={
            "max_concurrent_execution_units": 1,
            "max_output_bytes": 1_048_576,
            "max_context_tokens": 32_768,
        },
    )
    requirements = NodeResourceRequirementsV1(
        acceptable_models=[
            {"provider": "ollama", "name": "alternate:latest"},
            {"provider": "ollama", "name": "configured:latest"},
        ],
        exact_model_digest=alternate_digest,
        minimum_context_tokens=20_000,
    )

    selected = select_model_for_requirements(
        requirements,
        descriptor,
        preferred_model_name="configured:latest",
    )
    match = match_node_requirements(
        requirements,
        [],
        descriptor,
        [],
        preferred_model_name="configured:latest",
    )

    assert selected is not None
    assert selected.name == "alternate:latest"
    assert selected.digest == alternate_digest
    assert match.eligible is True
    assert match.selected_model == selected


def test_model_selection_preserves_configured_model_when_it_satisfies_constraints():
    descriptor = _descriptor(
        models=[
            {
                "provider": "ollama",
                "name": "a-canonical:latest",
                "context_tokens": 16_384,
            },
            {
                "provider": "ollama",
                "name": "configured:latest",
                "context_tokens": 16_384,
            },
        ]
    )

    selected = select_model_for_requirements(
        None,
        descriptor,
        preferred_model_name="configured:latest",
    )

    assert selected is not None
    assert selected.name == "configured:latest"


def test_supported_executor_and_worker_protocol_match():
    requirements = NodeResourceRequirementsV1(
        allowed_executor_kinds=["ollama"],
        required_worker_protocol_version="1",
    )

    match = match_node_requirements(requirements, [], _descriptor(), [])

    assert match.eligible is True
    assert match.reason_codes == ()


def test_node_output_capacity_above_task_requirement_is_eligible():
    match = match_node_requirements(
        None,
        [],
        _descriptor(max_output_bytes=4096),
        [],
        required_output_capacity_bytes=2048,
    )

    assert match.eligible is True
    assert match.reason_codes == ()


def test_node_output_capacity_equal_to_task_requirement_is_eligible():
    match = match_node_requirements(
        None,
        [],
        _descriptor(max_output_bytes=2048),
        [],
        required_output_capacity_bytes=2048,
    )

    assert match.eligible is True
    assert match.reason_codes == ()


def test_node_output_capacity_below_task_requirement_has_stable_reason():
    match = match_node_requirements(
        None,
        [],
        _descriptor(max_output_bytes=2047),
        [],
        required_output_capacity_bytes=2048,
    )

    assert match.eligible is False
    assert match.reason_codes == ("insufficient_output_capacity",)


def test_descriptorless_compatibility_does_not_fabricate_output_capacity():
    match = match_node_requirements(
        None,
        ["code"],
        None,
        ["code"],
        required_output_capacity_bytes=10_485_760,
    )

    assert match.eligible is True
    assert match.reason_codes == ()


@pytest.mark.parametrize(
    ("requirements", "reason"),
    [
        ({"minimum_logical_cpus": 9}, "insufficient_cpu"),
        ({"minimum_memory_bytes": 17 * 1024**3}, "insufficient_memory"),
        ({"allowed_gpu_vendors": ["amd"]}, "gpu_vendor_mismatch"),
        (
            {"minimum_gpu_memory_bytes": 9 * 1024**3},
            "insufficient_gpu_memory",
        ),
        ({"minimum_context_tokens": 32_768}, "insufficient_context"),
        ({"required_features": ["tool-use"]}, "missing_feature"),
        ({"allowed_isolation_kinds": ["container"]}, "isolation_mismatch"),
        (
            {
                "acceptable_models": [
                    {"provider": "ollama", "name": "different-model"}
                ]
            },
            "model_mismatch",
        ),
    ],
)
def test_each_typed_hard_constraint_has_a_stable_rejection_reason(
    requirements, reason
):
    result = match_node_requirements(requirements, [], _descriptor(), [])
    assert result.eligible is False
    assert reason in result.reason_codes


def test_gpu_requirement_does_not_treat_unknown_detection_as_no_gpu_claim():
    descriptor = _descriptor(
        hardware={
            "architecture": "x86_64",
            "logical_cpu_count": 8,
            "total_memory_bytes": 16 * 1024**3,
            "gpus": None,
        }
    )
    result = match_node_requirements(
        NodeResourceRequirementsV1(gpu_required=True), [], descriptor, []
    )
    assert result.reason_codes == ("gpu_required",)


def test_legacy_and_typed_constraints_are_both_enforced():
    descriptor = _descriptor()
    typed = NodeResourceRequirementsV1(minimum_logical_cpus=4)

    legacy_only = match_node_requirements(None, ["code"], None, ["code"])
    assert legacy_only.eligible is True

    missing_legacy = match_node_requirements(typed, ["special"], descriptor, ["code"])
    assert missing_legacy.reason_codes == ("legacy_capability_missing",)

    missing_typed = match_node_requirements(
        NodeResourceRequirementsV1(minimum_logical_cpus=32),
        ["code"],
        descriptor,
        ["code"],
    )
    assert missing_typed.reason_codes == ("insufficient_cpu",)


def test_requirement_digest_covers_typed_and_legacy_constraints():
    base = canonical_requirement_digest(None, ["code"])
    typed = canonical_requirement_digest(
        NodeResourceRequirementsV1(minimum_logical_cpus=2), ["code"]
    )
    legacy_changed = canonical_requirement_digest(None, ["gpu"])
    assert len(base) == 64
    assert typed != base
    assert legacy_changed != base
    assert canonical_requirement_digest(NodeResourceRequirementsV1(), ["code"]) == base
    assert (
        canonical_requirement_digest(
            NodeResourceRequirementsV1(gpu_required=False), ["code"]
        )
        == base
    )


def test_empty_or_nonrequiring_typed_object_has_no_hard_constraints():
    assert has_typed_resource_constraints(NodeResourceRequirementsV1()) is False
    assert (
        has_typed_resource_constraints(NodeResourceRequirementsV1(gpu_required=False))
        is False
    )
    assert has_typed_resource_constraints(
        NodeResourceRequirementsV1(gpu_required=True)
    ) is True
