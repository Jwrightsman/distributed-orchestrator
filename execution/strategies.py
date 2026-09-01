"""Production DAG and ensemble strategy adapters."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import ensemble
import orchestrator
from execution.contracts import DagOptionsV1, EnsembleOptionsV1
from execution.artifacts import ArtifactEntryV1, ArtifactError, ArtifactStore
from execution.dispatch import DispatchResult, Dispatcher, ExecutionUnit, PlacementDecision
from execution.registry import ExecutionStrategy, StrategyOutcome
from execution.validators import ValidatorRegistry
from ollama_client import generate


@dataclass
class StrategyContext:
    execution_id: str
    placement: PlacementDecision
    dispatcher: Dispatcher
    validators: ValidatorRegistry
    emit: Callable[[str, dict[str, Any]], None]
    selector_reason: str = ""
    selector_version: str = ""
    callbacks: dict[str, Any] = field(default_factory=dict)
    dag_runner: Callable[..., Any] = orchestrator.run_pipeline
    dispatch_results: list[DispatchResult] = field(default_factory=list)
    deadline_monotonic: float | None = None
    cancel_event: asyncio.Event | None = None
    artifacts: ArtifactStore | None = None
    artifact_root_path: Path | None = None
    artifact_registration_error: str | None = None

    def ensure_active(self) -> None:
        if self.cancel_event and self.cancel_event.is_set():
            raise asyncio.CancelledError
        if self.deadline_monotonic is not None and time.monotonic() >= self.deadline_monotonic:
            raise TimeoutError("execution deadline exceeded")

    async def validate_candidate(
        self,
        request,
        output: str,
        files: list[str],
        *,
        artifact_root: Path,
        authoritative_artifact_root: Path | None = None,
        validated_entries: list[ArtifactEntryV1] | None = None,
    ):
        """Run validators with explicit deadline and cancellation ownership."""
        self.ensure_active()
        return await self.validators.validate_async(
            request,
            output,
            files,
            artifact_root=artifact_root,
            authoritative_artifact_root=authoritative_artifact_root,
            validated_entries=validated_entries,
            deadline_monotonic=self.deadline_monotonic,
            cancel_event=self.cancel_event,
        )

    async def run_blocking(self, function, *args, **kwargs):
        """Run bounded filesystem/validator work under the execution deadline."""
        self.ensure_active()
        remaining = (
            None
            if self.deadline_monotonic is None
            else self.deadline_monotonic - time.monotonic()
        )
        if remaining is not None and remaining <= 0:
            raise TimeoutError("execution deadline exceeded before blocking stage")
        work = asyncio.to_thread(function, *args, **kwargs)
        if remaining is None:
            return await work
        return await asyncio.wait_for(work, timeout=remaining)


def _unit_summary(result: DispatchResult) -> dict[str, Any]:
    return {
        "unit_id": result.unit.unit_id,
        "kind": result.unit.kind,
        "title": result.unit.title,
        "depends_on": list(result.unit.depends_on),
        "status": result.status,
        "placement": result.placement,
        "node_id": result.node_id,
        "enrollment_id": result.enrollment_id,
        "capability_descriptor_version": result.capability_descriptor_version,
        "capability_descriptor_hash": result.capability_descriptor_hash,
        "attempt_id": result.attempt_id,
        "selected_model_provider": result.selected_model_provider,
        "selected_model_name": result.selected_model_name,
        "selected_model_digest": result.selected_model_digest,
        "evidence_role": result.evidence_role,
        "attempt_count": result.attempt_count,
        "duration_ms": result.duration_ms,
        "fallback_reason": result.fallback_reason,
    }


class DagStrategy(ExecutionStrategy):
    identifier = "dag"
    version = "1"

    async def execute(self, request, options, context: StrategyContext) -> StrategyOutcome:
        if not isinstance(options, DagOptionsV1):
            raise TypeError("DAG strategy requires DagOptionsV1")

        by_subtask: dict[int, DispatchResult] = {}

        async def dispatch_build(subtask: dict, dependency_context: str) -> str:
            context.ensure_active()
            unit = ExecutionUnit(
                unit_id=f"dag-{subtask['id']}",
                kind="dag_subtask",
                title=subtask["title"],
                prompt=orchestrator.compose_builder_prompt(subtask, dependency_context, request.task),
                system=orchestrator.BUILDER_SYSTEM,
                depends_on=tuple(f"dag-{item}" for item in subtask.get("depends_on", [])),
                metadata={"subtask_id": subtask["id"]},
            )

            async def local_executor():
                on_token = context.callbacks.get("on_token")
                token_callback = (lambda token: on_token(token, subtask)) if on_token else None
                return await orchestrator.build(
                    subtask,
                    dependency_context,
                    on_token=token_callback,
                    task=request.task,
                )

            dispatched = await context.dispatcher.execute(
                unit,
                request,
                context.execution_id,
                self.identifier,
                context.placement,
                local_executor,
                deadline_monotonic=context.deadline_monotonic,
                cancel_event=context.cancel_event,
            )
            by_subtask[subtask["id"]] = dispatched
            context.dispatch_results.append(dispatched)
            if dispatched.status != "completed":
                raise RuntimeError(dispatched.error or f"execution unit {unit.unit_id} failed")
            return dispatched.output

        def build_metadata(subtask: dict) -> dict[str, Any]:
            result = by_subtask.get(subtask["id"])
            if not result:
                return {}
            return {
                "node_id": result.node_id,
                "enrollment_id": result.enrollment_id,
                "placement": result.placement,
                "fallback_reason": result.fallback_reason,
            }

        def review_started() -> None:
            context.emit("review_started", {"strategy": self.identifier})
            callback = context.callbacks.get("on_review_start")
            if callback:
                callback()

        def artifact_root_ready(path: Path) -> None:
            context.artifact_root_path = Path(path)
            if context.artifacts:
                try:
                    context.artifacts.register_root(
                        context.execution_id,
                        path,
                        strategy=self.identifier,
                        active=True,
                    )
                except ArtifactError as exc:
                    context.artifact_registration_error = str(exc)

        context.ensure_active()
        result = await context.dag_runner(
            request.task,
            on_plan=context.callbacks.get("on_plan"),
            on_build=context.callbacks.get("on_build"),
            on_review_start=review_started,
            project_id=request.project_id,
            build_fn=dispatch_build,
            build_metadata_fn=build_metadata,
            maximum_subtasks=options.maximum_subtasks,
            review_enabled=options.review_enabled,
            revision_enabled=options.revision_enabled,
            execution_mode=context.placement.selected,
            execution_id=context.execution_id,
            strategy_requested=request.strategy,
            strategy_selected=self.identifier,
            strategy_version=self.version,
            selector_reason=context.selector_reason,
            selector_version=context.selector_version,
            placement_fallback=context.placement.fallback_reason,
            on_revision_start=lambda revision_pass: context.emit(
                "revision_started", {"strategy": self.identifier, "revision_pass": revision_pass}
            ),
            on_artifact_root=artifact_root_ready,
            validator_process_executor=context.validators.process_executor,
            validator_deadline_monotonic=context.deadline_monotonic,
            validator_cancel_event=context.cancel_event,
            validator_artifact_store=context.artifacts,
        )

        files = [str(path) for path in result.get("code_files", [])]
        project_dir = Path(result.get("project_dir", ""))
        validation_root = (
            project_dir / "code" if (project_dir / "code").is_dir() else project_dir
        )
        validated_entries: list[ArtifactEntryV1] | None = None
        authoritative_root: Path | None = None
        if context.artifacts and files:
            if context.artifact_registration_error:
                raise ArtifactError(context.artifact_registration_error)
            subtree = validation_root.relative_to(project_dir).as_posix()
            validated_entries = await context.run_blocking(
                context.artifacts.validate_subtree,
                context.execution_id,
                subtree,
            )
            authoritative_root = await context.run_blocking(
                context.artifacts.root_path,
                context.execution_id,
            )
        context.ensure_active()
        validation = await context.validate_candidate(
            request,
            result.get("final_output") or result.get("review", ""),
            files,
            artifact_root=validation_root,
            authoritative_artifact_root=authoritative_root,
            validated_entries=validated_entries,
        )
        for evidence in validation:
            context.emit(
                "candidate_validation_completed",
                {
                    "candidate_id": None,
                    "validator": evidence.validator_name,
                    "status": evidence.status,
                },
            )

        accepted = context.validators.accepted(validation)
        validation_summary = context.validators.summarize(validation)
        if accepted:
            status = "completed"
            lifecycle_status = "completed"
        elif request.verification.allow_unverified_fallback and result.get("final_output"):
            status = "unverified"
            lifecycle_status = "completed"
        else:
            status = "failed"
            lifecycle_status = "failed"

        log: dict[str, Any] = {}
        log_path = project_dir / "full_log.json"
        if log_path.is_file():
            try:
                log = json.loads(log_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log = {}

        plan = result.get("plan", [])
        units = [_unit_summary(item) for item in context.dispatch_results]
        if not units:  # injected test/fake runners may not call the build function
            units = [
                {
                    "unit_id": f"dag-{item.get('id', index)}",
                    "kind": "dag_subtask",
                    "title": item.get("title", f"Subtask {index}"),
                    "depends_on": [f"dag-{dep}" for dep in item.get("depends_on", [])],
                    "status": "completed",
                    "placement": context.placement.selected,
                    "attempt_count": 1,
                    "duration_ms": 0,
                }
                for index, item in enumerate(plan, 1)
            ]

        legacy = dict(result)
        legacy["results"] = {str(key): value for key, value in result.get("results", {}).items()}
        legacy["mode"] = context.placement.selected
        legacy["nodes_used"] = len({r.node_id for r in context.dispatch_results if r.node_id})

        output_path = project_dir / "output.md"
        return StrategyOutcome(
            status=status,
            lifecycle_status=lifecycle_status,
            validation_outcome=validation_summary.outcome,
            assurance_level=validation_summary.assurance_level,
            validation_summary=validation_summary,
            legacy_payload=legacy,
            execution_units=units,
            validation_evidence=[item.model_dump(mode="json") for item in validation],
            produced_files=files,
            output_reference=str(output_path) if output_path.is_file() else str(project_dir),
            output_preview=(result.get("final_output") or result.get("review", ""))[:1000],
            review_metadata={
                "enabled": options.review_enabled,
                "rating": result.get("rating"),
                "duration_ms": int(float(log.get("review_seconds", 0)) * 1000),
            },
            revision_metadata={"enabled": options.revision_enabled, **(log.get("revision") or {})},
            errors=(
                []
                if status != "failed"
                else [{"code": "validation_failed", "message": "DAG output did not satisfy required validators"}]
            ),
            telemetry={
                "candidate_count": 0,
                "attempt_count": sum(item.attempt_count for item in context.dispatch_results),
                "generation_duration_ms": sum(item.duration_ms for item in context.dispatch_results),
                "validation_duration_ms": sum(item.duration_ms for item in validation),
                "total_output_bytes": len((result.get("final_output") or "").encode("utf-8")),
                "credit_records": log.get("credits", []),
            },
        )


class EnsembleStrategy(ExecutionStrategy):
    identifier = "ensemble"
    version = "1"
    artifact_root = Path("execution_artifacts")

    async def execute(self, request, options, context: StrategyContext) -> StrategyOutcome:
        if not isinstance(options, EnsembleOptionsV1):
            raise TypeError("ensemble strategy requires EnsembleOptionsV1")

        root = self.artifact_root / context.execution_id
        root.mkdir(parents=True, exist_ok=True)
        context.artifact_root_path = root
        if context.artifacts:
            try:
                context.artifacts.register_root(
                    context.execution_id,
                    root,
                    strategy=self.identifier,
                    active=True,
                )
            except ArtifactError as exc:
                context.artifact_registration_error = str(exc)
        semaphore = asyncio.Semaphore(options.concurrency)

        async def run_candidate(index: int) -> tuple[DispatchResult, list[str], list[Any], str | None]:
            candidate_id = f"candidate-{index}"
            contract_text = ""
            if request.output_contract:
                contract_text = (
                    "\n\n## Output contract\n"
                    + json.dumps(request.output_contract.model_dump(mode="json"), indent=2)
                )
            prompt = request.task + contract_text
            unit = ExecutionUnit(
                unit_id=candidate_id,
                kind="candidate",
                title=f"Complete candidate {index}",
                prompt=prompt,
                system=ensemble._system_prompt(),
            )

            files: list[str] = []
            evidence = []
            failure_stage: str | None = None
            dispatched: DispatchResult | None = None
            try:
                context.ensure_active()

                async def local_executor():
                    return await generate(prompt, system=unit.system)

                failure_stage = "generation"
                async with semaphore:
                    dispatched = await context.dispatcher.execute(
                        unit,
                        request,
                        context.execution_id,
                        self.identifier,
                        context.placement,
                        local_executor,
                        deadline_monotonic=context.deadline_monotonic,
                        cancel_event=context.cancel_event,
                    )
                context.dispatch_results.append(dispatched)
                if dispatched.status != "completed":
                    failure_stage = "generation"
                elif dispatched.output:
                    candidate = ensemble.CandidateResult(index=index, raw_output=dispatched.output)
                    candidate.elapsed_seconds = dispatched.duration_ms / 1000
                    candidate_dir = root / f"candidate_{index}"
                    failure_stage = "directory_creation"
                    candidate_dir.mkdir(parents=True, exist_ok=True)
                    failure_stage = "materialization"
                    await context.run_blocking(
                        (candidate_dir / "candidate.md").write_text,
                        dispatched.output,
                        encoding="utf-8",
                    )
                    failure_stage = "artifact_extraction"
                    candidate = await context.run_blocking(
                        ensemble.materialise,
                        candidate,
                        root,
                        validate_parsers=False,
                    )
                    files = list(candidate.files)
                    failure_stage = "manifest_creation"
                    validated_entries: list[ArtifactEntryV1] | None = None
                    authoritative_root: Path | None = None
                    validation_root = (
                        candidate_dir / "code"
                        if (candidate_dir / "code").is_dir()
                        else candidate_dir
                    )
                    if context.artifacts:
                        subtree = validation_root.relative_to(root).as_posix()
                        validated_entries = await context.run_blocking(
                            context.artifacts.validate_subtree,
                            context.execution_id,
                            subtree,
                        )
                        authoritative_root = await context.run_blocking(
                            context.artifacts.root_path,
                            context.execution_id,
                        )
                    failure_stage = "validation"
                    evidence = await context.validate_candidate(
                        request,
                        dispatched.output,
                        files,
                        artifact_root=validation_root,
                        authoritative_artifact_root=authoritative_root,
                        validated_entries=validated_entries,
                    )
                    failure_stage = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"{failure_stage or 'candidate'} failed: {type(exc).__name__}: {exc}"[:500]
                if dispatched is None:
                    dispatched = DispatchResult(
                        unit=unit,
                        status="failed",
                        placement=context.placement.selected,
                        error=error,
                        attempt_count=0,
                    )
                    context.dispatch_results.append(dispatched)
                else:
                    dispatched.status = "failed"
                    dispatched.error = error
                evidence = []
            context.emit(
                "candidate_generated",
                {
                    "candidate_id": candidate_id,
                    "status": dispatched.status,
                    "output_bytes": len(dispatched.output.encode("utf-8")),
                },
            )
            for item in evidence:
                context.emit(
                    "candidate_validation_completed",
                    {
                        "candidate_id": candidate_id,
                        "validator": item.validator_name,
                        "status": item.status,
                    },
                )
            return dispatched, files, evidence, failure_stage

        tasks = [asyncio.create_task(run_candidate(index)) for index in range(1, options.candidates + 1)]
        generated_completion_order = []
        try:
            for completed_task in asyncio.as_completed(tasks):
                generated_completion_order.append(await completed_task)
        finally:
            # ``as_completed`` does not own the candidate tasks.  A total
            # deadline or operator cancellation must not leave model calls
            # running after the execution has become terminal.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        generated = sorted(
            generated_completion_order,
            key=lambda item: int(item[0].unit.unit_id.rsplit("-", 1)[-1]),
        )
        accepted = [item for item in generated if item[0].status == "completed" and context.validators.accepted(item[2])]
        completed = [item for item in generated if item[0].status == "completed" and item[0].output]

        def validation_score(item) -> float:
            evidence = item[2]
            return sum(ev.score if ev.score is not None else (1.0 if ev.status == "passed" else 0.0) for ev in evidence)

        def selection_key(item):
            evidence = item[2]
            assurance_order = {"unverified": 0, "structural": 1, "model_judged": 2, "deterministic": 3}
            assurance = context.validators.summarize(evidence).assurance_level
            candidate_number = int(item[0].unit.unit_id.rsplit("-", 1)[-1])
            return (
                assurance_order.get(assurance, 0),
                validation_score(item),
                -item[0].duration_ms,
                -candidate_number,
            )

        winner = None
        verified = False
        if accepted:
            if options.selection_policy == "first_valid":
                winner = next(item for item in generated_completion_order if item in accepted)
                decisive = "first acceptable completion order"
            else:
                winner = max(accepted, key=selection_key)
                other_candidates = [item for item in accepted if item is not winner]
                if not other_candidates:
                    decisive = "only candidate with acceptable required validation"
                else:
                    runner_up = max(other_candidates, key=selection_key)
                    winner_summary = context.validators.summarize(winner[2])
                    runner_summary = context.validators.summarize(runner_up[2])
                    comparisons = (
                        (
                            "assurance strength",
                            winner_summary.assurance_level,
                            runner_summary.assurance_level,
                        ),
                        (
                            "meaningful validator score",
                            f"{validation_score(winner):.3f}",
                            f"{validation_score(runner_up):.3f}",
                        ),
                        (
                            "lower generation latency",
                            str(winner[0].duration_ms),
                            str(runner_up[0].duration_ms),
                        ),
                        (
                            "stable candidate identifier",
                            winner[0].unit.unit_id,
                            runner_up[0].unit.unit_id,
                        ),
                    )
                    criterion, winner_value, runner_value = next(
                        comparison
                        for comparison in comparisons
                        if comparison[1] != comparison[2]
                    )
                    decisive = (
                        f"{criterion} ({winner_value} versus {runner_value} for "
                        f"{runner_up[0].unit.unit_id})"
                    )
            verified = True
            explanation = (
                f"Selected {winner[0].unit.unit_id}: required validation passed; assurance="
                f"{context.validators.summarize(winner[2]).assurance_level}; score={validation_score(winner):.3f}; "
                f"generation_ms={winner[0].duration_ms}; policy={options.selection_policy}; "
                f"decisive criterion={decisive}. "
                "Stable candidate id is the final tie-break. These checks establish contract assurance, "
                "not general behavioral correctness."
            )
            status = "completed"
        elif completed and request.verification.allow_unverified_fallback:
            winner = max(completed, key=selection_key)
            explanation = (
                f"Selected {winner[0].unit.unit_id} as an unverified fallback because no candidate passed all "
                "required validators. This is not a deterministic correctness claim."
            )
            status = "unverified"
        else:
            explanation = "No candidate completed with acceptable validation evidence."
            status = "failed"

        winner_id = winner[0].unit.unit_id if winner else None
        summaries = []
        aggregate_evidence = []
        for dispatched, files, evidence, failure_stage in generated:
            is_winner = dispatched.unit.unit_id == winner_id
            passed = context.validators.accepted(evidence) if evidence else False
            if is_winner:
                candidate_status = "selected" if verified else "unverified"
            elif dispatched.status != "completed":
                candidate_status = "failed"
            elif not passed:
                candidate_status = "rejected"
                context.emit(
                    "candidate_rejected",
                    {"candidate_id": dispatched.unit.unit_id, "reason": "required validation did not pass"},
                )
            else:
                candidate_status = "completed"
            candidate_validation = context.validators.summarize(evidence)
            summaries.append(
                {
                    "candidate_id": dispatched.unit.unit_id,
                    "status": candidate_status,
                    "output_bytes": len(dispatched.output.encode("utf-8")),
                    "output_preview": dispatched.output[:500],
                    "produced_files": files,
                    "error": dispatched.error,
                    "placement": dispatched.placement,
                    "node_id": dispatched.node_id,
                    "enrollment_id": dispatched.enrollment_id,
                    "capability_descriptor_version": (
                        dispatched.capability_descriptor_version
                    ),
                    "capability_descriptor_hash": (
                        dispatched.capability_descriptor_hash
                    ),
                    "attempt_id": dispatched.attempt_id,
                    "selected_model_provider": dispatched.selected_model_provider,
                    "selected_model_name": dispatched.selected_model_name,
                    "selected_model_digest": dispatched.selected_model_digest,
                    "evidence_role": dispatched.evidence_role,
                    "generation_duration_ms": dispatched.duration_ms,
                    "validation_duration_ms": sum(item.duration_ms for item in evidence),
                    "validation": [item.model_dump(mode="json") for item in evidence],
                    "validation_outcome": candidate_validation.outcome,
                    "assurance_level": candidate_validation.assurance_level,
                    "validation_summary": candidate_validation.model_dump(mode="json"),
                    "failure_stage": failure_stage,
                }
            )
            aggregate_evidence.extend(evidence)

        if winner:
            context.emit("winner_selected", {"candidate_id": winner_id, "verified": verified, "reason": explanation})
            winner_output = winner[0].output
            winner_files = winner[1]
            output_reference = str(root / f"candidate_{winner_id.split('-')[-1]}" / "candidate.md")
        else:
            winner_output = ""
            winner_files = []
            output_reference = str(root)

        selected_evidence = winner[2] if winner else []
        result_validation = context.validators.summarize(
            selected_evidence if winner else aggregate_evidence
        )
        if not winner and aggregate_evidence:
            result_validation = result_validation.model_copy(
                update={
                    "explanation": (
                        "Candidate validation ran, but no candidate satisfied the required "
                        f"policy and no final result was selected. {result_validation.explanation}"
                    )[:1000]
                }
            )

        legacy = {
            "project_dir": str(root),
            "plan": [
                {"id": index, "title": f"Complete candidate {index}", "prompt": request.task, "depends_on": []}
                for index in range(1, options.candidates + 1)
            ],
            "results": {item[0].unit.unit_id: item[0].output for item in generated},
            "review": explanation,
            "final_output": winner_output,
            "rating": "PASS" if status == "completed" else "UNVERIFIED" if status == "unverified" else "FAIL",
            "code_files": winner_files,
            "code_problems": [
                evidence.failure_reason
                for evidence in (winner[2] if winner else [])
                if evidence.status != "passed" and evidence.failure_reason
            ],
            "project_id": request.project_id or "",
            "mode": context.placement.selected,
            "nodes_used": len({item[0].node_id for item in generated if item[0].node_id}),
        }
        return StrategyOutcome(
            status=status,
            lifecycle_status="failed" if status == "failed" else "completed",
            validation_outcome=result_validation.outcome,
            assurance_level=result_validation.assurance_level,
            validation_summary=result_validation,
            legacy_payload=legacy,
            execution_units=[_unit_summary(item[0]) for item in generated],
            candidates=summaries,
            # Top-level evidence describes the selected final only. Complete
            # per-candidate evidence remains in each candidate summary. With no
            # selected final, the aggregate summary above still records what ran.
            validation_evidence=[
                item.model_dump(mode="json") for item in selected_evidence
            ],
            winning_candidate=winner_id,
            winner_selection_explanation=explanation,
            produced_files=winner_files,
            output_reference=output_reference,
            output_preview=winner_output[:1000],
            errors=(
                []
                if status != "failed"
                else [{"code": "all_candidates_failed", "message": explanation, "retryable": True}]
            ),
            telemetry={
                "candidate_count": options.candidates,
                "candidate_outcomes": {item[0].unit.unit_id: item[0].status for item in generated},
                "attempt_count": sum(item[0].attempt_count for item in generated),
                "generation_duration_ms": sum(item[0].duration_ms for item in generated),
                "validation_duration_ms": sum(item.duration_ms for _, _, ev, _ in generated for item in ev),
                "total_output_bytes": sum(len(item[0].output.encode("utf-8")) for item in generated),
            },
        )
