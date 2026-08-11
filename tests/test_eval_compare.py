"""The statistics behind promote-or-delete.

Every expected value here was computed directly and checked against the four
real eval runs in `evals/results/` before being written down, so these pin
behaviour that is already known to reproduce the project's actual history:
v1 -> v3 promotes, v3 -> v4 and v3 -> v5 delete.

The one-sided/two-sided distinction is the load-bearing part. See
`mcnemar_exact_p`'s docstring: a two-sided gate at 0.05 would have rejected
v3, the best change ever made to this project.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
from compare import compare, mcnemar_exact_p, min_detectable  # noqa: E402


# ── The test that decides promotions ─────────────────────────────────

def test_v1_to_v3_promotes_one_sided_but_not_two_sided():
    """The real numbers from the project's best change: 9 up, 2 down.

    This is the whole argument for using a one-sided test, so it is pinned
    with the actual counts rather than a synthetic example.
    """
    assert mcnemar_exact_p(2, 9, one_sided=True) == pytest.approx(0.0327, abs=1e-4)
    assert mcnemar_exact_p(2, 9) == pytest.approx(0.0654, abs=1e-4)

    assert mcnemar_exact_p(2, 9, one_sided=True) < 0.05   # promoted, correctly
    assert mcnemar_exact_p(2, 9) > 0.05                   # would have been rejected


def test_two_sided_is_exactly_double_one_sided():
    for b, c in [(2, 9), (0, 5), (3, 7), (1, 4)]:
        assert mcnemar_exact_p(b, c) == pytest.approx(
            2 * mcnemar_exact_p(b, c, one_sided=True), abs=1e-12
        )


def test_a_regression_is_never_significant_in_the_promote_direction():
    """v3 -> v4 lost 6 prompts. Nothing about that should look promotable."""
    assert mcnemar_exact_p(10, 4, one_sided=True) > 0.9


def test_a_wash_is_not_significant():
    """v3 -> v5: 7 up, 8 down. Half the set moved and it means nothing."""
    p = mcnemar_exact_p(8, 7, one_sided=True)
    assert p > 0.5
    assert mcnemar_exact_p(8, 7) == pytest.approx(1.0)


def test_no_discordant_pairs_is_p_one():
    assert mcnemar_exact_p(0, 0) == 1.0
    assert mcnemar_exact_p(0, 0, one_sided=True) == 1.0


# ── Power: what this eval set can and cannot see ─────────────────────

def test_min_detectable_matches_the_real_runs():
    assert min_detectable(11) == 9    # v1 -> v3 got exactly 9 — it barely cleared
    assert min_detectable(15) == 12   # v3 -> v5 got 7
    assert min_detectable(5) == 5     # a clean sweep, and only just


def test_a_small_category_can_never_reach_significance():
    """The single most important guard rail in this file.

    A category in this eval set has 4-6 prompts. With only four discordant
    pairs there is no split that clears alpha=0.05 — not even 4-0. So
    "but web_app improved" can never be evidence on its own, which is exactly
    the reasoning that kept v5 alive for a session.
    """
    assert min_detectable(4) is None
    assert min_detectable(3) is None


def test_higher_alpha_needs_fewer_wins():
    assert min_detectable(4, alpha=0.10) == 4
    assert min_detectable(4, alpha=0.05) is None


# ── The comparison itself ────────────────────────────────────────────

def _run(**outcomes):
    return {k: {"success": v, "category": "web_app"} for k, v in outcomes.items()}


def test_compare_counts_direction_not_just_totals():
    a = _run(w=True, x=True, y=False, z=False)
    b = _run(w=False, x=True, y=True, z=True)

    res = compare(a, b)
    assert res["n"] == 4
    assert res["a_success"] == 2
    assert res["b_success"] == 3
    assert res["up"] == ["y", "z"]
    assert res["down"] == ["w"]
    assert res["churn"] == 3
    assert res["net"] == 1


def test_identical_totals_can_still_be_maximum_churn():
    """The failure mode a success-rate diff hides completely."""
    a = _run(w=True, x=True, y=False, z=False)
    b = _run(w=False, x=False, y=True, z=True)

    res = compare(a, b)
    assert res["a_success"] == res["b_success"] == 2
    assert res["net"] == 0
    assert res["churn"] == 4          # every single prompt flipped
    assert res["p_one_sided"] > 0.05


def test_compare_ignores_prompts_missing_from_either_run():
    a = _run(x=True, y=False)
    b = _run(x=True, y=True, extra=True)

    res = compare(a, b)
    assert res["n"] == 2
    assert "extra" not in res["up"]


def test_compare_can_be_restricted_to_a_subset():
    a = _run(x=True, y=False, z=False)
    b = _run(x=False, y=True, z=True)

    res = compare(a, b, ids=["y", "z"])
    assert res["n"] == 2
    assert res["net"] == 2
