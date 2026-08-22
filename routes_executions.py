"""Canonical versioned execution API."""

from fastapi import APIRouter, HTTPException, Request, Response, status

import server_state as state
from execution.contracts import ExecutionRequestV1, ExecutionResultV1
from execution.service import get_execution_service
from server_state import _check_pitch_key, _check_rate_limit

router = APIRouter(prefix="/v1")


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
    queued = service.submit(request_body)
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
    result = await get_execution_service().cancel(execution_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return result
