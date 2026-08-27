"""
Node management and task distribution routes.

Workers register here, long-poll for work, and submit results. The circuit
breaker (consecutive-failure blacklist) also lives on this surface.
"""

import asyncio
import math
import secrets
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from access_control import require_viewer
from capability_evidence import (
    FUTURE_ACTIVE_EXPERIMENT_BLOCKING_REASON_ORDER,
    future_active_experiment_eligibility,
)
from execution.attempts import (
    DEFAULT_MAX_OUTPUT_BYTES,
    AttemptRejected,
    WorkerPayloadLimitExceeded,
)
from ledger import sync_compatibility_ledger
import node_capabilities
from node_enrollments import (
    EnrollmentAuthenticationFailed,
    EnrollmentCredentialConflict,
    EnrollmentLabelConflict,
    EnrollmentNotFound,
    EnrollmentRevoked,
    EnrollmentRotationConflict,
    InvalidEnrollmentCredential,
    NodeEnrollmentError,
)
from node_sessions import DuplicateNodeSession, NodeSessionDescriptorConflict
import server_state as state
from server_state import (
    NodeRegistration,
    TaskResult,
    TokenBatch,
    _check_node_auth,
    _check_node_session,
    _emit,
    _safe_diagnostic_emit,
    node_blacklist,
    node_failure_count,
    nodes,
    task_inflight,
    task_queue,
    task_results,
)

router = APIRouter()


def _clear_assignment(task: dict) -> None:
    for field in (
        "assigned_to",
        "assigned_session_id",
        "assigned_enrollment_id",
        "assigned_credential_version",
        "assigned_descriptor_version",
        "assigned_descriptor_hash",
        "selected_model",
        "assigned_at",
        "attempt_id",
        "nonce",
        "lease_expires_at",
    ):
        task.pop(field, None)


def _descriptor_for_session(session_record):
    """Resolve the exact claim immutably bound to an authenticated session."""

    descriptor_hash = session_record.capability_descriptor_hash
    descriptor_version = session_record.capability_descriptor_version
    if descriptor_hash is None:
        return None
    if session_record.enrollment_id is not None:
        snapshot = state.capability_snapshot_store.get(
            session_record.enrollment_id, descriptor_hash
        )
        if snapshot is None or snapshot.descriptor_version != descriptor_version:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "node_capability_snapshot_unavailable",
                    "message": "the registered capability snapshot is unavailable",
                    "action": "register_again",
                },
            )
        return snapshot.descriptor

    node = nodes.get(session_record.node_id, {})
    descriptor = node.get("capability_descriptor")
    if descriptor is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "node_capability_snapshot_unavailable",
                "message": "the registered capability claim is unavailable",
                "action": "register_again",
            },
        )
    parsed = node_capabilities.NodeCapabilityDescriptorV1.model_validate(descriptor)
    if (
        parsed.descriptor_version != descriptor_version
        or node_capabilities.capability_descriptor_digest(parsed) != descriptor_hash
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "node_capability_snapshot_inconsistent",
                "message": "the registered capability claim is inconsistent",
                "action": "register_again",
            },
        )
    return parsed


def _reclaim_replaced_session(node_id: str, replaced_session_id: str) -> None:
    """Close and requeue work that belonged to a replaced stale session."""

    with state._task_queue_lock:
        task_ids = [
            task_id
            for task_id, task in task_inflight.items()
            if task.get("assigned_to") == node_id
            and task.get("assigned_session_id") == replaced_session_id
        ]
        for task_id in task_ids:
            task = task_inflight.get(task_id)
            if task is None:
                continue
            attempt_id = task.get("attempt_id")
            try:
                changed = bool(
                    attempt_id
                    and state.attempt_store.transition_active(
                        attempt_id=attempt_id,
                        state="reclaimed",
                        reason="assigned node session was replaced after staleness",
                        terminal_cause="session_replaced",
                    )
                )
            except Exception as exc:
                _safe_diagnostic_emit(
                    "attempt_reclaim_failed",
                    {
                        "task_id": task_id,
                        "node_id": node_id,
                        "enrollment_id": task.get("assigned_enrollment_id"),
                        "phase": "session_replacement",
                        "error_type": type(exc).__name__,
                    },
                )
                continue
            if attempt_id and not changed:
                continue
            task = task_inflight.pop(task_id)
            enrollment_id = task.get("assigned_enrollment_id")
            _clear_assignment(task)
            deadline = task.get("execution_deadline_at")
            if not deadline or float(deadline) > time.time():
                task_queue.append(task)
            _emit("task_reclaimed", {
                "task_id": task_id,
                "node_id": node_id,
                "enrollment_id": enrollment_id,
                "reason": "node session replaced after staleness",
            })


def _verification_key(node_id: str) -> str | None:
    """Return the legacy sampled-agreement key for this live identity."""

    node = nodes.get(node_id, {})
    try:
        from verification import verification_identity_key

        return verification_identity_key(
            enrollment_id=node.get("enrollment_id"),
            session_id=node.get("session_id"),
        )
    except ImportError:  # Rolling-source compatibility during the additive change.
        return None


def _lifetime_summary(
    node_id: str,
    enrollment_id: str | None,
    session_id: str | None = None,
) -> dict:
    if enrollment_id:
        return state.attempt_store.lifetime_contribution_summary(
            node_id, enrollment_id=enrollment_id
        )
    if session_id:
        return state.attempt_store.lifetime_contribution_summary(
            node_id, session_id=session_id
        )
    # Only rows that predate explicit enrollment/session attribution are
    # eligible for the historical label-only view.  Never merge those rows
    # into a newly registered legacy session merely because its label matches.
    return state.attempt_store.lifetime_contribution_summary(node_id)


def _raise_enrollment_http_error(exc: NodeEnrollmentError) -> None:
    if isinstance(
        exc,
        (
            EnrollmentLabelConflict,
            EnrollmentCredentialConflict,
            EnrollmentRotationConflict,
        ),
    ):
        status_code = 409
    elif isinstance(exc, EnrollmentRevoked):
        status_code = 403
    elif isinstance(exc, EnrollmentAuthenticationFailed):
        status_code = 401
    elif isinstance(exc, EnrollmentNotFound):
        status_code = 404
    elif isinstance(exc, InvalidEnrollmentCredential):
        status_code = 422
    else:
        status_code = 401
    raise HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.reason},
    ) from exc


def _should_defer(_node_id: str, _waiting_since: float) -> bool:
    """Deprecated compatibility hook; sampled agreement never defers work."""

    return False


@router.post("/nodes/register")
async def register_node(reg: NodeRegistration, request: Request):
    descriptor = reg.capability_descriptor
    if descriptor is not None and not any(
        model.provider == "ollama" and model.name == reg.model
        for model in descriptor.models
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "node_capability_descriptor_model_mismatch",
                "message": (
                    "the legacy configured model must appear in the capability "
                    "descriptor model list"
                ),
            },
        )
    enrollment = None
    enrollment_idempotent = False
    effective_action = reg.enrollment_action

    if reg.enrollment_action is None:
        # Preserve the legacy path only in the explicit local compatibility
        # mode. Validate admission first so an old trusted-alpha worker receives
        # the actionable upgrade response only after proving it has the invite.
        _check_node_auth(request)
        if state.node_enrollment_required():
            raise HTTPException(
                status_code=426,
                detail={
                    "code": "durable_node_enrollment_required",
                    "message": (
                        "This coordinator requires durable node enrollment. "
                        "Upgrade the worker and register with enrollment_action "
                        "and an enrollment credential."
                    ),
                    "action": "upgrade_worker",
                },
                headers={"X-Node-Enrollment-Required": "true"},
            )
        if reg.enrollment_credential is not None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "node_enrollment_action_required",
                    "message": "enrollment_action is required when a credential is supplied",
                },
            )
        if state.enrollment_store.get_by_node(reg.node_id) is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "node_enrollment_label_conflict",
                    "message": (
                        "node label belongs to a durable enrollment; authenticate "
                        "with its enrollment credential"
                    ),
                },
            )
        effective_action = "legacy_compat"
    elif reg.enrollment_action == "bootstrap":
        _check_node_auth(request)
        if reg.enrollment_credential is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_enrollment_credential",
                    "message": "bootstrap requires an enrollment credential",
                },
            )
        if descriptor is None:
            raise HTTPException(
                status_code=426,
                detail={
                    "code": "node_capability_descriptor_required",
                    "message": (
                        "Durably enrolled workers must submit a typed capability "
                        "descriptor. Upgrade the worker and register again."
                    ),
                    "action": "upgrade_worker",
                },
                headers={"X-Node-Capability-Descriptor-Required": "true"},
            )
        try:
            bootstrapped = state.enrollment_store.bootstrap(
                reg.node_id, reg.enrollment_credential
            )
        except NodeEnrollmentError as exc:
            _raise_enrollment_http_error(exc)
        enrollment = bootstrapped.record
        enrollment_idempotent = bootstrapped.idempotent
    elif reg.enrollment_action == "returning":
        if reg.enrollment_credential is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_enrollment_credential",
                    "message": "returning registration requires an enrollment credential",
                },
            )
        try:
            enrollment = state.enrollment_store.authenticate(
                reg.node_id, reg.enrollment_credential
            )
        except NodeEnrollmentError as exc:
            _raise_enrollment_http_error(exc)
        if descriptor is None:
            raise HTTPException(
                status_code=426,
                detail={
                    "code": "node_capability_descriptor_required",
                    "message": (
                        "Durably enrolled workers must submit a typed capability "
                        "descriptor. Upgrade the worker and register again."
                    ),
                    "action": "upgrade_worker",
                },
                headers={"X-Node-Capability-Descriptor-Required": "true"},
            )

    descriptor_version = descriptor.descriptor_version if descriptor else None
    descriptor_hash = (
        node_capabilities.capability_descriptor_digest(descriptor)
        if descriptor is not None
        else None
    )
    snapshot = None

    def _remember_accepted_descriptor() -> None:
        nonlocal snapshot, descriptor_version, descriptor_hash
        if enrollment is None or descriptor is None:  # pragma: no cover - guarded call
            return
        snapshot = state.capability_snapshot_store.remember(
            enrollment.enrollment_id, descriptor
        )
        descriptor_version = snapshot.descriptor_version
        descriptor_hash = snapshot.descriptor_hash

    try:
        grant = state.node_sessions.register(
            reg.node_id,
            enrollment_id=enrollment.enrollment_id if enrollment else None,
            credential_version=enrollment.credential_version if enrollment else None,
            capability_descriptor_version=descriptor_version,
            capability_descriptor_hash=descriptor_hash,
            presented_token=request.headers.get("X-Node-Session"),
            before_grant=(
                _remember_accepted_descriptor if enrollment is not None else None
            ),
        )
    except NodeSessionDescriptorConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "node_capability_descriptor_conflict",
                "message": str(exc),
                "action": "drain_or_establish_new_session",
            },
        ) from exc
    except DuplicateNodeSession as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "node_id_in_use",
                "message": str(exc),
                "reclaim_after_seconds": state._NODE_TIMEOUT,
            },
        ) from exc

    if grant.replaced_session_id:
        _reclaim_replaced_session(reg.node_id, grant.replaced_session_id)

    # Auto-add a "model:<name>" capability tag so tasks can soft-route by model.
    claimed_caps = list(reg.capabilities)
    compatibility_caps: list[str] = []
    caps = list(claimed_caps)
    model_tag = f"model:{reg.model}"
    if model_tag not in caps:
        caps.append(model_tag)
        compatibility_caps.append(model_tag)
    existing = nodes.get(reg.node_id) if grant.idempotent else None
    lifetime = _lifetime_summary(
        reg.node_id,
        enrollment.enrollment_id if enrollment else None,
        grant.record.session_id,
    )
    session_tasks = int((existing or {}).get("session_tasks_completed", 0))
    session_points = float((existing or {}).get("session_contribution_points", 0))
    session_metadata = grant.record.public_metadata()
    nodes[reg.node_id] = {
        "node_id": reg.node_id,
        "enrollment_id": enrollment.enrollment_id if enrollment else None,
        "credential_version": enrollment.credential_version if enrollment else None,
        "enrollment_status": enrollment.status if enrollment else "unenrolled",
        "enrolled": enrollment is not None,
        "model": reg.model,
        "platform": reg.platform,
        "machine": reg.machine,
        "hostname": reg.hostname,
        "cpu_count": reg.cpu_count,
        "ram_gb": reg.ram_gb,
        "gpu": reg.gpu,
        "capability_descriptor": (
            descriptor.model_dump(mode="json") if descriptor is not None else None
        ),
        "capability_descriptor_version": descriptor_version,
        "capability_descriptor_hash": descriptor_hash,
        "claimed_capabilities": claimed_caps,
        "server_compatibility_capabilities": compatibility_caps,
        "capabilities": caps,
        "registered_at": (existing or {}).get(
            "registered_at", datetime.now(timezone.utc).isoformat()
        ),
        "last_seen": time.time(),
        "session_id": grant.record.session_id,
        "session_started_at": session_metadata["session_started_at"],
        "session_expires_at": session_metadata["session_expires_at"],
        "session_tasks_completed": session_tasks,
        "session_contribution_points": session_points,
        **lifetime,
        # Compatibility aliases for older dashboards.  These are session, not
        # lifetime, values and new clients should use the explicit names above.
        "tasks_completed": session_tasks,
        "credits_earned": session_points,
        "current_task": (existing or {}).get("current_task"),
        "draining": False,
    }
    return {
        "message": f"Welcome, {reg.node_id}. You are node #{len(nodes)} in the network.",
        "node_id": reg.node_id,
        "enrollment_id": enrollment.enrollment_id if enrollment else None,
        "credential_version": enrollment.credential_version if enrollment else None,
        "enrolled": enrollment is not None,
        "enrollment_action": effective_action,
        "enrollment_idempotent": enrollment_idempotent,
        "capability_descriptor": (
            descriptor.model_dump(mode="json") if descriptor is not None else None
        ),
        "capability_descriptor_version": descriptor_version,
        "capability_descriptor_hash": descriptor_hash,
        "claimed_capabilities": claimed_caps,
        "server_compatibility_capabilities": compatibility_caps,
        "capabilities": caps,
        "session_id": grant.record.session_id,
        "session_token": grant.session_token,
        "session_expires_at": session_metadata["session_expires_at"],
        "session_started_at": session_metadata["session_started_at"],
        "idempotent": grant.idempotent or enrollment_idempotent,
        "session_idempotent": grant.idempotent,
        **lifetime,
    }


@router.get("/nodes")
async def list_nodes():
    """Connected nodes without process-local sampled-agreement records."""

    out = []
    for n in nodes.values():
        lifetime = _lifetime_summary(
            n["node_id"], n.get("enrollment_id"), n.get("session_id")
        )
        n.update(lifetime)
        safe_node = {
            key: value
            for key, value in n.items()
            if key != "capability_descriptor"
        }
        out.append(safe_node)
    return {
        "nodes": out,
        "count": len(nodes),
        "verify_rate": state.verification_pool.verify_rate,
    }


@router.get("/v1/operator/node-enrollments")
async def list_node_enrollments(request: Request):
    """Protected secret-free durable enrollment and live-session inventory."""

    require_viewer(request)
    raw_requirements = request.query_params.get("resource_requirements")
    if raw_requirements is not None:
        if len(raw_requirements.encode("utf-8")) > 16_384:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_resource_requirements",
                    "message": "resource_requirements must be 16384 bytes or fewer",
                },
            )
        try:
            diagnostic_requirements = (
                node_capabilities.NodeResourceRequirementsV1.model_validate_json(
                    raw_requirements
                )
            )
        except ValidationError as exc:
            errors = exc.errors()
            error_type = str(errors[0].get("type", "")) if errors else ""
            code = (
                error_type
                if error_type.startswith("unsupported_")
                else "invalid_resource_requirements"
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "code": code,
                    "message": "resource_requirements is invalid",
                },
            ) from exc
    else:
        diagnostic_requirements = None
    raw_output_capacity = request.query_params.get(
        "required_output_capacity_bytes"
    )
    if raw_output_capacity is None:
        diagnostic_output_capacity = None
    else:
        try:
            diagnostic_output_capacity = int(raw_output_capacity)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_required_output_capacity",
                    "message": (
                        "required_output_capacity_bytes must be an integer"
                    ),
                },
            ) from exc
        if not 1 <= diagnostic_output_capacity <= node_capabilities.MAX_NODE_OUTPUT_BYTES:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_required_output_capacity",
                    "message": (
                        "required_output_capacity_bytes is outside the protocol limit"
                    ),
                },
            )
    diagnostic_legacy = [
        value.strip()
        for value in request.query_params.getlist("required_capability")
    ]
    if (
        len(diagnostic_legacy) > 16
        or any(not value or len(value) > 128 for value in diagnostic_legacy)
        or len(set(diagnostic_legacy)) != len(diagnostic_legacy)
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_required_capabilities",
                "message": "required_capability values must be unique bounded tags",
            },
        )
    out = []
    records = state.enrollment_store.list()
    for enrollment in records:
        node = nodes.get(enrollment.node_id)
        if node is not None and node.get("enrollment_id") != enrollment.enrollment_id:
            node = None
        session = state.node_sessions.current(enrollment.node_id)
        if session is not None and session.enrollment_id != enrollment.enrollment_id:
            session = None
        session_metadata = session.public_metadata() if session is not None else {}
        snapshots = state.capability_snapshot_store.list_for_enrollment(
            enrollment.enrollment_id
        )
        snapshot = None
        if session is not None and session.capability_descriptor_hash is not None:
            snapshot = state.capability_snapshot_store.get(
                enrollment.enrollment_id,
                session.capability_descriptor_hash,
            )
        elif session is None and snapshots:
            snapshot = max(
                snapshots,
                key=lambda item: (item.last_seen_at, item.descriptor_hash),
            )
        legacy_capabilities = list((node or {}).get("capabilities", []))
        capability_match = node_capabilities.match_node_requirements(
            diagnostic_requirements,
            diagnostic_legacy,
            snapshot.descriptor if snapshot is not None else None,
            legacy_capabilities,
            preferred_model_name=(node or {}).get("model"),
            required_output_capacity_bytes=diagnostic_output_capacity,
        )
        lifetime = _lifetime_summary(enrollment.node_id, enrollment.enrollment_id)
        out.append(
            {
                **enrollment.public_metadata(),
                "live_session_present": session is not None,
                "session_id": session_metadata.get("session_id"),
                "session_started_at": session_metadata.get("session_started_at"),
                "session_expires_at": session_metadata.get("session_expires_at"),
                "last_seen": session_metadata.get("last_seen"),
                "draining": bool(node and node.get("draining")),
                "current_task": node.get("current_task") if node else None,
                "capability_descriptor_version": (
                    snapshot.descriptor_version if snapshot is not None else None
                ),
                "capability_descriptor_hash": (
                    snapshot.descriptor_hash if snapshot is not None else None
                ),
                "capability_descriptor": (
                    snapshot.descriptor.model_dump(mode="json")
                    if snapshot is not None
                    else None
                ),
                "capability_descriptor_is_live": bool(
                    session is not None
                    and snapshot is not None
                    and session.capability_descriptor_hash == snapshot.descriptor_hash
                ),
                "capability_snapshot_count": len(snapshots),
                "legacy_capability_tags": legacy_capabilities,
                "claimed_legacy_capability_tags": list(
                    (node or {}).get("claimed_capabilities", [])
                ),
                "server_compatibility_tags": list(
                    (node or {}).get("server_compatibility_capabilities", [])
                ),
                "hard_requirement_eligibility": capability_match.as_dict(),
                **lifetime,
            }
        )
    return {
        "enrollments": out,
        "count": len(out),
        "active_count": sum(item.status == "active" for item in records),
        "revoked_count": sum(item.status == "revoked" for item in records),
    }


@router.get("/v1/operator/capability-evidence")
async def list_capability_evidence(request: Request):
    """Protected aggregate observations and shadow-only diagnostics."""

    require_viewer(request)
    try:
        raw_limit = request.query_params.get("limit", "100")
        limit = int(raw_limit)
        enrollment_id = request.query_params.get("enrollment_id")
        descriptor_hash = request.query_params.get("descriptor_hash")
        task_class = request.query_params.get("task_class")
        evidence_role = request.query_params.get("evidence_role", "production")
        raw_window_started_at = request.query_params.get("window_started_at")
        raw_window_ended_at = request.query_params.get("window_ended_at")
        window_started_at = (
            float(raw_window_started_at)
            if raw_window_started_at is not None
            else None
        )
        window_ended_at = (
            float(raw_window_ended_at)
            if raw_window_ended_at is not None
            else None
        )
        for value in (window_started_at, window_ended_at):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("operational-health window timestamps are invalid")
        cfg = state.get_config()
        minimum_samples = int(cfg.get("capability_evidence_min_samples", 5))
        summaries = state.capability_evidence_store.list_scope_aggregates(
            enrollment_id=enrollment_id,
            descriptor_hash=descriptor_hash,
            task_class=task_class,
            role=evidence_role,
            limit=limit,
            minimum_samples=minimum_samples,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_capability_evidence_query",
                "message": "the capability-evidence query is invalid",
            },
        ) from exc

    shadow = ()
    shadow_decisions_available = True
    try:
        shadow = await asyncio.to_thread(
            state.capability_shadow_decision_store.aggregate_counts_for_scope_keys,
            [summary.scope.scope_key for summary in summaries],
        )
    except Exception as exc:
        shadow_decisions_available = False
        state.capability_shadow_process_counters.increment(
            "unexpected_containment_failure"
        )
        state.logger.warning(
            "capability shadow decision report unavailable error_type=%s",
            type(exc).__name__,
        )

    operational = None
    try:
        operational = await asyncio.to_thread(
            state.capability_shadow_operational_store.report,
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
        )
    except Exception as exc:
        state.capability_shadow_process_counters.increment(
            "unexpected_containment_failure"
        )
        state.logger.warning(
            "capability shadow operational report unavailable error_type=%s",
            type(exc).__name__,
        )
    process_local = state.capability_shadow_process_counters.snapshot()

    shadow_by_scope = {item.actual_scope_key: item for item in shadow}

    def binary(item):
        return {
            "sample_count": item.sample_count,
            "positive_count": item.positive_count,
            "negative_count": item.negative_count,
            "rate": item.rate,
            "wilson_interval": {
                "low": item.wilson_low,
                "high": item.wilson_high,
            },
        }

    scopes = []
    future_active_reason_counts = {
        reason: 0 for reason in FUTURE_ACTIVE_EXPERIMENT_BLOCKING_REASON_ORDER
    }
    future_active_eligible_count = 0
    for summary in summaries:
        scope = summary.scope
        aggregate = summary.aggregate
        shadow_counts = shadow_by_scope.get(scope.scope_key)
        try:
            snapshot = state.capability_snapshot_store.get(
                scope.enrollment_id,
                scope.descriptor_hash,
            )
            descriptor_identity_reconstructable = bool(
                snapshot is not None
                and snapshot.descriptor_version == scope.descriptor_version
                and snapshot.descriptor.executor.kind == scope.executor_kind
                and snapshot.descriptor.executor.version == scope.executor_version
                and snapshot.descriptor.executor.worker_protocol_version
                == scope.worker_protocol_version
                and node_capabilities.capability_descriptor_digest(
                    snapshot.descriptor
                )
                == scope.descriptor_hash
            )
            model_identity_reconstructable = bool(
                snapshot is not None
                and len(
                    [
                        model
                        for model in snapshot.descriptor.models
                        if model.provider == scope.model_provider
                        and model.name == scope.model_name
                        and model.digest == scope.model_digest
                        and model.variant == scope.model_variant
                    ]
                )
                == 1
            )
        except Exception:
            descriptor_identity_reconstructable = False
            model_identity_reconstructable = False
        identity_diagnostic = future_active_experiment_eligibility(
            scope,
            descriptor_identity_reconstructable=(
                descriptor_identity_reconstructable
            ),
            model_identity_reconstructable=model_identity_reconstructable,
        )
        if identity_diagnostic.eligible_for_future_active_experiment:
            future_active_eligible_count += 1
        for reason in identity_diagnostic.blocking_reasons:
            future_active_reason_counts[reason] += 1
        scopes.append(
            {
                "scope_key": scope.scope_key,
                "enrollment_id": scope.enrollment_id,
                "node_label": summary.node_id,
                "descriptor_version": scope.descriptor_version,
                "descriptor_hash": scope.descriptor_hash,
                "executor": {
                    "kind": scope.executor_kind,
                    "version": scope.executor_version,
                    "worker_protocol_version": scope.worker_protocol_version,
                },
                "model": {
                    "provider": scope.model_provider,
                    "name": scope.model_name,
                    "digest": scope.model_digest,
                    "variant": scope.model_variant,
                },
                "task_class": scope.task_class,
                "evidence_role": scope.evidence_role,
                "observation_count": aggregate.observation_count,
                "settlement": {
                    "sample_count": aggregate.settlement_count,
                    "output_count": aggregate.settled_output_count,
                    "worker_error_count": aggregate.settled_worker_error_count,
                    "empty_output_count": aggregate.settled_empty_output_count,
                },
                "deadline_success": {
                    **binary(aggregate.deadline_completion),
                    "meaning": "nonempty_output_settled_before_lease_deadline",
                },
                "contract_floor": {
                    **binary(aggregate.contract_floor),
                    "meaning": "structural_contract_assurance_not_semantic_correctness",
                },
                "sampled_agreement": {
                    **binary(aggregate.sampled_agreement),
                    "meaning": "output_shape_agreement_not_correctness",
                },
                "lease_expiration_count": aggregate.lease_expiration_count,
                "worker_disconnect_count": aggregate.worker_disconnect_count,
                "latency": {
                    "sample_count": aggregate.latency_sample_count,
                    "recent_median_coordinator_wall_seconds": (
                        aggregate.recent_median_latency_seconds
                    ),
                },
                "throughput": {
                    "sample_count": aggregate.throughput_sample_count,
                    "recent_median_effective_output_bytes_per_second": (
                        aggregate.recent_median_output_bytes_per_second
                    ),
                },
                "minimum_samples": aggregate.minimum_samples,
                "insufficient_evidence": aggregate.insufficient_evidence,
                "last_observed_at": summary.last_observed_at,
                "shadow_policy": {
                    "available": shadow_decisions_available,
                    "decision_count": (
                        shadow_counts.decision_count
                        if shadow_counts
                        else (0 if shadow_decisions_available else None)
                    ),
                    "same_count": (
                        shadow_counts.same_count
                        if shadow_counts
                        else (0 if shadow_decisions_available else None)
                    ),
                    "different_count": (
                        shadow_counts.different_count
                        if shadow_counts
                        else (0 if shadow_decisions_available else None)
                    ),
                    "no_preference_count": (
                        shadow_counts.no_preference_count
                        if shadow_counts
                        else (0 if shadow_decisions_available else None)
                    ),
                    "last_decision_at": (
                        shadow_counts.last_decision_at if shadow_counts else None
                    ),
                },
                "future_active_experiment_eligibility": (
                    identity_diagnostic.as_dict()
                ),
                "affects_routing": False,
            }
        )
    return {
        "mode": str(cfg.get("capability_evidence_mode", "off")),
        "minimum_samples": minimum_samples,
        "affects_routing": False,
        "shadow_decision_aggregates_available": shadow_decisions_available,
        "categories": {
            "claim": "node_advertised_descriptor",
            "observation": "coordinator_recorded_operational_outcome",
            "agreement": "bounded_output_comparison_not_correctness",
            "assurance": "task_specific_contract_validation",
            "reputation": "not_implemented",
        },
        "shadow_operational_health": {
            "meaning": "operational_experiment_health_not_node_reputation",
            "authoritative": False,
            "affects_routing": False,
            "durable": {
                "available": True,
                "counts_by_phase": {
                    "admission": operational.admission_counts,
                    "evaluation": operational.evaluation_counts,
                },
                "orphan_evaluation_total": operational.orphan_evaluation_total,
                "assignment_observation_total": (
                    operational.assignment_observation_total
                ),
                "offered_total": operational.offered_total,
                "scheduled_total": operational.scheduled_total,
                "completed_total": operational.completed_total,
                "skipped_total": operational.skipped_total,
                "failed_total": operational.failed_total,
                "pending_total": operational.pending_total,
                "drop_failure_numerator": (
                    operational.drop_failure_numerator
                ),
                "drop_failure_denominator": (
                    operational.drop_failure_denominator
                ),
                "drop_failure_rate": operational.drop_failure_rate,
                "window": {
                    "started_at": operational.window_started_at,
                    "ended_at": operational.window_ended_at,
                    "cohort_basis": "inclusive_admission_occurred_at",
                },
                "latest_event_at": operational.latest_event_at,
            }
            if operational is not None
            else {
                "available": False,
                "counts_by_phase": None,
                "orphan_evaluation_total": None,
                "assignment_observation_total": None,
                "offered_total": None,
                "scheduled_total": None,
                "completed_total": None,
                "skipped_total": None,
                "failed_total": None,
                "pending_total": None,
                "drop_failure_numerator": None,
                "drop_failure_denominator": None,
                "drop_failure_rate": None,
                "window": {
                    "started_at": window_started_at,
                    "ended_at": window_ended_at,
                    "cohort_basis": "inclusive_admission_occurred_at",
                },
                "latest_event_at": None,
            },
            "process_local": {
                "reset_at": process_local.reset_at,
                "durable_health_record_write_failure": (
                    process_local.durable_health_record_write_failure
                ),
                "unexpected_containment_failure": (
                    process_local.unexpected_containment_failure
                ),
                "background_task_callback_failure": (
                    process_local.background_task_callback_failure
                ),
                "durable": False,
            },
        },
        "future_active_experiment_eligibility": {
            "eligible_scope_count": future_active_eligible_count,
            "blocked_scope_count": len(scopes) - future_active_eligible_count,
            "blocking_reason_counts": future_active_reason_counts,
            "necessary_prerequisites": [
                "immutable_model_identity",
                "all_live_experiment_thresholds",
                "separate_accepted_adr",
                "separately_reviewed_implementation_pr",
            ],
            "active_routing_implemented": False,
            "meaning": (
                "identity_prerequisites_only_not_correctness_reputation_trust_"
                "or_active_routing"
            ),
        },
        "scopes": scopes,
        "count": len(scopes),
    }


@router.post("/nodes/{node_id}/heartbeat")
async def heartbeat_node(node_id: str, request: Request):
    """Refresh one registered worker session without polling for work."""

    session_record = _check_node_session(request, node_id)
    if not state.touch_node(session_record.node_id):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "node_registration_required",
                "message": "node registry entry is absent; register again",
                "action": "register_again",
            },
            headers={"X-Node-Session-Required": "true"},
        )
    node = nodes[session_record.node_id]
    node.update(
        _lifetime_summary(
            session_record.node_id,
            session_record.enrollment_id,
            session_record.session_id,
        )
    )
    return {
        "ok": True,
        "node_id": session_record.node_id,
        "enrollment_id": session_record.enrollment_id,
        "enrolled": session_record.enrollment_id is not None,
        "session_id": session_record.session_id,
        "session_tasks_completed": node.get("session_tasks_completed", 0),
        "session_contribution_points": node.get(
            "session_contribution_points", 0
        ),
        "lifetime_tasks_completed": node.get("lifetime_tasks_completed", 0),
        "lifetime_contribution_points": node.get(
            "lifetime_contribution_points", 0
        ),
    }


@router.post("/nodes/{node_id}/drain")
async def drain_node(node_id: str, request: Request):
    """Stop handing new work to a session while allowing its current result."""

    session_record = _check_node_session(request, node_id)
    if not state.touch_node(session_record.node_id):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "node_registration_required",
                "message": "node registry entry is absent; register again",
                "action": "register_again",
            },
            headers={"X-Node-Session-Required": "true"},
        )
    nodes[session_record.node_id]["draining"] = True
    state.waiting_nodes.pop(session_record.node_id, None)
    _emit("node_draining", {
        "node_id": session_record.node_id,
        "enrollment_id": session_record.enrollment_id,
        "session_id": session_record.session_id,
    })
    return {
        "ok": True,
        "node_id": session_record.node_id,
        "enrollment_id": session_record.enrollment_id,
        "enrolled": session_record.enrollment_id is not None,
        "session_id": session_record.session_id,
        "draining": True,
        "current_task": nodes[session_record.node_id].get("current_task"),
    }


@router.get("/tasks/next")
async def next_task(node_id: str, request: Request):
    """Worker asks for the next available task.

    Long-polls up to _LONG_POLL_TIMEOUT seconds — holds the connection open
    until work arrives or the timeout expires. Much more efficient than the
    node polling every few seconds and getting empty 204s.

    Returns 429 if the node is circuit-breaker blacklisted.
    """
    session_record = _check_node_session(request, node_id)
    node_id = session_record.node_id
    if not state.touch_node(node_id):
        state.node_sessions.invalidate_node(
            node_id, session_id=session_record.session_id
        )
        raise HTTPException(
            status_code=401,
            detail={
                "code": "node_registration_required",
                "message": "node registry entry is absent; register again",
                "action": "register_again",
            },
            headers={"X-Node-Session-Required": "true"},
        )
    if nodes[node_id].get("draining"):
        return Response(status_code=204)

    # Circuit breaker — check if this node is blacklisted for repeated failures
    if node_id in node_blacklist:
        if time.time() < node_blacklist[node_id]:
            remaining = int(node_blacklist[node_id] - time.time())
            return JSONResponse(
                status_code=429,
                content={"error": "circuit_open", "retry_after": remaining},
            )
        else:
            # Blacklist expired — let it back in
            node_blacklist.pop(node_id, None)
            node_failure_count[node_id] = 0

    # Resolve the exact immutable claim bound to this session once. A later
    # process incarnation must authenticate again under the handout lock.
    node_descriptor = _descriptor_for_session(session_record)

    def _find_task() -> tuple[int, node_capabilities.CapabilityMatchResultV1] | None:
        """Index and exact match decision for the first task this node may take.

        Peeks rather than pops because the match is rechecked while holding the
        queue lock immediately before assignment.
        """
        current_node_caps = list(nodes.get(node_id, {}).get("capabilities", []))
        for i, t in enumerate(task_queue):
            # A verification duplicate must land on a different node than the
            # original, or it compares a node against itself and proves nothing.
            if t.get("exclude_node") and t["exclude_node"] == node_id:
                continue
            eligible = set(t.get("eligible_nodes", []))
            if eligible and node_id not in eligible:
                continue
            capability_match = node_capabilities.match_node_requirements(
                t.get("resource_requirements"),
                t.get("requires", []),
                node_descriptor,
                current_node_caps,
                preferred_model_name=nodes.get(node_id, {}).get("model"),
                required_output_capacity_bytes=t.get(
                    "max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES
                ),
            )
            if capability_match.eligible:
                return i, capability_match
        return None

    # Long-poll: wait up to _LONG_POLL_TIMEOUT for a task to appear
    deadline = time.time() + state._LONG_POLL_TIMEOUT
    state.waiting_nodes[node_id] = time.time()
    try:
        while True:
            state.waiting_nodes[node_id] = time.time()
            found = _find_task()
            if found is not None:
                # The token may reach its absolute expiry while this request is
                # held in a long poll.  Recheck immediately before handout.
                session_record = _check_node_session(request, node_id)
                with state._task_queue_lock:
                    # A returning registration may replace a live incarnation
                    # after the pre-lock check. Reauthenticate under the same
                    # lock used by replacement reclaim, so an old poll can
                    # neither strand nor receive a newly issued attempt.
                    session_record = _check_node_session(request, node_id)
                    # Another TestClient thread may have claimed it between the
                    # peek and this mutation. Recompute while holding the lock.
                    found = _find_task()
                    if found is None:
                        continue
                    idx, capability_match = found
                    task = task_queue.pop(idx)
                    task["assigned_to"] = node_id
                    task["assigned_session_id"] = session_record.session_id
                    task["assigned_enrollment_id"] = session_record.enrollment_id
                    task["assigned_credential_version"] = (
                        session_record.credential_version
                    )
                    task["assigned_descriptor_version"] = (
                        session_record.capability_descriptor_version
                    )
                    task["assigned_descriptor_hash"] = (
                        session_record.capability_descriptor_hash
                    )
                    task["selected_model"] = (
                        {
                            "provider": capability_match.selected_model.provider,
                            "name": capability_match.selected_model.name,
                            "digest": capability_match.selected_model.digest,
                        }
                        if capability_match.selected_model is not None
                        else None
                    )
                    task["assigned_at"] = time.time()
                    execution_deadline = task.get("execution_deadline_at")
                    if execution_deadline and task["assigned_at"] >= float(execution_deadline):
                        _emit("worker_task_expired", {
                            "task_id": task["task_id"],
                            "execution_id": task.get("execution_id"),
                            "reason": "execution deadline elapsed before assignment",
                        })
                        continue
                    # Bind this handout to this node. The nonce is unguessable
                    # and separate from task_id, which appears in events/logs.
                    task["attempt_id"] = uuid.uuid4().hex
                    task["nonce"] = secrets.token_urlsafe(24)
                    lease_seconds = min(
                        7200,
                        max(
                            1,
                            int(
                                task.get(
                                    "lease_seconds",
                                    state.ATTEMPT_LEASE_SECONDS,
                                )
                            ),
                        ),
                    )
                    task["lease_expires_at"] = task["assigned_at"] + lease_seconds
                    if execution_deadline:
                        task["lease_expires_at"] = min(
                            task["lease_expires_at"], float(execution_deadline)
                        )
                    try:
                        requirement_version, requirement_digest = (
                            node_capabilities.canonical_requirement_binding(
                                task.get("resource_requirements"),
                                task.get("requires", []),
                            )
                        )
                        for field, expected in (
                            ("requirement_version", requirement_version),
                            ("requirement_digest", requirement_digest),
                        ):
                            supplied = task.get(field)
                            if supplied is not None and str(supplied) != expected:
                                raise ValueError(
                                    f"pre-populated {field} does not match the "
                                    "canonical task requirements"
                                )
                            task[field] = expected
                        attempt_record = state.attempt_store.issue(
                            task,
                            assigned_node_id=node_id,
                            attempt_id=task["attempt_id"],
                            nonce=task["nonce"],
                            issued_at=task["assigned_at"],
                            lease_expires_at=task["lease_expires_at"],
                            assigned_session_id=session_record.session_id,
                            assigned_enrollment_id=session_record.enrollment_id,
                            assigned_credential_version=(
                                session_record.credential_version
                            ),
                            assigned_descriptor_version=(
                                session_record.capability_descriptor_version
                            ),
                            assigned_descriptor_hash=(
                                session_record.capability_descriptor_hash
                            ),
                            requirement_version=task["requirement_version"],
                            requirement_digest=task["requirement_digest"],
                        )
                    except Exception as exc:
                        # Never expose work unless its authority is durable.
                        _clear_assignment(task)
                        task_queue.insert(min(idx, len(task_queue)), task)
                        _safe_diagnostic_emit(
                            "attempt_issue_failed",
                            {
                                "task_id": task["task_id"],
                                "node_id": node_id,
                                "enrollment_id": session_record.enrollment_id,
                                "phase": "attempt_issue",
                                "error_type": type(exc).__name__,
                            },
                        )
                        raise HTTPException(
                            status_code=503,
                            detail="worker attempt could not be issued durably",
                        ) from exc
                    if node_id in nodes:
                        nodes[node_id]["current_task"] = task.get(
                            "title", task["task_id"]
                        )
                    task_inflight[task["task_id"]] = task
                    _emit("node_busy", {
                        "node_id": node_id,
                        "enrollment_id": session_record.enrollment_id,
                        "task_id": task["task_id"],
                        "unit_id": task.get("execution_unit_id"),
                    })
                    _emit("attempt_started", {
                        "task_id": task["task_id"],
                        "attempt_id": task["attempt_id"],
                        "execution_id": task.get("execution_id"),
                        "unit_id": task.get("execution_unit_id"),
                        "node_id": node_id,
                        "enrollment_id": session_record.enrollment_id,
                        "descriptor_version": (
                            session_record.capability_descriptor_version
                        ),
                        "descriptor_hash": (
                            session_record.capability_descriptor_hash
                        ),
                        "placement": "distributed",
                    })
                    # The handout is already fixed and durable. Shadow mode only
                    # schedules a post-assignment counterfactual; it cannot
                    # reorder the queue, change eligibility, or delay on evidence.
                    state.schedule_capability_shadow_evaluation(
                        attempt_record,
                        actual_descriptor=node_descriptor,
                        resource_requirements=task.get("resource_requirements"),
                        required_capabilities=task.get("requires", []),
                        eligible_node_ids=task.get("eligible_nodes", []),
                        decision_at=task["assigned_at"],
                    )
                    return task

            if time.time() >= deadline:
                return Response(status_code=204)

            await asyncio.sleep(0.5)
    finally:
        state.waiting_nodes.pop(node_id, None)


def _settle_and_publish(
    task_id: str,
    result: TaskResult,
    *,
    session_id: str,
    enrollment_id: str | None,
    credential_version: int | None,
):
    """Serialize in-memory lifecycle changes around the SQLite transaction."""
    with state._task_queue_lock:
        pending = task_inflight.get(task_id)
        if pending is not None and state.attempt_store.active_for_task(task_id) is None:
            # Rolling-upgrade compatibility: make an already-issued, fully
            # bound in-memory attempt durable before considering its result.
            state.attempt_store.adopt_inflight(pending)
        outcome = state.attempt_store.settle(
            task_id=task_id,
            node_id=result.node_id,
            output=result.output,
            error=result.error,
            elapsed_seconds=result.elapsed_seconds,
            contract_version=result.contract_version,
            attempt_id=result.attempt_id,
            nonce=result.nonce,
            execution_id=result.execution_id,
            execution_unit_id=result.execution_unit_id,
            execution_unit_kind=result.execution_unit_kind,
            session_id=session_id,
            enrollment_id=enrollment_id,
            credential_version=credential_version,
        )
        receipt = outcome.receipt
        state.accepted_result_broker.publish(receipt)
        if outcome.replayed:
            return pending, outcome, None, ""

        task = task_inflight.get(task_id)
        if task and task.get("attempt_id") == receipt.attempt_id:
            task = task_inflight.pop(task_id)
        else:
            task = None
        trace_id = task.get("trace_id", "") if task else ""
        # Compatibility/diagnostic mirror. Dispatcher authority is exclusively
        # the accepted receipt broker above.
        task_results[task_id] = receipt.as_legacy_result(trace_id=trace_id)
        return pending, outcome, task, trace_id


@router.post("/tasks/{task_id}/result")
async def submit_result(task_id: str, result: TaskResult, request: Request):
    """Settle a result only through its active server-issued attempt."""
    session_record = _check_node_session(request, result.node_id)
    if not state.touch_node(session_record.node_id):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "node_registration_required",
                "message": "node registry entry is absent; register again",
                "action": "register_again",
            },
            headers={"X-Node-Session-Required": "true"},
        )
    pending = task_inflight.get(task_id)
    try:
        pending, outcome, _task, trace_id = _settle_and_publish(
            task_id,
            result,
            session_id=session_record.session_id,
            enrollment_id=session_record.enrollment_id,
            credential_version=session_record.credential_version,
        )
    except WorkerPayloadLimitExceeded as exc:
        try:
            quarantine_id = state.attempt_store.quarantine(
                task_id=task_id,
                claimed_attempt_id=result.attempt_id,
                claimed_node_id=result.node_id,
                claimed_enrollment_id=session_record.enrollment_id,
                claimed_execution_id=result.execution_id,
                claimed_unit_id=result.execution_unit_id,
                claimed_unit_kind=result.execution_unit_kind,
                claimed_contract_version=result.contract_version,
                output=result.output,
                error=result.error,
                reason=exc.reason,
            )
        except Exception:
            quarantine_id = None
        _emit("result_rejected", {
            "task_id": task_id,
            "claimed_by": result.node_id,
            "enrollment_id": session_record.enrollment_id,
            "assigned_to": pending.get("assigned_to") if pending else None,
            "reason": exc.reason,
            "attempt_state": exc.state,
            "error_code": exc.code,
            "quarantined": quarantine_id is not None,
            "quarantine_id": quarantine_id,
        })
        return JSONResponse(
            status_code=413,
            content={
                "status": "rejected",
                "error": exc.code,
                "detail": exc.reason,
                "field": exc.field,
                "max_bytes": exc.limit,
                "observed_bytes": exc.observed,
                "quarantined": quarantine_id is not None,
            },
        )
    except AttemptRejected as exc:
        try:
            quarantine_id = state.attempt_store.quarantine(
                task_id=task_id,
                claimed_attempt_id=result.attempt_id,
                claimed_node_id=result.node_id,
                claimed_enrollment_id=session_record.enrollment_id,
                claimed_execution_id=result.execution_id,
                claimed_unit_id=result.execution_unit_id,
                claimed_unit_kind=result.execution_unit_kind,
                claimed_contract_version=result.contract_version,
                output=result.output,
                error=result.error,
                reason=exc.reason,
            )
        except Exception:
            quarantine_id = None
        _emit("result_rejected", {
            "task_id": task_id,
            "claimed_by": result.node_id,
            "enrollment_id": session_record.enrollment_id,
            "assigned_to": pending.get("assigned_to") if pending else None,
            "reason": exc.reason,
            "attempt_state": exc.state,
            "quarantined": quarantine_id is not None,
            "quarantine_id": quarantine_id,
        })
        raise HTTPException(status_code=403, detail=f"result rejected: {exc.reason}") from exc

    receipt = outcome.receipt
    if outcome.replayed:
        try:
            sync_compatibility_ledger()
        except Exception:
            pass
        return outcome.response

    success = bool(receipt.output and not receipt.error)
    node_id = receipt.assigned_node_id
    if not success:
        count = node_failure_count.get(node_id, 0) + 1
        node_failure_count[node_id] = count
        if count >= state._FAILURE_THRESHOLD:
            node_blacklist[node_id] = time.time() + state._BLACKLIST_DURATION
            _emit("node_blacklisted", {
                "node_id": node_id,
                "enrollment_id": receipt.assigned_enrollment_id,
                "failure_count": count,
                "blacklist_seconds": state._BLACKLIST_DURATION,
            })
    else:
        node_failure_count[node_id] = 0

    credits_earned = int(outcome.response.get("credits_earned", 0))
    try:
        node_record = nodes.get(node_id)
        if (
            node_record is not None
            and node_record.get("session_id") == session_record.session_id
            and node_record.get("enrollment_id") == receipt.assigned_enrollment_id
            and node_record.get("credential_version")
            == session_record.credential_version
        ):
            # This is only a process-local compatibility mirror. A concurrent
            # revoke, rotation, or returning registration may have removed or
            # replaced the incarnation after durable settlement; never turn
            # that accepted commit into a 500 or credit the replacement session.
            node_record["session_tasks_completed"] = int(
                node_record.get("session_tasks_completed", 0)
            ) + 1
            node_record["tasks_completed"] = node_record[
                "session_tasks_completed"
            ]
            node_record["current_task"] = None
            if credits_earned:
                node_record["session_contribution_points"] = (
                    float(node_record.get("session_contribution_points", 0))
                    + credits_earned
                )
            node_record["credits_earned"] = node_record.get(
                "session_contribution_points", 0
            )
            node_record.update(
                _lifetime_summary(
                    node_id,
                    receipt.assigned_enrollment_id,
                    session_record.session_id,
                )
            )
    except Exception as exc:
        _safe_diagnostic_emit(
            "post_settlement_mirror_failed",
            {
                "task_id": task_id,
                "node_id": node_id,
                "enrollment_id": receipt.assigned_enrollment_id,
                "phase": "session_counter_mirror",
                "error_type": type(exc).__name__,
            },
        )
    try:
        sync_compatibility_ledger()
    except Exception:
        # The SQLite contribution is already atomic with settlement. Failure to
        # refresh a compatibility projection must not alter the accepted reply.
        pass

    _emit("node_idle", {
        "node_id": node_id,
        "enrollment_id": getattr(receipt, "assigned_enrollment_id", None),
        "credits_earned": credits_earned,
        "contribution_basis": "compute_contribution" if credits_earned else None,
        "points_are_monetary": False,
        "elapsed_seconds": receipt.elapsed_seconds,
        "success": success,
        "trace_id": trace_id,
    })
    _emit("attempt_completed", {
        "task_id": receipt.task_id,
        "attempt_id": receipt.attempt_id,
        "execution_id": receipt.execution_id,
        "unit_id": receipt.execution_unit_id,
        "unit_kind": receipt.execution_unit_kind,
        "node_id": node_id,
        "enrollment_id": getattr(receipt, "assigned_enrollment_id", None),
        "descriptor_version": receipt.assigned_descriptor_version,
        "descriptor_hash": receipt.assigned_descriptor_hash,
        "status": "completed" if success else "failed",
        "placement": "distributed",
    })
    return outcome.response


@router.post("/tasks/{task_id}/tokens")
@router.post("/tasks/{task_id}/stream")
async def stream_task_tokens(task_id: str, batch: TokenBatch, request: Request):
    """Worker node relays streamed tokens back to the orchestrator.

    The server re-emits them as WebSocket 'token' events so the dashboard
    live-streams output from remote nodes, not just local builds.

    Streaming tokens also counts as a heartbeat, and used not to. `last_seen`
    was refreshed only by /tasks/next and /tasks/{id}/result, so a node stayed
    "alive" only between tasks — a build longer than _NODE_TIMEOUT (90 s) went
    silent by that measure while the node was in fact sending a batch every
    0.3 s. The janitor then evicted a working node, reclaimed the task it was
    mid-way through, and re-queued it into an empty registry where nothing
    could pick it up. Found in a dress rehearsal: on a single-node setup the
    fourth subtask was reclaimed under the node, the dashboard dropped to
    0 nodes, and the run stalled — while the node's own terminal showed it
    building. A node emitting tokens for a task is the strongest liveness
    signal there is.
    """
    session_record = _check_node_session(request, batch.node_id)
    if session_record.node_id not in nodes:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "node_registration_required",
                "message": "node registry entry is absent; register again",
                "action": "register_again",
            },
            headers={"X-Node-Session-Required": "true"},
        )
    task = task_inflight.get(task_id)
    try:
        stream_outcome = state.attempt_store.record_stream_batch(
            task_id=task_id,
            node_id=batch.node_id,
            tokens=batch.tokens,
            contract_version=batch.contract_version,
            attempt_id=batch.attempt_id,
            nonce=batch.nonce,
            execution_id=batch.execution_id,
            execution_unit_id=batch.execution_unit_id,
            execution_unit_kind=batch.execution_unit_kind,
            session_id=session_record.session_id,
            enrollment_id=session_record.enrollment_id,
            credential_version=session_record.credential_version,
        )
    except AttemptRejected as exc:
        raise HTTPException(
            status_code=403, detail=f"stream rejected: {exc.reason}"
        ) from exc

    state.touch_node(batch.node_id)
    if not stream_outcome.accepted:
        if batch.node_id in nodes:
            nodes[batch.node_id]["current_task"] = None
        if stream_outcome.emit_limit_event:
            _emit("stream_limit_exceeded", {
                "task_id": task_id,
                "attempt_id": stream_outcome.attempt_id,
                "node_id": batch.node_id,
                "enrollment_id": session_record.enrollment_id,
                "error": stream_outcome.error_code,
                "detail": stream_outcome.detail,
                "max_output_bytes": stream_outcome.max_output_bytes,
                "streamed_bytes": stream_outcome.streamed_bytes,
                "stream_batch_count": stream_outcome.stream_batch_count,
            })
        return JSONResponse(
            status_code=(
                429
                if stream_outcome.error_code == "stream_rate_limit_exceeded"
                else 413
            ),
            content={
                "ok": False,
                "status": "limit_exceeded",
                "error": stream_outcome.error_code,
                "detail": stream_outcome.detail,
                "max_output_bytes": stream_outcome.max_output_bytes,
                "streamed_bytes": stream_outcome.streamed_bytes,
                "stream_batch_count": stream_outcome.stream_batch_count,
            },
        )

    _emit("token", {
        "token": batch.tokens,
        "subtask_id": task.get("subtask_id", 0) if task else 0,
        "job_id": task.get("job_id", "") if task else "",
        "trace_id": task.get("trace_id", "") if task else "",
        "source": "node",
        "node_id": batch.node_id,
        "enrollment_id": session_record.enrollment_id,
    })
    return {"ok": True}
