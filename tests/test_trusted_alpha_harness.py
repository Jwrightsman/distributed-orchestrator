"""Focused checks for the operational harness and its fake distributed direct path."""

from __future__ import annotations

import pytest

import execution.strategies as strategies
import server_state as state
from execution.artifacts import ArtifactStore
from execution.contracts import ExecutionRequestV1
from execution.dispatch import DispatchResult, Dispatcher
from execution.persistence import ExecutionStore
from execution.service import ExecutionService
from scripts import trusted_alpha_harness as harness


def test_bounded_harness_declares_every_release_coverage_axis():
    assert harness.declared_coverage("bounded") == harness.REQUIRED_COVERAGE
    assert len(harness.selected_nodeids("bounded")) == len(
        set(harness.selected_nodeids("bounded"))
    )
    for nodeid in harness.selected_nodeids("bounded"):
        assert (harness.REPO_ROOT / nodeid.split("::", 1)[0]).is_file()


def test_nightly_profile_adds_churn_without_losing_bounded_coverage():
    bounded = set(harness.selected_nodeids("bounded"))
    nightly = set(harness.selected_nodeids("nightly"))

    assert harness.declared_coverage("nightly") == harness.REQUIRED_COVERAGE
    assert bounded < nightly
    assert any("many_nodes_churning" in nodeid for nodeid in nightly)
    assert any("concurrent_store_initialization" in nodeid for nodeid in nightly)
    assert any("active_execution_is_never_pruned" in nodeid for nodeid in nightly)


def test_harness_diagnostics_redact_every_static_authority():
    authorities = ("viewer-private-value", "pitch-private-value", "node-private-value")
    rendered = harness._redact("/".join(authorities), authorities)

    assert rendered == "<redacted>/<redacted>/<redacted>"
    assert not any(authority in rendered for authority in authorities)


def test_ci_runs_bounded_harness_and_nightly_runs_repeated_churn():
    ci = (harness.REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    nightly = (
        harness.REPO_ROOT / ".github" / "workflows" / "trusted-alpha-nightly.yml"
    ).read_text(encoding="utf-8")

    assert "trusted_alpha_harness.py --profile bounded" in ci
    assert "Image starts one coordinator process" in ci
    assert "/v1/operator/health" in ci
    assert "schedule:" in nightly
    assert "workflow_dispatch:" in nightly
    assert "--profile nightly --iterations" in nightly
    assert "SQLite contention, artifact churn, and node reconnects" in nightly


def test_sealed_artifact_is_retrievable_after_store_restart(tmp_path):
    database = tmp_path / "events.db"
    storage = tmp_path / "execution_artifacts"
    root = storage / ("e" * 32)
    deliverable = root / "code" / "main.py"
    deliverable.parent.mkdir(parents=True)
    deliverable.write_text("print('durable artifact')\n", encoding="utf-8")

    before_restart = ArtifactStore(database, allowed_roots=[storage])
    before_restart.register_root("e" * 32, root, active=True)
    sealed = before_restart.seal_manifest("e" * 32)

    after_restart = ArtifactStore(database, allowed_roots=[storage])
    reopened = after_restart.get_manifest("e" * 32)
    resolved, entry = after_restart.resolve_entry("e" * 32, "code/main.py")

    assert reopened.integrity_mode == "sealed"
    assert reopened.manifest_hash == sealed.manifest_hash
    assert entry.sha256 == sealed.entries[0].sha256
    assert resolved.read_text(encoding="utf-8") == "print('durable artifact')\n"


@pytest.mark.asyncio
async def test_distributed_direct_completes_with_fake_worker(tmp_path, monkeypatch):
    state.nodes.clear()
    state.task_queue.clear()
    state.task_inflight.clear()
    state.task_results.clear()
    state.nodes["fake-worker"] = {"capabilities": ["code"], "last_seen": 0}
    monkeypatch.setattr(
        strategies.EnsembleStrategy,
        "artifact_root",
        tmp_path / "execution_artifacts",
    )
    dispatched = []

    async def fake_remote(self, unit, request, execution_id, strategy, decision, **kwargs):
        dispatched.append((unit, request, execution_id, strategy, decision, kwargs))
        return DispatchResult(
            unit=unit,
            status="completed",
            output="complete fake-worker direct result",
            placement="distributed",
            node_id="fake-worker",
            duration_ms=2,
            attempt_count=1,
            observed_placements=("distributed",),
        )

    monkeypatch.setattr(Dispatcher, "_distributed", fake_remote)
    database = tmp_path / "events.db"
    service = ExecutionService(
        store=ExecutionStore(database),
        artifacts=ArtifactStore(database, allowed_roots=[tmp_path]),
    )
    service._emit = lambda *_args, **_kwargs: None
    request = ExecutionRequestV1.model_validate(
        {
            "task": "Complete one fake distributed direct result.",
            "strategy": "direct",
            "strategy_options": {"candidates": 1, "concurrency": 1},
            "placement": "distributed",
            "confidentiality": "trusted_guild",
            "remote_dispatch_consent": True,
            "requirements": {"allow_local_fallback": False},
        }
    )

    run = await service.execute(request)

    assert run.result.lifecycle_status == "completed"
    assert run.result.strategy_requested == "direct"
    assert run.result.strategy_selected == "ensemble"
    assert run.result.placement_requested == "distributed"
    assert run.result.placement_observed == "distributed"
    assert run.result.participating_nodes == ["fake-worker"]
    assert run.result.attempt_count == 1
    assert len(dispatched) == 1
    unit, dispatched_request, _execution_id, selected_strategy, decision, _kwargs = dispatched[0]
    assert unit.kind == "candidate"
    assert dispatched_request.remote_dispatch_consent is True
    assert selected_strategy == "ensemble"
    assert decision.qualifying_nodes == ("fake-worker",)
