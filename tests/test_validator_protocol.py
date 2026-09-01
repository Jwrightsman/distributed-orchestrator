"""Strict bounds for the built-in validator child protocol."""

from __future__ import annotations

import json
import os

import pytest
from pydantic import ValidationError

from execution import validator_runner
from execution.validator_protocol import (
    MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V1,
    _RESERVED_DETAIL_KEYS,
    ValidatorContractProjectionV1,
    ValidatorProtocolError,
    ValidatorRunnerRequestV1,
    ValidatorRunnerResponseV1,
    dump_bounded_json_bytes,
    dump_runner_request_bytes,
    dump_runner_response_bytes,
    parse_runner_request_bytes,
    parse_runner_response_bytes,
)


def _schema_contract() -> ValidatorContractProjectionV1:
    return ValidatorContractProjectionV1(
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "integer"}},
            "required": ["answer"],
        },
    )


def _schema_request() -> ValidatorRunnerRequestV1:
    return ValidatorRunnerRequestV1(
        validator_name="json_schema",
        validator_version="2",
        output='{"answer":42}',
        contract=_schema_contract(),
    )


def test_valid_request_and_response_round_trip():
    request = _schema_request()
    response = ValidatorRunnerResponseV1(
        validator_name="json_schema",
        validator_version="2",
        ok=True,
        score=1.0,
        detail={"schema_valid": True, "claim": "contract_conformance"},
    )

    parsed_request = parse_runner_request_bytes(dump_runner_request_bytes(request))
    parsed_response = parse_runner_response_bytes(
        dump_runner_response_bytes(response),
        request=parsed_request,
    )

    assert parsed_request == request
    assert parsed_response == response


def test_runner_framing_does_not_exceed_an_exact_response_byte_cap():
    raw = b"x" * 1024
    read_descriptor, write_descriptor = os.pipe()
    try:
        validator_runner._write_protocol_bytes(write_descriptor, raw)
    finally:
        os.close(write_descriptor)
    try:
        captured = os.read(read_descriptor, len(raw) + 1)
    finally:
        os.close(read_descriptor)

    assert captured == raw


def test_request_rejects_unsupported_protocol_version():
    payload = _schema_request().model_dump(mode="json")
    payload["protocol_version"] = "2"

    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_request_bytes(json.dumps(payload).encode())

    assert error.value.code == "validator_runner_request_invalid"


def test_request_rejects_unknown_validator():
    payload = _schema_request().model_dump(mode="json")
    payload["validator_name"] = "package.module:callable"

    with pytest.raises(ValidationError):
        ValidatorRunnerRequestV1.model_validate(payload)


def test_request_rejects_validator_version_mismatch():
    payload = _schema_request().model_dump(mode="json")
    payload["validator_version"] = "999"

    with pytest.raises(ValidationError, match="built-in allowlist"):
        ValidatorRunnerRequestV1.model_validate(payload)


def test_protocol_models_do_not_coerce_numeric_or_boolean_fields():
    payload = _schema_request().model_dump(mode="json")
    payload["limits"]["memory_bytes"] = "268435456"
    with pytest.raises(ValidationError):
        ValidatorRunnerRequestV1.model_validate(payload)

    payload = _schema_request().model_dump(mode="json")
    payload["limits"]["cpu_time_seconds"] = True
    with pytest.raises(ValidationError):
        ValidatorRunnerRequestV1.model_validate(payload)


def test_response_rejects_unknown_fields():
    response = {
        "protocol_version": "1",
        "validator_name": "json_schema",
        "validator_version": "2",
        "ok": True,
        "score": 1.0,
        "detail": {},
        "failure_reason": None,
        "assurance_level": "deterministic",
    }

    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_response_bytes(json.dumps(response).encode(), request=_schema_request())

    assert error.value.code == "validator_runner_response_invalid"


@pytest.mark.parametrize(
    "reserved_key", sorted(_RESERVED_DETAIL_KEYS)
)
@pytest.mark.parametrize("nesting", ["top_level", "nested_object", "object_in_list"])
def test_response_detail_cannot_supply_parent_authoritative_metadata(reserved_key, nesting):
    detail = {reserved_key: "forged"}
    if nesting == "nested_object":
        detail = {"descriptive": detail}
    elif nesting == "object_in_list":
        detail = {"descriptive": [{"nested": detail}]}

    response = {
        "protocol_version": "1",
        "validator_name": "nonempty",
        "validator_version": "2",
        "ok": True,
        "score": 1.0,
        "detail": detail,
        "failure_reason": None,
    }

    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_response_bytes(json.dumps(response).encode())

    assert error.value.code == "validator_runner_response_invalid"


def test_malformed_json_is_a_stable_protocol_error():
    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_response_bytes(b'{"protocol_version":')

    assert error.value.code == "validator_protocol_malformed_json"
    assert "protocol_version" not in str(error.value)


def test_oversized_request_is_rejected_before_json_parsing():
    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_request_bytes(b"x" * 101, max_bytes=100)

    assert error.value.code == "validator_protocol_input_oversized"


def test_hard_request_ceiling_cannot_be_raised_by_caller():
    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_request_bytes(
            b"x" * (MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V1 + 1),
            max_bytes=MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V1 * 2,
        )

    assert error.value.code == "validator_protocol_input_oversized"


def test_oversized_response_is_rejected():
    response = ValidatorRunnerResponseV1(
        validator_name="nonempty",
        validator_version="2",
        ok=True,
        detail={"output_bytes": 1},
    )

    with pytest.raises(ValidatorProtocolError) as error:
        dump_runner_response_bytes(response, max_bytes=16)

    assert error.value.code == "validator_protocol_output_oversized"


def test_wire_json_nesting_is_checked_before_json_decoder():
    raw = b"[" * 70 + b"0" + b"]" * 70

    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_response_bytes(raw)

    assert error.value.code == "validator_protocol_json_depth_exceeded"


def test_response_rejects_deep_or_excessive_detail():
    deep: dict[str, object] = {"leaf": True}
    for _ in range(7):
        deep = {"next": deep}
    with pytest.raises(ValidationError, match="nested too deeply"):
        ValidatorRunnerResponseV1(
            validator_name="nonempty",
            validator_version="2",
            ok=True,
            detail=deep,
        )

    with pytest.raises(ValidationError, match="too many items"):
        ValidatorRunnerResponseV1(
            validator_name="nonempty",
            validator_version="2",
            ok=True,
            detail={"values": list(range(33))},
        )


@pytest.mark.parametrize(
    "exposed",
    [
        "/tmp/mycelium-validator-abcd/code/main.py",
        "prefix:/tmp/mycelium-validator-abcd/code/main.py",
        r"C:\Users\operator\AppData\Local\Temp\validator\main.py",
        r"prefix:C:\Users\operator\AppData\Local\Temp\validator\main.py",
        "parser failed at /private/tmp/validator/main.py",
        "file:///tmp/mycelium-validator-abcd/code/main.py",
        "uri=file:///private/tmp/validator/main.py",
    ],
)
def test_absolute_temporary_paths_are_rejected(exposed):
    with pytest.raises(ValidationError, match="absolute paths"):
        ValidatorRunnerResponseV1(
            validator_name="code_parse",
            validator_version="2",
            ok=False,
            detail={"path": exposed},
            failure_reason="code_parse_failed",
        )


def test_absolute_temporary_paths_are_rejected_in_detail_keys():
    with pytest.raises(ValidationError, match="absolute paths"):
        ValidatorRunnerResponseV1(
            validator_name="code_parse",
            validator_version="2",
            ok=False,
            detail={"prefix:/tmp/validator/main.py": True},
            failure_reason="code_parse_failed",
        )


@pytest.mark.parametrize(
    "path",
    [
        "../secret.py",
        "/absolute.py",
        "C:/absolute.py",
        r"code\main.py",
        "code/./main.py",
        "code//main.py",
    ],
)
def test_staged_paths_reject_traversal_and_nonportable_forms(path):
    with pytest.raises(ValidationError):
        ValidatorRunnerRequestV1(
            validator_name="code_parse",
            validator_version="2",
            staged_files=[path],
        )


def test_staged_paths_are_normalized_and_portably_unique():
    request = ValidatorRunnerRequestV1(
        validator_name="code_parse",
        validator_version="2",
        staged_files=[" code/main.py "],
    )
    assert request.staged_files == ["code/main.py"]

    with pytest.raises(ValidationError, match="unique"):
        ValidatorRunnerRequestV1(
            validator_name="code_parse",
            validator_version="2",
            staged_files=["code/main.py", "CODE/main.py"],
        )


@pytest.mark.parametrize(
    ("validator_name", "validator_version", "contract"),
    [
        ("code_parse", "2", None),
        ("artifact_extraction", "2", None),
        (
            "artifact_contract",
            "1",
            ValidatorContractProjectionV1(artifact_count=1),
        ),
        (
            "file_manifest",
            "2",
            ValidatorContractProjectionV1(required_files=["result.txt"]),
        ),
    ],
)
def test_path_validators_accept_bounded_logical_names(
    validator_name,
    validator_version,
    contract,
):
    request = ValidatorRunnerRequestV1(
        validator_name=validator_name,
        validator_version=validator_version,
        contract=contract,
        staged_files=["result.txt"],
    )

    assert request.staged_files == ["result.txt"]


def test_non_path_validator_rejects_logical_file_names():
    with pytest.raises(ValidationError, match="does not accept staged files"):
        ValidatorRunnerRequestV1(
            validator_name="nonempty",
            validator_version="2",
            output="complete",
            staged_files=["result.txt"],
        )


def test_request_contains_only_the_payload_selected_validator_uses():
    with pytest.raises(ValidationError, match="does not accept output"):
        ValidatorRunnerRequestV1(
            validator_name="code_parse",
            validator_version="2",
            output="print('not passed through the wrong field')",
            staged_files=["main.py"],
        )

    with pytest.raises(ValidationError, match="requires bounded output"):
        ValidatorRunnerRequestV1(
            validator_name="structured_json",
            validator_version="2",
        )

    encoded = json.loads(dump_runner_request_bytes(_schema_request()))
    assert encoded["contract"] == {
        "json_schema": _schema_contract().json_schema,
    }


def test_response_identity_must_match_parent_request():
    response = ValidatorRunnerResponseV1(
        validator_name="structured_json",
        validator_version="2",
        ok=True,
        detail={"json_type": "object"},
    )

    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_response_bytes(
            dump_runner_response_bytes(response),
            request=_schema_request(),
        )

    assert error.value.code == "validator_response_identity_mismatch"


def test_response_outcome_is_coherent_and_finite():
    with pytest.raises(ValidationError, match="require a bounded failure reason"):
        ValidatorRunnerResponseV1(
            validator_name="nonempty",
            validator_version="2",
            ok=False,
        )
    with pytest.raises(ValidationError):
        ValidatorRunnerResponseV1(
            validator_name="nonempty",
            validator_version="2",
            ok=True,
            score=float("nan"),
        )


def test_dispatcher_exception_is_reduced_to_stable_bounded_failure():
    request = ValidatorRunnerRequestV1(
        validator_name="nonempty",
        validator_version="2",
        output="present",
    )

    def explode(_request):
        raise RuntimeError("secret source at C:\\private\\generated.py")

    response = validator_runner._execute_request(request, explode)

    assert response.ok is False
    assert response.failure_reason == "validator_execution_error"
    assert response.detail == {}
    assert "private" not in dump_bounded_json_bytes(response, max_bytes=1024).decode()
