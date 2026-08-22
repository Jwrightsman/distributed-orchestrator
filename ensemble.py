"""Ensemble execution — N complete candidates, pick the one that works.

The default pipeline *decomposes*: a planner splits a task into subtasks and
separate builder agents, which cannot see each other, each write a piece. That
works well when the pieces are independent and each is cheaply checkable. It
works badly when the artifact is tightly coupled, because blind agents have to
agree on shared interfaces by luck — our own numbers say so, a labelled bar
chart at 10/10 against a playable Snake game at 2/10 on the same model, same
prompts, same harness.

Ensemble is the other shape. Every node gets the *whole* pitch and produces a
*complete* deliverable on its own. Nothing has to be agreed across agents,
because nothing is shared. Then the coordinator runs mechanical checks over the
candidates and keeps the one that passes.

The trade is explicit: ensemble spends N times the inference on one artifact and
buys N independent attempts at it, while decomposition spends its inference on
different pieces and needs every one of them to fit. Which is better is an
empirical question per workload, not a matter of taste — `scripts/ensemble_experiment.py`
is how it gets answered, and the answer is written down whichever way it comes out.

**On the selector.** Selection here uses only checks a coordinator can run
without knowing the right answer: did a file come out, does it parse, does it
load, does it draw, does it move, does it respond to input, is it free of
console errors. That is what makes this deployable rather than a thought
experiment. It also means the selector cannot rescue a batch where every
candidate is broken — ensemble multiplies the chance of *getting* a good
candidate, it does not create one.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import orchestrator
from extract import check_code_files, extract_code_files

# One model call per candidate. The system prompt is deliberately the *same*
# builder prompt decomposition uses, so a comparison between the two isolates
# the architecture rather than measuring a new prompt.
ENSEMBLE_SYSTEM_SUFFIX = """

YOU ARE PRODUCING THE ENTIRE DELIVERABLE ALONE.

There are no other agents and no later passes. Nothing you leave unfinished will
be filled in by anyone else. Every function you call must be one you wrote in
this file, and the result must be complete and runnable exactly as emitted."""


@dataclass
class CandidateResult:
    """One node's complete attempt, plus what the mechanical checks made of it."""

    index: int
    raw_output: str
    files: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    error: str | None = None

    @property
    def extracted(self) -> bool:
        return bool(self.files)

    @property
    def parses(self) -> bool:
        return self.extracted and not self.problems


def _system_prompt() -> str:
    # orchestrator.BUILDER_SYSTEM tracks the active prompt set, including a
    # --prompt-set override, so ensemble and decomposition always run the same
    # builder prompt and a comparison isolates the architecture.
    return orchestrator.BUILDER_SYSTEM + ENSEMBLE_SYSTEM_SUFFIX


def materialise(result: CandidateResult, output_dir: Path) -> CandidateResult:
    """Write a candidate's files to disk and run the cheap structural checks.

    Cheap means: no browser. Extraction and parse are the first filter, and they
    are free — a candidate that produced no file, or a file that does not parse,
    is out before anything expensive runs on it.
    """
    cand_dir = output_dir / f"candidate_{result.index}"
    cand_dir.mkdir(parents=True, exist_ok=True)
    result.files = extract_code_files(result.raw_output, cand_dir)
    result.problems = check_code_files(result.files) if result.files else ["nothing extracted"]
    return result


def rank(results: list[CandidateResult], browser_ok: dict[int, bool] | None = None) -> list[CandidateResult]:
    """Best first, using only what a coordinator can know without the answer.

    Order: candidates that pass the browser check, then ones that parse, then
    ones that at least produced a file, then the rest. Within a tier, lower
    observed generation latency wins and the stable candidate identifier is the
    final tie-break. Output size is not treated as a quality signal.
    """
    browser_ok = browser_ok or {}

    def key(r: CandidateResult):
        return (
            0 if browser_ok.get(r.index) else 1,
            0 if r.parses else 1,
            0 if r.extracted else 1,
            r.elapsed_seconds,
            r.index,
        )

    return sorted(results, key=key)


async def run_ensemble(
    pitch: str,
    n: int,
    output_dir: Path,
    model: str | None = None,
    concurrent: bool = False,
    on_candidate=None,
) -> list[CandidateResult]:
    """Compatibility wrapper over the production ensemble strategy.

    concurrent=False by default and that is not a placeholder: this project's
    reference machine is 8 GB with no GPU, where two simultaneous model calls
    do not run twice as fast, they thrash — a dress rehearsal measured roughly
    3x wall clock on both of two concurrent pipelines. Across *separate nodes*
    the calls are genuinely parallel and concurrent=True is the right setting;
    on one box, sequential is both faster and honest.

    Protocol v1 bounds one execution to five candidates. Larger experiment
    trial counts are therefore split into bounded production executions while
    retaining the historical ``candidate_N`` artifact layout.
    """
    from execution.contracts import ExecutionRequestV1
    from execution.service import get_execution_service

    if n < 1:
        raise ValueError("candidate count must be at least 1")
    if model is not None:
        raise ValueError("per-run model overrides are not part of execution protocol v1")

    output_dir.mkdir(parents=True, exist_ok=True)
    done: list[CandidateResult] = []
    offset = 0
    while offset < n:
        count = min(5, n - offset)
        request = ExecutionRequestV1.model_validate({
            "task": pitch,
            "strategy": "direct" if count == 1 else "ensemble",
            "strategy_options": {
                "candidates": count,
                "concurrency": count if concurrent else 1,
            },
            "placement": "local",
            "output_contract": {
                "kind": "single_artifact",
                "validators": [
                    {"name": "artifact_extraction", "required": True},
                    {"name": "code_parse", "required": True},
                ],
            },
        })
        execution = await get_execution_service().execute(request)
        source_root = Path(execution.legacy_payload.get("project_dir", ""))
        summaries = execution.result.candidates
        for batch_index, summary in enumerate(summaries, 1):
            index = offset + batch_index
            source = source_root / f"candidate_{batch_index}"
            destination = output_dir / f"candidate_{index}"
            if source.is_dir() and source.resolve() != destination.resolve():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                destination.mkdir(parents=True, exist_ok=True)
            raw_file = destination / "candidate.md"
            raw_output = raw_file.read_text(encoding="utf-8") if raw_file.is_file() else summary.output_preview
            files = [str(path) for path in sorted((destination / "code").glob("*")) if path.is_file()]
            problems = [
                item.failure_reason or f"{item.validator_name}: {item.status}"
                for item in summary.validation
                if item.status != "passed"
            ]
            candidate = CandidateResult(
                index=index,
                raw_output=raw_output,
                files=files,
                problems=problems,
                elapsed_seconds=summary.generation_duration_ms / 1000,
                error=summary.error,
            )
            done.append(candidate)
            if on_candidate:
                on_candidate(candidate)
        offset += count
    return done
