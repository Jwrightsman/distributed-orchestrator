"""The controls, in CI, on a fixture corpus with a stubbed model.

No Ollama, no network, no live inference. These are the tests that decide
whether anything else in `evals/` is worth reading:

* a deliberately degraded arm must come out worse (positive control),
* two runs of the identical configuration must not (negative control),
* and neither result counts unless the corpus was non-empty, every item was
  graded, nothing was skipped, and the grader returned both outcomes.

The non-vacuity tests here are not padding. Four test files in this repository
have passed on zero inputs, and a property campaign passed for a week while
three of its rules were structurally unreachable.
"""

import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))
import controls  # noqa: E402
import corpus as corpus_mod  # noqa: E402

SEED = 20260903


@pytest.fixture(scope="module")
def items():
    loaded = controls.load_fixture_corpus()
    assert loaded, "the fixture corpus is empty — every control below is vacuous"
    return loaded


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    return tmp_path_factory.mktemp("controls")


@pytest.fixture(scope="module")
def truncated(items, workdir):
    return controls.positive_control(items, "truncated", workdir, SEED)


@pytest.fixture(scope="module")
def shuffled(items, workdir):
    return controls.positive_control(items, "shuffled", workdir, SEED)


@pytest.fixture(scope="module")
def negative(items, workdir):
    return controls.negative_control(items, workdir, SEED, SEED + 1)


# -- positive controls ------------------------------------------------------

def test_the_truncated_arm_is_detected_as_worse(truncated):
    """An instrument that cannot see a broken arm cannot see a prompt change."""
    assert truncated["detected"], truncated["report"]
    assert truncated["degraded_passes"] < truncated["baseline_passes"]
    assert truncated["result"].p_one_sided <= 0.05


def test_the_shuffled_arm_is_detected_as_worse(shuffled):
    """Valid output for the wrong question is still wrong."""
    assert shuffled["detected"], shuffled["report"]
    assert shuffled["degraded_passes"] < shuffled["baseline_passes"]
    assert shuffled["result"].p_one_sided <= 0.05


def test_the_shuffled_arm_is_invisible_to_parse_and_run(shuffled):
    """The measurement that justifies output-level grading.

    Every shuffled artifact parses and executes cleanly, so an instrument
    checking only those two things sees no discordant pairs at all and reports
    that nothing changed. This is the HTML `browser_ok` situation in miniature:
    `web-snake` passed the old check 5 times out of 5 while the behavioural
    checker measured the same artifact at 2 in 10.
    """
    assert shuffled["weak_result"].discordant == 0
    assert not shuffled["weak_detected"]


def test_the_positive_controls_report_their_contingency_tables(truncated, shuffled):
    for outcome in (truncated, shuffled):
        report = outcome["report"]
        assert "paired items" in report
        assert "discordant" in report
        assert "p = " in report


def test_the_degraded_arms_are_paired_with_the_baseline_draw(items, workdir):
    """The pairing has to be real or the test is not a paired test.

    Both degraded arms are transformations of the *same* baseline draw, so an
    item the default arm would have failed anyway is never counted as a
    degradation. The check: no item improves under degradation.
    """
    assert items
    baseline = controls.run_arm(items, "default", SEED, workdir)
    for arm in ("truncated", "shuffled"):
        degraded = controls.run_arm(items, arm, SEED, workdir)
        improved = [
            item_id for item_id in baseline.outcomes
            if degraded.outcomes[item_id] and not baseline.outcomes[item_id]
        ]
        assert improved == [], f"{arm} improved on {improved}, so the arms are not paired"


# -- negative control -------------------------------------------------------

def test_two_identical_configurations_do_not_differ(negative):
    assert not negative["differs"], negative["report"]
    assert negative["result"].p_two_sided > 0.05


def test_the_negative_control_uses_two_different_draws(items, workdir):
    """Replaying one seed would pass trivially and prove nothing.

    The two runs must actually disagree somewhere, or the control is testing a
    byte-for-byte replay rather than the noise it is supposed to characterise.
    """
    assert items
    first = controls.run_arm(items, "default", SEED, workdir)
    second = controls.run_arm(items, "default", SEED + 1, workdir)
    assert first.outcomes != second.outcomes


def test_the_false_positive_rate_is_near_alpha(items, workdir):
    """What "the noise model is right" actually claims.

    One non-significant negative control is equally consistent with a correct
    noise model and a lucky seed. The rate over many identical-configuration
    pairs is the thing worth checking, and the gate is on the interval rather
    than the point estimate because with a handful of trials the observed rate
    is itself noisy.
    """
    assert items
    pairs = [(SEED + i, SEED + i + 1) for i in range(8)]
    rate = controls.negative_control_false_positive_rate(items, workdir, pairs)
    assert rate["trials"] == 8
    assert len(rate["p_values"]) == 8
    assert rate["ci95"][0] <= rate["alpha"], (
        f"identical configurations came out significant {rate['rate']:.0%} of the time, "
        f"with the whole interval above alpha — the noise model is wrong"
    )


def test_the_false_positive_rate_refuses_zero_trials(items, workdir):
    assert items
    with pytest.raises(controls.VacuousControl):
        controls.negative_control_false_positive_rate(items, workdir, [])


# -- non-vacuity ------------------------------------------------------------

def test_a_control_on_an_empty_corpus_raises_rather_than_passing(workdir):
    with pytest.raises(controls.VacuousControl):
        controls.run_arm([], "default", SEED, workdir)
    with pytest.raises(controls.VacuousControl):
        controls.positive_control([], "truncated", workdir)


def test_the_fixture_corpus_is_large_enough_to_reach_significance():
    """A clean sweep of four discordant pairs cannot clear alpha in any split.

    A fixture corpus small enough that no result could ever be significant
    would make the positive controls unfailable in the wrong direction: they
    would report "not detected" forever, or worse, be quietly relaxed until
    they passed.
    """
    import stats

    items = controls.load_fixture_corpus()
    assert len(items) >= 8
    assert stats.min_detectable(len(items)) is not None


def test_a_skipped_item_is_caught(items):
    assert items
    outcome = controls.ArmOutcome(arm="default", seed=SEED)
    outcome.outcomes = {items[0].id: True}
    outcome.grades = {items[0].id: {"graded": True}}
    with pytest.raises(controls.VacuousControl, match="silently skipped"):
        controls.assert_not_vacuous(items, outcome)


def test_an_ungraded_item_is_caught(items):
    assert items
    outcome = controls.ArmOutcome(arm="default", seed=SEED)
    outcome.outcomes = {item.id: True for item in items}
    outcome.grades = {item.id: {"graded": True} for item in items}
    outcome.grades[items[0].id] = {"graded": False}
    with pytest.raises(controls.VacuousControl, match="ungraded"):
        controls.assert_not_vacuous(items, outcome)


def test_an_arm_that_produced_nothing_is_caught(items):
    assert items
    with pytest.raises(controls.VacuousControl, match="no results"):
        controls.assert_not_vacuous(items, controls.ArmOutcome(arm="default", seed=SEED))


def test_a_grader_stuck_on_one_verdict_is_caught():
    """A grader that always passes and one that always fails both look clean."""
    for verdict in (True, False):
        outcome = controls.ArmOutcome(arm="default", seed=SEED)
        outcome.outcomes = {"a": verdict, "b": verdict, "c": verdict}
        with pytest.raises(controls.VacuousControl, match="never discriminated"):
            controls.assert_grader_discriminates(outcome)


def test_the_real_controls_pass_their_own_non_vacuity_checks(items, workdir):
    assert items
    for arm in controls.ARMS:
        outcome = controls.run_arm(items, arm, SEED, workdir)
        controls.assert_not_vacuous(items, outcome)
        assert len(outcome.outcomes) == len(items)


def test_the_baseline_arm_produces_both_outcomes(items, workdir):
    assert items
    baseline = controls.run_arm(items, "default", SEED, workdir)
    controls.assert_grader_discriminates(baseline)
    assert 0 < baseline.passes < len(items)


# -- the stub itself --------------------------------------------------------

def test_the_stub_is_deterministic(items):
    assert items
    ids = [i.id for i in items]
    first = controls.StubModel("default", SEED).source_for(items[0], ids)
    second = controls.StubModel("default", SEED).source_for(items[0], ids)
    assert first == second


def test_different_seeds_change_at_least_one_artifact(items):
    assert items
    ids = [i.id for i in items]
    a = [controls.StubModel("default", SEED).source_for(i, ids) for i in items]
    b = [controls.StubModel("default", SEED + 1).source_for(i, ids) for i in items]
    assert a != b


def test_the_shuffled_arm_answers_a_different_item(items):
    assert len(items) >= 2
    ids = [i.id for i in items]
    stub = controls.StubModel("shuffled", SEED)
    for item in items:
        source = stub.source_for(item, ids)
        assert item.id not in source, f"{item.id} was answered with its own artifact"


def test_the_pass_rate_spread_keeps_the_fixture_corpus_discriminating(items):
    """Floor and ceiling items carry no information in a paired test."""
    assert items
    rates = [controls.item_pass_rate(i.id) for i in items]
    assert min(rates) >= 0.30 and max(rates) <= 0.80
    discriminating = [r for r in rates if 0.20 < r < 0.80]
    assert len(discriminating) == len(rates)


def test_the_fixture_corpus_is_a_valid_corpus():
    items = corpus_mod.load_corpus(controls.FIXTURE_CORPUS)
    assert items
    assert all(i.split == "development" for i in items), (
        "the control fixtures must never be part of a confirmatory set"
    )
