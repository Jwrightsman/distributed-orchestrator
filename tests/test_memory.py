"""Tests for project memory (runs against a temp CWD — see conftest)."""

from pathlib import Path

import pytest

from memory import (
    _slug,
    add_iteration,
    create_project,
    get_memory_context,
    list_projects,
    load_project,
)


def test_slug_normalizes():
    assert _slug("My Cool App!") == "my-cool-app"
    assert _slug("  spaces   everywhere  ") == "spaces-everywhere"
    assert _slug("") == "project"
    assert len(_slug("x" * 100)) <= 40


def test_create_and_load_project():
    pid = create_project("Todo App", "Build a todo app")
    meta = load_project(pid)
    assert meta["name"] == "Todo App"
    assert meta["iteration_count"] == 0
    assert (Path("projects") / pid / "memory.md").exists()


def test_create_project_unique_slugs():
    a = create_project("Same Name", "task one")
    b = create_project("Same Name", "task two")
    assert a != b


def test_load_missing_project_raises():
    with pytest.raises(FileNotFoundError):
        load_project("does-not-exist")


def test_memory_context_empty_before_iterations():
    pid = create_project("Fresh", "Build something")
    assert get_memory_context(pid) == ""


def test_add_iteration_updates_memory_and_meta():
    pid = create_project("Iterating", "Build something")
    result = {
        "project_dir": "nonexistent",  # no files to copy — that's fine
        "plan": [{"id": 1, "title": "Design it", "prompt": "p", "depends_on": []}],
        "rating": "PASS",
        "final_output": "The complete deliverable text.",
    }
    n = add_iteration(pid, result, "Build something")
    assert n == 1
    assert load_project(pid)["iteration_count"] == 1

    context = get_memory_context(pid)
    assert "Design it" in context
    assert "PASS" in context


def test_memory_context_truncated():
    pid = create_project("Big", "Build something")
    memory_file = Path("projects") / pid / "memory.md"
    memory_file.write_text("# Project: Big\n" + "x" * 5000)
    context = get_memory_context(pid)
    assert len(context) < 2200
    assert "truncated" in context


def test_list_projects_sorted_most_recent_first():
    a = create_project("First", "task")
    b = create_project("Second", "task")
    add_iteration(b, {"plan": [], "rating": "PASS", "final_output": ""}, "task")
    projects = list_projects()
    assert projects[0]["project_id"] == b
    assert {p["project_id"] for p in projects} == {a, b}


def test_list_projects_empty():
    assert list_projects() == []
