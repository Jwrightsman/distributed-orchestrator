"""Tests for the builder prompt composer.

Builders see only their own subtask text. Observed failure (Aug 1, showcase
run): given "build a single self-contained HTML game", the planner wrote a
subtask reading "implement the game logic" and the builder produced a Python
class. The overall project must travel with every subtask.
"""

from orchestrator import _MAX_CONTEXT_CHARS, compose_builder_prompt

SUBTASK = {"id": 2, "title": "Game logic", "prompt": "Implement the game loop and collision detection."}
TASK = "Build a retro Snake game as ONE single self-contained HTML file."


def test_includes_subtask_prompt():
    prompt = compose_builder_prompt(SUBTASK)
    assert "Implement the game loop" in prompt


def test_includes_overall_task_when_given():
    prompt = compose_builder_prompt(SUBTASK, task=TASK)
    assert "single self-contained HTML file" in prompt
    assert "Do not switch technologies" in prompt


def test_omits_project_section_when_no_task():
    prompt = compose_builder_prompt(SUBTASK)
    assert "The overall project" not in prompt


def test_includes_context_when_given():
    prompt = compose_builder_prompt(SUBTASK, context="[Design]: use a 20x20 grid", task=TASK)
    assert "20x20 grid" in prompt
    assert "Context from previous subtasks" in prompt


def test_omits_context_section_when_empty():
    assert "Context from previous subtasks" not in compose_builder_prompt(SUBTASK, task=TASK)


def test_long_context_truncated_from_the_front():
    long_context = "OLDEST-MARKER" + ("x" * (_MAX_CONTEXT_CHARS + 500)) + "NEWEST-MARKER"
    prompt = compose_builder_prompt(SUBTASK, context=long_context, task=TASK)
    # Keeps the most recent context; drops the oldest
    assert "NEWEST-MARKER" in prompt
    assert "OLDEST-MARKER" not in prompt
    assert "truncated" in prompt


def test_ordering_project_then_context_then_subtask():
    prompt = compose_builder_prompt(SUBTASK, context="prior output here", task=TASK)
    assert prompt.index("The overall project") < prompt.index("Context from previous subtasks")
    assert prompt.index("Context from previous subtasks") < prompt.index("Your subtask")


def test_distributed_and_local_paths_compose_identically():
    """routes_pitch dispatch must give remote nodes the same prompt as a local build."""
    import routes_pitch

    assert routes_pitch.compose_builder_prompt is compose_builder_prompt
