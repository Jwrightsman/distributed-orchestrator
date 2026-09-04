"""The statistics the eval instrument reports with.

Every expected value here is a worked example that can be checked by hand or
against a published figure from this repository, not a value copied out of the
implementation. Three things are pinned:

1. **The exact tests agree with their textbook values**, including the cases
   where an exact test and the normal approximation give different verdicts.
   That divergence is not academic: at the sample sizes this project runs, the
   chi-square approximation rejects nulls the exact test does not.
2. **Power is calibrated.** A power function that is not calibrated is worse
   than none, because it produces a confident corpus size. The check is that at
   a true difference of zero the rejection probability is at most alpha — that
   is the definition of the test's size, and it is what makes the rest of the
   curve mean anything.
3. **A p-value never travels alone.** `render_paired` and `render_unpaired`
   always carry the contingency table and n.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import stats  # noqa: E402


# -- McNemar, exact ---------------------------------------------------------

def test_mcnemar_matches_the_projects_best_change():
    """v1 -> v3: 9 improved, 2 regressed. p = 0.0327 one-sided, 0.0654 two-sided."""
    assert stats.mcnemar_exact_p(2, 9, one_sided=True) == pytest.approx(0.0327, abs=1e-4)
    assert stats.mcnemar_exact_p(2, 9) == pytest.approx(0.0654, abs=1e-4)


def test_mcnemar_worked_by_hand():
    """b=0, c=5 is a clean sweep of five: one-sided p = 1/32."""
    assert stats.mcnemar_exact_p(0, 5, one_sided=True) == pytest.approx(1 / 32)
    assert stats.mcnemar_exact_p(0, 5) == pytest.approx(2 / 32)


def test_no_discordant_pairs_is_p_one():
    assert stats.mcnemar_exact_p(0, 0) == 1.0
    assert stats.mcnemar_exact_p(0, 0, one_sided=True) == 1.0


def test_exact_and_normal_approximation_disagree_at_four_pairs():
    """The boundary case that justifies computing this exactly.

    With four discordant pairs all falling one way, the uncorrected McNemar
    chi-square is (0-4)^2/4 = 4.0, which is p = 0.046 two-sided and would be
    called significant at alpha = 0.05. The exact test says 2/16 = 0.125.

    A category in this eval set has four to six prompts, so this is not a
    corner case here, it is the common case — and the approximation would have
    handed out significance to a coin flip.
    """
    exact_two_sided = stats.mcnemar_exact_p(0, 4)
    assert exact_two_sided == pytest.approx(0.125)

    chi_square = (0 - 4) ** 2 / 4
    assert chi_square == pytest.approx(4.0)
    normal_two_sided = 0.0455  # chi-square, 1 df, at 4.0
    assert normal_two_sided < 0.05 < exact_two_sided

    # And the power statement agrees: four pairs cannot clear alpha in any split.
    assert stats.min_detectable(4) is None


def test_mcnemar_survives_counts_far_past_float_range():
    """2**n overflows a float at n > 1030; the ratio is still a probability."""
    p = stats.mcnemar_exact_p(600, 700, one_sided=True)
    assert 0.0 < p < 1.0
    assert stats.mcnemar_exact_p(0, 2000, one_sided=True) == pytest.approx(0.0, abs=1e-12)


def test_two_sided_is_exactly_double_one_sided_at_the_tail():
    for b, c in [(2, 9), (0, 5), (3, 7), (1, 4)]:
        assert stats.mcnemar_exact_p(b, c) == pytest.approx(
            2 * stats.mcnemar_exact_p(b, c, one_sided=True), rel=1e-9
        )


def test_negative_counts_are_rejected():
    with pytest.raises(ValueError):
        stats.mcnemar_exact_p(-1, 3)


# -- Fisher, exact ----------------------------------------------------------

def test_fisher_reproduces_the_published_showcase_result():
    """Chart 10/10 against Snake 2/10, published as p = 0.00036."""
    assert stats.fisher_exact_greater(10, 0, 2, 8) == pytest.approx(0.00036, abs=1e-5)


def test_fisher_matches_the_tea_tasting_textbook_values():
    """Fisher's own example. [[3,1],[1,3]] is 17/70; [[4,0],[0,4]] is 1/70."""
    assert stats.fisher_exact_greater(3, 1, 1, 3) == pytest.approx(17 / 70)
    assert stats.fisher_exact_greater(4, 0, 0, 4) == pytest.approx(1 / 70)


def test_fisher_reproduces_the_inconclusive_ensemble_result():
    """12/22 ensemble against 2/10 decomposition: p = 0.073, published as such."""
    assert stats.fisher_exact_greater(12, 10, 2, 8) == pytest.approx(0.073, abs=1e-3)


# -- interval estimates -----------------------------------------------------

def test_wilson_reproduces_the_published_headline_interval():
    """The README's 32/56 = 57%, 95% CI 44-69%."""
    lo, hi = stats.wilson(32, 56)
    assert (round(lo * 100), round(hi * 100)) == (44, 69)


def test_wilson_behaves_at_the_boundaries():
    assert stats.wilson(0, 10)[0] == 0.0
    assert stats.wilson(10, 10)[1] == 1.0
    assert stats.wilson(0, 0) == (0.0, 1.0)


def test_clopper_pearson_reproduces_the_published_ten_of_ten_bound():
    """docs/showcase-ceiling.md: ten for ten puts the true rate at >= 74%.

    That is the one-sided 95% bound, which is the two-sided 90% interval's
    lower end — 0.05 ** (1/10) exactly.
    """
    lower = stats.clopper_pearson(10, 10, alpha=0.10)[0]
    assert lower == pytest.approx(0.05 ** (1 / 10), abs=1e-6)
    assert round(lower * 100) == 74


def test_clopper_pearson_is_wider_than_wilson_at_small_n():
    cp_lo, cp_hi = stats.clopper_pearson(2, 10)
    w_lo, w_hi = stats.wilson(2, 10)
    assert cp_lo <= w_lo and cp_hi >= w_hi


# -- power ------------------------------------------------------------------

def test_power_at_zero_effect_is_at_most_alpha():
    """The calibration check: with no true difference, the test's own size.

    A discrete exact test is conservative, so this is <= alpha rather than
    == alpha. If this ever exceeds alpha the power curve is wrong and every
    corpus size derived from it is wrong with it.
    """
    for n in (20, 28, 60, 100):
        assert stats.mcnemar_power(n, 0.64, 0.0, alpha=0.05) <= 0.05


def test_power_increases_with_corpus_size():
    previous = -1.0
    for n in (20, 40, 80, 160):
        power = stats.mcnemar_power(n, 0.64, 0.20)
        assert power > previous
        previous = power


def test_power_increases_with_effect_size():
    previous = -1.0
    for delta in (0.05, 0.10, 0.20, 0.40):
        power = stats.mcnemar_power(100, 0.64, delta)
        assert power > previous
        previous = power


def test_an_effect_larger_than_the_discordant_rate_is_impossible():
    """A paired test cannot show a difference bigger than the fraction that move.

    Rejected loudly rather than returning a number, because a delta above the
    discordant rate is a modelling error and returning 1.0 for it would let a
    caller compute a corpus size for a difference the design cannot express.
    """
    with pytest.raises(ValueError):
        stats.mcnemar_power(100, 0.30, 0.40)


def test_minimum_detectable_effect_at_the_current_corpus():
    """n=28 at the measured noise floor: about 38 points, not six prompts.

    ROADMAP section 4 said the instrument could not see anything smaller than
    about six prompts. Six of 28 is 21 percentage points. The computed figure
    at 80% power is 38 points, which is 10.8 prompts — the rule of thumb was
    optimistic by roughly a factor of two, and this is where that is pinned.
    """
    mde = stats.min_detectable_effect(28, 18 / 28)
    assert mde == pytest.approx(0.384, abs=0.005)
    assert mde * 28 == pytest.approx(10.8, abs=0.2)


def test_required_n_for_a_fifteen_point_effect():
    assert stats.required_n(0.15, 18 / 28) == 187


def test_required_n_is_none_when_no_reachable_size_works():
    """A difference larger than the discordant rate is never resolvable."""
    assert stats.required_n(0.80, 0.30) is None


def test_min_detectable_effect_is_none_when_power_is_unreachable():
    assert stats.min_detectable_effect(4, 0.64) is None


def test_power_curve_reports_every_requested_size():
    rows = stats.power_curve([10, 28, 100], 0.64, 0.15)
    assert [row["n"] for row in rows] == [10, 28, 100]
    assert all("mde" in row and "power_at_delta" in row for row in rows)


# -- discordance ------------------------------------------------------------

def test_discordance_reports_its_own_n_and_interval():
    a = {"x": True, "y": False, "z": True, "w": False}
    b = {"x": False, "y": True, "z": True, "w": False}
    result = stats.discordance(a, b)
    assert result["n"] == 4
    assert result["discordant"] == 2
    assert result["up"] == 1 and result["down"] == 1
    assert result["rate"] == 0.5
    assert result["ci95"][0] < 0.5 < result["ci95"][1]


def test_discordance_refuses_runs_with_nothing_in_common():
    with pytest.raises(ValueError):
        stats.discordance({"a": True}, {"b": True})


# -- reporting --------------------------------------------------------------

def test_paired_result_carries_the_whole_table():
    a = {"1": True, "2": True, "3": False, "4": False, "5": True}
    b = {"1": True, "2": False, "3": True, "4": True, "5": True}
    result = stats.paired_test(a, b)
    assert result.n == 5
    assert (result.both_pass, result.a_only, result.b_only, result.both_fail) == (2, 1, 2, 0)
    assert result.both_pass + result.a_only + result.b_only + result.both_fail == result.n
    assert result.discordant == 3
    assert result.net == 1


def test_rendered_paired_result_never_shows_a_bare_p_value():
    result = stats.paired_test(
        {"a": True, "b": False, "c": False}, {"a": False, "b": True, "c": True}
    )
    text = stats.render_paired(result)
    assert "p = " in text
    assert "n = 3 paired items" in text
    assert "discordant" in text
    # The four cells of the table are all present.
    assert text.count("pass") >= 2 and text.count("fail") >= 2


def test_rendered_unpaired_result_carries_both_arms_and_intervals():
    result = stats.unpaired_test(2, 10, 12, 22)
    text = stats.render_unpaired(result, "decomposition", "ensemble")
    assert "2/10" in text and "12/22" in text
    assert "95% CI" in text
    assert "Fisher exact one-sided p" in text
    assert result.p_one_sided == pytest.approx(0.073, abs=1e-3)


def test_unpaired_test_refuses_an_empty_arm():
    with pytest.raises(ValueError):
        stats.unpaired_test(0, 0, 5, 10)


def test_paired_test_refuses_runs_with_nothing_in_common():
    with pytest.raises(ValueError):
        stats.paired_test({"a": True}, {"b": False})
