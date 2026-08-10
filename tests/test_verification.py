"""Tests for verification and node reputation.

The thing being defended against is a node that returns *plausible* garbage —
the failure the circuit breaker explicitly cannot catch. The tests below care
most about two properties: real disagreement is caught, and honest variation
between two small models is NOT treated as disagreement. A verifier that flags
everything is worse than none, because it would penalise every honest node.
"""

import random

import pytest

from verification import (
    MIN_SAMPLES_FOR_ROUTING,
    NodeReputation,
    VerificationPool,
    compare_outputs,
)

PY_A = """```python
def add(a, b):
    \"\"\"Return the sum.\"\"\"
    return a + b
```"""

PY_B = """```python
def add(x, y):
    # different names, different comment, same job
    total = x + y
    return total
```"""

REFUSAL = "I cannot help with that request."
STUB = "```python\npass\n```"


class TestHonestVariationIsNotDisagreement:
    def test_same_task_different_wording_agrees(self):
        agreed, reason = compare_outputs(PY_A, PY_B)
        assert agreed, reason

    def test_both_empty_agrees(self):
        agreed, _ = compare_outputs("", "   ")
        assert agreed

    def test_modest_length_difference_agrees(self):
        a = "```python\n" + "x = 1\n" * 40 + "```"
        b = "```python\n" + "x = 1\n" * 60 + "```"
        agreed, reason = compare_outputs(a, b)
        assert agreed, reason


class TestRealDisagreementIsCaught:
    def test_refusal_against_real_work(self):
        agreed, reason = compare_outputs(PY_A, REFUSAL)
        assert not agreed
        assert "code" in reason or "empty" in reason

    def test_stub_against_real_work(self):
        agreed, reason = compare_outputs("```python\n" + "line = 1\n" * 50 + "```", STUB)
        assert not agreed
        assert "4x" in reason or "length" in reason

    def test_wrong_language(self):
        agreed, reason = compare_outputs(PY_A, "```javascript\nconst add = (a,b) => a+b;\n```")
        assert not agreed
        assert "language" in reason

    def test_empty_against_work(self):
        agreed, reason = compare_outputs(PY_A, "")
        assert not agreed


class TestReputationScoring:
    def test_unmeasured_node_is_not_a_suspect(self):
        rep = NodeReputation("fresh")
        assert rep.score == 1.0
        assert rep.routing_weight == 1.0

    def test_score_is_agreement_ratio(self):
        rep = NodeReputation("n")
        for _ in range(3):
            rep.record(True)
        rep.record(False)
        assert rep.score == pytest.approx(0.75)

    def test_routing_weight_ignored_until_enough_samples(self):
        rep = NodeReputation("n")
        rep.record(False)
        # One bad sample must not demote a node — it may have been the peer
        assert rep.total < MIN_SAMPLES_FOR_ROUTING
        assert rep.routing_weight == 1.0

    def test_routing_weight_has_a_floor(self):
        rep = NodeReputation("n")
        for _ in range(10):
            rep.record(False)
        assert rep.routing_weight >= 0.25, "a node must never be starved to zero by disagreement"

    def test_sample_history_is_bounded(self):
        rep = NodeReputation("n")
        for _ in range(50):
            rep.record(True, "x")
        assert len(rep.samples) <= 20


class TestVerificationSampling:
    def test_disabled_by_default(self):
        pool = VerificationPool()
        assert not pool.should_verify(available_nodes=5)

    def test_never_verifies_with_one_node(self):
        """Nothing to compare against — must not block work on a solo network."""
        pool = VerificationPool(verify_rate=1.0)
        assert not pool.should_verify(available_nodes=1)

    def test_always_verifies_at_rate_one(self):
        pool = VerificationPool(verify_rate=1.0)
        assert pool.should_verify(available_nodes=2)

    def test_rate_is_respected(self):
        pool = VerificationPool(verify_rate=0.5, rng=random.Random(7))
        hits = sum(pool.should_verify(available_nodes=3) for _ in range(400))
        assert 150 < hits < 250, f"expected ~200 of 400, got {hits}"


class TestComparisonCreditsBothNodes:
    def test_agreement_raises_both(self):
        pool = VerificationPool(verify_rate=1.0)
        pool.record_comparison("a", PY_A, "b", PY_B)
        assert pool.reputation("a").agreed == 1
        assert pool.reputation("b").agreed == 1

    def test_disagreement_lowers_both(self):
        """We cannot tell which one was wrong, so neither is credited."""
        pool = VerificationPool(verify_rate=1.0)
        result = pool.record_comparison("a", PY_A, "b", REFUSAL)
        assert not result["agreed"]
        assert pool.reputation("a").disagreed == 1
        assert pool.reputation("b").disagreed == 1

    def test_bad_node_separates_over_many_samples(self):
        """The point of the design: one liar, many honest peers."""
        pool = VerificationPool(verify_rate=1.0)
        for peer in ("good1", "good2", "good3"):
            for _ in range(4):
                pool.record_comparison(peer, PY_A, "liar", REFUSAL)
        for _ in range(4):
            pool.record_comparison("good1", PY_A, "good2", PY_B)

        liar = pool.reputation("liar").routing_weight
        honest = pool.reputation("good1").routing_weight
        assert liar < honest, f"liar {liar} should rank below honest {honest}"


class TestRouting:
    def test_rank_prefers_higher_weight(self):
        pool = VerificationPool(verify_rate=1.0)
        for _ in range(MIN_SAMPLES_FOR_ROUTING + 1):
            pool.reputation("bad").record(False)
            pool.reputation("good").record(True)
        assert pool.rank(["bad", "good"]) == ["good", "bad"]

    def test_rank_is_stable_for_equal_weights(self):
        pool = VerificationPool()
        assert pool.rank(["c", "a", "b"]) == ["a", "b", "c"]

    def test_unranked_nodes_are_not_penalised(self):
        """A brand-new node must not be sent to the back of the queue."""
        pool = VerificationPool(verify_rate=1.0)
        for _ in range(MIN_SAMPLES_FOR_ROUTING + 1):
            pool.reputation("proven").record(True)
        assert pool.rank(["fresh", "proven"])[0] in ("fresh", "proven")
        assert pool.reputation("fresh").routing_weight == 1.0
