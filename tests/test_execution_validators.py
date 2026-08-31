"""End-to-end validator process containment and fail-closed integration."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import execution.validator_process as validator_process_module
from execution.contracts import ExecutionRequestV1
from execution.validator_process import (
    ValidatorProcessExecutor,
    ValidatorProcessSettings,
)
from execution.validators import ValidatorRegistry, check_code_files_isolated_async


class _PopenRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.processes: list[subprocess.Popen[bytes]] = []
        self.spawned = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, **kwargs):
        process = subprocess.Popen(**kwargs)
        with self._lock:
            self.calls.append(dict(kwargs))
            self.processes.append(process)
            self.spawned.set()
        return process


def _settings(
    *,
    mode: str = "auto",
    timeout: int = 10,
    request_max_bytes: int = 2 * 1024 * 1024,
    response_max_bytes: int = 32 * 1024,
    memory_mb: int = 256,
) -> ValidatorProcessSettings:
    return ValidatorProcessSettings(
        execution_mode=mode,
        timeout_seconds=timeout,
        memory_mb=memory_mb,
        request_max_bytes=request_max_bytes,
        response_max_bytes=response_max_bytes,
    )


def _write_runner(tmp_path: Path, source: str, name: str = "runner.py") -> Path:
    runner = tmp_path / name
    runner.write_text(source.strip() + "\n", encoding="utf-8")
    return runner


def _script_executor(
    script: Path,
    *,
    settings: ValidatorProcessSettings | None = None,
    recorder: _PopenRecorder | None = None,
    work_directories: list[Path] | None = None,
) -> ValidatorProcessExecutor:
    def command(work_directory: Path):
        if work_directories is not None:
            work_directories.append(work_directory)
        return (sys.executable, "-I", str(script))

    return ValidatorProcessExecutor(
        settings or _settings(response_max_bytes=1024),
        command_factory=command,
        popen_factory=recorder or subprocess.Popen,
    )


def _execute_output_validator(
    executor: ValidatorProcessExecutor,
    *,
    output: str = "{}",
    deadline_monotonic: float | None = None,
    cancel_event: threading.Event | None = None,
):
    return executor.execute(
        validator_name="structured_json",
        validator_version="2",
        output=output,
        files=[],
        contract=None,
        artifact_root=None,
        deadline_monotonic=deadline_monotonic,
        cancel_event=cancel_event,
    )


def _execute_file_validator(
    executor: ValidatorProcessExecutor,
    artifact: Path,
    *,
    deadline_monotonic: float | None = None,
    cancel_event: threading.Event | None = None,
):
    return executor.execute(
        validator_name="code_parse",
        validator_version="2",
        output="",
        files=[artifact],
        contract=None,
        artifact_root=artifact.parent,
        deadline_monotonic=deadline_monotonic,
        cancel_event=cancel_event,
    )


_VALID_RESPONSE_RUNNER = r"""
import json
import sys

request = json.load(sys.stdin)
json.dump(
    {
        "protocol_version": "1",
        "validator_name": request["validator_name"],
        "validator_version": request["validator_version"],
        "ok": True,
        "score": 1.0,
        "detail": {"json_type": "object"},
        "failure_reason": None,
    },
    sys.stdout,
)
"""


_SLEEP_RUNNER = r"""
import sys
import time

sys.stdin.buffer.read()
time.sleep(60)
"""


def _by_name(evidence):
    return {item.validator_name: item for item in evidence}


def test_code_parse_runs_in_child_and_staging_is_removed(tmp_path):
    artifact = tmp_path / "main.py"
    artifact.write_text("VALUE = 1\n", encoding="utf-8")
    recorder = _PopenRecorder()
    work_directories: list[Path] = []

    def command(work_directory: Path):
        work_directories.append(work_directory)
        return ValidatorProcessExecutor._default_command(work_directory)

    executor = ValidatorProcessExecutor(
        _settings(),
        command_factory=command,
        popen_factory=recorder,
    )
    registry = ValidatorRegistry.default(process_executor=executor)
    request = ExecutionRequestV1(
        task="parse one file",
        output_contract={"kind": "code", "artifact_count": 1},
    )

    evidence = registry.validate(
        request,
        "one file",
        [str(artifact)],
        artifact_root=tmp_path,
    )

    parsed = _by_name(evidence)["code_parse"]
    assert parsed.status == "passed"
    assert parsed.evidence["execution_mode"] == "subprocess_isolated"
    assert parsed.evidence["runner_protocol_version"] == "1"
    assert parsed.proves_behavioral_correctness is False
    assert len(recorder.processes) == 1
    assert recorder.processes[0].pid != os.getpid()
    assert recorder.processes[0].returncode == 0
    assert all(not directory.exists() for directory in work_directories)


def test_relative_canonical_artifact_paths_are_normalized_before_staging(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    authoritative_root = Path("execution_artifacts") / ("a" * 32)
    validation_root = authoritative_root / "candidate_1" / "code"
    validation_root.mkdir(parents=True)
    artifact = validation_root / "main.py"
    artifact.write_text("VALUE = 1\n", encoding="utf-8")

    outcome = ValidatorProcessExecutor(_settings()).execute(
        validator_name="code_parse",
        validator_version="2",
        output="",
        files=[artifact],
        contract=None,
        artifact_root=validation_root,
        authoritative_artifact_root=authoritative_root.resolve(),
    )

    assert outcome.completed is True
    assert outcome.ok is True


def test_json_schema_and_structured_json_run_in_children():
    recorder = _PopenRecorder()
    registry = ValidatorRegistry.default(
        process_executor=ValidatorProcessExecutor(
            _settings(),
            popen_factory=recorder,
        )
    )
    request = ExecutionRequestV1(
        task="return data",
        output_contract={
            "kind": "structured_json",
            "json_schema": {
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
            },
        },
    )

    evidence = registry.validate(request, '{"answer": 42}', [])
    by_name = _by_name(evidence)

    assert by_name["structured_json"].status == "passed"
    assert by_name["json_schema"].status == "passed"
    assert by_name["structured_json"].evidence["execution_mode"] == "subprocess_isolated"
    assert by_name["json_schema"].evidence["execution_mode"] == "subprocess_isolated"
    assert len(recorder.processes) == 2
    assert all(process.returncode == 0 for process in recorder.processes)


def test_inline_safe_validator_preserves_behavior_without_spawning():
    calls = 0

    def unexpected_spawn(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("inline validator attempted a process launch")

    registry = ValidatorRegistry.default(
        process_executor=ValidatorProcessExecutor(
            _settings(),
            popen_factory=unexpected_spawn,
        )
    )
    evidence = registry.validate(ExecutionRequestV1(task="write text"), "complete", [])

    nonempty = _by_name(evidence)["nonempty"]
    assert nonempty.status == "passed"
    assert nonempty.evidence["execution_mode"] == "inline_trusted"
    assert calls == 0


def test_registry_rejects_arbitrary_validator_plugins():
    class ArbitraryValidator:
        name = "arbitrary_plugin"
        version = "1"
        execution_policy = "inline_trusted"
        assurance_level = "deterministic"
        proves_behavioral_correctness = True

        def validate(self, _value):
            return True, 1.0, {}, None

    with pytest.raises(ValueError, match="closed built-in"):
        ValidatorRegistry().register(ArbitraryValidator())


def test_forced_subprocess_mode_runs_compatible_inline_validator_in_child():
    recorder = _PopenRecorder()
    registry = ValidatorRegistry.default(
        process_executor=ValidatorProcessExecutor(
            _settings(mode="subprocess"),
            popen_factory=recorder,
        )
    )

    evidence = registry.validate(ExecutionRequestV1(task="write text"), "complete", [])

    nonempty = _by_name(evidence)["nonempty"]
    assert nonempty.status == "passed"
    assert nonempty.evidence["execution_mode"] == "subprocess_isolated"
    assert len(recorder.processes) == 1


@pytest.mark.parametrize("contract_kind", ["single_artifact", "file_manifest"])
def test_forced_subprocess_mode_supports_minimal_file_contract_projections(
    tmp_path,
    contract_kind,
):
    artifact = tmp_path / "result.txt"
    artifact.write_text("complete", encoding="utf-8")
    if contract_kind == "file_manifest":
        contract = {
            "kind": "file_manifest",
            "required_files": ["result.txt"],
        }
    else:
        contract = {
            "kind": "single_artifact",
            "format": "txt",
            "artifact_count": 1,
        }
    registry = ValidatorRegistry.default(
        process_settings=_settings(mode="subprocess")
    )
    request = ExecutionRequestV1(task="return one file", output_contract=contract)

    evidence = registry.validate(
        request,
        "complete",
        [str(artifact)],
        artifact_root=tmp_path,
    )

    by_name = _by_name(evidence)
    assert registry.accepted(evidence) is True
    assert by_name["artifact_contract"].status == "passed"
    assert by_name["artifact_contract"].evidence["execution_mode"] == "subprocess_isolated"
    if contract_kind == "file_manifest":
        assert by_name["file_manifest"].status == "passed"


def test_explicit_inline_mode_is_visible_as_weaker_compatibility(tmp_path):
    artifact = tmp_path / "main.py"
    artifact.write_text("VALUE = 1\n", encoding="utf-8")
    registry = ValidatorRegistry.default(process_settings=_settings(mode="inline"))
    request = ExecutionRequestV1(
        task="parse one file",
        output_contract={"kind": "code", "artifact_count": 1},
    )

    parsed = _by_name(
        registry.validate(
            request,
            "one file",
            [str(artifact)],
            artifact_root=tmp_path,
        )
    )["code_parse"]

    assert parsed.status == "passed"
    assert parsed.evidence["execution_mode"] == "inline_compatibility"
    assert parsed.evidence["containment_level"] == "coordinator_process"


def test_python_and_html_parsing_never_executes_generated_content(tmp_path):
    side_effect_marker = tmp_path / "SHOULD_NOT_EXIST"
    imported_marker = tmp_path / "IMPORTED_SHOULD_NOT_EXIST"
    shell_marker = tmp_path / "SHELL_SHOULD_NOT_EXIST"
    shell_command = f'echo executed > "{shell_marker}"'
    files = {
        "side_effect.py": (
            "from pathlib import Path\n"
            f"Path({str(side_effect_marker)!r}).write_text('executed')\n"
            "raise RuntimeError('executed')\n"
        ),
        "imports.py": "import side_effect_module\n",
        "side_effect_module.py": (
            "from pathlib import Path\n"
            f"Path({str(imported_marker)!r}).write_text('imported')\n"
        ),
        "shell.py": f"import os\nos.system({shell_command!r})\n",
    }
    paths = []
    for name, source in files.items():
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        paths.append(str(path))

    registry = ValidatorRegistry.default()
    python_request = ExecutionRequestV1(
        task="parse generated Python",
        output_contract={"kind": "code", "artifact_count": len(paths)},
    )
    python_evidence = registry.validate(
        python_request,
        "generated files",
        paths,
        artifact_root=tmp_path,
    )

    html = tmp_path / "index.html"
    html.write_text(
        "<!doctype html><html><body><script>"
        "throw new Error('generated script must not run');"
        "</script></body></html>",
        encoding="utf-8",
    )
    html_request = ExecutionRequestV1(
        task="parse generated HTML",
        output_contract={"kind": "single_artifact", "format": "html"},
    )
    html_evidence = registry.validate(
        html_request,
        "generated HTML",
        [str(html)],
        artifact_root=tmp_path,
    )

    assert _by_name(python_evidence)["code_parse"].status == "passed"
    assert _by_name(html_evidence)["code_parse"].status == "passed"
    assert not side_effect_marker.exists()
    assert not imported_marker.exists()
    assert not shell_marker.exists()


def test_timeout_terminates_reaps_and_removes_stage(tmp_path):
    script = _write_runner(tmp_path, _SLEEP_RUNNER)
    artifact = tmp_path / "main.py"
    artifact.write_text("VALUE = 1\n", encoding="utf-8")
    recorder = _PopenRecorder()
    work_directories: list[Path] = []
    executor = _script_executor(
        script,
        recorder=recorder,
        work_directories=work_directories,
    )

    outcome = _execute_file_validator(
        executor,
        artifact,
        deadline_monotonic=time.monotonic() + 0.2,
    )

    assert outcome.completed is False
    assert outcome.failure_reason == "validator_timeout"
    assert outcome.termination_reason == "validator_timeout"
    assert len(recorder.processes) == 1
    assert recorder.processes[0].poll() is not None
    assert all(not directory.exists() for directory in work_directories)


def test_child_crash_is_bounded_reaped_and_cleans_stage(tmp_path):
    script = _write_runner(tmp_path, "import sys\nsys.exit(7)")
    artifact = tmp_path / "main.py"
    artifact.write_text("VALUE = 1\n", encoding="utf-8")
    recorder = _PopenRecorder()
    work_directories: list[Path] = []
    outcome = _execute_file_validator(
        _script_executor(
            script,
            recorder=recorder,
            work_directories=work_directories,
        ),
        artifact,
    )

    assert outcome.completed is False
    assert outcome.failure_reason == "validator_crash"
    assert outcome.detail == {}
    assert recorder.processes[0].poll() is not None
    assert all(not directory.exists() for directory in work_directories)


def test_workspace_cleanup_failure_is_counted_and_fails_closed(tmp_path, monkeypatch):
    script = _write_runner(tmp_path, _VALID_RESPONSE_RUNNER)
    work_directories: list[Path] = []
    executor = _script_executor(script, work_directories=work_directories)
    before = executor.diagnostics()["process_local_counters"][
        "staging_cleanup_failures"
    ]
    original_rmtree = validator_process_module.shutil.rmtree
    failed = False

    def fail_first_cleanup(path):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("synthetic cleanup failure with sensitive text")
        return original_rmtree(path)

    monkeypatch.setattr(validator_process_module.shutil, "rmtree", fail_first_cleanup)
    try:
        outcome = _execute_output_validator(executor)
    finally:
        for work_directory in work_directories:
            if work_directory.exists():
                original_rmtree(work_directory)

    after = executor.diagnostics()["process_local_counters"][
        "staging_cleanup_failures"
    ]
    assert outcome.completed is False
    assert outcome.failure_reason == "validator_stage_cleanup_failed"
    assert outcome.detail == {}
    assert after == before + 1


def test_cancellation_after_staging_prevents_process_spawn(tmp_path, monkeypatch):
    artifact = tmp_path / "main.py"
    artifact.write_text("VALUE = 1\n", encoding="utf-8")
    script = _write_runner(tmp_path, _VALID_RESPONSE_RUNNER)
    recorder = _PopenRecorder()
    cancel_event = threading.Event()
    original_stage = validator_process_module.stage_validator_files

    def stage_then_cancel(**kwargs):
        staged = original_stage(**kwargs)
        cancel_event.set()
        return staged

    monkeypatch.setattr(
        validator_process_module,
        "stage_validator_files",
        stage_then_cancel,
    )
    outcome = _execute_file_validator(
        _script_executor(script, recorder=recorder),
        artifact,
        cancel_event=cancel_event,
    )

    assert outcome.completed is False
    assert outcome.failure_reason == "validator_cancelled"
    assert recorder.processes == []


def test_deadline_expiry_after_staging_prevents_process_spawn(tmp_path, monkeypatch):
    artifact = tmp_path / "main.py"
    artifact.write_text("VALUE = 1\n", encoding="utf-8")
    script = _write_runner(tmp_path, _VALID_RESPONSE_RUNNER)
    recorder = _PopenRecorder()
    clock = [100.0]
    original_stage = validator_process_module.stage_validator_files

    def stage_then_expire(**kwargs):
        staged = original_stage(**kwargs)
        clock[0] = 106.0
        return staged

    monkeypatch.setattr(validator_process_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        validator_process_module,
        "stage_validator_files",
        stage_then_expire,
    )
    outcome = _execute_file_validator(
        _script_executor(script, recorder=recorder),
        artifact,
        deadline_monotonic=105.0,
    )

    assert outcome.completed is False
    assert outcome.failure_reason == "validator_timeout"
    assert recorder.processes == []


def test_unconfirmed_cleanup_fails_closed_and_counts_once(tmp_path, monkeypatch):
    script = _write_runner(tmp_path, _VALID_RESPONSE_RUNNER)
    executor = _script_executor(script)
    before = executor.diagnostics()["process_local_counters"][
        "process_cleanup_failures"
    ]
    monkeypatch.setattr(
        validator_process_module,
        "_terminate_process_tree",
        lambda _process: False,
    )

    outcome = _execute_output_validator(executor)
    after = executor.diagnostics()["process_local_counters"][
        "process_cleanup_failures"
    ]

    assert outcome.completed is False
    assert outcome.failure_reason == "validator_process_cleanup_failed"
    assert after == before + 1


def test_unconfirmed_cleanup_cannot_block_on_nonreading_stdin(tmp_path, monkeypatch):
    script = _write_runner(tmp_path, "import time\ntime.sleep(60)")
    recorder = _PopenRecorder()
    executor = _script_executor(
        script,
        settings=_settings(
            request_max_bytes=16 * 1024 * 1024,
            response_max_bytes=1024,
        ),
        recorder=recorder,
    )
    original_terminate = validator_process_module._terminate_process_tree
    monkeypatch.setattr(
        validator_process_module,
        "_terminate_process_tree",
        lambda _process: False,
    )
    completed = threading.Event()
    outcomes = []

    def run_validation():
        try:
            outcomes.append(
                _execute_output_validator(
                    executor,
                    output="x" * (8 * 1024 * 1024),
                    deadline_monotonic=time.monotonic() + 0.2,
                )
            )
        finally:
            completed.set()

    owner = threading.Thread(target=run_validation, daemon=True)
    owner.start()
    assert recorder.spawned.wait(timeout=5)
    try:
        assert completed.wait(timeout=2)
    finally:
        monkeypatch.setattr(
            validator_process_module,
            "_terminate_process_tree",
            original_terminate,
        )
        for process in recorder.processes:
            original_terminate(process)
        owner.join(timeout=5)

    assert len(outcomes) == 1
    assert outcomes[0].completed is False
    assert outcomes[0].failure_reason in {
        "validator_timeout",
        "validator_stage_cleanup_failed",
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group coverage")
def test_runner_descendant_is_removed_even_after_direct_child_exits(tmp_path):
    pid_file = tmp_path / "descendant.pid"
    script = _write_runner(
        tmp_path,
        f"""
import json
import subprocess
import sys
request = json.load(sys.stdin)
descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
)
with open({str(pid_file)!r}, "w", encoding="utf-8") as handle:
    handle.write(str(descendant.pid))
json.dump(
    {{
        "protocol_version": "1",
        "validator_name": request["validator_name"],
        "validator_version": request["validator_version"],
        "ok": True,
        "score": 1.0,
        "detail": {{"json_type": "object"}},
        "failure_reason": None,
    }},
    sys.stdout,
)
""",
    )

    outcome = _execute_output_validator(_script_executor(script))
    descendant_pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        with pytest.raises(ProcessLookupError):
            os.kill(descendant_pid, 0)
    finally:
        try:
            os.kill(descendant_pid, 9)
        except ProcessLookupError:
            pass

    assert outcome.completed is True


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree coverage")
def test_windows_job_blocks_or_reaps_runner_descendant(tmp_path):
    pid_file = tmp_path / "descendant.pid"
    script = _write_runner(
        tmp_path,
        f"""
import json
from pathlib import Path
import subprocess
import sys
request = json.load(sys.stdin)
spawned = False
try:
    descendant = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    Path({str(pid_file)!r}).write_text(str(descendant.pid), encoding="utf-8")
    spawned = True
except OSError:
    pass
json.dump(
    {{
        "protocol_version": "1",
        "validator_name": request["validator_name"],
        "validator_version": request["validator_version"],
        "ok": True,
        "score": 1.0,
        "detail": {{"descendant_spawned": spawned}},
        "failure_reason": None,
    }},
    sys.stdout,
)
""",
    )

    outcome = _execute_output_validator(_script_executor(script))

    assert outcome.completed is True
    if pid_file.exists():
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x00100000, False, int(pid_file.read_text()))
        if handle:
            try:
                assert kernel32.WaitForSingleObject(handle, 0) != 258
            finally:
                kernel32.CloseHandle(handle)


def test_spawn_failure_is_error_and_never_falls_back_inline():
    def fail_spawn(**_kwargs):
        raise OSError("synthetic spawn failure with sensitive text")

    registry = ValidatorRegistry.default(
        process_executor=ValidatorProcessExecutor(
            _settings(),
            popen_factory=fail_spawn,
        )
    )
    request = ExecutionRequestV1(
        task="validate JSON",
        verification={"validators": [{"name": "structured_json"}]},
    )

    evidence = registry.validate(request, "{}", [])
    isolated = _by_name(evidence)["structured_json"]

    assert isolated.status == "error"
    assert isolated.failure_reason == "validator_spawn_failed"
    assert isolated.evidence["execution_mode"] == "subprocess_isolated"
    assert registry.accepted(evidence) is False


@pytest.mark.parametrize(
    ("stream", "expected_reason"),
    [
        ("stdout", "validator_response_oversized"),
        ("stderr", "validator_stderr_oversized"),
    ],
)
def test_excessive_child_output_is_terminated_without_content(
    tmp_path,
    caplog,
    stream,
    expected_reason,
):
    secret = "RAW_CHILD_OUTPUT_MUST_NOT_ESCAPE"
    target = "sys.stdout.buffer" if stream == "stdout" else "sys.stderr.buffer"
    script = _write_runner(
        tmp_path,
        f"""
import sys
import time
sys.stdin.buffer.read()
{target}.write(({secret!r}.encode() * 8192))
{target}.flush()
time.sleep(60)
""",
        name=f"{stream}_runner.py",
    )
    recorder = _PopenRecorder()

    outcome = _execute_output_validator(
        _script_executor(script, recorder=recorder),
        deadline_monotonic=time.monotonic() + 5,
    )

    assert outcome.completed is False
    assert outcome.failure_reason == expected_reason
    assert secret not in json.dumps(outcome.__dict__)
    assert secret not in caplog.text
    assert recorder.processes[0].poll() is not None


def test_builtin_runner_returns_stable_error_when_result_exceeds_response_cap(tmp_path):
    artifacts: list[Path] = []
    for index in range(10):
        artifact = tmp_path / (("long-validator-file-name-" * 4) + f"{index}.py")
        artifact.write_text("def broken(:\n", encoding="utf-8")
        artifacts.append(artifact)
    executor = ValidatorProcessExecutor(_settings(response_max_bytes=1024))
    before = executor.diagnostics()["process_local_counters"]["oversized_responses"]

    outcome = executor.execute(
        validator_name="code_parse",
        validator_version="2",
        output="",
        files=artifacts,
        contract=None,
        artifact_root=tmp_path,
    )

    after = executor.diagnostics()["process_local_counters"]["oversized_responses"]
    assert outcome.completed is False
    assert outcome.failure_reason == "validator_response_oversized"
    assert outcome.detail == {}
    assert after == before + 1


def test_malformed_or_unknown_child_response_is_protocol_error(tmp_path):
    script = _write_runner(
        tmp_path,
        r"""
import json
import sys
request = json.load(sys.stdin)
json.dump(
    {
        "protocol_version": "1",
        "validator_name": request["validator_name"],
        "validator_version": request["validator_version"],
        "ok": True,
        "score": 1.0,
        "detail": {},
        "failure_reason": None,
        "unknown": "rejected",
    },
    sys.stdout,
)
""",
    )

    outcome = _execute_output_validator(_script_executor(script))

    assert outcome.completed is False
    assert outcome.failure_reason == "validator_malformed_response"
    assert outcome.detail == {}


def test_oversized_request_is_rejected_before_spawn(tmp_path):
    script = _write_runner(tmp_path, _VALID_RESPONSE_RUNNER)
    recorder = _PopenRecorder()
    executor = _script_executor(
        script,
        settings=_settings(request_max_bytes=16 * 1024, response_max_bytes=1024),
        recorder=recorder,
    )

    outcome = _execute_output_validator(executor, output="x" * (20 * 1024))

    assert outcome.completed is False
    assert outcome.failure_reason == "validator_request_oversized"
    assert recorder.processes == []


def test_sensitive_environment_and_payload_are_absent_from_launch_and_evidence(
    tmp_path,
    monkeypatch,
    caplog,
):
    environment_secret = "ENVIRONMENT_SECRET_MUST_NOT_ESCAPE"
    payload_secret = "PAYLOAD_SECRET_MUST_NOT_ESCAPE"
    monkeypatch.setenv("MYCELIUM_VALIDATOR_TEST_SECRET", environment_secret)
    recorder = _PopenRecorder()
    registry = ValidatorRegistry.default(
        process_executor=ValidatorProcessExecutor(
            _settings(),
            popen_factory=recorder,
        )
    )
    request = ExecutionRequestV1(
        task=f"do not expose {payload_secret}",
        output_contract={
            "kind": "structured_json",
            "json_schema": {
                "type": "object",
                "properties": {"payload": {"const": payload_secret}},
                "required": ["payload"],
            },
        },
    )

    evidence = registry.validate(
        request,
        json.dumps({"payload": payload_secret}),
        [],
    )

    assert registry.accepted(evidence) is True
    rendered_evidence = json.dumps(
        [item.model_dump(mode="json") for item in evidence],
        sort_keys=True,
    )
    assert environment_secret not in rendered_evidence
    assert payload_secret not in rendered_evidence
    assert environment_secret not in caplog.text
    assert payload_secret not in caplog.text
    for launch in recorder.calls:
        arguments = json.dumps([str(value) for value in launch["args"]])
        environment = launch["env"]
        assert isinstance(environment, dict)
        assert environment_secret not in arguments
        assert payload_secret not in arguments
        assert "MYCELIUM_VALIDATOR_TEST_SECRET" not in environment
        assert environment_secret not in json.dumps(environment)
        assert payload_secret not in json.dumps(environment)
        assert launch["shell"] is False
        assert launch["close_fds"] is True


def test_child_observes_no_unallowlisted_environment_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCELIUM_VALIDATOR_TEST_SECRET", "present-in-parent")
    script = _write_runner(
        tmp_path,
        r"""
import json
import os
import sys
request = json.load(sys.stdin)
json.dump(
    {
        "protocol_version": "1",
        "validator_name": request["validator_name"],
        "validator_version": request["validator_version"],
        "ok": True,
        "score": 1.0,
        "detail": {"secret_present": "MYCELIUM_VALIDATOR_TEST_SECRET" in os.environ},
        "failure_reason": None,
    },
    sys.stdout,
)
""",
    )

    outcome = _execute_output_validator(_script_executor(script))

    assert outcome.completed is True
    assert outcome.detail == {"secret_present": False}


def test_runner_sanitizer_keeps_temporary_files_inside_owned_workspace(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MYCELIUM_VALIDATOR_TEST_SECRET", "present-in-parent")
    repository_root = Path(__file__).resolve().parents[1]
    script = _write_runner(
        tmp_path,
        f"""
import json
import os
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, {str(repository_root)!r})
from execution.validator_runner import _sanitize_environment
_sanitize_environment()
tempfile.tempdir = None
request = json.load(sys.stdin)
json.dump(
    {{
        "protocol_version": "1",
        "validator_name": request["validator_name"],
        "validator_version": request["validator_version"],
        "ok": True,
        "score": 1.0,
        "detail": {{
            "secret_present": "MYCELIUM_VALIDATOR_TEST_SECRET" in os.environ,
            "temporary_is_controlled": (
                Path(tempfile.gettempdir()).resolve() == Path.cwd().resolve().parent
            ),
        }},
        "failure_reason": None,
    }},
    sys.stdout,
)
""",
    )

    outcome = _execute_output_validator(_script_executor(script))

    assert outcome.completed is True
    assert outcome.detail == {
        "secret_present": False,
        "temporary_is_controlled": True,
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor inheritance coverage")
def test_unrelated_inheritable_file_descriptor_is_closed(tmp_path):
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("private", encoding="utf-8")
    descriptor = os.open(unrelated, os.O_RDONLY)
    try:
        os.set_inheritable(descriptor, True)
        expected = os.fstat(descriptor)
        script = _write_runner(
            tmp_path,
            f"""
import json
import os
import sys
request = json.load(sys.stdin)
try:
    status = os.fstat({descriptor})
    leaked = (status.st_dev, status.st_ino) == ({expected.st_dev}, {expected.st_ino})
except OSError:
    leaked = False
json.dump(
    {{
        "protocol_version": "1",
        "validator_name": request["validator_name"],
        "validator_version": request["validator_version"],
        "ok": True,
        "score": 1.0,
        "detail": {{"unrelated_descriptor_inherited": leaked}},
        "failure_reason": None,
    }},
    sys.stdout,
)
""",
        )

        outcome = _execute_output_validator(_script_executor(script))
    finally:
        os.close(descriptor)

    assert outcome.completed is True
    assert outcome.detail == {"unrelated_descriptor_inherited": False}


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux RLIMIT_AS containment coverage",
)
def test_excessive_memory_is_contained_by_posix_limit(tmp_path):
    repository_root = Path(__file__).resolve().parents[1]
    script = _write_runner(
        tmp_path,
        f"""
import json
import sys
from types import SimpleNamespace
sys.path.insert(0, {str(repository_root)!r})
from execution.validator_runner import _apply_request_posix_limits
request = json.load(sys.stdin)
limits = SimpleNamespace(**request["limits"])
_apply_request_posix_limits(limits)
allocation = bytearray(limits.memory_bytes + 64 * 1024 * 1024)
json.dump(
    {{
        "protocol_version": "1",
        "validator_name": request["validator_name"],
        "validator_version": request["validator_version"],
        "ok": True,
        "score": 1.0,
        "detail": {{"allocation_succeeded": len(allocation) > 0}},
        "failure_reason": None,
    }},
    sys.stdout,
)
""",
    )
    recorder = _PopenRecorder()

    outcome = _execute_output_validator(
        _script_executor(
            script,
            settings=_settings(memory_mb=128, response_max_bytes=1024),
            recorder=recorder,
        ),
        deadline_monotonic=time.monotonic() + 10,
    )

    assert outcome.completed is False
    assert outcome.failure_reason == "validator_crash"
    assert recorder.processes[0].poll() is not None


@pytest.mark.asyncio
async def test_async_cancellation_terminates_and_reaps_child(tmp_path):
    script = _write_runner(tmp_path, _SLEEP_RUNNER)
    recorder = _PopenRecorder()
    registry = ValidatorRegistry.default(
        process_executor=_script_executor(script, recorder=recorder)
    )
    request = ExecutionRequestV1(
        task="validate JSON",
        verification={"validators": [{"name": "structured_json"}]},
    )
    validation = asyncio.create_task(
        registry.validate_async(
            request,
            "{}",
            [],
            deadline_monotonic=time.monotonic() + 10,
        )
    )
    assert await asyncio.to_thread(recorder.spawned.wait, 5)

    validation.cancel()
    await asyncio.sleep(0)
    validation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await validation

    assert len(recorder.processes) == 1
    assert recorder.processes[0].poll() is not None


@pytest.mark.asyncio
async def test_repair_precheck_cancellation_terminates_and_reaps_child(tmp_path):
    script = _write_runner(tmp_path, _SLEEP_RUNNER)
    recorder = _PopenRecorder()
    executor = _script_executor(script, recorder=recorder)
    artifact = tmp_path / "main.py"
    artifact.write_text("VALUE = 1\n", encoding="utf-8")
    validation = asyncio.create_task(
        check_code_files_isolated_async(
            [str(artifact)],
            artifact_root=tmp_path,
            process_executor=executor,
            deadline_monotonic=time.monotonic() + 10,
        )
    )
    assert await asyncio.to_thread(recorder.spawned.wait, 5)

    validation.cancel()
    await asyncio.sleep(0)
    validation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await validation

    assert len(recorder.processes) == 1
    assert recorder.processes[0].poll() is not None


def test_cancellation_race_records_remaining_required_floors_and_fails_closed(
    monkeypatch,
):
    registry = ValidatorRegistry.default()
    cancel_event = threading.Event()
    nonempty = registry._validators["nonempty"]
    original_validate = nonempty.validate

    def validate_then_cancel(value):
        result = original_validate(value)
        cancel_event.set()
        return result

    monkeypatch.setattr(nonempty, "validate", validate_then_cancel)
    request = ExecutionRequestV1(
        task="return code",
        output_contract={"kind": "code", "artifact_count": 1},
    )

    evidence = registry.validate(
        request,
        "nonempty output",
        [],
        cancel_event=cancel_event,
    )
    by_name = _by_name(evidence)

    assert by_name["nonempty"].status == "passed"
    for name in ("artifact_extraction", "artifact_contract", "code_parse"):
        assert by_name[name].status == "error"
        assert by_name[name].failure_reason == "validator_cancelled"
    assert registry.accepted(evidence) is False


@pytest.mark.parametrize(("required", "accepted"), [(True, False), (False, True)])
def test_timeout_follows_existing_required_optional_aggregation(
    tmp_path,
    required,
    accepted,
):
    script = _write_runner(tmp_path, _SLEEP_RUNNER)
    registry = ValidatorRegistry.default(
        process_executor=_script_executor(script)
    )
    request = ExecutionRequestV1(
        task="validate JSON",
        verification={
            "validators": [{"name": "structured_json", "required": required}]
        },
    )

    evidence = registry.validate(
        request,
        "{}",
        [],
        deadline_monotonic=time.monotonic() + 0.2,
    )

    isolated = _by_name(evidence)["structured_json"]
    assert isolated.status == "error"
    assert isolated.failure_reason == "validator_timeout"
    assert isolated.evidence["termination_reason"] == "validator_timeout"
    assert registry.accepted(evidence) is accepted


def test_one_validator_crash_does_not_poison_later_validation(tmp_path):
    crash = _write_runner(tmp_path, "import sys\nsys.exit(9)", "crash.py")
    success = _write_runner(tmp_path, _VALID_RESPONSE_RUNNER, "success.py")
    selected = iter((crash, success))

    def command(_work_directory: Path):
        return (sys.executable, "-I", str(next(selected)))

    registry = ValidatorRegistry.default(
        process_executor=ValidatorProcessExecutor(
            _settings(response_max_bytes=1024),
            command_factory=command,
        )
    )
    request = ExecutionRequestV1(
        task="validate JSON",
        verification={"validators": [{"name": "structured_json"}]},
    )

    first = registry.validate(request, "{}", [])
    second = registry.validate(request, "{}", [])

    assert _by_name(first)["structured_json"].failure_reason == "validator_crash"
    assert _by_name(second)["structured_json"].status == "passed"
    assert registry.accepted(first) is False
    assert registry.accepted(second) is True


def test_process_diagnostics_are_bounded_operational_health(tmp_path):
    script = _write_runner(tmp_path, _VALID_RESPONSE_RUNNER)
    executor = _script_executor(script)
    before = executor.diagnostics()["process_local_counters"]["subprocess_runs"]

    outcome = _execute_output_validator(executor)
    diagnostics = executor.diagnostics()
    counters = diagnostics["process_local_counters"]

    assert outcome.completed is True
    assert counters["subprocess_runs"] >= before + 1
    assert counters["successful_responses"] >= 1
    assert counters["reset_at"].endswith("+00:00")
    assert diagnostics["runner_protocol_version"] == "1"
    assert "not correctness" in diagnostics["statement"]
    assert "reputation" in diagnostics["statement"]
    assert "hostile-code sandbox" in diagnostics["statement"]
