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

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import orchestrator
from extract import check_code_files, extract_code_files
from ollama_client import generate

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


async def generate_candidate(pitch: str, index: int, model: str | None = None) -> CandidateResult:
    """One node, one complete attempt at the whole artifact."""
    import time

    start = time.time()
    try:
        out = await generate(pitch, system=_system_prompt(), model=model)
    except Exception as e:  # a dead runner must not kill the whole ensemble
        return CandidateResult(index=index, raw_output="", error=f"{type(e).__name__}: {e}",
                               elapsed_seconds=time.time() - start)
    return CandidateResult(index=index, raw_output=out, elapsed_seconds=time.time() - start)


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
    ones that at least produced a file, then the rest. Within a tier, the larger
    artifact wins — for a single-file deliverable, truncation is the failure that
    shows up as "shorter", and every tie observed so far has been a truncated
    candidate against a complete one.
    """
    browser_ok = browser_ok or {}

    def key(r: CandidateResult):
        return (
            0 if browser_ok.get(r.index) else 1,
            0 if r.parses else 1,
            0 if r.extracted else 1,
            -sum(Path(f).stat().st_size for f in r.files if Path(f).exists()),
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
    """Produce n complete candidates for one pitch.

    concurrent=False by default and that is not a placeholder: this project's
    reference machine is 8 GB with no GPU, where two simultaneous model calls
    do not run twice as fast, they thrash — a dress rehearsal measured roughly
    3x wall clock on both of two concurrent pipelines. Across *separate nodes*
    the calls are genuinely parallel and concurrent=True is the right setting;
    on one box, sequential is both faster and honest.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    if concurrent:
        raw = await asyncio.gather(*(generate_candidate(pitch, i, model) for i in range(n)))
        done = [materialise(r, output_dir) for r in raw]
        for r in done:
            if on_candidate:
                on_candidate(r)
        return done

    done = []
    for i in range(n):
        r = materialise(await generate_candidate(pitch, i, model), output_dir)
        done.append(r)
        if on_candidate:
            on_candidate(r)
    return done
