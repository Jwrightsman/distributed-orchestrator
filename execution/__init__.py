"""Versioned execution protocol, strategies, dispatch, validation, and storage."""

from execution.contracts import ExecutionRequestV1, ExecutionResultV1
from execution.service import ExecutionService, get_execution_service

__all__ = [
    "ExecutionRequestV1",
    "ExecutionResultV1",
    "ExecutionService",
    "get_execution_service",
]
