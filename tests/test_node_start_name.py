"""`python node.py --server X` must start the worker the installer enrolled.

The installer can enrol under a name other than the hostname, and the identity
file is bound to that name. The start command it prints has no `--node-id`, so
the worker has to take the name from the file — found in the 2026-09-21
onboarding rehearsal, where the printed command failed with "worker identity
belongs to a different node_id" and still exited 0.
"""

from __future__ import annotations

import asyncio

import pytest

import node
from node_capabilities import NodeCapabilityDescriptorV1
from worker_identity import WorkerIdentityError, create_worker_identity

COORDINATOR = "https://coordinator.example"


@pytest.fixture
def enrolled_identity(tmp_path):
    path = tmp_path / "identity.json"
    create_worker_identity(path, coordinator=COORDINATOR, node_id="laptop-ab12")
    return path


def _descriptor() -> NodeCapabilityDescriptorV1:
    return NodeCapabilityDescriptorV1.model_validate(
        {
            "executor": {"kind": "ollama", "worker_protocol_version": "1"},
            "models": [{"provider": "ollama", "name": "qwen3.5:4b"}],
            "hardware": {"architecture": "x86_64", "logical_cpu_count": 4},
            "features": [],
            "limits": {
                "max_concurrent_execution_units": 1,
                "max_output_bytes": 1_048_576,
            },
            "isolation": {"kind": "none"},
        }
    )


def test_the_enrolled_name_is_read_back_from_the_identity_file(enrolled_identity, tmp_path):
    assert node._enrolled_node_id(enrolled_identity, COORDINATOR) == "laptop-ab12"
    assert node._enrolled_node_id(tmp_path / "missing.json", COORDINATOR) is None


def test_without_node_id_the_worker_starts_under_the_enrolled_name(
    enrolled_identity, monkeypatch
):
    chosen: list[str] = []

    async def _ollama_ok():
        return {"ok": True, "models": ["qwen3.5:4b"]}

    async def _descriptor_stub(**_kwargs):
        return _descriptor()

    def _capture(path, *, coordinator, node_id):
        chosen.append(node_id)
        raise WorkerIdentityError("stop here; the name is what is under test")

    monkeypatch.setattr(node, "check_ollama", _ollama_ok)
    monkeypatch.setattr(node, "build_stock_capability_descriptor", _descriptor_stub)
    monkeypatch.setattr(node, "load_or_create_worker_identity", _capture)
    monkeypatch.setattr(node.platform, "node", lambda: "Jwrightsman")

    code = asyncio.run(
        node.main(["--server", COORDINATOR, "--identity-file", str(enrolled_identity)])
    )

    assert chosen == ["laptop-ab12"], "the hostname must not override the enrolled name"
    assert code == 1


def test_a_mismatched_node_id_names_the_one_that_works_and_fails_loudly(
    enrolled_identity, capsys
):
    code = asyncio.run(
        node.main(
            [
                "--server",
                COORDINATOR,
                "--identity-file",
                str(enrolled_identity),
                "--node-id",
                "jwrightsman",
            ]
        )
    )

    assert code == 1, "a worker that cannot start must not exit 0"
    out = " ".join(capsys.readouterr().out.split())
    assert "joined https://coordinator.example as 'laptop-ab12'" in out
    assert "--node-id laptop-ab12" in out
