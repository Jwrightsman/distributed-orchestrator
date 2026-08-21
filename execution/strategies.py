"""Production DAG and ensemble strategy adapters."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import ensemble
import orchestrator
from execution.contracts import DagOptionsV1, EnsembleOptionsV1
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
    callbacks: dict[str, Any] = field(default_factory=dict)
    dag_runner: Callable[..., Any] = orchestrator.run_pipeline
    dispatch_results: list[DispatchResult] = field(default_factory=list)


def _unit_summary(result: DispatchResult) -> dict[str, Any]:
    return {
        "unit_id": result.unit.unit_id,
        "kind": result.unit.kind,
        "title": result.unit.title,
        "depends_on": list(result.unit.depends_on),
        "status": result.status,
        "placement": result.placement,
        "node_id": result.node_id,
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
                "placement": result.placement,
                "fallback_reason": result.fallback_reason,
            }

        result = await context.dag_runner(
            request.task,
            on_plan=context.callbacks.get("on_plan"),
            on_build=context.callbacks.get("on_build"),
            on_review_start=context.callbacks.get("on_review_start"),
            project_id=request.project_id,
            build_fn=dispatch_build,
            build_metadata_fn=build_metadata,
            maximum_subtasks=options.maximum_subtasks,
            review_enabled=options.review_enabled,
            revision_enabled=options.revision_enabled,
            execution_mode=context.placement.selected,
        )

        files = [str(path) for path in result.get("code_files", [])]
        validation = context.validators.validate(
            request,
            result.get("final_output") or result.get("review", ""),
            files,
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
        if accepted:
            status = "completed"
        elif request.verification.allow_unverified_fallback and result.get("final_output"):
            status = "unverified"
        else:
            status = "failed"

        log: dict[str, Any] = {}
        project_dir = Path(result.get("project_dir", ""))
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
        semaphore = asyncio.Semaphore(options.concurrency)

        async def run_candidate(index: int) -> tuple[DispatchResult, list[str], list[Any]]:
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

            async def local_executor():
                return await generate(prompt, system=unit.system)

            async with semaphore:
                dispatched = await context.dispatcher.execute(
                    unit,
                    request,
                    context.execution_id,
                    self.identifier,
                    context.placement,
                    local_executor,
                )
            context.dispatch_results.append(dispatched)
            files: list[str] = []
            evidence = []
            if dispatched.output:
                candidate = ensemble.CandidateResult(index=index, raw_output=dispatched.output)
                candidate.elapsed_seconds = dispatched.duration_ms / 1000
                candidate_dir = root / f"candidate_{index}"
                candidate_dir.mkdir(parents=True, exist_ok=True)
                (candidate_dir / "candidate.md").write_text(dispatched.output, encoding="utf-8")
                candidate = ensemble.materialise(candidate, root)
                files = list(candidate.files)
                evidence = context.validators.validate(request, dispatched.output, files)
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
            return dispatched, files, evidence

        generated = await asyncio.gather(*(run_candidate(index) for index in range(1, options.candidates + 1)))
        accepted = [item for item in generated if item[0].status == "completed" and context.validators.accepted(item[2])]
        completed = [item for item in generated if item[0].status == "completed" and item[0].output]

        def validation_score(item) -> float:
            evidence = item[2]
            return sum(ev.score if ev.score is not None else (1.0 if ev.status == "passed" else 0.0) for ev in evidence)

        winner = None
        verified = False
        if accepted:
            winner = accepted[0] if options.selection_policy == "first_valid" else max(
                accepted,
                key=lambda item: (validation_score(item), len(item[0].output), -int(item[0].unit.unit_id.split("-")[-1])),
            )
            verified = True
            explanation = (
                f"Selected {winner[0].unit.unit_id} because it passed all required validators; "
                f"selection policy was {options.selection_policy}."
            )
            status = "completed"
        elif completed and request.verification.allow_unverified_fallback:
            winner = max(completed, key=lambda item: (validation_score(item), len(item[0].output)))
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
        all_evidence = []
        for dispatched, files, evidence in generated:
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
                    "generation_duration_ms": dispatched.duration_ms,
                    "validation_duration_ms": sum(item.duration_ms for item in evidence),
                    "validation": [item.model_dump(mode="json") for item in evidence],
                }
            )
            all_evidence.extend(item.model_dump(mode="json") for item in evidence)

        if winner:
            context.emit("winner_selected", {"candidate_id": winner_id, "verified": verified, "reason": explanation})
            winner_output = winner[0].output
            winner_files = winner[1]
            output_reference = str(root / f"candidate_{winner_id.split('-')[-1]}" / "candidate.md")
        else:
            winner_output = ""
            winner_files = []
            output_reference = str(root)

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
            legacy_payload=legacy,
            execution_units=[_unit_summary(item[0]) for item in generated],
            candidates=summaries,
            validation_evidence=all_evidence,
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
                "validation_duration_ms": sum(item.duration_ms for _, _, ev in generated for item in ev),
                "total_output_bytes": sum(len(item[0].output.encode("utf-8")) for item in generated),
            },
        )
