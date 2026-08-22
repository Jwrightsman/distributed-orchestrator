"""Versioned validator registry with contract floors and honest assurance."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from execution.contracts import (
    AssuranceLevelV1,
    ExecutionRequestV1,
    MAX_ARTIFACTS_V1,
    OutputContractV1,
    RequirementSourceV1,
    ValidationAggregationV1,
    ValidationEvidenceV1,
    ValidationSummaryV1,
    ValidatorSpecV1,
    normalize_manifest_path,
)
from extract import check_code_files


_ASSURANCE_STRENGTH: dict[AssuranceLevelV1, int] = {
    "unverified": 0,
    "structural": 1,
    "model_judged": 2,
    "deterministic": 3,
}

_FORMAT_EXTENSIONS: dict[str, frozenset[str]] = {
    "py": frozenset({".py"}),
    "python": frozenset({".py"}),
    "html": frozenset({".html", ".htm"}),
    "htm": frozenset({".html", ".htm"}),
    "javascript": frozenset({".js", ".mjs", ".cjs"}),
    "js": frozenset({".js", ".mjs", ".cjs"}),
    "typescript": frozenset({".ts", ".tsx"}),
    "ts": frozenset({".ts", ".tsx"}),
    "json": frozenset({".json"}),
    "markdown": frozenset({".md", ".markdown"}),
    "md": frozenset({".md", ".markdown"}),
    "css": frozenset({".css"}),
    "csv": frozenset({".csv"}),
    "svg": frozenset({".svg"}),
    "text": frozenset({".txt"}),
    "txt": frozenset({".txt"}),
    "yaml": frozenset({".yaml", ".yml"}),
    "yml": frozenset({".yaml", ".yml"}),
}


@dataclass(frozen=True)
class ValidationInput:
    output: str
    files: list[str]
    contract: OutputContractV1 | None
    artifact_root: Path | None = None


@dataclass(frozen=True)
class ResolvedValidatorSpec:
    """One validator after contract floors and explicit policy are merged."""

    name: str
    required: bool
    minimum_score: float | None
    source: RequirementSourceV1
    aggregation: ValidationAggregationV1


class Validator(Protocol):
    name: str
    version: str
    assurance_level: AssuranceLevelV1
    proves_behavioral_correctness: bool

    def validate(self, value: ValidationInput) -> tuple[bool, float | None, dict[str, Any], str | None]: ...


def _json_document(text: str) -> Any:
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL | re.IGNORECASE)
    if fenced:
        stripped = fenced.group(1)
    return json.loads(stripped)


def _actual_artifact_paths(value: ValidationInput) -> tuple[list[str], list[str]]:
    """Return normalized logical paths and any path-safety failures.

    Callers materializing files should supply ``artifact_root``. Absolute server
    paths are then reduced relative to that root and cannot escape it. The
    root-less mode remains useful for validators used directly with logical
    relative paths and for compatibility with older callers.
    """

    if not value.files:
        return [], []

    root = value.artifact_root.resolve() if value.artifact_root else None
    raw_paths = [Path(item) for item in value.files]
    inferred_root: Path | None = None
    if root is None and all(path.is_absolute() for path in raw_paths):
        try:
            inferred_root = Path(os.path.commonpath([str(path.resolve().parent) for path in raw_paths]))
        except ValueError:
            return [], ["artifact files do not share a filesystem root"]
    elif root is None and any(path.is_absolute() for path in raw_paths):
        return [], ["artifact paths cannot mix absolute and relative forms"]

    normalized: list[str] = []
    failures: list[str] = []
    for raw in raw_paths:
        try:
            if root is not None:
                if raw.is_absolute():
                    resolved = raw.resolve()
                else:
                    direct = raw.resolve()
                    try:
                        direct.relative_to(root)
                        resolved = direct
                    except ValueError:
                        resolved = (root / raw).resolve()
                try:
                    logical = resolved.relative_to(root).as_posix()
                except ValueError as exc:
                    raise ValueError("artifact path escapes its materialization root") from exc
            elif inferred_root is not None:
                logical = raw.resolve().relative_to(inferred_root).as_posix()
            else:
                # ``Path.as_posix`` converts native Windows separators emitted
                # by internal materializers. Caller-supplied manifest paths are
                # separately validated strictly in OutputContractV1.
                logical = raw.as_posix()
            normalized.append(normalize_manifest_path(logical))
        except (OSError, ValueError) as exc:
            failures.append(f"{raw}: {exc}")

    portable = [path.casefold() for path in normalized]
    if len(set(portable)) != len(portable):
        failures.append("artifact paths contain duplicate normalized paths")
    return normalized, failures


class NonemptyValidator:
    name = "nonempty"
    version = "2"
    assurance_level: AssuranceLevelV1 = "structural"
    proves_behavioral_correctness = False

    def validate(self, value):
        size = len(value.output.strip().encode("utf-8"))
        ok = size > 0
        return ok, 1.0 if ok else 0.0, {"output_bytes": size}, None if ok else "candidate output is empty"


class ArtifactExtractionValidator:
    name = "artifact_extraction"
    version = "2"
    assurance_level: AssuranceLevelV1 = "structural"
    proves_behavioral_correctness = False

    def validate(self, value):
        count = len(value.files)
        ok = count > 0
        return ok, 1.0 if ok else 0.0, {"file_count": count}, None if ok else "no artifacts were extracted"


class ArtifactContractValidator:
    name = "artifact_contract"
    version = "1"
    assurance_level: AssuranceLevelV1 = "structural"
    proves_behavioral_correctness = False

    def validate(self, value):
        paths, path_failures = _actual_artifact_paths(value)
        count = len(value.files)
        contract = value.contract
        expected = contract.artifact_count if contract else None
        failures = list(path_failures)
        if count > MAX_ARTIFACTS_V1:
            failures.append(f"artifact count {count} exceeds maximum {MAX_ARTIFACTS_V1}")
        if expected is not None and count != expected:
            failures.append(f"expected exactly {expected} artifact(s), received {count}")

        detail: dict[str, Any] = {
            "artifact_count": count,
            "expected_artifact_count": expected,
            "maximum_artifact_count": MAX_ARTIFACTS_V1,
            "normalized_paths": paths,
        }
        required_format = contract.format if contract else None
        if required_format:
            allowed = _FORMAT_EXTENSIONS.get(required_format)
            if allowed is None:
                detail["format_check"] = "not_mechanically_supported"
                detail["not_checked"] = [f"artifact_format:{required_format}"]
            else:
                wrong = [path for path in paths if Path(path).suffix.lower() not in allowed]
                detail.update(
                    {
                        "format_check": "checked",
                        "required_format": required_format,
                        "allowed_extensions": sorted(allowed),
                        "wrong_format": wrong,
                    }
                )
                if wrong:
                    failures.append(
                        f"artifact format must be {required_format}; wrong-format paths: {', '.join(wrong)}"
                    )

        ok = not failures
        return ok, 1.0 if ok else 0.0, detail, None if ok else "; ".join(failures)[:500]


class CodeParseValidator:
    name = "code_parse"
    version = "2"
    assurance_level: AssuranceLevelV1 = "structural"
    proves_behavioral_correctness = False

    def validate(self, value):
        if not value.files:
            return False, 0.0, {"problems": []}, "no files were available to parse"
        supported = [path for path in value.files if Path(path).suffix.lower() in {".py", ".html"}]
        unsupported = [path for path in value.files if path not in supported]
        problems = check_code_files(supported)
        if unsupported:
            problems.append(
                "no supported parser for: " + ", ".join(Path(path).name for path in unsupported[:10])
            )
        ok = bool(supported) and not problems
        evidence = {
            "checked_files": [str(path) for path in supported],
            "unsupported_files": [str(path) for path in unsupported],
            "problems": problems[:10],
            "not_checked": [f"code_parse:{Path(path).name}" for path in unsupported],
        }
        reason = None if ok else "; ".join(problems[:3])[:500] or "no supported code parser was available"
        return ok, 1.0 if ok else 0.0, evidence, reason


class StructuredJsonValidator:
    name = "structured_json"
    version = "2"
    assurance_level: AssuranceLevelV1 = "structural"
    proves_behavioral_correctness = False

    def validate(self, value):
        try:
            parsed = _json_document(value.output)
        except (json.JSONDecodeError, ValueError) as exc:
            return False, 0.0, {}, f"output is not valid JSON: {exc}"
        kind = "object" if isinstance(parsed, dict) else "array" if isinstance(parsed, list) else type(parsed).__name__
        return True, 1.0, {"json_type": kind}, None


class JsonSchemaValidator:
    name = "json_schema"
    version = "2"
    assurance_level: AssuranceLevelV1 = "deterministic"
    proves_behavioral_correctness = False

    def validate(self, value):
        schema = value.contract.json_schema if value.contract else None
        if schema is None:
            return False, 0.0, {}, "json_schema validator requires output_contract.json_schema"
        try:
            parsed = _json_document(value.output)
            Draft202012Validator(schema).validate(parsed)
        except Exception as exc:
            return False, 0.0, {}, f"JSON Schema validation failed: {exc}"[:500]
        return True, 1.0, {"schema_valid": True, "claim": "contract_conformance"}, None


class FileManifestValidator:
    name = "file_manifest"
    version = "2"
    assurance_level: AssuranceLevelV1 = "structural"
    proves_behavioral_correctness = False

    def validate(self, value):
        required = list(value.contract.required_files) if value.contract else []
        available, path_failures = _actual_artifact_paths(value)
        required_keys = {path.casefold(): path for path in required}
        available_keys = {path.casefold(): path for path in available}
        missing = [required_keys[key] for key in required_keys.keys() - available_keys.keys()]
        unexpected = [available_keys[key] for key in available_keys.keys() - required_keys.keys()]
        ok = bool(required) and not missing and not unexpected and not path_failures
        reason_parts = []
        if not required:
            reason_parts.append("no file manifest supplied")
        if missing:
            reason_parts.append("missing required files: " + ", ".join(sorted(missing)))
        if unexpected:
            reason_parts.append("unexpected files: " + ", ".join(sorted(unexpected)))
        reason_parts.extend(path_failures)
        return (
            ok,
            1.0 if ok else 0.0,
            {
                "required": required,
                "available": sorted(available),
                "missing": sorted(missing),
                "unexpected": sorted(unexpected),
            },
            None if ok else "; ".join(reason_parts)[:500],
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
            ArtifactContractValidator(),
        ):
            registry.register(validator)
        return registry

    def register(self, validator: Validator) -> None:
        if validator.name in self._validators:
            raise ValueError(f"validator already registered: {validator.name}")
        self._validators[validator.name] = validator

    def has(self, name: str) -> bool:
        return name in self._validators

    @staticmethod
    def _floor_names(contract: OutputContractV1 | None) -> list[str]:
        names = ["nonempty"]
        if contract is None:
            return names
        if contract.kind == "structured_json":
            names.append("structured_json")
            if contract.json_schema is not None:
                names.append("json_schema")
        elif contract.kind == "file_manifest":
            names.extend(("artifact_extraction", "artifact_contract", "file_manifest"))
        elif contract.kind == "single_artifact":
            names.extend(("artifact_extraction", "artifact_contract"))
            if contract.format in {"py", "python", "html"}:
                names.append("code_parse")
        elif contract.kind == "code":
            names.extend(("artifact_extraction", "artifact_contract", "code_parse"))
        return names

    def specs_for(self, request: ExecutionRequestV1) -> list[ResolvedValidatorSpec]:
        """Resolve immutable contract floors plus caller-added validators."""

        resolved: dict[str, ResolvedValidatorSpec] = {
            name: ResolvedValidatorSpec(
                name=name,
                required=True,
                minimum_score=None,
                source="contract_floor",
                aggregation="all",
            )
            for name in self._floor_names(request.output_contract)
        }
        explicit: list[ValidatorSpecV1] = []
        if request.output_contract:
            explicit.extend(request.output_contract.validators)
        explicit.extend(request.verification.validators)
        aggregation: ValidationAggregationV1 = "all" if request.verification.require_all else "any"
        for spec in explicit:
            existing = resolved.get(spec.name)
            minimum_score = spec.minimum_score
            if existing is not None:
                if existing.minimum_score is not None:
                    minimum_score = max(existing.minimum_score, minimum_score or 0.0)
                resolved[spec.name] = ResolvedValidatorSpec(
                    name=existing.name,
                    required=True if existing.source == "contract_floor" else existing.required or spec.required,
                    minimum_score=minimum_score,
                    source=existing.source,
                    aggregation=existing.aggregation,
                )
                continue
            resolved[spec.name] = ResolvedValidatorSpec(
                name=spec.name,
                required=spec.required,
                minimum_score=minimum_score,
                source="explicit",
                aggregation=aggregation,
            )
        return list(resolved.values())

    def validate(
        self,
        request: ExecutionRequestV1,
        output: str,
        files: list[str],
        *,
        artifact_root: str | Path | None = None,
    ) -> list[ValidationEvidenceV1]:
        value = ValidationInput(
            output=output,
            files=files,
            contract=request.output_contract,
            artifact_root=Path(artifact_root) if artifact_root is not None else None,
        )
        evidence: list[ValidationEvidenceV1] = []
        for spec in self.specs_for(request):
            validator = self._validators.get(spec.name)
            started = time.perf_counter()
            metadata = {
                "required": spec.required,
                "requirement_source": spec.source,
                "aggregation": spec.aggregation,
            }
            if validator is None:
                evidence.append(
                    ValidationEvidenceV1(
                        validator_name=spec.name,
                        validator_version="unknown",
                        status="error",
                        assurance_level="unverified",
                        requirement_source=spec.source,
                        required=spec.required,
                        aggregation=spec.aggregation,
                        evidence=metadata,
                        failure_reason=f"validator is not registered: {spec.name}",
                    )
                )
                continue
            try:
                ok, score, detail, reason = validator.validate(value)
                if spec.minimum_score is not None and (score is None or score < spec.minimum_score):
                    ok = False
                    reason = reason or f"score {score} is below required minimum {spec.minimum_score}"
                evidence.append(
                    ValidationEvidenceV1(
                        validator_name=validator.name,
                        validator_version=validator.version,
                        status="passed" if ok else "failed",
                        assurance_level=validator.assurance_level,
                        proves_behavioral_correctness=validator.proves_behavioral_correctness,
                        requirement_source=spec.source,
                        required=spec.required,
                        aggregation=spec.aggregation,
                        score=score,
                        evidence={**detail, **metadata},
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
                        assurance_level=validator.assurance_level,
                        proves_behavioral_correctness=validator.proves_behavioral_correctness,
                        requirement_source=spec.source,
                        required=spec.required,
                        aggregation=spec.aggregation,
                        evidence=metadata,
                        failure_reason=f"{type(exc).__name__}: {exc}"[:500],
                        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    )
                )
        return evidence

    @staticmethod
    def accepted(evidence: list[ValidationEvidenceV1]) -> bool:
        floors = [item for item in evidence if item.requirement_source == "contract_floor" and item.required]
        if not floors or not all(item.status == "passed" for item in floors):
            return False
        explicit = [item for item in evidence if item.requirement_source == "explicit" and item.required]
        if not explicit:
            return True
        require_all = all(item.aggregation == "all" for item in explicit)
        if require_all:
            return all(item.status == "passed" for item in explicit)
        return any(item.status == "passed" for item in explicit)

    @classmethod
    def summarize(cls, evidence: list[ValidationEvidenceV1]) -> ValidationSummaryV1:
        if not evidence:
            return ValidationSummaryV1(checks_not_run=["validation", "behavioral_correctness"])

        run = [item for item in evidence if item.status != "skipped"]
        passed = [item.validator_name for item in run if item.status == "passed"]
        failed = [item.validator_name for item in run if item.status in ("failed", "error")]
        not_run = [item.validator_name for item in evidence if item.status == "skipped"]
        for item in evidence:
            detail = item.evidence.get("not_checked", [])
            if isinstance(detail, list):
                not_run.extend(str(value) for value in detail)

        behavioral = any(
            item.status == "passed" and item.proves_behavioral_correctness for item in evidence
        )
        if not behavioral:
            not_run.append("behavioral_correctness")

        passed_levels = [item.assurance_level for item in evidence if item.status == "passed"]
        assurance: AssuranceLevelV1 = max(
            passed_levels or ["unverified"],
            key=lambda level: _ASSURANCE_STRENGTH[level],
        )
        if cls.accepted(evidence):
            outcome = "passed"
        elif passed:
            outcome = "partial"
        else:
            outcome = "failed"

        claim = (
            "Behavioral correctness evidence passed."
            if behavioral
            else "These checks do not establish behavioral correctness."
        )
        explanation = (
            f"Validation outcome is {outcome}; assurance is {assurance}. "
            f"{len(passed)} check(s) passed and {len(failed)} failed or errored. {claim}"
        )
        return ValidationSummaryV1(
            outcome=outcome,
            assurance_level=assurance,
            checks_run=[item.validator_name for item in run],
            checks_passed=passed,
            checks_failed=failed,
            checks_not_run=list(dict.fromkeys(not_run)),
            proves_behavioral_correctness=behavioral,
            explanation=explanation,
        )

    def validate_with_summary(
        self,
        request: ExecutionRequestV1,
        output: str,
        files: list[str],
        *,
        artifact_root: str | Path | None = None,
    ) -> tuple[list[ValidationEvidenceV1], ValidationSummaryV1]:
        evidence = self.validate(request, output, files, artifact_root=artifact_root)
        return evidence, self.summarize(evidence)

    @classmethod
    def assurance_level(cls, evidence: list[ValidationEvidenceV1]) -> AssuranceLevelV1:
        """Return the strongest assurance actually earned by passing checks."""

        return cls.summarize(evidence).assurance_level

    @staticmethod
    def assurance_strength(level: AssuranceLevelV1) -> int:
        return _ASSURANCE_STRENGTH[level]
