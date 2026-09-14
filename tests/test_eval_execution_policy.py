"""An execution opt-out must reach every grader, regardless of artifact kind."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import corpus  # noqa: E402
import grading  # noqa: E402
import run_evals  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", ["python", "html"])
async def test_no_exec_never_enters_legacy_or_primary_execution(tmp_path, monkeypatch, artifact):
    sentinel = tmp_path / "executed.txt"
    if artifact == "python":
        source = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('bad')\n"
        specs = [
            {"kind": "stdout_contains", "substrings": ["expected"]},
            {"kind": "stdout_json_schema", "schema": "record_array"},
        ]
        path = tmp_path / "main.py"
    else:
        source = "<!DOCTYPE html><html><body><canvas></canvas></body></html>"
        specs = [{"kind": "html_behaviour", "canvas_drawn": True}]
        path = tmp_path / "index.html"
    path.write_text(source, encoding="utf-8")
    item = corpus.CorpusItem(
        "policy", "script", "algorithmic_kernel", "authored fixture", "taxonomy",
        "development", {"artifact": artifact, "checks": specs},
    )
    calls = []
    for module, name in [
        (run_evals, "execute_artifacts"),
        (grading, "check_runs"),
        (grading, "run_python"),
        (grading, "check_stdout_contains"),
        (grading, "check_stdout_json_schema"),
        (grading, "check_html_behaviour"),
        (grading.scoring, "execute_html"),
    ]:
        spy = Mock(side_effect=AssertionError(f"execution entered: {name}"))
        monkeypatch.setattr(module, name, spy)
        calls.append(spy)
    monkeypatch.setattr(run_evals, "run_pipeline", AsyncMock(return_value={
        "code_files": [str(path)], "final_output": "fixture", "plan": [],
    }))
    args = SimpleNamespace(orchestrator=None, no_exec=True, no_judge=True, exec_timeout=1)
    record = await run_evals.run_one(item, args, tmp_path)

    assert all(call.call_count == 0 for call in calls)
    assert not sentinel.exists()
    assert record["exec_outcome"] == "skipped"
    assert not record["graded"] and not record["primary_pass"]
    checks = {check["kind"]: check for check in record["grading"]["checks"]}
    for kind in ["runs", *(spec["kind"] for spec in specs)]:
        assert checks[kind]["graded"] is False
        assert checks[kind]["passed"] is False
        assert "disabled" in checks[kind]["detail"]
    assert checks["parses"]["graded"] and checks["parses"]["passed"]
    assert checks["artifact_kind"]["passed"]


def test_disabled_execution_still_reports_static_failure(tmp_path):
    path = tmp_path / "broken.py"
    path.write_text("def f(:\n", encoding="utf-8")
    item = corpus.CorpusItem(
        "broken", "script", "algorithmic_kernel", "fixture", "taxonomy",
        "development", {"artifact": "python"},
    )
    result = grading.grade(item, [str(path)], execution_enabled=False)
    assert result.failed_checks == ["parses"]
    assert result.ungraded_checks == ["runs"]
    assert not result.graded and not result.passed
