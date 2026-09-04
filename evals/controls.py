"""Controls: does this instrument detect anything, and does it invent anything?

Every measurement failure in this repository's recent history has been an
instrument reporting something that was not there, or reporting nothing while
appearing to work. A grep "proved" a working game had no game logic. A 0/7 demo
result was Ollama being down. A property campaign passed while three of its
rules were structurally unreachable. A showcase checker with a 1200 ms blind
spot called four working games broken and nearly published it twice.

So the controls come before the corpus work, and they run in CI on a fixture
corpus with a stubbed model — no Ollama, no network, no live inference.

**Positive control.** A deliberately degraded arm must be detected as worse.
Two are built:

  * `truncated` — the arm's output is cut off at a fraction of its length, so
    the artifact no longer parses. This is the crude failure.
  * `shuffled` — the arm answers a shuffled prompt-to-task pairing: perfectly
    valid output, for the wrong question. This is the failure a syntax check
    cannot see, and it is the one that matters, because an instrument that
    only notices broken syntax cannot notice a prompt change either.

**Negative control.** Two runs of the identical configuration must not produce
a significant difference. Running that once proves very little — at alpha=0.05
one draw in twenty is significant by construction — so it runs over a series of
seeds and checks the *false-positive rate*, which is what "the noise model is
right" actually means.

**Non-vacuity.** Every control asserts its own preconditions: a non-empty
corpus, every item graded, no item silently skipped, and both outcomes seen at
least once from the grader. Test files here have passed on zero inputs four
separate times; assume it will happen again.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import corpus as corpus_mod
import grading
import stats

FIXTURE_CORPUS = Path(__file__).resolve().parent / "fixtures" / "control_corpus.json"

ARMS = ("default", "truncated", "shuffled")

# How much of the output the truncating arm keeps. Low enough that a Python
# file reliably stops parsing, which is the point of the crude control.
TRUNCATE_FRACTION = 0.45


class VacuousControl(AssertionError):
    """The control ran but proved nothing. Louder than a quiet pass."""


def item_pass_rate(item_id: str) -> float:
    """The stub's per-item success probability, derived from the item id.

    Deterministic and spread across 0.30-0.80 so the fixture corpus sits mostly
    in the discriminating band. Derived rather than declared so the fixture
    corpus stays a plain corpus that `evals/corpus.py` validates unchanged, and
    so nobody can quietly tune an item's rate to make a control pass.
    """
    digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:8]
    return 0.30 + 0.50 * (int(digest, 16) / 0xFFFFFFFF)


def _marker(item_id: str) -> str:
    return f"FIXTURE-{item_id.upper()}-OK"


def _good_source(item_id: str) -> str:
    return (
        f'"""Stub artifact for {item_id}."""\n'
        "\n"
        "\n"
        "def report():\n"
        f'    return "{_marker(item_id)}"\n'
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    print(report())\n"
    )


def _wrong_answer_source(item_id: str) -> str:
    """Valid Python that runs cleanly and answers the wrong thing.

    The stub's ordinary failure mode. It parses and it exits zero, so an
    instrument that only checks "did it run" scores it as a pass — which is
    exactly the weakness these controls exist to detect.
    """
    return (
        f'"""Stub artifact for {item_id} that misses the point."""\n'
        "\n"
        "\n"
        "def report():\n"
        '    return "nothing in particular"\n'
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    print(report())\n"
    )


@dataclass
class StubModel:
    """A deterministic stand-in for the pipeline. Never calls a model.

    Given (arm, seed, item) it produces a Python file. The `default` arm
    succeeds at the item's own rate; the degraded arms take that output and
    break it in a specific, named way.
    """

    arm: str
    seed: int

    def source_for(self, item, all_ids: Sequence[str]) -> str:
        if self.arm == "shuffled":
            # Answer the *next* item's question instead of this one. Valid
            # output, wrong pairing — the failure a parse check cannot see.
            index = all_ids.index(item.id)
            other = all_ids[(index + 1) % len(all_ids)]
            return self._default_source(other)
        source = self._default_source(item.id)
        if self.arm == "truncated":
            return source[: max(1, int(len(source) * TRUNCATE_FRACTION))]
        return source

    def _default_source(self, item_id: str) -> str:
        # Seeded on the *default* arm regardless of which arm is asking, so the
        # degraded arms are a transformation of the same draw rather than an
        # independent one. That makes the pairing real: an item the default arm
        # would have failed is not counted as a degradation.
        rng = random.Random(f"default:{self.seed}:{item_id}")
        if rng.random() < item_pass_rate(item_id):
            return _good_source(item_id)
        return _wrong_answer_source(item_id)


# The checks the original harness effectively relied on for an HTML artifact:
# it parsed and it loaded without throwing. Kept here so a control can report
# what a weaker instrument would have concluded from the same artifacts.
WEAK_CHECKS = ("parses", "runs")


@dataclass
class ArmOutcome:
    arm: str
    seed: int
    outcomes: dict[str, bool] = field(default_factory=dict)
    grades: dict[str, dict] = field(default_factory=dict)

    @property
    def passes(self) -> int:
        return sum(1 for v in self.outcomes.values() if v)

    @property
    def weak_outcomes(self) -> dict[str, bool]:
        """What "it parsed and it ran" alone would have said about each item."""
        return {
            item_id: all(
                check["passed"]
                for check in record["checks"]
                if check["kind"] in WEAK_CHECKS
            )
            for item_id, record in self.grades.items()
        }


def run_arm(items: Sequence, arm: str, seed: int, workdir: Path) -> ArmOutcome:
    """Generate and grade one arm over the fixture corpus.

    Grading goes through `evals/grading.py` unchanged — the controls test the
    real grader, not a copy of it.
    """
    if not items:
        raise VacuousControl("control arm was handed an empty corpus")
    stub = StubModel(arm=arm, seed=seed)
    all_ids = [i.id for i in items]
    outcome = ArmOutcome(arm=arm, seed=seed)
    for item in items:
        item_dir = Path(workdir) / arm / str(seed) / item.id
        item_dir.mkdir(parents=True, exist_ok=True)
        artifact = item_dir / "solution.py"
        artifact.write_text(stub.source_for(item, all_ids), encoding="utf-8")
        result = grading.grade(item, [str(artifact)])
        outcome.outcomes[item.id] = result.passed
        outcome.grades[item.id] = result.as_record()
    return outcome


def assert_not_vacuous(items: Sequence, outcome: ArmOutcome) -> None:
    """The preconditions a control must prove before its result means anything."""
    if not items:
        raise VacuousControl("corpus is empty")
    if not outcome.outcomes:
        raise VacuousControl(f"arm {outcome.arm!r} produced no results at all")
    missing = sorted({i.id for i in items} - set(outcome.outcomes))
    if missing:
        raise VacuousControl(f"arm {outcome.arm!r} silently skipped items: {missing}")
    ungraded = sorted(
        item_id for item_id, record in outcome.grades.items() if not record["graded"]
    )
    if ungraded:
        raise VacuousControl(
            f"arm {outcome.arm!r} left items ungraded, so its score is not a measurement: "
            f"{ungraded}"
        )


def assert_grader_discriminates(outcome: ArmOutcome) -> None:
    """The grader must have returned both outcomes at least once.

    A grader stuck on "pass" and a grader stuck on "fail" both produce a clean
    run and a meaningless number.
    """
    values = set(outcome.outcomes.values())
    if values != {True, False}:
        raise VacuousControl(
            f"arm {outcome.arm!r} graded every item {values.pop() if values else 'nothing'} — "
            "the grader never discriminated, so nothing was measured"
        )


def positive_control(
    items: Sequence, degraded_arm: str, workdir: Path, seed: int = 20260903, alpha: float = 0.05
) -> dict:
    """Default arm against a deliberately degraded one. Must detect the damage."""
    if degraded_arm not in ("truncated", "shuffled"):
        raise ValueError(f"{degraded_arm!r} is not a degraded arm")
    baseline = run_arm(items, "default", seed, workdir)
    degraded = run_arm(items, degraded_arm, seed, workdir)

    assert_not_vacuous(items, baseline)
    assert_not_vacuous(items, degraded)
    assert_grader_discriminates(baseline)

    # b = the degraded arm, a = ... deliberately the other way round: the
    # hypothesis under test is "the default arm beats the degraded one", so the
    # default arm is the candidate.
    result = stats.paired_test(degraded.outcomes, baseline.outcomes, alpha=alpha)

    # The same artifacts, judged by parse-and-run alone. For the `shuffled` arm
    # this is the whole point: valid output for the wrong question passes every
    # check that does not look at what was produced, so an instrument without
    # output-level grading reports no difference and is satisfied.
    weak = stats.paired_test(degraded.weak_outcomes, baseline.weak_outcomes, alpha=alpha)
    return {
        "kind": "positive",
        "degraded_arm": degraded_arm,
        "seed": seed,
        "baseline_passes": baseline.passes,
        "degraded_passes": degraded.passes,
        "n": result.n,
        "result": result,
        "detected": result.significant_one_sided and result.net > 0,
        "report": stats.render_paired(result, label_a=degraded_arm, label_b="default"),
        "weak_result": weak,
        "weak_detected": weak.significant_one_sided and weak.net > 0,
        "weak_report": stats.render_paired(weak, label_a=degraded_arm, label_b="default"),
    }


def negative_control(
    items: Sequence, workdir: Path, seed_a: int = 20260903, seed_b: int = 20260904,
    alpha: float = 0.05,
) -> dict:
    """The same configuration twice. Must not report a difference.

    Two *different* seeds on purpose. Replaying one seed would compare a run
    with itself and pass trivially while proving nothing about the noise.
    """
    first = run_arm(items, "default", seed_a, workdir)
    second = run_arm(items, "default", seed_b, workdir)
    assert_not_vacuous(items, first)
    assert_not_vacuous(items, second)
    assert_grader_discriminates(first)
    assert_grader_discriminates(second)

    result = stats.paired_test(first.outcomes, second.outcomes, alpha=alpha)
    return {
        "kind": "negative",
        "seeds": (seed_a, seed_b),
        "n": result.n,
        "result": result,
        # Two-sided: the question here is "did these differ at all", not
        # "did one improve".
        "differs": result.significant_two_sided,
        "report": stats.render_paired(result, label_a=f"run {seed_a}", label_b=f"run {seed_b}"),
    }


def negative_control_false_positive_rate(
    items: Sequence, workdir: Path, seeds: Iterable[tuple[int, int]], alpha: float = 0.05
) -> dict:
    """Run the negative control over many seed pairs and count the false alarms.

    One negative control that comes out non-significant is close to no
    evidence: at alpha=0.05, one draw in twenty is significant by construction,
    and a single pass is equally consistent with a correct noise model and a
    lucky seed. What "the noise model is right" actually claims is that the
    *rate* of significant results between identical configurations is about
    alpha, and that is what this measures.
    """
    pairs = list(seeds)
    if not pairs:
        raise VacuousControl("no seed pairs — the false-positive rate is undefined")
    flagged: list[tuple[int, int]] = []
    p_values: list[float] = []
    # One arm per seed, not one per pair: `run_arm` is deterministic given a
    # seed, and grading sixteen artifacts costs a subprocess each.
    cache: dict[int, ArmOutcome] = {}

    def arm_for(seed: int) -> ArmOutcome:
        if seed not in cache:
            outcome = run_arm(items, "default", seed, workdir)
            assert_not_vacuous(items, outcome)
            assert_grader_discriminates(outcome)
            cache[seed] = outcome
        return cache[seed]

    for seed_a, seed_b in pairs:
        result = stats.paired_test(
            arm_for(seed_a).outcomes, arm_for(seed_b).outcomes, alpha=alpha
        )
        p_values.append(result.p_two_sided)
        if result.significant_two_sided:
            flagged.append((seed_a, seed_b))
    lo, hi = stats.wilson(len(flagged), len(pairs))
    return {
        "kind": "negative_rate",
        "trials": len(pairs),
        "flagged": flagged,
        "rate": len(flagged) / len(pairs),
        "ci95": (lo, hi),
        "p_values": p_values,
        "alpha": alpha,
    }


def load_fixture_corpus() -> list:
    items = corpus_mod.load_corpus(FIXTURE_CORPUS)
    if len(items) < 8:
        raise VacuousControl(
            f"fixture corpus holds {len(items)} items; a positive control needs enough "
            "discordant pairs that a clean sweep can clear alpha at all"
        )
    return items
