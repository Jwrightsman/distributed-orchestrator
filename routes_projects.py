"""Project routes — persistent memory across pipeline runs."""

from fastapi import APIRouter, HTTPException

from server_state import NewProjectRequest

router = APIRouter()


@router.get("/projects")
async def get_projects():
    """List all projects."""
    from memory import list_projects
    return {"projects": list_projects()}


@router.post("/projects")
async def create_new_project(req: NewProjectRequest):
    """Create a new project and return its ID."""
    from memory import create_project
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Project name cannot be empty")
    project_id = create_project(req.name.strip(), req.initial_task.strip())
    return {"project_id": project_id, "name": req.name.strip()}


@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    """Get project metadata and memory."""
    from memory import load_project, get_memory_context, PROJECTS_DIR
    try:
        meta = load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    memory = get_memory_context(project_id)
    # List iteration dirs
    iter_dir = PROJECTS_DIR / project_id / "iterations"
    iterations = sorted([d.name for d in iter_dir.iterdir() if d.is_dir()], key=lambda x: int(x)) if iter_dir.exists() else []
    return {**meta, "memory_context": memory, "iterations": iterations}
