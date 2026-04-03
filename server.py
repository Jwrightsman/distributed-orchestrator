"""
FastAPI server for the orchestrator.

Endpoints:
  POST /pitch  — submit a task, get back the full pipeline result
  GET  /health — check if the server and Ollama are up
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx

from orchestrator import run_pipeline
from ollama_client import OLLAMA_URL

app = FastAPI(title="Distributed AI Orchestrator", version="0.1.0")


class PitchRequest(BaseModel):
    task: str


class PitchResponse(BaseModel):
    project_dir: str
    plan: list[dict]
    results: dict[str, str]
    review: str


@app.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
        return {"status": "ok", "ollama": "connected", "models": models}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Ollama unavailable: {e}")


@app.post("/pitch", response_model=PitchResponse)
async def pitch(req: PitchRequest):
    if not req.task.strip():
        raise HTTPException(status_code=400, detail="Task cannot be empty")
    result = await run_pipeline(req.task)
    # Convert int keys to str for JSON
    result["results"] = {str(k): v for k, v in result["results"].items()}
    return result
