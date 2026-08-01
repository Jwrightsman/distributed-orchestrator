"""Unit tests for the orchestrator's parsing and validation helpers.

These are the functions that stand between a small local model's messy output
and the pipeline — the highest-value regression surface in the repo.
"""

import json

import pytest

from orchestrator import (
    _extract_final_output,
    _extract_issues,
    _extract_json,
    _extract_rating,
    _is_refusal,
    _validate_subtasks,
)

VALID_PLAN = [
    {"id": 1, "title": "Design schema", "prompt": "Design the database schema for the app.", "depends_on": []},
    {"id": 2, "title": "Write API", "prompt": "Write the REST API using the schema.", "depends_on": [1]},
]


# ── _extract_json ────────────────────────────────────────────────────────

def test_extract_json_plain_array():
    assert _extract_json(json.dumps(VALID_PLAN)) == VALID_PLAN


def test_extract_json_markdown_fenced():
    text = "```json\n" + json.dumps(VALID_PLAN) + "\n```"
    assert _extract_json(text) == VALID_PLAN


def test_extract_json_with_surrounding_prose():
    text = "Here is your plan:\n" + json.dumps(VALID_PLAN) + "\nHope that helps!"
    assert _extract_json(text) == VALID_PLAN


def test_extract_json_single_object_wrapped_in_list():
    obj = {"id": 1, "title": "Only task", "prompt": "Do the whole thing.", "depends_on": []}
    # No array anywhere — model returned one object; it should be wrapped
    assert _extract_json(json.dumps(obj)) == [obj]


def test_extract_json_no_json_raises():
    with pytest.raises(ValueError):
        _extract_json("Sorry, I can't produce a plan for that.")


def test_extract_json_malformed_raises():
    with pytest.raises(json.JSONDecodeError):
        _extract_json('[{"id": 1, "title": "broken"')


# ── _validate_subtasks ───────────────────────────────────────────────────

def test_validate_accepts_valid_plan():
    cleaned = _validate_subtasks(VALID_PLAN)
    assert [st["id"] for st in cleaned] == [1, 2]
    assert cleaned[1]["depends_on"] == [1]


def test_validate_empty_list_raises():
    with pytest.raises(ValueError):
        _validate_subtasks([])


def test_validate_non_list_raises():
    with pytest.raises(ValueError):
        _validate_subtasks({"id": 1})


def test_validate_caps_at_five_subtasks():
    plan = [
        {"id": i, "title": f"Task {i}", "prompt": f"Do thing {i} completely.", "depends_on": []}
        for i in range(1, 8)
    ]
    assert len(_validate_subtasks(plan)) == 5


def test_validate_missing_title_raises():
    with pytest.raises(ValueError):
        _validate_subtasks([{"id": 1, "title": "", "prompt": "Do it.", "depends_on": []}])


def test_validate_missing_prompt_raises():
    with pytest.raises(ValueError):
        _validate_subtasks([{"id": 1, "title": "A task", "prompt": "", "depends_on": []}])


def test_validate_drops_forward_references():
    plan = [
        {"id": 1, "title": "First", "prompt": "Do the first part.", "depends_on": [2]},
        {"id": 2, "title": "Second", "prompt": "Do the second part.", "depends_on": [1]},
    ]
    cleaned = _validate_subtasks(plan)
    # Forward ref (1 depends on 2) is dropped; backward ref (2 on 1) survives —
    # this is also what makes model-emitted "cycles" resolvable
    assert cleaned[0]["depends_on"] == []
    assert cleaned[1]["depends_on"] == [1]


def test_validate_normalizes_string_ids_and_deps():
    plan = [
        {"id": "1", "title": "First", "prompt": "Do the first part.", "depends_on": []},
        {"id": "2", "title": "Second", "prompt": "Do the second part.", "depends_on": ["1"]},
    ]
    cleaned = _validate_subtasks(plan)
    assert cleaned[0]["id"] == 1
    assert cleaned[1]["depends_on"] == [1]


def test_validate_missing_id_gets_positional_id():
    plan = [
        {"title": "First", "prompt": "Do the first part completely."},
        {"title": "Second", "prompt": "Do the second part completely."},
    ]
    cleaned = _validate_subtasks(plan)
    assert [st["id"] for st in cleaned] == [1, 2]


def test_validate_non_list_depends_on_treated_as_empty():
    plan = [{"id": 1, "title": "Task", "prompt": "Do the task fully.", "depends_on": "none"}]
    assert _validate_subtasks(plan)[0]["depends_on"] == []


# ── Reviewer output extraction ───────────────────────────────────────────

REVIEW = """## Quality Rating
PASS

## Issues Found
None

## Final Assembled Output
# The Deliverable

Here is the complete assembled result with plenty of content in it.
"""


def test_extract_rating_pass():
    assert _extract_rating(REVIEW) == "PASS"


def test_extract_rating_needs_work():
    assert _extract_rating(REVIEW.replace("PASS", "NEEDS_WORK")) == "NEEDS_WORK"


def test_extract_rating_fail():
    assert _extract_rating(REVIEW.replace("PASS", "FAIL")) == "FAIL"


def test_extract_rating_defaults_to_pass_when_missing():
    assert _extract_rating("no rating anywhere in this text") == "PASS"


def test_extract_issues_none_is_empty():
    assert _extract_issues(REVIEW) == ""


def test_extract_issues_returns_section():
    review = REVIEW.replace("None", "- The code is missing imports\n- No error handling")
    issues = _extract_issues(review)
    assert "missing imports" in issues
    assert "Final Assembled Output" not in issues


def test_extract_final_output_returns_section():
    out = _extract_final_output(REVIEW)
    assert out is not None
    assert out.startswith("# The Deliverable")


def test_extract_final_output_tolerates_header_case():
    review = REVIEW.replace("## Final Assembled Output", "## FINAL ASSEMBLED OUTPUT")
    out = _extract_final_output(review)
    assert out is not None and "Deliverable" in out


def test_extract_final_output_fallback_strips_preamble():
    # No "Final Assembled Output" header at all — fallback strips the known
    # sections and returns the rest if it's substantial
    review = (
        "## Quality Rating\nPASS\n\n## Issues Found\nNone\n\n"
        "# Actual Content\n" + ("x" * 200)
    )
    out = _extract_final_output(review)
    assert out is not None and "Actual Content" in out


def test_extract_final_output_returns_none_when_nothing_left():
    assert _extract_final_output("## Quality Rating\nPASS\n\n## Issues Found\nNone\n") is None


# ── Refusal detection ────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "I cannot help with that task.",
    "I'm unable to complete this.",
    "As an AI language model, I don't write code.",
    "  I apologize, but this request is unclear.",
])
def test_is_refusal_true(text):
    assert _is_refusal(text)


@pytest.mark.parametrize("text", [
    "def main():\n    print('hello world')",
    "# Report\nThe system I cannot describe... just kidding, here it is.",
])
def test_is_refusal_false(text):
    assert not _is_refusal(text)
