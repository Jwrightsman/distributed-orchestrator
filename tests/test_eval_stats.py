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

import random
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


# -- the power curve as a function of psi ------------------------------------

# The table published in docs/eval-methodology.md section 7, at psi = 18/28.
# Copied from the document, not from the implementation: the point of the
# parameterisation is that it is a generalisation of what PR #71 published, and
# a generalisation that moved the published numbers would be a rewrite.
PUBLISHED_CURVE = [
    # n,   detectable effect, = items, power at 15 points
    (28, 0.384, 10.8, 0.19),
    (40, 0.325, 13.0, 0.26),
    (50, 0.290, 14.5, 0.32),
    (60, 0.265, 15.9, 0.37),
    (80, 0.230, 18.4, 0.46),
    (100, 0.206, 20.6, 0.55),
    (120, 0.188, 22.6, 0.62),
    (160, 0.162, 26.0, 0.74),
    (200, 0.145, 29.0, 0.82),
    (300, 0.118, 35.4, 0.94),
]


def test_the_measured_rate_is_the_one_that_was_measured():
    """18 of 28. Nothing here may quietly become a different measured floor."""
    assert stats.MEASURED_DISCORDANT_RATE == 18 / 28
    assert round(stats.MEASURED_DISCORDANT_RATE, 3) == 0.643


def test_the_psi_grid_reproduces_the_published_table_at_the_measured_rate():
    """Every published cell, to the precision it was published at."""
    assert PUBLISHED_CURVE, "the published table is empty — this test read nothing"
    cells = stats.psi_grid(
        [row[0] for row in PUBLISHED_CURVE], [stats.MEASURED_DISCORDANT_RATE], 0.15
    )
    assert len(cells) == len(PUBLISHED_CURVE)
    for cell, (n, mde, items, power) in zip(cells, PUBLISHED_CURVE):
        assert cell.n == n
        assert cell.projected is False
        assert cell.mde == pytest.approx(mde, abs=5e-4)
        assert cell.mde_items == pytest.approx(items, abs=0.05)
        assert cell.power_at_delta == pytest.approx(power, abs=5e-3)


def test_power_curve_and_psi_grid_agree_at_the_measured_rate():
    """The grid is the old curve with one more axis, not a second implementation."""
    sizes = [28, 100, 300]
    old = stats.power_curve(sizes, stats.MEASURED_DISCORDANT_RATE, 0.15)
    new = stats.psi_grid(sizes, [stats.MEASURED_DISCORDANT_RATE], 0.15)
    assert old and new
    for row, cell in zip(old, new):
        assert row["n"] == cell.n
        assert row["mde"] == pytest.approx(cell.mde)
        assert row["power_at_delta"] == pytest.approx(cell.power_at_delta)


def test_a_lower_floor_reaches_the_fifteen_point_target_the_measured_one_cannot():
    """The whole reason the curve is parameterised.

    At the measured floor a 15-point change needs 187 items. Halving psi is
    worth what doubling n is worth, so at psi = 0.32 the corpus this project
    already has becomes sufficient. That is a projection and the grid says so.
    """
    measured = stats.psi_grid([100], [stats.MEASURED_DISCORDANT_RATE], 0.15)[0]
    halved = stats.psi_grid([100], [0.32], 0.15)[0]
    assert measured.target_reachable is False
    assert halved.target_reachable is True
    assert measured.projected is False
    assert halved.projected is True
    assert measured.required_n == 187
    assert halved.required_n == 95


def test_required_n_scales_with_the_floor_as_the_published_model_says():
    """delta proportional to sqrt(psi/n) means required n is linear in psi."""
    base = stats.required_n(0.15, stats.MEASURED_DISCORDANT_RATE)
    for rate in (0.5, 0.4, 0.32, 0.25):
        predicted = base * rate / stats.MEASURED_DISCORDANT_RATE
        actual = stats.required_n(0.15, rate)
        assert actual == pytest.approx(predicted, rel=0.06), (rate, actual, predicted)


def test_a_floor_below_the_target_never_reports_the_target_as_reachable():
    """psi = 0.10 cannot resolve a 15-point effect at any n, ever.

    A paired design cannot show a marginal difference larger than the fraction
    of items that change outcome at all. The minimum detectable effect is
    capped at psi, so a naive `mde <= delta` test would call every one of these
    cells reachable — the answer that looks best and is impossible.
    """
    cells = stats.psi_grid([28, 100, 300], [0.10], 0.15)
    assert cells, "no cells — this test asserted nothing"
    assert all(cell.projected for cell in cells)
    assert not any(cell.target_reachable for cell in cells)
    assert all(cell.required_n is None for cell in cells)
    # And the rendering says so rather than printing a number. Power at a delta
    # the design cannot express is "n/a", not 1.00, and no cell is bracketed.
    text = stats.render_psi_grid(cells, 0.15)
    assert "n/a" in text
    assert "larger than the floor itself" in text
    rows = [ln for ln in text.splitlines() if ln.strip()[:3].strip().isdigit()]
    assert rows, "no data rows were rendered — this test asserted nothing"
    assert not any("[" in row for row in rows)


def test_a_projected_cell_cannot_be_printed_without_its_marker():
    with pytest.raises(ValueError, match="projection marker"):
        stats.format_psi_cell("20.6%", projected=True, marked=False)
    # A measured cell is unmarked, and may be printed either way.
    assert stats.format_psi_cell("20.6%", projected=False) == "20.6%"
    assert stats.format_psi_cell("20.6%", projected=True) == "20.6%*"


def test_every_projected_cell_in_the_rendered_grid_carries_the_marker():
    rates = [stats.MEASURED_DISCORDANT_RATE, 0.4, 0.25]
    cells = stats.psi_grid([28, 100], rates, 0.15)
    text = stats.render_psi_grid(cells, 0.15, per_item_minutes=29.3, corpus_n=100)
    assert text.strip(), "nothing was rendered — this test asserted nothing"
    assert "PROJECTION" in text
    # Exactly the projected cells are starred: two projected rates over two
    # sizes is four cells, plus their two column headers and two legend lines.
    projected = [c for c in cells if c.projected]
    assert len(projected) == 4
    assert text.count(stats.PROJECTION_MARKER) == len(projected) + 2 * len(rates[1:]) + 1
    assert "the corpus this project has" in text


def test_the_rendered_grid_marks_where_the_target_becomes_reachable():
    cells = stats.psi_grid([100], [stats.MEASURED_DISCORDANT_RATE, 0.25], 0.15)
    text = stats.render_psi_grid(cells, 0.15)
    # Brackets mark reachability, and only the projected column has any.
    assert "[" in text
    measured_line = [ln for ln in text.splitlines() if ln.strip().startswith("100")][0]
    assert measured_line.index("[") > measured_line.index("20.6%")


def test_the_grid_refuses_to_render_nothing():
    with pytest.raises(ValueError):
        stats.render_psi_grid([], 0.15)
    with pytest.raises(ValueError):
        stats.psi_grid([], [0.5], 0.15)
    with pytest.raises(ValueError):
        stats.psi_grid([28], [], 0.15)


# -- Wilcoxon signed-rank, for a replicated continuous endpoint ---------------

def _brute_force_signed_rank_p(differences):
    """One-sided p by enumerating every sign assignment. Independent of stats.py.

    Only usable for small vectors — which is the point: the worked examples are
    checked against a definition, not against the implementation under test.
    """
    nonzero = [d for d in differences if d != 0.0]
    ranks = stats._average_ranks([abs(d) for d in nonzero])
    observed = sum(r for d, r in zip(nonzero, ranks) if d > 0)
    m = len(nonzero)
    hits = 0
    for mask in range(2**m):
        total = sum(ranks[i] for i in range(m) if mask >> i & 1)
        if total >= observed - 1e-12:
            hits += 1
    return hits / 2**m


def test_signed_rank_worked_by_hand_clean_sweep():
    """Five positive differences, no ties: W+ = 15 and p = 1/32."""
    result = stats.wilcoxon_signed_rank([1, 2, 3, 4, 5])
    assert result.n_nonzero == 5 and result.zeros == 0
    assert result.w_plus == 15 and result.w_minus == 0
    assert result.p_one_sided == pytest.approx(1 / 32)
    assert result.p_two_sided == pytest.approx(2 / 32)
    assert result.exact is True


def test_signed_rank_worked_by_hand_with_ties():
    """|d| = .2 .2 .2 .4 .6 gives averaged ranks 2 2 2 4 5.

    With one of the .2 differences negative, W+ = 13 and W- = 2. Four of the
    32 sign assignments reach 13 or more — the whole set, and the three ways to
    drop a single rank-2 — so p = 4/32 = 0.125.
    """
    diffs = [0.2, 0.2, -0.2, 0.4, 0.6]
    result = stats.wilcoxon_signed_rank(diffs)
    assert result.w_plus == 13 and result.w_minus == 2
    assert result.p_one_sided == pytest.approx(4 / 32)
    assert result.p_one_sided == pytest.approx(_brute_force_signed_rank_p(diffs))


def test_signed_rank_drops_zero_differences_and_counts_them():
    """Wilcoxon's own treatment, and the count is reported rather than hidden.

    A replicate study over a banded corpus produces many exact zeros — items
    both arms pass every time, and items both arms fail every time. How many
    is the same information the McNemar table's concordant cells carry.
    """
    result = stats.wilcoxon_signed_rank([0.0, 1.0, 2.0, 3.0, 0.0])
    assert result.n == 5 and result.n_nonzero == 3 and result.zeros == 2
    assert result.w_plus == 6
    assert result.p_one_sided == pytest.approx(1 / 8)


def test_signed_rank_with_no_movement_at_all_is_not_a_null_result():
    result = stats.wilcoxon_signed_rank([0.0, 0.0, 0.0])
    assert result.n_nonzero == 0
    assert result.p_one_sided == 1.0
    assert "no evidence" in stats.render_wilcoxon(result)


def test_signed_rank_at_the_smallest_possible_n():
    """One non-zero difference can never clear alpha: p = 1/2 at best."""
    result = stats.wilcoxon_signed_rank([0.4])
    assert result.n_nonzero == 1
    assert result.p_one_sided == pytest.approx(0.5)
    assert result.significant_one_sided is False


def test_signed_rank_matches_brute_force_across_small_vectors():
    vectors = [
        [1.0, -1.0, 2.0],
        [0.2, 0.4, 0.4, -0.6, 0.8],
        [-0.2, -0.2, 0.2, 0.2, 0.2, 0.6],
        [0.0, 0.2, -0.2, 0.4],
        [1, 1, 1, 1, 1, -1, -1],
    ]
    assert vectors, "no vectors to check — this test read nothing"
    for vector in vectors:
        assert stats.wilcoxon_signed_rank(vector).p_one_sided == pytest.approx(
            _brute_force_signed_rank_p(vector)
        ), vector


def test_a_single_magnitude_makes_the_signed_rank_test_a_sign_test():
    """The binary endpoint's shape, computed exactly rather than approximated.

    Every |difference| equal means every rank is the same averaged value, so W+
    is that rank times the number of positives. The normal approximation is bad
    here — the statistic moves in steps of (m+1)/2, not 1 — so the exact sign
    test is used at any size.
    """
    diffs = [1.0] * 18 + [-1.0] * 12
    result = stats.wilcoxon_signed_rank(diffs)
    assert result.exact is True
    assert result.n_nonzero == 30
    assert result.p_one_sided == pytest.approx(
        stats.mcnemar_exact_p(12, 18, one_sided=True)
    )


def test_signed_rank_is_symmetric_under_negation():
    positive = stats.wilcoxon_signed_rank([0.2, 0.4, -0.2, 0.6])
    negative = stats.wilcoxon_signed_rank([-0.2, -0.4, 0.2, -0.6])
    assert positive.w_plus == negative.w_minus
    assert positive.p_two_sided == pytest.approx(negative.p_two_sided)


def test_the_approximation_does_not_converge_and_the_code_says_so():
    """Measures the approximation rather than assuming it gets better.

    Randomly drawn heavily-tied difference vectors, exact against the
    tie-corrected normal approximation. The disagreement falls from about 0.018
    at m = 20 to about 0.009 at m = 30 and then **stops falling**: 0.008 at both
    m = 60 and m = 100. With few distinct magnitudes the statistic moves in
    steps far larger than the half-unit continuity correction assumes, and more
    data does not fix that.

    This is the justification for two decisions at once. The threshold sits at
    60 so a study on the 36-item confirmatory set is exact. And a result that
    used the approximation says so in its own rendering, with the residual
    stated, because a plateau at 0.008 is not something to discover next to
    alpha = 0.05.
    """
    rng = random.Random(20260904)
    worst = {}
    for m in (20, 30, 60, 100):
        largest = 0.0
        for _ in range(25):
            vector = [rng.choice([-3, -2, -1, 1, 2, 3]) * 0.2 for _ in range(m)]
            exact = stats.wilcoxon_signed_rank(vector, exact=True)
            approximate = stats.wilcoxon_signed_rank(vector, exact=False)
            assert exact.exact is True and approximate.exact is False
            largest = max(largest, abs(exact.p_one_sided - approximate.p_one_sided))
        worst[m] = largest
    assert worst, "no vectors were compared — this test asserted nothing"
    assert worst[20] > 0.012, "the approximation is fine at m=20 — lower the threshold"
    assert worst[30] < 0.012
    # The plateau. If this ever drops the comment above is wrong, not the test.
    assert 0.004 < worst[60] < 0.012
    assert 0.004 < worst[100] < 0.012
    assert stats.MAX_EXACT_SIGNED_RANK == 60


def test_an_approximate_result_says_it_is_approximate():
    vector = [0.2 * (1 if i % 3 else -1) * (1 + i % 4) for i in range(80)]
    approximate = stats.wilcoxon_signed_rank(vector)
    assert approximate.exact is False
    text = stats.render_wilcoxon(approximate)
    assert "normal approximation" in text
    assert "0.01" in text
    # And exact is always available for a borderline result.
    forced = stats.wilcoxon_signed_rank(vector, exact=True)
    assert forced.exact is True


def test_rendered_signed_rank_never_shows_a_bare_p_value():
    result = stats.wilcoxon_signed_rank([0.2, 0.4, -0.2, 0.6, 0.0])
    text = stats.render_wilcoxon(result, "decomposition", "ensemble")
    assert "p = " in text
    assert "n = 5 paired items" in text
    assert "non-zero" in text and "exactly zero" in text
    assert "W+" in text and "W-" in text


def test_signed_rank_refuses_an_empty_input():
    with pytest.raises(ValueError):
        stats.wilcoxon_signed_rank([])


# -- the replicate design's simulated power ----------------------------------

def test_replicate_power_is_calibrated_at_no_effect():
    """With no true difference the rejection rate is the test's own size.

    The same calibration check as `mcnemar_power`, and for the same reason: an
    uncalibrated power number produces a confident study design. Monte Carlo
    slack is allowed for, since 600 trials at alpha = 0.05 has a standard error
    of about 0.009.
    """
    rate = stats.replicate_power(30, 5, 0.0, [0.2, 0.4, 0.6, 0.8], trials=600)
    assert rate <= 0.08


def test_replicate_power_increases_with_effect_and_with_runs():
    rates = [0.2, 0.4, 0.4, 0.6, 0.6, 0.8]
    small = stats.replicate_power(30, 3, 0.10, rates, trials=400)
    large = stats.replicate_power(30, 3, 0.30, rates, trials=400)
    assert large > small
    more_runs = stats.replicate_power(60, 3, 0.10, rates, trials=400)
    assert more_runs > small


def test_replicate_power_is_reproducible_at_a_fixed_seed():
    rates = [0.2, 0.4, 0.6, 0.8]
    first = stats.replicate_power(20, 5, 0.2, rates, trials=200, seed=7)
    second = stats.replicate_power(20, 5, 0.2, rates, trials=200, seed=7)
    assert first == second


def test_replicate_power_refuses_a_design_it_cannot_simulate():
    with pytest.raises(ValueError):
        stats.replicate_power(0, 5, 0.15, [0.5])
    with pytest.raises(ValueError):
        stats.replicate_power(10, 0, 0.15, [0.5])
    with pytest.raises(ValueError):
        stats.replicate_power(10, 5, 0.15, [])
    with pytest.raises(ValueError):
        stats.replicate_power(10, 5, 0.15, [0.5], trials=0)
