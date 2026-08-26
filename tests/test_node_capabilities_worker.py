"""Stock-worker capability detection stays bounded, optional, and claim-only."""

from __future__ import annotations

import json
import subprocess

import pytest

import node
from node_capabilities import (
    ExecutorDescriptorV1,
    HardwareDescriptorV1,
    IsolationDescriptorV1,
    ModelDescriptorV1,
    NodeCapabilityDescriptorV1,
    NodeLimitDescriptorV1,
)


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _MetadataClient:
    def __init__(self, responses: dict[str, _Response | BaseException]):
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url: str):
        value = self.responses[url.rsplit("/", 1)[-1]]
        if isinstance(value, BaseException):
            raise value
        return value


def _descriptor() -> NodeCapabilityDescriptorV1:
    return NodeCapabilityDescriptorV1(
        descriptor_version="1",
        executor=ExecutorDescriptorV1(
            kind="ollama", version="0.12.0", worker_protocol_version="1"
        ),
        models=[
            ModelDescriptorV1(
                provider="ollama",
                name="worker-model:latest",
                digest="a" * 64,
                context_tokens=8192,
            )
        ],
        hardware=HardwareDescriptorV1(
            architecture="x86_64",
            logical_cpu_count=8,
            total_memory_bytes=16 * 1024**3,
            gpus=[],
        ),
        features=["code"],
        limits=NodeLimitDescriptorV1(
            max_concurrent_execution_units=1,
            max_output_bytes=node._MAX_WORKER_OUTPUT_BYTES,
            max_context_tokens=8192,
        ),
        isolation=IsolationDescriptorV1(kind="none"),
    )


def test_nvidia_probe_is_fixed_argv_bounded_and_parses_memory(monkeypatch):
    captured = {}

    def run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="NVIDIA RTX 4090, 24564\nNVIDIA A100, 40960\n",
            stderr="",
        )

    monkeypatch.setattr(node.subprocess, "run", run)

    gpus = node._detect_nvidia_gpus()

    assert gpus is not None and len(gpus) == 2
    assert gpus[0].vendor == "nvidia"
    assert gpus[0].model == "NVIDIA RTX 4090"
    assert gpus[0].memory_bytes == 24564 * 1024**2
    assert captured["argv"] == [
        "nvidia-smi",
        "--query-gpu=name,memory.total",
        "--format=csv,noheader,nounits",
    ]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == node._GPU_PROBE_TIMEOUT_SECONDS


def test_nvidia_probe_collapses_identical_capability_claims(monkeypatch):
    monkeypatch.setattr(
        node.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout="NVIDIA RTX 4090, 24564\nNVIDIA RTX 4090, 24564\n",
            stderr="",
        ),
    )

    gpus = node._detect_nvidia_gpus()

    assert gpus is not None and len(gpus) == 1
    assert gpus[0].model == "NVIDIA RTX 4090"


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("missing"),
        subprocess.TimeoutExpired("nvidia-smi", 3),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"),
    ],
)
def test_optional_gpu_probe_failures_are_explicit_unknowns(monkeypatch, failure):
    def run(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(node.subprocess, "run", run)

    assert node._detect_nvidia_gpus() is None


def test_macos_memory_probe_treats_decode_failure_as_unknown(monkeypatch):
    monkeypatch.setattr(node.platform, "system", lambda: "Darwin")

    def run(*_args, **_kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")

    monkeypatch.setattr(node.subprocess, "run", run)

    assert node._memory_from_macos_sysctl() is None


def test_gpu_probe_rejects_unbounded_or_malformed_output(monkeypatch):
    monkeypatch.setattr(
        node.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout="x" * 16_385, stderr=""
        ),
    )
    assert node._detect_nvidia_gpus() is None

    monkeypatch.setattr(
        node.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout=f"{'x' * 129}, not-a-number\n", stderr=""
        ),
    )
    assert node._detect_nvidia_gpus() is None


def test_total_memory_uses_first_safe_standard_library_source(monkeypatch):
    monkeypatch.setattr(node, "_memory_from_sysconf", lambda: 8 * 1024**3)

    def must_not_run():
        raise AssertionError("later fallback ran after a safe result")

    monkeypatch.setattr(node, "_memory_from_windows", must_not_run)
    monkeypatch.setattr(node, "_memory_from_proc", must_not_run)
    monkeypatch.setattr(node, "_memory_from_macos_sysctl", must_not_run)

    assert node._total_memory_bytes() == 8 * 1024**3


@pytest.mark.asyncio
async def test_ollama_supplied_digest_and_version_are_claimed_without_invention(
    monkeypatch,
):
    digest = "b" * 64
    responses = {
        "version": _Response({"version": "0.12.3"}),
        "tags": _Response(
            {
                "models": [
                    {
                        "name": "worker-model:latest",
                        "digest": f"sha256:{digest}",
                        "details": {"quantization_level": "Q4_K_M"},
                    }
                ]
            }
        ),
    }
    monkeypatch.setattr(
        node.httpx,
        "AsyncClient",
        lambda **_kwargs: _MetadataClient(responses),
    )
    monkeypatch.setattr(
        node,
        "_detected_hardware_descriptor",
        lambda: HardwareDescriptorV1(architecture="x86_64"),
    )

    descriptor = await node.build_stock_capability_descriptor(
        model="worker-model:latest",
        context_tokens=8192,
        ollama_url="http://localhost:11434",
    )

    assert descriptor.executor.version == "0.12.3"
    assert descriptor.models[0].digest == f"sha256:{digest}"
    assert descriptor.models[0].variant == "Q4_K_M"
    assert descriptor.models[0].context_tokens == 8192
    assert descriptor.isolation.kind == "none"


@pytest.mark.asyncio
async def test_missing_model_metadata_does_not_crash_or_fabricate_digest(monkeypatch):
    responses = {
        "version": RuntimeError("optional endpoint unavailable"),
        "tags": _Response({"models": [{"name": "some-other-model", "digest": "c" * 64}]}),
    }
    monkeypatch.setattr(
        node.httpx,
        "AsyncClient",
        lambda **_kwargs: _MetadataClient(responses),
    )
    monkeypatch.setattr(
        node,
        "_detected_hardware_descriptor",
        lambda: HardwareDescriptorV1(architecture=None, gpus=None),
    )

    descriptor = await node.build_stock_capability_descriptor(
        model="worker-model:latest",
        context_tokens=None,
        ollama_url="http://localhost:11434",
    )

    assert descriptor.executor.version is None
    assert descriptor.models[0].digest is None
    assert descriptor.models[0].variant is None
    assert descriptor.models[0].context_tokens is None
    assert descriptor.hardware.gpus is None


@pytest.mark.asyncio
async def test_operator_overrides_are_strict_and_layer_over_detection(monkeypatch):
    async def no_metadata(*_args, **_kwargs):
        return None, None, None

    monkeypatch.setattr(node, "_detect_ollama_metadata", no_metadata)
    monkeypatch.setattr(
        node,
        "_detected_hardware_descriptor",
        lambda: HardwareDescriptorV1(
            architecture="arm64",
            logical_cpu_count=4,
            total_memory_bytes=8 * 1024**3,
            gpus=None,
        ),
    )

    descriptor = await node.build_stock_capability_descriptor(
        model="worker-model:latest",
        context_tokens=4096,
        ollama_url="http://localhost:11434",
        config_overrides={
            "hardware": {"logical_cpu_count": 12, "gpus": []},
            "features": ["large-context", "code"],
            "model_context_tokens": 32768,
            "max_context_tokens": 16384,
        },
    )

    assert descriptor.hardware.architecture == "arm64"
    assert descriptor.hardware.logical_cpu_count == 12
    assert descriptor.hardware.total_memory_bytes == 8 * 1024**3
    assert descriptor.hardware.gpus == []
    assert descriptor.features == ["code", "large-context"]
    assert descriptor.models[0].context_tokens == 32768
    assert descriptor.limits.max_context_tokens == 16384
    assert descriptor.models[0].digest is None
    assert descriptor.limits.max_concurrent_execution_units == 1
    assert descriptor.limits.max_output_bytes == node._MAX_WORKER_OUTPUT_BYTES

    with pytest.raises(node.WorkerCapabilityConfigurationError, match="extra_forbidden"):
        await node.build_stock_capability_descriptor(
            model="worker-model:latest",
            context_tokens=4096,
            ollama_url="http://localhost:11434",
            config_overrides={"hardware": {"serial_number": "must-not-be-collected"}},
        )


@pytest.mark.asyncio
async def test_post_override_descriptor_validation_has_configuration_error(
    monkeypatch,
):
    async def no_metadata(*_args, **_kwargs):
        return None, None, None

    with pytest.raises(node.ValidationError) as invalid_descriptor:
        HardwareDescriptorV1(logical_cpu_count=0)
    descriptor_error = invalid_descriptor.value

    def reject_descriptor(**_kwargs):
        raise descriptor_error

    monkeypatch.setattr(node, "_detect_ollama_metadata", no_metadata)
    monkeypatch.setattr(
        node,
        "_detected_hardware_descriptor",
        lambda: HardwareDescriptorV1(architecture="x86_64"),
    )
    monkeypatch.setattr(node, "NodeCapabilityDescriptorV1", reject_descriptor)

    with pytest.raises(
        node.WorkerCapabilityConfigurationError,
        match="descriptor is invalid after overrides",
    ):
        await node.build_stock_capability_descriptor(
            model="worker-model:latest",
            context_tokens=4096,
            ollama_url="http://localhost:11434",
            config_overrides={"features": ["code"]},
        )


@pytest.mark.asyncio
async def test_cli_override_file_wins_per_field_and_is_bounded(tmp_path, monkeypatch):
    async def no_metadata(*_args, **_kwargs):
        return None, None, None

    monkeypatch.setattr(node, "_detect_ollama_metadata", no_metadata)
    monkeypatch.setattr(
        node,
        "_detected_hardware_descriptor",
        lambda: HardwareDescriptorV1(architecture="x86_64", logical_cpu_count=2),
    )
    override_file = tmp_path / "claims.json"
    override_file.write_text(
        json.dumps({"hardware": {"logical_cpu_count": 16}, "features": ["code"]}),
        encoding="utf-8",
    )

    descriptor = await node.build_stock_capability_descriptor(
        model="worker-model:latest",
        context_tokens=8192,
        ollama_url="http://localhost:11434",
        config_overrides={"hardware": {"total_memory_bytes": 32 * 1024**3}},
        override_file=override_file,
    )

    assert descriptor.hardware.logical_cpu_count == 16
    assert descriptor.hardware.total_memory_bytes == 32 * 1024**3
    assert descriptor.features == ["code"]

    oversized = tmp_path / "oversized.json"
    oversized.write_text(" " * (node._MAX_CAPABILITY_OVERRIDE_BYTES + 1), encoding="utf-8")
    with pytest.raises(node.WorkerCapabilityConfigurationError, match="exceeds"):
        node._read_capability_override_file(oversized)


@pytest.mark.asyncio
async def test_registration_sends_typed_descriptor_and_preserves_legacy_tags(monkeypatch):
    descriptor = _descriptor()
    captured = {}

    class CoordinatorClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return _Response({"ok": True})

    def client_factory(**kwargs):
        captured["client_options"] = kwargs
        return CoordinatorClient()

    monkeypatch.setattr(node.httpx, "AsyncClient", client_factory)

    await node.register(
        "https://coordinator.example",
        "worker",
        capabilities=["legacy-code"],
        capability_descriptor=descriptor,
        model="worker-model:latest",
    )

    payload = captured["json"]
    assert payload["model"] == "worker-model:latest"
    assert payload["capabilities"] == ["legacy-code"]
    assert payload["capability_descriptor"] == descriptor.model_dump(mode="json")
    assert payload["cpu_count"] == 8
    assert payload["ram_gb"] == 16.0
    assert captured["client_options"]["trust_env"] is False
