"""Canonical versioned execution API."""

from fastapi import APIRouter, HTTPException, Request, Response, status

import server_state as state
from config import get as get_config
from execution.contracts import ExecutionRequestV1, ExecutionResultV1
from execution.idempotency import InvalidIdempotencyKey, submission_identity
from execution.persistence import IdempotencyConflictError, SubmissionConsistencyError
from execution.service import ExecutionPersistenceError, get_execution_service
from server_state import _check_pitch_key, _check_rate_limit

router = APIRouter(prefix="/v1")


def _persistence_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "execution_persistence_unavailable",
            "message": (
                "Required execution state could not be committed. "
                "Verify durable state before retrying."
            ),
        },
    )


@router.post("/executions", status_code=status.HTTP_202_ACCEPTED)
async def create_execution(request_body: ExecutionRequestV1, request: Request, response: Response):
    """Queue a canonical execution and return its durable identifier."""
    _check_pitch_key(request)
    remaining = _check_rate_limit(request)
    response.headers["X-RateLimit-Limit"] = str(state._rate_limits()[0])
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    service = get_execution_service()
    try:
        service.validate_request(request_body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    idempotency_key = (
        request.headers.get("Idempotency-Key")
        if "idempotency-key" in request.headers
        else None
    )
    try:
        if idempotency_key is None:
            queued = service.submit(request_body)
        else:
            configured_pitch_key = get_config().get("pitch_key", "")
            pitch_key = str(configured_pitch_key) if configured_pitch_key else ""
            scope_kind = "pitch-key" if pitch_key else "peer-host"
            scope_value = (
                pitch_key
                if pitch_key
                else request.client.host
                if request.client is not None
                else "unknown"
            )
            identity = submission_identity(
                request_body,
                idempotency_key=idempotency_key,
                requester_scope_kind=scope_kind,
                requester_scope_value=scope_value,
            )
            submitted = service.submit_idempotent(request_body, identity)
            queued = submitted.result
            response.headers["Idempotency-Replayed"] = str(submitted.replayed).lower()
    except InvalidIdempotencyKey as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_idempotency_key", "message": str(exc)},
        ) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "idempotency_conflict",
                "message": "Idempotency-Key is already bound to a different request.",
                "execution_id": exc.execution_id,
            },
        ) from exc
    except SubmissionConsistencyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "idempotency_consistency_error",
                "message": "The existing submission mapping is temporarily unavailable.",
            },
        ) from exc
    except ExecutionPersistenceError as exc:
        raise _persistence_unavailable() from exc
    return {
        "execution_id": queued.execution_id,
        "status": queued.status,
        "protocol_version": queued.protocol_version,
        "strategy_requested": queued.strategy_requested,
        "strategy_selected": queued.strategy_selected,
        "selector_reason": queued.selector_reason,
    }


@router.get("/executions/{execution_id}", response_model=ExecutionResultV1)
async def get_execution(execution_id: str):
    """Return persisted normalized state, including after coordinator restart."""
    result = get_execution_service().get(execution_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return result


@router.post("/executions/{execution_id}/cancel", response_model=ExecutionResultV1)
async def cancel_execution(execution_id: str):
    """Idempotently request cancellation and persist a truthful terminal state."""
    try:
        result = await get_execution_service().cancel(execution_id)
    except ExecutionPersistenceError as exc:
        raise _persistence_unavailable() from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return result
