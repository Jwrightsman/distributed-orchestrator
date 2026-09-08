"""Language the console is not allowed to use.

`HANDOFF.md` fixes the vocabulary and the design handoff's §7 carries the
substitution table. Each banned phrase is a claim the system cannot back, and
§7 is, in its own words, "the part most likely to drift back" — so it is a
test rather than a note.

Two layers, for two different failure modes:

1. **Rendered visible text.** What a reader actually sees, with `<script>` and
   `<style>` removed. This is where the credits/points and quality-percentage
   rules are enforced, because checking raw source for `credits` would trip
   over `credits_earned` — an API field name that is not going to be renamed
   by a language pass.
2. **Raw sources, including the partials.** Catches phrases sitting in a
   JavaScript string that only becomes visible at runtime. Restricted to
   phrases that cannot collide with an identifier.

Additions since the handoff, from work that landed after it. `docs/adr/0017` is
the normative source for the first two:

- the ledger chain is tamper-*evident*, never tamper-proof, and never proof of
  correct execution;
- the provenance envelope is a binding of identity, never a signature or an
  attestation;
- nothing may imply capability evidence affects routing — it is shadow-only;
- nothing may state a quality percentage. PR #71 showed every published
  `web_app` figure rested on `browser_ok`, a check weaker than the thing it was
  reported as, and PR #72 put the run-to-run discordant rate at 0.643. See
  `docs/design/HANDOFF-DELTA.md` §4.1.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard
from server import app

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

# Pages reachable without a slot-filling route, rendered through the same
# partial injection the server uses.
STANDALONE = ("index.html", "dashboard.html", "try.html", "locked.html")

# Every template and partial, checked as raw source. `_run_detail.css` styles
# the run-detail surface on both the console and the public run page, so the
# rules follow it there rather than leaving a hole where it sits.
SOURCES = STANDALONE + ("run.html", "status.html", "_dashboard.css", "_dashboard.js",
           "_status_model.js", "_run_detail.css")

# The modules that build user-visible HTML in Python. Every phrase this suite
# caught on its first run was in one of these rather than in a template, which
# is exactly the mistake docs/design/HANDOFF-DELTA.md §4.3 records: the design
# handoff sent the reader to status.html for two phrases that live in
# routes_status.py.
# `run_detail.py` builds the run-detail markup for both surfaces, so every
# sentence on either of them is written there. It is the single most important
# file in this list: the surface it renders is the one carrying the manifest
# and the envelope, which are exactly the two records the prohibited words
# would overstate.
RENDERING_MODULES = ("routes_status.py", "routes_run.py", "routes_try.py",
                     "run_detail.py")

_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def visible_text(html: str) -> str:
    """Roughly what a reader sees: no scripts, styles, comments or markup."""
    html = _SCRIPT_OR_STYLE.sub(" ", html)
    html = _COMMENT.sub(" ", html)
    return _TAG.sub(" ", html)


# Phrases that cannot collide with an identifier, so they can be checked
# against raw source as well as rendered text.
BANNED_PHRASES = (
    "swarm of ordinary computers",
    "volunteer machine",
    "volunteer hardware",
    "volunteer computer",
    "anonymous machine",
    "tamper-proof",
    "tamper proof",
    "tamperproof",
    "proof of correct execution",
    "trustless",
    "permissionless",
    "no cloud, no api keys",
)

# Words that are prohibited as claims but collide with identifiers, so they
# are matched on whole words rather than as substrings. Each is a claim ADR
# 0017 says this system cannot back:
#
#   verified     -- `unverified` is the assurance level's own value and is the
#                   opposite claim, so the boundary matters in both directions
#   signature    -- the envelope's slot is reserved and empty: no key, no key
#                   management, no transparency log, no third party
#   signed       -- collides with `assigned_node_id` and friends
#   attestation  -- an envelope binds identity; it attests to nothing
#   attested
BANNED_WORDS = ("verified", "signature", "signed", "attestation", "attested")

_WORD = {
    word: re.compile(rf"(?<![a-z-]){word}(?![a-z])", re.I) for word in BANNED_WORDS
}

# Regexes for claims that need a shape rather than a literal.
BANNED_PATTERNS = (
    # The provenance envelope is a binding of identity, not a signature.
    (re.compile(r"provenance[^.]{0,60}\b(signature|signed|attestation|attests)\b", re.I),
     "provenance described as a signature or attestation"),
    # Evidence is shadow-only and never routes. The negated form is the
    # required disclaimer — "never affects which machine gets work" — so only an
    # unnegated claim is a finding.
    (re.compile(r"\b(evidence|observation|agreement)\b"
                r"(?![^.]{0,60}\b(never|not|no|nothing)\b)"
                r"[^.]{0,60}\b"
                r"(routes?|routing|which machine gets|prioriti[sz]|affects? placement)\b", re.I),
     "capability evidence implied to affect routing"),
    # Agreement is a bounded output comparison, never correctness. As above,
    # the negated form ("agreement … is not correctness") is the disclaimer the
    # rows are required to carry, so only an unnegated claim is a finding.
    (re.compile(r"\bagreement\b(?![^.]{0,40}\b(never|not|no|nothing)\b)"
                r"[^.]{0,40}\bcorrect(ness)?\b", re.I),
     "agreement presented as correctness"),
)

# A percentage attached to task-success language. Deliberately narrow: "95%
# confidence" beside a withdrawn figure is part of the same claim, but a
# ratio like 2/10 from showcase_reliability is the stronger check and stays.
QUALITY_PERCENTAGE = re.compile(
    r"\d+\s*%[^.]{0,80}\b(task|runnable|on-spec|quality|success|pass(es|ed)?)\b"
    r"|\b(task|runnable|on-spec|quality|success)\b[^.]{0,80}\d+\s*%",
    re.I,
)

# `CREDITS` as a user-facing unit. The API field `credits_earned` keeps its
# name; this is about what the reader is told they earned.
CREDITS_AS_A_UNIT = re.compile(r"\bcredits\b", re.I)


def _rendered(page: str) -> str:
    return dashboard._page(page)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("page", SOURCES)
def test_no_template_source_contains_a_banned_phrase(page):
    """Catches a phrase living in a JS string that only renders at runtime."""
    text = (TEMPLATES / page).read_text(encoding="utf-8").lower()
    for phrase in BANNED_PHRASES:
        assert phrase not in text, (
            f"{page} contains the prohibited phrase {phrase!r}. "
            "See docs/design/HANDOFF-DELTA.md §4.3 and §4.5 for the replacement."
        )


@pytest.mark.parametrize("page", SOURCES)
def test_no_template_source_uses_a_prohibited_word(page):
    """Whole words, because these collide with identifiers.

    Checked against rendered text rather than raw source for the templates
    that carry script: a CSS class or a JS variable named `signed` would be a
    poor name and is not a claim, while the same word in a sentence is.
    """
    text = visible_text((TEMPLATES / page).read_text(encoding="utf-8"))
    for word, pattern in _WORD.items():
        found = pattern.search(text)
        assert not found, (
            f"{page} says {found.group(0)!r} to the reader. See ADR 0017: the "
            "envelope binds identity and the chain is tamper-evident; neither "
            "is a signature and neither establishes correctness."
        )


def copy_strings(source: str) -> list[tuple[int, str]]:
    """Every string literal in a module that is not a docstring.

    The copy on these surfaces lives in Python, so the rules have to reach it
    there. But a docstring saying "this is not a signature" is the rule being
    written down, not the claim being made — and a scan that cannot tell those
    apart would push the reasoning out of the file that needs it most. So the
    module is parsed and only the strings that can become output are read.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None) or []
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


@pytest.mark.parametrize("module", RENDERING_MODULES)
def test_no_rendering_module_uses_a_prohibited_word(module):
    """The copy on these surfaces lives in Python, not in a template."""
    root = Path(__file__).resolve().parent.parent
    source = (root / module).read_text(encoding="utf-8")
    for lineno, text in copy_strings(source):
        for pattern in _WORD.values():
            found = pattern.search(text)
            assert not found, (
                f"{module}:{lineno} says {found.group(0)!r} to the reader. See "
                "ADR 0017: the envelope binds identity and the chain is "
                "tamper-evident; neither is a signature and neither "
                "establishes correctness."
            )


@pytest.mark.parametrize("page", SOURCES)
def test_no_template_source_makes_a_prohibited_claim(page):
    text = (TEMPLATES / page).read_text(encoding="utf-8")
    for pattern, description in BANNED_PATTERNS:
        match = pattern.search(text)
        assert not match, f"{page} has {description}: {match.group(0)!r}"


@pytest.mark.parametrize("page", STANDALONE)
def test_no_rendered_page_says_credits_or_a_quality_percentage(page):
    body = visible_text(_rendered(page))

    found = CREDITS_AS_A_UNIT.search(body)
    assert not found, (
        f"{page} shows {found.group(0)!r} to the reader. The unit is POINTS, and "
        "the footnote saying what points do not mean travels with it."
    )

    found = QUALITY_PERCENTAGE.search(body)
    assert not found, (
        f"{page} states a quality percentage: {found.group(0)!r}. Every published "
        "web_app figure rested on browser_ok, which PR #71 showed was weaker than "
        "the claim it carried. No surface renders one."
    )


@pytest.mark.parametrize("route", ["/", "/dashboard", "/try"])
def test_served_pages_carry_no_prohibited_language(client, route):
    """The same rules against what the server actually sends."""
    body = visible_text(client.get(route).text)
    lowered = body.lower()
    for phrase in BANNED_PHRASES:
        assert phrase not in lowered, f"{route} served the prohibited phrase {phrase!r}"
    for word, pattern in _WORD.items():
        found = pattern.search(body)
        assert not found, f"{route} served the prohibited word {found.group(0)!r}"
    assert not CREDITS_AS_A_UNIT.search(body), f"{route} served 'credits' as a unit"
    assert not QUALITY_PERCENTAGE.search(body), f"{route} served a quality percentage"


def test_the_points_footnote_travels_with_the_points_column():
    """A page whose subject is what a machine earned is the last place to drop
    the sentence saying what earning does not mean."""
    body = _rendered("dashboard.html")
    assert "POINTS" in body, "the Nodes view no longer labels the column POINTS"
    for clause in (
        "do not mean the candidate was selected",
        "not money, a token, or a claim on future value",
    ):
        assert clause in body, f"the points footnote lost: {clause!r}"


def test_the_nodes_view_says_evidence_does_not_route():
    """Shadow evidence never routes, and the row has to say so."""
    body = (TEMPLATES / "_dashboard.js").read_text(encoding="utf-8")
    assert "never affects which machine gets work" in body
    assert "agreement between two runs is not correctness" in body


@pytest.mark.parametrize("module", RENDERING_MODULES)
def test_no_rendering_module_contains_a_banned_phrase(module):
    """The templates are not the only place this copy lives.

    Every phrase this suite caught on its first run was in one of these
    modules, not in a template.
    """
    root = Path(__file__).resolve().parent.parent
    text = (root / module).read_text(encoding="utf-8").lower()
    for phrase in BANNED_PHRASES:
        assert phrase not in text, f"{module} contains the prohibited phrase {phrase!r}"
