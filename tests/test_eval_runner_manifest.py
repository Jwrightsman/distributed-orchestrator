"""Offline runner preflight and durable measurement identity regressions."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import corpus  # noqa: E402
import grading  # noqa: E402
import run_evals  # noqa: E402
import runrecord  # noqa: E402


def plan():
    return {
        "version": "1", "study_id": "fixture-study", "item_ids": ["fixture", "later"],
        "arms": ["dag-a", "dag-b"], "replicates": [0], "aggregation": "single_replicate",
        "identity": {
            "measurement_identity_version": "2", "measurement_digest": "measurement",
            "grader_version": grading.GRADER_VERSION, "model_digest": "authored-fixture-v1",
        },
        "budget_policy": {"metric": "descriptive_only"},
    }


@pytest.mark.parametrize("field,value", [
    ("study", "other"), ("arm", "unplanned"), ("replicate", 1),
])
def test_study_selection_rejects_unplanned_identity(field, value):
    args = SimpleNamespace(study="fixture-study", arm="dag-a", replicate=0)
    setattr(args, field, value)
    with pytest.raises(ValueError):
        run_evals.validate_study_selection(plan(), args, [SimpleNamespace(id="fixture")], "measurement")


def test_study_selection_rejects_changed_measurement_or_unplanned_item():
    args = SimpleNamespace(study="fixture-study", arm="dag-a", replicate=0)
    with pytest.raises(ValueError, match="identity"):
        run_evals.validate_study_selection(plan(), args, [SimpleNamespace(id="fixture")], "changed")
    with pytest.raises(ValueError, match="absent"):
        run_evals.validate_study_selection(plan(), args, [SimpleNamespace(id="unexpected")], "measurement")


@pytest.mark.asyncio
async def test_study_requires_preexisting_manifest_before_generation(monkeypatch):
    pipeline = AsyncMock(side_effect=AssertionError("generation before plan"))
    monkeypatch.setattr(run_evals, "run_pipeline", pipeline)
    monkeypatch.setattr(sys, "argv", ["run_evals.py", "--study", "no-plan"])
    with pytest.raises(SystemExit) as exc:
        await run_evals.main()
    assert exc.value.code == 2
    pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_fixture_runner_freezes_full_plan_and_records_measurement(tmp_path, monkeypatch):
    item = corpus.CorpusItem(
        "fixture", "script", "algorithmic_kernel", "authored", "taxonomy",
        "development", {"artifact": "python"},
    )
    artifact = tmp_path / "main.py"
    artifact.write_text("raise AssertionError('must never execute')\n", encoding="utf-8")
    monkeypatch.setattr(run_evals, "load_prompts", lambda: [item])
    monkeypatch.setattr(run_evals, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(run_evals, "install_fake_backend", lambda: None)
    monkeypatch.setattr(corpus, "check_split_lock", lambda items: [])
    monkeypatch.setattr(run_evals, "run_pipeline", AsyncMock(return_value={"code_files": [str(artifact)]}))
    capture = AsyncMock(side_effect=AssertionError("fixture queried real model"))
    monkeypatch.setattr(runrecord, "capture_model_identity", capture)
    manifest = plan()
    manifest["identity"]["measurement_digest"] = corpus.measurement_digest(
        [item], grader_version=grading.GRADER_VERSION,
    )
    manifest_path = tmp_path / "planned.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "run_evals.py", "--fake", "--no-exec", "--no-judge", "--study", "fixture-study",
        "--study-manifest", str(manifest_path), "--arm", "dag-a",
    ])
    assert await run_evals.main() == 0
    capture.assert_not_called()
    [directory] = list((tmp_path / "results").iterdir())
    assert runrecord.load_manifest(directory) == manifest  # includes the unrun item and arm
    [row] = runrecord.load_runs(directory)
    assert row["measurement_identity_version"] == "2"
    assert row["measurement_digest"] == manifest["identity"]["measurement_digest"]
    assert row["replicate"] == 0
    assert row["model"]["provider"] == "fixture"
    assert row["graded"] is False and row["passed"] is False
