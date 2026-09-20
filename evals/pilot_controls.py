"""Authored-fixture controls, not a benchmark or an acceptance evaluation."""

import asyncio
from dataclasses import replace
import gc
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import httpx

from evals import corpus
from evals.canonical_pilot import (
    BUDGET, BudgetLedger, BudgetStopped, CanonicalPilot, PreflightError,
    REQUIRED_IDENTITIES, checker_work, digest, planned_cells, prepare_manifest,
    repair_task,
)
from execution.call_capture import CallCollector, capture_calls, captured_post


async def dry_run(directory):
    """Exercise 36 scheduled control cells through the real canonical adapters.

HTTP model responses and acceptance judgments are mocked. Authored Python is
only extracted/parsed by production static validators, never executed.
"""
    import config
    from execution.artifacts import ArtifactStore
    from execution.persistence import ExecutionStore
    from execution.service import ExecutionService
    import ollama_client

    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    fixture_root = Path(__file__).parent / "fixtures" / "canonical_pilot"
    positive = (fixture_root / "positive.txt").read_text(encoding="utf-8").strip()
    broken = (fixture_root / "broken.txt").read_text(encoding="utf-8").strip()
    private_sentinel = "PRIVATE-CHECKER-ANSWER-7d0db55d4cd74b71"
    control_items = [
        replace(item, expect={**item.expect, "_private_control": private_sentinel})
        for item in corpus.load_corpus()
    ]
    private_expectations = {
        item.id: item.expect["_private_control"] for item in control_items
    }
    identities = {name: digest({"offline_fixture": name}) for name in REQUIRED_IDENTITIES}
    identities["hardware_id"] = "OFFLINE-MOCK-NOT-HARDWARE"
    identities["runtime_version"] = "OFFLINE-MOCK-NOT-A-RUNTIME"
    observed_prompts = []
    positive_checks = 0
    broken_checks = 0
    transport_calls = 0

    async def checker(item_id, candidate, *, deadline_monotonic):
        nonlocal positive_checks, broken_checks
        # The control sentinel is available only through checker-owned state.
        # No private values are returned to any generation path.
        assert private_expectations[item_id] == private_sentinel
        accepted = candidate.strip() == positive
        if accepted:
            positive_checks += 1
        else:
            broken_checks += 1
        return accepted, "requirement"

    def respond(request):
        nonlocal transport_calls
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["thinking"]})
        assert request.url.path == "/api/generate", request.url
        payload = json.loads(request.content)
        observed_prompts.append(payload)
        transport_calls += 1
        cap = payload["options"]["num_predict"]
        if cap == 512:
            output = json.dumps([
                {"id": 1, "title": "First", "prompt": "Write the authored control", "depends_on": []},
                {"id": 2, "title": "Second", "prompt": "Write the authored control", "depends_on": []},
            ])
        elif cap == 1536:
            output = f"## Quality Rating\nPASS\n\n## Issues Found\nNone\n\n## Final Assembled Output\n{positive}"
        elif cap == 2048 and "Repair this candidate:" not in payload["prompt"]:
            output = broken
        else:
            output = positive
        return httpx.Response(200, json={"done": True, "response": output,
                                        "prompt_eval_count": 40, "eval_count": 32,
                                        "total_duration": 1000000})

    original_client = httpx.AsyncClient
    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        return original_client(*args, **kwargs)

    # The command uses a disposable CWD; all canonical files/SQLite stay there.
    with tempfile.TemporaryDirectory(prefix="mycelium-pilot-controls-") as workspace:
        previous = Path.cwd()
        os.chdir(workspace)
        try:
            settings = dict(config.DEFAULTS)
            settings.update(model="qwen3.5:4b", provider=None)
            with patch.object(config.get, "_cache", settings, create=True), \
                    patch.object(httpx, "AsyncClient", mock_client), \
                    patch.dict(ollama_client._capabilities_cache, clear=True), \
                    patch.object(corpus, "load_corpus", return_value=control_items):
                db = Path(workspace) / "execution.db"
                service = ExecutionService(store=ExecutionStore(db),
                                           artifacts=ArtifactStore(db, allowed_roots=[Path(workspace)]))
                service._emit = lambda *args: None
                manifest = prepare_manifest("OFFLINE-CONTROL-NOT-A-BENCHMARK", identities)
                pilot = CanonicalPilot(service, manifest, lambda: identities, checker,
                                       directory, warm_cold="cold")
                for cell in planned_cells():
                    assert await pilot.run_cell(cell) == "accepted"
                assert len(pilot.ledger.cells) == 36
                assert transport_calls == len(pilot.collector.records) == 84
                assert pilot.collector.cost_capture_complete
                assert positive_checks == 36 and broken_checks == 12
                assert all(p["options"]["num_ctx"] == 8192 and p["options"]["temperature"] == 0
                           and p["options"]["seed"] == 20260913 and p["think"] is False
                           for p in observed_prompts)
                assert {p["options"]["num_predict"] for p in observed_prompts} == {
                    512, 768, 1536, 2048, 4096,
                }
                assert private_sentinel not in json.dumps(observed_prompts, ensure_ascii=False)

                from evals import runrecord
                from scripts.eval_study_summary import check_complete

                records = runrecord.load_runs(directory)
                check_complete(records, list(manifest["arms"]), manifest)
                assert len(records) == len(planned_cells()) == BUDGET["cells"]
                assert all(record["checker_capture_complete"] is True for record in records)

                budget_rejections = []
                for name, configure in (
                    ("generated_tokens", lambda ledger: setattr(
                        ledger, "tokens", BUDGET["generated_tokens"]
                    )),
                    ("inference_seconds", lambda ledger: setattr(
                        ledger, "inference_reserved_seconds", BUDGET["inference_seconds"]
                    )),
                    ("cell_tokens", lambda ledger: ledger.current.update(
                        reserved_tokens=BUDGET["cell_tokens"]
                    )),
                    ("cell_inference_seconds", lambda ledger: ledger.current.update(
                        inference_reserved_seconds=BUDGET["cell_inference_seconds"]
                    )),
                ):
                    probe = BudgetLedger()
                    probe.begin_cell(planned_cells()[0])
                    probe.current["arm"] = "direct"
                    configure(probe)
                    try:
                        await probe.before_call({"role": "direct"}, {}, "ollama")
                    except BudgetStopped:
                        assert not probe.lock.locked()
                        budget_rejections.append(name)
                    else:  # pragma: no cover - assertion documents the control
                        raise AssertionError(f"budget probe was admitted: {name}")
                checker_probe = BudgetLedger()
                checker_probe.checker_seconds = BUDGET["checker_seconds"]
                try:
                    with checker_work(checker_probe, {}):
                        pass
                except BudgetStopped:
                    budget_rejections.append("checker_seconds")
                else:  # pragma: no cover - assertion documents the control
                    raise AssertionError("checker budget probe was admitted")

                assert "Repair this candidate:" in repair_task(
                    "Public authored task", "AUTHORED CANDIDATE", "syntax"
                )
                repair_rejections = []
                for name, args in (
                    ("oversized", ("Public authored task", "x" * 1000, "syntax")),
                    ("private_diagnostic", (
                        "Public authored task", "AUTHORED CANDIDATE", private_sentinel
                    )),
                ):
                    try:
                        repair_task(*args)
                    except PreflightError:
                        repair_rejections.append(name)
                    else:  # pragma: no cover - assertion documents the control
                        raise AssertionError(f"repair probe was accepted: {name}")

                missing_collector = CallCollector(
                    "OFFLINE-MOCK-NOT-HARDWARE", warm_cold="cold"
                )

                def missing_cost_response(request):
                    return httpx.Response(200, json={
                        "done": True, "response": positive,
                        "prompt_eval_count": 4, "eval_count": 2,
                    })

                async with original_client(
                    transport=httpx.MockTransport(missing_cost_response)
                ) as missing_client:
                    with capture_calls(missing_collector, role="direct"):
                        await captured_post(
                            missing_client,
                            "http://offline/api/generate",
                            payload={"model": "qwen3.5:4b"},
                        )
                [missing_record] = missing_collector.records
                assert missing_collector.reconciled
                assert missing_collector.cost_capture_complete is False
                assert missing_record["inference_seconds"] is None
                assert missing_record["input_tokens"] == 4
                assert missing_record["output_tokens"] == 2

                report = {"kind": "offline_authored_fixture_control", "benchmark_executed": False,
                          "planned_cells": 36, "control_cells": 36, "mock_runtime_calls": transport_calls,
                          "positive_fixture_checks": positive_checks, "broken_fixture_checks": broken_checks,
                          "cost_capture_complete": True, "identities": "synthetic offline controls only",
                          "checker_capture_complete": pilot.ledger.checker_capture_complete,
                          "planned_cell_accounting_proven": True,
                          "budget_rejections_proven": budget_rejections,
                          "repair_input_bounds_proven": repair_rejections,
                          "private_answer_exclusion_proven": True,
                          "missing_cost_remains_unknown": True,
                          "real_acceptance_checks_exercised": False}
                (directory / "dry-run-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                return report
        finally:
            os.chdir(previous)
            # Legacy contribution readers use SQLite transaction contexts;
            # collect their unreferenced connections before Windows temp cleanup.
            gc.collect()


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(dry_run(args.output)), indent=2))


if __name__ == "__main__":
    main()
