"""Process-local sampled output agreement.

Every duplicate costs a whole extra inference, so ``verify_rate`` (default 0)
samples only a fraction of tasks. Two stochastic model outputs are compared by
coarse shape rather than exact text. The resulting agreement/disagreement is a
diagnostic observation only: it is not correctness, trust, or a routing signal.

The operational circuit breaker is separate and continues to handle explicit
worker failures. This module neither excludes nor orders workers.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

SAMPLED_AGREEMENT_METHOD_VERSION = "shape-v1"


def verification_identity_key(
    *,
    enrollment_id: str | None = None,
    session_id: str | None = None,
) -> str | None:
    """Return a namespaced comparison key without using a mutable node label."""

    if enrollment_id:
        return f"enrollment:{enrollment_id}"
    if session_id:
        return f"legacy-session:{session_id}"
    return None


@dataclass
class SampledAgreementRecord:
    """Bounded process-local agreement observations for one identity."""

    identity_key: str
    node_id: str | None = None
    enrollment_id: str | None = None
    agreed: int = 0
    disagreed: int = 0
    samples: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.agreed + self.disagreed

    @property
    def agreement_rate(self) -> float | None:
        """Observed agreement ratio, or ``None`` when there are no samples."""
        if self.total == 0:
            return None
        return self.agreed / self.total

    def record(self, agreed: bool, detail: str = "") -> None:
        if agreed:
            self.agreed += 1
        else:
            self.disagreed += 1
        self.samples.append({"agreed": agreed, "detail": detail[:200]})
        del self.samples[:-20]  # keep the tail only

    def as_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "enrollment_id": self.enrollment_id,
            "sampled_comparisons": self.total,
            "agreements": self.agreed,
            "disagreements": self.disagreed,
            "agreement_rate": (
                round(self.agreement_rate, 3)
                if self.agreement_rate is not None
                else None
            ),
        }


_CODE_FENCE = re.compile(r"```(\w*)")


def _shape(text: str) -> dict:
    """The comparable shape of a deliverable.

    Deliberately coarse. Two stochastic models writing the same function will
    not agree token for token. Shape comparison records large observable
    differences such as an empty output, code/prose mismatch, or very different
    lengths without claiming which output is better.
    """
    langs = [m.group(1).lower() for m in _CODE_FENCE.finditer(text) if m.group(1)]
    stripped = text.strip()
    return {
        "length": len(stripped),
        "has_code": bool(langs) or stripped.startswith(("<!doctype", "<html", "#!", "import ", "def ")),
        "languages": sorted(set(langs)),
        # Genuinely nothing, not merely short. A 40-char answer can be a
        # valid one-liner; treating it as empty made a Python-vs-JavaScript
        # mismatch appear as agreement because both sides were "empty".
        "empty": len(stripped) < 15,
    }


def compare_outputs(a: str, b: str) -> tuple[bool, str]:
    """Do two answers to the same subtask have a comparable shape?

    Returns (agreed, reason). Length comparison is ratio-based with a generous
    band. This deliberately does not establish semantic equivalence or
    correctness.
    """
    sa, sb = _shape(a), _shape(b)

    if sa["empty"] != sb["empty"]:
        return False, "one output is empty, the other is not"
    if sa["empty"] and sb["empty"]:
        return True, "both empty"
    if sa["has_code"] != sb["has_code"]:
        return False, "one returned code, the other did not"

    if sa["languages"] and sb["languages"] and not set(sa["languages"]) & set(sb["languages"]):
        return False, f"different languages: {sa['languages']} vs {sb['languages']}"

    longer, shorter = max(sa["length"], sb["length"]), min(sa["length"], sb["length"])
    if longer and shorter / longer < 0.25:
        return False, f"length differs by more than 4x ({shorter} vs {longer})"

    return True, "shapes match"


class VerificationPool:
    """Tracks sampled agreement and decides which tasks get a second opinion."""

    def __init__(self, verify_rate: float = 0.0, rng: random.Random | None = None):
        self.verify_rate = verify_rate
        self.agreement_records: dict[str, SampledAgreementRecord] = {}
        self._rng = rng or random.Random()

    def agreement_record(
        self,
        identity_key: str,
        *,
        node_id: str | None = None,
        enrollment_id: str | None = None,
    ) -> SampledAgreementRecord:
        """Return one record keyed by enrollment or explicit legacy session."""

        if not identity_key:
            raise ValueError("sampled-agreement identity key is required")
        record = self.agreement_records.get(identity_key)
        if record is None:
            record = SampledAgreementRecord(
                identity_key=identity_key,
                node_id=node_id,
                enrollment_id=enrollment_id,
            )
            self.agreement_records[identity_key] = record
        else:
            if record.node_id is None and node_id is not None:
                record.node_id = node_id
            if record.enrollment_id is None and enrollment_id is not None:
                record.enrollment_id = enrollment_id
        return record

    def reputation(
        self,
        identity_key: str,
        *,
        node_id: str | None = None,
        enrollment_id: str | None = None,
    ) -> SampledAgreementRecord:
        """Deprecated compatibility alias; this record never affects routing."""

        return self.agreement_record(
            identity_key,
            node_id=node_id,
            enrollment_id=enrollment_id,
        )

    def should_verify(self, available_nodes: int) -> bool:
        """Duplicate this task? Needs a spare node and the dice.

        With one node there is nobody to compare against, so verification is
        silently off — it must never block work on a single-node network.
        """
        if self.verify_rate <= 0 or available_nodes < 2:
            return False
        return self._rng.random() < self.verify_rate

    def record_comparison(
        self,
        node_a: str,
        output_a: str,
        node_b: str,
        output_b: str,
        *,
        identity_a: str | None = None,
        identity_b: str | None = None,
        enrollment_id_a: str | None = None,
        enrollment_id_b: str | None = None,
    ) -> dict:
        """Compare two output shapes and record the same outcome for both.

        Neither output is assumed correct. A disagreement cannot identify which
        output, if either, is better.
        """
        agreed, reason = compare_outputs(output_a, output_b)
        recorded = bool(identity_a and identity_b)
        if recorded:
            self.agreement_record(
                str(identity_a),
                node_id=node_a,
                enrollment_id=enrollment_id_a,
            ).record(agreed, reason)
            self.agreement_record(
                str(identity_b),
                node_id=node_b,
                enrollment_id=enrollment_id_b,
            ).record(agreed, reason)
        return {
            "agreed": agreed,
            "reason": reason,
            "nodes": [node_a, node_b],
            "enrollment_id_a": enrollment_id_a,
            "enrollment_id_b": enrollment_id_b,
            "recorded": recorded,
        }

    def as_dict(self) -> dict:
        return {
            "verify_rate": self.verify_rate,
            "sampled_agreements": [
                record.as_dict() for record in self.agreement_records.values()
            ],
        }
