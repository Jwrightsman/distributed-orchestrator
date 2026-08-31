"""Bounded parent-side execution for trusted built-in validators.

This is a process-containment boundary, not a hostile-code sandbox.  The
parent sends one strict request over stdin, retains only bounded stdout, drops
stderr, and owns all timeout, process-tree cleanup, identity, and assurance
metadata.
"""

from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

from execution.artifacts import ArtifactEntryV1
from execution.contracts import OutputContractV1
from execution.validator_protocol import (
    ValidatorContractProjectionV1,
    ValidatorProtocolError,
    ValidatorRunnerLimitsV1,
    ValidatorRunnerRequestV1,
    dump_runner_request_bytes,
    parse_runner_response_bytes,
)
from execution.validator_staging import (
    StagingLimits,
    ValidatorStagingAborted,
    ValidatorStagingCleanupError,
    ValidatorStagingError,
    stage_validator_files,
)


ValidatorExecutionMode = Literal["auto", "subprocess", "inline"]
ValidatorContainmentLevel = Literal[
    "posix_resource_limits",
    "posix_partial_resource_limits",
    "windows_process_tree_best_effort",
]


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True)
class ValidatorProcessSettings:
    execution_mode: ValidatorExecutionMode = "auto"
    timeout_seconds: int = 10
    memory_mb: int = 256
    request_max_bytes: int = 2 * 1024 * 1024
    response_max_bytes: int = 32 * 1024

    @classmethod
    def from_config(cls, values: Mapping[str, Any]) -> "ValidatorProcessSettings":
        return cls(
            execution_mode=str(values.get("validator_execution_mode", "auto")),
            timeout_seconds=int(values.get("validator_subprocess_timeout_seconds", 10)),
            memory_mb=int(values.get("validator_subprocess_memory_mb", 256)),
            request_max_bytes=int(
                values.get("validator_subprocess_request_max_bytes", 2 * 1024 * 1024)
            ),
            response_max_bytes=int(
                values.get("validator_subprocess_response_max_bytes", 32 * 1024)
            ),
        )


@dataclass(frozen=True)
class ValidatorProcessOutcome:
    completed: bool
    ok: bool
    score: float | None
    detail: dict[str, Any]
    failure_reason: str | None
    containment_level: ValidatorContainmentLevel
    termination_reason: str | None = None


@dataclass
class ValidatorProcessCounters:
    reset_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    subprocess_runs: int = 0
    successful_responses: int = 0
    validation_failures: int = 0
    timeouts: int = 0
    crashes: int = 0
    malformed_responses: int = 0
    oversized_requests: int = 0
    oversized_responses: int = 0
    spawn_failures: int = 0
    staging_failures: int = 0
    staging_cleanup_failures: int = 0
    cancellations: int = 0
    process_cleanup_failures: int = 0


class _CounterStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values = ValidatorProcessCounters()

    def increment(self, name: str) -> None:
        with self._lock:
            setattr(self._values, name, int(getattr(self._values, name)) + 1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(vars(self._values))


_PROCESS_COUNTERS = _CounterStore()


class _ValidatorWorkspaceCleanupError(RuntimeError):
    pass


class _ValidatorWorkspaceCreationError(RuntimeError):
    pass


@contextmanager
def _validator_workspace():
    """Create and reliably remove one runner-owned workspace."""

    try:
        workspace = Path(tempfile.mkdtemp(prefix="mycelium-validator-"))
    except OSError as exc:
        raise _ValidatorWorkspaceCreationError(
            "validator workspace creation failed"
        ) from exc
    try:
        try:
            os.chmod(workspace, 0o700)
        except OSError:
            pass
        yield workspace
    finally:
        try:
            shutil.rmtree(workspace)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _ValidatorWorkspaceCleanupError(
                "validator workspace cleanup failed"
            ) from exc


@dataclass
class _PipeCapture:
    stream: Any
    maximum: int
    retain: bool
    data: bytearray = field(default_factory=bytearray)
    seen_bytes: int = 0
    oversized: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)

    def run(self) -> None:
        try:
            while True:
                chunk = self.stream.read(8192)
                if not chunk:
                    return
                self.seen_bytes += len(chunk)
                if self.seen_bytes > self.maximum:
                    if self.retain and len(self.data) < self.maximum:
                        remaining = self.maximum - len(self.data)
                        self.data.extend(chunk[:remaining])
                    self.oversized.set()
                    # Stop draining.  The pipe then becomes an additional hard
                    # backpressure bound until the parent terminates the child.
                    return
                if self.retain:
                    self.data.extend(chunk)
        except (OSError, ValueError):
            return
        finally:
            self.finished.set()


def _containment_level() -> ValidatorContainmentLevel:
    if os.name == "nt":
        return "windows_process_tree_best_effort"
    if sys.platform.startswith("linux"):
        return "posix_resource_limits"
    return "posix_partial_resource_limits"


def _sanitized_environment(work_directory: Path) -> dict[str, str]:
    environment = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in ("SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value and "\x00" not in value and len(value) <= 1024:
            environment[name] = value
    temporary = str(work_directory)
    if os.name == "nt":
        environment.update({"TEMP": temporary, "TMP": temporary})
    else:
        environment.update({"TMPDIR": temporary, "LANG": "C.UTF-8"})
    return environment


def _contract_projection(
    contract: OutputContractV1 | None,
    validator_name: str,
) -> ValidatorContractProjectionV1 | None:
    if contract is None:
        return None
    if validator_name == "json_schema":
        return ValidatorContractProjectionV1(json_schema=contract.json_schema)
    if validator_name == "file_manifest":
        return ValidatorContractProjectionV1(
            required_files=list(contract.required_files)
        )
    if validator_name == "artifact_contract":
        return ValidatorContractProjectionV1(
            artifact_count=contract.artifact_count,
            format=contract.format,
        )
    return None


def _wait_process(process: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        process.wait(timeout=max(0.01, timeout))
        return True
    except subprocess.TimeoutExpired:
        return False


def _posix_process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _WindowsJob:
    """Minimal kill-on-close Windows Job Object owned by one validator run."""

    def __init__(self, handle: Any, kernel32: Any) -> None:
        self._handle = handle
        self._kernel32 = kernel32
        self._closed = False

    def assign(self, process: subprocess.Popen[bytes]) -> bool:
        raw_process_handle = getattr(process, "_handle", None)
        if raw_process_handle is None:
            return False
        return bool(
            self._kernel32.AssignProcessToJobObject(
                self._handle,
                raw_process_handle,
            )
        )

    def terminate(self) -> bool:
        if self._closed:
            return True
        return bool(self._kernel32.TerminateJobObject(self._handle, 1))

    def close(self) -> bool:
        if self._closed:
            return True
        closed = bool(self._kernel32.CloseHandle(self._handle))
        if closed:
            self._closed = True
        return closed


def _create_windows_job() -> _WindowsJob | None:
    if os.name != "nt":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
        )
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        job = _WindowsJob(handle, kernel32)
        limits = _ExtendedLimitInformation()
        # Kill every assigned process when the parent closes this handle, and
        # reject child-process creation by the trusted runner.  Assignment can
        # still be unavailable inside a restrictive outer job, so taskkill /T
        # remains the documented fallback.
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x00000008
        limits.BasicLimitInformation.ActiveProcessLimit = 1
        configured = kernel32.SetInformationJobObject(
            handle,
            9,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if not configured:
            job.close()
            return None
        return job
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> bool:
    """Terminate and reap one runner and its descendants where supported."""

    if os.name == "posix":
        # The direct child may have exited after creating a descendant in its
        # dedicated group.  Do not use the child's exit as proof that the
        # process group is empty.
        if _posix_process_group_exists(process.pid):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        _wait_process(process, 0.25)
        term_deadline = time.monotonic() + 0.25
        while _posix_process_group_exists(process.pid) and time.monotonic() < term_deadline:
            time.sleep(0.01)
        if _posix_process_group_exists(process.pid):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        kill_deadline = time.monotonic() + 2.0
        while _posix_process_group_exists(process.pid) and time.monotonic() < kill_deadline:
            time.sleep(0.01)
        return _wait_process(process, 0.1) and not _posix_process_group_exists(
            process.pid
        )

    windows_job = getattr(process, "_mycelium_validator_job", None)
    if isinstance(windows_job, _WindowsJob):
        windows_job.terminate()
        closed = windows_job.close()
        if closed:
            try:
                delattr(process, "_mycelium_validator_job")
            except AttributeError:
                pass
        return _wait_process(process, 2.0) and closed

    if process.poll() is not None:
        # Standard-library Windows controls cannot rediscover descendants once
        # their direct parent has exited.  The runner never creates children;
        # timeout/cancellation uses taskkill /T while the parent is still live.
        return _wait_process(process, 0.1)
    try:
        if os.name == "nt":
            system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
            taskkill = (
                Path(system_root) / "System32" / "taskkill.exe"
                if system_root
                else None
            )
            if taskkill is not None and taskkill.is_file():
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    timeout=2,
                    check=False,
                )
            else:
                process.kill()
        else:
            process.kill()
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass
    return _wait_process(process, 2.0)


class ValidatorProcessExecutor:
    """Launch one fixed built-in runner with bounded, secret-free I/O."""

    _FILE_VALIDATORS = frozenset(
        {"file_manifest", "code_parse", "artifact_extraction", "artifact_contract"}
    )
    _OUTPUT_VALIDATORS = frozenset({"nonempty", "structured_json", "json_schema"})
    _CONTRACT_VALIDATORS = frozenset({"json_schema", "file_manifest", "artifact_contract"})

    def __init__(
        self,
        settings: ValidatorProcessSettings | None = None,
        *,
        command_factory: Callable[[Path], Sequence[str]] | None = None,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        counter_store: _CounterStore | None = None,
    ) -> None:
        self.settings = settings or ValidatorProcessSettings()
        self._command_factory = command_factory or self._default_command
        self._popen_factory = popen_factory
        self._counters = counter_store or _PROCESS_COUNTERS

    @staticmethod
    def _default_command(_work_directory: Path) -> Sequence[str]:
        runner = Path(__file__).with_name("validator_runner.py").resolve()
        return (sys.executable, "-I", "-B", str(runner))

    def diagnostics(self) -> dict[str, Any]:
        return {
            "execution_mode": self.settings.execution_mode,
            "runner_protocol_version": "1",
            "containment_level": _containment_level(),
            "process_local_counters": self._counters.snapshot(),
            "statement": (
                "Operational validator process health; not correctness, reputation, "
                "or a hostile-code sandbox."
            ),
        }

    def record_cleanup_failure(self) -> None:
        """Account for an owner that could not confirm bounded async cleanup."""

        self._counters.increment("process_cleanup_failures")

    def _error(
        self,
        reason: str,
        *,
        counter: str | None = None,
    ) -> ValidatorProcessOutcome:
        if counter:
            self._counters.increment(counter)
        return ValidatorProcessOutcome(
            completed=False,
            ok=False,
            score=None,
            detail={},
            failure_reason=reason,
            containment_level=_containment_level(),
            termination_reason=reason,
        )

    def _staged_files(
        self,
        *,
        selected_files: Sequence[str | Path],
        artifact_root: str | Path | None,
        authoritative_artifact_root: str | Path | None,
        validated_entries: Sequence[ArtifactEntryV1] | None,
        staging_root: Path,
        abort_reason: Callable[[], str | None],
    ) -> tuple[str, ...]:
        if not selected_files:
            staging_root.mkdir(mode=0o700)
            try:
                os.chmod(staging_root, 0o700)
            except OSError:
                pass
            return ()
        if artifact_root is None:
            raise ValidatorStagingError("artifact root is required for staged validation")
        authority = Path(authoritative_artifact_root or artifact_root).resolve(strict=True)
        subtree = Path(artifact_root).resolve(strict=True)
        normalized_files: list[Path] = []
        for selected in selected_files:
            selected_path = Path(selected)
            if not selected_path.is_absolute():
                process_relative = selected_path.resolve(strict=False)
                if process_relative.is_relative_to(subtree):
                    selected_path = process_relative
            normalized_files.append(selected_path)
        return stage_validator_files(
            authoritative_root=authority,
            authoritative_subtree=subtree,
            selected_files=normalized_files,
            staging_root=staging_root,
            limits=StagingLimits(),
            validated_entries=validated_entries,
            abort_reason=abort_reason,
        )

    def execute(
        self,
        *,
        validator_name: str,
        validator_version: str,
        output: str,
        files: Sequence[str | Path],
        contract: OutputContractV1 | None,
        artifact_root: str | Path | None,
        authoritative_artifact_root: str | Path | None = None,
        validated_entries: Sequence[ArtifactEntryV1] | None = None,
        deadline_monotonic: float | None = None,
        cancel_event: CancellationSignal | None = None,
    ) -> ValidatorProcessOutcome:
        try:
            return self._execute_owned(
                validator_name=validator_name,
                validator_version=validator_version,
                output=output,
                files=files,
                contract=contract,
                artifact_root=artifact_root,
                authoritative_artifact_root=authoritative_artifact_root,
                validated_entries=validated_entries,
                deadline_monotonic=deadline_monotonic,
                cancel_event=cancel_event,
            )
        except _ValidatorWorkspaceCleanupError:
            return self._error(
                "validator_stage_cleanup_failed",
                counter="staging_cleanup_failures",
            )
        except _ValidatorWorkspaceCreationError:
            return self._error("validator_input_rejected", counter="staging_failures")

    def _execute_owned(
        self,
        *,
        validator_name: str,
        validator_version: str,
        output: str,
        files: Sequence[str | Path],
        contract: OutputContractV1 | None,
        artifact_root: str | Path | None,
        authoritative_artifact_root: str | Path | None = None,
        validated_entries: Sequence[ArtifactEntryV1] | None = None,
        deadline_monotonic: float | None = None,
        cancel_event: CancellationSignal | None = None,
    ) -> ValidatorProcessOutcome:
        if cancel_event is not None and cancel_event.is_set():
            return self._error("validator_cancelled", counter="cancellations")

        configured_deadline = time.monotonic() + self.settings.timeout_seconds
        effective_deadline = (
            configured_deadline
            if deadline_monotonic is None
            else min(configured_deadline, deadline_monotonic)
        )
        remaining = effective_deadline - time.monotonic()
        if remaining <= 0:
            return self._error("validator_timeout", counter="timeouts")

        def abort_reason() -> str | None:
            if cancel_event is not None and cancel_event.is_set():
                return "validator_cancelled"
            if time.monotonic() >= effective_deadline:
                return "validator_timeout"
            return None

        def aborted_outcome(reason: str) -> ValidatorProcessOutcome:
            counter = "cancellations" if reason == "validator_cancelled" else "timeouts"
            return self._error(reason, counter=counter)

        process: subprocess.Popen[bytes] | None = None
        stdout_capture: _PipeCapture | None = None
        stderr_capture: _PipeCapture | None = None
        with _validator_workspace() as work_directory:
            staging_root = work_directory / "input"
            try:
                staged_files = self._staged_files(
                    selected_files=files if validator_name in self._FILE_VALIDATORS else (),
                    artifact_root=artifact_root,
                    authoritative_artifact_root=authoritative_artifact_root,
                    validated_entries=validated_entries,
                    staging_root=staging_root,
                    abort_reason=abort_reason,
                )
            except ValidatorStagingAborted as exc:
                return aborted_outcome(exc.reason)
            except ValidatorStagingCleanupError:
                return self._error(
                    "validator_stage_cleanup_failed",
                    counter="staging_cleanup_failures",
                )
            except (OSError, ValidatorStagingError, ValueError, TypeError):
                return self._error("validator_input_rejected", counter="staging_failures")

            current_abort = abort_reason()
            if current_abort is not None:
                return aborted_outcome(current_abort)

            remaining = effective_deadline - time.monotonic()

            try:
                projection = (
                    _contract_projection(contract, validator_name)
                    if validator_name in self._CONTRACT_VALIDATORS
                    else None
                )
                request = ValidatorRunnerRequestV1(
                    validator_name=validator_name,
                    validator_version=validator_version,
                    output=output if validator_name in self._OUTPUT_VALIDATORS else None,
                    contract=projection,
                    staged_files=list(staged_files),
                    limits=ValidatorRunnerLimitsV1(
                        wall_time_seconds=max(0.1, min(float(remaining), 120.0)),
                        cpu_time_seconds=max(1, min(math.ceil(remaining), 120)),
                        memory_bytes=self.settings.memory_mb * 1024 * 1024,
                        response_max_bytes=self.settings.response_max_bytes,
                    ),
                )
            except (TypeError, ValueError):
                return self._error("validator_request_invalid")
            try:
                request_bytes = dump_runner_request_bytes(
                    request,
                    max_bytes=self.settings.request_max_bytes,
                )
            except ValidatorProtocolError as exc:
                if exc.code == "validator_protocol_output_oversized":
                    return self._error(
                        "validator_request_oversized",
                        counter="oversized_requests",
                    )
                return self._error("validator_request_invalid")

            current_abort = abort_reason()
            if current_abort is not None:
                return aborted_outcome(current_abort)

            try:
                command = tuple(self._command_factory(work_directory))
            except Exception:
                return self._error("validator_spawn_failed", counter="spawn_failures")

            current_abort = abort_reason()
            if current_abort is not None:
                return aborted_outcome(current_abort)
            launch: dict[str, Any] = {
                "args": command,
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "cwd": staging_root,
                "env": _sanitized_environment(work_directory),
                "close_fds": True,
                "shell": False,
            }
            if os.name == "posix":
                launch["start_new_session"] = True
            elif os.name == "nt":
                launch["creationflags"] = getattr(
                    subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0,
                )
            windows_job = _create_windows_job()
            try:
                process = self._popen_factory(**launch)
                self._counters.increment("subprocess_runs")
            except (OSError, ValueError, subprocess.SubprocessError):
                if windows_job is not None:
                    windows_job.close()
                return self._error("validator_spawn_failed", counter="spawn_failures")
            if windows_job is not None:
                if windows_job.assign(process):
                    setattr(process, "_mycelium_validator_job", windows_job)
                else:
                    windows_job.close()

            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_capture = _PipeCapture(
                process.stdout,
                self.settings.response_max_bytes,
                True,
            )
            stderr_capture = _PipeCapture(process.stderr, 8192, False)
            stdout_thread = threading.Thread(target=stdout_capture.run, daemon=True)
            stderr_thread = threading.Thread(target=stderr_capture.run, daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            def write_request() -> None:
                try:
                    process.stdin.write(request_bytes)
                    process.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except (OSError, ValueError):
                        pass

            stdin_thread = threading.Thread(target=write_request, daemon=True)
            stdin_thread.start()

            cleanup_failure_recorded = False
            outcome: ValidatorProcessOutcome | None = None

            def collect_outcome() -> ValidatorProcessOutcome:
                nonlocal cleanup_failure_recorded
                termination: str | None = None
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        termination = "validator_cancelled"
                        break
                    if stdout_capture.oversized.is_set():
                        termination = "validator_response_oversized"
                        break
                    if stderr_capture.oversized.is_set():
                        termination = "validator_stderr_oversized"
                        break
                    if time.monotonic() >= effective_deadline:
                        termination = "validator_timeout"
                        break
                    time.sleep(0.01)

                if termination is not None:
                    if not _terminate_process_tree(process):
                        self._counters.increment("process_cleanup_failures")
                        cleanup_failure_recorded = True
                    if termination == "validator_timeout":
                        return self._error(termination, counter="timeouts")
                    if termination == "validator_cancelled":
                        return self._error(termination, counter="cancellations")
                    if termination == "validator_response_oversized":
                        return self._error(termination, counter="oversized_responses")
                    return self._error(termination, counter="crashes")

                if not _wait_process(process, 0.25):
                    if not _terminate_process_tree(process):
                        cleanup_failure_recorded = True
                        return self._error(
                            "validator_process_cleanup_failed",
                            counter="process_cleanup_failures",
                        )
                    return self._error("validator_process_cleanup_failed")

                stdout_capture.finished.wait(timeout=0.25)
                stderr_capture.finished.wait(timeout=0.25)
                if stdout_capture.oversized.is_set():
                    return self._error(
                        "validator_response_oversized",
                        counter="oversized_responses",
                    )
                if process.returncode != 0:
                    return self._error("validator_crash", counter="crashes")

                raw_response = bytes(stdout_capture.data).strip()
                try:
                    response = parse_runner_response_bytes(
                        raw_response,
                        request=request,
                        max_bytes=self.settings.response_max_bytes,
                    )
                except ValidatorProtocolError:
                    return self._error(
                        "validator_malformed_response",
                        counter="malformed_responses",
                    )

                self._counters.increment("successful_responses")
                infrastructure_failure_reasons = {
                    "validator_execution_error",
                    "validator_runner_protocol_error",
                    "validator_response_oversized",
                }
                infrastructure_failure = (
                    response.failure_reason in infrastructure_failure_reasons
                )
                if response.failure_reason == "validator_response_oversized":
                    self._counters.increment("oversized_responses")
                elif not response.ok and not infrastructure_failure:
                    self._counters.increment("validation_failures")
                return ValidatorProcessOutcome(
                    completed=not infrastructure_failure,
                    ok=response.ok,
                    score=response.score,
                    detail=dict(response.detail),
                    failure_reason=response.failure_reason,
                    containment_level=_containment_level(),
                    termination_reason=(
                        response.failure_reason if infrastructure_failure else None
                    ),
                )

            final_cleanup_failed = False
            try:
                outcome = collect_outcome()
            finally:
                # Every path after spawn owns the child and its three pipes.
                # Termination is attempted before staged input is removed.  If
                # it cannot be confirmed, never call BufferedWriter.close(): a
                # non-reading survivor may hold that buffer lock indefinitely.
                final_cleanup_failed = not _terminate_process_tree(process)
                if final_cleanup_failed and not cleanup_failure_recorded:
                    self._counters.increment("process_cleanup_failures")
                    cleanup_failure_recorded = True
                streams = (process.stdin, process.stdout, process.stderr)
                helper_deadline = time.monotonic() + 1.0
                for thread in (stdin_thread, stdout_thread, stderr_thread):
                    remaining_cleanup = helper_deadline - time.monotonic()
                    if remaining_cleanup <= 0:
                        break
                    thread.join(timeout=remaining_cleanup)
                helpers_finished = all(
                    not thread.is_alive()
                    for thread in (stdin_thread, stdout_thread, stderr_thread)
                )
                if not helpers_finished and not final_cleanup_failed:
                    final_cleanup_failed = True
                    if not cleanup_failure_recorded:
                        self._counters.increment("process_cleanup_failures")
                        cleanup_failure_recorded = True
                if helpers_finished:
                    for stream in streams:
                        try:
                            if stream is not None:
                                stream.close()
                        except (OSError, ValueError):
                            pass
            if final_cleanup_failed and outcome is not None and outcome.completed:
                return self._error("validator_process_cleanup_failed")
            assert outcome is not None
            return outcome
        # The owned workspace removes staged copies only after the child is reaped.
