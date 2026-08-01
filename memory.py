"""
Project Memory Bank — persistent context across pipeline runs.

Stores project state so the planner and reviewer can build on previous
iterations instead of starting from scratch every time.

Structure:
  projects/<project_id>/
    meta.json      — id, name, created_at, last_updated, iteration_count
    memory.md      — rolling context summary injected into prompts
    iterations/
      1/           — copy of output files from that run
      2/
      ...

Usage:
    from memory import create_project, load_project, list_projects
    from memory import add_iteration, get_memory_context

    project_id = create_project("todo-app", "Build a full-stack todo app")
    context    = get_memory_context(project_id)   # inject into prompts
    add_iteration(project_id, result)              # after each run
"""

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

PROJECTS_DIR = Path("projects")

# Max chars of memory context injected into prompts — keep it tight
_MAX_MEMORY_CHARS = 2000

# When memory.md exceeds this, run an auto-summarization pass
SUMMARIZE_THRESHOLD = 3000

_SUMMARIZE_SYSTEM = """You are a project memory compressor. You receive a running project memory log and compress it into a tight, factual summary that preserves all critical information: what was built, key decisions made, current state, and what still needs to be done.

RULES:
- Keep the ## Goal section intact at the top
- Summarize all iteration entries into a single ## History section (max 800 chars)
- Preserve the most recent iteration in full
- Output ONLY the compressed memory document — no preamble, no "here is the summary"
- Result must be under 1500 chars total"""


async def _summarize_memory(content: str) -> str:
    """Compress memory.md via Ollama when it grows too large.

    Returns compressed content, or original if compression failed/truncated.
    """
    try:
        from ollama_client import generate
        compressed = await generate(content, system=_SUMMARIZE_SYSTEM)
        compressed = compressed.strip()
        if len(compressed) < 200:
            return content  # sanity check — suspiciously short, keep original
        return compressed
    except Exception:
        return content  # never block on memory failure


# ── Project lifecycle ────────────────────────────────────────────────

def _slug(name: str) -> str:
    """Turn a free-form name into a safe directory name."""
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return s[:40] or "project"


def create_project(name: str, initial_task: str) -> str:
    """Create a new project and return its project_id."""
    PROJECTS_DIR.mkdir(exist_ok=True)
    base = _slug(name)
    # Make unique if slug already taken
    project_id = base
    suffix = 2
    while (PROJECTS_DIR / project_id).exists():
        project_id = f"{base}-{suffix}"
        suffix += 1

    project_dir = PROJECTS_DIR / project_id
    project_dir.mkdir()
    (project_dir / "iterations").mkdir()

    meta = {
        "project_id": project_id,
        "name": name,
        "initial_task": initial_task,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "iteration_count": 0,
    }
    (project_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    # Seed memory.md with the initial goal
    memory = f"# Project: {name}\n\n## Goal\n{initial_task}\n\n## Iterations\n_(none yet)_\n"
    (project_dir / "memory.md").write_text(memory)

    return project_id


def load_project(project_id: str) -> dict:
    """Load project metadata. Raises FileNotFoundError if not found."""
    meta_file = PROJECTS_DIR / project_id / "meta.json"
    if not meta_file.exists():
        raise FileNotFoundError(f"Project '{project_id}' not found")
    return json.loads(meta_file.read_text())


def list_projects() -> list[dict]:
    """List all projects, most recently updated first."""
    if not PROJECTS_DIR.exists():
        return []
    projects = []
    for d in PROJECTS_DIR.iterdir():
        if not d.is_dir():
            continue
        meta_file = d / "meta.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text())
            projects.append(meta)
        except (json.JSONDecodeError, OSError):
            pass
    return sorted(projects, key=lambda p: p.get("last_updated", ""), reverse=True)


def get_memory_context(project_id: str) -> str:
    """Return the memory context string to inject into prompts.

    Truncated to _MAX_MEMORY_CHARS to keep prompt sizes manageable.
    Returns empty string if project has no iterations yet.
    """
    memory_file = PROJECTS_DIR / project_id / "memory.md"
    if not memory_file.exists():
        return ""
    content = memory_file.read_text(errors="ignore")
    if "_(none yet)_" in content:
        return ""
    if len(content) > _MAX_MEMORY_CHARS:
        content = content[:_MAX_MEMORY_CHARS] + "\n\n...[earlier history truncated]"
    return content


def add_iteration(project_id: str, result: dict, task: str) -> int:
    """Record a completed pipeline run against the project.

    Updates memory.md with a summary of what was built.
    Returns the new iteration number.
    """
    project_dir = PROJECTS_DIR / project_id
    if not project_dir.exists():
        raise FileNotFoundError(f"Project '{project_id}' not found")

    meta = load_project(project_id)
    iteration = meta["iteration_count"] + 1

    # Copy output files into projects/<id>/iterations/<n>/
    iter_dir = project_dir / "iterations" / str(iteration)
    iter_dir.mkdir(parents=True, exist_ok=True)

    src = Path(result.get("project_dir", ""))
    if src.exists():
        for f in src.iterdir():
            if f.is_file():
                shutil.copy2(f, iter_dir / f.name)

    # Build a summary entry for memory.md
    plan = result.get("plan", [])
    subtask_titles = [st["title"] for st in plan]
    rating = result.get("rating", "?")
    final_output = result.get("final_output", "")
    # First 400 chars of output as a preview
    preview = final_output[:400].strip()
    if len(final_output) > 400:
        preview += "..."

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry = (
        f"\n### Iteration {iteration} — {ts}\n"
        f"**Task:** {task}\n"
        f"**Rating:** {rating}\n"
        f"**Subtasks built:** {', '.join(subtask_titles)}\n"
        f"**Output preview:**\n{preview}\n"
    )

    # Append to memory.md, replacing the "(none yet)" placeholder on first iteration
    memory_file = project_dir / "memory.md"
    memory = memory_file.read_text(errors="ignore")
    memory = memory.replace("_(none yet)_", "")
    memory = memory.rstrip() + "\n" + entry
    memory_file.write_text(memory)

    # Update meta
    meta["iteration_count"] = iteration
    meta["last_updated"] = datetime.now(timezone.utc).isoformat()
    (project_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    return iteration
