"""Strict bounds for the built-in validator child protocol."""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from pydantic import ValidationError

from execution import validator_runner
from execution.validator_protocol import (
    MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V1,
    MAX_VALIDATOR_OUTPUT_BYTES_V2,
    VALIDATOR_OUTPUT_REFERENCE_PATH_V2,
    VALIDATOR_OUTPUT_RESERVED_DIRECTORY_V2,
    _RESERVED_DETAIL_KEYS,
    ValidatorContractProjectionV1,
    ValidatorOutputReferenceV2,
    ValidatorProtocolError,
    ValidatorRunnerRequestV1,
    ValidatorRunnerRequestV2,
    ValidatorRunnerResponseV1,
    ValidatorRunnerResponseV2,
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


def _output_reference(*, byte_length: int = 13) -> ValidatorOutputReferenceV2:
    return ValidatorOutputReferenceV2(
        relative_path=VALIDATOR_OUTPUT_REFERENCE_PATH_V2,
        encoding="utf-8",
        byte_length=byte_length,
        sha256="a" * 64,
    )


def _schema_request_v2() -> ValidatorRunnerRequestV2:
    return ValidatorRunnerRequestV2(
        validator_name="json_schema",
        validator_version="2",
        output_reference=_output_reference(),
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


def test_valid_v2_request_and_response_round_trip():
    request = _schema_request_v2()
    response = ValidatorRunnerResponseV2(
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

    assert type(parsed_request) is ValidatorRunnerRequestV2
    assert type(parsed_response) is ValidatorRunnerResponseV2
    assert parsed_request == request
    assert parsed_response == response


def test_v1_round_trip_remains_explicitly_supported():
    request = _schema_request()
    response = ValidatorRunnerResponseV1(
        validator_name="json_schema",
        validator_version="2",
        ok=True,
        detail={"schema_valid": True},
    )

    parsed_request = parse_runner_request_bytes(dump_runner_request_bytes(request))
    parsed_response = parse_runner_response_bytes(
        dump_runner_response_bytes(response),
        request=parsed_request,
    )

    assert type(parsed_request) is ValidatorRunnerRequestV1
    assert type(parsed_response) is ValidatorRunnerResponseV1


def test_v2_control_request_contains_reference_metadata_not_output_body():
    sentinel = "DISTINCTIVE_GENERATED_OUTPUT_SENTINEL"
    output_bytes = f'{{"value":"{sentinel}"}}'.encode()
    digest = hashlib.sha256(output_bytes).hexdigest()
    request = ValidatorRunnerRequestV2(
        validator_name="json_schema",
        validator_version="2",
        output_reference=ValidatorOutputReferenceV2(
            relative_path=VALIDATOR_OUTPUT_REFERENCE_PATH_V2,
            encoding="utf-8",
            byte_length=len(output_bytes),
            sha256=digest,
        ),
        contract=_schema_contract(),
    )

    raw = dump_runner_request_bytes(request)
    decoded = json.loads(raw)

    assert sentinel.encode() not in raw
    assert "output" not in decoded
    assert decoded["output_reference"] == {
        "relative_path": VALIDATOR_OUTPUT_REFERENCE_PATH_V2,
        "encoding": "utf-8",
        "byte_length": len(output_bytes),
        "sha256": digest,
    }


def test_v2_maximum_output_reference_keeps_control_envelope_small():
    request = ValidatorRunnerRequestV2(
        validator_name="structured_json",
        validator_version="2",
        output_reference=_output_reference(
            byte_length=MAX_VALIDATOR_OUTPUT_BYTES_V2,
        ),
    )

    raw = dump_runner_request_bytes(request, max_bytes=2 * 1024 * 1024)

    assert len(raw) < 2048


def test_v2_forbids_inline_output_even_when_otherwise_valid():
    payload = _schema_request_v2().model_dump(mode="json")
    payload["output"] = '{"sentinel":true}'

    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_request_bytes(json.dumps(payload).encode())

    assert error.value.code == "validator_runner_request_invalid"


@pytest.mark.parametrize("validator_name", ["nonempty", "structured_json"])
def test_v2_output_consumers_require_reference(validator_name):
    with pytest.raises(ValidationError, match="requires an output reference"):
        ValidatorRunnerRequestV2(
            validator_name=validator_name,
            validator_version="2",
        )


def test_v2_json_schema_requires_reference():
    with pytest.raises(ValidationError, match="requires an output reference"):
        ValidatorRunnerRequestV2(
            validator_name="json_schema",
            validator_version="2",
            contract=_schema_contract(),
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
def test_v2_file_only_validators_forbid_output_reference(
    validator_name,
    validator_version,
    contract,
):
    with pytest.raises(ValidationError, match="does not accept an output reference"):
        ValidatorRunnerRequestV2(
            validator_name=validator_name,
            validator_version=validator_version,
            output_reference=_output_reference(),
            contract=contract,
            staged_files=["result.txt"],
        )


def test_v2_output_reference_accepts_exact_canonical_byte_boundary():
    reference = _output_reference(byte_length=MAX_VALIDATOR_OUTPUT_BYTES_V2)

    assert reference.relative_path == VALIDATOR_OUTPUT_REFERENCE_PATH_V2
    assert reference.encoding == "utf-8"
    assert reference.byte_length == MAX_VALIDATOR_OUTPUT_BYTES_V2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("relative_path", "output.utf8"),
        ("relative_path", "../output.utf8"),
        ("relative_path", "/tmp/output.utf8"),
        ("relative_path", r"__mycelium_validator_input__\output.utf8"),
        ("relative_path", "C:/output.utf8"),
        ("relative_path", "C:output.utf8"),
        ("relative_path", "__mycelium_validator_input__/output.utf8:stream"),
        ("relative_path", "__mycelium_validator_input__/output.utf8\x00"),
        ("encoding", "UTF-8"),
        ("byte_length", MAX_VALIDATOR_OUTPUT_BYTES_V2 + 1),
        ("sha256", "A" * 64),
        ("sha256", "a" * 63),
        ("sha256", "g" * 64),
    ],
)
def test_v2_output_reference_rejects_noncanonical_metadata(field, value):
    payload = {
        "relative_path": VALIDATOR_OUTPUT_REFERENCE_PATH_V2,
        "encoding": "utf-8",
        "byte_length": 13,
        "sha256": "a" * 64,
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        ValidatorOutputReferenceV2.model_validate(payload)


def test_v2_output_reference_forbids_unknown_fields():
    payload = _output_reference().model_dump(mode="json")
    payload["absolute_path"] = "not-allowed"

    with pytest.raises(ValidationError):
        ValidatorOutputReferenceV2.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    ["relative_path", "encoding", "byte_length", "sha256"],
)
def test_v2_output_reference_requires_every_binding_field(field):
    payload = _output_reference().model_dump(mode="json")
    del payload[field]

    with pytest.raises(ValidationError):
        ValidatorOutputReferenceV2.model_validate(payload)


@pytest.mark.parametrize(
    "path",
    [
        VALIDATOR_OUTPUT_RESERVED_DIRECTORY_V2,
        f"{VALIDATOR_OUTPUT_RESERVED_DIRECTORY_V2}/artifact.json",
        f"{VALIDATOR_OUTPUT_RESERVED_DIRECTORY_V2.upper()}/artifact.json",
    ],
)
def test_v2_staged_files_reject_reserved_output_namespace(path):
    with pytest.raises(ValidationError, match="reserved output namespace"):
        ValidatorRunnerRequestV2(
            validator_name="code_parse",
            validator_version="2",
            staged_files=[path],
        )


def test_v1_staged_file_compatibility_does_not_retroactively_reserve_namespace():
    request = ValidatorRunnerRequestV1(
        validator_name="code_parse",
        validator_version="2",
        staged_files=[f"{VALIDATOR_OUTPUT_RESERVED_DIRECTORY_V2}/legacy.py"],
    )

    assert request.staged_files == [
        f"{VALIDATOR_OUTPUT_RESERVED_DIRECTORY_V2}/legacy.py"
    ]


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
    payload["protocol_version"] = "3"

    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_request_bytes(json.dumps(payload).encode())

    assert error.value.code == "validator_protocol_version_unsupported"


def test_request_rejects_missing_protocol_version():
    payload = _schema_request().model_dump(mode="json")
    del payload["protocol_version"]

    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_request_bytes(json.dumps(payload).encode())

    assert error.value.code == "validator_protocol_version_missing"


@pytest.mark.parametrize("version", [2, True, "", "02", " 2", "v2"])
def test_request_rejects_nonstring_or_malformed_protocol_version(version):
    payload = _schema_request().model_dump(mode="json")
    payload["protocol_version"] = version

    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_request_bytes(json.dumps(payload).encode())

    assert error.value.code == "validator_protocol_version_invalid"


def test_malformed_v2_request_is_not_reinterpreted_as_v1():
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


def test_v2_response_preserves_parent_authority_and_path_privacy_bounds():
    with pytest.raises(ValidationError, match="parent-authoritative metadata"):
        ValidatorRunnerResponseV2(
            validator_name="nonempty",
            validator_version="2",
            ok=True,
            detail={"descriptive": [{"runner_protocol_version": "forged"}]},
        )

    with pytest.raises(ValidationError, match="absolute paths"):
        ValidatorRunnerResponseV2(
            validator_name="code_parse",
            validator_version="2",
            ok=False,
            detail={"path": "/private/validator/output.utf8"},
            failure_reason="code_parse_failed",
        )


@pytest.mark.parametrize(
    "private_key",
    ["output_reference", "relative_path", "encoding", "byte_length", "sha256"],
)
def test_v2_response_detail_cannot_expose_private_output_reference(private_key):
    with pytest.raises(ValidationError, match="private V2 transport metadata"):
        ValidatorRunnerResponseV2(
            validator_name="code_parse",
            validator_version="2",
            ok=True,
            detail={"nested": [{private_key: "private transport value"}]},
        )


@pytest.mark.parametrize("channel", ["detail", "failure_reason"])
def test_v2_output_validator_response_cannot_echo_output_body(channel):
    sentinel = "PRIVATE_GENERATED_OUTPUT_SENTINEL"
    values = {
        "validator_name": "structured_json",
        "validator_version": "2",
        "ok": channel == "detail",
        "score": 1.0 if channel == "detail" else 0.0,
        "detail": {"echo": sentinel} if channel == "detail" else {},
        "failure_reason": sentinel if channel == "failure_reason" else None,
    }

    with pytest.raises(ValidationError, match="not allowlisted"):
        ValidatorRunnerResponseV2(**values)


def test_v2_output_validator_response_score_must_match_builtin_outcome():
    with pytest.raises(ValidationError, match="score is incoherent"):
        ValidatorRunnerResponseV2(
            validator_name="structured_json",
            validator_version="2",
            ok=True,
            score=0.0,
            detail={"json_type": "object"},
        )


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


@pytest.mark.parametrize(
    ("runner_request", "runner_response"),
    [
        (
            _schema_request_v2(),
            ValidatorRunnerResponseV1(
                validator_name="json_schema",
                validator_version="2",
                ok=True,
                detail={"schema_valid": True},
            ),
        ),
        (
            _schema_request(),
            ValidatorRunnerResponseV2(
                validator_name="json_schema",
                validator_version="2",
                ok=True,
                score=1.0,
                detail={"schema_valid": True, "claim": "contract_conformance"},
            ),
        ),
    ],
)
def test_response_protocol_version_must_match_request(runner_request, runner_response):
    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_response_bytes(
            dump_runner_response_bytes(runner_response),
            request=runner_request,
        )

    assert error.value.code == "validator_response_identity_mismatch"


def test_malformed_v2_response_is_not_reinterpreted_as_v1():
    payload = ValidatorRunnerResponseV1(
        validator_name="json_schema",
        validator_version="2",
        ok=True,
        detail={"schema_valid": True},
    ).model_dump(mode="json")
    payload["protocol_version"] = "2"
    payload["output"] = "inline payload forbidden in every response"

    with pytest.raises(ValidatorProtocolError) as error:
        parse_runner_response_bytes(json.dumps(payload).encode())

    assert error.value.code == "validator_runner_response_invalid"


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
