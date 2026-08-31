"""Child entry point for one bounded, allowlisted built-in validator run.

The parent is responsible for process creation, a fresh working directory,
staged input copies, wall-clock enforcement, and process-tree cleanup.  This
entry point adds defense in depth: it closes the environment down again,
applies POSIX limits before reading a request, discards incidental output, and
writes exactly one bounded response to its saved protocol descriptor.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import Any, Callable


# Duplicated intentionally: importing Pydantic or validator dependencies before
# these hard ceilings are installed would defeat the early child boundary.
_HARD_REQUEST_BYTES = 16 * 1024 * 1024
_HARD_RESPONSE_BYTES = 256 * 1024
_HARD_CPU_SECONDS = 120
_HARD_WALL_SECONDS = 125
_HARD_MEMORY_BYTES = 1024 * 1024 * 1024
_HARD_FILE_BYTES = 1024 * 1024
_HARD_OPEN_FILES = 128
_HARD_CHILD_PROCESSES = 16

_SAFE_INHERITED_ENVIRONMENT = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "SYSTEMROOT",
        "WINDIR",
    }
)
_FIXED_ENVIRONMENT = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}

# The child must not mutate repository or user cache directories merely by
# importing the trusted built-in validator implementation.
sys.dont_write_bytecode = True


def _sanitize_environment() -> None:
    # The parent starts the runner in ``<fresh workspace>/input``.  Preserve a
    # controlled temporary-directory target derived from that owned workspace,
    # not from ambient TEMP/TMP values that may point at user or system state.
    controlled_temporary = str(Path.cwd().resolve().parent)
    retained = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _SAFE_INHERITED_ENVIRONMENT
        and isinstance(value, str)
        and len(value) <= 1024
        and "\x00" not in value
    }
    os.environ.clear()
    os.environ.update(retained)
    os.environ.update(_FIXED_ENVIRONMENT)
    os.environ.update(
        {
            "TEMP": controlled_temporary,
            "TMP": controlled_temporary,
            "TMPDIR": controlled_temporary,
        }
    )


def _saved_protocol_descriptor() -> int:
    descriptor = os.dup(sys.stdout.fileno())
    null_descriptor = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_descriptor, sys.stdout.fileno())
        os.dup2(null_descriptor, sys.stderr.fileno())
    finally:
        os.close(null_descriptor)
    return descriptor


def _lower_posix_limit(resource_module, name: str, soft: int, hard: int | None = None) -> None:
    identifier = getattr(resource_module, name, None)
    if identifier is None:
        return
    current_soft, current_hard = resource_module.getrlimit(identifier)
    infinity = resource_module.RLIM_INFINITY
    requested_hard = soft if hard is None else hard
    target_hard = requested_hard if current_hard == infinity else min(requested_hard, current_hard)
    target_soft = min(soft, target_hard)
    if current_soft != infinity:
        target_soft = min(target_soft, current_soft)
    resource_module.setrlimit(identifier, (target_soft, target_hard))


def _apply_hard_posix_limits() -> None:
    if os.name != "posix":
        return
    import resource

    _lower_posix_limit(resource, "RLIMIT_CORE", 0, 0)
    _lower_posix_limit(resource, "RLIMIT_CPU", _HARD_CPU_SECONDS, _HARD_CPU_SECONDS + 1)
    _lower_posix_limit(resource, "RLIMIT_AS", _HARD_MEMORY_BYTES, _HARD_MEMORY_BYTES)
    _lower_posix_limit(resource, "RLIMIT_FSIZE", _HARD_FILE_BYTES, _HARD_FILE_BYTES)
    _lower_posix_limit(resource, "RLIMIT_NOFILE", _HARD_OPEN_FILES, _HARD_OPEN_FILES)
    _lower_posix_limit(resource, "RLIMIT_NPROC", _HARD_CHILD_PROCESSES, _HARD_CHILD_PROCESSES)
    os.umask(0o077)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(_HARD_WALL_SECONDS)


def _apply_request_posix_limits(limits: Any) -> None:
    if os.name != "posix":
        return
    import resource

    cpu = max(1, int(limits.cpu_time_seconds))
    _lower_posix_limit(resource, "RLIMIT_CPU", cpu, min(_HARD_CPU_SECONDS + 1, cpu + 1))
    _lower_posix_limit(resource, "RLIMIT_AS", int(limits.memory_bytes), int(limits.memory_bytes))
    _lower_posix_limit(resource, "RLIMIT_FSIZE", int(limits.file_size_bytes), int(limits.file_size_bytes))
    _lower_posix_limit(resource, "RLIMIT_NOFILE", int(limits.open_files), int(limits.open_files))
    _lower_posix_limit(resource, "RLIMIT_NPROC", int(limits.child_processes), int(limits.child_processes))
    signal.setitimer(signal.ITIMER_REAL, float(limits.wall_time_seconds))


def _read_request_bytes() -> bytes:
    raw = sys.stdin.buffer.read(_HARD_REQUEST_BYTES + 1)
    if len(raw) > _HARD_REQUEST_BYTES:
        raise ValueError("request_oversized")
    return raw


def _stable_failure_response(request, reason: str):
    from execution.validator_protocol import ValidatorRunnerResponseV1

    return ValidatorRunnerResponseV1(
        validator_name=request.validator_name,
        validator_version=request.validator_version,
        ok=False,
        detail={},
        failure_reason=reason,
    )


def _execute_request(request, dispatcher: Callable[[Any], Any]):
    from execution.validator_protocol import (
        ValidatorRunnerResponseV1,
        ensure_response_identity,
    )

    try:
        response = dispatcher(request)
        if not isinstance(response, ValidatorRunnerResponseV1):
            response = ValidatorRunnerResponseV1.model_validate(response)
        return ensure_response_identity(request, response)
    except BaseException:
        return _stable_failure_response(request, "validator_execution_error")


def _fallback_bytes() -> bytes:
    # A malformed request has no trustworthy identity.  The parent compares the
    # response against its own request and therefore rejects this fixed identity.
    return (
        b'{"protocol_version":"1","validator_name":"nonempty",'
        b'"validator_version":"2","ok":false,"score":null,"detail":{},'
        b'"failure_reason":"validator_runner_protocol_error"}'
    )


def _write_protocol_bytes(descriptor: int, raw: bytes) -> None:
    if len(raw) > _HARD_RESPONSE_BYTES:
        raw = _fallback_bytes()
    # The protocol is EOF-delimited.  Adding a framing newline here would make
    # an otherwise valid response of exactly ``response_max_bytes`` exceed the
    # parent's stream cap by one byte.
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            return
        view = view[written:]


def main() -> int:
    protocol_descriptor = _saved_protocol_descriptor()
    exit_code = 0
    try:
        try:
            _sanitize_environment()
            _apply_hard_posix_limits()

            repository_root = Path(__file__).resolve().parents[1]
            if str(repository_root) not in sys.path:
                sys.path.insert(0, str(repository_root))

            from execution.validator_protocol import (
                MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V1,
                MAX_VALIDATOR_RUNNER_RESPONSE_BYTES_V1,
                ValidatorProtocolError,
                dump_runner_response_bytes,
                parse_runner_request_bytes,
            )

            if (
                MAX_VALIDATOR_RUNNER_REQUEST_BYTES_V1 != _HARD_REQUEST_BYTES
                or MAX_VALIDATOR_RUNNER_RESPONSE_BYTES_V1 != _HARD_RESPONSE_BYTES
            ):
                raise RuntimeError("runner_hard_limit_mismatch")
            request = parse_runner_request_bytes(_read_request_bytes())
            _apply_request_posix_limits(request.limits)

            # Imported only after the early environment and resource boundary.
            from execution.validators import execute_runner_request

            # The launcher owns cwd and fixes it to the private staged-input
            # directory.  Resolve it once as explicit parent-controlled context;
            # the strict request cannot select or replace this filesystem root.
            stage_root = Path.cwd().resolve(strict=True)
            response = _execute_request(
                request,
                lambda candidate: execute_runner_request(
                    candidate,
                    stage_root=stage_root,
                ),
            )
            try:
                raw = dump_runner_response_bytes(
                    response,
                    max_bytes=request.limits.response_max_bytes,
                )
            except ValidatorProtocolError as exc:
                if exc.code != "validator_protocol_output_oversized":
                    raise
                raw = dump_runner_response_bytes(
                    _stable_failure_response(
                        request,
                        "validator_response_oversized",
                    ),
                    max_bytes=request.limits.response_max_bytes,
                )
        except BaseException:
            raw = _fallback_bytes()
            exit_code = 2
        _write_protocol_bytes(protocol_descriptor, raw)
        return exit_code
    finally:
        try:
            os.close(protocol_descriptor)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
