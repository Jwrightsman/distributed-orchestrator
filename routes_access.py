"""Viewer sessions, canonical artifact delivery, and explicit run sharing."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Literal

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
    ArtifactIntegrityError,
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
    ExecutionShareRecordV1,
    PublicExecutionShareV1,
    artifact_manifest_for_share,
    get_share_store,
    redact_execution_for_share,
)

router = APIRouter(prefix="/v1")

SHARE_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_SHARE_TOKEN_PATH = re.compile(r"(/v1/shares/)[^/?#]+")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ViewerSessionRequest(_StrictModel):
    viewer_key: str = Field(min_length=1, max_length=4096)


class ViewerSessionResponse(_StrictModel):
    authenticated: bool
    expires_at: str


class RevokedSharesResponse(_StrictModel):
    execution_id: str
    revoked_count: int = Field(ge=0)


def redact_share_token_path(path: str) -> str:
    """Return a log-safe share route without its bearer capability."""
    return _SHARE_TOKEN_PATH.sub(r"\1<redacted>", path)


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
    if isinstance(exc, ArtifactIntegrityError):
        return HTTPException(status_code=409, detail="Artifact integrity check failed")
    return HTTPException(status_code=400, detail="Invalid artifact path or artifact tree")


def _artifact_roles(view: Literal["deliverable", "audit", "all"]):
    if view == "deliverable":
        return {"deliverable"}
    if view == "audit":
        return {"provenance", "log", "candidate_source", "internal"}
    return None


def _share_or_404(token: str):
    share = get_share_store().get_active(token)
    if share is None:
        # Invalid, revoked, and expired capabilities deliberately look alike.
        raise HTTPException(
            status_code=404,
            detail="Share not found",
            headers=SHARE_SECURITY_HEADERS,
        )
    return share


def _share_with_artifacts_or_403(token: str):
    share = _share_or_404(token)
    if not share.allow_artifact_download:
        raise HTTPException(
            status_code=403,
            detail="This share does not permit artifact access",
            headers=SHARE_SECURITY_HEADERS,
        )
    return share


def _public_share_manifest(token: str):
    share = _share_with_artifacts_or_403(token)
    execution = _execution_or_404(share.execution_id)
    manifest = get_artifact_store().get_manifest(share.execution_id)
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
async def execution_artifacts(
    execution_id: str,
    response: Response,
    role: Literal["deliverable", "audit", "all"] = "deliverable",
):
    _execution_or_404(execution_id)
    try:
        if role == "all":
            response.headers["Deprecation"] = "true"
            response.headers["Sunset"] = "compatibility-view"
        return get_artifact_store().get_manifest(
            execution_id,
            roles=_artifact_roles(role),
        )
    except (ArtifactNotFound, ArtifactLimitError, ArtifactSecurityError) as exc:
        raise _artifact_http_error(exc) from exc


@router.post("/executions/{execution_id}/artifacts/seal", response_model=ArtifactManifestV1)
async def seal_execution_artifacts(execution_id: str):
    _execution_or_404(execution_id)
    try:
        return get_artifact_store().seal_manifest(execution_id)
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
        prepared = get_artifact_store().prepare_archive(
            execution_id,
            roles={"deliverable"},
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


@router.get("/executions/{execution_id}/audit-download")
async def download_execution_audit(execution_id: str):
    _execution_or_404(execution_id)
    try:
        prepared = get_artifact_store().prepare_archive(
            execution_id,
            roles={"provenance", "log", "candidate_source", "internal"},
        )
    except (ArtifactNotFound, ArtifactLimitError, ArtifactSecurityError) as exc:
        raise _artifact_http_error(exc) from exc
    return FileResponse(
        prepared.path,
        media_type="application/zip",
        filename=prepared.download_name.replace("_artifacts.zip", "_audit.zip"),
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


@router.get(
    "/executions/{execution_id}/shares",
    response_model=list[ExecutionShareRecordV1],
)
async def list_execution_shares(execution_id: str):
    _execution_or_404(execution_id)
    return get_share_store().list_active(execution_id)


@router.delete(
    "/executions/{execution_id}/shares/{share_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_execution_share(execution_id: str, share_id: str):
    _execution_or_404(execution_id)
    if not get_share_store().revoke(execution_id, share_id):
        raise HTTPException(status_code=404, detail="Share not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/executions/{execution_id}/shares",
    response_model=RevokedSharesResponse,
)
async def revoke_all_execution_shares(execution_id: str):
    _execution_or_404(execution_id)
    count = get_share_store().revoke_all(execution_id)
    return RevokedSharesResponse(execution_id=execution_id, revoked_count=count)


@router.get("/shares/{token}", response_model=PublicExecutionShareV1)
async def public_execution_share(token: str, response: Response):
    response.headers.update(SHARE_SECURITY_HEADERS)
    share = _share_or_404(token)
    execution = _execution_or_404(share.execution_id)
    manifest = None
    try:
        complete_manifest = get_artifact_store().get_manifest(share.execution_id)
        manifest = artifact_manifest_for_share(complete_manifest, share, execution)
    except ArtifactNotFound:
        pass
    except (ArtifactLimitError, ArtifactSecurityError) as exc:
        raise _artifact_http_error(exc) from exc
    return redact_execution_for_share(execution, share, manifest=manifest, token=token)


@router.get("/shares/{token}/artifacts", response_model=ArtifactManifestV1)
async def public_share_artifacts(token: str, response: Response):
    response.headers.update(SHARE_SECURITY_HEADERS)
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
        headers={
            **SHARE_SECURITY_HEADERS,
            "ETag": f'"{entry.sha256}"',
            "X-Content-SHA256": entry.sha256,
        },
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
        headers={**SHARE_SECURITY_HEADERS, "Content-Length": str(prepared.size_bytes)},
        background=BackgroundTask(prepared.path.unlink, missing_ok=True),
    )
