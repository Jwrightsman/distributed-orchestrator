"""Canonical REST, CLI, MCP, event, and worker-payload surfaces."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

import cli
import execution.strategies as strategies
import mcp_server
import routes_executions
import server_state as state
from execution.contracts import ExecutionRequestV1
from execution.dispatch import Dispatcher, ExecutionUnit, PlacementDecision
from execution.persistence import ExecutionStore
from execution.service import ExecutionService
from server import app


@pytest.fixture(autouse=True)
def clean_runtime():
    for store in (
        state.nodes,
        state.task_queue,
        state.task_inflight,
        state.task_results,
        state.jobs,
        state._pitch_timestamps,
    ):
        store.clear()
    yield


def test_canonical_rest_post_and_get_normalized_execution(tmp_path, monkeypatch):
    service = ExecutionService(store=ExecutionStore(tmp_path / "executions.db"))
    captured = {}

    def submit(request):
        captured["request"] = request
        queued = service._new_result(request, "e" * 32, None, "queued")
        service.store.save(request, queued)
        return queued

    monkeypatch.setattr(service, "submit", submit)
    monkeypatch.setattr(routes_executions, "get_execution_service", lambda: service)
    with TestClient(app) as client:
        response = client.post(
            "/v1/executions",
            json={
                "protocol_version": "1",
                "task": "Build a complete option",
                "strategy": "direct",
                "placement": "local",
            },
        )
        fetched = client.get(f"/v1/executions/{'e' * 32}")

    assert response.status_code == 202
    assert response.json()["execution_id"] == "e" * 32
    assert response.json()["strategy_selected"] == "ensemble"
    assert captured["request"].strategy == "direct"
    body = fetched.json()
    assert body["protocol_version"] == "1"
    assert body["strategy_requested"] == "direct"
    assert body["selector_version"] == "conservative-v1"


def test_canonical_rest_rejects_unknown_strategy_before_service(monkeypatch):
    monkeypatch.setattr(
        routes_executions,
        "get_execution_service",
        lambda: pytest.fail("invalid request reached service"),
    )
    with TestClient(app) as client:
        response = client.post("/v1/executions", json={"task": "x", "strategy": "debate"})
    assert response.status_code == 422
    assert "strategy" in response.text


@pytest.mark.parametrize(
    ("argv", "strategy", "candidates", "placement", "remaining"),
    [
        (["build", "it"], "auto", None, "local", ["build", "it"]),
        (
            ["build", "it", "--strategy", "ensemble", "--candidates", "3", "--placement", "distributed"],
            "ensemble",
            3,
            "distributed",
            ["build", "it"],
        ),
        (["--project", "demo", "next", "--strategy", "direct"], "direct", None, "local", ["--project", "demo", "next"]),
    ],
)
def test_cli_execution_argument_parsing(argv, strategy, candidates, placement, remaining):
    options, rest = cli.parse_execution_args(argv)
    assert (options.strategy, options.candidates, options.placement) == (strategy, candidates, placement)
    assert rest == remaining


@pytest.mark.parametrize(
    "argv",
    [
        ["task", "--strategy", "dag", "--candidates", "2"],
        ["task", "--strategy", "direct", "--candidates", "2"],
        ["task", "--strategy", "auto", "--candidates", "3"],
        ["task", "--strategy", "ensemble", "--candidates", "6"],
    ],
)
def test_cli_rejects_invalid_strategy_combinations(argv):
    with pytest.raises(SystemExit):
        cli.parse_execution_args(argv)


@pytest.mark.asyncio
async def test_mcp_pitch_forwards_optional_protocol_fields(monkeypatch):
    captured = {}

    class RecordingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, path, json):
            captured.update({"path": path, "json": json})
            return httpx.Response(
                200,
                json={"job_id": "job_abcdef1234567890", "execution_id": "a" * 32},
            )

    monkeypatch.setattr(mcp_server, "_client", lambda: RecordingClient())
    output = await mcp_server.pitch_task(
        "Build one file",
        strategy="ensemble",
        candidates=4,
        placement="distributed",
        output_contract={"kind": "single_artifact", "format": "html"},
        verification_policy={"validators": [{"name": "code_parse"}]},
        confidentiality="approved_nodes",
    )

    assert captured["path"] == "/pitch/async"
    assert captured["json"]["strategy"] == "ensemble"
    assert captured["json"]["candidates"] == 4
    assert captured["json"]["placement"] == "distributed"
    assert captured["json"]["output_contract"]["kind"] == "single_artifact"
    assert captured["json"]["verification"]["validators"][0]["name"] == "code_parse"
    assert "execution_id" in output


@pytest.mark.asyncio
async def test_execution_events_cover_selection_units_validation_and_completion(tmp_path, monkeypatch):
    emitted = []

    async def generated(*args, **kwargs):
        return "complete output"

    monkeypatch.setattr(strategies, "generate", generated)
    monkeypatch.setattr(strategies.EnsembleStrategy, "artifact_root", tmp_path / "artifacts")
    service = ExecutionService(store=ExecutionStore(tmp_path / "executions.db"))
    service._emit = lambda name, data: emitted.append((name, data))
    queued = service.submit(
        ExecutionRequestV1(task="Complete it", strategy="direct", placement="local")
    )
    for _ in range(100):
        await asyncio.sleep(0.01)
        result = service.get(queued.execution_id)
        if result and result.status not in ("queued", "running"):
            break

    names = [name for name, _ in emitted]
    assert names[:2] == ["execution_created", "strategy_selected"]
    assert "attempt_started" in names
    assert "candidate_generated" in names
    assert "candidate_validation_completed" in names
    assert "winner_selected" in names
    assert names[-1] == "execution_completed"


@pytest.mark.asyncio
async def test_distributed_worker_payload_carries_protocol_identity():
    request = ExecutionRequestV1.model_validate({
        "task": "Build it",
        "strategy": "ensemble",
        "strategy_options": {"candidates": 1},
        "placement": "distributed",
        "output_contract": {"kind": "single_artifact"},
    })
    unit = ExecutionUnit(
        unit_id="candidate-1",
        kind="candidate",
        title="Complete candidate 1",
        prompt="Build it",
        system="system",
    )
    dispatcher = Dispatcher()
    running = asyncio.create_task(
        dispatcher._distributed(
            unit,
            request,
            "f" * 32,
            "ensemble",
            PlacementDecision("distributed", "test", qualifying_nodes=("worker",)),
        )
    )
    for _ in range(100):
        await asyncio.sleep(0.001)
        if state.task_queue:
            break
    queued = state.task_queue[0]
    state.task_results[queued["task_id"]] = {
        "node_id": "worker",
        "output": "complete output",
        "error": None,
    }
    result = await running

    assert queued["contract_version"] == "1"
    assert queued["execution_id"] == "f" * 32
    assert queued["strategy"] == "ensemble"
    assert queued["execution_unit_id"] == "candidate-1"
    assert queued["execution_unit_kind"] == "candidate"
    assert queued["output_contract"]["kind"] == "single_artifact"
    assert queued["eligible_nodes"] == ["worker"]
    assert result.output == "complete output"


def test_queue_limit_is_atomic_for_a_generated_wave(monkeypatch):
    monkeypatch.setattr(state, "_MAX_TASK_QUEUE", 3)
    with ThreadPoolExecutor(max_workers=12) as pool:
        accepted = list(pool.map(lambda index: state.enqueue_task({"task_id": str(index)}), range(20)))
    assert sum(accepted) == 3
    assert len(state.task_queue) == 3
