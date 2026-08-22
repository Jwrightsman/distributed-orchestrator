"""Ensemble strategy and the statistics that decide whether it won.

The statistics matter more than the plumbing here. This project has already
deleted two prompt sets that looked like improvements and were noise, so the
test that counts is the one asserting the harness calls a moderate effect
INCONCLUSIVE rather than a win.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ensemble  # noqa: E402
from ensemble_experiment import (  # noqa: E402
    baseline_trials_needed,
    ensemble_rate_empirical,
    fisher_exact_greater,
    min_trials_for_significance,
    wilson,
)


# ── The statistics ───────────────────────────────────────────────────

def test_fisher_reproduces_the_published_chart_vs_snake_result():
    """docs/showcase-ceiling.md publishes p = 0.0004 for 10/10 against 2/10."""
    assert fisher_exact_greater(10, 0, 2, 8) == pytest.approx(0.0004, abs=0.0001)


def test_fisher_does_not_call_an_identical_table_significant():
    assert fisher_exact_greater(2, 8, 2, 8) > 0.5


def test_fisher_is_one_sided_in_the_right_direction():
    """A worse result must not come out significant."""
    assert fisher_exact_greater(0, 10, 2, 8) > 0.9


def test_a_moderate_effect_is_not_significant_against_a_ten_run_baseline():
    """5/10 against 2/10 looks like a big win and is not one. This is the
    whole reason the harness exists."""
    assert fisher_exact_greater(5, 5, 2, 8) > 0.05


def test_more_trials_cannot_rescue_a_moderate_effect():
    """Fisher is limited by the smaller sample, and the baseline is 10 runs.

    If this ever starts returning a number, the baseline was expanded and the
    docstrings that say otherwise need updating.
    """
    assert min_trials_for_significance(2, 10, 0.5) is None
    # …but a large effect resolves quickly.
    assert min_trials_for_significance(2, 10, 0.85) <= 8


def test_expanding_both_arms_does_rescue_it():
    n = baseline_trials_needed(0.5, 0.2)
    assert n is not None and 10 <= n <= 40


def test_wilson_behaves_at_the_extremes():
    lo, hi = wilson(10, 10)
    assert 0.6 < lo < 1.0 and hi == 1.0
    lo, hi = wilson(0, 12)
    assert lo == 0.0 and 0.0 < hi < 0.5


def test_resampled_ensemble_rate_matches_the_closed_form():
    outcomes = [True] * 5 + [False] * 5   # p = 0.5
    assert ensemble_rate_empirical(outcomes, 3) == pytest.approx(1 - 0.5**3, abs=0.02)


def test_ensemble_of_n_cannot_exceed_certainty_or_beat_all_failures():
    assert ensemble_rate_empirical([False] * 8, 5) == 0.0
    assert ensemble_rate_empirical([True] * 8, 1) == 1.0


# ── Selection ────────────────────────────────────────────────────────

def _cand(idx, files=(), problems=(), raw="x"):
    r = ensemble.CandidateResult(index=idx, raw_output=raw)
    r.files = list(files)
    r.problems = list(problems)
    return r


def test_rank_prefers_a_browser_pass_over_a_merely_parsing_candidate(tmp_path):
    good = tmp_path / "good.html"
    good.write_text("<html></html>", encoding="utf-8")
    big = tmp_path / "big.html"
    big.write_text("<html>" + "x" * 5000 + "</html>", encoding="utf-8")

    parses_and_bigger = _cand(0, files=[str(big)])
    passes_browser = _cand(1, files=[str(good)])

    ordered = ensemble.rank([parses_and_bigger, passes_browser], browser_ok={1: True})
    assert ordered[0].index == 1, "a browser pass must outrank a bigger file"


def test_rank_puts_a_candidate_that_produced_nothing_last(tmp_path):
    f = tmp_path / "a.html"
    f.write_text("<html></html>", encoding="utf-8")
    ordered = ensemble.rank([_cand(0, problems=["nothing extracted"]), _cand(1, files=[str(f)])])
    assert ordered[-1].index == 0


def test_rank_breaks_ties_by_stable_id_not_output_length(tmp_path):
    small, large = tmp_path / "s.html", tmp_path / "l.html"
    small.write_text("<html>tiny</html>", encoding="utf-8")
    large.write_text("<html>" + "y" * 3000 + "</html>", encoding="utf-8")
    ordered = ensemble.rank([_cand(0, files=[str(small)]), _cand(1, files=[str(large)])])
    assert ordered[0].index == 0


def test_selection_uses_no_knowledge_of_the_right_answer():
    """rank() must depend only on things a coordinator can observe.

    If this signature grows a parameter carrying the expected output, the
    experiment stops measuring anything deployable.
    """
    import inspect

    params = set(inspect.signature(ensemble.rank).parameters)
    assert params == {"results", "browser_ok"}


# ── The candidate prompt ─────────────────────────────────────────────

def test_ensemble_reuses_the_active_builder_prompt():
    """Same prompt as decomposition, so a comparison isolates architecture."""
    import orchestrator

    assert ensemble._system_prompt().startswith(orchestrator.BUILDER_SYSTEM)


def test_ensemble_prompt_tells_the_model_it_is_alone():
    text = ensemble._system_prompt()
    assert "ENTIRE DELIVERABLE ALONE" in text
    assert "no other agents" in text.lower()


def test_run_ensemble_is_sequential_by_default():
    """Two concurrent model calls on an 8 GB CPU box thrash rather than parallelise."""
    import inspect

    assert inspect.signature(ensemble.run_ensemble).parameters["concurrent"].default is False


@pytest.mark.asyncio
async def test_experiment_wrapper_uses_bounded_production_executions(tmp_path, monkeypatch):
    import execution.service as service_module
    import execution.strategies as strategies
    from execution.persistence import ExecutionStore

    async def generated(*args, **kwargs):
        return "```html\n<!doctype html><title>complete</title>\n```"

    monkeypatch.setattr(strategies, "generate", generated)
    monkeypatch.setattr(strategies.EnsembleStrategy, "artifact_root", tmp_path / "production")
    service = service_module.ExecutionService(store=ExecutionStore(tmp_path / "executions.db"))
    service._emit = lambda *args, **kwargs: None
    monkeypatch.setattr(service_module, "_SERVICE", service)

    output_dir = tmp_path / "experiment"
    results = await ensemble.run_ensemble("build one HTML file", 6, output_dir)

    assert len(results) == 6
    assert all((output_dir / f"candidate_{index}" / "candidate.md").is_file() for index in range(1, 7))
    # Six trials require two protocol-v1 executions because one execution is
    # intentionally bounded to five candidates.
    assert len({path.parent.parent.name for path in (tmp_path / "production").glob("*/candidate_*/candidate.md")}) == 2
