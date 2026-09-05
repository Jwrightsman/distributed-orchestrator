"""A starved parse runner must never be readable as a defect in the code.

The parse precheck runs a parser in a bounded subprocess. Before this module's
subject existed, `check_code_files_isolated` returned `["validator_timeout"]`
and `["main.py is not valid Python (line 3)"]` through the same `list[str]`,
so nothing downstream could tell "the validator never ran" from "the worker's
code is broken". These tests hold the two channels apart *at the call sites*,
because that is where the confusion had consequences: a wasted repair, an
unearned revision, a demoted candidate, a defect published against a run.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import ensemble
import orchestrator
from execution.validator_process import (
    ValidatorProcessExecutor,
    ValidatorProcessOutcome,
    ValidatorProcessSettings,
)
from execution.validators import (
    ParsePrecheckResult,
    check_code_files_isolated,
    check_code_files_isolated_async,
)

BROKEN_PY = 'def handler(items):\n    return {"out": [\n        {"id": i},\n        for i in items\n    ]}\n'
GOOD_PY = 'def handler(items):\n    return {"out": [{"id": i} for i in items]}\n'

BROKEN_MD = f"Here is the code.\n\n```python\n{BROKEN_PY}```\n"
FIXED_MD = f"Here is the corrected code.\n\n```python\n{GOOD_PY}```\n"

# A runner that reads its request and then outlives any budget a test gives it.
_SLEEP_RUNNER = """
import sys
import time

sys.stdin.buffer.read()
time.sleep(60)
"""

RUNNER_FAILURE_REASONS = (
    "validator_timeout",
    "validator_execution_error",
    "validator_malformed_response",
    "validator_input_rejected",
)


def _timing_out_executor(tmp_path: Path) -> ValidatorProcessExecutor:
    """A real executor whose child never answers inside its budget.

    One second is this executor's own budget, not a production value: the
    default in `config.DEFAULTS` is untouched and nothing here reads it.
    """

    script = tmp_path / "sleep_runner.py"
    script.write_text(_SLEEP_RUNNER.strip() + "\n", encoding="utf-8")
    return ValidatorProcessExecutor(
        ValidatorProcessSettings(execution_mode="auto", timeout_seconds=1),
        command_factory=lambda work_directory: (sys.executable, "-I", str(script)),
        popen_factory=subprocess.Popen,
    )


def _real_executor() -> ValidatorProcessExecutor:
    """The production runner on a budget no test machine can miss."""

    return ValidatorProcessExecutor(
        ValidatorProcessSettings(execution_mode="auto", timeout_seconds=120)
    )


class _StarvesAfter:
    """An executor that answers normally, then stops reaching a verdict.

    Starves one specific parse in a sequence while the others run for real,
    which is how the repair path's two parses are told apart.
    """

    def __init__(self, inner: ValidatorProcessExecutor, *, healthy_calls: int):
        self._inner = inner
        self._healthy_calls = healthy_calls
        self.calls = 0

    @property
    def settings(self):
        return self._inner.settings

    def execute(self, **kwargs):
        self.calls += 1
        if self.calls <= self._healthy_calls:
            return self._inner.execute(**kwargs)
        return ValidatorProcessOutcome(
            completed=False,
            ok=False,
            score=None,
            detail={},
            failure_reason="validator_timeout",
            containment_level="windows_process_tree_best_effort",
            termination_reason="timeout",
        )


def _write(tmp_path: Path, name: str, source: str) -> str:
    path = tmp_path / "code" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return str(path)


# ── The channel itself ──────────────────────────────────────────────────────


def test_a_timeout_is_a_runner_failure_and_carries_no_code_problem(tmp_path):
    """The exact conflation this module exists to prevent."""

    precheck = check_code_files_isolated(
        [_write(tmp_path, "main.py", GOOD_PY)],
        artifact_root=tmp_path / "code",
        process_executor=_timing_out_executor(tmp_path),
    )

    assert precheck.runner_failure == "validator_timeout"
    assert precheck.problems == ()
    assert not precheck.reached_a_verdict


def test_a_real_defect_is_a_code_problem_and_not_a_runner_failure(tmp_path):
    """The control: the fix did not simply stop reporting defects."""

    precheck = check_code_files_isolated(
        [_write(tmp_path, "main.py", BROKEN_PY)],
        artifact_root=tmp_path / "code",
        process_executor=_real_executor(),
    )

    assert precheck.reached_a_verdict
    assert precheck.runner_failure is None
    assert precheck.problems
    assert any("main.py" in problem for problem in precheck.problems)


def test_the_two_channels_are_not_interchangeable_for_a_caller(tmp_path):
    """A caller cannot fall back into the old shape by accident.

    The old return was a `list[str]`, so every list operation a caller might
    still reach for has to fail loudly rather than silently yield strings that
    describe the runner and read as defects.
    """

    precheck = check_code_files_isolated(
        [_write(tmp_path, "main.py", GOOD_PY)],
        artifact_root=tmp_path / "code",
        process_executor=_timing_out_executor(tmp_path),
    )

    with pytest.raises(TypeError):
        len(precheck)
    with pytest.raises(TypeError):
        precheck[0]
    with pytest.raises(TypeError):
        list(precheck)
    # And the truthiness that drove the repair decision is no longer a
    # statement about defects at all.
    assert precheck.problems == ()


def test_a_result_without_a_verdict_cannot_also_report_problems():
    """The invariant that stops the two being merged back together later."""

    with pytest.raises(ValueError):
        ParsePrecheckResult(
            problems=("main.py is not valid Python",),
            runner_failure="validator_timeout",
        )


@pytest.mark.asyncio
async def test_the_async_precheck_uses_the_same_channel(tmp_path):
    precheck = await check_code_files_isolated_async(
        [_write(tmp_path, "main.py", GOOD_PY)],
        artifact_root=tmp_path / "code",
        process_executor=_timing_out_executor(tmp_path),
    )

    assert precheck.runner_failure == "validator_timeout"
    assert precheck.problems == ()


# ── The orchestrator call site ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_starved_runner_does_not_spend_a_repair(tmp_path, monkeypatch):
    """The consequence that reached the model.

    A timeout used to be truthy in `code_problems`, so the stage quoted
    "Fix exactly these defects: - validator_timeout" to the reviser and spent
    a revision on output nothing had found fault with.
    """

    revisions = []

    async def record_revision(task, issues, current):
        revisions.append(issues)
        return FIXED_MD

    monkeypatch.setattr(orchestrator, "revise", record_revision)

    final, _files, problems, precheck_error = await orchestrator.extract_and_repair(
        "t",
        BROKEN_MD,
        BROKEN_MD,
        tmp_path,
        validator_process_executor=_timing_out_executor(tmp_path),
    )

    assert precheck_error == "validator_timeout"
    assert problems == []
    assert revisions == [], "a revision was spent on a defect nobody observed"
    assert final == BROKEN_MD


@pytest.mark.asyncio
async def test_a_starved_repair_check_does_not_accept_the_revision(
    tmp_path, monkeypatch
):
    """The second consequence: an unearned improvement.

    The first parse finds a real defect; the parse that judges the repair never
    reaches a verdict. Counting its empty problem list as "zero defects" would
    score any revision at all as an improvement and adopt it.
    """

    async def unhelpful_revise(task, issues, current):
        return BROKEN_MD.replace('{"id": i},', '{"id": i},,')  # still broken

    monkeypatch.setattr(orchestrator, "revise", unhelpful_revise)
    executor = _StarvesAfter(_real_executor(), healthy_calls=1)

    final, _files, problems, precheck_error = await orchestrator.extract_and_repair(
        "t",
        BROKEN_MD,
        BROKEN_MD,
        tmp_path,
        validator_process_executor=executor,
    )

    assert executor.calls >= 2, "the repair-judging parse never ran"
    assert final == BROKEN_MD, "a revision was adopted on an unreached verdict"
    assert problems, "the real defect the first parse found is still reported"
    assert precheck_error is None
    assert "validator_timeout" not in problems


@pytest.mark.asyncio
async def test_the_stage_never_returns_a_runner_reason_as_a_code_problem(
    tmp_path, monkeypatch
):
    """Whatever the runner fails with, it does not land in `problems`."""

    async def should_not_run(*a, **kw):
        raise AssertionError("a revision was spent on an unreached verdict")

    monkeypatch.setattr(orchestrator, "revise", should_not_run)

    for reason in RUNNER_FAILURE_REASONS:

        async def starved(*args, _reason=reason, **kwargs):
            return ParsePrecheckResult(runner_failure=_reason)

        monkeypatch.setattr(
            orchestrator, "check_code_files_isolated_async", starved
        )
        project_dir = tmp_path / reason
        project_dir.mkdir(parents=True, exist_ok=True)
        _, _, problems, precheck_error = await orchestrator.extract_and_repair(
            "t", BROKEN_MD, BROKEN_MD, project_dir
        )
        assert problems == []
        assert precheck_error == reason


def test_the_run_reports_an_unreached_verdict_separately_from_defects(
    tmp_path, monkeypatch
):
    """The durable record says "not checked", never "checked and clean"."""

    plan_json = '[{"id": 1, "title": "First", "prompt": "do it", "depends_on": []}]'
    built = f"```python\n{GOOD_PY}```"
    # `final_output` is taken from the reviewer's assembled section and the
    # extractor reads that, so a reviewer stub carrying no code yields no
    # files and the precheck is never reached at all.
    reviewed = (
        "## Quality Rating\nPASS\n\n## Issues Found\nNone\n\n"
        f"## Final Assembled Output\n{built}"
    )

    async def fake_generate(prompt, system="", model=None, role=None, format=None):
        if system == orchestrator.PLANNER_SYSTEM:
            return plan_json
        if system == orchestrator.REVIEWER_SYSTEM:
            return reviewed
        return built

    async def fake_stream(*a, **k):
        yield built

    async def starved(*args, **kwargs):
        return ParsePrecheckResult(runner_failure="validator_timeout")

    monkeypatch.setattr(orchestrator, "generate", fake_generate)
    monkeypatch.setattr(orchestrator, "generate_stream", fake_stream)
    monkeypatch.setattr(orchestrator, "check_code_files_isolated_async", starved)
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")

    result = asyncio.run(orchestrator.run_pipeline("build a thing"))

    assert result["code_files"], (
        "no files were extracted, so the precheck never ran and this test "
        "would pass without exercising anything"
    )
    assert result["code_problems"] == []
    assert result["code_precheck_error"] == "validator_timeout"

    log = json.loads(
        (Path(result["project_dir"]) / "full_log.json").read_text(encoding="utf-8")
    )
    assert log["code_problems"] == []
    assert log["code_precheck_error"] == "validator_timeout"


# ── The ensemble call site ──────────────────────────────────────────────────


def test_a_starved_runner_does_not_demote_a_candidate(tmp_path, monkeypatch):
    """Ranking must not let the coordinator's load pick the winner.

    `problems` feeds `parses`, which is the second term of the ranking key. A
    timeout landing there ranked a candidate below one that merely happened to
    get a working validator.
    """

    monkeypatch.setattr(
        ensemble,
        "check_code_files_isolated",
        lambda *a, **kw: ParsePrecheckResult(runner_failure="validator_timeout"),
    )

    materialised = ensemble.materialise(
        ensemble.CandidateResult(index=1, raw_output=f"```python\n{GOOD_PY}```\n"),
        tmp_path,
    )

    assert materialised.problems == []
    assert materialised.precheck_error == "validator_timeout"
    assert materialised.parses, "a starved runner demoted the candidate"


def test_a_real_defect_still_demotes_a_candidate(tmp_path, monkeypatch):
    """The control for the test above."""

    monkeypatch.setattr(
        ensemble,
        "check_code_files_isolated",
        lambda *a, **kw: ParsePrecheckResult(
            problems=("main.py is not valid Python (line 3)",)
        ),
    )

    materialised = ensemble.materialise(
        ensemble.CandidateResult(index=1, raw_output=f"```python\n{BROKEN_PY}```\n"),
        tmp_path,
    )

    assert materialised.problems == ["main.py is not valid Python (line 3)"]
    assert materialised.precheck_error is None
    assert not materialised.parses


def test_an_unchecked_candidate_does_not_rank_below_a_broken_one():
    parsed = ensemble.CandidateResult(index=1, raw_output="x", files=["a.py"])
    unchecked = ensemble.CandidateResult(
        index=2, raw_output="x", files=["b.py"], precheck_error="validator_timeout"
    )
    defective = ensemble.CandidateResult(
        index=3, raw_output="x", files=["c.py"], problems=["not valid Python"]
    )

    order = [item.index for item in ensemble.rank([defective, unchecked, parsed])]

    assert order.index(2) < order.index(3), (
        "the candidate whose validator was starved ranked below a genuinely "
        "broken one"
    )


# ── Attribution ─────────────────────────────────────────────────────────────


def test_the_precheck_cannot_reach_shape_agreement_evidence():
    """Theme 3B-1's boundary, asserted rather than assumed.

    Sampled-agreement evidence carries `subject_node_id` and
    `fault_attribution="subject_output"`, so whatever decides its verdict is
    attributed to a node. That verdict is `compare_outputs`, a pure function of
    two output strings: it never consults the parse precheck, so a starved
    coordinator cannot be recorded as a worker's fault.
    """

    import verification

    source = Path(verification.__file__).read_text(encoding="utf-8")
    assert "check_code_files" not in source
    assert "execution.validators" not in source

    agreed, reason = verification.compare_outputs(GOOD_PY, GOOD_PY)
    assert agreed and reason


def test_no_runner_failure_is_a_worker_attributable_terminal_cause():
    """Capability evidence only attributes causes on its closed worker list."""

    import capability_evidence

    for reason in RUNNER_FAILURE_REASONS:
        assert reason not in capability_evidence._WORKER_TERMINAL_CAUSES
