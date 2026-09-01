"""Strict, bounded protocol shared by the validator parent and child process.

The protocol intentionally describes only built-in validators.  It has no
module, callable, executable, environment, credential, task, or database
fields.  Resource settings are numeric, parent-clamped ceilings rather than a
general child configuration channel.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal, Mapping, TypeAlias, TypeVar, overload

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


VALIDATOR_RUNNER_PROTOCOL_VERSION_V1 = "1"
VALIDATOR_RUNNER_PROTOCOL_VERSION_V2 = "2"

# The output path is protocol-owned rather than caller-selected.  Candidate
# artifact names are forbidden from occupying this directory in V2 requests.
VALIDATOR_OUTPUT_RESERVED_DIRECTORY_V2 = "__mycelium_validator_input__"
VALIDATOR_OUTPUT_RESERVED_NAMESPACE_V2 = VALIDATOR_OUTPUT_RESERVED_DIRECTORY_V2
VALIDATOR_OUTPUT_REFERENCE_PATH_V2 = (
    f"{VALIDATOR_OUTPUT_RESERVED_DIRECTORY_V2}/output.utf8"
)

VALIDATOR_VERSIONS_V1: Mapping[str, str] = {
    "nonempty": "2",
    "structured_json": "2",
    "json_schema": "2",
    "file_manifest": "2",
    "code_parse": "2",
    "artifact_extraction": "2",
    "artifact_contract": "1",
}
VALIDATOR_VERSIONS_V2: Mapping[str, str] = VALIDATOR_VERSIONS_V1

ValidatorNameV1 = Literal[
    "nonempty",
    "structured_json",
    "json_schema",
    "file_manifest",
    "code_parse",
    "artifact_extraction",
    "artifact_contract",
]
ValidatorNameV2 = ValidatorNameV1

MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V1 = 16 * 1024 * 1024
MAX_VALIDATOR_RUNNER_RESPONSE_BYTES_V1 = 256 * 1024
MAX_VALIDATOR_OUTPUT_BYTES_V1 = 10 * 1024 * 1024
MAX_VALIDATOR_SCHEMA_BYTES_V1 = 16 * 1024
MAX_VALIDATOR_STAGED_FILES_V1 = 20
MAX_VALIDATOR_STAGED_PATH_LENGTH_V1 = 200
MAX_VALIDATOR_FAILURE_REASON_LENGTH_V1 = 500

# V2 changes transport, not these protocol-wide ceilings.  The request limit
# applies to the JSON control envelope; output bytes are independently bounded
# by the output reference below.
MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V2 = MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V1
MAX_VALIDATOR_RUNNER_RESPONSE_BYTES_V2 = MAX_VALIDATOR_RUNNER_RESPONSE_BYTES_V1
MAX_VALIDATOR_OUTPUT_BYTES_V2 = MAX_VALIDATOR_OUTPUT_BYTES_V1
MAX_VALIDATOR_SCHEMA_BYTES_V2 = MAX_VALIDATOR_SCHEMA_BYTES_V1
MAX_VALIDATOR_STAGED_FILES_V2 = MAX_VALIDATOR_STAGED_FILES_V1
MAX_VALIDATOR_STAGED_PATH_LENGTH_V2 = MAX_VALIDATOR_STAGED_PATH_LENGTH_V1
MAX_VALIDATOR_FAILURE_REASON_LENGTH_V2 = MAX_VALIDATOR_FAILURE_REASON_LENGTH_V1

MIN_VALIDATOR_MEMORY_BYTES_V1 = 128 * 1024 * 1024
MAX_VALIDATOR_MEMORY_BYTES_V1 = 1024 * 1024 * 1024

_MAX_WIRE_JSON_DEPTH = 64
_MAX_DETAIL_DEPTH = 5
_MAX_DETAIL_CONTAINER_ITEMS = 32
_MAX_DETAIL_NODES = 256
_MAX_DETAIL_KEY_LENGTH = 64
_MAX_DETAIL_STRING_LENGTH = 500
_MAX_DETAIL_INTEGER = 2**63 - 1

_MAX_SCHEMA_DEPTH = 32
_MAX_SCHEMA_CONTAINER_ITEMS = 256
_MAX_SCHEMA_NODES = 2048
_MAX_SCHEMA_KEY_LENGTH = 200
_MAX_SCHEMA_STRING_LENGTH = 4096

# Child detail is intentionally descriptive only.  These names belong to the
# parent-side evidence envelope and must never be supplied by a runner, even as
# bounded nested data that a downstream consumer could accidentally trust.
_RESERVED_DETAIL_KEYS = frozenset(
    {
        "aggregation",
        "assurance_level",
        "containment_level",
        "execution_mode",
        "proves_behavioral_correctness",
        "required",
        "requirement_source",
        "runner_protocol_version",
        "status",
        "termination_reason",
        "validator_execution_policy",
        "validator_name",
        "validator_version",
    }
)

_WINDOWS_ABSOLUTE_FRAGMENT = re.compile(
    r"(?:^|[\s('\"=:;,\[\]{}])(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/])"
)
_POSIX_ABSOLUTE_FRAGMENT = re.compile(
    r"(?:^|[\s('\"=:;,\[\]{}])/(?!/)(?:[^/\s'\")]+/)*[^/\s'\")]+"
)


class ValidatorProtocolError(ValueError):
    """A stable protocol failure safe to surface without input content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _RunnerProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


def normalize_staged_relative_path(value: str) -> str:
    """Normalize one portable child-stage path or reject it."""

    if not isinstance(value, str):
        raise ValueError("staged file paths must be strings")
    value = value.strip()
    if not value or len(value) > MAX_VALIDATOR_STAGED_PATH_LENGTH_V1:
        raise ValueError("staged file paths must be 1-200 characters")
    if "\x00" in value:
        raise ValueError("staged file paths cannot contain NUL bytes")
    if "\\" in value:
        raise ValueError("staged file paths must use POSIX separators")
    if value.startswith("/") or PureWindowsPath(value).drive or re.match(r"^[A-Za-z]:", value):
        raise ValueError("staged file paths must be relative")
    if ":" in value:
        raise ValueError("staged file paths cannot contain colons")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("staged file paths must be normalized without dot segments")
    normalized = PurePosixPath(*parts).as_posix()
    if normalized in ("", ".") or normalized.startswith("../"):
        raise ValueError("staged file paths must be normalized relative paths")
    return normalized


def _walk_bounded_json(
    value: Any,
    *,
    max_depth: int,
    max_container_items: int,
    max_nodes: int,
    max_key_length: int,
    max_string_length: int,
    reject_absolute_paths: bool,
) -> None:
    nodes = 0
    active_containers: set[int] = set()

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise ValueError("JSON value contains too many items")
        if depth > max_depth:
            raise ValueError("JSON value is nested too deeply")

        if item is None or isinstance(item, bool):
            return
        if isinstance(item, int):
            if abs(item) > _MAX_DETAIL_INTEGER:
                raise ValueError("JSON integer is outside the supported range")
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            return
        if isinstance(item, str):
            if len(item) > max_string_length or "\x00" in item:
                raise ValueError("JSON string exceeds its bound")
            if reject_absolute_paths and contains_absolute_path_fragment(item):
                raise ValueError("absolute paths are forbidden in validator responses")
            return

        if isinstance(item, dict):
            identity = id(item)
            if identity in active_containers:
                raise ValueError("cyclic JSON values are forbidden")
            if len(item) > max_container_items:
                raise ValueError("JSON object contains too many items")
            active_containers.add(identity)
            try:
                for key, child in item.items():
                    if not isinstance(key, str) or not key or len(key) > max_key_length or "\x00" in key:
                        raise ValueError("JSON object keys violate protocol bounds")
                    if reject_absolute_paths and contains_absolute_path_fragment(key):
                        raise ValueError("absolute paths are forbidden in validator responses")
                    visit(child, depth + 1)
            finally:
                active_containers.remove(identity)
            return

        if isinstance(item, list):
            identity = id(item)
            if identity in active_containers:
                raise ValueError("cyclic JSON values are forbidden")
            if len(item) > max_container_items:
                raise ValueError("JSON array contains too many items")
            active_containers.add(identity)
            try:
                for child in item:
                    visit(child, depth + 1)
            finally:
                active_containers.remove(identity)
            return

        raise ValueError("value is not JSON-compatible")

    visit(value, 0)


def _reject_reserved_detail_keys(value: dict[str, Any]) -> None:
    """Reject parent-authoritative names anywhere in child detail."""

    pending: list[Any] = [value]
    seen_containers: set[int] = set()
    while pending:
        item = pending.pop()
        if not isinstance(item, (dict, list)):
            continue
        identity = id(item)
        if identity in seen_containers:
            continue
        seen_containers.add(identity)
        if isinstance(item, dict):
            if _RESERVED_DETAIL_KEYS.intersection(item):
                raise ValueError("validator detail contains parent-authoritative metadata")
            pending.extend(item.values())
        else:
            pending.extend(item)


def _reject_v2_private_detail_keys(value: dict[str, Any]) -> None:
    """Reject private output-reference fields anywhere in V2 child detail."""

    pending: list[Any] = [value]
    seen_containers: set[int] = set()
    while pending:
        item = pending.pop()
        if not isinstance(item, (dict, list)):
            continue
        identity = id(item)
        if identity in seen_containers:
            continue
        seen_containers.add(identity)
        if isinstance(item, dict):
            if _V2_PRIVATE_DETAIL_KEYS.intersection(item):
                raise ValueError("validator detail contains private V2 transport metadata")
            pending.extend(item.values())
        else:
            pending.extend(item)


def validate_bounded_detail(value: dict[str, Any]) -> dict[str, Any]:
    """Validate child-supplied detail without permitting unbounded evidence."""

    _reject_reserved_detail_keys(value)

    _walk_bounded_json(
        value,
        max_depth=_MAX_DETAIL_DEPTH,
        max_container_items=_MAX_DETAIL_CONTAINER_ITEMS,
        max_nodes=_MAX_DETAIL_NODES,
        max_key_length=_MAX_DETAIL_KEY_LENGTH,
        max_string_length=_MAX_DETAIL_STRING_LENGTH,
        reject_absolute_paths=True,
    )
    return value


def contains_absolute_path_fragment(value: str) -> bool:
    """Return whether a response string appears to expose an absolute path."""

    if "file://" in value.casefold():
        return True
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        return True
    return bool(_WINDOWS_ABSOLUTE_FRAGMENT.search(value) or _POSIX_ABSOLUTE_FRAGMENT.search(value))


class ValidatorContractProjectionV1(_RunnerProtocolModel):
    """Only output-contract fields consumed by one selected validator."""

    artifact_count: int | None = Field(
        default=None,
        ge=1,
        le=MAX_VALIDATOR_STAGED_FILES_V1,
    )
    format: str | None = Field(default=None, max_length=64)
    required_files: list[str] | None = Field(default=None, max_length=50)
    json_schema: dict[str, Any] | None = None

    @field_validator("format")
    @classmethod
    def normalize_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower().removeprefix(".")
        if not value or not re.fullmatch(r"[a-z0-9][a-z0-9+._-]{0,63}", value):
            raise ValueError("format must be a portable identifier")
        return value

    @field_validator("required_files")
    @classmethod
    def normalize_required_files(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        clean = [normalize_staged_relative_path(value) for value in values]
        if len({value.casefold() for value in clean}) != len(clean):
            raise ValueError("required file paths must be unique")
        return clean

    @field_validator("json_schema")
    @classmethod
    def bounded_json_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        _walk_bounded_json(
            value,
            max_depth=_MAX_SCHEMA_DEPTH,
            max_container_items=_MAX_SCHEMA_CONTAINER_ITEMS,
            max_nodes=_MAX_SCHEMA_NODES,
            max_key_length=_MAX_SCHEMA_KEY_LENGTH,
            max_string_length=_MAX_SCHEMA_STRING_LENGTH,
            reject_absolute_paths=False,
        )
        try:
            encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("json_schema must be JSON-compatible") from exc
        if len(encoded) > MAX_VALIDATOR_SCHEMA_BYTES_V1:
            raise ValueError("json_schema exceeds the protocol byte limit")
        return value

class ValidatorRunnerLimitsV1(_RunnerProtocolModel):
    """Numeric limits already clamped by the authoritative parent."""

    # The operator setting is at least one second, but the parent may clamp a
    # run below that when less than one second remains on the execution.
    wall_time_seconds: float = Field(default=10.0, ge=0.1, le=120.0)
    cpu_time_seconds: int = Field(default=10, ge=1, le=120)
    memory_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=MIN_VALIDATOR_MEMORY_BYTES_V1,
        le=MAX_VALIDATOR_MEMORY_BYTES_V1,
    )
    file_size_bytes: int = Field(default=1024 * 1024, ge=4096, le=1024 * 1024)
    open_files: int = Field(default=64, ge=16, le=128)
    child_processes: int = Field(default=0, ge=0, le=16)
    response_max_bytes: int = Field(
        default=MAX_VALIDATOR_RUNNER_RESPONSE_BYTES_V1,
        ge=1024,
        le=MAX_VALIDATOR_RUNNER_RESPONSE_BYTES_V1,
    )


_OUTPUT_VALIDATORS = frozenset({"nonempty", "structured_json", "json_schema"})
_PATH_VALIDATORS = frozenset(
    {
        "file_manifest",
        "code_parse",
        "artifact_extraction",
        "artifact_contract",
    }
)

_V2_PRIVATE_DETAIL_KEYS = frozenset(
    {
        "byte_length",
        "encoding",
        "output_reference",
        "relative_path",
        "sha256",
    }
)

_V2_INFRASTRUCTURE_FAILURE_REASONS = frozenset(
    {
        "validator_execution_error",
        "validator_output_digest_mismatch",
        "validator_output_file_missing",
        "validator_output_file_not_regular",
        "validator_output_invalid_utf8",
        "validator_output_oversized",
        "validator_output_reference_invalid",
        "validator_output_reference_missing",
        "validator_output_size_mismatch",
        "validator_response_oversized",
        "validator_runner_protocol_error",
    }
)

_V2_JSON_TYPES = frozenset(
    {"NoneType", "array", "bool", "float", "int", "object", "str"}
)
_CONTRACT_VALIDATORS = frozenset({"json_schema", "file_manifest", "artifact_contract"})


def _validate_minimal_request_payload(
    *,
    validator_name: str,
    contract: ValidatorContractProjectionV1 | None,
    staged_files: list[str],
    has_output: bool,
    missing_output_description: str,
    supplied_output_description: str,
) -> None:
    if validator_name in _OUTPUT_VALIDATORS and not has_output:
        raise ValueError(f"selected validator requires {missing_output_description}")
    if validator_name not in _OUTPUT_VALIDATORS and has_output:
        raise ValueError(f"selected validator does not accept {supplied_output_description}")
    if validator_name not in _PATH_VALIDATORS and staged_files:
        raise ValueError("selected validator does not accept staged files")
    if validator_name not in _CONTRACT_VALIDATORS and contract is not None:
        raise ValueError("selected validator does not accept a contract projection")
    if validator_name == "json_schema":
        if contract is None or contract.json_schema is None:
            raise ValueError("json_schema requires a bounded schema projection")
        if (
            contract.artifact_count is not None
            or contract.format is not None
            or contract.required_files is not None
        ):
            raise ValueError("json_schema accepts only the bounded schema projection")
    if validator_name == "file_manifest":
        if contract is None or not contract.required_files:
            raise ValueError("file_manifest requires a bounded required-file projection")
        if (
            contract.artifact_count is not None
            or contract.format is not None
            or contract.json_schema is not None
        ):
            raise ValueError("file_manifest accepts only required-file projection data")
    if validator_name == "artifact_contract":
        if contract is None or contract.artifact_count is None:
            raise ValueError("artifact_contract requires an artifact-count projection")
        if contract.required_files is not None or contract.json_schema is not None:
            raise ValueError("artifact_contract projection contains unrelated fields")


class ValidatorRunnerRequestV1(_RunnerProtocolModel):
    protocol_version: Literal["1"] = VALIDATOR_RUNNER_PROTOCOL_VERSION_V1
    validator_name: ValidatorNameV1
    validator_version: str = Field(min_length=1, max_length=32)
    output: str | None = None
    contract: ValidatorContractProjectionV1 | None = None
    staged_files: list[str] = Field(default_factory=list, max_length=MAX_VALIDATOR_STAGED_FILES_V1)
    limits: ValidatorRunnerLimitsV1 = Field(default_factory=ValidatorRunnerLimitsV1)

    @field_validator("output")
    @classmethod
    def bounded_output(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                size = len(value.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError("validator output must be valid UTF-8 text") from exc
            if size > MAX_VALIDATOR_OUTPUT_BYTES_V1:
                raise ValueError("validator output exceeds the protocol byte limit")
        return value

    @field_validator("staged_files")
    @classmethod
    def normalized_staged_files(cls, values: list[str]) -> list[str]:
        clean = [normalize_staged_relative_path(value) for value in values]
        if len({value.casefold() for value in clean}) != len(clean):
            raise ValueError("staged file paths must be unique")
        return clean

    @model_validator(mode="after")
    def known_identity_and_minimal_payload(self):
        if VALIDATOR_VERSIONS_V1[self.validator_name] != self.validator_version:
            raise ValueError("validator version does not match the built-in allowlist")
        _validate_minimal_request_payload(
            validator_name=self.validator_name,
            contract=self.contract,
            staged_files=self.staged_files,
            has_output=self.output is not None,
            missing_output_description="bounded output",
            supplied_output_description="output",
        )
        return self


class ValidatorRunnerResponseV1(_RunnerProtocolModel):
    protocol_version: Literal["1"] = VALIDATOR_RUNNER_PROTOCOL_VERSION_V1
    validator_name: ValidatorNameV1
    validator_version: str = Field(min_length=1, max_length=32)
    ok: bool
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    detail: dict[str, Any] = Field(default_factory=dict, max_length=_MAX_DETAIL_CONTAINER_ITEMS)
    failure_reason: str | None = Field(default=None, max_length=MAX_VALIDATOR_FAILURE_REASON_LENGTH_V1)

    @field_validator("detail")
    @classmethod
    def bounded_detail(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_bounded_detail(value)

    @field_validator("failure_reason")
    @classmethod
    def bounded_safe_reason(cls, value: str | None) -> str | None:
        if value is not None and contains_absolute_path_fragment(value):
            raise ValueError("absolute paths are forbidden in validator responses")
        return value

    @model_validator(mode="after")
    def known_identity_and_coherent_outcome(self):
        if VALIDATOR_VERSIONS_V1[self.validator_name] != self.validator_version:
            raise ValueError("validator version does not match the built-in allowlist")
        _validate_response_outcome(ok=self.ok, failure_reason=self.failure_reason)
        return self


def _validate_response_outcome(*, ok: bool, failure_reason: str | None) -> None:
    if ok and failure_reason is not None:
        raise ValueError("successful validator responses cannot carry a failure reason")
    if not ok and not failure_reason:
        raise ValueError("failed validator responses require a bounded failure reason")


ValidatorContractProjectionV2 = ValidatorContractProjectionV1
ValidatorRunnerLimitsV2 = ValidatorRunnerLimitsV1


class ValidatorOutputReferenceV2(_RunnerProtocolModel):
    """Parent-authored binding for the one reserved staged output file."""

    relative_path: Literal[VALIDATOR_OUTPUT_REFERENCE_PATH_V2]
    encoding: Literal["utf-8"]
    byte_length: int = Field(ge=0, le=MAX_VALIDATOR_OUTPUT_BYTES_V2)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ValidatorRunnerRequestV2(_RunnerProtocolModel):
    protocol_version: Literal["2"] = VALIDATOR_RUNNER_PROTOCOL_VERSION_V2
    validator_name: ValidatorNameV2
    validator_version: str = Field(min_length=1, max_length=32)
    output_reference: ValidatorOutputReferenceV2 | None = None
    contract: ValidatorContractProjectionV2 | None = None
    staged_files: list[str] = Field(default_factory=list, max_length=MAX_VALIDATOR_STAGED_FILES_V2)
    limits: ValidatorRunnerLimitsV2 = Field(default_factory=ValidatorRunnerLimitsV2)

    @field_validator("staged_files")
    @classmethod
    def normalized_staged_files(cls, values: list[str]) -> list[str]:
        clean = [normalize_staged_relative_path(value) for value in values]
        if len({value.casefold() for value in clean}) != len(clean):
            raise ValueError("staged file paths must be unique")
        reserved = VALIDATOR_OUTPUT_RESERVED_DIRECTORY_V2.casefold()
        if any(
            value.casefold() == reserved or value.casefold().startswith(f"{reserved}/")
            for value in clean
        ):
            raise ValueError("staged file paths cannot occupy the reserved output namespace")
        return clean

    @model_validator(mode="after")
    def known_identity_and_minimal_payload(self):
        if VALIDATOR_VERSIONS_V2[self.validator_name] != self.validator_version:
            raise ValueError("validator version does not match the built-in allowlist")
        _validate_minimal_request_payload(
            validator_name=self.validator_name,
            contract=self.contract,
            staged_files=self.staged_files,
            has_output=self.output_reference is not None,
            missing_output_description="an output reference",
            supplied_output_description="an output reference",
        )
        return self


class ValidatorRunnerResponseV2(_RunnerProtocolModel):
    protocol_version: Literal["2"] = VALIDATOR_RUNNER_PROTOCOL_VERSION_V2
    validator_name: ValidatorNameV2
    validator_version: str = Field(min_length=1, max_length=32)
    ok: bool
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    detail: dict[str, Any] = Field(default_factory=dict, max_length=_MAX_DETAIL_CONTAINER_ITEMS)
    failure_reason: str | None = Field(default=None, max_length=MAX_VALIDATOR_FAILURE_REASON_LENGTH_V2)

    @field_validator("detail")
    @classmethod
    def bounded_detail(cls, value: dict[str, Any]) -> dict[str, Any]:
        value = validate_bounded_detail(value)
        _reject_v2_private_detail_keys(value)
        return value

    @field_validator("failure_reason")
    @classmethod
    def bounded_safe_reason(cls, value: str | None) -> str | None:
        if value is not None and contains_absolute_path_fragment(value):
            raise ValueError("absolute paths are forbidden in validator responses")
        return value

    @model_validator(mode="after")
    def known_identity_and_coherent_outcome(self):
        if VALIDATOR_VERSIONS_V2[self.validator_name] != self.validator_version:
            raise ValueError("validator version does not match the built-in allowlist")
        _validate_response_outcome(ok=self.ok, failure_reason=self.failure_reason)
        _validate_v2_output_response(
            validator_name=self.validator_name,
            ok=self.ok,
            score=self.score,
            detail=self.detail,
            failure_reason=self.failure_reason,
        )
        return self


def _validate_v2_output_response(
    *,
    validator_name: ValidatorNameV2,
    ok: bool,
    score: float | None,
    detail: dict[str, Any],
    failure_reason: str | None,
) -> None:
    """Constrain output-consuming evidence to content-free built-in shapes."""

    if validator_name not in _OUTPUT_VALIDATORS:
        return
    if failure_reason in _V2_INFRASTRUCTURE_FAILURE_REASONS:
        if ok or score is not None or detail:
            raise ValueError("validator infrastructure failures require empty evidence")
        return
    if score != (1.0 if ok else 0.0):
        raise ValueError("output-validator response score is incoherent")

    if validator_name == "nonempty":
        output_bytes = detail.get("output_bytes")
        if (
            set(detail) != {"output_bytes"}
            or type(output_bytes) is not int
            or not 0 <= output_bytes <= MAX_VALIDATOR_OUTPUT_BYTES_V2
        ):
            raise ValueError("nonempty response detail does not match its built-in shape")
        if ok:
            if output_bytes == 0 or failure_reason is not None:
                raise ValueError("nonempty success response is incoherent")
        elif output_bytes != 0 or failure_reason != "candidate output is empty":
            raise ValueError("nonempty failure response is incoherent")
        return

    if validator_name == "structured_json":
        if ok:
            if set(detail) != {"json_type"} or detail.get("json_type") not in _V2_JSON_TYPES:
                raise ValueError("structured_json response detail is not allowlisted")
        elif detail or failure_reason != "output is not valid JSON":
            raise ValueError("structured_json failure response is not allowlisted")
        return

    if ok:
        if detail != {"schema_valid": True, "claim": "contract_conformance"}:
            raise ValueError("json_schema response detail is not allowlisted")
    elif detail or failure_reason not in {
        "JSON Schema input could not be parsed",
        "JSON Schema validation failed",
    }:
        raise ValueError("json_schema failure response is not allowlisted")


ValidatorRunnerRequest: TypeAlias = ValidatorRunnerRequestV1 | ValidatorRunnerRequestV2
ValidatorRunnerResponse: TypeAlias = ValidatorRunnerResponseV1 | ValidatorRunnerResponseV2


@overload
def ensure_response_identity(
    request: ValidatorRunnerRequestV1,
    response: ValidatorRunnerResponseV1,
) -> ValidatorRunnerResponseV1: ...


@overload
def ensure_response_identity(
    request: ValidatorRunnerRequestV2,
    response: ValidatorRunnerResponseV2,
) -> ValidatorRunnerResponseV2: ...


def ensure_response_identity(
    request: ValidatorRunnerRequest,
    response: ValidatorRunnerResponse,
) -> ValidatorRunnerResponse:
    if (
        response.protocol_version != request.protocol_version
        or response.validator_name != request.validator_name
        or response.validator_version != request.validator_version
    ):
        raise ValidatorProtocolError("validator_response_identity_mismatch")
    return response


def _json_depth_at_most(raw: bytes, maximum: int) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { [
            depth += 1
            if depth > maximum:
                return False
        elif byte in (0x7D, 0x5D):  # } ]
            depth -= 1
            if depth < 0:
                # This is malformed rather than deeply nested. Let the JSON
                # decoder classify it through the stable malformed-json path.
                return True
    # Unclosed containers/strings are also syntax errors. This scanner has one
    # job: reject excessive nesting before invoking the decoder.
    return True


def load_bounded_json_bytes(raw: bytes, *, max_bytes: int) -> Any:
    if not isinstance(raw, bytes):
        raise ValidatorProtocolError("validator_protocol_bytes_required")
    if len(raw) > max_bytes:
        raise ValidatorProtocolError("validator_protocol_input_oversized")
    if not _json_depth_at_most(raw, _MAX_WIRE_JSON_DEPTH):
        raise ValidatorProtocolError("validator_protocol_json_depth_exceeded")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidatorProtocolError("validator_protocol_malformed_json") from exc


def dump_bounded_json_bytes(value: Any, *, max_bytes: int) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    try:
        raw = json.dumps(
            value,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidatorProtocolError("validator_protocol_non_json_value") from exc
    if len(raw) > max_bytes:
        raise ValidatorProtocolError("validator_protocol_output_oversized")
    return raw


ModelT = TypeVar("ModelT", bound=BaseModel)


def _validate_parsed_model(parsed: Any, model: type[ModelT], *, error_code: str) -> ModelT:
    try:
        return model.model_validate(parsed)
    except Exception as exc:
        raise ValidatorProtocolError(error_code) from exc


def _read_protocol_version(parsed: Any) -> str:
    if not isinstance(parsed, dict):
        raise ValidatorProtocolError("validator_protocol_version_invalid")
    if "protocol_version" not in parsed:
        raise ValidatorProtocolError("validator_protocol_version_missing")
    version = parsed["protocol_version"]
    if not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]{0,3}", version):
        raise ValidatorProtocolError("validator_protocol_version_invalid")
    if version not in {
        VALIDATOR_RUNNER_PROTOCOL_VERSION_V1,
        VALIDATOR_RUNNER_PROTOCOL_VERSION_V2,
    }:
        raise ValidatorProtocolError("validator_protocol_version_unsupported")
    return version


def parse_runner_request_bytes(
    raw: bytes,
    *,
    max_bytes: int = MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V2,
) -> ValidatorRunnerRequest:
    parsed = load_bounded_json_bytes(
        raw,
        max_bytes=min(
            max_bytes,
            max(
                MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V1,
                MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V2,
            ),
        ),
    )
    version = _read_protocol_version(parsed)
    model: type[ValidatorRunnerRequestV1] | type[ValidatorRunnerRequestV2]
    if version == VALIDATOR_RUNNER_PROTOCOL_VERSION_V1:
        model = ValidatorRunnerRequestV1
    else:
        model = ValidatorRunnerRequestV2
    return _validate_parsed_model(
        parsed,
        model,
        error_code="validator_runner_request_invalid",
    )


def parse_runner_response_bytes(
    raw: bytes,
    *,
    request: ValidatorRunnerRequest | None = None,
    max_bytes: int = MAX_VALIDATOR_RUNNER_RESPONSE_BYTES_V2,
) -> ValidatorRunnerResponse:
    parsed = load_bounded_json_bytes(
        raw,
        max_bytes=min(
            max_bytes,
            max(
                MAX_VALIDATOR_RUNNER_RESPONSE_BYTES_V1,
                MAX_VALIDATOR_RUNNER_RESPONSE_BYTES_V2,
            ),
        ),
    )
    version = _read_protocol_version(parsed)
    model: type[ValidatorRunnerResponseV1] | type[ValidatorRunnerResponseV2]
    if version == VALIDATOR_RUNNER_PROTOCOL_VERSION_V1:
        model = ValidatorRunnerResponseV1
    else:
        model = ValidatorRunnerResponseV2
    response = _validate_parsed_model(
        parsed,
        model,
        error_code="validator_runner_response_invalid",
    )
    return ensure_response_identity(request, response) if request is not None else response


def dump_runner_request_bytes(
    request: ValidatorRunnerRequest,
    *,
    max_bytes: int = MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V2,
) -> bytes:
    if not isinstance(request, (ValidatorRunnerRequestV1, ValidatorRunnerRequestV2)):
        raise ValidatorProtocolError("validator_runner_request_invalid")
    hard_max = (
        MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V1
        if isinstance(request, ValidatorRunnerRequestV1)
        else MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V2
    )
    return dump_bounded_json_bytes(
        request,
        max_bytes=min(max_bytes, hard_max),
    )


def dump_runner_response_bytes(
    response: ValidatorRunnerResponse,
    *,
    max_bytes: int = MAX_VALIDATOR_RUNNER_RESPONSE_BYTES_V2,
) -> bytes:
    if not isinstance(response, (ValidatorRunnerResponseV1, ValidatorRunnerResponseV2)):
        raise ValidatorProtocolError("validator_runner_response_invalid")
    hard_max = (
        MAX_VALIDATOR_RUNNER_RESPONSE_BYTES_V1
        if isinstance(response, ValidatorRunnerResponseV1)
        else MAX_VALIDATOR_RUNNER_RESPONSE_BYTES_V2
    )
    return dump_bounded_json_bytes(
        response,
        max_bytes=min(max_bytes, hard_max),
    )
