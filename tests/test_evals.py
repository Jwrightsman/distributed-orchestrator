"""Tests for the eval harness.

The harness is the instrument the whole output-quality phase is measured with,
so it needs to be at least as trustworthy as the pipeline it grades. These
tests pin the scoring rules — especially that a run producing no runnable file
can never be counted as a success.
"""

import json
import sys
from pathlib import Path

import pytest

EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS_DIR))

import scoring  # noqa: E402


# ── prompt set ──────────────────────────────────────────────────────────────

def test_prompt_set_is_valid_and_broad():
    """The corpus is larger than the original 28. The size is not arbitrary.

    It was chosen from the power analysis in `docs/eval-methodology.md`: at the
    measured discordant rate of 0.643, n=28 could only detect a 38-point
    difference at 80% power, and n=100 brings that to 21 points. The schema
    checks live in `tests/test_eval_corpus.py`; this one keeps the historical
    guarantees the original 28 items were written under.
    """
    data = json.loads((EVALS_DIR / "prompts.json").read_text(encoding="utf-8"))
    prompts = data["prompts"]

    assert len(prompts) >= 100, "the corpus was grown to 100 items — see the power analysis"

    ids = [p["id"] for p in prompts]
    assert len(ids) == len(set(ids)), "prompt ids must be unique"

    categories = {p["category"] for p in prompts}
    for required in ("web_app", "cli_tool", "data_processing", "api", "algorithm", "vague"):
        assert required in categories, f"missing category {required}"

    vague = [p for p in prompts if p["category"] == "vague"]
    assert len(vague) >= 3, "need deliberately ambiguous prompts for robustness"

    for p in prompts:
        assert p["task"].strip()
        assert p["expect"]["artifact"] in ("python", "html", "any")


# ── scoring rules ───────────────────────────────────────────────────────────

def test_no_files_is_never_a_success():
    record = {
        "extracted": False,
        "parses": True,
        "executes": True,
        "artifact_match": True,
        "keywords_ok": True,
        "judge_score": 5,
    }
    assert scoring.is_success(record) is False


def test_high_judge_score_cannot_rescue_broken_code():
    record = {
        "extracted": True,
        "parses": False,
        "executes": False,
        "artifact_match": True,
        "keywords_ok": True,
        "judge_score": 5,
    }
    assert scoring.is_success(record) is False


def test_runnable_and_on_spec_is_a_success():
    record = {
        "extracted": True,
        "parses": True,
        "executes": True,
        "artifact_match": True,
        "keywords_ok": True,
        "judge_score": 4,
    }
    assert scoring.is_success(record) is True


def test_judge_below_bar_fails():
    record = {
        "extracted": True,
        "parses": True,
        "executes": True,
        "artifact_match": True,
        "keywords_ok": True,
        "judge_score": 3,
    }
    assert scoring.is_success(record) is False


def test_missing_judge_score_is_not_a_pass():
    record = {
        "extracted": True,
        "parses": True,
        "executes": True,
        "artifact_match": True,
        "keywords_ok": True,
        "judge_score": None,
    }
    assert scoring.is_success(record) is False


def test_mechanical_only_scoring_drops_the_judge_gate():
    """--no-judge runs score without a judge, but only when asked."""
    record = {
        "extracted": True,
        "parses": True,
        "executes": True,
        "artifact_match": True,
        "keywords_ok": True,
        "judge_score": None,
    }
    assert scoring.is_success(record) is False
    assert scoring.is_success(record, require_judge=False) is True


def test_mechanical_only_scoring_still_requires_runnable_output():
    """Dropping the judge must not turn into dropping every standard."""
    record = {
        "extracted": True,
        "parses": False,
        "executes": False,
        "artifact_match": True,
        "keywords_ok": True,
        "judge_score": None,
    }
    assert scoring.is_success(record, require_judge=False) is False


@pytest.mark.parametrize(
    "response,expected",
    [("5", 5), ("4/5", 4), ("I would say 3.", 3), ("score: 1", 1), ("", None), ("nine", None)],
)
def test_parse_judge_score(response, expected):
    assert scoring.parse_judge_score(response) == expected


def test_judge_prompt_truncates_long_deliverables():
    prompt = scoring.build_judge_prompt("do a thing", "x" * 50_000)
    assert len(prompt) < 20_000
    assert "truncated for grading" in prompt


# ── artifact checks ─────────────────────────────────────────────────────────

def test_keywords_are_matched_case_insensitively(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("import ArgParse\nCSV = 1\n")
    ok, missing = scoring.check_keywords([str(f)], ["argparse", "csv"])
    assert ok is True
    assert missing == []


def test_missing_keywords_are_reported(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("print('hi')\n")
    ok, missing = scoring.check_keywords([str(f)], ["argparse"])
    assert ok is False
    assert missing == ["argparse"]


def test_artifact_kind_mismatch_is_caught(tmp_path):
    py = tmp_path / "game.py"
    py.write_text("print('hi')\n")
    # Asked for a single HTML file, got Python — the exact showcase failure
    # mode from the August sprint.
    assert scoring.matches_expected_artifact([str(py)], "html") is False
    assert scoring.matches_expected_artifact([str(py)], "python") is True
    assert scoring.matches_expected_artifact([str(py)], "any") is True


def test_parses_uses_the_production_checker(tmp_path):
    bad = tmp_path / "broken.py"
    bad.write_text("def f(:\n    pass\n")
    ok, problems = scoring.check_parses([str(bad)])
    assert ok is False
    assert problems


# ── execution ───────────────────────────────────────────────────────────────

def test_execute_python_clean_exit(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text("print('done')\n")
    result = scoring.execute_python([str(f)])
    assert result["ok"] is True
    assert result["outcome"] == "exited_clean"


def test_execute_python_reports_crash(tmp_path):
    f = tmp_path / "boom.py"
    f.write_text("raise ValueError('nope')\n")
    result = scoring.execute_python([str(f)])
    assert result["ok"] is False
    assert result["outcome"] == "error"
    assert "nope" in result["detail"]


def test_execute_python_flags_missing_dependency(tmp_path):
    f = tmp_path / "needs.py"
    f.write_text("import definitely_not_a_real_package_xyz\n")
    result = scoring.execute_python([str(f)])
    assert result["ok"] is False
    assert result["outcome"] == "missing_dependency"


def test_execute_python_tolerates_scripts_wanting_stdin(tmp_path):
    f = tmp_path / "asks.py"
    f.write_text("name = input('name? ')\nprint(name)\n")
    result = scoring.execute_python([str(f)])
    assert result["outcome"] == "needs_stdin"
    assert result["ok"] is True


def test_long_running_script_counts_as_running(tmp_path):
    f = tmp_path / "loop.py"
    f.write_text("import time\nwhile True:\n    time.sleep(0.1)\n")
    result = scoring.execute_python([str(f)], timeout=2)
    assert result["ok"] is True
    assert result["outcome"] == "ran_until_timeout"


def test_entrypoint_prefers_main_guard(tmp_path):
    lib = tmp_path / "helpers.py"
    lib.write_text("# a longer file that is not the entrypoint\n" + "x = 1\n" * 50)
    main = tmp_path / "app.py"
    main.write_text("if __name__ == '__main__':\n    print('go')\n")
    assert scoring._python_entrypoint([str(lib), str(main)]) == str(main)


def test_html_static_check_catches_truncated_script(tmp_path):
    f = tmp_path / "page.html"
    f.write_text("<!DOCTYPE html><html><body><script>let a = 1;")
    result = scoring._html_static_check(str(f))
    assert result["ok"] is False
    assert result["outcome"] == "unbalanced_script"


def test_html_static_check_accepts_sound_page(tmp_path):
    f = tmp_path / "page.html"
    f.write_text("<!DOCTYPE html><html><body><script>let a = 1;</script></body></html>")
    result = scoring._html_static_check(str(f))
    assert result["ok"] is True


# ── aggregation ─────────────────────────────────────────────────────────────

def _record(**over):
    base = {
        "id": "x",
        "category": "web_app",
        "extracted": True,
        "parses": True,
        "executes": True,
        "artifact_match": True,
        "keywords_ok": True,
        "judge_score": 5,
        "seconds": 10.0,
        "subtask_count": 3,
    }
    base.update(over)
    base["success"] = scoring.is_success(base)
    return base


def test_summarize_computes_rate_and_breakdown():
    records = [
        _record(id="a"),
        _record(id="b"),
        _record(id="c", category="cli_tool", extracted=False, parses=False, executes=False),
        _record(id="d", category="cli_tool", judge_score=2),
    ]
    summary = scoring.summarize(records)

    assert summary["total"] == 4
    assert summary["success"] == 2
    assert summary["success_rate"] == 0.5
    assert summary["by_category"]["web_app"]["rate"] == 1.0
    assert summary["by_category"]["cli_tool"]["rate"] == 0.0
    assert summary["by_stage"]["no_files_extracted"] == 1
    assert summary["by_stage"]["judged_below_bar"] == 1


def test_unparseable_judge_is_counted_not_hidden():
    records = [_record(id="a"), _record(id="b", judge_score=None)]
    summary = scoring.summarize(records)

    assert summary["success"] == 1
    assert summary["by_stage"]["judge_unparseable"] == 1


def test_summarize_handles_empty_run():
    summary = scoring.summarize([])
    assert summary["total"] == 0
    assert summary["success_rate"] == 0.0


def test_render_markdown_includes_headline_and_rows():
    records = [_record(id="a"), _record(id="b", judge_score=1)]
    summary = scoring.summarize(records)
    md = scoring.render_markdown(summary, records, {"run_id": "T", "model": "m", "mode": "real"})

    assert "Success rate: 50%" in md
    assert "`a`" in md and "`b`" in md
    assert "Failure detail" in md
