"""Digest-only identity for retry-safe canonical execution submission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from execution.contracts import ExecutionRequestV1
from node_capabilities import has_typed_resource_constraints

MAX_IDEMPOTENCY_KEY_LENGTH = 128
REQUEST_HASH_VERSION_V1 = "1"
REQUEST_HASH_VERSION_V2 = "2"
SUPPORTED_REQUEST_HASH_VERSIONS = frozenset(
    {REQUEST_HASH_VERSION_V1, REQUEST_HASH_VERSION_V2}
)

_KEY_DOMAIN = b"mycelium:execution-idempotency-key:v1\0"
_SCOPE_DOMAIN = b"mycelium:execution-requester-scope:v1\0"

_REQUEST_V1_FIELDS = (
    "protocol_version",
    "task",
    "project_id",
    "strategy",
    "strategy_options",
    "placement",
    "remote_dispatch_consent",
    "requirements",
    "output_contract",
    "verification",
    "confidentiality",
    "timeout_seconds",
    "max_output_bytes",
    "network_policy",
)
_REQUIREMENTS_V1_FIELDS = (
    "required_capabilities",
    "approved_node_ids",
    "allow_local_fallback",
)
_DAG_OPTIONS_V1_FIELDS = (
    "kind",
    "maximum_subtasks",
    "review_enabled",
    "revision_enabled",
)
_ENSEMBLE_OPTIONS_V1_FIELDS = (
    "kind",
    "candidates",
    "concurrency",
    "selection_policy",
)
_VALIDATOR_SPEC_V1_FIELDS = ("name", "required", "minimum_score")
_OUTPUT_CONTRACT_V1_FIELDS = (
    "kind",
    "artifact_count",
    "format",
    "required_files",
    "json_schema",
    "validators",
)
_VERIFICATION_POLICY_V1_FIELDS = (
    "validators",
    "allow_unverified_fallback",
    "require_all",
)
_RESOURCE_REQUIREMENTS_V1_FIELDS = (
    "requirement_version",
    "allowed_executor_kinds",
    "required_worker_protocol_version",
    "acceptable_models",
    "exact_model_digest",
    "minimum_logical_cpus",
    "minimum_memory_bytes",
    "gpu_required",
    "allowed_gpu_vendors",
    "minimum_gpu_memory_bytes",
    "minimum_context_tokens",
    "required_features",
    "allowed_isolation_kinds",
)
_ACCEPTABLE_MODEL_V1_FIELDS = ("provider", "name")


class InvalidIdempotencyKey(ValueError):
    """The supplied HTTP idempotency key is outside the canonical contract."""


class UnsupportedRequestHashVersion(ValueError):
    """The durable mapping names a serializer this process does not support."""


class RequestHashVersionIncompatible(ValueError):
    """A request uses fields that an older canonical serializer cannot represent."""


@dataclass(frozen=True)
class SubmissionIdentity:
    """Only irreversible digests cross the service/persistence boundary."""

    requester_scope_hash: str
    idempotency_key_hash: str
    request_hash: str
    request_hash_version: str = REQUEST_HASH_VERSION_V1

    def __post_init__(self) -> None:
        for field_name in (
            "requester_scope_hash",
            "idempotency_key_hash",
            "request_hash",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError(
                    f"{field_name} must be a lowercase hexadecimal SHA-256 digest"
                )
        if (
            not isinstance(self.request_hash_version, str)
            or self.request_hash_version not in SUPPORTED_REQUEST_HASH_VERSIONS
        ):
            raise UnsupportedRequestHashVersion(
                f"unsupported request hash version: {self.request_hash_version!r}"
            )


def validate_idempotency_key(value: str) -> str:
    """Validate without normalizing an otherwise valid caller-selected key."""

    if not value or len(value) > MAX_IDEMPOTENCY_KEY_LENGTH or not value.strip():
        raise InvalidIdempotencyKey(
            "Idempotency-Key must be 1-128 printable ASCII characters and not whitespace-only."
        )
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        raise InvalidIdempotencyKey(
            "Idempotency-Key must be 1-128 printable ASCII characters and not whitespace-only."
        )
    return value


def request_hash_version(request: ExecutionRequestV1) -> str:
    """Select v2 only when the request contains an effective typed constraint."""

    if has_typed_resource_constraints(request.requirements.resource_requirements):
        return REQUEST_HASH_VERSION_V2
    return REQUEST_HASH_VERSION_V1


def _selected_fields(payload: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: payload[field] for field in fields}


def _canonical_legacy_request_payload(request: ExecutionRequestV1) -> dict[str, Any]:
    payload = _selected_fields(
        request.model_dump(mode="json"),
        _REQUEST_V1_FIELDS,
    )
    payload["requirements"] = _selected_fields(
        payload["requirements"],
        _REQUIREMENTS_V1_FIELDS,
    )

    strategy_options = payload["strategy_options"]
    if strategy_options is not None:
        option_fields = (
            _DAG_OPTIONS_V1_FIELDS
            if strategy_options["kind"] == "dag"
            else _ENSEMBLE_OPTIONS_V1_FIELDS
        )
        payload["strategy_options"] = _selected_fields(strategy_options, option_fields)

    output_contract = payload["output_contract"]
    if output_contract is not None:
        payload["output_contract"] = _selected_fields(
            output_contract,
            _OUTPUT_CONTRACT_V1_FIELDS,
        )
        payload["output_contract"]["validators"] = [
            _selected_fields(validator, _VALIDATOR_SPEC_V1_FIELDS)
            for validator in output_contract["validators"]
        ]

    verification = _selected_fields(
        payload["verification"],
        _VERIFICATION_POLICY_V1_FIELDS,
    )
    verification["validators"] = [
        _selected_fields(validator, _VALIDATOR_SPEC_V1_FIELDS)
        for validator in payload["verification"]["validators"]
    ]
    payload["verification"] = verification
    return payload


def _canonical_request_payload_v1(request: ExecutionRequestV1) -> dict[str, Any]:
    """Project exactly the pre-Theme-2 validated request shape.

    Version 1 deliberately excludes typed resource requirements. Keeping an
    explicit projection prevents later defaulted protocol fields from silently
    redefining hashes already stored by the original serializer.
    """

    if has_typed_resource_constraints(request.requirements.resource_requirements):
        raise RequestHashVersionIncompatible(
            "request hash version 1 cannot represent typed resource requirements"
        )
    return _canonical_legacy_request_payload(request)


def _canonical_request_payload_v2(request: ExecutionRequestV1) -> dict[str, Any]:
    """Freeze the Theme-2 request shape, including its typed requirement block."""

    payload = _canonical_legacy_request_payload(request)
    requirements = request.requirements.resource_requirements
    if requirements is None:
        payload["requirements"]["resource_requirements"] = None
        return payload

    typed_payload = _selected_fields(
        requirements.model_dump(mode="json"),
        _RESOURCE_REQUIREMENTS_V1_FIELDS,
    )
    if typed_payload["acceptable_models"] is not None:
        typed_payload["acceptable_models"] = [
            _selected_fields(model, _ACCEPTABLE_MODEL_V1_FIELDS)
            for model in typed_payload["acceptable_models"]
        ]
    payload["requirements"]["resource_requirements"] = typed_payload
    return payload


def canonical_request_json(
    request: ExecutionRequestV1,
    *,
    hash_version: str | None = None,
) -> str:
    """Serialize one validated request under an explicit deterministic version."""

    selected_version = (
        request_hash_version(request) if hash_version is None else hash_version
    )
    if selected_version == REQUEST_HASH_VERSION_V1:
        payload = _canonical_request_payload_v1(request)
    elif selected_version == REQUEST_HASH_VERSION_V2:
        payload = _canonical_request_payload_v2(request)
    else:
        raise UnsupportedRequestHashVersion(
            f"unsupported request hash version: {selected_version!r}"
        )

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_request_digest(
    request: ExecutionRequestV1,
    *,
    hash_version: str | None = None,
) -> str:
    return hashlib.sha256(
        canonical_request_json(request, hash_version=hash_version).encode("utf-8")
    ).hexdigest()


def requester_scope_digest(scope_kind: str, value: str) -> str:
    """Hash a configured credential or direct peer address in separate domains."""

    if scope_kind not in {"pitch-key", "peer-host"}:
        raise ValueError("unsupported requester scope kind")
    material = scope_kind.encode("ascii") + b"\0" + value.encode("utf-8")
    return hashlib.sha256(_SCOPE_DOMAIN + material).hexdigest()


def idempotency_key_digest(value: str) -> str:
    validated = validate_idempotency_key(value)
    return hashlib.sha256(_KEY_DOMAIN + validated.encode("ascii")).hexdigest()


def submission_identity(
    request: ExecutionRequestV1,
    *,
    idempotency_key: str,
    requester_scope_kind: str,
    requester_scope_value: str,
) -> SubmissionIdentity:
    """Build the digest-only identity passed to durable submission storage."""

    hash_version = request_hash_version(request)

    return SubmissionIdentity(
        requester_scope_hash=requester_scope_digest(
            requester_scope_kind,
            requester_scope_value,
        ),
        idempotency_key_hash=idempotency_key_digest(idempotency_key),
        request_hash=canonical_request_digest(request, hash_version=hash_version),
        request_hash_version=hash_version,
    )
