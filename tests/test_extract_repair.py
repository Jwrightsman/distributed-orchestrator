"""Tests for the extract → verify → repair stage shared by both pipelines."""

from pathlib import Path

import pytest

import orchestrator
from orchestrator import extract_and_repair

BROKEN_MD = """Here is the code.

```python
def handler(items):
    return {"out": [
        {"id": i},
        for i in items
    ]}
```
"""

FIXED_MD = """Here is the corrected code.

```python
def handler(items):
    return {"out": [
        {"id": i}
        for i in items
    ]}
```
"""

GOOD_MD = """All good.

```python
def handler(items):
    return {"out": [{"id": i} for i in items]}
```
"""


@pytest.mark.asyncio
async def test_clean_output_needs_no_repair(tmp_path, monkeypatch):
    called = {"n": 0}

    async def should_not_run(*a, **kw):
        called["n"] += 1
        return ""

    monkeypatch.setattr(orchestrator, "revise", should_not_run)
    final, files, problems = await extract_and_repair("t", GOOD_MD, GOOD_MD, tmp_path)
    assert problems == []
    assert called["n"] == 0  # no revision spent on working code
    assert len(files) == 1


@pytest.mark.asyncio
async def test_broken_code_gets_repaired(tmp_path, monkeypatch):
    seen_issues = {}

    async def fake_revise(task, issues, current):
        seen_issues["text"] = issues
        return FIXED_MD

    monkeypatch.setattr(orchestrator, "revise", fake_revise)
    final, files, problems = await extract_and_repair("t", BROKEN_MD, BROKEN_MD, tmp_path)

    assert problems == []            # defect resolved
    assert final == FIXED_MD         # repaired output adopted
    # The reviser was told exactly what was wrong, not just "fix it"
    assert "not valid Python" in seen_issues["text"]
    # Saved artifacts reflect the repair
    assert (tmp_path / "output.md").read_text() == FIXED_MD
    assert "for i in items" in Path(files[0]).read_text()


@pytest.mark.asyncio
async def test_failed_repair_keeps_original(tmp_path, monkeypatch):
    async def unhelpful_revise(task, issues, current):
        return BROKEN_MD.replace("{\"id\": i},", "{\"id\": i},,")  # still broken

    monkeypatch.setattr(orchestrator, "revise", unhelpful_revise)
    final, files, problems = await extract_and_repair("t", BROKEN_MD, BROKEN_MD, tmp_path)
    assert final == BROKEN_MD  # no improvement — original retained
    assert problems  # and the caller still learns it's broken


@pytest.mark.asyncio
async def test_truncated_repair_rejected(tmp_path, monkeypatch):
    async def truncating_revise(task, issues, current):
        return "oops"

    monkeypatch.setattr(orchestrator, "revise", truncating_revise)
    final, _, problems = await extract_and_repair("t", BROKEN_MD, BROKEN_MD, tmp_path)
    assert final == BROKEN_MD
    assert problems


@pytest.mark.asyncio
async def test_repair_scratch_dir_cleaned_up(tmp_path, monkeypatch):
    async def fake_revise(task, issues, current):
        return FIXED_MD

    monkeypatch.setattr(orchestrator, "revise", fake_revise)
    await extract_and_repair("t", BROKEN_MD, BROKEN_MD, tmp_path)
    assert not (tmp_path / "_repair_check").exists()


@pytest.mark.asyncio
async def test_falls_back_to_review_when_no_final_output(tmp_path, monkeypatch):
    async def should_not_run(*a, **kw):
        raise AssertionError("no repair possible without final_output")

    monkeypatch.setattr(orchestrator, "revise", should_not_run)
    final, files, problems = await extract_and_repair("t", None, BROKEN_MD, tmp_path)
    assert final is None
    assert len(files) == 1   # still extracted from the review text
    assert problems          # and still reported as broken
