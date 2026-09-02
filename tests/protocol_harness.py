"""The driving surface for the adversarial protocol campaign.

Everything here talks to the *real* coordinator: the FastAPI worker routes, the
canonical execution router, the durable stores, and the janitor. Nothing in this
module decides what should happen — that is `tests/protocol_model.py`'s job.

Three narrow seams exist so the adversarial scenarios in ROADMAP §5 are
reachable at all. Each is the smallest thing that makes one scenario testable:

* `CoordinatorClock` moves `server_state.coordinator_now`, the one function that
  decides lease issue and lease expiry. Nothing else in the process is affected.
* `PersistenceFaultInjector` swaps `sqlite_store.RetryConnection` for a subclass
  that fails a chosen operation index. It is installed by tests only; there is
  no production fault-injection endpoint and no general framework.
* `CoordinatorHarness.restart()` calls the same `server_state._init_db()` epoch
  boundary the real lifespan calls, then rebuilds every store on new
  connections.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import access_control
import config as config_module
import ledger as ledger_module
import routes_executions
import server_state as state
import sqlite_store
from capability_evidence import CapabilityEvidenceStore
from execution.artifacts import ArtifactStore
from execution.attempts import AcceptedResultBroker, AttemptStore
from execution.persistence import ExecutionStore
from execution.registry import StrategyOutcome, StrategyRegistry
from execution.service import ExecutionService
from node_capabilities import NodeCapabilitySnapshotStore
from node_enrollments import NodeEnrollmentStore
from server import app as worker_app


# ── Opaque synthetic identifiers ─────────────────────────────────────
#
# Counterexamples must be safe to paste into a CI log, so every value the
# generator can choose is a fixed synthetic constant. Nothing here is a real
# credential, and nothing here carries content.

ADMISSION_SECRET = "campaign-bootstrap-admission-secret-0000"

NODE_LABELS = ("n0", "n1", "n2", "n3")
CREDENTIALS = tuple(f"campaign-credential-{index:02d}-{'0' * 24}" for index in range(8))
IDEMPOTENCY_KEYS = ("k0", "k1", "k2")
REQUESTER_HOSTS = ("10.0.0.1", "10.0.0.2")
TASK_TEXTS = ("synthetic-task-alpha", "synthetic-task-beta")
WORKER_OUTPUTS = ("synthetic-output-alpha", "synthetic-output-beta")

# Everything the campaign asserts must never reach storage, logs, events, the
# ledger projection, or an error body.
SYNTHETIC_SECRETS = (ADMISSION_SECRET,) + CREDENTIALS + IDEMPOTENCY_KEYS


class CampaignStrategy:
    """A deterministic strategy. It never calls a model and never sleeps."""

    identifier = "dag"
    version = "adversarial-campaign"

    async def execute(self, request, options, context) -> StrategyOutcome:
        return StrategyOutcome(
            status="completed",
            validation_outcome="passed",
            assurance_level="structural",
            output_preview="synthetic deliverable",
        )


@dataclass
class CoordinatorClock:
    """A movable reading of the one clock that decides lease authority."""

    offset: float = 0.0

    def now(self) -> float:
        return time.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds

    def rewind(self, seconds: float) -> None:
        """Simulate a coordinator clock that jumps backward (NTP correction)."""
        self.offset -= seconds


class PersistenceFaultInjector:
    """Fail one SQLite operation at a chosen index, then get out of the way.

    ``mode`` selects where the failure lands:

    ``io``     a hard ``disk I/O error`` on the numbered statement — the
               disk-full case. ``retry_busy`` does not retry these.
    ``commit`` the same failure, but only when a transaction commits, which is
               the mid-transaction boundary that matters for
               durable-before-public.
    ``busy``   ``database is locked`` for a burst long enough to exhaust
               ``sqlite_store.SQLITE_BUSY_RETRIES``, exercising the retry path
               rather than bypassing it.
    """

    def __init__(self) -> None:
        self.armed = False
        self.mode = "io"
        self.target_index = 0
        self.counter = 0
        self.fired = False
        self._burst_remaining = 0

    def arm(self, *, target_index: int, mode: str = "io") -> None:
        self.armed = True
        self.mode = mode
        self.target_index = max(0, int(target_index))
        self.counter = 0
        self.fired = False
        self._burst_remaining = 0

    def disarm(self) -> None:
        self.armed = False
        self._burst_remaining = 0

    def check(self, operation: str) -> None:
        if not self.armed:
            return
        if self._burst_remaining > 0:
            self._burst_remaining -= 1
            raise sqlite3.OperationalError("database is locked")
        if self.mode == "commit" and operation != "commit":
            return
        if self.counter != self.target_index:
            self.counter += 1
            return
        self.counter += 1
        self.fired = True
        self.armed = False
        if self.mode == "busy":
            # One more than the bounded retry budget, so the caller sees the
            # exhausted-retry path rather than a lucky success.
            self._burst_remaining = sqlite_store.SQLITE_BUSY_RETRIES
            raise sqlite3.OperationalError("database is locked")
        raise sqlite3.OperationalError("disk I/O error")


_ACTIVE_INJECTOR: PersistenceFaultInjector | None = None


class _FaultingConnection(sqlite_store.RetryConnection):
    """`sqlite_store.connect` names its factory as a module global, so swapping
    that global is all it takes to reach every store the coordinator opens.

    Each override reimplements its parent by calling `sqlite3.Connection`
    directly under the same `retry_busy` policy, rather than via `super()`.
    `RetryConnection`'s own methods resolve `super(RetryConnection, self)` through
    the module global this class replaces, so delegating upward would resolve
    straight back into this subclass.
    """

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        if _ACTIVE_INJECTOR is not None:
            _ACTIVE_INJECTOR.check("execute")
        return sqlite_store.retry_busy(
            lambda: sqlite3.Connection.execute(self, sql, parameters)
        )

    def executemany(self, sql, seq_of_parameters, /):  # type: ignore[override]
        if _ACTIVE_INJECTOR is not None:
            _ACTIVE_INJECTOR.check("execute")
        return sqlite_store.retry_busy(
            lambda: sqlite3.Connection.executemany(self, sql, seq_of_parameters)
        )

    def executescript(self, sql_script, /):  # type: ignore[override]
        if _ACTIVE_INJECTOR is not None:
            _ACTIVE_INJECTOR.check("execute")
        return sqlite_store.retry_busy(
            lambda: sqlite3.Connection.executescript(self, sql_script)
        )

    def commit(self) -> None:  # type: ignore[override]
        if _ACTIVE_INJECTOR is not None:
            _ACTIVE_INJECTOR.check("commit")
        return sqlite_store.retry_busy(lambda: sqlite3.Connection.commit(self))


class _CapturingLogHandler(logging.Handler):
    """Keep every formatted log line so the secret scan can read them all."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # pragma: no cover - a formatting failure is not a leak
            self.lines.append(str(record.msg))


@dataclass
class Handout:
    """One server-issued attempt, exactly as a worker receives it."""

    task_id: str
    attempt_id: str
    nonce: str
    execution_id: str | None
    unit_id: str | None
    unit_kind: str | None
    contract_version: str | None
    label: str
    max_output_bytes: int
    raw: dict[str, Any] = field(default_factory=dict)


def payload_digest(body: dict[str, Any]) -> str:
    """A stable digest of the fields settlement binds, for replay comparison."""
    material = json.dumps(
        {
            key: body.get(key)
            for key in (
                "node_id",
                "output",
                "error",
                "elapsed_seconds",
                "contract_version",
                "attempt_id",
                "execution_id",
                "execution_unit_id",
                "execution_unit_kind",
            )
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class CoordinatorHarness:
    """One isolated coordinator, driven through its real boundaries."""

    def __init__(self, root: Path) -> None:
        global _ACTIVE_INJECTOR

        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "events.db"
        self.clock = CoordinatorClock()
        self.faults = PersistenceFaultInjector()
        self.session_tokens: dict[str, str] = {}
        self.session_ids: dict[str, str] = {}
        self.enrollment_ids: dict[str, str] = {}
        self.credential_versions: dict[str, int] = {}
        # Server-minted values that must never surface anywhere durable.
        self.minted_secrets: set[str] = set()

        self.settings = dict(config_module.DEFAULTS)
        self.settings.update(
            {
                "node_secret": ADMISSION_SECRET,
                "node_enrollment_mode": "required",
                "viewer_key": "",
                "pitch_key": "",
                "pitch_rate_max": 100_000,
                "pitch_rate_window": 60,
                "capability_evidence_mode": "off",
                "verify_rate": 0.0,
                "deployment_mode": "local",
            }
        )

        self._saved: dict[tuple[Any, str], Any] = {}
        self._log_handler = _CapturingLogHandler()
        logging.getLogger().addHandler(self._log_handler)
        logging.getLogger().setLevel(logging.DEBUG)

        self._patch(sqlite_store, "RetryConnection", _FaultingConnection)
        _ACTIVE_INJECTOR = self.faults

        self._patch(ledger_module, "LEDGER_DB_FILE", self.database)
        self._patch(ledger_module, "LEDGER_FILE", self.root / "ledger.json")
        self._patch(state, "_DB_PATH", self.database)
        self._patch(
            state,
            "_CAPABILITY_SHADOW_OPERATIONAL_DB_PATH",
            self.root / "capability-shadow-health.db",
        )
        self._patch(state, "_LONG_POLL_TIMEOUT", 0.01)
        self._patch(state, "coordinator_now", self.clock.now)
        self._patch(state, "get_config", lambda: self.settings)
        self._patch(access_control, "get_config", lambda: self.settings)
        self._patch(routes_executions, "get_config", lambda: self.settings)

        self._install_stores()

        # Worker protocol: the real app, so middleware and lifespan hooks run.
        self.client = TestClient(worker_app)
        self.client.__enter__()

        # Canonical submission: the real router, mounted twice so two distinct
        # requester scopes exist without inventing a second identity mechanism.
        self._execution_app = FastAPI()
        self._execution_app.include_router(routes_executions.router)
        self.requesters = {
            host: TestClient(self._execution_app, client=(host, 50000))
            for host in REQUESTER_HOSTS
        }

    # ── wiring ───────────────────────────────────────────────────────

    def _patch(self, target: Any, name: str, value: Any) -> None:
        key = (target, name)
        if key not in self._saved:
            self._saved[key] = getattr(target, name)
        setattr(target, name, value)

    def _install_stores(self) -> None:
        """(Re)build every durable store on fresh connections to this database."""
        attempt_store = AttemptStore(self.database)
        self._patch(state, "attempt_store", attempt_store)
        self._patch(state, "enrollment_store", NodeEnrollmentStore(self.database))
        self._patch(
            state,
            "capability_snapshot_store",
            NodeCapabilitySnapshotStore(self.database),
        )
        self._patch(
            state,
            "capability_evidence_store",
            CapabilityEvidenceStore(self.database),
        )
        self._patch(
            state,
            "accepted_result_broker",
            AcceptedResultBroker(attempt_store),
        )

        registry = StrategyRegistry()
        registry.register(CampaignStrategy())
        self.service = ExecutionService(
            store=ExecutionStore(self.database),
            registry=registry,
            artifacts=ArtifactStore(self.database, allowed_roots=[self.root]),
        )
        self.service.store.migrate()
        self.service.artifacts.migrate()
        self._patch(routes_executions, "get_execution_service", lambda: self.service)

        state._init_db()
        state._pitch_timestamps.clear()

    def close(self) -> None:
        global _ACTIVE_INJECTOR

        self.faults.disarm()
        _ACTIVE_INJECTOR = None
        for requester in self.requesters.values():
            requester.close()
        try:
            self.client.__exit__(None, None, None)
        except Exception:  # pragma: no cover - teardown must not mask a finding
            pass
        logging.getLogger().removeHandler(self._log_handler)
        for (target, name), value in self._saved.items():
            setattr(target, name, value)
        self._saved.clear()
        self._clear_process_local()

    def _clear_process_local(self) -> None:
        for mapping in (
            state.nodes,
            state.task_inflight,
            state.task_results,
            state.node_failure_count,
            state.node_blacklist,
            state.waiting_nodes,
            state.settled_attempts,
            state.jobs,
        ):
            mapping.clear()
        state.task_queue.clear()
        state.pipeline_events.clear()
        state._pitch_timestamps.clear()

    # ── restart ──────────────────────────────────────────────────────

    def restart(self) -> None:
        """A new coordinator epoch over the same durable state.

        This is exactly what the real lifespan does: process-local queues,
        sessions, and in-flight assignments are gone; `_init_db()` interrupts
        every live attempt so a late result fails closed; canonical
        reconciliation moves non-terminal executions to `interrupted`.
        """
        self._clear_process_local()
        self._install_stores()
        self.service.reconcile_after_restart(f"campaign-restart-{time.time_ns():x}")
        self.session_tokens.clear()
        self.session_ids.clear()

    # ── worker protocol ──────────────────────────────────────────────

    @staticmethod
    def _descriptor(model: str = "qwen3.5:4b") -> dict[str, Any]:
        return {
            "executor": {"kind": "ollama", "worker_protocol_version": "1"},
            "models": [{"provider": "ollama", "name": model}],
            "hardware": {
                "architecture": "x86_64",
                "logical_cpu_count": 4,
                "total_memory_bytes": 8 * 1024**3,
            },
            "features": ["code"],
            "limits": {
                "max_concurrent_execution_units": 1,
                "max_output_bytes": 1_048_576,
            },
            "isolation": {"kind": "none"},
        }

    def _registration(self, label: str, credential: str, action: str) -> dict[str, Any]:
        return {
            "node_id": label,
            "enrollment_action": action,
            "enrollment_credential": credential,
            "model": "qwen3.5:4b",
            "platform": "Linux",
            "machine": "x86_64",
            "hostname": label,
            "cpu_count": 4,
            "ram_gb": 8.0,
            "capabilities": ["code"],
            "capability_descriptor": self._descriptor(),
        }

    def register(self, label: str, credential: str, action: str):
        headers = {"X-Node-Secret": ADMISSION_SECRET} if action == "bootstrap" else {}
        response = self.client.post(
            "/nodes/register",
            json=self._registration(label, credential, action),
            headers=headers,
        )
        if response.status_code == 200:
            body = response.json()
            self.session_tokens[label] = body["session_token"]
            self.session_ids[label] = body["session_id"]
            self.minted_secrets.add(body["session_token"])
            if body.get("enrollment_id"):
                self.enrollment_ids[label] = body["enrollment_id"]
            if body.get("credential_version"):
                self.credential_versions[label] = int(body["credential_version"])
        return response

    def headers(self, label: str, *, token: str | None = None) -> dict[str, str]:
        resolved = token if token is not None else self.session_tokens.get(label, "")
        return {"X-Node-Session": resolved} if resolved else {}

    def drain(self, label: str):
        return self.client.post(f"/nodes/{label}/drain", headers=self.headers(label))

    def enqueue_unit(
        self,
        task_id: str,
        *,
        execution_id: str,
        unit_id: str,
        max_output_bytes: int = 4096,
        lease_seconds: int = 900,
    ) -> None:
        state.task_queue.append(
            {
                "task_id": task_id,
                "title": "candidate",
                "prompt": "synthetic prompt",
                "system": "synthetic system",
                "contract_version": "1",
                "execution_id": execution_id,
                "execution_unit_id": unit_id,
                "execution_unit_kind": "candidate",
                "max_output_bytes": max_output_bytes,
                "lease_seconds": lease_seconds,
            }
        )

    def poll(self, label: str) -> Handout | None:
        response = self.client.get(
            "/tasks/next", params={"node_id": label}, headers=self.headers(label)
        )
        if response.status_code != 200:
            return None
        task = response.json()
        if not task.get("attempt_id"):
            return None
        self.minted_secrets.add(task["nonce"])
        return Handout(
            task_id=task["task_id"],
            attempt_id=task["attempt_id"],
            nonce=task["nonce"],
            execution_id=task.get("execution_id"),
            unit_id=task.get("execution_unit_id"),
            unit_kind=task.get("execution_unit_kind"),
            contract_version=task.get("contract_version"),
            label=label,
            max_output_bytes=int(task.get("max_output_bytes") or 1_048_576),
            raw=task,
        )

    @staticmethod
    def result_body(handout: Handout, *, output: str = "synthetic-output-alpha", **overrides):
        body: dict[str, Any] = {
            "node_id": handout.label,
            "output": output,
            "error": None,
            "elapsed_seconds": 1.0,
            "contract_version": handout.contract_version,
            "attempt_id": handout.attempt_id,
            "nonce": handout.nonce,
            "execution_id": handout.execution_id,
            "execution_unit_id": handout.unit_id,
            "execution_unit_kind": handout.unit_kind,
        }
        body.update(overrides)
        return body

    def submit(self, task_id: str, body: dict[str, Any], *, label: str, token: str | None = None):
        return self.client.post(
            f"/tasks/{task_id}/result",
            json=body,
            headers=self.headers(label, token=token),
        )

    def stream(self, task_id: str, handout: Handout, tokens: str, *, label: str | None = None):
        return self.client.post(
            f"/tasks/{task_id}/stream",
            json={
                "node_id": label or handout.label,
                "tokens": tokens,
                "contract_version": handout.contract_version,
                "attempt_id": handout.attempt_id,
                "nonce": handout.nonce,
                "execution_id": handout.execution_id,
                "execution_unit_id": handout.unit_id,
                "execution_unit_kind": handout.unit_kind,
            },
            headers=self.headers(label or handout.label),
        )

    # ── operator actions ─────────────────────────────────────────────

    def revoke(self, enrollment_id: str, reason: str = "campaign revocation") -> None:
        state.enrollment_store.revoke(enrollment_id, reason, now=self.clock.now())
        state.reclaim_enrollment_work(enrollment_id, "enrollment revoked")

    def rotate(self, enrollment_id: str, credential: str, expected_version: int):
        result = state.enrollment_store.rotate(
            enrollment_id,
            credential,
            expected_credential_version=expected_version,
            now=self.clock.now(),
        )
        state.reclaim_enrollment_work(enrollment_id, "enrollment credential rotated")
        return result

    def janitor(self) -> None:
        """One sweep of the real background janitor."""
        state._cleanup_pass()

    def supersede(self, attempt_id: str) -> bool:
        return bool(
            state.attempt_store.transition_active(
                attempt_id=attempt_id,
                state="superseded",
                reason="campaign supersession",
                terminal_cause="superseded",
                now=self.clock.now(),
            )
        )

    # ── canonical executions ─────────────────────────────────────────

    def submit_execution(
        self,
        *,
        host: str,
        task: str,
        idempotency_key: str | None,
        timeout_seconds: int = 1800,
    ):
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self.requesters[host].post(
            "/v1/executions",
            json={
                "task": task,
                "strategy": "dag",
                "timeout_seconds": timeout_seconds,
            },
            headers=headers,
        )

    def get_execution(self, execution_id: str, *, host: str = REQUESTER_HOSTS[0]):
        return self.requesters[host].get(f"/v1/executions/{execution_id}")

    def cancel_execution(self, execution_id: str, *, host: str = REQUESTER_HOSTS[0]):
        return self.requesters[host].post(f"/v1/executions/{execution_id}/cancel")

    # ── durable reads used by the invariants ─────────────────────────

    def rows(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        """Read committed state with a connection nothing else is holding."""
        with sqlite3.connect(self.database) as con:
            con.row_factory = sqlite3.Row
            try:
                return list(con.execute(sql, parameters).fetchall())
            except sqlite3.OperationalError:
                # A table can legitimately not exist yet before first migration.
                return []

    def durable_receipts(self) -> dict[str, sqlite3.Row]:
        return {
            row["attempt_id"]: row
            for row in self.rows("SELECT * FROM accepted_result_receipts")
        }

    def durable_credits(self) -> dict[str, sqlite3.Row]:
        return {
            row["attempt_id"]: row
            for row in self.rows(
                "SELECT * FROM contributions WHERE basis = 'compute_contribution'"
            )
            if row["attempt_id"]
        }

    def durable_attempts(self) -> dict[str, sqlite3.Row]:
        return {row["attempt_id"]: row for row in self.rows("SELECT * FROM attempts")}

    def durable_execution(self, execution_id: str) -> sqlite3.Row | None:
        found = self.rows(
            "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
        )
        return found[0] if found else None

    def capability_observations(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM node_capability_observations")

    def quarantine_rows(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM result_quarantine")

    # ── secret hygiene ───────────────────────────────────────────────

    def scan_for_secrets(self, extra: tuple[str, ...] = ()) -> list[str]:
        """Return every place a secret-class value became readable.

        Deliberately narrower than "no generated value anywhere": an accepted
        result's output *is* stored, in `accepted_result_receipts.output`, and a
        rejected one's bounded preview is stored in quarantine. Both are
        documented (`docs/PROTOCOL.md`, "Settlement, replay, rejection, and
        quarantine"). What may never be readable is identity material —
        credentials, session tokens, attempt nonces, idempotency keys — and no
        prompt or output may reach the event stream, the logs, or the ledger.
        """
        findings: list[str] = []
        needles = tuple(SYNTHETIC_SECRETS) + tuple(self.minted_secrets) + tuple(extra)

        blobs: list[tuple[str, str]] = []
        for database in (self.database, self.root / "capability-shadow-health.db"):
            if database.exists():
                blobs.append((f"sqlite:{database.name}", database.read_bytes().decode("latin-1")))
        projection = self.root / "ledger.json"
        if projection.exists():
            blobs.append(("ledger.json", projection.read_text(encoding="utf-8")))
        blobs.append(("events", json.dumps(list(state.pipeline_events), default=str)))
        blobs.append(("logs", "\n".join(self._log_handler.lines)))

        for needle in needles:
            if not needle:
                continue
            for where, blob in blobs:
                if needle in blob:
                    findings.append(f"{where} contains identity material")
        # Prompts and outputs are legitimate durable result content, but never
        # telemetry. Only the non-durable surfaces are checked for them.
        for where, blob in blobs:
            if where == "sqlite:events.db":
                continue
            for content in TASK_TEXTS + WORKER_OUTPUTS:
                if content in blob:
                    findings.append(f"{where} contains prompt or output content")
        return sorted(set(findings))

    def log_lines(self) -> list[str]:
        return list(self._log_handler.lines)
