"""Tests for process-local sampled output agreement.

The comparison is deliberately coarse and observational: it records shape
agreement without treating either output as correct and without affecting task
assignment.
"""

import random

import pytest

from verification import (
    SampledAgreementRecord,
    VerificationPool,
    compare_outputs,
    verification_identity_key,
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


def _enrollment_key(name: str) -> str:
    key = verification_identity_key(enrollment_id=f"enrollment-{name}")
    assert key is not None
    return key


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


class TestAgreementRecording:
    def test_unmeasured_record_has_no_rate(self):
        record = SampledAgreementRecord("fresh")
        assert record.agreement_rate is None

    def test_rate_is_agreement_ratio(self):
        record = SampledAgreementRecord("n")
        for _ in range(3):
            record.record(True)
        record.record(False)
        assert record.agreement_rate == pytest.approx(0.75)

    def test_serialized_record_uses_agreement_terms_only(self):
        record = SampledAgreementRecord("n")
        record.record(False)

        payload = record.as_dict()

        assert payload["sampled_comparisons"] == 1
        assert payload["disagreements"] == 1
        assert payload["agreement_rate"] == 0.0
        assert "routing_weight" not in payload
        assert "trusted_for_routing" not in payload
        assert "agreement_score" not in payload

    def test_sample_history_is_bounded(self):
        record = SampledAgreementRecord("n")
        for _ in range(50):
            record.record(True, "x")
        assert len(record.samples) <= 20


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


class TestComparisonRecordsBothNodes:
    def test_agreement_is_recorded_for_both(self):
        pool = VerificationPool(verify_rate=1.0)
        key_a, key_b = _enrollment_key("a"), _enrollment_key("b")
        pool.record_comparison(
            "a", PY_A, "b", PY_B, identity_a=key_a, identity_b=key_b
        )
        assert pool.agreement_record(key_a).agreed == 1
        assert pool.agreement_record(key_b).agreed == 1

    def test_disagreement_is_recorded_for_both(self):
        """The observation does not decide which output, if either, was wrong."""
        pool = VerificationPool(verify_rate=1.0)
        key_a, key_b = _enrollment_key("a"), _enrollment_key("b")
        result = pool.record_comparison(
            "a", PY_A, "b", REFUSAL, identity_a=key_a, identity_b=key_b
        )
        assert not result["agreed"]
        assert pool.agreement_record(key_a).disagreed == 1
        assert pool.agreement_record(key_b).disagreed == 1

    def test_many_disagreements_remain_diagnostics_only(self):
        pool = VerificationPool(verify_rate=1.0)
        for peer in ("good1", "good2", "good3"):
            for _ in range(4):
                pool.record_comparison(
                    peer,
                    PY_A,
                    "liar",
                    REFUSAL,
                    identity_a=_enrollment_key(peer),
                    identity_b=_enrollment_key("liar"),
                )
        for _ in range(4):
            pool.record_comparison(
                "good1",
                PY_A,
                "good2",
                PY_B,
                identity_a=_enrollment_key("good1"),
                identity_b=_enrollment_key("good2"),
            )

        compared = pool.agreement_record(_enrollment_key("liar"))
        assert compared.disagreed == 12
        assert not hasattr(compared, "routing_weight")
        assert not hasattr(pool, "rank")
        assert not hasattr(pool, "rank_nodes")

    def test_same_label_does_not_share_enrollment_or_legacy_session_record(self):
        pool = VerificationPool(verify_rate=1.0)
        enrolled_a = verification_identity_key(enrollment_id="enrollment-a")
        enrolled_b = verification_identity_key(enrollment_id="enrollment-b")
        legacy_a = verification_identity_key(session_id="session-a")
        legacy_b = verification_identity_key(session_id="session-b")
        assert None not in {enrolled_a, enrolled_b, legacy_a, legacy_b}

        pool.agreement_record(str(enrolled_a), node_id="shared").record(False)
        pool.agreement_record(str(legacy_a), node_id="shared").record(False)

        assert pool.agreement_record(str(enrolled_b), node_id="shared").total == 0
        assert pool.agreement_record(str(legacy_b), node_id="shared").total == 0

    def test_comparison_without_identity_is_not_attributed_to_labels(self):
        pool = VerificationPool(verify_rate=1.0)
        verdict = pool.record_comparison("same-label", PY_A, "peer", PY_B)
        assert verdict["recorded"] is False
        assert pool.agreement_records == {}
