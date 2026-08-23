#!/usr/bin/env python3
"""Bounded, no-Ollama operational harness for a trusted-alpha coordinator.

The live smoke starts a real Uvicorn process on a dynamically allocated
loopback port and points it at a tiny fake Ollama-compatible HTTP server.  The
rest of the release contract is exercised by focused pytest scenarios whose
fixtures use fake workers/executors and isolated temporary state.

Human/CI entry points::

    python scripts/trusted_alpha_harness.py
    python scripts/trusted_alpha_harness.py --profile nightly --iterations 5

Credential values are written only to a temporary private configuration file.
They are never passed on a command line or included in harness output.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence
from urllib.parse import quote

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
TERMINAL_EXECUTION_STATES = frozenset(
    {"completed", "failed", "cancelled", "interrupted", "unverified"}
)
REQUIRED_COVERAGE = frozenset(
    {
        "coordinator_startup",
        "viewer_pitch_node_authentication",
        "two_node_sessions",
        "distributed_direct",
        "distributed_dag",
        "distributed_ensemble",
        "attempt_assignment_and_settlement",
        "remote_consent",
        "token_streaming",
        "output_limit_rejection",
        "cancellation_and_late_result",
        "restart_reconciliation",
        "artifact_sealing_download_and_mutation",
        "share_creation_and_revocation",
        "database_concurrency",
        "clean_shutdown",
    }
)


@dataclass(frozen=True)
class Scenario:
    coverage: tuple[str, ...]
    description: str
    nodeids: tuple[str, ...]
    nightly_only: bool = False


SCENARIOS = (
    Scenario(
        coverage=("viewer_pitch_node_authentication",),
        description="static viewer, pitch, and node authorities deny missing credentials",
        nodeids=(
            "tests/test_access_control.py::test_sensitive_read_routes_require_viewer",
            "tests/test_security.py::test_pitch_endpoints_reject_missing_key",
            "tests/test_server_api.py::test_tasks_next_requires_secret",
        ),
    ),
    Scenario(
        coverage=("distributed_direct",),
        description="direct strategy normalizes to one complete fake-worker candidate",
        nodeids=(
            "tests/test_trusted_alpha_harness.py::test_distributed_direct_completes_with_fake_worker",
        ),
    ),
    Scenario(
        coverage=("distributed_dag",),
        description="DAG build unit uses the distributed dispatcher and a fake worker",
        nodeids=(
            "tests/test_execution_strategies.py::test_dag_distributed_uses_the_same_strategy_and_full_worker_dispatch",
        ),
    ),
    Scenario(
        coverage=("distributed_ensemble",),
        description="distributed ensemble fans out bounded complete fake-worker candidates",
        nodeids=(
            "tests/test_execution_strategies.py::test_distributed_ensemble_fans_out_complete_candidates_with_one_worker",
        ),
    ),
    Scenario(
        coverage=("attempt_assignment_and_settlement", "two_node_sessions"),
        description="server session and attempt bindings gate stream and settlement",
        nodeids=(
            "tests/test_node_sessions.py::test_result_and_stream_require_session_and_attempt_binding",
            "tests/test_attempt_authority.py::test_two_concurrent_submissions_settle_exactly_once",
        ),
    ),
    Scenario(
        coverage=("remote_consent",),
        description="remote-capable CLI requests require and propagate explicit consent",
        nodeids=(
            "tests/test_cli_execution_args.py::test_distributed_cli_requires_explicit_consent",
            "tests/test_cli_execution_args.py::test_every_existing_strategy_can_be_distributed_with_consent",
        ),
    ),
    Scenario(
        coverage=("token_streaming", "output_limit_rejection"),
        description="cumulative streams and multibyte results stop at server-issued byte budgets",
        nodeids=(
            "tests/test_output_stream_limits.py::test_multibyte_oversize_is_terminal_before_receipt_or_contribution",
            "tests/test_output_stream_limits.py::test_stream_budget_is_cumulative_and_emits_one_terminal_event",
            "tests/test_output_stream_limits.py::test_stream_after_settlement_is_rejected",
        ),
    ),
    Scenario(
        coverage=("cancellation_and_late_result",),
        description="remote cancellation is terminal and late worker settlement is rejected",
        nodeids=(
            "tests/test_execution_lifecycle.py::test_remote_cancellation_is_terminal_and_rejects_late_result",
            "tests/test_result_binding.py::test_late_result_after_cancellation_is_rejected",
        ),
    ),
    Scenario(
        coverage=("restart_reconciliation",),
        description="nonterminal canonical state is durably interrupted after restart",
        nodeids=(
            "tests/test_execution_persistence.py::test_restart_reconciliation_interrupts_nonterminal_execution_once",
            "tests/test_node_sessions.py::test_coordinator_restart_invalidates_process_local_session",
        ),
    ),
    Scenario(
        coverage=("artifact_sealing_download_and_mutation",),
        description="sealed baselines stay stable, downloads rehash, and mutations fail",
        nodeids=(
            "tests/test_artifacts.py::test_terminal_seal_is_stable_and_mutation_never_rewrites_baseline",
            "tests/test_artifacts.py::test_archive_uses_one_sealed_manifest_snapshot",
            "tests/test_shares.py::test_share_api_is_private_to_create_but_public_by_capability",
            "tests/test_trusted_alpha_harness.py::test_sealed_artifact_is_retrievable_after_store_restart",
        ),
    ),
    Scenario(
        coverage=("share_creation_and_revocation",),
        description="operators list and revoke capability shares without plaintext tokens",
        nodeids=(
            "tests/test_shares.py::test_share_admin_routes_list_revoke_one_and_revoke_all",
            "tests/test_shares.py::test_revoked_and_expired_share_urls_are_not_disclosed",
        ),
    ),
    Scenario(
        coverage=("database_concurrency",),
        description="simultaneous cross-store writes remain complete and atomic",
        nodeids=(
            "tests/test_sqlite_policy.py::test_concurrent_cross_store_writes_are_complete_and_atomic",
        ),
    ),
    Scenario(
        coverage=("database_concurrency",),
        description="nightly migration and queue contention across higher concurrency",
        nodeids=(
            "tests/test_sqlite_policy.py::test_concurrent_store_initialization_is_idempotent",
            "tests/test_execution_interfaces.py::test_queue_limit_is_atomic_for_a_generated_wave",
        ),
        nightly_only=True,
    ),
    Scenario(
        coverage=("artifact_sealing_download_and_mutation",),
        description="nightly artifact churn keeps active roots and retention safe",
        nodeids=(
            "tests/test_artifacts.py::test_file_count_and_aggregate_quotas_are_enforced",
            "tests/test_artifacts.py::test_active_execution_is_never_pruned",
            "tests/test_artifacts.py::test_retention_covers_output_and_execution_artifact_roots",
        ),
        nightly_only=True,
    ),
    Scenario(
        coverage=("two_node_sessions", "restart_reconciliation"),
        description="nightly node disconnect, reclaim, and re-registration cycles",
        nodeids=(
            "tests/test_node_sessions.py::test_stale_id_can_be_reclaimed_and_old_token_stops_working",
            "tests/test_node_rejoin.py::test_in_flight_work_is_reclaimed_when_a_node_disappears",
            "tests/test_node_rejoin.py::test_a_readmitted_node_can_receive_work",
            "tests/test_chaos.py::test_many_nodes_churning_does_not_lose_queued_work",
        ),
        nightly_only=True,
    ),
)


class HarnessFailure(RuntimeError):
    """An operational boundary did not satisfy the harness contract."""


class _FakeModelHandler(BaseHTTPRequestHandler):
    """Minimal deterministic Ollama-compatible surface; it runs no model."""

    server_version = "MyceliumHarnessFakeModel/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/api/tags":
            self._json({"models": [{"name": "mycelium-harness-fake:latest"}]})
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0") or 0)
        request_payload = {}
        if length:
            raw = self.rfile.read(min(length, 1_048_576))
            try:
                parsed = json.loads(raw)
                request_payload = parsed if isinstance(parsed, dict) else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                request_payload = {}
        if self.path == "/api/show":
            self._json({"capabilities": []})
            return
        if self.path == "/api/generate":
            output = "trusted alpha fake executor result"
            response_format = request_payload.get("format")
            if isinstance(response_format, dict) and response_format.get("type") == "array":
                output = json.dumps(
                    [
                        {
                            "id": 1,
                            "title": "Build first component",
                            "prompt": "Produce the first complete HTML component.",
                            "depends_on": [],
                        },
                        {
                            "id": 2,
                            "title": "Build second component",
                            "prompt": "Produce the second complete HTML component.",
                            "depends_on": [],
                        },
                    ],
                    separators=(",", ":"),
                )
            self._json(
                {
                    "model": "mycelium-harness-fake:latest",
                    "response": output,
                    "done": True,
                }
            )
            return
        self._json({"error": "not found"}, status=404)


def profile_scenarios(profile: str) -> tuple[Scenario, ...]:
    if profile not in {"bounded", "nightly"}:
        raise ValueError("profile must be bounded or nightly")
    return tuple(
        scenario
        for scenario in SCENARIOS
        if profile == "nightly" or not scenario.nightly_only
    )


def selected_nodeids(profile: str) -> tuple[str, ...]:
    return tuple(
        nodeid
        for scenario in profile_scenarios(profile)
        for nodeid in scenario.nodeids
    )


def declared_coverage(profile: str) -> frozenset[str]:
    coverage = {"coordinator_startup", "clean_shutdown"}
    for scenario in profile_scenarios(profile):
        coverage.update(scenario.coverage)
    return frozenset(coverage)


def _redact(text: str, sensitive_values: Sequence[str]) -> str:
    redacted = text
    for value in sensitive_values:
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def _start_fake_model() -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModelHandler)
    thread = threading.Thread(target=server.serve_forever, name="fake-model", daemon=True)
    thread.start()
    return server, thread


def _write_trusted_config(state_dir: Path, fake_model_port: int) -> tuple[Path, dict]:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import config

    path = state_dir / "config.json"
    config.ensure_trusted_alpha_config(
        path,
        model="mycelium-harness-fake:latest",
        ollama_url=f"http://127.0.0.1:{fake_model_port}",
    )
    stored = json.loads(path.read_text(encoding="utf-8"))
    return path, stored


def _request_graceful_shutdown(process: subprocess.Popen[str]) -> int | None:
    """Ask Uvicorn to stop and return the signal the harness delivered."""
    if process.poll() is not None:
        return None
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
        return int(signal.CTRL_BREAK_EVENT)
    else:
        process.terminate()
        return int(signal.SIGTERM)


def _shutdown_exit_is_expected(
    returncode: int | None,
    requested_signal: int | None,
    *,
    platform_name: str,
) -> bool:
    """Classify only harness-requested signal exits as clean shutdowns.

    Newer Uvicorn releases perform lifespan teardown, restore the process's
    original signal handler, and then re-raise the captured signal. POSIX
    therefore reports ``-SIGTERM`` even though graceful teardown completed.
    """
    if returncode in {0, None}:
        return True
    if requested_signal is None:
        return False
    if platform_name == "nt":
        return returncode in {3, -1073741510}
    return returncode == -requested_signal


def _wait_for_health(client: httpx.Client, process: subprocess.Popen[str]) -> dict:
    deadline = time.monotonic() + 30
    last_error = "coordinator did not answer"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise HarnessFailure("coordinator exited before becoming healthy")
        try:
            response = client.get("/health")
            if response.status_code == 200:
                payload = response.json()
                if payload.get("status") == "ok":
                    return payload
                last_error = f"health status was {payload.get('status')!r}"
        except (httpx.HTTPError, ValueError) as exc:
            last_error = type(exc).__name__
        time.sleep(0.1)
    raise HarnessFailure(f"coordinator health deadline expired ({last_error})")


def _assert_status(response: httpx.Response, expected: int, label: str) -> None:
    if response.status_code != expected:
        raise HarnessFailure(
            f"{label}: expected HTTP {expected}, received {response.status_code}"
        )


def _register_node(
    client: httpx.Client,
    node_secret: str,
    node_id: str,
) -> dict:
    response = client.post(
        "/nodes/register",
        headers={"X-Node-Secret": node_secret},
        json={
            "node_id": node_id,
            "model": "mycelium-harness-fake:latest",
            "platform": sys.platform,
            "machine": "harness",
            "hostname": node_id,
            "cpu_count": 2,
            "ram_gb": 8,
            "capabilities": ["code"],
        },
    )
    _assert_status(response, 200, f"register {node_id}")
    payload = response.json()
    if not all(payload.get(field) for field in ("node_id", "session_id", "session_token")):
        raise HarnessFailure(f"register {node_id}: incomplete session grant")
    return payload


def _node_headers(settings: dict, registration: dict) -> dict[str, str]:
    return {
        "X-Node-Secret": str(settings["node_secret"]),
        "X-Node-Session": str(registration["session_token"]),
    }


def _worker_binding(task: dict, node_id: str) -> dict:
    return {
        "node_id": node_id,
        "contract_version": task["contract_version"],
        "attempt_id": task["attempt_id"],
        "nonce": task["nonce"],
        "execution_id": task["execution_id"],
        "execution_unit_id": task["execution_unit_id"],
        "execution_unit_kind": task["execution_unit_kind"],
    }


def _claim_worker_task(base_url: str, settings: dict, registration: dict) -> dict:
    with httpx.Client(base_url=base_url, timeout=35, trust_env=False) as worker:
        response = worker.get(
            "/tasks/next",
            params={"node_id": registration["node_id"]},
            headers=_node_headers(settings, registration),
        )
    _assert_status(response, 200, f"task claim {registration['node_id']}")
    task = response.json()
    required = {
        "task_id",
        "contract_version",
        "attempt_id",
        "nonce",
        "execution_id",
        "execution_unit_id",
        "execution_unit_kind",
        "max_output_bytes",
    }
    if not required.issubset(task):
        raise HarnessFailure(
            f"task claim {registration['node_id']}: authoritative binding is incomplete"
        )
    return task


def _settle_worker_task(
    base_url: str,
    settings: dict,
    registration: dict,
) -> tuple[str, str]:
    task = _claim_worker_task(base_url, settings, registration)
    binding = _worker_binding(task, registration["node_id"])
    headers = _node_headers(settings, registration)
    with httpx.Client(base_url=base_url, timeout=15, trust_env=False) as worker:
        streamed = worker.post(
            f"/tasks/{task['task_id']}/tokens",
            headers=headers,
            json={**binding, "tokens": f"partial from {registration['node_id']}"},
        )
        _assert_status(streamed, 200, f"token stream {registration['node_id']}")
        html = (
            "<!DOCTYPE html><html><head><title>Harness</title></head>"
            f"<body><main>{registration['node_id']}</main>"
            "<script>document.body.dataset.ready='true';</script></body></html>"
        )
        settled = worker.post(
            f"/tasks/{task['task_id']}/result",
            headers=headers,
            json={
                **binding,
                "output": html,
                "error": None,
                "elapsed_seconds": 0.01,
            },
        )
    _assert_status(settled, 200, f"attempt settlement {registration['node_id']}")
    return str(registration["node_id"]), str(task["task_id"])


def _exercise_live_coordinator(state_dir: Path) -> None:
    fake_model, fake_thread = _start_fake_model()
    fake_model_port = int(fake_model.server_address[1])
    config_path, settings = _write_trusted_config(state_dir, fake_model_port)
    sensitive = [
        str(settings.get(name, ""))
        for name in ("viewer_key", "pitch_key", "node_secret")
    ]
    port = _allocate_loopback_port()
    environment = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "MYCELIUM_CONFIG_FILE": str(config_path),
        "MYCELIUM_STATE_DIR": str(state_dir),
        "MYCELIUM_DEPLOYMENT_MODE": "trusted_alpha",
        "PYTHONUNBUFFERED": "1",
    }
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        cwd=state_dir,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creation_flags,
    )
    failure: BaseException | None = None
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            # SQLite's deliberate busy timeout is ten seconds. Operational
            # reads should normally return immediately, but the harness must
            # not declare a coordinator dead while a bounded terminal artifact
            # transaction is still finishing.
            timeout=12,
            trust_env=False,
        ) as client:
            health = _wait_for_health(client, process)
            if health.get("private_routes_protected") is not True:
                raise HarnessFailure("health did not report private-route protection")

            _assert_status(client.get("/v1/operator/health"), 401, "viewer denial")
            operator = client.get(
                "/v1/operator/health",
                headers={"X-Viewer-Key": settings["viewer_key"]},
            )
            _assert_status(operator, 200, "viewer authorization")
            if not operator.json().get("single_coordinator_lock"):
                raise HarnessFailure("operator health did not report the coordinator lock")

            registration_body = {
                "node_id": "unauthorized-harness-node",
                "model": "mycelium-harness-fake:latest",
                "platform": sys.platform,
                "machine": "harness",
                "hostname": "unauthorized-harness-node",
                "cpu_count": 2,
                "ram_gb": 8,
                "capabilities": ["code"],
            }
            _assert_status(
                client.post("/nodes/register", json=registration_body),
                401,
                "node admission denial",
            )
            first = _register_node(client, settings["node_secret"], "harness-node-a")
            second = _register_node(client, settings["node_secret"], "harness-node-b")
            if first["session_id"] == second["session_id"]:
                raise HarnessFailure("two node registrations received the same session id")
            if first["session_token"] == second["session_token"]:
                raise HarnessFailure("two node registrations received the same session token")

            for registration in (first, second):
                heartbeat = client.post(
                    f"/nodes/{registration['node_id']}/heartbeat",
                    headers={
                        "X-Node-Secret": settings["node_secret"],
                        "X-Node-Session": registration["session_token"],
                    },
                )
                if heartbeat.status_code != 200:
                    try:
                        heartbeat_detail = heartbeat.json().get("detail", "rejected")
                    except (ValueError, AttributeError):
                        heartbeat_detail = "rejected"
                    raise HarnessFailure(
                        f"session heartbeat {registration['node_id']} was rejected: "
                        f"{heartbeat_detail}"
                    )
                _assert_status(
                    heartbeat,
                    200,
                    f"session heartbeat {registration['node_id']}",
                )

            request_body = {
                "task": "Build two complete operational harness HTML attempts.",
                "strategy": "ensemble",
                "strategy_options": {"candidates": 2, "concurrency": 2},
                "placement": "distributed",
                "confidentiality": "trusted_guild",
                "remote_dispatch_consent": True,
                "requirements": {"allow_local_fallback": False},
                "output_contract": {
                    "kind": "single_artifact",
                    "format": "html",
                    "validators": [
                        {"name": "artifact_extraction", "required": True},
                        {"name": "code_parse", "required": True},
                    ],
                },
                "timeout_seconds": 30,
                "max_output_bytes": 4096,
            }
            _assert_status(
                client.post("/v1/executions", json=request_body),
                401,
                "pitch denial",
            )
            accepted = client.post(
                "/v1/executions",
                headers={"X-Pitch-Key": settings["pitch_key"]},
                json=request_body,
            )
            _assert_status(accepted, 202, "pitch authorization")
            execution_id = accepted.json().get("execution_id")
            if not execution_id:
                raise HarnessFailure("accepted pitch did not return an execution id")

            base_url = f"http://127.0.0.1:{port}"
            with ThreadPoolExecutor(max_workers=2) as workers:
                completions = [
                    workers.submit(
                        _settle_worker_task,
                        base_url,
                        settings,
                        registration,
                    )
                    for registration in (first, second)
                ]
                completed_workers = {
                    future.result(timeout=45)[0] for future in completions
                }
            if completed_workers != {"harness-node-a", "harness-node-b"}:
                raise HarnessFailure("both fake workers did not settle distributed attempts")

            execution_deadline = time.monotonic() + 30
            result_payload = None
            while time.monotonic() < execution_deadline:
                result = client.get(
                    f"/v1/executions/{execution_id}",
                    headers={"X-Viewer-Key": settings["viewer_key"]},
                )
                _assert_status(result, 200, "execution status")
                result_payload = result.json()
                if result_payload.get("status") in TERMINAL_EXECUTION_STATES:
                    break
                time.sleep(0.1)
            else:
                raise HarnessFailure("two-worker execution did not become terminal")

            if result_payload.get("lifecycle_status") != "completed":
                raise HarnessFailure("two-worker distributed execution did not complete")
            if result_payload.get("units_distributed") != 2:
                raise HarnessFailure("two-worker execution did not record two remote units")
            if set(result_payload.get("participating_nodes", [])) != completed_workers:
                raise HarnessFailure("execution telemetry lost a participating fake worker")
            if result_payload.get("artifact_integrity_mode") != "sealed":
                raise HarnessFailure("terminal artifact manifest was not sealed")
            sealed_hash = result_payload.get("sealed_manifest_hash")
            if not sealed_hash:
                raise HarnessFailure("sealed artifact manifest hash is absent")

            viewer_headers = {"X-Viewer-Key": settings["viewer_key"]}
            manifest_response = client.get(
                f"/v1/executions/{execution_id}/artifacts",
                headers=viewer_headers,
            )
            _assert_status(manifest_response, 200, "deliverable manifest")
            manifest = manifest_response.json()
            if manifest.get("integrity_mode") != "sealed" or not manifest.get("entries"):
                raise HarnessFailure("sealed deliverable manifest is empty or mutable")
            if manifest.get("manifest_hash") != sealed_hash:
                raise HarnessFailure("execution and artifact manifest hashes disagree")
            archive = client.get(
                f"/v1/executions/{execution_id}/download",
                headers=viewer_headers,
            )
            _assert_status(archive, 200, "deliverable archive")
            if not archive.content.startswith(b"PK"):
                raise HarnessFailure("deliverable archive is not a ZIP file")

            created_share = client.post(
                f"/v1/executions/{execution_id}/shares",
                headers=viewer_headers,
                json={"allow_artifact_download": True},
            )
            _assert_status(created_share, 201, "share creation")
            share = created_share.json()
            public_share = client.get(f"/v1/shares/{share['token']}")
            _assert_status(public_share, 200, "share capability")
            if public_share.headers.get("Cache-Control") != "no-store":
                raise HarnessFailure("share capability response is cacheable")
            revoked = client.delete(
                f"/v1/executions/{execution_id}/shares/{share['share_id']}",
                headers=viewer_headers,
            )
            _assert_status(revoked, 204, "share revocation")
            _assert_status(
                client.get(f"/v1/shares/{share['token']}"),
                404,
                "revoked share denial",
            )

            entry = manifest["entries"][0]
            relative_path = str(entry["relative_path"])
            artifact_path = state_dir / "execution_artifacts" / execution_id
            artifact_path = artifact_path.joinpath(*relative_path.split("/"))
            if not artifact_path.is_file():
                raise HarnessFailure("sealed artifact is absent from temporary state")
            artifact_path.write_text("mutated after sealing", encoding="utf-8")
            mutated = client.get(
                f"/v1/executions/{execution_id}/artifacts/{quote(relative_path, safe='/')}",
                headers=viewer_headers,
            )
            _assert_status(mutated, 409, "artifact mutation detection")
            stable_manifest = client.get(
                f"/v1/executions/{execution_id}/artifacts",
                headers=viewer_headers,
            )
            _assert_status(stable_manifest, 200, "sealed baseline reread")
            if stable_manifest.json().get("manifest_hash") != sealed_hash:
                raise HarnessFailure("artifact mutation rewrote the sealed baseline")

            dag_body = {
                "task": "Build two coordinated fake-worker HTML components.",
                "strategy": "dag",
                "strategy_options": {
                    "maximum_subtasks": 2,
                    "review_enabled": False,
                    "revision_enabled": False,
                },
                "placement": "distributed",
                "confidentiality": "trusted_guild",
                "remote_dispatch_consent": True,
                "requirements": {"allow_local_fallback": False},
                "timeout_seconds": 30,
                "max_output_bytes": 4096,
            }
            dag = client.post(
                "/v1/executions",
                headers={"X-Pitch-Key": settings["pitch_key"]},
                json=dag_body,
            )
            _assert_status(dag, 202, "distributed DAG submission")
            dag_execution_id = dag.json()["execution_id"]
            with ThreadPoolExecutor(max_workers=2) as workers:
                dag_completions = [
                    workers.submit(
                        _settle_worker_task,
                        base_url,
                        settings,
                        registration,
                    )
                    for registration in (first, second)
                ]
                dag_workers = {
                    future.result(timeout=45)[0] for future in dag_completions
                }
            dag_deadline = time.monotonic() + 30
            while time.monotonic() < dag_deadline:
                dag_result = client.get(
                    f"/v1/executions/{dag_execution_id}",
                    headers=viewer_headers,
                )
                _assert_status(dag_result, 200, "distributed DAG status")
                dag_payload = dag_result.json()
                if dag_payload.get("status") in TERMINAL_EXECUTION_STATES:
                    break
                time.sleep(0.1)
            else:
                raise HarnessFailure("distributed DAG execution did not become terminal")
            if (
                dag_payload.get("lifecycle_status") != "completed"
                or dag_payload.get("strategy_selected") != "dag"
                or dag_payload.get("units_distributed") != 2
                or set(dag_payload.get("participating_nodes", [])) != dag_workers
            ):
                raise HarnessFailure("distributed DAG execution telemetry is incomplete")

            direct_body = {
                "task": "Complete one authoritative fake-worker direct result.",
                "strategy": "direct",
                "strategy_options": {"candidates": 1, "concurrency": 1},
                "placement": "distributed",
                "confidentiality": "trusted_guild",
                "remote_dispatch_consent": True,
                "requirements": {"allow_local_fallback": False},
                "timeout_seconds": 30,
                "max_output_bytes": 4096,
            }
            direct = client.post(
                "/v1/executions",
                headers={"X-Pitch-Key": settings["pitch_key"]},
                json=direct_body,
            )
            _assert_status(direct, 202, "distributed direct submission")
            direct_execution_id = direct.json()["execution_id"]
            _settle_worker_task(base_url, settings, first)
            direct_deadline = time.monotonic() + 30
            while time.monotonic() < direct_deadline:
                direct_result = client.get(
                    f"/v1/executions/{direct_execution_id}",
                    headers=viewer_headers,
                )
                _assert_status(direct_result, 200, "distributed direct status")
                direct_payload = direct_result.json()
                if direct_payload.get("status") in TERMINAL_EXECUTION_STATES:
                    break
                time.sleep(0.1)
            else:
                raise HarnessFailure("distributed direct execution did not become terminal")
            if (
                direct_payload.get("lifecycle_status") != "completed"
                or direct_payload.get("strategy_requested") != "direct"
                or direct_payload.get("units_distributed") != 1
            ):
                raise HarnessFailure("distributed direct execution telemetry is incomplete")

            cancellation_body = {
                "task": "Hold one fake worker result for cancellation.",
                "strategy": "direct",
                "strategy_options": {"candidates": 1, "concurrency": 1},
                "placement": "distributed",
                "confidentiality": "trusted_guild",
                "remote_dispatch_consent": True,
                "requirements": {"allow_local_fallback": False},
                "timeout_seconds": 30,
                "max_output_bytes": 4096,
            }
            cancellable = client.post(
                "/v1/executions",
                headers={"X-Pitch-Key": settings["pitch_key"]},
                json=cancellation_body,
            )
            _assert_status(cancellable, 202, "cancellable execution submission")
            cancelled_execution_id = cancellable.json()["execution_id"]
            late_task = _claim_worker_task(base_url, settings, first)
            cancelled = client.post(
                f"/v1/executions/{cancelled_execution_id}/cancel",
                headers=viewer_headers,
            )
            _assert_status(cancelled, 200, "execution cancellation")
            if cancelled.json().get("lifecycle_status") != "cancelled":
                raise HarnessFailure("execution cancellation was not terminal")
            late_binding = _worker_binding(late_task, first["node_id"])
            late_result = client.post(
                f"/tasks/{late_task['task_id']}/result",
                headers=_node_headers(settings, first),
                json={
                    **late_binding,
                    "output": "late result must not settle",
                    "error": None,
                    "elapsed_seconds": 0.01,
                },
            )
            _assert_status(late_result, 403, "late result rejection")
    except BaseException as exc:
        failure = exc
    finally:
        requested_shutdown_signal = _request_graceful_shutdown(process)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            if failure is None:
                failure = HarnessFailure("coordinator did not shut down cleanly")
        output = process.stdout.read() if process.stdout is not None else ""
        fake_model.shutdown()
        fake_model.server_close()
        fake_thread.join(timeout=5)

    if (
        not _shutdown_exit_is_expected(
            process.returncode,
            requested_shutdown_signal,
            platform_name=os.name,
        )
        and failure is None
    ):
        failure = HarnessFailure(f"coordinator exited with status {process.returncode}")

    if failure is None:
        from coordinator_lock import CoordinatorLock

        released = CoordinatorLock(state_dir, deployment_mode="trusted_alpha")
        try:
            released.acquire()
        except Exception as exc:
            failure = HarnessFailure(
                f"coordinator lock remained held after shutdown ({type(exc).__name__})"
            )
        finally:
            released.release()

    if failure is not None:
        diagnostics = _redact(output[-4000:], sensitive).strip()
        if diagnostics:
            print("Sanitized coordinator diagnostics:", file=sys.stderr)
            print(diagnostics, file=sys.stderr)
        raise failure


def _run_pytest(profile: str, state_dir: Path, iteration: int) -> None:
    base_temp = state_dir / f"pytest-{iteration}"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--tb=short",
        "--basetemp",
        str(base_temp),
        *selected_nodeids(profile),
    ]
    environment = {
        **os.environ,
        "MYCELIUM_OPERATIONAL_HARNESS": "1",
        # A focused test that accidentally escapes its fake would fail quickly,
        # not discover or use an operator's Ollama instance.
        "OLLAMA_HOST": "http://127.0.0.1:9",
    }
    completed = subprocess.run(command, cwd=REPO_ROOT, env=environment, check=False)
    if completed.returncode:
        raise HarnessFailure(f"focused scenario matrix failed with status {completed.returncode}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("bounded", "nightly"),
        default="bounded",
        help="bounded runs once for CI; nightly adds churn/contention cases",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        help="repeat live restart and scenario cycles (default: 1 bounded, 5 nightly)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="print the non-secret scenario coverage without running it",
    )
    return parser


def _print_plan(profile: str, iterations: int) -> None:
    print(
        f"Trusted-alpha operational harness: profile={profile}, "
        f"iterations={iterations}, live_model=fake",
        flush=True,
    )
    print(
        "  live: ephemeral coordinator startup, health/protection, viewer/pitch/node auth, "
        "two fake workers across distributed direct/DAG/ensemble, stream/settlement, "
        "sealed artifacts, shares, cancellation/late rejection, clean shutdown",
        flush=True,
    )
    for scenario in profile_scenarios(profile):
        print(f"  {','.join(scenario.coverage)}: {scenario.description}", flush=True)
    print(
        f"  focused pytest selectors per iteration: {len(selected_nodeids(profile))}",
        flush=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    iterations = args.iterations if args.iterations is not None else (5 if args.profile == "nightly" else 1)
    if iterations < 1 or iterations > 20:
        _parser().error("--iterations must be between 1 and 20")
    missing = REQUIRED_COVERAGE - declared_coverage(args.profile)
    if missing:
        raise HarnessFailure("profile has undeclared coverage gaps: " + ", ".join(sorted(missing)))

    _print_plan(args.profile, iterations)
    if args.list_only:
        return 0

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mycelium-trusted-alpha-harness-") as temporary:
        root = Path(temporary)
        for iteration in range(1, iterations + 1):
            print(f"iteration {iteration}/{iterations}: live coordinator", flush=True)
            _exercise_live_coordinator(root / f"live-{iteration}")
            print(f"iteration {iteration}/{iterations}: focused scenarios", flush=True)
            _run_pytest(args.profile, root, iteration)

    elapsed = time.monotonic() - started
    print(
        f"TRUSTED-ALPHA HARNESS PASS: {iterations} iteration(s), "
        f"{len(selected_nodeids(args.profile))} selectors/iteration, {elapsed:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
