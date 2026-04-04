"""
Orchestrator: the core pipeline.

Takes a natural language task, decomposes it into subtasks (planner),
executes each subtask (builder), and reviews the assembled output (reviewer).
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import platform

from ollama_client import generate
from config import get as get_config
from ledger import log_contribution
from extract import extract_code_files

OUTPUT_DIR = Path("output")

# ── System prompts for each agent role ──────────────────────────────────

PLANNER_SYSTEM = """You are a task planner for an AI agent system. Given a project description, decompose it into 3-5 subtasks.

IMPORTANT RULES:
- Each subtask must be specific enough that a separate AI agent can complete it
- Each subtask prompt must be detailed: explain exactly what to produce, what format, what constraints
- Use depends_on to link tasks that need output from earlier tasks
- Keep subtask count between 3 and 5. Do not exceed 5.

Return ONLY valid JSON. No text before or after. No markdown fences.

Format: a JSON array of objects with these exact keys:
- "id": integer starting at 1
- "title": short name (2-5 words)
- "prompt": detailed instruction for the builder agent (at least 2 sentences)
- "depends_on": array of integer ids this depends on (use [] if none)

Example:
[{"id":1,"title":"Design data model","prompt":"Design a data model with tables, columns, and relationships for a task management app. Include user, task, and category tables with appropriate foreign keys.","depends_on":[]},{"id":2,"title":"Build API","prompt":"Create a REST API with endpoints for CRUD operations on tasks. Use the data model from the previous subtask as the foundation.","depends_on":[1]}]"""

BUILDER_SYSTEM = """You are a builder agent in a distributed AI system. You receive a task and produce the complete deliverable.

RULES:
- Produce the COMPLETE deliverable. No shortcuts, no "add more here" comments.
- If the task asks for code: write real, runnable code with all imports and functions.
- If the task asks for text: write polished, complete content.
- If you receive context from previous subtasks, BUILD ON IT. Don't ignore or duplicate it.
- Output ONLY the deliverable itself. No explanations like "here is the code" or "this implements...".
- No TODOs, no placeholders, no "you can customize this later" comments."""

REVISER_SYSTEM = """You are a code/content reviser. You receive the original task, a list of specific issues found by a reviewer, and the current assembled output.

Your job: produce a REVISED version of the output that fixes every listed issue.

RULES:
- Output ONLY the revised content — no preamble, no "here's what I changed"
- Keep everything that was working. Only fix what the issues list points to.
- If the issues mention missing code, add it. If they mention errors, fix them.
- The output must be complete and self-contained."""


REVIEWER_SYSTEM = """You are a quality reviewer for an AI agent system. You receive the original task, the plan, and all builder outputs.

Your job:
1. Check if the combined output fulfills the original request
2. Identify any gaps, errors, or inconsistencies between builder outputs
3. Produce the FINAL ASSEMBLED OUTPUT — merge ALL builder outputs into one complete, usable deliverable
4. Rate quality: PASS, NEEDS_WORK, or FAIL

RULES FOR THE FINAL ASSEMBLED OUTPUT:
- It must be COMPLETE and SELF-CONTAINED. Someone should be able to use it without reading the builder outputs.
- If builders produced code files: merge them into one working script with all imports at the top.
- If builders produced prose/docs: merge into one flowing document. Remove duplicate headings.
- Do NOT summarize — include the actual content. Do NOT say "see builder 2 output" — include it.
- Fix any obvious errors or inconsistencies you find while merging.

Respond using EXACTLY these section headers (no extra text before ## Quality Rating):

## Quality Rating
PASS

## Issues Found
None

## Final Assembled Output
[complete merged deliverable — this section must contain the full usable result]"""


# ── Pipeline functions ──────────────────────────────────────────────────

def _extract_json(text: str) -> list:
    """Pull a JSON array out of a model response, tolerating markdown fences."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    text = text.strip()

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON array found in planner output:\n{text[:300]}")
    return json.loads(text[start : end + 1])


def _validate_subtasks(subtasks: list) -> list:
    """Validate and normalize subtasks from planner. Raises ValueError on bad data."""
    if not isinstance(subtasks, list) or len(subtasks) == 0:
        raise ValueError("Planner returned empty subtask list")

    # Cap at 5
    subtasks = subtasks[:5]

    seen_ids: set[int] = set()
    cleaned = []
    for i, st in enumerate(subtasks):
        if not isinstance(st, dict):
            raise ValueError(f"Subtask {i} is not a dict")

        # Normalize id to int
        try:
            task_id = int(st.get("id", i + 1))
        except (TypeError, ValueError):
            task_id = i + 1

        title = str(st.get("title", "")).strip()
        if not title:
            raise ValueError(f"Subtask {task_id} is missing a title")

        prompt = str(st.get("prompt", "")).strip()
        if not prompt:
            raise ValueError(f"Subtask {task_id} is missing a prompt")

        # Only keep deps that reference IDs we've already seen
        raw_deps = st.get("depends_on", [])
        if not isinstance(raw_deps, list):
            raw_deps = []
        depends_on = []
        for d in raw_deps:
            try:
                dep_id = int(d)
            except (TypeError, ValueError):
                continue
            if dep_id in seen_ids:
                depends_on.append(dep_id)

        seen_ids.add(task_id)
        cleaned.append({
            "id": task_id,
            "title": title,
            "prompt": prompt,
            "depends_on": depends_on,
        })

    return cleaned


def _extract_final_output(review_text: str) -> str | None:
    """Pull the Final Assembled Output section out of a reviewer response."""
    marker = "## Final Assembled Output"
    idx = review_text.find(marker)
    if idx == -1:
        return None
    return review_text[idx + len(marker):].strip()


def _extract_rating(review_text: str) -> str:
    """Return 'PASS', 'NEEDS_WORK', or 'FAIL' from a reviewer response."""
    for line in review_text.splitlines():
        stripped = line.strip()
        if stripped in ("PASS", "NEEDS_WORK", "FAIL"):
            return stripped
    return "PASS"  # default if we can't find it


def _extract_issues(review_text: str) -> str:
    """Pull the Issues Found section, returning empty string if 'None'."""
    start_marker = "## Issues Found"
    end_marker = "## Final Assembled Output"
    start = review_text.find(start_marker)
    if start == -1:
        return ""
    end = review_text.find(end_marker, start)
    section = review_text[start + len(start_marker): end if end != -1 else None].strip()
    if section.lower() in ("none", "none.", "n/a", ""):
        return ""
    return section


async def revise(task: str, issues: str, current_output: str) -> str:
    """Run one targeted revision pass to fix issues identified by the reviewer."""
    prompt = (
        f"## Original Task\n{task}\n\n"
        f"## Issues to Fix\n{issues}\n\n"
        f"## Current Output (fix this)\n{current_output}"
    )
    return await generate(prompt, system=REVISER_SYSTEM)


# Max chars of a single builder output included in the review prompt.
# Keeps the combined review prompt from growing huge on CPU-memory-limited machines.
_MAX_BUILDER_CHARS_IN_REVIEW = 3000

# Max chars of dependency context passed into a builder prompt.
_MAX_CONTEXT_CHARS = 2000

# Minimum char length for a builder output to be considered valid.
_MIN_BUILDER_OUTPUT = 50


PLANNER_MEMORY_PREFIX = """You are continuing work on an existing project. Here is the project memory — what has been built so far:

{memory}

---

Based on this history, decompose the NEXT task into subtasks. Build on existing work; don't repeat what's already been done.

"""


async def plan(task: str, max_retries: int | None = None, memory_context: str = "") -> list[dict]:
    """Decompose a task into subtasks using the planner agent."""
    if max_retries is None:
        max_retries = get_config()["planner_retries"]
    system = PLANNER_SYSTEM
    if memory_context:
        system = PLANNER_MEMORY_PREFIX.format(memory=memory_context) + PLANNER_SYSTEM

    last_err: Exception = ValueError("no attempts made")
    for attempt in range(max_retries):
        raw = await generate(task, system=system)
        try:
            subtasks = _extract_json(raw)
            return _validate_subtasks(subtasks)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
    raise ValueError(f"Planner failed after {max_retries} attempts: {last_err}")


async def build(subtask: dict, context: str = "", max_retries: int = 2) -> str:
    """Execute a single subtask using the builder agent."""
    prompt = subtask["prompt"]
    if context:
        if len(context) > _MAX_CONTEXT_CHARS:
            context = "...[earlier context truncated]\n\n" + context[-_MAX_CONTEXT_CHARS:]
        prompt = f"Context from previous subtasks:\n{context}\n\n---\n\nYour task:\n{prompt}"

    for attempt in range(max_retries):
        output = await generate(prompt, system=BUILDER_SYSTEM)
        if len(output.strip()) >= _MIN_BUILDER_OUTPUT:
            return output
        # Output suspiciously short — retry
    return output  # return last attempt regardless


async def review(task: str, subtasks: list[dict], results: dict[int, str], memory_context: str = "") -> str:
    """Review all builder outputs against the original task."""
    parts = []
    if memory_context:
        parts.append(f"## Project History (for context)\n{memory_context}\n")
    parts.append(f"## Current Task\n{task}\n")
    parts.append("## Planned Subtasks")
    for st in subtasks:
        parts.append(f"### Subtask {st['id']}: {st['title']}\n{st['prompt']}\n")
    parts.append("## Builder Outputs")
    for st in subtasks:
        output = results[st["id"]]
        if len(output) > _MAX_BUILDER_CHARS_IN_REVIEW:
            output = output[:_MAX_BUILDER_CHARS_IN_REVIEW] + "\n\n...[output truncated]"
        parts.append(f"### Output for Subtask {st['id']}: {st['title']}\n{output}\n")

    combined = "\n".join(parts)
    return await generate(combined, system=REVIEWER_SYSTEM)


# ── Full pipeline ───────────────────────────────────────────────────────

async def run_pipeline(task: str, on_plan=None, on_build=None, on_review_start=None, project_id: str | None = None) -> dict:
    """Run the full planner -> builder -> reviewer pipeline.

    Optional callbacks for live progress:
      on_plan(subtasks)          — called after planning completes
      on_build(subtask, output)  — called after each builder finishes
      on_review_start()          — called when reviewer begins

    Returns a dict with the plan, individual results, and final review.
    """
    node_id = platform.node()  # this machine's hostname

    # Load project memory if continuing an existing project
    memory_context = ""
    if project_id:
        try:
            from memory import get_memory_context
            memory_context = get_memory_context(project_id)
        except (ImportError, FileNotFoundError):
            pass

    # 1. Plan
    subtasks = await plan(task, memory_context=memory_context)
    log_contribution(node_id, "pitch", credits=1, task=task[:100])
    if on_plan:
        on_plan(subtasks)

    # 2. Build (respecting dependencies)
    results: dict[int, str] = {}
    for st in sorted(subtasks, key=lambda s: s["id"]):
        # Gather context from dependencies
        context_parts = []
        for dep_id in st.get("depends_on", []):
            if dep_id in results:
                dep_task = next((s for s in subtasks if s["id"] == dep_id), None)
                label = dep_task["title"] if dep_task else f"Subtask {dep_id}"
                context_parts.append(f"[{label}]:\n{results[dep_id]}")
        context = "\n\n".join(context_parts)
        results[st["id"]] = await build(st, context)
        log_contribution(node_id, "compute", credits=5, task=st["title"])
        if on_build:
            on_build(st, results[st["id"]])

    # 3. Review
    if on_review_start:
        on_review_start()
    review_output = await review(task, subtasks, results, memory_context=memory_context)
    log_contribution(node_id, "compute", credits=3, task="review", details=task[:100])

    # 4. Revision pass if reviewer found issues
    rating = _extract_rating(review_output)
    final_output = _extract_final_output(review_output)
    issues = _extract_issues(review_output)

    if rating == "NEEDS_WORK" and issues and final_output:
        revised = await revise(task, issues, final_output)
        if len(revised.strip()) > len(final_output) // 2:  # sanity: not a blank response
            final_output = revised

    # 5. Save everything
    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    project_dir = OUTPUT_DIR / f"{timestamp}"
    project_dir.mkdir()

    (project_dir / "plan.json").write_text(json.dumps(subtasks, indent=2))

    for st in subtasks:
        safe_title = re.sub(r"[^\w\s-]", "", st["title"]).strip().replace(" ", "_")
        (project_dir / f"builder_{st['id']}_{safe_title}.md").write_text(results[st["id"]])

    (project_dir / "review.md").write_text(review_output)

    if final_output:
        (project_dir / "output.md").write_text(final_output)

    # Extract runnable code files from the final (possibly revised) output
    extract_source = final_output if final_output else review_output
    code_files = extract_code_files(extract_source, project_dir)

    log = {
        "task": task,
        "timestamp": timestamp,
        "plan": subtasks,
        "results": {str(k): v for k, v in results.items()},
        "review": review_output,
        "rating": rating,
        "code_files": [str(f) for f in code_files],
    }
    log["project_id"] = project_id or ""
    (project_dir / "full_log.json").write_text(json.dumps(log, indent=2))

    result = {
        "project_dir": str(project_dir),
        "plan": subtasks,
        "results": results,
        "review": review_output,
        "final_output": final_output or "",
        "rating": rating,
        "code_files": code_files,
        "project_id": project_id or "",
    }

    # Save iteration to project memory
    if project_id:
        try:
            from memory import add_iteration
            add_iteration(project_id, result, task)
        except Exception:
            pass  # memory write failure never blocks the pipeline

    return result
