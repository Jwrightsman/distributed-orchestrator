"""
Orchestrator: the core pipeline.

Takes a natural language task, decomposes it into subtasks (planner),
executes each subtask (builder), and reviews the assembled output (reviewer).
"""

import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import platform

from ollama_client import generate, generate_stream, strip_thinking
from config import get as get_config
from ledger import log_contribution
from extract import check_code_files, extract_code_files
import prompts

OUTPUT_DIR = Path("output")

# ── System prompts ──────────────────────────────────────────────────────
# The four prompts live in prompts/ as frozen, named sets so tuning runs stay
# comparable. v1 is the baseline every recorded eval score refers to; select a
# different one with PROMPT_SET=..., `prompt_set` in config.json, or
# apply_prompt_set() / --prompt-set before the pipeline runs.
_ACTIVE_PROMPT_SET = prompts.get_prompt_set(prompts.resolve_default_name())

PLANNER_SYSTEM = _ACTIVE_PROMPT_SET.planner
BUILDER_SYSTEM = _ACTIVE_PROMPT_SET.builder
REVISER_SYSTEM = _ACTIVE_PROMPT_SET.reviser
REVIEWER_SYSTEM = _ACTIVE_PROMPT_SET.reviewer


def active_prompt_set() -> prompts.PromptSet:
    """Which prompt set the pipeline is currently running."""
    return _ACTIVE_PROMPT_SET


def apply_prompt_set(name: str) -> prompts.PromptSet:
    """Switch prompt sets. Call before running a pipeline, not during one.

    Also exported to the environment so anything this process spawns (a worker
    node, a server) picks up the same set rather than silently reverting to the
    default and making a comparison meaningless.
    """
    global _ACTIVE_PROMPT_SET, PLANNER_SYSTEM, BUILDER_SYSTEM, REVISER_SYSTEM, REVIEWER_SYSTEM
    chosen = prompts.get_prompt_set(name)
    _ACTIVE_PROMPT_SET = chosen
    PLANNER_SYSTEM = chosen.planner
    BUILDER_SYSTEM = chosen.builder
    REVISER_SYSTEM = chosen.reviser
    REVIEWER_SYSTEM = chosen.reviewer
    os.environ["PROMPT_SET"] = chosen.name
    return chosen


# ── Pipeline functions ──────────────────────────────────────────────────

def _extract_json(text: str) -> list:
    """Pull a JSON array out of a model response, tolerating markdown fences.

    Handles the shapes small models actually emit: a plain array (possibly in
    prose or fences), a single subtask object (wrapped in a list), and an
    envelope object like {"subtasks": [...]} (unwrapped).
    """
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    text = text.strip()

    array_start = text.find("[")
    obj_start = text.find("{")

    # Try whichever JSON value starts first — an object whose fields contain
    # arrays must not be mistaken for the plan array itself.
    candidates = []
    if array_start != -1:
        candidates.append(text[array_start : text.rfind("]") + 1])
    if obj_start != -1:
        candidates.append(text[obj_start : text.rfind("}") + 1])
    if array_start != -1 and obj_start != -1 and obj_start < array_start:
        candidates.reverse()

    last_err: Exception | None = None
    for chunk in candidates:
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError as e:
            last_err = e
            continue
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            # Envelope form ({"subtasks": [...]}) — unwrap the single list of dicts
            inner_lists = [
                v for v in parsed.values()
                if isinstance(v, list) and v and all(isinstance(item, dict) for item in v)
            ]
            if len(inner_lists) == 1:
                return inner_lists[0]
            return [parsed]

    if last_err is not None:
        raise last_err
    raise ValueError(f"No JSON found in planner output:\n{text[:300]}")


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

        # Only keep deps that reference IDs we've already seen (no forward refs)
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

    # Cycle detection via topological sort (Kahn's algorithm)
    id_set = {st["id"] for st in cleaned}
    in_degree = {st["id"]: 0 for st in cleaned}
    for st in cleaned:
        for dep in st["depends_on"]:
            if dep in id_set:
                in_degree[st["id"]] += 1

    queue = [sid for sid, deg in in_degree.items() if deg == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for st in cleaned:
            if node in st["depends_on"]:
                in_degree[st["id"]] -= 1
                if in_degree[st["id"]] == 0:
                    queue.append(st["id"])

    if visited != len(cleaned):
        raise ValueError("Planner returned a dependency cycle — retrying")

    return cleaned


def _extract_final_output(review_text: str) -> str | None:
    """Pull the Final Assembled Output section out of a reviewer response.

    Tolerates minor header variations (extra spaces, different case).
    Falls back to stripping the Quality Rating / Issues Found preamble and
    returning whatever remains — so we never lose the reviewer's work just
    because it formatted its headers slightly differently.
    """
    match = re.search(r"##\s*final assembled output\s*\n", review_text, re.IGNORECASE)
    if match:
        return review_text[match.end():].strip()

    # Fallback: strip the rating + issues preamble sections if they exist
    # and return what's left as the assembled output.
    stripped = review_text
    for pattern in (
        r"##\s*quality rating.*?(?=\n#|\Z)",
        r"##\s*issues found.*?(?=\n#|\Z)",
    ):
        stripped = re.sub(pattern, "", stripped, flags=re.IGNORECASE | re.DOTALL)
    stripped = stripped.strip()

    # Only use the fallback if something meaningful remains
    if len(stripped) >= 100:
        return stripped
    return None


def _extract_rating(review_text: str) -> str:
    """Return 'PASS', 'NEEDS_WORK', or 'FAIL' from a reviewer response."""
    # First look inside a Quality Rating section for precision
    section_match = re.search(r"##\s*quality rating\s*\n(.*?)(?=\n##|\Z)", review_text, re.IGNORECASE | re.DOTALL)
    search_text = section_match.group(1) if section_match else review_text
    for line in search_text.splitlines():
        stripped = line.strip()
        if stripped in ("PASS", "NEEDS_WORK", "FAIL"):
            return stripped
    return "PASS"  # default if we can't find it


def _extract_issues(review_text: str) -> str:
    """Pull the Issues Found section, returning empty string if 'None'."""
    start_match = re.search(r"##\s*issues found\s*\n", review_text, re.IGNORECASE)
    if not start_match:
        return ""
    end_match = re.search(r"##\s*final assembled output", review_text[start_match.end():], re.IGNORECASE)
    if end_match:
        section = review_text[start_match.end(): start_match.end() + end_match.start()].strip()
    else:
        section = review_text[start_match.end():].strip()
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


# Fallback cap on a single builder output in the review prompt, used when the
# budget can't be computed. Prefer _review_char_budget().
_MAX_BUILDER_CHARS_IN_REVIEW = 3000

# Fraction of the context window the review PROMPT may occupy. The rest is
# generation room — the reviewer has to re-emit the whole deliverable, so it
# needs at least as much space as the answer.
_REVIEW_PROMPT_FRACTION = 0.55

# Conservative chars-per-token for code and markup (real ratio is ~3-4).
_CHARS_PER_TOKEN = 3


def _review_char_budget(builder_count: int) -> int:
    """Chars of each builder's output to include in the review prompt.

    Derived from the configured context window rather than fixed, because a
    hardcoded cap silently starves the reviewer: with a 3000-char slice of a
    14000-char builder output, it never sees the code it is supposed to merge
    and assembles something worse than its own inputs (observed Aug 1 — the
    showcase reviewer emitted a game shell with no game loop).
    """
    if builder_count <= 0:
        return _MAX_BUILDER_CHARS_IN_REVIEW
    context_tokens = get_config().get("context_tokens", 8192)
    prompt_chars = context_tokens * _CHARS_PER_TOKEN * _REVIEW_PROMPT_FRACTION
    # Leave room for the task, plan, and section headers
    usable = max(prompt_chars - 2000, 1000)
    return max(int(usable // builder_count), _MAX_BUILDER_CHARS_IN_REVIEW)

# Max chars of dependency context passed into a builder prompt.
_MAX_CONTEXT_CHARS = 2000

# Minimum char length for a builder output to be considered valid.
_MIN_BUILDER_OUTPUT = 50

# Phrases that indicate the model refused the task rather than completing it.
# Checked case-insensitively at the START of trimmed output.
_REFUSAL_PREFIXES = (
    "i cannot",
    "i'm unable",
    "i am unable",
    "i'm sorry, but",
    "i apologize, but",
    "as an ai",
    "as a language model",
)


def _is_refusal(text: str) -> bool:
    """Return True if the output looks like a model refusal rather than real work."""
    lowered = text.strip().lower()
    return any(lowered.startswith(prefix) for prefix in _REFUSAL_PREFIXES)


# JSON schema for Ollama structured outputs — the runtime constrains the planner's
# sampling to this shape, so parse failures become rare instead of retried-away.
# External providers ignore it; _extract_json below remains the fallback parser.
PLANNER_FORMAT = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "title": {"type": "string"},
            "prompt": {"type": "string"},
            "depends_on": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["id", "title", "prompt", "depends_on"],
    },
}


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
    prompt = task
    for attempt in range(max_retries):
        raw = await generate(prompt, system=system, role="planner", format=PLANNER_FORMAT)
        try:
            subtasks = _extract_json(raw)
            return _validate_subtasks(subtasks)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            # On retry, inject the error so the model knows what it got wrong
            if attempt + 1 < max_retries:
                prompt = (
                    f"{task}\n\n"
                    f"IMPORTANT: Your previous response could not be parsed. Error: {e}\n"
                    f"You MUST return ONLY a valid JSON array — no markdown, no explanation, no text outside the array."
                )
    raise ValueError(f"Planner failed after {max_retries} attempts: {last_err}")


def compose_builder_prompt(subtask: dict, context: str = "", task: str = "") -> str:
    """Build the full prompt a builder agent sees for one subtask.

    Includes the overall project when known. Without it, a builder only sees
    its own subtask text and will happily satisfy it in the wrong language or
    format — e.g. writing a Python class for a subtask of "build a single
    self-contained HTML game". Shared by local and distributed dispatch so
    both paths give builders identical information.
    """
    parts = []
    if task:
        parts.append(
            f"## The overall project\n{task}\n\n"
            "Your piece below must fit that project — same language, same file "
            "format, same constraints. Do not switch technologies.\n"
        )
    if context:
        if len(context) > _MAX_CONTEXT_CHARS:
            context = "...[earlier context truncated]\n\n" + context[-_MAX_CONTEXT_CHARS:]
        parts.append(f"## Context from previous subtasks\n{context}\n")
    parts.append(f"## Your subtask\n{subtask['prompt']}")
    return "\n".join(parts)


async def build(
    subtask: dict,
    context: str = "",
    max_retries: int = 2,
    on_token=None,
    task: str = "",
) -> str:
    """Execute a single subtask using the builder agent.

    on_token(token: str) — optional callback fired for each streamed token.
    When provided, uses Ollama's streaming API for live output.

    task — the overall project text, so the builder keeps to its constraints.
    """
    base_prompt = compose_builder_prompt(subtask, context, task)

    prompt = base_prompt
    last_output = ""
    for attempt in range(max_retries):
        # A transient failure (timeout, dead Ollama runner, network blip) must
        # not kill a multi-hour pipeline — burn a retry attempt on it instead.
        try:
            if on_token is not None:
                chunks = []
                async for token in generate_stream(prompt, system=BUILDER_SYSTEM):
                    on_token(token)
                    chunks.append(token)
                # Tokens stream raw for live display; sanitize the saved result
                output = strip_thinking("".join(chunks))
            else:
                output = await generate(prompt, system=BUILDER_SYSTEM)
        except Exception:
            if attempt + 1 < max_retries:
                continue
            raise

        last_output = output
        output_ok = len(output.strip()) >= _MIN_BUILDER_OUTPUT and not _is_refusal(output)
        if output_ok:
            return output

        # Output too short or a refusal — give explicit feedback on the retry
        if attempt + 1 < max_retries:
            if _is_refusal(output):
                prompt = (
                    f"{base_prompt}\n\n"
                    f"IMPORTANT: Do not refuse this task. You are a builder agent and "
                    f"this is a safe, constructive task. Produce the complete deliverable now."
                )
            else:
                prompt = (
                    f"{base_prompt}\n\n"
                    f"IMPORTANT: Your previous response was too short or empty. "
                    f"You MUST produce the complete deliverable. No summaries, no stubs. "
                    f"Write the full result now."
                )

    return last_output  # return last attempt regardless


async def extract_and_repair(
    task: str,
    final_output: str | None,
    review_output: str,
    project_dir: Path,
    builder_outputs: dict[str, str] | None = None,
) -> tuple[str | None, list[str], list[str]]:
    """Extract code files, verify them mechanically, and repair once if broken.

    The reviewer rates prose quality and will pass code that doesn't parse, so
    the extracted files get checked for real. When defects are found we spend
    one revision quoting them, and keep the result only if it actually fixed
    more than it broke.

    Returns (final_output, code_files, remaining_problems). Writes output.md
    and the code/ directory as a side effect. Shared by the local pipeline and
    the distributed path so both produce the same guarantees.
    """
    extract_source = final_output if final_output else review_output
    code_files = extract_code_files(extract_source, project_dir)
    code_problems = check_code_files(code_files)

    if not code_problems or not final_output:
        return final_output, code_files, code_problems

    issue_text = "The extracted code does not run. Fix exactly these defects:\n" + "\n".join(
        f"- {p}" for p in code_problems
    )
    repaired = await revise(task, issue_text, final_output)
    if len(repaired.strip()) <= len(final_output) // 2:
        return final_output, code_files, code_problems  # came back truncated

    candidate_dir = project_dir / "_repair_check"
    candidate_dir.mkdir(exist_ok=True)
    try:
        candidate_files = extract_code_files(repaired, candidate_dir)
        if len(check_code_files(candidate_files)) >= len(code_problems):
            return final_output, code_files, code_problems  # no improvement
    finally:
        shutil.rmtree(candidate_dir, ignore_errors=True)

    final_output = repaired
    (project_dir / "output.md").write_text(final_output, encoding="utf-8")
    for stale in (project_dir / "code").glob("*"):
        stale.unlink()
    code_files = extract_code_files(final_output, project_dir)
    return final_output, code_files, check_code_files(code_files)


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
    budget = _review_char_budget(len(subtasks))
    for st in subtasks:
        output = results[st["id"]]
        if len(output) > budget:
            output = output[:budget] + "\n\n...[output truncated]"
        parts.append(f"### Output for Subtask {st['id']}: {st['title']}\n{output}\n")

    combined = "\n".join(parts)
    return await generate(combined, system=REVIEWER_SYSTEM, role="reviewer")


# ── Full pipeline ───────────────────────────────────────────────────────

# Hard cap on revision passes. The loop also breaks early when the reviewer is
# satisfied or the reviser returns junk, but this is the bound that guarantees
# a pitch always terminates — a reviewer that is never happy must not spin.
_MAX_REVISIONS = 2


def make_run_dir(output_dir: Path | None = None) -> tuple[str, Path]:
    """Create a fresh output/<timestamp> directory and return (timestamp, path).

    Run directories are named to the second, so two pitches that finish inside
    the same second used to collide with FileExistsError — reachable whenever
    jobs run concurrently through /pitch/async. On a collision we step forward
    a second at a time rather than adding a suffix, because the history views
    parse these names with a strict %Y%m%d_%H%M%S.
    """
    base = output_dir or OUTPUT_DIR
    base.mkdir(exist_ok=True)
    when = datetime.now(timezone.utc)
    for _ in range(120):
        timestamp = when.strftime("%Y%m%d_%H%M%S")
        project_dir = base / timestamp
        try:
            project_dir.mkdir()
            return timestamp, project_dir
        except FileExistsError:
            when += timedelta(seconds=1)
    raise RuntimeError("Could not allocate an output directory — 120 seconds all taken")


async def run_pipeline(
    task: str,
    on_plan=None,
    on_build=None,
    on_review_start=None,
    on_token=None,
    project_id: str | None = None,
    build_fn=None,
) -> dict:
    """Run the full planner -> builder -> reviewer pipeline.

    Optional callbacks for live progress:
      on_plan(subtasks)               — called after planning completes
      on_build(subtask, output)       — called after each builder finishes
      on_review_start()               — called when reviewer begins
      on_token(token, subtask)        — called per streamed token (local builds only)

    build_fn(subtask, context) -> str
      When provided, replaces the default local build() call for every subtask.
      Use this to dispatch builds to remote worker nodes. When build_fn is set,
      on_token is not called (remote nodes don't stream tokens back yet).

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

    # 2. Build in parallel waves — independent subtasks run concurrently.
    #    Each wave contains all tasks whose dependencies are already resolved.
    #    Pattern borrowed from swarms/open-multi-agent DAG execution.
    results: dict[int, str] = {}

    async def _build_one(st: dict) -> tuple[int, str]:
        context_parts = []
        for dep_id in st.get("depends_on", []):
            if dep_id in results:
                dep_task = next((s for s in subtasks if s["id"] == dep_id), None)
                label = dep_task["title"] if dep_task else f"Subtask {dep_id}"
                context_parts.append(f"[{label}]:\n{results[dep_id]}")
        context = "\n\n".join(context_parts)

        if build_fn is not None:
            # Caller-supplied dispatcher (e.g. distributed worker nodes)
            output = await build_fn(st, context)
        else:
            # Default: local Ollama inference with optional token streaming
            st_on_token = (lambda tok: on_token(tok, st)) if on_token else None
            output = await build(st, context, on_token=st_on_token, task=task)

        log_contribution(node_id, "compute", credits=5, task=st["title"])
        if on_build:
            on_build(st, output)
        return st["id"], output

    remaining = {st["id"]: st for st in subtasks}
    while remaining:
        # All tasks whose deps are fully resolved can run this wave
        wave = [st for st in remaining.values()
                if all(dep_id in results for dep_id in st.get("depends_on", []))]
        if not wave:
            break  # shouldn't happen — cycle detection already ran
        wave_results = await asyncio.gather(*[_build_one(st) for st in wave])
        for st_id, output in wave_results:
            results[st_id] = output
            remaining.pop(st_id)

    # 3. Review
    if on_review_start:
        on_review_start()
    review_output = await review(task, subtasks, results, memory_context=memory_context)
    log_contribution(node_id, "compute", credits=3, task="review", details=task[:100])

    # 4. Revision passes — up to 2 rounds of targeted fixes for NEEDS_WORK or FAIL.
    #    Each pass feeds the previous output back to the reviser with the outstanding issues.
    #    Stops early if the reviser produces a blank/truncated response (sanity check).
    rating = _extract_rating(review_output)
    final_output = _extract_final_output(review_output)
    issues = _extract_issues(review_output)

    for _rev_pass in range(_MAX_REVISIONS):
        if rating not in ("NEEDS_WORK", "FAIL") or not issues or not final_output:
            break
        revised = await revise(task, issues, final_output)
        if len(revised.strip()) <= len(final_output) // 2:
            break  # revision came back mostly empty — don't replace
        final_output = revised
        # Re-extract issues from the revised text in case it introduced new markers
        issues = _extract_issues(revised)
        # A revision pass clears the rating — if issues are gone, we're done
        if not issues:
            rating = "PASS"
            break

    # 5. Save everything
    timestamp, project_dir = make_run_dir()

    (project_dir / "plan.json").write_text(json.dumps(subtasks, indent=2), encoding="utf-8")

    for st in subtasks:
        safe_title = re.sub(r"[^\w\s-]", "", st["title"]).strip().replace(" ", "_")
        (project_dir / f"builder_{st['id']}_{safe_title}.md").write_text(results[st["id"]], encoding="utf-8")

    (project_dir / "review.md").write_text(review_output, encoding="utf-8")

    if final_output:
        (project_dir / "output.md").write_text(final_output, encoding="utf-8")

    # Extract runnable code files, then mechanically verify and repair them
    final_output, code_files, code_problems = await extract_and_repair(
        task,
        final_output,
        review_output,
        project_dir,
        builder_outputs={
            f"builder {st['id']} ({st['title']})": results[st["id"]] for st in subtasks
        },
    )

    log = {
        "task": task,
        "timestamp": timestamp,
        "plan": subtasks,
        "results": {str(k): v for k, v in results.items()},
        "review": review_output,
        "rating": rating,
        "code_files": [str(f) for f in code_files],
        "code_problems": code_problems,
        "mode": "local",
        "project_id": project_id or "",
    }
    (project_dir / "full_log.json").write_text(json.dumps(log, indent=2), encoding="utf-8")

    result = {
        "project_dir": str(project_dir),
        "plan": subtasks,
        "results": results,
        "review": review_output,
        "final_output": final_output or "",
        "rating": rating,
        "code_files": code_files,
        "code_problems": code_problems,
        "project_id": project_id or "",
    }

    # Save iteration to project memory, auto-summarize if it's grown too large
    if project_id:
        try:
            from memory import add_iteration, _summarize_memory, SUMMARIZE_THRESHOLD, PROJECTS_DIR as _PROJ_DIR
            add_iteration(project_id, result, task)
            memory_file = _PROJ_DIR / project_id / "memory.md"
            if memory_file.exists():
                raw = memory_file.read_text(errors="ignore", encoding="utf-8")
                if len(raw) > SUMMARIZE_THRESHOLD:
                    compressed = await _summarize_memory(raw)
                    if compressed and compressed != raw:
                        memory_file.write_text(compressed, encoding="utf-8")
        except Exception:
            pass  # memory write failure never blocks the pipeline

    return result
