"""Prompt set v3 — the current default. Built from the v1 baseline's actual failures.

v2 was written before any baseline existed and targets failures from the sprint
logs. v3 keeps v2's wording and adds rules aimed at the failure modes the
**measured** v1 baseline produced (run 20260806_195850, 10/28 = 36%). Every rule
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

Every candidate must be measured before promoting:

    python evals/run_evals.py --prompt-set v3

Compare against 36% (10/28) from `evals/results/20260806_195850`. If it does
not move the score, delete it. That is the rule.

---

MEASURED: v3 = 17/28 (61%) vs v1's 10/28 (36%). Promoted to default Aug 8.

**A v4 was tried and deleted — read this before writing a v5.** It kept v3
entirely and added three reviewer rules aimed at v3's largest remaining failure
group (imports of sibling modules that the merge never produced, duplicate
definitions, files that don't run as one unit). The reasoning was sound and the
rules worked: v4 fixed all the prompts it targeted — web-todo, data-log-parser,
api-url-shortener, vague-make-a-game.

It still scored **11/28 (39%)**, twenty-two points *below* v3, because it broke
ten prompts it was not aiming at — including the whole algorithm category, which
v3 had at 3/4. Run: `evals/results/20260809_053327`.

The lesson is about budget, not content: a 4B model has finite attention for
instructions. Three new mandatory operations crowded out the reviewer's ability
to do the merge it was already doing competently. **If you add a rule, cut one.**
Measure a shorter reviewer prompt before a longer one.

**A v5 was tried and deleted — and it taught us the eval's noise floor.**
v5 kept v3 entirely and inverted one planner rule: instead of splitting a
single coupled file "by concern" across blind agents, a tightly-coupled
single-file deliverable went to ONE builder whole. Run:
`evals/results/20260810_041455`.

The mechanism provably worked. Mean subtasks fell 3.68 -> 2.46, and the two
failure modes it targeted went to zero (`no_files_extracted` 2 -> 0,
`parse_failed` 1 -> 0). The planner did stop fragmenting single files.

It scored **16/28 (57%)** against v3's 17/28 (61%). It did not move the score,
so by the rule above it goes.

**The far more useful result is why "but web_app went 3/6 -> 5/6!" is not a
reason to keep it.** Comparing the per-prompt records of all four runs:

    v1 -> v3   9 up,  2 down   net +7
    v3 -> v4   4 up, 10 down   net -6
    v3 -> v5   7 up,  8 down   net -1
    v4 -> v5  10 up,  5 down   net +5

**Between any two runs, 11-18 of the 28 prompts flip outcome.** Half the set is
not stable from run to run. Whatever else these prompt sets differ by, a 4B
model re-rolls a large fraction of the outcomes every time.

That churn sets what this instrument can and cannot resolve:

- v3 vs v5 overall: 8 discordant one way, 7 the other. McNemar p ~ 1.0. The
  -1 is indistinguishable from zero.
- v3 vs v5 on web_app: 3 up, 1 down out of 6 prompts. McNemar p ~ 0.63. The
  category "gain" that motivated keeping v5 is four coin flips landing 3-1.
  It is exactly the kind of post-hoc subgroup that this project's own
  discipline exists to reject.

**Rules for anyone tuning prompts from here:**

1. **A delta of 1-3 prompts means nothing.** Do not promote on it, and do not
   keep a set "for special cases" on the strength of one category. That is the
   middle ground the rule forbids, and it is how unmeasured cruft accumulates.
2. **Only large deltas are real** at n=28 — v1 -> v3's +7 and v3 -> v4's -6 are
   the only two movements this set has ever resolved.
3. **Nobody has yet run the same prompt set twice.** The churn above is
   prompt-change and run-to-run variance mixed together, and no measurement here
   separates them. A repeat run of v3 against itself is the single most valuable
   eval this project has not done: it would give a true noise floor, and it
   costs the same ~9 hours as any other run.

v5's planner text is preserved in git history (`git show 3c4a7b0`) if a future
conditional "v6" wants it, but do not restore it on the strength of the web_app
split — that number is noise until a bigger n says otherwise.
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
        "MEASURED 17/28 (61%) vs v1 10/28 (36%). v2 plus builder rules aimed at "
        "the four failure "
        "modes measured in the v1 baseline: undefined names, uninstalled "
        "third-party imports, self-contradicting tests, and JS console errors."
    ),
    planner=PLANNER_SYSTEM,
    builder=BUILDER_SYSTEM,
    reviewer=REVIEWER_SYSTEM,
    reviser=REVISER_SYSTEM,
)
