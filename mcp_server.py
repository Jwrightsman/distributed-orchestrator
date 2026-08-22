"""
MCP server — lets any MCP client (Claude Desktop, Claude Code, etc.) delegate
tasks to the swarm.

A thin adapter over the orchestrator's async job API: nothing here touches the
pipeline. The orchestrator must be running separately:

    python -m uvicorn server:app --host 0.0.0.0 --port 8000

Run this server (stdio, for Claude Desktop):

    python mcp_server.py

Or as a streamable-HTTP server (remote orchestrator scenarios):

    python mcp_server.py --http            # serves on 127.0.0.1:8765/mcp

Environment:
    ORCHESTRATOR_URL  where the orchestrator lives (default http://localhost:8000)
    PITCH_KEY         sent as X-Pitch-Key when the orchestrator requires one

Setup guide: docs/MCP.md
"""

import os
import sys
from typing import Annotated, Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from pydantic import Field

CandidateCount = Annotated[int, Field(ge=1, le=5)]

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8000").rstrip("/")
PITCH_KEY = os.environ.get("PITCH_KEY", "")
VIEWER_KEY = os.environ.get("VIEWER_KEY", "")

server = MCPServer(
    name="mycelium",
    instructions=(
        "Delegate coding/writing/analysis tasks to a swarm of AI agents running "
        "on volunteer hardware. pitch_task returns a job_id immediately; the swarm "
        "works in the background (minutes on CPU hardware — poll get_job_status, "
        "don't wait synchronously). Use projects to iterate: work pitched with the "
        "same project_id remembers what was built before when using DAG; "
        "ensemble/direct reject project memory until selected-result-only updates are available. Strategy choices are: "
        "dag for coordinated components, ensemble for complete alternatives, "
        "direct for one complete attempt, and auto for a conservative recorded choice."
    ),
)


def _client() -> httpx.AsyncClient:
    """One place to build the HTTP client — tests swap this for an in-process app."""
    headers = {}
    if PITCH_KEY:
        headers["X-Pitch-Key"] = PITCH_KEY
    if VIEWER_KEY:
        headers["X-Viewer-Key"] = VIEWER_KEY
    return httpx.AsyncClient(base_url=ORCHESTRATOR_URL, headers=headers, timeout=30)


def _connection_help(exc: Exception) -> str:
    return (
        f"Could not reach the orchestrator at {ORCHESTRATOR_URL} ({exc}). "
        "Is it running? Start it with: python -m uvicorn server:app --host 0.0.0.0 --port 8000"
    )


@server.tool()
async def pitch_task(
    task: str,
    project_id: str | None = None,
    strategy: Literal["auto", "dag", "ensemble", "direct"] = "auto",
    candidates: CandidateCount | None = None,
    placement: Literal["auto", "local", "distributed"] = "local",
    output_contract: dict[str, Any] | None = None,
    verification_policy: dict[str, Any] | None = None,
    confidentiality: Literal["local_only", "trusted_guild", "approved_nodes", "public"] = "local_only",
    requirements: dict[str, Any] | None = None,
) -> str:
    """Submit a task to the swarm. Returns immediately with a job_id.

    DAG builds coordinated components. Ensemble generates complete alternatives.
    Direct is one complete attempt. Auto is conservative, deterministic, and
    records its reason. Placement (local, distributed, auto) is independent.

    Args:
        task: what to build/write/analyze, in plain language (max 1000 chars)
        project_id: optional — continue an existing DAG project so the swarm
            remembers previous iterations (ensemble/direct reject this until
            selected-result-only memory updates are implemented)
        strategy: auto, dag, ensemble, or direct
        candidates: ensemble candidate count, from 1 through 5
        placement: auto, local, or distributed
        output_contract: optional bounded protocol-v1 output contract object
        verification_policy: optional validators and unverified-fallback policy
        confidentiality: local_only, trusted_guild, approved_nodes, or public
        requirements: optional capabilities, approved node ids, and fallback policy
    """
    payload: dict = {
        "task": task,
        "strategy": strategy,
        "placement": placement,
        "confidentiality": confidentiality,
    }
    if project_id:
        payload["project_id"] = project_id
    if candidates is not None:
        payload["candidates"] = candidates
    if output_contract is not None:
        payload["output_contract"] = output_contract
    if verification_policy is not None:
        payload["verification"] = verification_policy
    if requirements is not None:
        payload["requirements"] = requirements
    try:
        async with _client() as client:
            resp = await client.post("/pitch/async", json=payload)
    except httpx.HTTPError as e:
        return _connection_help(e)
    if resp.status_code == 401:
        return "The orchestrator requires a pitch key. Set the PITCH_KEY environment variable for this MCP server."
    if resp.status_code == 429:
        return "Rate limited (5 pitches/minute). Wait a minute and try again."
    if resp.status_code != 200:
        return f"Pitch failed ({resp.status_code}): {resp.text[:300]}"
    job = resp.json()
    return (
        f"Task accepted. job_id: {job['job_id']}\n"
        f"execution_id: {job.get('execution_id', 'pending')}\n"
        f"The swarm is working — this takes a few minutes. "
        f"Poll get_job_status('{job['job_id']}') to track it, then "
        f"get_result('{job['job_id']}') when complete."
    )


@server.tool()
async def get_job_status(job_id: str) -> str:
    """Check on a running job. Statuses: queued, running, complete, failed."""
    try:
        async with _client() as client:
            resp = await client.get(f"/jobs/{job_id}")
    except httpx.HTTPError as e:
        return _connection_help(e)
    if resp.status_code == 404:
        return f"No job with id {job_id}. Use pitch_task to start one."
    job = resp.json()
    status = job.get("status", "unknown")
    lines = [f"Status: {status}", f"Task: {job.get('task', '?')}"]
    if job.get("strategy_selected"):
        lines.append(
            f"Strategy: {job['strategy_selected']}"
            + (f" ({job.get('selector_reason')})" if job.get("selector_reason") else "")
        )
    if job.get("placement_selected"):
        lines.append(f"Placement: {job['placement_selected']}")
    if job.get("plan"):
        lines.append("Subtasks: " + "; ".join(st.get("title", "?") for st in job["plan"]))
    if status == "failed":
        lines.append(f"Error: {job.get('error')}")
    if status == "complete":
        lines.append(f"Rating: {job.get('rating', '?')}")
        lines.append(f"Ready — call get_result('{job_id}') for the output.")
    return "\n".join(lines)


@server.tool()
async def get_result(job_id: str) -> str:
    """Fetch final metadata and artifacts through authenticated canonical APIs."""
    try:
        async with _client() as client:
            resp = await client.get(f"/jobs/{job_id}")
            if resp.status_code == 404:
                return f"No job with id {job_id}."
            job = resp.json()
            status = job.get("status")
            if status in ("queued", "running"):
                return f"Job is still {status} — check get_job_status('{job_id}') and try again shortly."
            if status == "failed":
                return f"Job failed: {job.get('error')}"

            execution_id = job.get("execution_id")
            if not execution_id:
                return "Job is complete but has no canonical execution identifier."
            canonical = await client.get(f"/v1/executions/{execution_id}")
            if canonical.status_code != 200:
                return "Job is complete but its normalized execution record could not be read."
            normalized = canonical.json()
            manifest_response = await client.get(f"/v1/executions/{execution_id}/artifacts")
            manifest = manifest_response.json() if manifest_response.status_code == 200 else {"entries": []}
            entries = manifest.get("entries", [])
            review_metadata = normalized.get("review_metadata") or {}
            rating = review_metadata.get("rating") or job.get("rating") or "?"
            lines = [
                f"Status: {normalized.get('status', '?')}",
                f"Lifecycle: {normalized.get('lifecycle_status', normalized.get('status', '?'))}",
                f"Assurance: {normalized.get('assurance_level', 'unverified')}",
                f"Strategy: {normalized.get('strategy_selected', '?')}",
                f"Winner: {normalized.get('winning_candidate') or 'n/a'}",
                f"Rating: {rating}",
            ]
            if entries:
                lines.append("Artifacts: " + ", ".join(item["relative_path"] for item in entries))
                preferred = next(
                    (
                        item for item in entries
                        if item.get("relative_path", "").endswith("output.md")
                        and item.get("size_bytes", 0) <= 1_048_576
                    ),
                    None,
                )
                if preferred:
                    artifact = await client.get(
                        f"/v1/executions/{execution_id}/artifacts/{preferred['relative_path']}"
                    )
                    if artifact.status_code == 200:
                        lines.extend(("", artifact.text))
                        return "\n".join(lines)
                lines.append(
                    f"Authenticated archive: /v1/executions/{execution_id}/download"
                )
            else:
                lines.extend(("", normalized.get("output_preview") or "(no artifact or preview recorded)"))
            return "\n".join(lines)
    except httpx.HTTPError as e:
        return _connection_help(e)


@server.tool()
async def list_projects() -> str:
    """List persistent projects. Pitching with a project_id gives the swarm memory of previous runs."""
    try:
        async with _client() as client:
            resp = await client.get("/projects")
    except httpx.HTTPError as e:
        return _connection_help(e)
    projects = resp.json().get("projects", [])
    if not projects:
        return "No projects yet. pitch_task with continue_project, or create one by pitching through the dashboard."
    lines = []
    for p in projects:
        lines.append(
            f"- {p['project_id']}: {p.get('name', '?')} "
            f"({p.get('iteration_count', 0)} iterations, last updated {p.get('last_updated', '?')[:10]})"
        )
    return "\n".join(lines)


@server.tool()
async def continue_project(project_id: str, task: str) -> str:
    """Pitch the next iteration of an existing project — the swarm loads the
    project's memory (what was built, key decisions) before planning.

    Args:
        project_id: from list_projects
        task: the next step, e.g. "add user authentication"
    """
    try:
        async with _client() as client:
            check = await client.get(f"/projects/{project_id}")
    except httpx.HTTPError as e:
        return _connection_help(e)
    if check.status_code == 404:
        return f"No project '{project_id}'. Call list_projects to see what exists."
    return await pitch_task(task, project_id=project_id)


if __name__ == "__main__":
    if "--http" in sys.argv:
        # Streamable HTTP on 127.0.0.1:8765/mcp — for clients on other machines,
        # front it with something that terminates TLS; this binds loopback only.
        server.run(transport="streamable-http", host="127.0.0.1", port=8765)
    else:
        server.run()  # stdio — what Claude Desktop spawns
