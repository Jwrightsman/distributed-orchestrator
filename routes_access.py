"""Viewer sessions, canonical artifact delivery, and explicit run sharing."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from access_control import (
    VIEWER_COOKIE_NAME,
    issue_viewer_session,
    viewer_auth_configured,
    viewer_key_matches,
)
from config import get as get_config
from execution.artifacts import (
    ArtifactLimitError,
    ArtifactManifestV1,
    ArtifactNotFound,
    ArtifactSecurityError,
    get_artifact_store,
    normalize_relative_path,
)
from execution.service import get_execution_service
from execution.sharing import (
    CreateExecutionShareV1,
    CreatedExecutionShareV1,
    PublicExecutionShareV1,
    artifact_manifest_for_share,
    get_share_store,
    redact_execution_for_share,
)

router = APIRouter(prefix="/v1")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ViewerSessionRequest(_StrictModel):
    viewer_key: str = Field(min_length=1, max_length=4096)


class ViewerSessionResponse(_StrictModel):
    authenticated: bool
    expires_at: str


def _execution_or_404(execution_id: str):
    execution = get_execution_service().get(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return execution


def _artifact_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ArtifactNotFound):
        return HTTPException(status_code=404, detail="Artifact not found")
    if isinstance(exc, ArtifactLimitError):
        return HTTPException(status_code=413, detail=str(exc))
    return HTTPException(status_code=400, detail="Invalid artifact path or artifact tree")


def _share_or_404(token: str):
    share = get_share_store().get_active(token)
    if share is None:
        # Invalid, revoked, and expired capabilities deliberately look alike.
        raise HTTPException(status_code=404, detail="Share not found")
    return share


def _share_with_artifacts_or_403(token: str):
    share = _share_or_404(token)
    if not share.allow_artifact_download:
        raise HTTPException(status_code=403, detail="This share does not permit artifact access")
    return share


def _public_share_manifest(token: str):
    share = _share_with_artifacts_or_403(token)
    execution = _execution_or_404(share.execution_id)
    manifest = get_artifact_store().refresh_manifest(share.execution_id)
    return share, artifact_manifest_for_share(manifest, share, execution)


@router.post("/viewer/session", response_model=ViewerSessionResponse)
async def create_viewer_session(body: ViewerSessionRequest, request: Request, response: Response):
    """Exchange the configured static key for a signed HttpOnly browser cookie."""
    if not viewer_auth_configured():
        raise HTTPException(status_code=409, detail="Viewer authentication is not configured")
    if not viewer_key_matches(body.viewer_key):
        raise HTTPException(
            status_code=401,
            detail="Invalid viewer credential",
            headers={"WWW-Authenticate": "Bearer"},
        )
    cookie, expires = issue_viewer_session()
    cfg = get_config()
    secure = bool(cfg.get("viewer_cookie_secure", False)) or request.url.scheme == "https"
    now = int(datetime.now(timezone.utc).timestamp())
    response.set_cookie(
        VIEWER_COOKIE_NAME,
        cookie,
        max_age=max(0, expires - now),
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    return ViewerSessionResponse(
        authenticated=True,
        expires_at=datetime.fromtimestamp(expires, timezone.utc).isoformat(),
    )


@router.delete("/viewer/session", status_code=status.HTTP_204_NO_CONTENT)
async def delete_viewer_session(response: Response):
    response.status_code = status.HTTP_204_NO_CONTENT
    response.delete_cookie(VIEWER_COOKIE_NAME, path="/", httponly=True, samesite="lax")
    return response


@router.get("/executions/{execution_id}/artifacts", response_model=ArtifactManifestV1)
async def execution_artifacts(execution_id: str):
    _execution_or_404(execution_id)
    try:
        return get_artifact_store().refresh_manifest(execution_id)
    except (ArtifactNotFound, ArtifactLimitError, ArtifactSecurityError) as exc:
        raise _artifact_http_error(exc) from exc


@router.get("/executions/{execution_id}/artifacts/{relative_path:path}")
async def execution_artifact(execution_id: str, relative_path: str):
    _execution_or_404(execution_id)
    try:
        path, entry = get_artifact_store().resolve_entry(execution_id, relative_path)
    except (ArtifactNotFound, ArtifactLimitError, ArtifactSecurityError) as exc:
        raise _artifact_http_error(exc) from exc
    return FileResponse(
        path,
        media_type=entry.media_type,
        filename=Path(entry.relative_path).name,
        headers={
            "ETag": f'"{entry.sha256}"',
            "X-Content-SHA256": entry.sha256,
            "Content-Length": str(entry.size_bytes),
        },
    )


@router.get("/executions/{execution_id}/download")
async def download_execution_artifacts(execution_id: str):
    _execution_or_404(execution_id)
    try:
        prepared = get_artifact_store().prepare_archive(execution_id)
    except (ArtifactNotFound, ArtifactLimitError, ArtifactSecurityError) as exc:
        raise _artifact_http_error(exc) from exc
    return FileResponse(
        prepared.path,
        media_type="application/zip",
        filename=prepared.download_name,
        headers={"Content-Length": str(prepared.size_bytes)},
        background=BackgroundTask(prepared.path.unlink, missing_ok=True),
    )


@router.post(
    "/executions/{execution_id}/shares",
    response_model=CreatedExecutionShareV1,
    status_code=status.HTTP_201_CREATED,
)
async def create_execution_share(execution_id: str, body: CreateExecutionShareV1):
    _execution_or_404(execution_id)
    return get_share_store().create(execution_id, body)


@router.delete(
    "/executions/{execution_id}/shares/{share_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_execution_share(execution_id: str, share_id: str):
    _execution_or_404(execution_id)
    if not get_share_store().revoke(execution_id, share_id):
        raise HTTPException(status_code=404, detail="Share not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/shares/{token}", response_model=PublicExecutionShareV1)
async def public_execution_share(token: str):
    share = _share_or_404(token)
    execution = _execution_or_404(share.execution_id)
    manifest = None
    try:
        complete_manifest = get_artifact_store().refresh_manifest(share.execution_id)
        manifest = artifact_manifest_for_share(complete_manifest, share, execution)
    except ArtifactNotFound:
        pass
    except (ArtifactLimitError, ArtifactSecurityError) as exc:
        raise _artifact_http_error(exc) from exc
    return redact_execution_for_share(execution, share, manifest=manifest, token=token)


@router.get("/shares/{token}/artifacts", response_model=ArtifactManifestV1)
async def public_share_artifacts(token: str):
    try:
        _, manifest = _public_share_manifest(token)
        return manifest
    except (ArtifactNotFound, ArtifactLimitError, ArtifactSecurityError) as exc:
        raise _artifact_http_error(exc) from exc


@router.get("/shares/{token}/artifacts/{relative_path:path}")
async def public_share_artifact(token: str, relative_path: str):
    try:
        share, manifest = _public_share_manifest(token)
        normalized = normalize_relative_path(relative_path)
        if normalized not in {entry.relative_path for entry in manifest.entries}:
            raise ArtifactNotFound("artifact was not found")
        path, entry = get_artifact_store().resolve_entry(share.execution_id, relative_path)
    except (ArtifactNotFound, ArtifactLimitError, ArtifactSecurityError) as exc:
        raise _artifact_http_error(exc) from exc
    return FileResponse(
        path,
        media_type=entry.media_type,
        filename=Path(entry.relative_path).name,
        headers={"ETag": f'"{entry.sha256}"', "X-Content-SHA256": entry.sha256},
    )


@router.get("/shares/{token}/download")
async def download_public_share_artifacts(token: str):
    try:
        share, manifest = _public_share_manifest(token)
        prepared = get_artifact_store().prepare_archive(
            share.execution_id,
            relative_paths={entry.relative_path for entry in manifest.entries},
        )
    except (ArtifactNotFound, ArtifactLimitError, ArtifactSecurityError) as exc:
        raise _artifact_http_error(exc) from exc
    return FileResponse(
        prepared.path,
        media_type="application/zip",
        filename=prepared.download_name,
        headers={"Content-Length": str(prepared.size_bytes)},
        background=BackgroundTask(prepared.path.unlink, missing_ok=True),
    )
