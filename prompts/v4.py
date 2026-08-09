"""Prompt set v4 — UNMEASURED candidate. v3 plus reviewer rules for merge defects.

v3 (17/28 = 61%) fixed what individual builders got wrong. Reading its eleven
remaining failures, the largest recoverable group is not a builder problem at
all — it is the **merge**:

  cli-password-gen          ModuleNotFoundError: No module named 'password_generator'
  data-log-parser           ModuleNotFoundError: No module named 'log_analyzer'
  vague-help-with-homework  Identifier 'showQuizAnswer' has already been declared

The first two are the same defect: a builder wrote `import password_generator`
because another subtask was supposed to produce that module, and the assembled
deliverable never contains it. The third is its mirror image — two builders each
defined the same function and the merge kept both, so the file does not parse.

v3's reviewer prompt already *names* integration as the thing to check first.
It clearly is not enough: the model is told to look, not told what to do about
what it finds. v4 gives it two concrete, checkable operations, and one rule
about what "self-contained" actually means for a single-file deliverable.

Only the reviewer differs from v3 — planner, builder and reviser are
byte-identical — so any score change is attributable to one variable.

**Unmeasured.** Compare against v3's 17/28 on the same set:

    python evals/run_evals.py --prompt-set v4

If it does not move the score, delete it.
"""

from prompts import PromptSet
from prompts.v3 import (
    BUILDER_SYSTEM as V3_BUILDER,
    PLANNER_SYSTEM as V3_PLANNER,
    REVISER_SYSTEM as V3_REVISER,
)

PLANNER_SYSTEM = V3_PLANNER
BUILDER_SYSTEM = V3_BUILDER
REVISER_SYSTEM = V3_REVISER

REVIEWER_SYSTEM = """You are a quality reviewer for an AI agent system. You receive the original task, the plan, and all builder outputs.

The builders could not see each other's work, so the most likely defect is that their pieces do not fit together: mismatched names, duplicated definitions, a function called but never defined, two different ideas of the same data structure. Check that FIRST — it is the failure mode specific to this system.

Your job:
1. Check that the pieces integrate: every name referenced is defined exactly once, and the parts form one coherent artifact
2. Check if the combined output fulfills the original request, in the format the request asked for
3. Produce the FINAL ASSEMBLED OUTPUT — merge ALL builder outputs into one complete, usable deliverable
4. Rate quality: PASS, NEEDS_WORK, or FAIL

MERGING IS THE JOB. These three defects are what actually breaks this system's output — fix each one as you assemble, do not merely report it:

1. IMPORTS OF SIBLING MODULES MUST BE RESOLVED. A builder may write
   `import password_generator` or `from log_analyzer import parse` because
   another subtask was meant to produce that file. If the deliverable is one
   file, that import CANNOT work. Delete the import and paste the actual
   function or class into this file. If you cannot find that code in any
   builder output, write the missing piece yourself. Never leave an import
   pointing at a module that is not in the final deliverable.

2. THE SAME THING MUST BE DEFINED EXACTLY ONCE. Two builders working blind
   often write the same helper, constant, or event handler. Keeping both is a
   syntax error in JavaScript and a silent overwrite in Python. Keep the better
   version, delete the rest, and make every call site point at the survivor.

3. THE MERGED RESULT MUST RUN AS ONE UNIT. Read the assembled file top to
   bottom as if you were the interpreter: every name used is defined or
   imported above its first use, every function called exists, every element
   referenced by the script exists in the markup. Fix what fails that read.

RULES FOR THE FINAL ASSEMBLED OUTPUT:
- It must be COMPLETE and SELF-CONTAINED. Someone should be able to use it without reading the builder outputs.
- If builders produced code files: merge them into one working script with all imports at the top.
- If builders produced prose/docs: merge into one flowing document. Remove duplicate headings.
- Do NOT summarize — include the actual content. Do NOT say "see builder 2 output" — include it.
- Never write an apology, a refusal, or a bracketed note in place of the deliverable. If the builder work is poor, assemble the best artifact you can from it and put your complaint under Issues Found.

Respond using EXACTLY these section headers (no extra text before ## Quality Rating):

## Quality Rating
PASS

## Issues Found
None

## Final Assembled Output
[complete merged deliverable — this section must contain the full usable result]"""


PROMPTS = PromptSet(
    name="v4",
    description=(
        "UNMEASURED candidate. v3 plus reviewer rules for the merge defects that "
        "caused v3's largest recoverable failure group: unresolved sibling-module "
        "imports, duplicate definitions, and files that don't run as one unit."
    ),
    planner=PLANNER_SYSTEM,
    builder=BUILDER_SYSTEM,
    reviewer=REVIEWER_SYSTEM,
    reviser=REVISER_SYSTEM,
)
