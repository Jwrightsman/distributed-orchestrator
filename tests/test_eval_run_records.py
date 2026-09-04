"""Run records, and the summariser's refusal to report an incomplete study.

Two properties:

1. **Records are append-only and carry their own conditions.** A re-run does
   not overwrite an earlier one; comparing them is how you detect that
   something moved underneath you. Facts the run could not determine are
   recorded as unknown rather than inferred, which is the convention the
   provenance envelope already uses (ADR 0017).

2. **The summariser refuses an incomplete study.** A missing cell or an
   ungraded item stops it computing anything at all. A partial study is not a
   smaller study — its missing cells are correlated with something, and you do
   not know what.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals"))
import runrecord  # noqa: E402

SUMMARY_SCRIPT = REPO / "scripts" / "eval_study_summary.py"


def make_record(item_id, arm, passed=True, graded=True, **over):
    base = dict(
        study_id="s1",
        run_id="r1",
        item_id=item_id,
        arm=arm,
        strategy="dag",
        strategy_config={"prompt_set": "v3"},
        corpus_version="2",
        corpus_digest="abc123",
        band="discriminating",
        model=runrecord.ModelIdentity("ollama", "qwen3.5:4b", digest="sha256:aa"),
        descriptor_hash=None,
        wall_clock_seconds=60.0,
        tokens=None,
        artifact_sha256="deadbeef",
        artifact_paths=["a.py"],
        grading={"grader_version": "2", "ungraded_checks": []},
        graded=graded,
        passed=passed,
    )
    base.update(over)
    return runrecord.RunRecord(**base)


def write_study(tmp_path, records):
    for record in records:
        runrecord.append_run(tmp_path, record)
    return tmp_path


def run_summary(study_dir, *args):
    return subprocess.run(
        [sys.executable, str(SUMMARY_SCRIPT), str(study_dir), *args],
        capture_output=True, text=True, cwd=str(REPO),
    )


# -- the record itself ------------------------------------------------------

def test_a_record_carries_the_conditions_it_ran_under(tmp_path):
    payload = make_record("x", "a").as_record()
    for field in (
        "corpus_version", "corpus_digest", "item_id", "band", "arm", "strategy",
        "strategy_config", "model", "descriptor_hash", "wall_clock_seconds",
        "tokens", "artifact_sha256", "grading", "passed", "recorded_at",
    ):
        assert field in payload, f"a run record without {field} cannot be compared to another"
    assert payload["model"]["provider"] == "ollama"
    assert payload["model"]["digest"] == "sha256:aa"


def test_absent_facts_are_recorded_as_unknown_not_inferred():
    payload = make_record(
        "x", "a",
        model=runrecord.ModelIdentity("ollama", "qwen3.5:4b", digest=None),
    ).as_record()
    assert runrecord.UNKNOWN_MODEL_DIGEST in payload["unknown_facts"]
    assert runrecord.UNKNOWN_DESCRIPTOR_HASH in payload["unknown_facts"]
    assert runrecord.UNKNOWN_TOKENS in payload["unknown_facts"]
    assert runrecord.UNKNOWN_PROVENANCE in payload["unknown_facts"]


def test_a_complete_record_claims_no_unknowns():
    payload = make_record(
        "x", "a",
        model=runrecord.ModelIdentity(
            "ollama", "qwen3.5:4b", digest="sha256:aa", temperature=0.0, seed=7
        ),
        descriptor_hash="d" * 64,
        tokens={"prompt": 100, "completion": 200},
        provenance_envelope_digest="e" * 64,
    ).as_record()
    assert payload["unknown_facts"] == []


def test_an_unpinned_temperature_and_seed_are_reported_as_unknown():
    """Today's ordinary case, and it is a gap rather than a detail.

    `config.json` has no temperature or seed setting, so a local run takes
    whatever Ollama defaults to. A study that claims to have pinned them would
    be claiming something the harness cannot currently deliver, so the record
    says it does not know.
    """
    payload = make_record("x", "a").as_record()
    assert payload["model"]["temperature"] is None
    assert payload["model"]["seed"] is None
    assert runrecord.UNKNOWN_TEMPERATURE in payload["unknown_facts"]
    assert runrecord.UNKNOWN_SEED in payload["unknown_facts"]


def test_the_judge_score_is_labelled_exploratory_in_the_record():
    payload = make_record("x", "a", judge_score=5).as_record()
    assert payload["judge_score"] == 5
    assert "exploratory" in payload["judge_score_status"]


def test_appending_never_overwrites(tmp_path):
    runrecord.append_run(tmp_path, make_record("x", "a", passed=True))
    runrecord.append_run(tmp_path, make_record("x", "a", passed=False))
    records = runrecord.load_runs(tmp_path)
    assert len(records) == 2
    assert [r["passed"] for r in records] == [True, False]
    assert runrecord.superseded_count(records) == 1


def test_the_latest_record_speaks_for_a_cell_but_the_earlier_one_survives(tmp_path):
    runrecord.append_run(tmp_path, make_record("x", "a", passed=True))
    runrecord.append_run(tmp_path, make_record("x", "a", passed=False))
    records = runrecord.load_runs(tmp_path)
    latest = runrecord.latest_per_key(records)
    assert latest[("x", "a", 0)]["passed"] is False
    assert len(records) == 2


def test_loading_a_missing_run_log_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        runrecord.load_runs(tmp_path)


def test_loading_an_empty_run_log_raises(tmp_path):
    (tmp_path / runrecord.RUNS_FILENAME).write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="no runs"):
        runrecord.load_runs(tmp_path)


def test_artifact_digest_changes_with_content(tmp_path):
    a = tmp_path / "a.py"
    a.write_text("print(1)\n", encoding="utf-8")
    first = runrecord.artifact_digest([str(a)])
    a.write_text("print(2)\n", encoding="utf-8")
    assert runrecord.artifact_digest([str(a)]) != first


def test_artifact_digest_is_none_when_nothing_was_produced(tmp_path):
    assert runrecord.artifact_digest([]) is None
    assert runrecord.artifact_digest([str(tmp_path / "never-written.py")]) is None


# -- the summariser ---------------------------------------------------------

def test_the_summariser_reports_a_complete_study(tmp_path):
    records = []
    for index in range(6):
        records.append(make_record(f"item-{index}", "baseline", passed=index < 2))
        records.append(make_record(f"item-{index}", "candidate", passed=index < 5))
    study = write_study(tmp_path, records)

    result = run_summary(study, "--paired", "baseline", "candidate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2/6" in result.stdout and "5/6" in result.stdout
    assert "McNemar exact" in result.stdout
    # The rule that outlives this test: never a p-value on its own.
    assert "paired items" in result.stdout and "discordant" in result.stdout


def test_the_summariser_refuses_a_missing_cell(tmp_path):
    records = [
        make_record("item-0", "baseline"),
        make_record("item-0", "candidate"),
        make_record("item-1", "baseline"),  # candidate never ran this one
    ]
    result = run_summary(write_study(tmp_path, records))
    assert result.returncode == 1
    assert "REFUSING TO SUMMARISE" in result.stdout
    assert "never ran" in result.stdout


def test_the_summariser_refuses_an_ungraded_item(tmp_path):
    records = [
        make_record("item-0", "baseline"),
        make_record("item-0", "candidate"),
        make_record("item-1", "baseline"),
        make_record(
            "item-1", "candidate", graded=False, passed=False,
            grading={"grader_version": "2", "ungraded_checks": ["html_behaviour"]},
        ),
    ]
    result = run_summary(write_study(tmp_path, records))
    assert result.returncode == 1
    assert "REFUSING TO SUMMARISE" in result.stdout
    assert "not graded" in result.stdout
    assert "html_behaviour" in result.stdout


def test_the_summariser_refuses_an_empty_study(tmp_path):
    (tmp_path / runrecord.RUNS_FILENAME).write_text("", encoding="utf-8")
    result = run_summary(tmp_path)
    assert result.returncode == 1
    assert "ERROR" in result.stdout


def test_the_summariser_warns_when_the_model_changed_mid_study(tmp_path):
    records = [
        make_record("item-0", "baseline"),
        make_record("item-0", "candidate",
                    model=runrecord.ModelIdentity("ollama", "qwen3.5:4b", digest="sha256:bb")),
    ]
    result = run_summary(write_study(tmp_path, records))
    assert result.returncode == 0
    assert "the model changed during this study, which invalidates it" in result.stdout


def test_the_summariser_warns_when_grader_versions_differ(tmp_path):
    records = [
        make_record("item-0", "baseline"),
        make_record("item-0", "candidate",
                    grading={"grader_version": "1", "ungraded_checks": []}),
    ]
    result = run_summary(write_study(tmp_path, records))
    assert "different grader versions" in result.stdout


def test_the_summariser_says_when_the_arms_were_not_cost_matched(tmp_path):
    """Equal-compute is the primary endpoint, so it has to be checked, not assumed."""
    records = []
    for index in range(4):
        records.append(make_record(f"item-{index}", "baseline", wall_clock_seconds=3000.0))
        records.append(make_record(f"item-{index}", "candidate", wall_clock_seconds=360.0))
    result = run_summary(write_study(tmp_path, records), "--paired", "baseline", "candidate")
    assert result.returncode == 0
    assert "NOT within" in result.stdout
    assert "equal-compute endpoint is not established" in result.stdout


def test_the_summariser_confirms_equal_compute_when_the_arms_match(tmp_path):
    records = []
    for index in range(4):
        records.append(make_record(f"item-{index}", "baseline", wall_clock_seconds=1800.0))
        records.append(make_record(f"item-{index}", "candidate", wall_clock_seconds=1900.0))
    result = run_summary(write_study(tmp_path, records), "--paired", "baseline", "candidate")
    assert result.returncode == 0
    assert "IS the equal-compute comparison" in result.stdout


def test_the_summariser_rejects_a_corrupt_run_log(tmp_path):
    (tmp_path / runrecord.RUNS_FILENAME).write_text(
        json.dumps({"item_id": "x"}) + "\nnot json at all\n", encoding="utf-8"
    )
    result = run_summary(tmp_path)
    assert result.returncode == 1
    assert "not valid JSON" in result.stdout
