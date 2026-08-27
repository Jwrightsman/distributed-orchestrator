"""
Shared server state and infrastructure.

Everything the route modules have in common lives here: in-memory orchestration
state, SQLite persistence, the WebSocket manager, event emission, rate limiting,
node auth, and the request/response models. Route modules import from here;
server.py assembles the app.
"""

import asyncio
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass
import json
import logging
import math
import queue
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import HTTPException, Request, WebSocket
from pydantic import BaseModel, Field, field_validator

from capability_evidence import (
    CapabilityEvidenceStore,
    CapabilityShadowDecisionStore,
    CapabilityShadowOperationalStore,
    EligibleShadowCandidate,
    EvidenceScope,
    ShadowAdmissionOutcome,
    ShadowEvaluation,
    ShadowEvaluationOutcome,
    ShadowOperationalOutcome,
    ShadowOperationalPhase,
    ShadowOperationalProcessCounters,
    evaluate_shadow_preference,
)
from config import get as get_config
from execution.attempts import AcceptedResultBroker, AttemptStore
from execution.contracts import (
    ConfidentialityV1,
    ExecutionRequirementsV1,
    OutputContractV1,
    PlacementV1,
    StrategyNameV1,
    StrategyOptionsV1,
    VerificationPolicyV1,
)
from verification import VerificationPool
from sqlite_store import connection, migration_lock
from node_enrollments import (
    ENROLLMENT_CREDENTIAL_MAX_LENGTH,
    ENROLLMENT_CREDENTIAL_MIN_LENGTH,
    EnrollmentCredentialRotated,
    EnrollmentRevoked,
    EnrollmentSessionMismatch,
    InvalidEnrollmentCredential,
    NodeEnrollmentError,
    NodeEnrollmentStore,
    validate_enrollment_credential,
)
from node_sessions import (
    InvalidNodeId,
    InvalidNodeSession,
    NodeSessionRecord,
    NodeSessionRegistry,
    normalize_node_id,
)
from node_capabilities import (
    NodeCapabilityDescriptorV1,
    NodeCapabilitySnapshotStore,
    capability_descriptor_digest,
    match_node_requirements,
)

logger = logging.getLogger("mycelium.diagnostics")

# ── Node staleness threshold (seconds) ───────────────────────────────────
_NODE_TIMEOUT = 90
_MAX_TASK_QUEUE = 100
_LONG_POLL_TIMEOUT = 25   # seconds to hold GET /tasks/next open waiting for work

# Sessions deliberately live only for this coordinator process. Durable
# enrollment authentication binds trusted-alpha sessions; explicit local
# compatibility sessions still rely on the shared admission secret.
node_sessions = NodeSessionRegistry(stale_after_seconds=_NODE_TIMEOUT)

# ── Circuit breaker thresholds ────────────────────────────────────────────
_FAILURE_THRESHOLD = 3    # consecutive failures before blacklisting
_BLACKLIST_DURATION = 60  # seconds a blacklisted node sits out

# ── Pitch rate limiting (per IP) ──────────────────────────────────────────
_RATE_WINDOW = 60         # seconds
_RATE_MAX = 5             # max pitches per IP per window
_pitch_timestamps: dict[str, list[float]] = {}   # ip -> list of recent timestamps

# ── Public pitch page limits (/try — keyless, so much harsher) ────────────
_PUBLIC_RATE_WINDOW = 3600   # seconds
_PUBLIC_RATE_MAX = 2         # pitches per IP per window
_PUBLIC_TASK_MAX = 300       # task length cap (chars)
_PUBLIC_MAX_ACTIVE = 3       # concurrent public jobs across all visitors
_PUBLIC_MAX_ACTIVE_PER_SOURCE = 1
_public_pitch_timestamps: dict[str, list[float]] = {}

# Basic content filter for keyless public pitching. Substring match, so it
# over-blocks (e.g. "hackathon") — acceptable at this trust tier.
_PUBLIC_BLOCKLIST = (
    "hack", "malware", "ransomware", "exploit", "phishing", "ddos", "botnet",
    "keylogger", "spyware", "crack password", "bypass", "nude", "porn",
    "sexual", "nsfw", "bomb", "weapon", "ghost gun", "suicide", "kill ",
)

# ── Pipeline event log (for dashboard live updates) ──────────────────
pipeline_events: list[dict] = []   # recent events for polling fallback

# ── In-memory state ──────────────────────────────────────────────────
nodes: dict[str, dict] = {}          # node_id -> info

# ── Task attempt binding ─────────────────────────────────────────────
#
# node_secret is *network admission*, not per-node identity: everyone holding it
# presents the same credential. Result submission used to trust the node_id in
# the request body and locate the task by task_id alone, so any admitted node
# could submit a result attributed to a different node and take its credit.
#
# When a task is handed out the server durably mints an attempt: an id and a
# nonce, both unguessable and distinct from task_id. ``AttemptStore`` validates
# and atomically settles it; ``AcceptedResultBroker`` is the only dispatcher
# channel. The dictionaries below are process-local scheduling/compatibility
# projections, not settlement authority.
#
# Bearer enrollment adds independent revocation and durable attribution, but it
# is not a keypair, signature, physical-machine identity, attestation, or Sybil
# defense. Those stronger identity mechanisms remain deferred.
ATTEMPT_LEASE_SECONDS = 900

# Deprecated in-memory compatibility mirror. Durable exact replay is handled by
# AttemptStore.response_json/result_hash and does not consult this dictionary.
settled_attempts: dict[str, dict] = {}
_MAX_SETTLED = 5000


def remember_settlement(
    attempt_id: str,
    outcome: dict,
    *,
    node_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """Record an attempt as settled, bounding the memory this can consume."""
    settled_attempts[attempt_id] = {
        "response": outcome,
        "node_id": node_id,
        "task_id": task_id,
    }
    if len(settled_attempts) > _MAX_SETTLED:
        for stale in list(settled_attempts)[: len(settled_attempts) - _MAX_SETTLED]:
            settled_attempts.pop(stale, None)


# Process start, for the public /status.json uptime figure. A stranger deciding
# whether this network is real cares that it has been up for days, not seconds.
STARTED_AT = time.time()
task_queue: list[dict] = []          # pending tasks for workers
task_results: dict[str, dict] = {}   # task_id -> result
task_inflight: dict[str, dict] = {}  # task_id -> task (assigned but not yet returned)
_task_queue_lock = threading.RLock()


def enqueue_task(task: dict) -> bool:
    """Atomically enforce the pending-task cap for every generated unit."""
    with _task_queue_lock:
        if len(task_queue) >= _MAX_TASK_QUEUE:
            return False
        task_queue.append(task)
        return True


def remove_queued_task(task_id: str) -> bool:
    """Remove a queued task by id without a check-then-mutate race."""
    with _task_queue_lock:
        for index, task in enumerate(task_queue):
            if task.get("task_id") == task_id:
                task_queue.pop(index)
                return True
    return False

# ── Circuit breaker state ─────────────────────────────────────────────
node_failure_count: dict[str, int] = {}   # node_id -> consecutive failure count
node_blacklist: dict[str, float] = {}     # node_id -> blacklist_until timestamp

# Sampled output agreement
# One pool for the process. verify_rate is read from config at first use rather
# than captured at import, so a config edit takes effect on the next pitch
# instead of needing a restart.
verification_pool = VerificationPool(verify_rate=0.0)

# Capability evidence is append-only and observational. Shadow jobs operate on
# already-issued attempts and have no reference to the queue mutation APIs.
_CAPABILITY_SHADOW_TASK_LIMIT = 64
# SQLite work is bounded independently. This shorter graceful drain keeps the
# coordinator inside Docker's 30-second stop budget while allowing ordinary
# decision writes to finish and report their real terminal outcome.
_CAPABILITY_SHADOW_SHUTDOWN_DRAIN_SECONDS = 12.0
_CAPABILITY_SHADOW_SHUTDOWN_CANCEL_SECONDS = 1.0


class _BoundedDaemonExecutor:
    """Run optional shadow work without joining blocked threads at process exit.

    The standard asyncio executor uses non-daemon workers that its runner joins
    during process shutdown. Shadow telemetry is explicitly best effort, so a
    blocked optional write must not extend coordinator ownership past the
    graceful drain. This tiny private executor bounds workers and queued calls;
    queued futures are cancellable, while already-running daemon calls may be
    abandoned safely when the process exits.
    """

    _STOP = object()

    def __init__(self, *, name: str, max_workers: int, max_pending: int):
        self._name = name
        self._max_workers = max_workers
        self._queue: queue.Queue[object] = queue.Queue(maxsize=max_pending)
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._accepting = True

    def _start_workers_locked(self) -> None:
        if self._threads:
            return
        for index in range(self._max_workers):
            worker = threading.Thread(
                target=self._worker,
                name=f"{self._name}-{index + 1}",
                daemon=True,
            )
            self._threads.append(worker)
            worker.start()

    def submit(
        self,
        function: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any]:
        future: Future[Any] = Future()
        with self._lock:
            if not self._accepting:
                raise RuntimeError("capability shadow executor is closed")
            self._start_workers_locked()
            try:
                self._queue.put_nowait((future, function, args, kwargs))
            except queue.Full as exc:
                raise RuntimeError("capability shadow executor queue is full") from exc
        return future

    def close(self) -> None:
        """Cancel queued calls and ask idle/running daemon workers to exit."""

        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if item is not self._STOP:
                        future, _function, _args, _kwargs = item
                        future.cancel()
                finally:
                    self._queue.task_done()
            for _worker in self._threads:
                self._queue.put_nowait(self._STOP)

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                future, function, args, kwargs = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = function(*args, **kwargs)
                except BaseException as exc:  # mirrors concurrent.futures
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._queue.task_done()


def _new_capability_shadow_work_executor() -> _BoundedDaemonExecutor:
    return _BoundedDaemonExecutor(
        name="mycelium-shadow-work",
        max_workers=8,
        max_pending=_CAPABILITY_SHADOW_TASK_LIMIT,
    )


def _new_capability_shadow_health_executor() -> _BoundedDaemonExecutor:
    return _BoundedDaemonExecutor(
        name="mycelium-shadow-health",
        max_workers=2,
        max_pending=_CAPABILITY_SHADOW_HEALTH_TASK_LIMIT,
    )


_capability_shadow_tasks: set[asyncio.Task] = set()
_CAPABILITY_SHADOW_HEALTH_TASK_LIMIT = 256
_CAPABILITY_SHADOW_TERMINAL_TRACKING_LIMIT = 4096
_capability_shadow_health_tasks: set[asyncio.Task] = set()
_capability_shadow_task_attempt_ids: dict[asyncio.Task, str] = {}
_capability_shadow_task_decision_times: dict[asyncio.Task, float] = {}
_capability_shadow_terminal_claims: dict[str, ShadowEvaluationOutcome] = {}
_capability_shadow_decision_writes: set[str] = set()
_capability_shadow_decision_futures: dict[str, Future[Any]] = {}
_capability_shadow_admissions: set[str] = set()
_capability_shadow_lifecycle_lock = threading.RLock()
_capability_shadow_shutdown_requested = False
_capability_shadow_work_executor = _new_capability_shadow_work_executor()
_capability_shadow_health_executor = _new_capability_shadow_health_executor()


@dataclass(frozen=True)
class _CapabilityShadowCandidateClaim:
    """Non-secret assignment-time inputs for background canonical matching."""

    node_id: str
    draining: bool
    blacklist_until: float
    enrollment_id: str | None
    descriptor_payload: dict[str, Any] | None
    descriptor_hash: str | None
    descriptor_version: str | None
    session_id: str | None
    legacy_capabilities: tuple[str, ...]
    preferred_model_name: str | None

# Compatibility registry for active GET /tasks/next polls. Sampled agreement
# does not read it and it has no effect on assignment order.
waiting_nodes: dict[str, float] = {}


def touch_node(node_id: str) -> bool:
    """Mark a registered node alive without inventing a placeholder identity.

    A worker whose registry entry was evicted must obtain a new server-issued
    session through ``/nodes/register``.  Polling with an arbitrary label can no
    longer create a fully admitted node record.
    """
    node = nodes.get(node_id)
    if node is not None:
        node["last_seen"] = time.time()
        return True
    return False


def _refresh_verify_rate() -> float:
    """Sync the independent sample rate with config and return it."""
    cfg = get_config()
    # Sampled duplicates are detached, process-local work today.  Until their
    # evidence and terminal state are durable, trusted-alpha mode disables them
    # rather than allowing an unfinished post-hoc task to disappear or be
    # mistaken for canonical assurance.  Local development retains the existing
    # experimental switch.
    if str(cfg.get("deployment_mode", "local")) == "trusted_alpha":
        verification_pool.verify_rate = 0.0
        return 0.0
    try:
        rate = float(cfg.get("verify_rate", 0.0) or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    verification_pool.verify_rate = max(0.0, min(1.0, rate))
    return verification_pool.verify_rate

# ── Async job store ──────────────────────────────────────────────────
# Jobs allow /pitch/async to return immediately with a job_id.
# Status: "queued" | "running" | "complete" | "failed" | "interrupted"
jobs: dict[str, dict] = {}          # job_id -> job record
OUTPUT_DIR = Path("output")

_JOB_TTL = 7 * 24 * 3600    # keep finished jobs for 7 days
_RESULT_TTL = 3600           # keep raw task results for 1 hour

# ── SQLite event persistence ──────────────────────────────────────────
_DB_PATH = Path("events.db")
_CAPABILITY_SHADOW_OPERATIONAL_DB_PATH = Path("capability-shadow-health.db")
_db_lock = threading.Lock()

# Durable attempt state and receipts share the existing SQLite database. The
# queue above remains deliberately process-local; receipts, settlement and
# rejection authority do not.
attempt_store = AttemptStore(_DB_PATH)
enrollment_store = NodeEnrollmentStore(_DB_PATH)
capability_snapshot_store = NodeCapabilitySnapshotStore(_DB_PATH)
capability_evidence_store = CapabilityEvidenceStore(_DB_PATH)
# Optional shadow decisions and operational health are deliberately isolated
# from authoritative attempt persistence. Their best-effort writers can never
# hold events.db's SQLite writer lock or make issuance/settlement wait behind
# experiment telemetry.
capability_shadow_decision_store = CapabilityShadowDecisionStore(
    _CAPABILITY_SHADOW_OPERATIONAL_DB_PATH
)
capability_shadow_operational_store = CapabilityShadowOperationalStore(
    _CAPABILITY_SHADOW_OPERATIONAL_DB_PATH
)
capability_shadow_process_counters = ShadowOperationalProcessCounters(
    reset_at=STARTED_AT
)
accepted_result_broker = AcceptedResultBroker(attempt_store)
_COORDINATOR_RESTART_MARKER = f"restart-{secrets.token_hex(12)}"


def _record_terminal_capability_evidence(attempt_id: str | None) -> None:
    """Best-effort projection after an authoritative terminal transition."""

    if not attempt_id:
        return
    record = attempt_store.get(attempt_id)
    if record is None or record.settled_at is None:
        return
    capability_evidence_store.best_effort(
        capability_evidence_store.record_terminal,
        record,
        terminal_at=record.settled_at,
    )


def _reconcile_capability_evidence() -> None:
    """Replay bounded terminal truth into the idempotent observation store."""

    for record in attempt_store.list_evidence_reconciliation_candidates(limit=1000):
        if record.state == "settled":
            receipt = attempt_store.get_receipt_for_task(record.task_id)
            if receipt is None or receipt.attempt_id != record.attempt_id:
                continue
            output_bytes = len((receipt.output or "").encode("utf-8"))
            capability_evidence_store.best_effort(
                capability_evidence_store.record_settlement,
                record,
                accepted_at=receipt.accepted_at,
                output_bytes=output_bytes,
            )
        elif record.settled_at is not None:
            capability_evidence_store.best_effort(
                capability_evidence_store.record_terminal,
                record,
                terminal_at=record.settled_at,
            )


def _record_capability_shadow_operation(
    *,
    attempt_id: str,
    phase: ShadowOperationalPhase,
    outcome: ShadowOperationalOutcome,
    reason_code: str,
    occurred_at: float,
) -> None:
    """Best-effort durable health write with one non-recursive fallback."""

    try:
        capability_shadow_operational_store.record(
            attempt_id=attempt_id,
            phase=phase,
            outcome=outcome,
            reason_code=reason_code,
            occurred_at=occurred_at,
        )
    except Exception:
        capability_shadow_process_counters.increment(
            "durable_health_record_write_failure"
        )


def _migrate_capability_shadow_operational_store() -> None:
    """Best-effort health-store initialization outside startup authority."""

    try:
        capability_shadow_operational_store.migrate()
    except Exception as exc:
        capability_shadow_process_counters.increment(
            "durable_health_record_write_failure"
        )
        logger.warning(
            "capability shadow operational store unavailable error_type=%s",
            type(exc).__name__,
        )


def _migrate_capability_shadow_decision_store() -> None:
    """Best-effort initialization and idempotent pre-isolation copy-forward."""

    try:
        capability_shadow_decision_store.migrate()
        capability_shadow_decision_store.import_legacy_decisions(_DB_PATH)
    except Exception as exc:
        capability_shadow_process_counters.increment(
            "durable_health_record_write_failure"
        )
        logger.warning(
            "capability shadow decision store unavailable error_type=%s",
            type(exc).__name__,
        )


def _shadow_health_task_done(task: asyncio.Task) -> None:
    _capability_shadow_health_tasks.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # pragma: no cover - final containment boundary
        capability_shadow_process_counters.increment(
            "background_task_callback_failure"
        )
        logger.warning(
            "capability shadow health callback failed error_type=%s",
            type(exc).__name__,
        )


async def _await_capability_shadow_future(future: Future[Any]) -> Any:
    """Bridge one private daemon-pool future into the coordinator loop."""

    return await asyncio.wrap_future(future)


def _schedule_capability_shadow_operation(
    *,
    attempt_id: str,
    phase: ShadowOperationalPhase,
    outcome: ShadowOperationalOutcome,
    reason_code: str,
    occurred_at: float,
) -> None:
    """Queue telemetry separately so it cannot hold up a production handout."""

    try:
        if len(_capability_shadow_health_tasks) >= _CAPABILITY_SHADOW_HEALTH_TASK_LIMIT:
            capability_shadow_process_counters.increment(
                "unexpected_containment_failure"
            )
            return
        loop = asyncio.get_running_loop()
        future = _capability_shadow_health_executor.submit(
            _record_capability_shadow_operation,
            attempt_id=attempt_id,
            phase=phase,
            outcome=outcome,
            reason_code=reason_code,
            occurred_at=occurred_at,
        )
        task = loop.create_task(
            _await_capability_shadow_future(future)
        )
        _capability_shadow_health_tasks.add(task)
        task.add_done_callback(_shadow_health_task_done)
    except Exception:
        capability_shadow_process_counters.increment(
            "unexpected_containment_failure"
        )


def _reserve_capability_shadow_terminal(
    attempt_id: str,
    outcome: ShadowEvaluationOutcome,
) -> bool:
    """Give shutdown or the evaluator sole ownership of a terminal outcome."""

    with _capability_shadow_lifecycle_lock:
        if attempt_id in _capability_shadow_terminal_claims:
            return False
        _capability_shadow_terminal_claims[attempt_id] = outcome
        while (
            len(_capability_shadow_terminal_claims)
            > _CAPABILITY_SHADOW_TERMINAL_TRACKING_LIMIT
        ):
            oldest_attempt_id = next(iter(_capability_shadow_terminal_claims))
            _capability_shadow_terminal_claims.pop(oldest_attempt_id, None)
        return True


def _record_capability_shadow_terminal(
    *,
    attempt_id: str,
    outcome: ShadowEvaluationOutcome,
    reason_code: str,
) -> None:
    if not _reserve_capability_shadow_terminal(attempt_id, outcome):
        return
    _schedule_capability_shadow_operation(
        attempt_id=attempt_id,
        phase="evaluation",
        outcome=outcome,
        reason_code=reason_code,
        occurred_at=time.time(),
    )


def _record_capability_shadow_terminal_direct(
    *,
    attempt_id: str,
    outcome: ShadowEvaluationOutcome,
    reason_code: str,
) -> None:
    """Persist a terminal owned by a worker thread before that thread exits."""

    if not _reserve_capability_shadow_terminal(attempt_id, outcome):
        return
    _record_capability_shadow_operation(
        attempt_id=attempt_id,
        phase="evaluation",
        outcome=outcome,
        reason_code=reason_code,
        occurred_at=time.time(),
    )


def _shadow_task_done(task: asyncio.Task) -> None:
    _capability_shadow_tasks.discard(task)
    _capability_shadow_task_decision_times.pop(task, None)
    attempt_id = _capability_shadow_task_attempt_ids.pop(task, None)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # pragma: no cover - final containment boundary
        capability_shadow_process_counters.increment(
            "background_task_callback_failure"
        )
        if attempt_id is not None:
            _record_capability_shadow_terminal(
                attempt_id=attempt_id,
                outcome="evaluator_failed",
                reason_code="evaluator_failed",
            )
        logger.warning(
            "capability shadow evaluation callback failed error_type=%s",
            type(exc).__name__,
        )


def _snapshot_capability_shadow_candidate_claims(
    *,
    actual_node_id: str,
    eligible_node_ids: tuple[str, ...],
) -> tuple[_CapabilityShadowCandidateClaim, ...]:
    """Copy bounded, non-secret claim inputs before the handout returns."""

    other_node_ids = sorted(
        {
            node_id
            for node_id in eligible_node_ids
            if isinstance(node_id, str) and node_id and node_id != actual_node_id
        }
    )[:63]
    snapshots: list[_CapabilityShadowCandidateClaim] = []
    for node_id in (actual_node_id, *other_node_ids):
        node = nodes.get(node_id)
        if node is None:
            continue
        descriptor_payload = node.get("capability_descriptor")
        snapshots.append(
            _CapabilityShadowCandidateClaim(
                node_id=node_id,
                draining=bool(node.get("draining")),
                blacklist_until=float(node_blacklist.get(node_id, 0)),
                enrollment_id=(
                    str(node["enrollment_id"])
                    if node.get("enrollment_id")
                    else None
                ),
                descriptor_payload=(
                    deepcopy(descriptor_payload)
                    if isinstance(descriptor_payload, dict)
                    else None
                ),
                descriptor_hash=(
                    str(node["capability_descriptor_hash"])
                    if node.get("capability_descriptor_hash")
                    else None
                ),
                descriptor_version=(
                    str(node["capability_descriptor_version"])
                    if node.get("capability_descriptor_version")
                    else None
                ),
                session_id=(
                    str(node["session_id"])
                    if node.get("session_id")
                    else None
                ),
                legacy_capabilities=tuple(node.get("capabilities", [])),
                preferred_model_name=(
                    str(node["model"]) if node.get("model") else None
                ),
            )
        )
    return tuple(snapshots)


def _capture_capability_shadow_scopes(
    *,
    actual_attempt: Any,
    actual_descriptor: NodeCapabilityDescriptorV1,
    resource_requirements: dict[str, Any] | None,
    required_capabilities: tuple[str, ...],
    eligible_node_ids: tuple[str, ...],
    captured_at: float,
    candidate_claims: tuple[_CapabilityShadowCandidateClaim, ...] | None = None,
) -> tuple[EvidenceScope, ...]:
    """Freeze already-matched claim/model scopes before background work starts."""

    claims = (
        candidate_claims
        if candidate_claims is not None
        else _snapshot_capability_shadow_candidate_claims(
            actual_node_id=str(actual_attempt.assigned_node_id),
            eligible_node_ids=eligible_node_ids,
        )
    )

    if getattr(actual_attempt, "evidence_role", None) != "production":
        raise ValueError("shadow evaluation requires a production attempt")
    if getattr(actual_attempt, "execution_unit_kind", None) not in {
        "dag_subtask",
        "candidate",
    }:
        raise ValueError("shadow evaluation requires a bounded task class")
    descriptor_hash = capability_descriptor_digest(actual_descriptor)
    if (
        actual_descriptor.descriptor_version
        != getattr(actual_attempt, "assigned_descriptor_version", None)
        or descriptor_hash
        != getattr(actual_attempt, "assigned_descriptor_hash", None)
    ):
        raise ValueError("actual shadow descriptor does not match the issued attempt")
    actual_models = [
        model
        for model in actual_descriptor.models
        if model.provider == getattr(actual_attempt, "assigned_model_provider", None)
        and model.name == getattr(actual_attempt, "assigned_model_name", None)
        and model.digest == getattr(actual_attempt, "assigned_model_digest", None)
    ]
    if len(actual_models) != 1:
        raise ValueError("actual shadow model does not match the issued attempt")
    actual_model = actual_models[0]
    actual_node_id = str(actual_attempt.assigned_node_id)
    actual_claim = next(
        (claim for claim in claims if claim.node_id == actual_node_id),
        None,
    )
    if (
        actual_claim is None
        or actual_claim.draining
        or actual_claim.blacklist_until > captured_at
        or actual_claim.enrollment_id
        != getattr(actual_attempt, "assigned_enrollment_id", None)
        or actual_claim.session_id
        != getattr(actual_attempt, "assigned_session_id", None)
        or actual_claim.descriptor_version != actual_descriptor.descriptor_version
        or actual_claim.descriptor_hash != descriptor_hash
    ):
        raise ValueError("actual shadow node is no longer registered")
    actual_match = match_node_requirements(
        resource_requirements,
        required_capabilities,
        actual_descriptor,
        actual_claim.legacy_capabilities,
        preferred_model_name=getattr(actual_attempt, "assigned_model_name", None),
        required_output_capacity_bytes=getattr(
            actual_attempt, "max_output_bytes", None
        ),
    )
    if not actual_match.eligible or actual_match.selected_model != actual_model:
        raise ValueError("actual shadow node no longer satisfies task requirements")
    actual_scope = EvidenceScope(
        enrollment_id=str(actual_attempt.assigned_enrollment_id),
        descriptor_version=actual_descriptor.descriptor_version,
        descriptor_hash=descriptor_hash,
        executor_kind=actual_descriptor.executor.kind,
        executor_version=actual_descriptor.executor.version,
        worker_protocol_version=(
            actual_descriptor.executor.worker_protocol_version
        ),
        model_provider=actual_model.provider,
        model_name=actual_model.name,
        model_digest=actual_model.digest,
        model_variant=actual_model.variant,
        task_class=actual_attempt.execution_unit_kind,
        evidence_role="production",
    )

    scopes: dict[str, EvidenceScope] = {actual_scope.scope_key: actual_scope}
    for claim in claims:
        if len(scopes) >= 64:
            break
        node_id = claim.node_id
        if node_id == actual_node_id:
            continue
        if claim.draining:
            continue
        if claim.blacklist_until > captured_at:
            continue
        enrollment_id = claim.enrollment_id
        descriptor_payload = claim.descriptor_payload
        descriptor_hash = claim.descriptor_hash
        descriptor_version = claim.descriptor_version
        if (
            not enrollment_id
            or descriptor_payload is None
            or not descriptor_hash
            or not descriptor_version
            or not claim.session_id
        ):
            continue
        try:
            descriptor = NodeCapabilityDescriptorV1.model_validate(descriptor_payload)
            if (
                descriptor.descriptor_version != descriptor_version
                or capability_descriptor_digest(descriptor) != descriptor_hash
            ):
                continue
            match = match_node_requirements(
                resource_requirements,
                required_capabilities,
                descriptor,
                claim.legacy_capabilities,
                preferred_model_name=claim.preferred_model_name,
                required_output_capacity_bytes=getattr(
                    actual_attempt, "max_output_bytes", None
                ),
            )
            model = match.selected_model
            if not match.eligible or model is None:
                continue
            scope = EvidenceScope(
                enrollment_id=str(enrollment_id),
                descriptor_version=descriptor.descriptor_version,
                descriptor_hash=str(descriptor_hash),
                executor_kind=descriptor.executor.kind,
                executor_version=descriptor.executor.version,
                worker_protocol_version=descriptor.executor.worker_protocol_version,
                model_provider=model.provider,
                model_name=model.name,
                model_digest=model.digest,
                model_variant=model.variant,
                task_class=actual_scope.task_class,
                evidence_role="production",
            )
        except Exception:
            continue
        scopes.setdefault(scope.scope_key, scope)

    return tuple(sorted(scopes.values(), key=lambda item: item.scope_key))


def _build_capability_shadow_evaluation(
    *,
    actual_attempt_id: str,
    actual_scope_key: str,
    candidate_scopes: tuple[EvidenceScope, ...],
    minimum_samples: int,
    decision_at: float,
) -> ShadowEvaluation:
    """Evaluate immutable assignment-time candidates without writing a decision."""

    candidates = [
        EligibleShadowCandidate(
            candidate_id=scope.scope_key,
            aggregate=capability_evidence_store.aggregate_read_only(
                scope,
                minimum_samples=minimum_samples,
                recorded_before=decision_at,
            ),
        )
        for scope in candidate_scopes
    ]
    return evaluate_shadow_preference(
        actual_attempt_id=actual_attempt_id,
        actual_candidate_id=actual_scope_key,
        candidates=candidates,
        minimum_samples=minimum_samples,
        decision_at=decision_at,
    )


def _evaluate_capability_shadow(
    *,
    actual_attempt_id: str,
    actual_scope_key: str,
    candidate_scopes: tuple[EvidenceScope, ...],
    minimum_samples: int,
    decision_at: float,
) -> None:
    """Compatibility helper for a contained synchronous shadow evaluation."""

    evaluation = _build_capability_shadow_evaluation(
        actual_attempt_id=actual_attempt_id,
        actual_scope_key=actual_scope_key,
        candidate_scopes=candidate_scopes,
        minimum_samples=minimum_samples,
        decision_at=decision_at,
    )
    capability_shadow_decision_store.record(evaluation)


def _persist_capability_shadow_decision(
    *,
    actual_attempt_id: str,
    evaluation: ShadowEvaluation,
) -> None:
    """Persist a decision and its truthful terminal health in one worker thread.

    An asyncio task can stop waiting after the bounded shutdown drain, but
    Python cannot cancel a SQLite call already running in a thread. Keeping the
    terminal classification in that same thread prevents a committed decision
    from being mislabeled as a shutdown cancellation.
    """

    try:
        capability_shadow_decision_store.record(evaluation)
    except Exception:
        _record_capability_shadow_terminal_direct(
            attempt_id=actual_attempt_id,
            outcome="decision_write_failed",
            reason_code="decision_write_failed",
        )
    else:
        _record_capability_shadow_terminal_direct(
            attempt_id=actual_attempt_id,
            outcome="completed",
            reason_code="decision_persisted",
        )
    finally:
        with _capability_shadow_lifecycle_lock:
            _capability_shadow_decision_writes.discard(actual_attempt_id)


def _shadow_decision_future_done(
    future: Future[Any],
    *,
    attempt_id: str,
) -> None:
    with _capability_shadow_lifecycle_lock:
        if _capability_shadow_decision_futures.get(attempt_id) is future:
            _capability_shadow_decision_futures.pop(attempt_id, None)


async def _run_capability_shadow_evaluation(
    *,
    actual_attempt_id: str,
    actual_scope_key: str,
    candidate_scopes: tuple[EvidenceScope, ...],
    minimum_samples: int,
    decision_at: float,
) -> None:
    """Contain evaluator and decision-write failures outside production work."""

    try:
        evaluation_future = _capability_shadow_work_executor.submit(
            _build_capability_shadow_evaluation,
            actual_attempt_id=actual_attempt_id,
            actual_scope_key=actual_scope_key,
            candidate_scopes=candidate_scopes,
            minimum_samples=minimum_samples,
            decision_at=decision_at,
        )
        evaluation = await _await_capability_shadow_future(evaluation_future)
    except asyncio.CancelledError:
        raise
    except Exception:
        _record_capability_shadow_terminal(
            attempt_id=actual_attempt_id,
            outcome="evaluator_failed",
            reason_code="evaluator_failed",
        )
        return

    with _capability_shadow_lifecycle_lock:
        if actual_attempt_id in _capability_shadow_terminal_claims:
            return
        _capability_shadow_decision_writes.add(actual_attempt_id)
    try:
        decision_future = _capability_shadow_work_executor.submit(
            _persist_capability_shadow_decision,
            actual_attempt_id=actual_attempt_id,
            evaluation=evaluation,
        )
        with _capability_shadow_lifecycle_lock:
            _capability_shadow_decision_futures[actual_attempt_id] = (
                decision_future
            )
        decision_future.add_done_callback(
            lambda future: _shadow_decision_future_done(
                future,
                attempt_id=actual_attempt_id,
            )
        )
        # Shield a running write so the daemon records whether it really
        # committed even if the outer pipeline stops waiting after the drain.
        await asyncio.shield(
            _await_capability_shadow_future(decision_future)
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - synchronous helper contains failures
        with _capability_shadow_lifecycle_lock:
            _capability_shadow_decision_writes.discard(actual_attempt_id)
        _record_capability_shadow_terminal_direct(
            attempt_id=actual_attempt_id,
            outcome="decision_write_failed",
            reason_code="decision_write_failed",
        )


async def _run_capability_shadow_pipeline(
    *,
    actual_attempt: Any,
    actual_descriptor: NodeCapabilityDescriptorV1,
    resource_requirements: dict[str, Any] | None,
    required_capabilities: tuple[str, ...],
    eligible_node_ids: tuple[str, ...],
    candidate_claims: tuple[_CapabilityShadowCandidateClaim, ...],
    minimum_samples: int,
    decision_at: float,
) -> None:
    """Capture scopes and evaluate entirely outside the production handout."""

    attempt_id = str(actual_attempt.attempt_id)
    try:
        capture_future = _capability_shadow_work_executor.submit(
            _capture_capability_shadow_scopes,
            actual_attempt=actual_attempt,
            actual_descriptor=actual_descriptor,
            resource_requirements=resource_requirements,
            required_capabilities=required_capabilities,
            eligible_node_ids=eligible_node_ids,
            captured_at=decision_at,
            candidate_claims=candidate_claims,
        )
        candidate_scopes = await _await_capability_shadow_future(capture_future)
        actual_scope = next(
            scope
            for scope in candidate_scopes
            if scope.enrollment_id == actual_attempt.assigned_enrollment_id
            and scope.descriptor_hash == actual_attempt.assigned_descriptor_hash
            and scope.model_provider == actual_attempt.assigned_model_provider
            and scope.model_name == actual_attempt.assigned_model_name
            and scope.model_digest == actual_attempt.assigned_model_digest
        )
    except asyncio.CancelledError:
        with _capability_shadow_lifecycle_lock:
            shutdown_requested = _capability_shadow_shutdown_requested
        if not shutdown_requested:
            capability_shadow_process_counters.increment(
                "unexpected_containment_failure"
            )
        raise
    except Exception:
        _schedule_capability_shadow_operation(
            attempt_id=attempt_id,
            phase="admission",
            outcome="scope_capture_failed",
            reason_code="scope_capture_failed",
            occurred_at=decision_at,
        )
        return
    finally:
        with _capability_shadow_lifecycle_lock:
            _capability_shadow_admissions.discard(attempt_id)

    _schedule_capability_shadow_operation(
        attempt_id=attempt_id,
        phase="admission",
        outcome="scheduled",
        reason_code="evaluation_scheduled",
        occurred_at=decision_at,
    )
    with _capability_shadow_lifecycle_lock:
        shutdown_requested = _capability_shadow_shutdown_requested
    if shutdown_requested:
        _record_capability_shadow_terminal(
            attempt_id=attempt_id,
            outcome="cancelled_on_shutdown",
            reason_code="coordinator_shutdown",
        )
        return
    await _run_capability_shadow_evaluation(
        actual_attempt_id=attempt_id,
        actual_scope_key=actual_scope.scope_key,
        candidate_scopes=candidate_scopes,
        minimum_samples=minimum_samples,
        decision_at=decision_at,
    )


def schedule_capability_shadow_evaluation(
    actual_attempt: Any,
    *,
    actual_descriptor: NodeCapabilityDescriptorV1 | None,
    resource_requirements: dict[str, Any] | None,
    required_capabilities: list[str] | tuple[str, ...],
    eligible_node_ids: list[str] | tuple[str, ...],
    decision_at: float,
) -> bool:
    """Schedule a bounded post-assignment diagnostic; never await evidence."""

    attempt_id = getattr(actual_attempt, "attempt_id", None)
    if not isinstance(attempt_id, str) or not attempt_id:
        capability_shadow_process_counters.increment(
            "unexpected_containment_failure"
        )
        return False
    try:
        occurred_at = float(decision_at)
        if not math.isfinite(occurred_at) or occurred_at < 0:
            raise ValueError("invalid decision timestamp")
    except (TypeError, ValueError):
        capability_shadow_process_counters.increment(
            "unexpected_containment_failure"
        )
        return False

    def _admission(
        outcome: ShadowAdmissionOutcome,
        reason_code: str,
    ) -> None:
        _schedule_capability_shadow_operation(
            attempt_id=attempt_id,
            phase="admission",
            outcome=outcome,
            reason_code=reason_code,
            occurred_at=occurred_at,
        )

    try:
        cfg = get_config()
        mode = str(cfg.get("capability_evidence_mode", "off"))
        minimum_samples = int(cfg.get("capability_evidence_min_samples", 5))
    except Exception:
        capability_shadow_process_counters.increment(
            "unexpected_containment_failure"
        )
        return False
    if mode != "shadow":
        _admission("disabled", "mode_disabled")
        return False
    if getattr(actual_attempt, "evidence_role", None) != "production":
        _admission("not_applicable", "nonproduction_attempt")
        return False
    if getattr(actual_attempt, "execution_unit_kind", None) not in {
        "dag_subtask",
        "candidate",
    }:
        _admission("not_applicable", "unsupported_task_class")
        return False
    if (
        actual_descriptor is None
        or getattr(actual_attempt, "assigned_descriptor_hash", None) is None
    ):
        _admission("not_applicable", "legacy_descriptor_identity")
        return False
    if len(_capability_shadow_tasks) >= _CAPABILITY_SHADOW_TASK_LIMIT:
        _admission(
            "queue_saturated",
            "background_queue_limit_reached",
        )
        return False

    try:
        loop = asyncio.get_running_loop()
        candidates = tuple(
            sorted(
                {
                    str(node_id)
                    for node_id in eligible_node_ids
                    if isinstance(node_id, str) and node_id
                }
            )[:64]
        )
        candidate_claims = _snapshot_capability_shadow_candidate_claims(
            actual_node_id=str(actual_attempt.assigned_node_id),
            eligible_node_ids=candidates,
        )
    except Exception:
        _admission("scope_capture_failed", "scope_capture_failed")
        return False

    try:
        with _capability_shadow_lifecycle_lock:
            if _capability_shadow_shutdown_requested:
                task = None
            else:
                _capability_shadow_admissions.add(attempt_id)
                task = loop.create_task(
                    _run_capability_shadow_pipeline(
                        actual_attempt=actual_attempt,
                        actual_descriptor=actual_descriptor,
                        resource_requirements=resource_requirements,
                        required_capabilities=tuple(required_capabilities),
                        eligible_node_ids=candidates,
                        candidate_claims=candidate_claims,
                        minimum_samples=minimum_samples,
                        decision_at=occurred_at,
                    )
                )
                _capability_shadow_tasks.add(task)
                _capability_shadow_task_attempt_ids[task] = attempt_id
                _capability_shadow_task_decision_times[task] = occurred_at
        if task is None:
            _admission(
                "scope_capture_failed",
                "coordinator_shutdown_during_scope_capture",
            )
            return False
        task.add_done_callback(_shadow_task_done)
        return True
    except Exception:
        with _capability_shadow_lifecycle_lock:
            _capability_shadow_admissions.discard(attempt_id)
        capability_shadow_process_counters.increment(
            "unexpected_containment_failure"
        )
        _admission("scope_capture_failed", "scope_capture_failed")
        return False


async def shutdown_capability_shadow_evaluations() -> None:
    """Classify unfinished work and drain it for a finite graceful interval."""

    global _capability_shadow_shutdown_requested
    with _capability_shadow_lifecycle_lock:
        _capability_shadow_shutdown_requested = True
        evaluations = tuple(_capability_shadow_tasks)
    cancellation_records: list[str] = []
    for task in evaluations:
        if task.done():
            continue
        attempt_id = _capability_shadow_task_attempt_ids.get(task)
        cancel_task = False
        with _capability_shadow_lifecycle_lock:
            admission_in_progress = attempt_id in _capability_shadow_admissions
            decision_write_in_progress = (
                attempt_id in _capability_shadow_decision_writes
            )
            terminal_owned = (
                attempt_id in _capability_shadow_terminal_claims
            )
            decision_future = _capability_shadow_decision_futures.get(
                attempt_id
            )
            cancellation_reserved = False
            if terminal_owned:
                cancel_task = True
            elif admission_in_progress:
                pass
            elif decision_write_in_progress:
                if decision_future is not None and decision_future.cancel():
                    _capability_shadow_decision_writes.discard(attempt_id)
                    cancellation_reserved = bool(
                        attempt_id is not None
                        and _reserve_capability_shadow_terminal(
                            attempt_id,
                            "cancelled_on_shutdown",
                        )
                    )
                    cancel_task = True
            else:
                cancellation_reserved = bool(
                    attempt_id is not None
                    and _reserve_capability_shadow_terminal(
                        attempt_id,
                        "cancelled_on_shutdown",
                    )
                )
                cancel_task = True
        if cancellation_reserved and attempt_id is not None:
            cancellation_records.append(attempt_id)
        if cancel_task:
            task.cancel()
    for attempt_id in cancellation_records:
        _schedule_capability_shadow_operation(
            attempt_id=attempt_id,
            phase="evaluation",
            outcome="cancelled_on_shutdown",
            reason_code="coordinator_shutdown",
            occurred_at=time.time(),
        )

    pending: set[asyncio.Task] = set()
    if evaluations:
        _done, pending = await asyncio.wait(
            evaluations,
            timeout=_CAPABILITY_SHADOW_SHUTDOWN_DRAIN_SECONDS,
        )

    timed_out_captures: list[tuple[str, float]] = []
    timed_out_cancellations: list[str] = []
    for task in pending:
        if task.done():
            continue
        attempt_id = _capability_shadow_task_attempt_ids.get(task)
        with _capability_shadow_lifecycle_lock:
            admission_in_progress = attempt_id in _capability_shadow_admissions
            decision_write_in_progress = (
                attempt_id in _capability_shadow_decision_writes
            )
            terminal_owned = (
                attempt_id in _capability_shadow_terminal_claims
            )
            decision_future = _capability_shadow_decision_futures.get(
                attempt_id
            )
            if terminal_owned:
                pass
            elif admission_in_progress and attempt_id is not None:
                _capability_shadow_admissions.discard(attempt_id)
                admission_time = _capability_shadow_task_decision_times.get(task)
                if admission_time is None:
                    capability_shadow_process_counters.increment(
                        "unexpected_containment_failure"
                    )
                    admission_time = time.time()
                timed_out_captures.append((attempt_id, admission_time))
            elif decision_write_in_progress:
                if decision_future is not None and decision_future.cancel():
                    _capability_shadow_decision_writes.discard(attempt_id)
                    if attempt_id is not None and (
                        _reserve_capability_shadow_terminal(
                            attempt_id,
                            "cancelled_on_shutdown",
                        )
                    ):
                        timed_out_cancellations.append(attempt_id)
                elif decision_future is not None and decision_future.running():
                    # The daemon records completed/write-failed truth when its
                    # bounded SQLite operation returns. Until then the report
                    # correctly shows this scheduled assignment as pending.
                    capability_shadow_process_counters.increment(
                        "unexpected_containment_failure"
                    )
                elif decision_future is not None and decision_future.done():
                    # The synchronous writer claims a terminal before setting
                    # its Future result; reaching this branch is containment
                    # failure rather than a normal drain-boundary completion.
                    capability_shadow_process_counters.increment(
                        "unexpected_containment_failure"
                    )
                else:
                    _capability_shadow_decision_writes.discard(attempt_id)
                    capability_shadow_process_counters.increment(
                        "unexpected_containment_failure"
                    )
            elif attempt_id is not None and _reserve_capability_shadow_terminal(
                attempt_id,
                "cancelled_on_shutdown",
            ):
                timed_out_cancellations.append(attempt_id)
            else:
                capability_shadow_process_counters.increment(
                    "unexpected_containment_failure"
                )
        task.cancel()

    for attempt_id, admission_time in timed_out_captures:
        _schedule_capability_shadow_operation(
            attempt_id=attempt_id,
            phase="admission",
            outcome="scope_capture_failed",
            reason_code="coordinator_shutdown_during_scope_capture",
            occurred_at=admission_time,
        )
    for attempt_id in timed_out_cancellations:
        _schedule_capability_shadow_operation(
            attempt_id=attempt_id,
            phase="evaluation",
            outcome="cancelled_on_shutdown",
            reason_code="coordinator_shutdown",
            occurred_at=time.time(),
        )

    # Cancels queued optional calls and never joins already-running daemon work.
    # This is the process-exit boundary that asyncio's default executor cannot
    # provide for best-effort telemetry.
    _capability_shadow_work_executor.close()

    if pending:
        _done, still_pending = await asyncio.wait(
            pending,
            timeout=_CAPABILITY_SHADOW_SHUTDOWN_CANCEL_SECONDS,
        )
        for _task in still_pending:
            capability_shadow_process_counters.increment(
                "unexpected_containment_failure"
            )

    health_tasks = tuple(_capability_shadow_health_tasks)
    if health_tasks:
        _done, pending = await asyncio.wait(health_tasks, timeout=1.0)
        for task in pending:
            task.cancel()
        if pending:
            _done, still_pending = await asyncio.wait(
                pending,
                timeout=_CAPABILITY_SHADOW_SHUTDOWN_CANCEL_SECONDS,
            )
            for _task in still_pending:
                capability_shadow_process_counters.increment(
                    "background_task_callback_failure"
                )
    _capability_shadow_health_executor.close()


def begin_capability_shadow_runtime() -> None:
    """Allow background admission at the start of one coordinator lifespan."""

    global _capability_shadow_health_executor
    global _capability_shadow_shutdown_requested
    global _capability_shadow_work_executor
    with _capability_shadow_lifecycle_lock:
        _capability_shadow_work_executor.close()
        _capability_shadow_health_executor.close()
        _capability_shadow_work_executor = _new_capability_shadow_work_executor()
        _capability_shadow_health_executor = (
            _new_capability_shadow_health_executor()
        )
        _capability_shadow_shutdown_requested = False


def node_enrollment_required() -> bool:
    """Whether this coordinator rejects legacy shared-secret-only registration."""

    return str(get_config().get("node_enrollment_mode", "compat")) == "required"


def _clear_worker_assignment(task: dict[str, Any]) -> None:
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


def reclaim_enrollment_work(enrollment_id: str, reason: str) -> list[str]:
    """Close and safely requeue work held by an invalid durable enrollment.

    The durable attempt transition happens first.  A process-local task is
    removed only when ``AttemptStore`` confirms that its active attempt changed,
    preserving the existing fail-closed reclaim ordering.
    """

    normalized = str(enrollment_id or "").strip().lower()
    if not normalized:
        return []
    try:
        reclaimed_ids = set(attempt_store.reclaim_enrollment(normalized, reason))
    except Exception as exc:
        _safe_diagnostic_emit(
            "attempt_reclaim_failed",
            {
                "enrollment_id": normalized,
                "phase": "enrollment_reclaim",
                "error_type": type(exc).__name__,
            },
        )
        return []

    now = time.time()
    requeued: list[str] = []
    with _task_queue_lock:
        for task_id in reclaimed_ids:
            task = task_inflight.get(task_id)
            if task is None or task.get("assigned_enrollment_id") != normalized:
                continue
            task = task_inflight.pop(task_id)
            assigned_node = task.get("assigned_to")
            _clear_worker_assignment(task)
            deadline = task.get("execution_deadline_at")
            if not deadline or float(deadline) > now:
                task_queue.append(task)
                requeued.append(task_id)
            _emit(
                "task_reclaimed",
                {
                    "task_id": task_id,
                    "node_id": assigned_node,
                    "enrollment_id": normalized,
                    "reason": reason,
                },
            )

    for node_id, node in list(nodes.items()):
        if node.get("enrollment_id") == normalized:
            waiting_nodes.pop(node_id, None)
            nodes.pop(node_id, None)
    node_sessions.invalidate_enrollment(normalized)
    return requeued

# Persisted events are operational lifecycle metadata, not a second prompt or
# result log.  Keep this an allowlist: newly introduced payload fields remain
# private until they are deliberately classified as structural telemetry.
_GENERIC_SAFE_EVENT_FIELDS = frozenset(
    {
        "execution_id",
        "job_id",
        "trace_id",
        "task_id",
        "attempt_id",
        "unit_id",
        "candidate_id",
        "node_id",
        "enrollment_id",
        "session_id",
        "status",
        "lifecycle_status",
        "phase",
        "stage",
        "error_type",
        "error_code",
        "retryable",
    }
)
_SAFE_EVENT_FIELDS = {
    "pitch": frozenset({"trace_id", "job_id", "mode"}),
    "plan": frozenset({"trace_id", "job_id", "subtask_count"}),
    "build": frozenset({"trace_id", "job_id", "subtask_id"}),
    "review_start": frozenset({"trace_id", "job_id"}),
    "complete": frozenset({"execution_id", "trace_id", "job_id", "project_dir"}),
    "error": frozenset({"trace_id", "job_id", "error_type"}),
    "cancelling": frozenset(
        {"trace_id", "job_id", "dropped", "still_running"}
    ),
    "cancelled": frozenset(
        {"trace_id", "job_id", "stage", "completed", "credits"}
    ),
    "node_task_queued": frozenset(
        {"trace_id", "job_id", "task_id", "verification"}
    ),
    "node_readmitted": frozenset({"node_id", "enrollment_id"}),
    "verification": frozenset(
        {
            "execution_id",
            "job_id",
            "trace_id",
            "unit_id",
            "agreed",
            "nodes",
            "enrollment_id_a",
            "enrollment_id_b",
        }
    ),
    "execution_created": frozenset(
        {"execution_id", "job_id", "protocol_version"}
    ),
    "strategy_selected": frozenset(
        {"execution_id", "strategy_requested", "strategy_selected"}
    ),
    "execution_running": frozenset({"execution_id", "lifecycle_status"}),
    "execution_completed": frozenset(
        {"execution_id", "status", "lifecycle_status", "assurance_level"}
    ),
    "execution_failed": frozenset(
        {"execution_id", "status", "lifecycle_status", "assurance_level"}
    ),
    "execution_timed_out": frozenset(
        {"execution_id", "status", "lifecycle_status", "assurance_level"}
    ),
    "execution_cancelled": frozenset(
        {"execution_id", "status", "lifecycle_status", "assurance_level"}
    ),
    "execution_interrupted": frozenset(
        {
            "execution_id",
            "status",
            "lifecycle_status",
            "assurance_level",
            "retryable",
        }
    ),
    "execution_callback_failed": frozenset({"execution_id", "stage"}),
    "execution_persistence_failed": frozenset(
        {"execution_id", "lifecycle_status", "attempts"}
    ),
    "execution_terminal_persistence_failed": frozenset(
        {"execution_id", "lifecycle_status", "attempts"}
    ),
    "execution_unit_queued": frozenset(
        {"execution_id", "unit_id", "task_id", "placement"}
    ),
    "placement_fallback": frozenset(
        {"execution_id", "unit_id", "placement_selected"}
    ),
    "attempt_started": frozenset(
        {
            "execution_id",
            "task_id",
            "attempt_id",
            "unit_id",
            "node_id",
            "enrollment_id",
            "descriptor_version",
            "descriptor_hash",
            "placement",
        }
    ),
    "attempt_completed": frozenset(
        {
            "execution_id",
            "task_id",
            "attempt_id",
            "unit_id",
            "unit_kind",
            "node_id",
            "enrollment_id",
            "descriptor_version",
            "descriptor_hash",
            "placement",
            "status",
            "duration_ms",
        }
    ),
    "review_started": frozenset({"execution_id", "strategy"}),
    "revision_started": frozenset(
        {"execution_id", "strategy", "revision_pass"}
    ),
    "candidate_generated": frozenset(
        {"execution_id", "candidate_id", "status", "output_bytes"}
    ),
    "candidate_validation_completed": frozenset(
        {"execution_id", "candidate_id", "validator", "status"}
    ),
    "candidate_rejected": frozenset({"execution_id", "candidate_id"}),
    "winner_selected": frozenset(
        {"execution_id", "candidate_id", "verified"}
    ),
    "node_draining": frozenset({"node_id", "enrollment_id", "session_id"}),
    "node_busy": frozenset({"node_id", "enrollment_id", "task_id", "unit_id"}),
    "node_idle": frozenset(
        {
            "node_id",
            "enrollment_id",
            "credits_earned",
            "contribution_basis",
            "points_are_monetary",
            "elapsed_seconds",
            "success",
            "trace_id",
        }
    ),
    "node_blacklisted": frozenset(
        {"node_id", "enrollment_id", "failure_count", "blacklist_seconds"}
    ),
    "task_reclaimed": frozenset({"task_id", "node_id", "enrollment_id"}),
    "worker_task_expired": frozenset(
        {"task_id", "execution_id", "node_id", "enrollment_id"}
    ),
    "result_rejected": frozenset(
        {
            "task_id",
            "claimed_by",
            "assigned_to",
            "attempt_state",
            "error_code",
            "quarantined",
            "quarantine_id",
            "enrollment_id",
        }
    ),
    "stream_limit_exceeded": frozenset(
        {
            "task_id",
            "attempt_id",
            "node_id",
            "enrollment_id",
            "max_output_bytes",
            "streamed_bytes",
            "stream_batch_count",
        }
    ),
    "attempt_expiry_failed": frozenset(
        {"task_id", "node_id", "enrollment_id", "phase", "error_type"}
    ),
    "attempt_reclaim_failed": frozenset(
        {"task_id", "node_id", "enrollment_id", "phase", "error_type"}
    ),
    "attempt_issue_failed": frozenset(
        {"task_id", "node_id", "enrollment_id", "phase", "error_type"}
    ),
    "enrollment_revalidation_failed": frozenset(
        {"node_id", "enrollment_id", "phase", "error_type"}
    ),
    "post_settlement_mirror_failed": frozenset(
        {"task_id", "node_id", "enrollment_id", "phase", "error_type"}
    ),
    "output_pruned": frozenset({"runs_deleted_count", "cap_mb"}),
    # Generated token text is allowed only for an ephemeral live broadcast.
    # The durable sanitizer and startup migration never retain ``token``.
    "token": frozenset(
        {"job_id", "trace_id", "subtask_id", "source", "node_id", "enrollment_id"}
    ),
    "token_fanout_truncated": frozenset(),
}
_SAFE_EVENT_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,159}$")
_SAFE_EVENT_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\\-]{0,511}$")
_SAFE_EVENT_NUMBERS = frozenset(
    {
        "subtask_count",
        "revision_pass",
        "output_bytes",
        "duration_ms",
        "credits_earned",
        "elapsed_seconds",
        "failure_count",
        "blacklist_seconds",
        "max_output_bytes",
        "streamed_bytes",
        "stream_batch_count",
        "cap_mb",
        "attempts",
        "dropped",
        "still_running",
        "completed",
        "credits",
        "runs_deleted_count",
    }
)
_SAFE_EVENT_BOOLEANS = frozenset(
    {
        "agreed",
        "verified",
        "verification",
        "retryable",
        "points_are_monetary",
        "success",
        "quarantined",
    }
)
_SAFE_EVENT_LISTS = frozenset({"nodes"})


def _safe_event_value(field: str, value: Any) -> Any:
    """Return a bounded structural value, or ``None`` when it is unsafe."""

    if value is None:
        return None
    if field in _SAFE_EVENT_BOOLEANS:
        return value if isinstance(value, bool) else None
    if field in _SAFE_EVENT_NUMBERS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value if math.isfinite(value) else None
    if field in _SAFE_EVENT_LISTS:
        if not isinstance(value, list) or len(value) > 100:
            return None
        if not all(
            isinstance(item, str) and _SAFE_EVENT_CODE.fullmatch(item)
            for item in value
        ):
            return None
        return list(value)
    if field == "project_dir":
        return value if isinstance(value, str) and _SAFE_EVENT_PATH.fullmatch(value) else None
    if field == "subtask_id" and isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and _SAFE_EVENT_CODE.fullmatch(value):
        return value
    return None


def _sanitize_event_payload(
    event_type: str,
    data: Any,
    *,
    allow_ephemeral_token: bool = False,
) -> dict[str, Any]:
    """Keep only stable structural telemetry for one event payload."""

    if not isinstance(data, dict):
        return {}
    allowed = _SAFE_EVENT_FIELDS.get(event_type, _GENERIC_SAFE_EVENT_FIELDS)
    sanitized: dict[str, Any] = {}
    for field in allowed:
        if field not in data:
            continue
        value = _safe_event_value(field, data[field])
        if value is not None or data[field] is None:
            sanitized[field] = value
    if event_type == "plan" and "subtask_count" not in sanitized:
        subtasks = data.get("subtasks")
        if isinstance(subtasks, list):
            sanitized["subtask_count"] = len(subtasks)
    if event_type == "output_pruned" and "runs_deleted_count" not in sanitized:
        runs_deleted = data.get("runs_deleted")
        if isinstance(runs_deleted, list):
            sanitized["runs_deleted_count"] = len(runs_deleted)
    if allow_ephemeral_token and event_type == "token":
        token = data.get("token")
        if isinstance(token, str):
            sanitized["token"] = token
    return sanitized


def _sanitize_event_record(
    event: Any,
    *,
    allow_ephemeral_token: bool = False,
) -> dict[str, Any]:
    """Sanitize a flattened in-memory/WebSocket event record."""

    if not isinstance(event, dict):
        return {}
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return {}
    cleaned = {"type": event_type}
    if isinstance(event.get("time"), str):
        cleaned["time"] = event["time"]
    cleaned.update(
        _sanitize_event_payload(
            event_type,
            event,
            allow_ephemeral_token=allow_ephemeral_token,
        )
    )
    if isinstance(event.get("id"), int) and not isinstance(event["id"], bool):
        cleaned["id"] = event["id"]
    return cleaned


def _decode_persisted_event(
    event_id: int,
    event_type: str,
    event_time: str,
    blob: str,
) -> dict[str, Any]:
    """Decode a durable event without trusting legacy JSON payloads."""

    try:
        data = json.loads(blob)
    except (TypeError, json.JSONDecodeError):
        data = {}
    event = {
        "id": event_id,
        "type": event_type,
        "time": event_time,
        **_sanitize_event_payload(event_type, data),
    }
    return event


def _redact_events_in_transaction(con: sqlite3.Connection) -> None:
    """Idempotently remove prompt/output text from historical event rows."""

    rows = con.execute("SELECT id, type, time, data FROM events ORDER BY id").fetchall()
    for event_id, event_type, event_time, blob in rows:
        event = _decode_persisted_event(event_id, event_type, event_time, blob)
        safe_blob = json.dumps(
            {key: value for key, value in event.items() if key not in {"id", "type", "time"}},
            sort_keys=True,
            separators=(",", ":"),
        )
        if blob != safe_blob:
            con.execute("UPDATE events SET data = ? WHERE id = ?", (safe_blob, event_id))


def _redact_in_memory_events() -> None:
    """Apply the durable event policy to any same-process compatibility cache."""

    pipeline_events[:] = [
        cleaned
        for event in pipeline_events
        if (cleaned := _sanitize_event_record(event))
    ]


def _init_db() -> None:
    # Registration sessions are intentionally not durable.  Calling startup
    # initialization represents a new coordinator epoch, even in an in-process
    # test client, so every previous bearer token must stop authorizing work.
    node_sessions.reset()
    with _db_lock, migration_lock(_DB_PATH), connection(_DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id   INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                time TEXT NOT NULL,
                data TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id      TEXT PRIMARY KEY,
                task        TEXT,
                project_id  TEXT,
                status      TEXT,
                submitted_at TEXT,
                finished_at TEXT,
                error       TEXT,
                project_dir TEXT,
                rating      TEXT,
                trace_id    TEXT
            )
        """)
        existing_job_columns = {
            row[1] for row in con.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for name, declaration in {
            "started_at": "TEXT",
            "interrupted_at": "TEXT",
            "interruption_reason": "TEXT",
            "coordinator_restart_marker": "TEXT",
            "retryable": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in existing_job_columns:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
        _redact_events_in_transaction(con)
        con.commit()
    _redact_in_memory_events()
    enrollment_store.migrate()
    capability_snapshot_store.migrate()
    attempt_store.migrate()
    capability_evidence_store.migrate()
    _migrate_capability_shadow_decision_store()
    _migrate_capability_shadow_operational_store()
    # Redact legacy free-form contribution metadata and regenerate the JSON
    # projection before any read route can expose it.
    from ledger import sync_compatibility_ledger

    sync_compatibility_ledger(db_path=_DB_PATH)
    # A live attempt cannot be resumed after coordinator restart because the
    # worker queue is process-local. Fail it closed instead of accepting a late
    # result into a new process that has no matching dispatcher wait.
    attempt_store.interrupt_active(
        "coordinator restarted; process-local worker task is no longer resumable"
    )
    _reconcile_capability_evidence()
    accepted_result_broker.clear()


def _db_write_job(job: dict) -> None:
    with _db_lock, connection(_DB_PATH) as con:
        con.execute(
            """INSERT OR REPLACE INTO jobs
               (job_id, task, project_id, status, submitted_at, started_at,
                finished_at, error, project_dir, rating, trace_id,
                interrupted_at, interruption_reason,
                coordinator_restart_marker, retryable)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job["job_id"], job.get("task"), job.get("project_id"),
                job.get("status"), job.get("submitted_at"), job.get("started_at"),
                job.get("finished_at"),
                job.get("error"),
                job["result"]["project_dir"] if job.get("result") else None,
                job["result"].get("rating") if job.get("result") else None,
                job.get("trace_id"),
                job.get("interrupted_at"),
                job.get("interruption_reason"),
                job.get("coordinator_restart_marker"),
                int(bool(job.get("retryable", False))),
            ),
        )
        con.commit()


def _db_load_jobs() -> None:
    """Reconcile non-resumable jobs, then load the latest durable records."""
    try:
        with _db_lock, connection(_DB_PATH, row_factory=sqlite3.Row) as con:
            interrupted_at = datetime.now(timezone.utc).isoformat()
            reason = (
                "coordinator restarted; process-local execution task is no longer resumable"
            )
            con.execute(
                """
                UPDATE jobs
                SET status = 'interrupted', interrupted_at = ?,
                    interruption_reason = ?, coordinator_restart_marker = ?,
                    retryable = 1, finished_at = COALESCE(finished_at, ?),
                    error = COALESCE(error, ?)
                WHERE status IN ('queued', 'running')
                """,
                (
                    interrupted_at,
                    reason,
                    _COORDINATOR_RESTART_MARKER,
                    interrupted_at,
                    reason,
                ),
            )
            con.commit()
            rows = con.execute(
                "SELECT * FROM jobs ORDER BY submitted_at DESC LIMIT 200"
            ).fetchall()
        for row in rows:
            jid = row["job_id"]
            # Startup reconciliation has just made this row authoritative.
            # Replace any stale same-process projection as well as loading
            # missing rows, so repeated lifespan/test startup cannot leave an
            # in-memory queued/running mirror ahead of durable interruption.
            jobs[jid] = {
                "job_id": jid,
                "task": row["task"],
                "project_id": row["project_id"],
                "status": row["status"],
                "submitted_at": row["submitted_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "error": row["error"],
                "result": {"project_dir": row["project_dir"], "rating": row["rating"]}
                if row["project_dir"]
                else None,
                "trace_id": row["trace_id"],
                "interrupted_at": row["interrupted_at"],
                "interruption_reason": row["interruption_reason"],
                "coordinator_restart_marker": row["coordinator_restart_marker"],
                "retryable": bool(row["retryable"]),
            }
    except Exception as exc:
        logging.getLogger("mycelium.jobs").error(
            "failed to reconcile/load legacy jobs error_type=%s",
            type(exc).__name__,
        )


def _db_write_event(event_type: str, event_time: str, data: dict) -> int:
    blob = _sanitize_event_payload(event_type, data)
    with _db_lock, connection(_DB_PATH) as con:
        cur = con.execute(
            "INSERT INTO events (type, time, data) VALUES (?, ?, ?)",
            (event_type, event_time, json.dumps(blob)),
        )
        rowid = cur.lastrowid
        con.commit()
    return rowid


# ── WebSocket connection manager ──────────────────────────────────────
_WS_QUEUE_MAX = 64


class _WSManager:
    def __init__(self):
        # One bounded queue and one sender task per viewer.  A slow socket can
        # therefore retain at most ``_WS_QUEUE_MAX`` events instead of causing a
        # new, indefinitely blocked broadcast task for every token batch.
        self._connections: dict[WebSocket, dict[str, Any]] = {}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_WS_QUEUE_MAX)
        sender = asyncio.create_task(self._send_loop(ws, queue))
        self._connections[ws] = {
            "queue": queue,
            "sender": sender,
            "truncation_notified": False,
        }

    def disconnect(self, ws: WebSocket):
        connection = self._connections.pop(ws, None)
        if connection is None:
            return
        sender = connection["sender"]
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if sender is not current and not sender.done():
            sender.cancel()

    async def _send_loop(self, ws: WebSocket, queue: asyncio.Queue[dict]):
        try:
            while True:
                event = await queue.get()
                try:
                    await ws.send_json(event)
                finally:
                    queue.task_done()
        except (asyncio.CancelledError, Exception):
            self.disconnect(ws)

    def publish(self, data: dict) -> None:
        """Non-blockingly fan out an event with a bounded slow-viewer policy."""

        data = _sanitize_event_record(data, allow_ephemeral_token=True)
        if not data:
            return

        for ws, client_state in list(self._connections.items()):
            queue: asyncio.Queue[dict] = client_state["queue"]
            try:
                queue.put_nowait(data)
                continue
            except asyncio.QueueFull:
                pass

            if data.get("type") == "token":
                # Drop high-frequency token data for this viewer and enqueue one
                # explicit truncation signal.  The worker attempt itself is not
                # truncated; only this slow viewer's live projection is.
                if client_state["truncation_notified"]:
                    continue
                try:
                    queue.get_nowait()
                    queue.task_done()
                except asyncio.QueueEmpty:  # pragma: no cover - queue race guard
                    pass
                client_state["truncation_notified"] = True
                try:
                    queue.put_nowait({
                        "type": "token_fanout_truncated",
                        "time": datetime.now(timezone.utc).isoformat(),
                        "reason": "slow viewer exceeded bounded live-event queue",
                    })
                except asyncio.QueueFull:  # pragma: no cover - defensive
                    self.disconnect(ws)
                continue

            # Preserve lower-frequency lifecycle events by dropping the oldest
            # queued projection.  Memory remains bounded and the latest state is
            # more useful than stale intermediate state to a lagging viewer.
            try:
                queue.get_nowait()
                queue.task_done()
                queue.put_nowait(data)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                self.disconnect(ws)

    async def broadcast(self, data: dict):
        """Compatibility coroutine for callers that previously awaited sends."""

        self.publish(data)

    @property
    def queued_event_count(self) -> int:
        return sum(connection["queue"].qsize() for connection in self._connections.values())


ws_manager = _WSManager()


# ── Event emission ────────────────────────────────────────────────────

def _emit(event_type: str, data: dict):
    """Push an event to the pipeline log, SQLite, and broadcast to WebSocket clients."""
    event = {
        "type": event_type,
        "time": datetime.now(timezone.utc).isoformat(),
        **_sanitize_event_payload(
            event_type,
            data,
            allow_ephemeral_token=True,
        ),
    }
    # Token events are high-frequency — broadcast only, don't pollute the event log
    if event_type != "token":
        pipeline_events.append(event)
        if len(pipeline_events) > 100:
            pipeline_events.pop(0)
        event["id"] = _db_write_event(event_type, event["time"], data)

    # Emitting from a sync context (a script, a test, the CLI) must record the
    # event rather than blow up: with no loop running there is no WebSocket
    # client to broadcast to anyway. get_event_loop() used to be used here,
    # which raises on Python 3.12+ outside a coroutine.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    ws_manager.publish(event)


def _safe_diagnostic_emit(event_type: str, data: dict) -> None:
    """Publish secret-safe diagnostics without masking the original failure."""

    try:
        _emit(event_type, data)
    except Exception as exc:
        logger.error(
            "diagnostic event publication failed event_type=%s error_type=%s",
            event_type,
            type(exc).__name__,
        )


# ── Rate limiting ─────────────────────────────────────────────────────
def _rate_limits() -> tuple[int, int]:
    """(max pitches, window seconds) per IP, from config.

    The module constants stay as the fallback so a malformed config can never
    disable the limiter — it just reverts to the safe default.
    """
    cfg = get_config()
    try:
        return int(cfg.get("pitch_rate_max", _RATE_MAX)), int(
            cfg.get("pitch_rate_window", _RATE_WINDOW)
        )
    except (TypeError, ValueError):
        return _RATE_MAX, _RATE_WINDOW


def _check_rate_limit(request: Request) -> int:
    """Raise 429 if this IP has exceeded the configured pitch rate.

    Returns the number of remaining pitches allowed in the current window.
    """
    rate_max, rate_window = _rate_limits()
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window_start = now - rate_window
    timestamps = [t for t in _pitch_timestamps.get(ip, []) if t > window_start]
    if len(timestamps) >= rate_max:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: max {rate_max} pitches per {rate_window}s. Try again shortly.",
            headers={
                "X-RateLimit-Limit": str(rate_max),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(min(timestamps)) + rate_window),
            },
        )
    timestamps.append(now)
    _pitch_timestamps[ip] = timestamps
    return rate_max - len(timestamps)


# ── Pitch auth ───────────────────────────────────────────────────────
def _check_pitch_key(request: Request):
    """Raise 401 if pitch_key is configured and the request doesn't present it.

    Pitchers must include 'X-Pitch-Key: <value>' in their request headers.
    When pitch_key is empty in config, pitching is open (trusted-network mode).
    """
    key = get_config().get("pitch_key", "")
    if not key:
        return  # auth disabled
    provided = request.headers.get("X-Pitch-Key", "")
    if not secrets.compare_digest(str(provided), str(key)):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Pitch-Key header")


# ── Node auth ────────────────────────────────────────────────────────
def _check_node_auth(request: Request):
    """Require the shared secret for bootstrap or explicit legacy compatibility.

    Returning enrolled workers and their normal operations use per-enrollment
    credentials and process-local sessions instead.
    """
    secret = get_config().get("node_secret", "")
    if not secret:
        return  # auth disabled
    provided = request.headers.get("X-Node-Secret", "")
    if not secrets.compare_digest(str(provided), str(secret)):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Node-Secret header")


def _check_node_session(request: Request, node_id: str) -> NodeSessionRecord:
    """Require a current session and validate its durable enrollment binding.

    Enrolled sessions are sufficient authority for normal worker operations.
    The deployment-wide admission secret remains required only for legacy
    compatibility sessions and initial bootstrap.
    """

    provided = request.headers.get("X-Node-Session")
    try:
        record = node_sessions.authenticate(node_id, provided)
    except InvalidNodeSession as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "node_session_rejected",
                "message": exc.reason,
                "action": "register_again",
            },
            headers={"X-Node-Session-Required": "true"},
        ) from exc

    if record.enrollment_id is None:
        if node_enrollment_required():
            node_sessions.invalidate_node(
                record.node_id, session_id=record.session_id
            )
            nodes.pop(record.node_id, None)
            raise HTTPException(
                status_code=426,
                detail={
                    "code": "durable_node_enrollment_required",
                    "message": (
                        "This coordinator requires durable node enrollment; "
                        "upgrade the worker and register with an enrollment credential."
                    ),
                    "action": "upgrade_worker",
                },
                headers={"X-Node-Enrollment-Required": "true"},
            )
        _check_node_auth(request)
        return record

    try:
        enrollment_store.validate_session(
            record.enrollment_id,
            record.node_id,
            record.credential_version or 0,
        )
    except NodeEnrollmentError as exc:
        reclaim_enrollment_work(record.enrollment_id, exc.reason)
        if isinstance(exc, EnrollmentRevoked):
            status_code = 403
            action = "stop_and_contact_operator"
        elif isinstance(exc, EnrollmentCredentialRotated):
            status_code = 401
            action = "reload_identity_and_register_again"
        elif isinstance(exc, EnrollmentSessionMismatch):
            status_code = 401
            action = "register_again"
        else:
            status_code = 401
            action = "register_again"
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": exc.code,
                "message": exc.reason,
                "action": action,
            },
            headers={"X-Node-Session-Required": "true"},
        ) from exc
    return record


# ── Request/response models ──────────────────────────────────────────
class PitchRequest(BaseModel):
    task: str
    project_id: str | None = None   # optional: continue an existing project
    strategy: StrategyNameV1 = "auto"
    strategy_options: StrategyOptionsV1 | None = None
    candidates: int | None = Field(default=None, ge=1, le=5)
    placement: PlacementV1 | None = None
    requirements: ExecutionRequirementsV1 = Field(default_factory=ExecutionRequirementsV1)
    output_contract: OutputContractV1 | None = None
    verification: VerificationPolicyV1 = Field(default_factory=VerificationPolicyV1)
    confidentiality: ConfidentialityV1 = "trusted_guild"

    @field_validator("task")
    @classmethod
    def task_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("task cannot be empty")
        if len(v) > 1000:
            raise ValueError("task must be 1000 characters or fewer")
        return v


class PitchResponse(BaseModel):
    project_dir: str
    plan: list[dict]
    results: dict[str, str]
    review: str


class NodeRegistration(BaseModel):
    node_id: str = Field(min_length=1, max_length=64)
    enrollment_action: Literal["bootstrap", "returning"] | None = None
    enrollment_credential: str | None = Field(
        default=None,
        min_length=ENROLLMENT_CREDENTIAL_MIN_LENGTH,
        max_length=ENROLLMENT_CREDENTIAL_MAX_LENGTH,
        repr=False,
    )
    model: str = Field(min_length=1, max_length=96)
    platform: str = Field(min_length=1, max_length=64)
    machine: str = Field(min_length=1, max_length=64)
    hostname: str = Field(min_length=1, max_length=253)
    cpu_count: int | None = Field(default=None, ge=1, le=4096)
    ram_gb: float | None = Field(default=None, ge=0, le=1_048_576)
    gpu: str | None = Field(default=None, max_length=128)
    # Optional capability tags — e.g. ["code", "large-context"] or ["gpu", "fast"]
    # Used by the dispatcher to match tasks to capable nodes.
    # One slot is reserved for the server-added ``model:<name>`` tag.
    capabilities: list[str] = Field(default_factory=list, max_length=31)
    capability_descriptor: NodeCapabilityDescriptorV1 | None = None

    @field_validator("node_id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        try:
            return normalize_node_id(value)
        except InvalidNodeId as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("enrollment_credential")
    @classmethod
    def validate_enrollment_secret(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_enrollment_credential(value)
        except InvalidEnrollmentCredential as exc:
            raise ValueError(exc.reason) from exc

    @field_validator("model", "platform", "machine", "hostname", "gpu")
    @classmethod
    def normalize_bounded_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("registration text fields cannot be blank")
        if any(ord(character) < 32 for character in normalized):
            raise ValueError("registration text fields cannot contain control characters")
        return normalized

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw).strip()
            if not value:
                raise ValueError("capabilities cannot contain blank values")
            if len(value) > 64:
                raise ValueError("each capability must be 64 characters or fewer")
            if any(ord(character) < 32 for character in value):
                raise ValueError("capabilities cannot contain control characters")
            if value not in seen:
                normalized.append(value)
                seen.add(value)
        return normalized


class TaskResult(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    output: str | None = Field(default=None, max_length=10_485_760)
    error: str | None = Field(default=None, max_length=2048)
    elapsed_seconds: float = Field(default=0, ge=0, le=7200)
    # Issued with the task. Missing/mismatched bindings are rejected and may be
    # retained only in the bounded quarantine, never as operational output.
    attempt_id: str | None = Field(default=None, max_length=128)
    nonce: str | None = Field(default=None, max_length=128)
    contract_version: str | None = Field(default=None, max_length=16)
    execution_id: str | None = Field(default=None, max_length=64)
    execution_unit_id: str | None = Field(default=None, max_length=128)
    execution_unit_kind: str | None = Field(default=None, max_length=64)

    @field_validator("node_id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        try:
            return normalize_node_id(value)
        except InvalidNodeId as exc:
            raise ValueError(str(exc)) from exc


class TokenBatch(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    tokens: str = Field(min_length=1, max_length=65_536)
    contract_version: str | None = Field(default=None, max_length=16)
    attempt_id: str | None = Field(default=None, max_length=128)
    nonce: str | None = Field(default=None, max_length=128)
    execution_id: str | None = Field(default=None, max_length=64)
    execution_unit_id: str | None = Field(default=None, max_length=128)
    execution_unit_kind: str | None = Field(default=None, max_length=64)

    @field_validator("node_id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        try:
            return normalize_node_id(value)
        except InvalidNodeId as exc:
            raise ValueError(str(exc)) from exc


class NewProjectRequest(BaseModel):
    name: str
    initial_task: str


# ── Output directory size cap ────────────────────────────────────────
def _prune_output_dir() -> list[str]:
    """Delete oldest runs until output/ is under the configured size cap.

    Returns the list of pruned run-directory names. No-op when the cap is 0
    or the directory doesn't exist. Run dirs are timestamp-named, so sorting
    by name is sorting by age.
    """
    import shutil

    max_mb = get_config().get("output_max_mb", 0)
    if not max_mb or not OUTPUT_DIR.exists():
        return []
    try:
        from execution.artifacts import get_artifact_store

        active_roots = get_artifact_store().active_root_paths()
    except Exception:
        # If the active-root registry is unavailable, deletion cannot prove a
        # target is terminal. Retention fails closed.
        return []
    cap_bytes = max_mb * 1024 * 1024

    run_dirs = sorted(d for d in OUTPUT_DIR.iterdir() if d.is_dir())
    sizes = {}
    total = 0
    for d in run_dirs:
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        sizes[d] = size
        total += size

    pruned = []
    for d in run_dirs:  # oldest first
        if total <= cap_bytes:
            break
        try:
            if d.resolve() in active_roots:
                continue
        except OSError:
            continue
        try:
            shutil.rmtree(d)
            total -= sizes[d]
            pruned.append(d.name)
        except OSError:
            pass
    if pruned:
        try:
            _emit("output_pruned", {"runs_deleted": pruned, "cap_mb": max_mb})
        except Exception:
            pass  # pruning must never fail on event emission (e.g. no event loop)
    return pruned


# ── Background cleanup loop ──────────────────────────────────────────
async def _cleanup_stale_nodes():
    """Remove stale nodes, reclaim their in-flight tasks, and prune old records.

    Runs every 30 seconds.
    """
    while True:
        await asyncio.sleep(30)
        try:
            _cleanup_pass()
        except Exception as exc:
            # A single malformed in-memory record or transient storage error
            # must not permanently terminate every later cleanup sweep.
            logger.error(
                "node janitor sweep failed error_type=%s",
                type(exc).__name__,
            )


def _cleanup_pass():
    """One sweep of the janitor. Split out from the loop so it can be tested.

    Never raises: this runs unattended behind a background task, and a failure
    here must not take the orchestrator down mid-demo.
    """
    now = time.time()

    # 1. Expired worker leases. Close the old attempt before either dropping or
    # re-queuing its process-local task, so late output cannot win a race.
    with _task_queue_lock:
        expired = [
            tid
            for tid, task in task_inflight.items()
            if task.get("lease_expires_at") and float(task["lease_expires_at"]) < now
        ]
    for tid in expired:
        with _task_queue_lock:
            task = task_inflight.get(tid)
            if task is None:
                continue
            attempt_id = task.get("attempt_id")
            assigned_node = task.get("assigned_to")
            assigned_enrollment = task.get("assigned_enrollment_id")
            try:
                if attempt_id:
                    attempt_record = attempt_store.get(attempt_id)
                    expiry_cause = (
                        "execution_deadline"
                        if attempt_record is not None
                        and attempt_record.lease_deadline_kind == "execution_deadline"
                        else "lease_expired"
                    )
                    changed = attempt_store.transition_active(
                        attempt_id=attempt_id,
                        state="expired",
                        reason="lease expired",
                        terminal_cause=expiry_cause,
                        now=now,
                    )
                    if changed:
                        _record_terminal_capability_evidence(attempt_id)
            except Exception as exc:
                _safe_diagnostic_emit(
                    "attempt_expiry_failed",
                    {
                        "task_id": tid,
                        "node_id": assigned_node,
                        "enrollment_id": assigned_enrollment,
                        "phase": "lease_expiry",
                        "error_type": type(exc).__name__,
                    },
                )
                continue
            task = task_inflight.pop(tid)
            deadline = task.get("execution_deadline_at")
            if not deadline or float(deadline) > now:
                _clear_worker_assignment(task)
                task_queue.append(task)
                _emit("task_reclaimed", {
                    "task_id": tid,
                    "node_id": assigned_node,
                    "enrollment_id": assigned_enrollment,
                    "reason": "lease expired",
                })
            else:
                _emit("worker_task_expired", {
                    "task_id": tid,
                    "execution_id": task.get("execution_id"),
                    "node_id": assigned_node,
                    "enrollment_id": assigned_enrollment,
                    "reason": "execution deadline elapsed",
                })

    # 2. Durably revoked, rotated, or otherwise invalid enrollment sessions.
    # External local administration is therefore observed without a process
    # restart; the janitor interval bounds idle-session enforcement to 30s.
    for nid, node in list(nodes.items()):
        enrollment_id = node.get("enrollment_id")
        if not enrollment_id:
            continue
        try:
            enrollment_store.validate_session(
                str(enrollment_id),
                nid,
                int(node.get("credential_version") or 0),
            )
        except NodeEnrollmentError as exc:
            reclaim_enrollment_work(str(enrollment_id), exc.reason)
        except Exception as exc:
            # Storage unavailability is not evidence that an enrollment is
            # valid or revoked. Leave the session untouched, diagnose without
            # secrets, and retry it on the next bounded sweep.
            _safe_diagnostic_emit(
                "enrollment_revalidation_failed",
                {
                    "node_id": nid,
                    "enrollment_id": str(enrollment_id),
                    "phase": "janitor_revalidation",
                    "error_type": type(exc).__name__,
                },
            )

    # 3. Dead nodes
    cutoff = now - _NODE_TIMEOUT
    stale = [nid for nid, n in nodes.items() if n.get("last_seen", 0) < cutoff]
    for nid in stale:
        stale_node = nodes.get(nid)
        # Invalidate first so the old bearer cannot race the reclaim and submit
        # through a node record that is already being removed.
        node_sessions.invalidate_node(
            nid,
            session_id=stale_node.get("session_id") if stale_node else None,
        )
        nodes.pop(nid, None)
        # Reclaim any in-flight tasks assigned to this dead node
        with _task_queue_lock:
            reclaimed = [
                tid for tid, t in task_inflight.items()
                if t.get("assigned_to") == nid
            ]
        for tid in reclaimed:
            with _task_queue_lock:
                task = task_inflight.get(tid)
                if task is None:
                    continue
                attempt_id = task.get("attempt_id")
                enrollment_id = task.get("assigned_enrollment_id")
                try:
                    if attempt_id:
                        changed = attempt_store.transition_active(
                            attempt_id=attempt_id,
                            state="reclaimed",
                            reason=f"assigned node {nid} became stale",
                            terminal_cause="node_stale",
                            now=now,
                        )
                        if changed:
                            _record_terminal_capability_evidence(attempt_id)
                except Exception as exc:
                    # Fail closed: without a durable terminal transition, the
                    # old worker could still settle. Leave the task in-flight
                    # for a later cleanup pass instead of reissuing it.
                    _safe_diagnostic_emit(
                        "attempt_reclaim_failed",
                        {
                            "task_id": tid,
                            "node_id": nid,
                            "enrollment_id": enrollment_id,
                            "phase": "stale_node_reclaim",
                            "error_type": type(exc).__name__,
                        },
                    )
                    continue
                task = task_inflight.pop(tid)
                _clear_worker_assignment(task)
                task_queue.append(task)
            _emit(
                "task_reclaimed",
                {
                    "task_id": tid,
                    "node_id": nid,
                    "enrollment_id": enrollment_id,
                },
            )

    # 4. Old task results (only needed long enough for the caller to collect them)
    result_cutoff = now - _RESULT_TTL
    stale_results = [
        tid for tid, r in task_results.items()
        if r.get("completed_at", now) < result_cutoff
    ]
    for tid in stale_results:
        task_results.pop(tid, None)

    # 5. Old async jobs (finished jobs older than 7 days)
    job_cutoff = now - _JOB_TTL
    stale_jobs = []
    for jid, job in jobs.items():
        finished = job.get("finished_at")
        if finished and job["status"] in ("complete", "failed", "interrupted", "cancelled"):
            try:
                finished_ts = datetime.fromisoformat(finished).timestamp()
                if finished_ts < job_cutoff:
                    stale_jobs.append(jid)
            except Exception:
                pass
    for jid in stale_jobs:
        jobs.pop(jid, None)

    # 5. Prune SQLite event log — keep only the last 2000 rows
    try:
        with _db_lock, connection(_DB_PATH) as con:
            con.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT 2000)"
            )
            con.commit()
    except Exception:
        pass

    # 6. Enforce retention across legacy output/ and canonical artifact roots.
    try:
        _prune_output_dir()
    except Exception:
        pass
    try:
        from execution.artifacts import get_artifact_store

        active_execution_ids = {
            str(job["execution_id"])
            for job in jobs.values()
            if job.get("execution_id")
            and job.get("status") in {"queued", "running"}
        }
        with _db_lock, connection(_DB_PATH) as con:
            columns = {
                row[1] for row in con.execute("PRAGMA table_info(executions)").fetchall()
            }
            if "execution_id" in columns and "lifecycle_status" in columns:
                rows = con.execute(
                    "SELECT execution_id FROM executions "
                    "WHERE lifecycle_status IN ('queued', 'running')"
                ).fetchall()
                active_execution_ids.update(str(row[0]) for row in rows)
        get_artifact_store().prune(active_execution_ids=active_execution_ids)
    except Exception:
        pass
