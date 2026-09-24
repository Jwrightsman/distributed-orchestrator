"""The two-machine batch script, against a mocked coordinator; no model, no network."""

import asyncio
import json

import httpx
import pytest

from evals import corpus
from execution.contracts import ExecutionRequestV1
from scripts import two_node_throughput as tnt

GOOD = ("import csv, json\n"
        "with open('inventory.csv', newline='') as f:\n"
        "    for row in csv.DictReader(f):\n"
        "        print(json.dumps(row))\n")


def test_every_default_request_is_a_valid_remote_only_canonical_request():
    items = {i.id: i for i in corpus.load_corpus()}
    for name in tnt.DEFAULT_ITEMS:
        request = ExecutionRequestV1.model_validate(tnt.request_for(items[name]))
        assert request.placement == "distributed" and request.remote_dispatch_consent
        assert request.requirements.allow_local_fallback is False
        assert request.strategy == "direct"


def test_batch_submits_all_at_once_waits_and_grades(tmp_path, monkeypatch):
    submitted, polls = [], {}

    def respond(request):
        assert request.headers["X-Pitch-Key"] == "PITCH-SECRET-7f3a"
        assert request.headers["X-Viewer-Key"] == "VIEWER-SECRET-9c1e"
        path = request.url.path
        if path == "/nodes":
            return httpx.Response(200, json={"nodes": [{"node_id": "a"}, {"node_id": "b"}]})
        if path == "/v1/executions" and request.method == "POST":
            submitted.append(json.loads(request.content))
            return httpx.Response(202, json={"execution_id": f"e{len(submitted)}"})
        parts = path.split("/")
        execution_id = parts[3]
        if len(parts) == 4:
            polls[execution_id] = polls.get(execution_id, 0) + 1
            status = "completed" if polls[execution_id] > 1 else "running"
            return httpx.Response(200, json={"lifecycle_status": status,
                                             "participating_nodes": ["a"]})
        if parts[4] == "artifacts" and len(parts) == 5:
            return httpx.Response(200, json={"entries": [
                {"relative_path": "candidates/c1/main.py", "role": "deliverable"}]})
        body = GOOD if execution_id == "e1" else "print('wrong')\n"
        return httpx.Response(200, content=body.encode())

    real_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        return real_client(*args, **kwargs)

    async def no_sleep(_):
        return None

    monkeypatch.setenv("MYCELIUM_PITCH_KEY", "PITCH-SECRET-7f3a")
    monkeypatch.setenv("MYCELIUM_VIEWER_KEY", "VIEWER-SECRET-9c1e")
    monkeypatch.setattr(tnt.httpx, "AsyncClient", mock_client)
    monkeypatch.setattr(tnt.asyncio, "sleep", no_sleep)

    class Args:
        server = "https://coordinator.example"
        expect_nodes = 2
        label = "two-machines"
        output = tmp_path / "two"
        items = ["transform-csv-to-jsonl", "transform-csv-to-jsonl"]

    asyncio.run(tnt.run(Args))
    assert len(submitted) == 2
    summary = json.loads((tmp_path / "two" / "summary.json").read_text(encoding="utf-8"))
    assert summary["tasks"] == 2 and summary["accepted"] == 1
    assert summary["workers"] == ["a", "b"]
    written = "".join(p.read_text(encoding="utf-8", errors="replace")
                      for p in (tmp_path / "two").rglob("*") if p.is_file())
    assert "PITCH-SECRET" not in written and "VIEWER-SECRET" not in written


def test_refuses_when_the_worker_count_is_not_what_was_asked(tmp_path, monkeypatch):
    real_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda r: httpx.Response(200, json={"nodes": [{"node_id": "a"}]}))
        return real_client(*args, **kwargs)

    monkeypatch.setenv("MYCELIUM_PITCH_KEY", "p")
    monkeypatch.setenv("MYCELIUM_VIEWER_KEY", "v")
    monkeypatch.setattr(tnt.httpx, "AsyncClient", mock_client)

    class Args:
        server = "https://coordinator.example"
        expect_nodes = 2
        label = "x"
        output = tmp_path / "out"
        items = list(tnt.DEFAULT_ITEMS)

    with pytest.raises(SystemExit, match="Expected 2"):
        asyncio.run(tnt.run(Args))
