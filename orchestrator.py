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

REVIEWER_SYSTEM = """You are a quality reviewer for an AI agent system. You receive the original task, the plan, and all builder outputs.

Your job:
1. Check if the combined output fulfills the original request
2. Identify gaps, errors, or inconsistencies between builders
3. Produce a FINAL ASSEMBLED version that merges all builder outputs into one cohesive deliverable
4. Rate quality: PASS, NEEDS_WORK, or FAIL

IMPORTANT: The Final Assembled Output must be COMPLETE and USABLE. Combine all builder outputs into a single, working result. If builders produced code, merge it into one script/file. If they produced text, merge it into one document.

Format your response EXACTLY as:

## Quality Rating
PASS

## Issues Found
None

## Final Assembled Output
[the complete merged deliverable here]"""


# ── Pipeline functions ──────────────────────────────────────────────────

def _extract_json(text: str) -> list:
    """Pull a JSON array out of a model response, tolerating markdown fences."""
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    text = text.strip()

    # Find the first [ ... ] block
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON array found in planner output:\n{text[:300]}")
    return json.loads(text[start : end + 1])


async def plan(task: str, max_retries: int | None = None) -> list[dict]:
    """Decompose a task into subtasks using the planner agent."""
    if max_retries is None:
        max_retries = get_config()["planner_retries"]
    for attempt in range(max_retries):
        raw = await generate(task, system=PLANNER_SYSTEM)
        try:
            subtasks = _extract_json(raw)
            if not subtasks:
                raise ValueError("Planner returned empty subtask list")
            return subtasks
        except (ValueError, json.JSONDecodeError) as e:
            if attempt == max_retries - 1:
                raise ValueError(f"Planner failed to produce valid JSON after {max_retries} attempts: {e}")
    return []


async def build(subtask: dict, context: str = "") -> str:
    """Execute a single subtask using the builder agent."""
    prompt = subtask["prompt"]
    if context:
        prompt = f"Context from previous subtasks:\n{context}\n\n---\n\nYour task:\n{prompt}"
    return await generate(prompt, system=BUILDER_SYSTEM)


async def review(task: str, subtasks: list[dict], results: dict[int, str]) -> str:
    """Review all builder outputs against the original task."""
    parts = [f"## Original Project Description\n{task}\n"]
    parts.append("## Planned Subtasks")
    for st in subtasks:
        parts.append(f"### Subtask {st['id']}: {st['title']}\n{st['prompt']}\n")
    parts.append("## Builder Outputs")
    for st in subtasks:
        parts.append(f"### Output for Subtask {st['id']}: {st['title']}\n{results[st['id']]}\n")

    combined = "\n".join(parts)
    return await generate(combined, system=REVIEWER_SYSTEM)


# ── Full pipeline ───────────────────────────────────────────────────────

async def run_pipeline(task: str, on_plan=None, on_build=None, on_review_start=None) -> dict:
    """Run the full planner -> builder -> reviewer pipeline.

    Optional callbacks for live progress:
      on_plan(subtasks)          — called after planning completes
      on_build(subtask, output)  — called after each builder finishes
      on_review_start()          — called when reviewer begins

    Returns a dict with the plan, individual results, and final review.
    """
    node_id = platform.node()  # this machine's hostname

    # 1. Plan
    subtasks = await plan(task)
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
    review_output = await review(task, subtasks, results)
    log_contribution(node_id, "compute", credits=3, task="review", details=task[:100])

    # 4. Save everything
    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    project_dir = OUTPUT_DIR / f"{timestamp}"
    project_dir.mkdir()

    # Save plan
    (project_dir / "plan.json").write_text(json.dumps(subtasks, indent=2))

    # Save each builder output
    for st in subtasks:
        safe_title = re.sub(r"[^\w\s-]", "", st["title"]).strip().replace(" ", "_")
        (project_dir / f"builder_{st['id']}_{safe_title}.md").write_text(results[st["id"]])

    # Save review
    (project_dir / "review.md").write_text(review_output)

    # Save full log
    log = {
        "task": task,
        "timestamp": timestamp,
        "plan": subtasks,
        "results": {str(k): v for k, v in results.items()},
        "review": review_output,
    }
    (project_dir / "full_log.json").write_text(json.dumps(log, indent=2))

    # Extract runnable code files from the review output
    code_files = extract_code_files(review_output, project_dir)

    return {
        "project_dir": str(project_dir),
        "plan": subtasks,
        "results": results,
        "review": review_output,
        "code_files": code_files,
    }
