"""Prompt sets must be frozen, selectable, and honest about which one ran.

The point of versioning the prompts is that a recorded score can be traced to
the exact wording that produced it. Two things break that: editing v1, and a
run that silently uses a different set than the one asked for.
"""

import asyncio
import os

import pytest

import orchestrator
import prompts
import routes_pitch


@pytest.fixture(autouse=True)
def restore_prompt_set():
    """Every test leaves the process on whatever set it started with."""
    original = orchestrator.active_prompt_set().name
    original_env = os.environ.get("PROMPT_SET")
    yield
    orchestrator.apply_prompt_set(original)
    if original_env is None:
        os.environ.pop("PROMPT_SET", None)
    else:
        os.environ["PROMPT_SET"] = original_env


def test_default_is_the_promoted_set():
    """The default must be a set that beat the previous one on a measured run.

    v1 was the default until Aug 8, when v3 scored 17/28 (61%) against v1's
    10/28 (36%) on the same 28 prompts and the same model
    (evals/results/20260808_050610 vs 20260806_195850). Promotion requires a
    recorded run, not a preference — if this assertion is changed, the commit
    doing it must cite the run that justifies it.
    """
    assert prompts.DEFAULT_SET == "v3"
    assert prompts.DEFAULT_SET in {ps.name for ps in prompts.list_prompt_sets()}


def test_v1_remains_available_as_the_baseline():
    """Old scores refer to v1; it must stay loadable and untouched forever."""
    v1 = prompts.get_prompt_set("v1")
    assert v1.name == "v1"
    assert "You are a task planner" in v1.planner


def test_every_set_defines_all_four_prompts():
    for ps in prompts.list_prompt_sets():
        for field in ("planner", "builder", "reviewer", "reviser"):
            text = getattr(ps, field)
            assert isinstance(text, str) and len(text) > 100, f"{ps.name}.{field} looks empty"


def test_prompt_sets_are_frozen():
    """A set must not be mutable in place — that is how baselines get lost."""
    ps = prompts.get_prompt_set("v1")
    with pytest.raises(Exception):
        ps.planner = "something else"


def test_unknown_set_fails_loudly_and_lists_options():
    with pytest.raises(KeyError) as exc:
        prompts.get_prompt_set("v99")
    assert "v1" in str(exc.value)


def test_applying_a_set_switches_the_live_prompts():
    orchestrator.apply_prompt_set("v2")

    assert orchestrator.active_prompt_set().name == "v2"
    assert orchestrator.PLANNER_SYSTEM == prompts.get_prompt_set("v2").planner
    assert orchestrator.BUILDER_SYSTEM == prompts.get_prompt_set("v2").builder
    assert orchestrator.REVIEWER_SYSTEM == prompts.get_prompt_set("v2").reviewer
    assert orchestrator.REVISER_SYSTEM == prompts.get_prompt_set("v2").reviser


def test_applying_a_set_exports_it_to_the_environment():
    """Subprocesses (a worker node, a server) must inherit the same set."""
    orchestrator.apply_prompt_set("v2")
    assert os.environ["PROMPT_SET"] == "v2"


def test_distributed_path_sees_the_switch():
    """routes_pitch used to bind BUILDER_SYSTEM by value at import time.

    That made a prompt switch apply to local builds but not to work sent to
    worker nodes — the two halves of a comparison would silently disagree.
    """
    orchestrator.apply_prompt_set("v2")
    assert routes_pitch.orchestrator.BUILDER_SYSTEM == prompts.get_prompt_set("v2").builder


def test_v1_matches_the_recorded_baseline_wording():
    """v1 is the measurement baseline. Editing it invalidates every score.

    These anchors are deliberately specific: if someone rewords v1, this fails
    and they have to decide consciously to break comparability.
    """
    v1 = prompts.get_prompt_set("v1")
    assert "decompose it into 3-5 subtasks" in v1.planner
    assert "Keep subtask count between 3 and 5" in v1.planner
    assert "No TODOs, no placeholders" in v1.builder
    assert "## Final Assembled Output" in v1.reviewer
    assert "Keep everything that was working" in v1.reviser


def test_v2_addresses_the_logged_failure_modes():
    """v2 exists to fix specific observed defects — keep it pointed at them."""
    v2 = prompts.get_prompt_set("v2")
    # Cross-agent blindness and name drift
    assert "sees ONLY its own subtask prompt" in v2.planner
    assert "shared names" in v2.planner.lower()
    # Truncation
    assert "SMALLER" in v2.builder
    # Reviewer refusal shipped as the deliverable
    assert "NEVER put an apology, a refusal" in v2.reviewer


@pytest.mark.parametrize("set_name", ["v1", "v2"])
def test_pipeline_runs_end_to_end_on_each_set(set_name, tmp_path, monkeypatch):
    """Both sets must actually drive a full pipeline, not just parse."""
    orchestrator.apply_prompt_set(set_name)
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")

    seen_systems = []

    async def fake_generate(prompt, system="", model=None, role=None, format=None):
        seen_systems.append(system)
        if system == orchestrator.PLANNER_SYSTEM:
            return (
                '[{"id": 1, "title": "One", "prompt": "do a thing", "depends_on": []},'
                ' {"id": 2, "title": "Two", "prompt": "do another", "depends_on": [1]}]'
            )
        if system == orchestrator.REVIEWER_SYSTEM:
            return (
                "## Quality Rating\nPASS\n\n## Issues Found\nNone\n\n"
                "## Final Assembled Output\n\n```python\nprint('ok')\n```\n"
            )
        return "```python\nprint('built')\n```"

    async def fake_stream(*a, **k):
        yield ""

    monkeypatch.setattr(orchestrator, "generate", fake_generate)
    monkeypatch.setattr(orchestrator, "generate_stream", fake_stream)

    result = asyncio.run(orchestrator.run_pipeline("build a thing"))

    assert result["rating"] == "PASS"
    # The prompts the pipeline actually sent belong to the selected set.
    chosen = prompts.get_prompt_set(set_name)
    assert chosen.planner in seen_systems
    assert chosen.reviewer in seen_systems
