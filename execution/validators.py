"""Versioned validator registry with contract floors and honest assurance."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence

from jsonschema import Draft202012Validator, ValidationError as JsonSchemaValidationError

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
from execution.artifacts import ArtifactEntryV1
from execution.validator_process import (
    CancellationSignal,
    ValidatorProcessExecutor,
    ValidatorProcessSettings,
)
from execution.validator_protocol import (
    VALIDATOR_RUNNER_PROTOCOL_VERSION_V2,
    ValidatorRunnerRequest,
    ValidatorRunnerRequestV2,
    ValidatorRunnerResponse,
    ValidatorRunnerResponseV1,
    ValidatorRunnerResponseV2,
)
from execution.validator_staging import (
    ValidatorOutputReferenceError,
    read_staged_validator_output,
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

ValidatorExecutionPolicy = Literal["inline_trusted", "subprocess_isolated"]


@dataclass(frozen=True)
class _CombinedCancellationSignal:
    local: threading.Event
    external: CancellationSignal | None

    def is_set(self) -> bool:
        return self.local.is_set() or (
            self.external is not None and self.external.is_set()
        )


async def _await_owned_thread_cleanup(
    work: asyncio.Task[Any],
    *,
    timeout_seconds: float = 10.0,
) -> bool:
    """Wait through repeated caller cancellation until owned thread cleanup ends."""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        if work.done():
            try:
                work.result()
            except BaseException:
                pass
            return True
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        try:
            await asyncio.wait_for(asyncio.shield(work), timeout=remaining)
            return True
        except asyncio.CancelledError:
            # A second cancellation request must not detach the still-owned
            # worker while it is terminating and reaping the child process.
            continue
        except asyncio.TimeoutError:
            return work.done()
        except BaseException:
            # The work is complete even when it failed; the result is irrelevant
            # because the caller is already propagating cancellation.
            return work.done()


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
    execution_policy: ValidatorExecutionPolicy
    assurance_level: AssuranceLevelV1
    proves_behavioral_correctness: bool

    def validate(self, value: ValidationInput) -> tuple[bool, float | None, dict[str, Any], str | None]: ...


def _json_document(text: str) -> Any:
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL | re.IGNORECASE)
    if fenced:
        stripped = fenced.group(1)
    return json.loads(stripped)


def _actual_artifact_file_map(
    value: ValidationInput,
) -> tuple[list[tuple[str, Path]], list[str]]:
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

    normalized: list[tuple[str, Path]] = []
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
                resolved = raw.resolve()
                logical = resolved.relative_to(inferred_root).as_posix()
            else:
                # ``Path.as_posix`` converts native Windows separators emitted
                # by internal materializers. Caller-supplied manifest paths are
                # separately validated strictly in OutputContractV1.
                logical = raw.as_posix()
                resolved = raw
            normalized.append((normalize_manifest_path(logical), resolved))
        except (OSError, ValueError):
            failures.append("artifact path is unavailable or outside its validation root")

    portable = [path.casefold() for path, _ in normalized]
    if len(set(portable)) != len(portable):
        failures.append("artifact paths contain duplicate normalized paths")
    return normalized, failures


def _actual_artifact_paths(value: ValidationInput) -> tuple[list[str], list[str]]:
    mapped, failures = _actual_artifact_file_map(value)
    return [logical for logical, _ in mapped], failures


class NonemptyValidator:
    name = "nonempty"
    version = "2"
    execution_policy: ValidatorExecutionPolicy = "inline_trusted"
    assurance_level: AssuranceLevelV1 = "structural"
    proves_behavioral_correctness = False

    def validate(self, value):
        size = len(value.output.strip().encode("utf-8"))
        ok = size > 0
        return ok, 1.0 if ok else 0.0, {"output_bytes": size}, None if ok else "candidate output is empty"


class ArtifactExtractionValidator:
    name = "artifact_extraction"
    version = "2"
    execution_policy: ValidatorExecutionPolicy = "inline_trusted"
    assurance_level: AssuranceLevelV1 = "structural"
    proves_behavioral_correctness = False

    def validate(self, value):
        count = len(value.files)
        ok = count > 0
        return ok, 1.0 if ok else 0.0, {"file_count": count}, None if ok else "no artifacts were extracted"


class ArtifactContractValidator:
    name = "artifact_contract"
    version = "1"
    execution_policy: ValidatorExecutionPolicy = "inline_trusted"
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
    execution_policy: ValidatorExecutionPolicy = "subprocess_isolated"
    assurance_level: AssuranceLevelV1 = "structural"
    proves_behavioral_correctness = False

    def validate(self, value):
        mapped, path_failures = _actual_artifact_file_map(value)
        if not mapped:
            return False, 0.0, {"problems": []}, (
                path_failures[0] if path_failures else "no files were available to parse"
            )
        supported = [
            (logical, path)
            for logical, path in mapped
            if path.suffix.lower() in {".py", ".html"}
        ]
        unsupported = [logical for logical, path in mapped if path.suffix.lower() not in {".py", ".html"}]
        problems = list(path_failures)
        problems.extend(check_code_files([str(path) for _, path in supported]))
        if unsupported:
            problems.append(
                "no supported parser for: " + ", ".join(Path(path).name for path in unsupported[:10])
            )
        ok = bool(supported) and not problems
        evidence = {
            "checked_files": [logical for logical, _ in supported],
            "unsupported_files": unsupported,
            "problems": problems[:10],
            "not_checked": [f"code_parse:{Path(path).name}" for path in unsupported],
        }
        reason = None if ok else "; ".join(problems[:3])[:500] or "no supported code parser was available"
        return ok, 1.0 if ok else 0.0, evidence, reason


class StructuredJsonValidator:
    name = "structured_json"
    version = "2"
    execution_policy: ValidatorExecutionPolicy = "subprocess_isolated"
    assurance_level: AssuranceLevelV1 = "structural"
    proves_behavioral_correctness = False

    def validate(self, value):
        try:
            parsed = _json_document(value.output)
        except (json.JSONDecodeError, ValueError, MemoryError, RecursionError):
            return False, 0.0, {}, "output is not valid JSON"
        kind = "object" if isinstance(parsed, dict) else "array" if isinstance(parsed, list) else type(parsed).__name__
        return True, 1.0, {"json_type": kind}, None


class JsonSchemaValidator:
    name = "json_schema"
    version = "2"
    execution_policy: ValidatorExecutionPolicy = "subprocess_isolated"
    assurance_level: AssuranceLevelV1 = "deterministic"
    proves_behavioral_correctness = False

    def validate(self, value):
        schema = value.contract.json_schema if value.contract else None
        if schema is None:
            return False, 0.0, {}, "json_schema validator requires output_contract.json_schema"
        try:
            parsed = _json_document(value.output)
            Draft202012Validator(schema).validate(parsed)
        except JsonSchemaValidationError:
            return False, 0.0, {}, "JSON Schema validation failed"
        except (json.JSONDecodeError, ValueError, MemoryError, RecursionError):
            return False, 0.0, {}, "JSON Schema input could not be parsed"
        return True, 1.0, {"schema_valid": True, "claim": "contract_conformance"}, None


class FileManifestValidator:
    name = "file_manifest"
    version = "2"
    execution_policy: ValidatorExecutionPolicy = "inline_trusted"
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
                "required_files": required,
                "available": sorted(available),
                "missing": sorted(missing),
                "unexpected": sorted(unexpected),
            },
            None if ok else "; ".join(reason_parts)[:500],
        )


def _builtin_validators() -> tuple[Validator, ...]:
    return (
        NonemptyValidator(),
        StructuredJsonValidator(),
        JsonSchemaValidator(),
        FileManifestValidator(),
        CodeParseValidator(),
        ArtifactExtractionValidator(),
        ArtifactContractValidator(),
    )


_BUILTIN_VALIDATOR_TYPES = (
    NonemptyValidator,
    StructuredJsonValidator,
    JsonSchemaValidator,
    FileManifestValidator,
    CodeParseValidator,
    ArtifactExtractionValidator,
    ArtifactContractValidator,
)


def execute_runner_request(
    request: ValidatorRunnerRequest,
    *,
    stage_root: Path,
) -> ValidatorRunnerResponse:
    """Execute one already validated built-in request inside the child.

    The request protocol owns the allowlist.  Assurance, required/optional
    policy, aggregation, containment labels, and acceptance remain parent-side
    authority and therefore are deliberately absent here.
    """

    validator = next(
        (
            item
            for item in _builtin_validators()
            if item.name == request.validator_name and item.version == request.validator_version
        ),
        None,
    )
    if validator is None:
        raise ValueError("validator is not in the built-in runner allowlist")
    # The strict wire projection deliberately omits every contract field this
    # selected validator does not consume.  Built-ins only read the projected
    # attributes, so reconstructing a broader canonical contract is neither
    # necessary nor desirable inside the child.  Only code_parse receives
    # copied content beneath stage_root.  Metadata validators receive already
    # validated logical names and must not resolve them back to host paths.
    stage_root = stage_root.resolve(strict=True)
    if isinstance(request, ValidatorRunnerRequestV2):
        output = ""
        if request.output_reference is not None:
            try:
                output = read_staged_validator_output(
                    staging_root=stage_root,
                    relative_path=request.output_reference.relative_path,
                    encoding=request.output_reference.encoding,
                    byte_length=request.output_reference.byte_length,
                    sha256=request.output_reference.sha256,
                )
            except ValidatorOutputReferenceError as exc:
                return ValidatorRunnerResponseV2(
                    validator_name=request.validator_name,
                    validator_version=request.validator_version,
                    ok=False,
                    detail={},
                    failure_reason=exc.code,
                )
        response_model: type[ValidatorRunnerResponseV1] | type[ValidatorRunnerResponseV2] = (
            ValidatorRunnerResponseV2
        )
    else:
        output = request.output or ""
        response_model = ValidatorRunnerResponseV1

    contract = request.contract
    value = ValidationInput(
        output=output,
        files=list(request.staged_files),
        contract=contract,
        artifact_root=stage_root if request.validator_name == "code_parse" else None,
    )
    ok, score, detail, reason = validator.validate(value)
    return response_model(
        validator_name=validator.name,
        validator_version=validator.version,
        ok=ok,
        score=score,
        detail=detail,
        failure_reason=reason,
    )


@dataclass(frozen=True, slots=True)
class ParsePrecheckResult:
    """What the legacy parse precheck concluded, with the runner's own failures
    kept out of the defect list.

    `problems` describes the code, and is populated only when the parser
    actually reached a verdict about it.  When the runner could not produce one
    -- spawn failure, timeout, cancellation, rejected input, a malformed
    response -- `runner_failure` carries the stable reason and `problems` stays
    empty, because nothing was learned about the code.

    This is a record rather than a list because the two facts used to travel in
    one `list[str]`, where `"validator_timeout"` sat beside
    `"main.py is not valid Python (line 3)"` and no caller could tell them
    apart.  A record is not iterable, indexable, or sized, so a caller that
    still treats the verdict as a bare list of defects fails loudly instead of
    blaming a worker's code for a starved coordinator.
    """

    problems: tuple[str, ...] = ()
    runner_failure: str | None = None

    def __post_init__(self) -> None:
        if self.runner_failure is not None and self.problems:
            raise ValueError(
                "a precheck that did not reach a verdict has no code problems to report"
            )

    @property
    def reached_a_verdict(self) -> bool:
        """True when `problems` is a statement about the code under test."""

        return self.runner_failure is None


def check_code_files_isolated(
    paths: Sequence[str | Path],
    *,
    artifact_root: str | Path | None = None,
    authoritative_artifact_root: str | Path | None = None,
    validated_entries: Sequence[ArtifactEntryV1] | None = None,
    process_executor: ValidatorProcessExecutor | None = None,
    deadline_monotonic: float | None = None,
    cancel_event: CancellationSignal | None = None,
) -> ParsePrecheckResult:
    """Compatibility parser check using the same bounded child boundary.

    Legacy extraction/repair uses this before canonical validation evidence is
    assembled.  Bounded parser problems and infrastructure failures are
    returned through separate fields of `ParsePrecheckResult`; an
    infrastructure failure never becomes a code problem and never triggers an
    inline parser fallback.
    """

    if not paths:
        return ParsePrecheckResult()
    from config import get as get_config

    selected = [Path(path) for path in paths]
    root = Path(artifact_root) if artifact_root is not None else None
    if root is None:
        try:
            root = Path(os.path.commonpath([str(path.resolve().parent) for path in selected]))
        except (OSError, ValueError):
            return ParsePrecheckResult(runner_failure="validator_input_rejected")
    executor = process_executor or ValidatorProcessExecutor(
        ValidatorProcessSettings.from_config(get_config())
    )
    if executor.settings.execution_mode == "inline":
        # Explicit local-development compatibility is intentionally weaker.
        # Trusted-alpha preflight rejects this configuration.
        return ParsePrecheckResult(
            problems=tuple(check_code_files([str(path) for path in selected])[:10])
        )
    outcome = executor.execute(
        validator_name="code_parse",
        validator_version="2",
        output="",
        files=selected,
        contract=None,
        artifact_root=root,
        authoritative_artifact_root=authoritative_artifact_root,
        validated_entries=validated_entries,
        deadline_monotonic=deadline_monotonic,
        cancel_event=cancel_event,
    )
    if not outcome.completed:
        return ParsePrecheckResult(
            runner_failure=outcome.failure_reason or "validator_execution_error"
        )
    problems = outcome.detail.get("problems", [])
    if not isinstance(problems, list):
        # An off-shape response is a protocol failure by ADR 0013, so it says
        # nothing about the code either.
        return ParsePrecheckResult(runner_failure="validator_malformed_response")
    return ParsePrecheckResult(
        problems=tuple(str(problem)[:500] for problem in problems[:10])
    )


async def check_code_files_isolated_async(
    paths: Sequence[str | Path],
    *,
    artifact_root: str | Path | None = None,
    authoritative_artifact_root: str | Path | None = None,
    validated_entries: Sequence[ArtifactEntryV1] | None = None,
    process_executor: ValidatorProcessExecutor | None = None,
    deadline_monotonic: float | None = None,
    cancel_event: CancellationSignal | None = None,
) -> ParsePrecheckResult:
    """Run the legacy parse precheck off-loop with owned cancellation."""

    if process_executor is None:
        from config import get as get_config

        process_executor = ValidatorProcessExecutor(
            ValidatorProcessSettings.from_config(get_config())
        )
    local_cancel = threading.Event()
    combined_cancel = _CombinedCancellationSignal(local_cancel, cancel_event)
    work = asyncio.create_task(
        asyncio.to_thread(
            check_code_files_isolated,
            paths,
            artifact_root=artifact_root,
            authoritative_artifact_root=authoritative_artifact_root,
            validated_entries=validated_entries,
            process_executor=process_executor,
            deadline_monotonic=deadline_monotonic,
            cancel_event=combined_cancel,
        )
    )
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        local_cancel.set()
        cleaned = await _await_owned_thread_cleanup(work)
        if not cleaned:
            process_executor.record_cleanup_failure()
        raise


class ValidatorRegistry:
    def __init__(
        self,
        *,
        process_settings: ValidatorProcessSettings | None = None,
        process_executor: ValidatorProcessExecutor | None = None,
    ):
        self._validators: dict[str, Validator] = {}
        self.process_executor = process_executor or ValidatorProcessExecutor(process_settings)
        self.process_settings = self.process_executor.settings

    @classmethod
    def default(
        cls,
        *,
        process_settings: ValidatorProcessSettings | None = None,
        process_executor: ValidatorProcessExecutor | None = None,
    ) -> "ValidatorRegistry":
        registry = cls(
            process_settings=process_settings,
            process_executor=process_executor,
        )
        for validator in _builtin_validators():
            registry.register(validator)
        return registry

    def register(self, validator: Validator) -> None:
        if type(validator) not in _BUILTIN_VALIDATOR_TYPES:
            raise ValueError("only closed built-in validator implementations may register")
        if validator.name in self._validators:
            raise ValueError(f"validator already registered: {validator.name}")
        if validator.execution_policy not in {"inline_trusted", "subprocess_isolated"}:
            raise ValueError("validator execution policy is invalid")
        self._validators[validator.name] = validator

    def has(self, name: str) -> bool:
        return name in self._validators

    def diagnostics(self) -> dict[str, Any]:
        return {
            "configured_execution_mode": self.process_settings.execution_mode,
            "validators": [
                {
                    "name": item.name,
                    "version": item.version,
                    "execution_policy": item.execution_policy,
                }
                for item in self._validators.values()
            ],
            "runner": self.process_executor.diagnostics(),
        }

    def _actual_execution_mode(self, validator: Validator) -> str:
        configured = self.process_settings.execution_mode
        if configured == "subprocess":
            return "subprocess_isolated"
        if configured == "inline":
            return (
                "inline_trusted"
                if validator.execution_policy == "inline_trusted"
                else "inline_compatibility"
            )
        return validator.execution_policy

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
        authoritative_artifact_root: str | Path | None = None,
        validated_entries: Sequence[ArtifactEntryV1] | None = None,
        deadline_monotonic: float | None = None,
        cancel_event: CancellationSignal | None = None,
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
                        evidence={**metadata, "execution_mode": "not_run"},
                        failure_reason="validator_not_registered",
                    )
                )
                continue

            execution_mode = self._actual_execution_mode(validator)
            metadata.update(
                {
                    "execution_mode": execution_mode,
                    "validator_execution_policy": validator.execution_policy,
                    "runner_protocol_version": (
                        VALIDATOR_RUNNER_PROTOCOL_VERSION_V2
                        if execution_mode == "subprocess_isolated"
                        else None
                    ),
                    "containment_level": (
                        "coordinator_process"
                        if execution_mode != "subprocess_isolated"
                        else self.process_executor.diagnostics()["containment_level"]
                    ),
                }
            )
            if cancel_event is not None and cancel_event.is_set():
                metadata["termination_reason"] = "validator_cancelled"
                evidence.append(
                    ValidationEvidenceV1(
                        validator_name=validator.name,
                        validator_version=validator.version,
                        status="error",
                        assurance_level=validator.assurance_level,
                        proves_behavioral_correctness=(
                            validator.proves_behavioral_correctness
                        ),
                        requirement_source=spec.source,
                        required=spec.required,
                        aggregation=spec.aggregation,
                        evidence=metadata,
                        failure_reason="validator_cancelled",
                        duration_ms=max(
                            0,
                            int((time.perf_counter() - started) * 1000),
                        ),
                    )
                )
                continue
            try:
                if execution_mode == "subprocess_isolated":
                    outcome = self.process_executor.execute(
                        validator_name=validator.name,
                        validator_version=validator.version,
                        output=output,
                        files=files,
                        contract=request.output_contract,
                        artifact_root=artifact_root,
                        max_output_bytes=request.max_output_bytes,
                        authoritative_artifact_root=authoritative_artifact_root,
                        validated_entries=validated_entries,
                        deadline_monotonic=deadline_monotonic,
                        cancel_event=cancel_event,
                    )
                    metadata["containment_level"] = outcome.containment_level
                    if outcome.termination_reason is not None:
                        metadata["termination_reason"] = outcome.termination_reason
                    if not outcome.completed:
                        evidence.append(
                            ValidationEvidenceV1(
                                validator_name=validator.name,
                                validator_version=validator.version,
                                status="error",
                                assurance_level=validator.assurance_level,
                                proves_behavioral_correctness=(
                                    validator.proves_behavioral_correctness
                                ),
                                requirement_source=spec.source,
                                required=spec.required,
                                aggregation=spec.aggregation,
                                evidence=metadata,
                                failure_reason=outcome.failure_reason,
                                duration_ms=max(
                                    0,
                                    int((time.perf_counter() - started) * 1000),
                                ),
                            )
                        )
                        continue
                    ok, score, detail, reason = (
                        outcome.ok,
                        outcome.score,
                        outcome.detail,
                        outcome.failure_reason,
                    )
                else:
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
            except Exception:
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
                        failure_reason="validator_execution_error",
                        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    )
                )
        return evidence

    async def validate_async(
        self,
        request: ExecutionRequestV1,
        output: str,
        files: list[str],
        *,
        artifact_root: str | Path | None = None,
        authoritative_artifact_root: str | Path | None = None,
        validated_entries: Sequence[ArtifactEntryV1] | None = None,
        deadline_monotonic: float | None = None,
        cancel_event: CancellationSignal | None = None,
    ) -> list[ValidationEvidenceV1]:
        """Run validators off-loop while retaining cancellation ownership.

        ``asyncio.to_thread`` alone detaches its worker when the await is
        cancelled.  Shield the worker, signal the process runner, and wait a
        finite cleanup interval before propagating cancellation so the child is
        terminated and reaped first.
        """

        local_cancel = threading.Event()
        combined_cancel = _CombinedCancellationSignal(local_cancel, cancel_event)
        work = asyncio.create_task(
            asyncio.to_thread(
                self.validate,
                request,
                output,
                files,
                artifact_root=artifact_root,
                authoritative_artifact_root=authoritative_artifact_root,
                validated_entries=validated_entries,
                deadline_monotonic=deadline_monotonic,
                cancel_event=combined_cancel,
            )
        )
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            local_cancel.set()
            cleaned = await _await_owned_thread_cleanup(work)
            if not cleaned:
                self.process_executor.record_cleanup_failure()
            raise

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
