"""Run detail, and the two records the design keeps apart.

Ported from `docs/design/run-detail-2026-09-07`. The design's own reasoning is
in that directory; what is asserted here is only the part a future edit could
break silently.

The single most important rule in the pass has its own section below: the
sealed manifest and the provenance envelope make different claims and must
never read as one. Two chips side by side is a row of ticks, and ticks get
counted as one stronger claim, so the manifest gets a chip (its state genuinely
varies) and the envelope gets a sentence. No heading may span both.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

CSS = (TEMPLATES / "_dashboard.css").read_text(encoding="utf-8")
JS = (TEMPLATES / "_dashboard.js").read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    """The declaration block for one selector, or "" if it has none."""
    match = re.search(
        r"(?:^|[\n;}])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}",
        css,
        re.M,
    )
    return match.group(1) if match else ""


# ── The modal header at a narrow width ───────────────────────────────
# `.modal-head` was a non-wrapping flex row with `space-between`, and
# `.modal-title` had `min-width: 0` and no wrapping rule. A long unbreakable
# execution ID overflowed its squeezed box while `.modal-actions`
# (`flex-shrink: 0`) held its width, so the title ran under the buttons.
# Both modals share the rule, so the node modal is fixed by the same change.


def test_the_modal_header_may_wrap():
    assert "flex-wrap: wrap" in _rule(CSS, ".modal-head"), (
        "a non-wrapping header cannot put the title on its own line, which is "
        "the whole of the narrow-width fix"
    )


def test_the_modal_title_may_claim_a_line_and_break_a_long_id():
    rule = _rule(CSS, ".modal-title")
    assert "flex: 1 1 200px" in rule, "the title cannot claim a line of its own"
    assert "overflow-wrap: anywhere" in rule, (
        "an execution id has no break opportunity, so without this it overflows "
        "its box however much the box wraps"
    )


def test_the_modal_header_does_not_use_space_between():
    """`margin-left: auto` replaces it.

    They are equivalent while there is room — which is why this fix changes no
    geometry above the breakpoint — but `space-between` on a wrapped row pushes
    the actions to the far edge of their own line instead of following the
    title.
    """
    assert "margin-left: auto" in _rule(CSS, ".modal-actions")
    assert "justify-content: space-between" not in _rule(CSS, ".modal-head")


def test_both_modals_share_the_header_rule():
    """The node modal is fixed by the same four declarations, not a copy."""
    dashboard = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    heads = dashboard.count('class="modal-head"')
    assert heads == 2, f"expected the run and node modals to share the class, found {heads}"
