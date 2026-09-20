"""/s/{token} — the share card a person without a viewer key sees.

HANDOFF-UI section 5 (Sept 18): sharing is an explicit, expiring, revocable
capability, and this page is its human form. It is built from exactly the
redacted view GET /v1/shares/{token} already serves to anyone holding the
token (execution/sharing.redact_execution_for_share), so it can show nothing
that JSON does not. It is rendered on the server because a link preview in a
chat app does not run scripts.

An invalid, expired and revoked token all answer the same 404, as the JSON
route does, so the page cannot be used to probe which tokens once existed.
"""

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from dashboard import render
from execution.artifacts import ArtifactLimitError, ArtifactNotFound, ArtifactSecurityError
from execution.sharing import artifact_manifest_for_share, redact_execution_for_share
from routes_access import (
    SHARE_SECURITY_HEADERS,
    _committed_artifact_manifest,
    _public_execution_or_404,
    _share_or_404,
)
from routes_run import esc

router = APIRouter()

_REPO_URL = "https://github.com/Jwrightsman/distributed-orchestrator"


def _words(value) -> str:
    return str(value).replace("_", " ") if value else ""


def _expires_in(expires_at) -> str:
    if not expires_at:
        return "no expiry · until it is revoked"
    try:
        when = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "expires"
    seconds = int((when - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "expired"
    for size, word in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"expires in {seconds // size}{word}"
    return "expires in under a minute"


# The five assurance labels HANDOFF-UI section 7 allows, keyed by the levels
# the contract has. "Behavior tested" has no level yet, so it cannot appear.
ASSURANCE_LABELS = {
    "unverified": "Not checked",
    "structural": "Structure checked",
    "deterministic": "Contract validated",
    "model_judged": "AI reviewed",
}
_OUTCOME_WORDS = {
    "passed": ("ok", "CHECKS PASSED"),
    "partial": ("warn", "SOME CHECKS FAILED"),
    "failed": ("bad", "CHECKS FAILED"),
    "not_run": ("quiet", "NOT CHECKED"),
}


def _verdict(view) -> tuple[str, str]:
    """(tone, words). Lifecycle and the checks are two claims, never one."""
    life = _words(view.lifecycle_status) or "unknown"
    if view.lifecycle_status != "completed":
        return ("bad" if view.lifecycle_status in ("failed", "cancelled", "interrupted") else "quiet",
                life.upper())
    tone, words = _OUTCOME_WORDS.get(view.validation_outcome, ("quiet", "NO CHECKS RECORDED"))
    return tone, f"COMPLETED · {words}"


@router.get("/s/{token}", response_class=HTMLResponse)
async def share_page(token: str):
    share = _share_or_404(token)
    execution = _public_execution_or_404(share.execution_id)
    manifest = None
    try:
        _, complete = _committed_artifact_manifest(share.execution_id, public=True)
        manifest = artifact_manifest_for_share(complete, share, execution)
    except (ArtifactNotFound, ArtifactLimitError, ArtifactSecurityError):
        # A manifest the page cannot read costs the file list, not the page.
        manifest = None
    view = redact_execution_for_share(execution, share, manifest=manifest, token=token)

    tone, verdict = _verdict(view)
    files = view.produced_files or []
    meta = " · ".join(filter(None, [
        _words(view.placement_selected), _words(view.strategy_selected),
        f"{len(files)} file{'' if len(files) == 1 else 's'}" if files else "",
    ]))
    preview = (
        f'<pre class="code">{esc(view.output_preview)}</pre>' if view.output_preview else
        '<p class="empty">No output preview was recorded for this run.</p>'
    )
    download = (
        f'<a class="btn btn-primary" href="/v1/shares/{esc(token)}/download" '
        f'title="Downloads this run’s deliverable files as a zip">Download the files</a>'
        if view.artifact_download_allowed and files else ""
    )
    nodes = (
        ", ".join(esc(n) for n in view.participating_nodes)
        if view.participating_nodes else "not shown on this link"
    )
    assurance = ASSURANCE_LABELS.get(view.assurance_level or "unverified", "Not checked")
    checks = (
        f"{assurance}. Nothing here ran the program, so this is not a claim that it does what "
        "was asked. Review generated code before you run it."
    )
    html = render(
        "share.html",
        SHARE_TITLE=esc(view.task[:90] or "A shared run"),
        SHARE_TASK=esc(view.task or "A shared run"),
        SHARE_EXPIRES=esc(_expires_in(view.expires_at)),
        SHARE_TONE=tone,
        SHARE_VERDICT=esc(verdict),
        SHARE_META=esc(meta),
        SHARE_PREVIEW=preview,
        SHARE_PLACEMENT=esc(_words(view.placement_selected) or "not recorded"),
        SHARE_STRATEGY=esc(_words(view.strategy_selected) or "not recorded"),
        SHARE_FILES=esc(", ".join(files[:3]) + (f" +{len(files) - 3}" if len(files) > 3 else "") or "none"),
        SHARE_NODES=nodes,
        SHARE_CHECKS=esc(checks),
        SHARE_DOWNLOAD=download,
        SHARE_REPO=esc(_REPO_URL),
    )
    return HTMLResponse(html, headers=SHARE_SECURITY_HEADERS)
