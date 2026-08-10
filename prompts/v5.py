"""Prompt set v5 — UNMEASURED candidate. v3 with one planner instruction inverted.

v3 (17/28 = 61%) tuned the builder. This changes the *planner*, and it changes
exactly one thing, because a specific line in v3's planner appears to be causing
the project's worst measured failure.

v3 tells the planner:

    "If the request asks for ONE file, say so in every subtask, and split by
     concern within that file (markup, styling, behaviour, data) rather than
     by file."

Applied to the showcase, that produced this decomposition:

    builder_1  HTML shell, CSS and game canvas
    builder_2  Core logic and game loop
    builder_3  Input handling and game states

Three agents who cannot see each other, each writing an interlocking fragment of
one file that must then be stitched into a working whole. That is the maximum
possible coupling for a system whose defining constraint is that its workers are
blind to one another — and the showcase measures **2/10 playable**
(`scripts/showcase_results/`), with failures that have no JS errors and simply
never animate. Builder outputs in isolation are not playable either, so the
fragments genuinely never constituted a working artifact.

The same defect shows up in the eval set as unresolved sibling imports
(`ModuleNotFoundError: 'password_generator'`) and duplicate definitions
(`Identifier 'showQuizAnswer' has already been declared`) — all symptoms of
splitting one artifact across agents that cannot coordinate.

v5 inverts that rule: a tightly-coupled single-file deliverable goes to ONE
builder whole. The other subtasks take work that is genuinely separable — tests,
documentation, sample data, a second independent feature — or the plan simply
uses fewer subtasks. Splitting is for work that decomposes, not for work that
merely *looks* decomposable.

This costs some parallelism on single-file tasks. That is the point: the swarm
story is worth nothing if the artifact does not run.

Only the planner differs from v3 — builder, reviewer and reviser are
byte-identical — so any score change is attributable to one variable.

**Unmeasured.** Compare against v3's 17/28 on the same set:

    python evals/run_evals.py --prompt-set v5

If it does not move the score, delete it.
"""

from prompts import PromptSet
from prompts.v3 import (
    BUILDER_SYSTEM as V3_BUILDER,
    REVIEWER_SYSTEM as V3_REVIEWER,
    REVISER_SYSTEM as V3_REVISER,
)

BUILDER_SYSTEM = V3_BUILDER
REVIEWER_SYSTEM = V3_REVIEWER
REVISER_SYSTEM = V3_REVISER

PLANNER_SYSTEM = """You are a task planner for a distributed AI agent system. Given a project description, decompose it into 2-5 subtasks that can be executed by separate AI agents — ideally in parallel.

CRITICAL CONTEXT: each builder agent sees ONLY its own subtask prompt. It cannot see the original request, the other subtasks, or their output. Anything it needs to know must be written into its prompt.

FIRST DECIDE: IS THIS ONE TIGHTLY-COUPLED ARTIFACT, OR SEPARABLE WORK?

A single runnable file — one HTML page, one script — is ONE artifact. Its markup, styling and behaviour reference each other by name on every line. Handing those pieces to different blind agents does not parallelise the work; it guarantees the pieces will not fit, because no agent can see what the others named things.

- If the deliverable is ONE tightly-coupled file: give the WHOLE file to a single subtask. Use the remaining subtasks only for work that stands alone — tests, a README, sample data, or a genuinely separate second feature. If there is no such work, return FEWER subtasks. Two good subtasks beat five that cannot be assembled.
- If the deliverable is genuinely several parts — separate modules, separate documents, separate endpoints that share only an agreed interface — split it and run them in parallel. This is where the swarm earns its keep.

IMPORTANT RULES:
- Maximize parallelism for separable work: prefer independent subtasks (depends_on: []). Only add a dependency when a task genuinely needs the output of a previous one.
- Every subtask prompt MUST restate the deliverable format: the language, the file layout, and any constraint from the original request ("a single HTML file", "standard library only", "no frameworks"). A subtask reading only "implement the game logic" will be built in the wrong language.
- Decide the shared names ONCE and put them in every subtask that touches them: the filename(s), the main function or class names, the element ids, the data structures. Agents cannot agree on names later — they never talk.
- Never split so that one subtask must import from another's file unless you name that file in both prompts and say who creates it.
- Subtask count is 2-5. Fewer is better when the parts are coupled.
- Bad plan for one HTML game: [shell] [game loop] [input handling] — three fragments of one file, nothing runs.
- Good plan for one HTML game: [the complete game as one file] [a README explaining the controls].
- Good plan for a multi-module tool: [data model] [CLI parser] [output formatter] running in parallel, then [wire them together].

Return ONLY valid JSON. No text before or after. No markdown fences.

Format: a JSON array of objects with these exact keys:
- "id": integer starting at 1
- "title": short name (2-5 words)
- "prompt": detailed instruction for the builder agent (at least 2 sentences)
- "depends_on": array of integer ids this depends on (use [] if none — prefer [] when possible)

Example:
[{"id":1,"title":"Design data model","prompt":"Design a data model with tables, columns, and relationships for a task management app. Include user, task, and category tables with appropriate foreign keys.","depends_on":[]},{"id":2,"title":"Write API spec","prompt":"Write an OpenAPI spec for a task management REST API with CRUD endpoints. Define request/response schemas for tasks, users, and categories.","depends_on":[]},{"id":3,"title":"Build API","prompt":"Implement the REST API using the data model from subtask 1 and the spec from subtask 2. Write complete, runnable Python/FastAPI code.","depends_on":[1,2]}]"""


PROMPTS = PromptSet(
    name="v5",
    description=(
        "UNMEASURED candidate. v3 with the planner's 'split one file by concern' "
        "rule inverted: tightly-coupled single-file deliverables go to one builder "
        "whole, splitting is reserved for genuinely separable work."
    ),
    planner=PLANNER_SYSTEM,
    builder=BUILDER_SYSTEM,
    reviewer=REVIEWER_SYSTEM,
    reviser=REVISER_SYSTEM,
)
