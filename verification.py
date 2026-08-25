"""
Verification and node reputation.

The circuit breaker catches a node that *fails*. It cannot catch a node that
returns plausible-looking garbage — the honest limitation the README already
states. This closes that gap the cheap way: occasionally give the same subtask
to two nodes and compare what comes back.

Design constraints that shaped this:

- **Cost.** Every duplicate is a whole extra inference. Verification is
  sampled, not universal — `verify_rate` (default 0) picks a fraction of tasks.
- **Small models disagree constantly.** Two honest nodes will not produce
  identical text for the same prompt. Comparing strings would flag everything.
  So agreement is measured on what actually matters: does the deliverable have
  the same *shape* — same artifact kind, both parse, similar size.
- **A score has to survive being wrong.** One disagreement is not evidence of a
  bad node; it may be the other node that was wrong, or both may be fine. The
  score is a running ratio with a floor on sample size before it influences
  anything.

Reputation feeds routing weight: a node with a poor verified record gets offered
work last, not never. Exclusion is the circuit breaker's job.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

# Below this many verified samples a node's score is not trusted for routing.
MIN_SAMPLES_FOR_ROUTING = 3

# Reputation floor — a node never drops to zero weight from disagreement alone.
MIN_WEIGHT = 0.25


def verification_identity_key(
    *,
    enrollment_id: str | None = None,
    session_id: str | None = None,
) -> str | None:
    """Return a namespaced trust key without falling back to a node label."""

    if enrollment_id:
        return f"enrollment:{enrollment_id}"
    if session_id:
        return f"legacy-session:{session_id}"
    return None


@dataclass
class NodeReputation:
    """Running verification record for one node."""

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
    def score(self) -> float:
        """Agreement ratio, 1.0 when unmeasured — new nodes are not suspects."""
        if self.total == 0:
            return 1.0
        return self.agreed / self.total

    @property
    def routing_weight(self) -> float:
        """How strongly to prefer this node. 1.0 until it has a real record."""
        if self.total < MIN_SAMPLES_FOR_ROUTING:
            return 1.0
        return max(MIN_WEIGHT, self.score)

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
            "verified_samples": self.total,
            "agreement_score": round(self.score, 3),
            "routing_weight": round(self.routing_weight, 3),
            "trusted_for_routing": self.total >= MIN_SAMPLES_FOR_ROUTING,
        }


_CODE_FENCE = re.compile(r"```(\w*)")


def _shape(text: str) -> dict:
    """The comparable shape of a deliverable.

    Deliberately coarse. Two honest small models writing the same function will
    not agree token for token, and demanding that would make every comparison a
    disagreement. What a *dishonest or broken* node gets wrong is bigger: it
    returns nothing, returns prose where code was asked for, or returns a
    fraction of the length.
    """
    langs = [m.group(1).lower() for m in _CODE_FENCE.finditer(text) if m.group(1)]
    stripped = text.strip()
    return {
        "length": len(stripped),
        "has_code": bool(langs) or stripped.startswith(("<!doctype", "<html", "#!", "import ", "def ")),
        "languages": sorted(set(langs)),
        # Genuinely nothing, not merely short. A 40-char answer can be a
        # correct one-liner; treating it as empty made a Python-vs-JavaScript
        # mismatch score as agreement because both sides were "empty".
        "empty": len(stripped) < 15,
    }


def compare_outputs(a: str, b: str) -> tuple[bool, str]:
    """Do two answers to the same subtask agree in substance?

    Returns (agreed, reason). Length comparison is ratio-based with a generous
    band — the goal is catching a node that returns a stub or a refusal while
    another returns real work, not enforcing stylistic similarity.
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
    """Tracks reputation and decides which tasks get a second opinion."""

    def __init__(self, verify_rate: float = 0.0, rng: random.Random | None = None):
        self.verify_rate = verify_rate
        self.reputations: dict[str, NodeReputation] = {}
        self._rng = rng or random.Random()

    def reputation(
        self,
        identity_key: str,
        *,
        node_id: str | None = None,
        enrollment_id: str | None = None,
    ) -> NodeReputation:
        """Return one record keyed by enrollment or explicit legacy session."""

        if not identity_key:
            raise ValueError("verification identity key is required")
        record = self.reputations.get(identity_key)
        if record is None:
            record = NodeReputation(
                identity_key=identity_key,
                node_id=node_id,
                enrollment_id=enrollment_id,
            )
            self.reputations[identity_key] = record
        else:
            if record.node_id is None and node_id is not None:
                record.node_id = node_id
            if record.enrollment_id is None and enrollment_id is not None:
                record.enrollment_id = enrollment_id
        return record

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
        """Compare two answers and credit both nodes with the outcome.

        Neither node is assumed correct. Agreement raises both records;
        disagreement lowers both, because from here we cannot tell which one was
        wrong. Over many samples the consistently-odd node separates itself.
        """
        agreed, reason = compare_outputs(output_a, output_b)
        recorded = bool(identity_a and identity_b)
        if recorded:
            self.reputation(
                str(identity_a),
                node_id=node_a,
                enrollment_id=enrollment_id_a,
            ).record(agreed, reason)
            self.reputation(
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

    def rank(self, identity_keys: list[str]) -> list[str]:
        """Identity keys ordered best-first. Stable for equal weights."""
        return sorted(
            identity_keys,
            key=lambda key: (-self.reputation(key).routing_weight, key),
        )

    def rank_nodes(self, nodes: list[tuple[str, str]]) -> list[str]:
        """Return display labels ranked through their non-label identity keys."""

        return [
            node_id
            for node_id, _identity_key in sorted(
                nodes,
                key=lambda item: (
                    -self.reputation(item[1], node_id=item[0]).routing_weight,
                    item[0],
                ),
            )
        ]

    def as_dict(self) -> dict:
        return {
            "verify_rate": self.verify_rate,
            "nodes": [r.as_dict() for r in self.reputations.values()],
        }
