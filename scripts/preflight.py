#!/usr/bin/env python3
"""Trusted-alpha deployment preflight checks.

The report deliberately describes credential authorities only by name and
status. Credential values are never included in human or JSON output.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config as app_config  # noqa: E402
from coordinator_lock import CoordinatorLock, CoordinatorLockError  # noqa: E402


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    message: str


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    mode: str
    checks: tuple[Check, ...]

    def as_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "mode": self.mode,
                "checks": [asdict(check) for check in self.checks],
            },
            indent=2,
            sort_keys=True,
        )


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False
    normalized = host.strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _probe_directory(path: Path) -> str | None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return "path exists but is not a directory"
        with tempfile.NamedTemporaryFile(prefix=".preflight-", dir=path, delete=True):
            pass
    except OSError as exc:
        return f"not writable ({exc.__class__.__name__})"
    return None


def _check_existing_database(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        return "database path is not a regular file"
    try:
        uri = f"{path.resolve().as_uri()}?mode=rw"
        with sqlite3.connect(uri, uri=True, timeout=2) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except (OSError, sqlite3.Error) as exc:
        return f"database is not usable ({exc.__class__.__name__})"
    if not result or result[0] != "ok":
        return "database integrity check did not return ok"
    return None


def run_preflight(
    config_path: Path | str,
    *,
    state_dir: Path | str | None = None,
    requested_mode: str | None = None,
    bind_host: str | None = None,
    check_lock: bool = True,
) -> PreflightReport:
    """Evaluate deployment safety without returning any sensitive values."""
    path = Path(config_path)
    state = Path(state_dir) if state_dir is not None else path.parent
    checks: list[Check] = []

    marker_requires_strict = app_config.trusted_alpha_expected(path)
    overrides: dict[str, Any] = {}
    try:
        overrides = app_config.read_overrides(path, require_exists=marker_requires_strict)
    except app_config.ConfigError as exc:
        provisional_mode = requested_mode or (
            "trusted_alpha" if marker_requires_strict else "local"
        )
        status = "error" if provisional_mode == "trusted_alpha" else "warning"
        checks.append(Check("config_json", status, str(exc)))
    else:
        checks.append(Check("config_json", "pass", "configuration is valid JSON"))

    configured_mode = overrides.get("deployment_mode", app_config.DEFAULTS["deployment_mode"])
    mode = requested_mode or str(configured_mode)
    if marker_requires_strict:
        if requested_mode == "local" or configured_mode == "local":
            checks.append(
                Check(
                    "deployment_mode",
                    "error",
                    "trusted-alpha installation marker cannot be downgraded implicitly",
                )
            )
        mode = "trusted_alpha"
    if mode not in app_config.VALID_DEPLOYMENT_MODES:
        checks.append(
            Check(
                "deployment_mode",
                "error",
                "deployment mode must be local or trusted_alpha",
            )
        )
        mode = "local"
    else:
        checks.append(Check("deployment_mode", "pass", f"deployment mode is {mode}"))
    trusted = mode == "trusted_alpha"

    settings = app_config.DEFAULTS.copy()
    settings.update(overrides)
    settings["deployment_mode"] = mode

    enrollment_mode = settings.get("node_enrollment_mode")
    if (
        not isinstance(enrollment_mode, str)
        or enrollment_mode not in app_config.VALID_NODE_ENROLLMENT_MODES
    ):
        checks.append(
            Check(
                "node_enrollment_mode",
                "error" if trusted else "warning",
                "node_enrollment_mode must be compat or required",
            )
        )
    elif trusted and enrollment_mode != "required":
        checks.append(
            Check(
                "node_enrollment_mode",
                "error",
                "trusted-alpha requires durable node enrollment; set node_enrollment_mode to required",
            )
        )
    elif enrollment_mode == "compat":
        checks.append(
            Check(
                "node_enrollment_mode",
                "warning",
                "legacy unenrolled node sessions are explicitly enabled for local compatibility",
            )
        )
    else:
        checks.append(
            Check(
                "node_enrollment_mode",
                "pass",
                "durable node enrollment is required",
            )
        )

    authorities = ("viewer_key", "pitch_key", "node_secret")
    valid_authorities: dict[str, str] = {}
    for authority in authorities:
        value = settings.get(authority)
        if app_config.credential_meets_policy(value):
            valid_authorities[authority] = str(value).strip()
            checks.append(
                Check(authority, "pass", f"{authority} meets the static credential policy")
            )
        else:
            status = "error" if trusted else "warning"
            checks.append(
                Check(
                    authority,
                    status,
                    f"{authority} is disabled or shorter than the minimum policy",
                )
            )
    if len(valid_authorities) == len(authorities) and len(
        set(valid_authorities.values())
    ) == len(authorities):
        checks.append(
            Check("authority_separation", "pass", "the three authorities are distinct")
        )
    elif len(set(valid_authorities.values())) != len(valid_authorities):
        checks.append(
            Check(
                "authority_separation",
                "error" if trusted else "warning",
                "credential authorities must use distinct values",
            )
        )

    public_pitch = settings.get("public_pitch")
    public_ack = settings.get("public_pitch_acknowledged")
    if not isinstance(public_pitch, bool) or not isinstance(public_ack, bool):
        checks.append(
            Check("public_pitch", "error", "public pitch settings must be booleans")
        )
    elif public_pitch and not public_ack:
        checks.append(
            Check(
                "public_pitch",
                "error" if trusted else "warning",
                "public pitch requires an explicit abuse-risk acknowledgement",
            )
        )
    elif public_pitch:
        checks.append(
            Check("public_pitch", "warning", "public pitch is intentionally enabled")
        )
    else:
        checks.append(Check("public_pitch", "pass", "public pitch is disabled"))

    https_enabled = settings.get("https_enabled")
    private_overlay = settings.get("private_overlay")
    secure_cookie = settings.get("viewer_cookie_secure")
    trust_proxy = settings.get("trust_proxy_headers")
    if not all(isinstance(value, bool) for value in (https_enabled, secure_cookie, trust_proxy)):
        checks.append(
            Check("https_cookie", "error", "HTTPS and proxy settings must be booleans")
        )
    elif trust_proxy:
        checks.append(
            Check(
                "https_cookie",
                "error" if trusted else "warning",
                "proxy-header trust is not supported by the RC1 coordinator",
            )
        )
    elif https_enabled and not secure_cookie:
        checks.append(
            Check(
                "https_cookie",
                "error" if trusted else "warning",
                "HTTPS deployments must mark the viewer session cookie secure",
            )
        )
    elif secure_cookie and not https_enabled:
        checks.append(
            Check(
                "https_cookie",
                "error" if trusted else "warning",
                "secure viewer cookies require the HTTPS deployment flag",
            )
        )
    else:
        checks.append(Check("https_cookie", "pass", "HTTPS and cookie settings agree"))

    if not isinstance(private_overlay, bool):
        checks.append(
            Check(
                "worker_transport",
                "error" if trusted else "warning",
                "private_overlay must be a boolean",
            )
        )
    elif trusted and not (https_enabled is True or private_overlay is True):
        checks.append(
            Check(
                "worker_transport",
                "error",
                "trusted-alpha bearer credentials require HTTPS or an authenticated private overlay",
            )
        )
    elif https_enabled is True:
        checks.append(Check("worker_transport", "pass", "HTTPS transport is declared"))
    elif private_overlay is True:
        checks.append(
            Check("worker_transport", "pass", "authenticated private-overlay transport is declared")
        )
    else:
        checks.append(
            Check(
                "worker_transport",
                "warning",
                "local compatibility mode does not declare protected bearer transport",
            )
        )

    effective_bind = bind_host or settings.get("bind_host")
    if trusted:
        checks.append(
            Check("bind_host", "pass", "trusted-alpha access is credential protected")
        )
    elif _is_loopback(effective_bind):
        checks.append(Check("bind_host", "pass", "local mode is bound to loopback"))
    elif len(valid_authorities) != len(authorities):
        checks.append(
            Check(
                "bind_host",
                "warning",
                "local mode is reachable beyond loopback while credentials are disabled",
            )
        )
    else:
        checks.append(Check("bind_host", "pass", "non-loopback bind is credential protected"))

    state_error = _probe_directory(state)
    checks.append(
        Check(
            "state_directory",
            "error" if state_error else "pass",
            state_error or "state directory is writable",
        )
    )
    for directory_name in ("output", "execution_artifacts", "projects"):
        error = _probe_directory(state / directory_name)
        checks.append(
            Check(
                f"directory_{directory_name}",
                "error" if error else "pass",
                error or f"{directory_name} directory is writable",
            )
        )

    database_error = _check_existing_database(state / "events.db")
    checks.append(
        Check(
            "sqlite_database",
            "error" if database_error else "pass",
            database_error or "SQLite database path is usable",
        )
    )

    if check_lock and not state_error:
        lock = CoordinatorLock(state, deployment_mode=mode)
        try:
            lock.acquire()
        except CoordinatorLockError as exc:
            checks.append(Check("coordinator_lock", "error", str(exc)))
        else:
            checks.append(
                Check("coordinator_lock", "pass", "single-coordinator lock is available")
            )
        finally:
            lock.release()
    elif not check_lock:
        checks.append(
            Check("coordinator_lock", "warning", "coordinator lock check was skipped")
        )

    return PreflightReport(
        ok=not any(check.status == "error" for check in checks),
        mode=mode,
        checks=tuple(checks),
    )


def deployment_health_ready(payload: object) -> bool:
    """Return whether the public health response meets the deploy gate."""
    return (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("private_routes_protected") is True
        and payload.get("node_enrollment_required") is True
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--mode", choices=sorted(app_config.VALID_DEPLOYMENT_MODES))
    parser.add_argument("--bind-host")
    parser.add_argument("--skip-lock-check", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_preflight(
        args.config,
        state_dir=args.state_dir,
        requested_mode=args.mode,
        bind_host=args.bind_host,
        check_lock=not args.skip_lock_check,
    )
    if args.json_output:
        print(report.as_json())
    else:
        print(f"Mycelium preflight: {'PASS' if report.ok else 'FAIL'} ({report.mode})")
        for check in report.checks:
            print(f"  [{check.status.upper():7}] {check.name}: {check.message}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
