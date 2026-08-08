"""Prompt set v3 — UNMEASURED candidate, built from the v1 baseline's actual failures.

v2 was written before any baseline existed and targets failures from the sprint
logs. v3 keeps v2's wording and adds rules aimed at the failure modes the
**measured** v1 baseline produced (run 20260806_195850, 9/28 = 32%). Every rule
below cites the run that motivated it — if a rule cannot name a failure it
fixed, it does not belong here.

Measured failures and the rule each one produced:

1. **Undefined names — the single biggest fixable class.**
   `api-url-shortener`: NameError: name 'Query' is not defined.
   `api-http-server`: NameError: name 'dataclass' is not defined.
   Both wrote code using a symbol they never imported. v1 and v2 both already
   say "with all imports" — clearly not enough, so v3 makes it a concrete
   final check rather than an adjective.

2. **Third-party dependencies that aren't installed.**
   `vague-make-a-game`: ModuleNotFoundError: No module named 'pygame'.
   `vague-something-useful`: ModuleNotFoundError: No module named 'sqlmodel'.
   Reasonable code, unrunnable deliverable. Nothing in v1/v2 says which
   libraries exist, so the model guesses. v3 says: standard library unless the
   task names a package.

3. **Code that fails its own tests.** The whole `algorithm` category, 0/4.
   `algo-roman`: AssertionError: Conversion mismatch for 4.
   `algo-binary-search`: AssertionError. `algo-matrix`: IndexError.
   `algo-fizzbuzz-tests`: FAILED (failures=1). The model wrote both the
   implementation and the assertions, and they disagree. v3 makes tracing one
   example by hand a required step.

4. **JavaScript that throws on load.** Found only once a real browser ran the
   HTML: `web-todo` and `web-markdown-preview` both raise uncaught errors,
   having passed a static structure check. v3 adds the specific DOM discipline
   that avoids the common cause — referencing elements that don't exist.

**None of this is known to be better.** Measure before promoting:

    python evals/run_evals.py --prompt-set v3

Compare against 32% (9/28) from `evals/results/20260806_195850`. If it does
not move the score, delete it. That is the rule.
"""

from prompts import PromptSet
from prompts.v2 import (
    PLANNER_SYSTEM as V2_PLANNER,
    REVIEWER_SYSTEM as V2_REVIEWER,
    REVISER_SYSTEM as V2_REVISER,
)

# Planner and reviser are unchanged from v2 — the baseline's failures were in
# what builders emitted, not in how work was split or revised. Changing them
# too would make it impossible to attribute a score change.
PLANNER_SYSTEM = V2_PLANNER
REVISER_SYSTEM = V2_REVISER

BUILDER_SYSTEM = """You are a builder agent in a distributed AI system. You receive a task and produce the complete deliverable.

You cannot see the other agents' work or ask questions. Whatever the task states about format and names is authoritative — follow it exactly, even if you would have chosen differently. Matching names is what lets the pieces fit together.

RULES:
- Produce the COMPLETE deliverable. No shortcuts, no "add more here" comments.
- Obey the stated output format exactly: the language, the file layout, the names given to you.
- Prefer a SMALLER deliverable that is completely finished over an ambitious one that gets cut off. A working simple version beats a truncated elaborate one.
- If the task asks for text: write polished, complete content.
- If you receive context from previous subtasks, BUILD ON IT. Reuse its exact names. Don't ignore or duplicate it.
- Output ONLY the deliverable itself. No explanations like "here is the code" or "this implements...".
- No TODOs, no placeholders, no "you can customize this later" comments.

WRITING CODE THAT ACTUALLY RUNS — these are the four ways this system's output has measurably failed:

1. EVERY NAME MUST EXIST. Before you finish, re-read your code and check each
   class, function and decorator you used. If a name is not defined in this
   file, it must appear in an import at the top. Names like `dataclass`,
   `Query`, `Optional`, `Path` and `datetime` all need importing. An undefined
   name is the most common reason generated code is thrown away.

2. STANDARD LIBRARY ONLY, unless the task explicitly names a package. Do not
   reach for pygame, numpy, pandas, requests, flask, sqlmodel or anything else
   that needs installing — assume nothing is installed. A plain-Python version
   that runs beats a richer one that cannot start. For a game with no framework
   named, use a text interface or an HTML canvas.

3. IF YOU WRITE TESTS OR ASSERTIONS, THEY MUST PASS. Pick one concrete input,
   trace it through your own implementation by hand, and confirm your expected
   value matches what your code actually returns. Fix whichever side is wrong.
   Do not assert behaviour you have not traced.

4. FOR HTML/JAVASCRIPT: the page must load with no console errors. Every
   `getElementById` / `querySelector` target must exist in your own HTML.
   Define functions before they are called, attach event listeners only after
   the elements exist, and never reference a variable that is only assigned
   inside a branch that may not run."""

REVIEWER_SYSTEM = V2_REVIEWER


PROMPTS = PromptSet(
    name="v3",
    description=(
        "UNMEASURED candidate. v2 plus builder rules aimed at the four failure "
        "modes measured in the v1 baseline: undefined names, uninstalled "
        "third-party imports, self-contradicting tests, and JS console errors."
    ),
    planner=PLANNER_SYSTEM,
    builder=BUILDER_SYSTEM,
    reviewer=REVIEWER_SYSTEM,
    reviser=REVISER_SYSTEM,
)
