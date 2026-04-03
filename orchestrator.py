"""
Orchestrator: the core pipeline.

Takes a natural language task, decomposes it into subtasks (planner),
executes each subtask (builder), and reviews the assembled output (reviewer).
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ollama_client import generate

OUTPUT_DIR = Path("output")

# ── System prompts for each agent role ──────────────────────────────────

PLANNER_SYSTEM = """You are a task planner. Given a project description, break it down into 3-5 concrete subtasks that can each be completed independently by a builder agent.

Return ONLY a JSON array of objects, each with:
- "id": integer starting at 1
- "title": short name for the subtask
- "prompt": a detailed instruction that a builder agent can execute to produce the deliverable
- "depends_on": array of subtask ids this depends on (empty array if none)

Example output:
[
  {"id": 1, "title": "Design data model", "prompt": "Design a data model for...", "depends_on": []},
  {"id": 2, "title": "Build API endpoint", "prompt": "Create a REST API...", "depends_on": [1]}
]

Return ONLY the JSON array. No markdown, no explanation."""

BUILDER_SYSTEM = """You are a builder agent. You receive a specific task and produce the deliverable.

Rules:
- Be thorough and produce complete, working output
- If the task asks for code, write real, functional code
- If the task asks for text/copy, write polished content
- Include everything needed — no placeholders or TODOs
- Output ONLY the deliverable, no meta-commentary"""

REVIEWER_SYSTEM = """You are a quality reviewer. You receive:
1. The original project description
2. The subtasks that were planned
3. The output from each builder agent

Your job:
- Check if the combined output actually fulfills the original request
- Identify any gaps, errors, or inconsistencies
- Provide a final assembled version that combines and polishes all builder outputs into a cohesive deliverable
- Rate the overall quality: PASS, NEEDS_WORK, or FAIL

Format your response as:
## Quality Rating
[PASS/NEEDS_WORK/FAIL]

## Issues Found
[list any issues, or "None"]

## Final Assembled Output
[the complete, polished deliverable combining all builder outputs]"""


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


async def plan(task: str, max_retries: int = 3) -> list[dict]:
    """Decompose a task into subtasks using the planner agent."""
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
    # 1. Plan
    subtasks = await plan(task)
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
        if on_build:
            on_build(st, results[st["id"]])

    # 3. Review
    if on_review_start:
        on_review_start()
    review_output = await review(task, subtasks, results)

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

    return {
        "project_dir": str(project_dir),
        "plan": subtasks,
        "results": results,
        "review": review_output,
    }
