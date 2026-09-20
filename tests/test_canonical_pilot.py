"""Preflight controls; no real runtime or generated-artifact execution."""

import asyncio
import copy
import json
import time

import httpx
import pytest

from evals.canonical_pilot import (
    BUDGET, BudgetLedger, BudgetStopped, CanonicalPilot, DIGEST_IDENTITIES,
    PreflightError, REQUIRED_IDENTITIES,
    checker_work, digest, planned_cells, prepare_manifest, repair_task, validate_activation,
)
from execution.call_capture import CallCollector, capture_calls, captured_post


def test_plan_membership_bounds_and_unresolved_identities():
    manifest = prepare_manifest("authored-preflight")
    assert len(manifest["schedule"]) == len({(c["item_id"], c["arm"]) for c in manifest["schedule"]}) == 36
    assert manifest["budget"] == BUDGET
    assert BUDGET["generated_tokens"] == 36 * 4096
    assert manifest["identity"]["measurement_digest"] == "e4c78f84c67ea64a1ccf5ae2390c6bd15cc0423c5455978bcb2a53e1e7418ce5"
    assert manifest["identity"]["model_digest"] is None
    with pytest.raises(PreflightError, match="unresolved"):
        validate_activation(manifest, {})


@pytest.mark.parametrize("field", ["budget", "schedule", "settings", "identity"])
def test_freeze_rejects_drift(field):
    observed = {key: digest({"offline": key}) for key in REQUIRED_IDENTITIES}
    manifest = prepare_manifest("offline", observed)
    validate_activation(manifest, observed)
    changed = copy.deepcopy(manifest)
    changed[field] = {}
    with pytest.raises(PreflightError, match="frozen"):
        validate_activation(changed, observed)


@pytest.mark.parametrize("field", DIGEST_IDENTITIES)
def test_activation_rejects_tags_or_labels_in_digest_fields(field):
    observed = {key: digest({"offline": key}) for key in REQUIRED_IDENTITIES}
    observed[field] = "qwen3.5:4b" if field == "model_digest" else "latest"
    manifest = prepare_manifest("offline", observed)
    with pytest.raises(PreflightError, match="SHA-256"):
        validate_activation(manifest, observed)


def test_activation_accepts_ollama_sha256_prefix():
    observed = {key: digest({"offline": key}) for key in REQUIRED_IDENTITIES}
    observed["model_digest"] = "sha256:" + observed["model_digest"]
    manifest = prepare_manifest("offline", observed)
    validate_activation(manifest, observed)


def test_repair_never_truncates_or_accepts_private_diagnostic_text():
    candidate = "AUTHORED CANDIDATE"
    text = repair_task("Public task", candidate, "syntax")
    assert candidate in text
    with pytest.raises(PreflightError, match="1000-character"):
        repair_task("Public task", "x" * 1000, "syntax")
    with pytest.raises(PreflightError, match="allowlisted"):
        repair_task("Public task", candidate, "PRIVATE-REFERENCE-ANSWER")


def direct_cell():
    return {"item_id": planned_cells()[0]["item_id"], "arm": "direct", "replicate": 0}


def ledger_for_call():
    ledger = BudgetLedger()
    ledger.begin_cell(planned_cells()[0])
    # Unit-level ledger controls choose a role; end-to-end controls use the
    # immutable schedule without this fixture substitution.
    ledger.current["arm"] = "direct"
    return ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["tokens", "time", "cell_time", "deadline", "retry"])
async def test_dispatch_stops_before_budget_loss(limit):
    ledger = ledger_for_call()
    if limit == "tokens":
        ledger.tokens = 147455
    elif limit == "time":
        ledger.inference_reserved_seconds = 21600
    elif limit == "cell_time":
        ledger.current["inference_reserved_seconds"] = 600
    elif limit == "deadline":
        ledger.current["deadline"] = 0
    else:
        ledger.current["calls_by_role"]["direct"] = 1
    with pytest.raises(BudgetStopped):
        await ledger.before_call({"role": "direct"}, {}, "ollama")
    assert not ledger.lock.locked()


@pytest.mark.asyncio
async def test_exact_runtime_ceiling_finishes_current_call_then_blocks_next():
    ledger = ledger_for_call()
    ledger.tokens = BUDGET["generated_tokens"] - BUDGET["cell_tokens"]
    record = {"role": "direct"}
    await ledger.before_call(record, {}, "ollama")
    now = time.monotonic()
    record.update(
        input_tokens=1,
        output_tokens=BUDGET["cell_tokens"],
        inference_seconds=0.0,
        termination_confirmed=True,
        start_monotonic=now,
        end_monotonic=now,
    )
    await ledger.after_call(record)
    assert ledger.tokens == BUDGET["generated_tokens"]
    assert ledger.stopped is None
    assert not ledger.lock.locked()
    ledger.current["calls_by_role"].clear()
    with pytest.raises(BudgetStopped, match="token reservation"):
        await ledger.before_call({"role": "direct"}, {}, "ollama")
    assert not ledger.lock.locked()


@pytest.mark.asyncio
async def test_missing_cost_blocks_next_dispatch_and_stays_unknown():
    ledger = ledger_for_call()
    collector = CallCollector("offline", before_dispatch=ledger.before_call, after_finish=ledger.after_call)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"done": True, "response": "authored"}))) as client:
        with capture_calls(collector, role="direct"):
            await captured_post(client, "http://offline/api/generate", payload={})
            with pytest.raises(RuntimeError, match="unknown"):
                await captured_post(client, "http://offline/api/generate", payload={})
    assert len(collector.records) == 1
    assert collector.records[0]["output_tokens"] is None
    assert collector.records[0]["inference_seconds"] is None
    assert collector.reconciled and not collector.cost_capture_complete
    assert ledger.stopped


@pytest.mark.asyncio
async def test_all_started_calls_reconciled_after_cancellation():
    entered = asyncio.Event()
    async def wait(request):
        entered.set()
        await asyncio.Event().wait()
    collector = CallCollector("offline", warm_cold="warm")
    async with httpx.AsyncClient(transport=httpx.MockTransport(wait)) as client:
        with capture_calls(collector, execution_id="fixture", role="planner"):
            work = asyncio.create_task(captured_post(client, "http://offline/api/generate", payload={}))
            await entered.wait()
            assert not collector.reconciled
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
    assert collector.reconciled and not collector.cost_capture_complete
    assert collector.records[0]["outcome"] == "cancelled"
    assert not collector.records[0]["termination_confirmed"]


@pytest.mark.asyncio
async def test_retry_attempts_each_have_distinct_cost_and_identity():
    collector = CallCollector("offline", warm_cold="warm")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"done": True, "response": "authored", "prompt_eval_count": 4, "eval_count": 3, "total_duration": 1000000000}))) as client:
        with capture_calls(collector, execution_id="fixture", candidate_id="candidate-1", role="builder"):
            for attempt in (0, 1):
                await captured_post(client, "http://offline/api/generate", payload={}, attempt=attempt)
    assert collector.cost_capture_complete
    assert len({row["call_id"] for row in collector.records}) == 2
    assert [row["attempt"] for row in collector.records] == [0, 1]
    assert sum(row["output_tokens"] for row in collector.records) == 6


def test_checker_budget_nesting_and_deadline():
    ledger = BudgetLedger(clock=lambda: 1.0)
    ledger.checker_seconds = 3599
    outer = {}
    with checker_work(ledger, outer):
        assert outer["deadline_monotonic"] == 2.0
        with checker_work(ledger, {}):
            pass
    ledger.checker_seconds = 3600
    with pytest.raises(BudgetStopped, match="checker"):
        with checker_work(ledger, {}):
            pass


def test_checker_failure_persists_unknown_termination(tmp_path):
    journal = tmp_path / "events.jsonl"
    ledger = BudgetLedger(journal=journal)
    with pytest.raises(RuntimeError, match="authored checker failure"):
        with checker_work(ledger, {}):
            raise RuntimeError("authored checker failure")
    assert ledger.checker_capture_complete is False
    event = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    assert event["event"] == "checker_finished"
    assert event["capture_complete"] is False
    assert event["seconds_observed"] >= 0
    assert event["stop_reason"] == "checker failed or termination unknown"


@pytest.mark.asyncio
async def test_dynamic_warm_cold_must_be_observed_before_http_dispatch(tmp_path):
    observed = {key: digest({"offline": key}) for key in REQUIRED_IDENTITIES}
    manifest = prepare_manifest("offline", observed)

    class Service:
        validators = None

    async def checker(*args, **kwargs):
        return True, "requirement"

    pilot = CanonicalPilot(
        Service(), manifest, lambda: observed, checker, tmp_path, warm_cold=lambda: None
    )
    dispatched = False

    def respond(request):
        nonlocal dispatched
        dispatched = True
        return httpx.Response(200, json={"done": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with capture_calls(pilot.collector, role="direct"):
            with pytest.raises(PreflightError, match="warm/cold"):
                await captured_post(
                    client,
                    "http://offline/api/generate",
                    payload={"model": "qwen3.5:4b"},
                )
    assert dispatched is False
    assert pilot.collector.records == []


@pytest.mark.asyncio
async def test_actual_canonical_paths_offline_all_planned_cells(tmp_path):
    from evals.pilot_controls import dry_run
    from evals.runrecord import load_manifest, load_runs
    from scripts.eval_study_summary import check_complete, cost_for
    directory = tmp_path / "control"
    report = await dry_run(directory)
    assert report["benchmark_executed"] is False
    assert report["mock_runtime_calls"] == 84
    records = load_runs(directory)
    manifest = load_manifest(directory)
    check_complete(records, list(manifest["arms"]), manifest)
    assert all(cost_for(records, arm)["cost_complete"] for arm in manifest["arms"])
    assert all(cost_for(records, arm)["checker_cost_complete"] for arm in manifest["arms"])
    assert report["private_answer_exclusion_proven"] is True
    assert report["checker_capture_complete"] is True
    assert report["planned_cell_accounting_proven"] is True
    assert set(report["budget_rejections_proven"]) == {
        "generated_tokens", "inference_seconds", "cell_tokens",
        "cell_inference_seconds", "checker_seconds",
    }
    assert set(report["repair_input_bounds_proven"]) == {
        "oversized", "private_diagnostic",
    }
    assert report["missing_cost_remains_unknown"] is True
    events = [json.loads(line) for line in (directory / "pilot-events.jsonl").read_text().splitlines()]
    calls = [event["call"] for event in events if event["event"] == "call_finished"]
    assert {call["role"] for call in calls} == {"planner", "builder", "reviewer", "direct"}
    assert all(call["execution_id"] for call in calls)
    assert all(call["unit_id"] for call in calls if call["role"] in {"builder", "direct"})
    assert max(call["end_monotonic"] for call in calls) <= time.monotonic()
