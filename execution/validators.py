"""Versioned deterministic validator registry."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from execution.contracts import (
    ExecutionRequestV1,
    OutputContractV1,
    ValidationEvidenceV1,
    ValidatorSpecV1,
)
from extract import check_code_files


@dataclass(frozen=True)
class ValidationInput:
    output: str
    files: list[str]
    contract: OutputContractV1 | None


class Validator(Protocol):
    name: str
    version: str

    def validate(self, value: ValidationInput) -> tuple[bool, float | None, dict[str, Any], str | None]: ...


class NonemptyValidator:
    name = "nonempty"
    version = "1"

    def validate(self, value):
        size = len(value.output.strip().encode("utf-8"))
        ok = size > 0
        return ok, 1.0 if ok else 0.0, {"output_bytes": size}, None if ok else "candidate output is empty"


class ArtifactExtractionValidator:
    name = "artifact_extraction"
    version = "1"

    def validate(self, value):
        count = len(value.files)
        ok = count > 0
        return ok, 1.0 if ok else 0.0, {"file_count": count}, None if ok else "no artifacts were extracted"


class CodeParseValidator:
    name = "code_parse"
    version = "1"

    def validate(self, value):
        if not value.files:
            return False, 0.0, {"problems": []}, "no files were available to parse"
        problems = check_code_files(value.files)
        ok = not problems
        evidence = {"checked_files": len(value.files), "problems": problems[:10]}
        return ok, 1.0 if ok else 0.0, evidence, None if ok else "; ".join(problems[:3])[:500]


def _json_document(text: str) -> Any:
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL | re.IGNORECASE)
    if fenced:
        stripped = fenced.group(1)
    return json.loads(stripped)


class StructuredJsonValidator:
    name = "structured_json"
    version = "1"

    def validate(self, value):
        try:
            parsed = _json_document(value.output)
        except (json.JSONDecodeError, ValueError) as exc:
            return False, 0.0, {}, f"output is not valid JSON: {exc}"
        kind = "object" if isinstance(parsed, dict) else "array" if isinstance(parsed, list) else type(parsed).__name__
        return True, 1.0, {"json_type": kind}, None


class JsonSchemaValidator:
    name = "json_schema"
    version = "1"

    def validate(self, value):
        schema = value.contract.json_schema if value.contract else None
        if not schema:
            return False, 0.0, {}, "json_schema validator requires output_contract.json_schema"
        try:
            parsed = _json_document(value.output)
            import jsonschema

            jsonschema.validate(parsed, schema)
        except Exception as exc:
            return False, 0.0, {}, f"JSON Schema validation failed: {exc}"[:500]
        return True, 1.0, {"schema_valid": True}, None


class FileManifestValidator:
    name = "file_manifest"
    version = "1"

    def validate(self, value):
        required = list(value.contract.required_files) if value.contract else []
        available = {Path(path).name for path in value.files}
        missing = [name for name in required if Path(name).name not in available]
        ok = bool(required) and not missing
        return (
            ok,
            1.0 if ok else 0.0,
            {"required": required, "available": sorted(available), "missing": missing},
            None if ok else ("missing required files: " + ", ".join(missing) if missing else "no file manifest supplied"),
        )


class ValidatorRegistry:
    def __init__(self):
        self._validators: dict[str, Validator] = {}

    @classmethod
    def default(cls) -> "ValidatorRegistry":
        registry = cls()
        for validator in (
            NonemptyValidator(),
            StructuredJsonValidator(),
            JsonSchemaValidator(),
            FileManifestValidator(),
            CodeParseValidator(),
            ArtifactExtractionValidator(),
        ):
            registry.register(validator)
        return registry

    def register(self, validator: Validator) -> None:
        if validator.name in self._validators:
            raise ValueError(f"validator already registered: {validator.name}")
        self._validators[validator.name] = validator

    def has(self, name: str) -> bool:
        return name in self._validators

    def specs_for(self, request: ExecutionRequestV1) -> list[ValidatorSpecV1]:
        explicit = list(request.verification.validators)
        if request.output_contract:
            explicit.extend(request.output_contract.validators)
        if explicit:
            seen: set[str] = set()
            return [spec for spec in explicit if not (spec.name in seen or seen.add(spec.name))]

        contract = request.output_contract
        specs = [ValidatorSpecV1(name="nonempty")]
        if not contract:
            return specs
        if contract.kind == "structured_json":
            specs.append(ValidatorSpecV1(name="json_schema" if contract.json_schema else "structured_json"))
        elif contract.kind == "file_manifest":
            specs.extend((ValidatorSpecV1(name="artifact_extraction"), ValidatorSpecV1(name="file_manifest")))
        elif contract.kind in ("single_artifact", "code"):
            specs.extend((ValidatorSpecV1(name="artifact_extraction"), ValidatorSpecV1(name="code_parse")))
        return specs

    def validate(self, request: ExecutionRequestV1, output: str, files: list[str]) -> list[ValidationEvidenceV1]:
        value = ValidationInput(output=output, files=files, contract=request.output_contract)
        evidence: list[ValidationEvidenceV1] = []
        for spec in self.specs_for(request):
            validator = self._validators.get(spec.name)
            started = time.perf_counter()
            if validator is None:
                evidence.append(
                    ValidationEvidenceV1(
                        validator_name=spec.name,
                        validator_version="unknown",
                        status="error",
                        evidence={"required": spec.required},
                        failure_reason=f"validator is not registered: {spec.name}",
                    )
                )
                continue
            try:
                ok, score, detail, reason = validator.validate(value)
                if spec.minimum_score is not None and (score is None or score < spec.minimum_score):
                    ok = False
                    reason = reason or f"score {score} is below required minimum {spec.minimum_score}"
                detail = {**detail, "required": spec.required}
                evidence.append(
                    ValidationEvidenceV1(
                        validator_name=validator.name,
                        validator_version=validator.version,
                        status="passed" if ok else "failed",
                        score=score,
                        evidence=detail,
                        failure_reason=reason,
                        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    )
                )
            except Exception as exc:
                evidence.append(
                    ValidationEvidenceV1(
                        validator_name=validator.name,
                        validator_version=validator.version,
                        status="error",
                        evidence={"required": spec.required},
                        failure_reason=f"{type(exc).__name__}: {exc}"[:500],
                        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    )
                )
        return evidence

    @staticmethod
    def accepted(evidence: list[ValidationEvidenceV1]) -> bool:
        required = [item for item in evidence if item.evidence.get("required", True)]
        return bool(required) and all(item.status == "passed" for item in required)
