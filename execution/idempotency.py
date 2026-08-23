"""Digest-only identity for retry-safe canonical execution submission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from execution.contracts import ExecutionRequestV1

MAX_IDEMPOTENCY_KEY_LENGTH = 128

_KEY_DOMAIN = b"mycelium:execution-idempotency-key:v1\0"
_SCOPE_DOMAIN = b"mycelium:execution-requester-scope:v1\0"


class InvalidIdempotencyKey(ValueError):
    """The supplied HTTP idempotency key is outside the canonical contract."""


@dataclass(frozen=True)
class SubmissionIdentity:
    """Only irreversible digests cross the service/persistence boundary."""

    requester_scope_hash: str
    idempotency_key_hash: str
    request_hash: str

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


def canonical_request_json(request: ExecutionRequestV1) -> str:
    """Serialize the validated model, including defaults, deterministically."""

    return json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_request_digest(request: ExecutionRequestV1) -> str:
    return hashlib.sha256(canonical_request_json(request).encode("utf-8")).hexdigest()


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

    return SubmissionIdentity(
        requester_scope_hash=requester_scope_digest(
            requester_scope_kind,
            requester_scope_value,
        ),
        idempotency_key_hash=idempotency_key_digest(idempotency_key),
        request_hash=canonical_request_digest(request),
    )
