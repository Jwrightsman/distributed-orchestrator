"""
Worker node — runs on any machine that wants to contribute compute.

Connects to the orchestrator server, picks up tasks, runs them locally
via Ollama, and sends results back. This is what turns a laptop into
a node in the network.

Usage:
    python node.py --server http://ORCHESTRATOR_IP:8000
    python node.py --server http://192.168.1.50:8000
"""

import argparse
import asyncio
import os
import platform
import time
from pathlib import Path

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from ollama_client import generate_stream, check_ollama, DEFAULT_MODEL
from worker_identity import (
    WorkerIdentity,
    WorkerIdentityError,
    default_identity_file,
    load_worker_identity,
    load_or_create_worker_identity,
    normalize_coordinator,
    normalize_enrollment_id,
    normalize_worker_node_id,
    persist_learned_enrollment,
)

console = Console()

_MAX_WORKER_OUTPUT_BYTES = 10_485_760
_MAX_WORKER_ERROR_BYTES = 2048


class NodeSessionRejected(RuntimeError):
    """The coordinator requires this worker to register for a new session."""


class NodeRegistrationRejected(RuntimeError):
    """The coordinator rejected enrollment or registration without echoing secrets."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str = "node_registration_rejected",
        action: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.action = action

    @property
    def retryable(self) -> bool:
        return self.status_code == 429 or self.status_code >= 500


def _hardware_info() -> dict:
    """Collect basic hardware info to send on registration."""
    info: dict = {
        "cpu_count": os.cpu_count(),
        "ram_gb": None,
        "gpu": None,
    }
    try:
        import psutil  # type: ignore
        info["ram_gb"] = round(psutil.virtual_memory().total / 1024 ** 3, 1)
    except ImportError:
        pass
    # Best-effort GPU detection — non-critical
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            info["gpu"] = r.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return info


def _auth_headers(secret: str, session_token: str = "") -> dict:
    """Build admission and per-node session headers without logging either."""

    headers = {"X-Node-Secret": secret} if secret else {}
    if session_token:
        headers["X-Node-Session"] = session_token
    return headers


def _registration_headers(
    *,
    enrollment_action: str | None,
    secret: str,
    session_token: str,
) -> dict[str, str]:
    # Returning enrollment authentication replaces the deployment-wide
    # bootstrap secret.  A legacy registration has no action and retains the
    # old admission header for local compatibility only.
    bootstrap_secret = secret if enrollment_action in {None, "bootstrap"} else ""
    return _auth_headers(bootstrap_secret, session_token)


def _session_rejected(response) -> bool:
    if response.status_code not in {401, 403, 426}:
        return False
    try:
        detail = response.json().get("detail", {})
    except Exception:
        detail = {}
    return isinstance(detail, dict) and detail.get("code") in {
        "node_session_rejected",
        "node_registration_required",
        "node_enrollment_revoked",
        "node_enrollment_credential_rotated",
        "enrollment_revoked",
        "enrollment_credential_rotated",
    }


def _bounded_registration_error(
    response,
    *,
    sensitive_values: tuple[str, ...],
) -> NodeRegistrationRejected:
    code = "node_registration_rejected"
    action = ""
    message = f"coordinator rejected node registration (HTTP {response.status_code})"
    try:
        detail = response.json().get("detail", {})
    except Exception:
        detail = {}
    secrets_to_redact = sorted(
        {value for value in sensitive_values if value},
        key=len,
        reverse=True,
    )

    def redacted(value: str) -> str:
        for secret_value in secrets_to_redact:
            value = value.replace(secret_value, "<redacted>")
        return value

    if isinstance(detail, dict):
        raw_code = detail.get("code")
        raw_action = detail.get("action")
        raw_message = detail.get("message") or detail.get("reason")
        if isinstance(raw_code, str) and raw_code:
            code = redacted(raw_code)[:96]
        if isinstance(raw_action, str) and raw_action:
            action = redacted(raw_action)[:160]
        if isinstance(raw_message, str) and raw_message:
            message = redacted(raw_message)[:512]
    elif isinstance(detail, str) and detail:
        message = redacted(detail)[:512]
    rendered = f"{code}: {message}"
    if action:
        rendered += f" (action: {action})"
    return NodeRegistrationRejected(
        rendered,
        status_code=int(response.status_code),
        code=code,
        action=action,
    )


def _bounded_utf8(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


async def register(
    server: str,
    node_id: str,
    secret: str = "",
    capabilities: list[str] | None = None,
    session_token: str = "",
    enrollment_action: str | None = None,
    enrollment_credential: str = "",
) -> dict:
    """Register or idempotently refresh this node's server-issued session."""
    hw = _hardware_info()

    # Auto-detect capabilities if not provided
    caps = list(capabilities) if capabilities else []
    if not caps:
        if hw.get("gpu"):
            caps.append("gpu")
        if (hw.get("ram_gb") or 0) >= 16:
            caps.append("large-context")

    info = {
        "node_id": node_id,
        "model": DEFAULT_MODEL,
        "platform": platform.system(),
        "machine": platform.machine(),
        "hostname": platform.node(),
        "capabilities": caps,
        **hw,
    }
    if enrollment_action is not None:
        if enrollment_action not in {"bootstrap", "returning"}:
            raise ValueError("enrollment_action must be bootstrap or returning")
        info["enrollment_action"] = enrollment_action
        info["enrollment_credential"] = enrollment_credential
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        resp = await client.post(
            f"{server}/nodes/register",
            json=info,
            headers=_registration_headers(
                enrollment_action=enrollment_action,
                secret=secret,
                session_token=session_token,
            ),
        )
        if resp.status_code >= 400:
            raise _bounded_registration_error(
                resp,
                sensitive_values=(secret, enrollment_credential, session_token),
            )
        return resp.json()


def _apply_registration(
    session: dict,
    registration: dict,
    *,
    identity: WorkerIdentity | None = None,
    identity_file: Path | str | None = None,
) -> str:
    """Install a registration grant and return the normalized node id."""

    token = registration.get("session_token")
    session_id = registration.get("session_id")
    node_id = registration.get("node_id")
    if not token or not session_id or not node_id:
        raise RuntimeError("orchestrator registration did not issue a node session")
    normalized_node_id = normalize_worker_node_id(str(node_id))
    if identity is not None and normalized_node_id != identity.node_id:
        raise WorkerIdentityError(
            "coordinator registration returned a different node_id for this identity"
        )
    enrollment_confirmation = registration.get("enrolled")
    if identity is not None:
        expected_action = (
            "returning" if identity.enrollment_id is not None else "bootstrap"
        )
        if (
            enrollment_confirmation is not True
            or registration.get("enrollment_action") != expected_action
        ):
            raise WorkerIdentityError(
                "coordinator did not confirm the requested durable enrollment; "
                "upgrade the coordinator instead of using a legacy session"
            )
    enrolled = enrollment_confirmation is True
    updated_identity = identity
    if enrolled:
        enrollment_id = normalize_enrollment_id(registration.get("enrollment_id"))
        credential_version = registration.get("credential_version")
        if identity is None or identity_file is None:
            raise WorkerIdentityError(
                "enrolled registration cannot be installed without its identity file"
            )
        if identity.enrollment_id is None:
            updated_identity = persist_learned_enrollment(
                identity_file,
                identity,
                enrollment_id,
                credential_version,
            )
        elif identity.enrollment_id != enrollment_id:
            raise WorkerIdentityError(
                "coordinator returned a different enrollment_id for this identity"
            )
        elif identity.credential_version != credential_version:
            raise WorkerIdentityError(
                "coordinator returned a different credential_version for this identity"
            )
        session["enrollment_id"] = enrollment_id
        session["credential_version"] = credential_version
    else:
        # Explicit legacy sessions exist only for local compatibility.  They
        # continue using node_secret on normal requests and never masquerade as
        # durable enrollment identity.
        session["enrollment_id"] = None
        session["credential_version"] = None
    session["session_token"] = str(token)
    session["session_id"] = str(session_id)
    session["session_expires_at"] = registration.get("session_expires_at")
    session["enrolled"] = enrolled
    if updated_identity is not None:
        session["worker_identity"] = updated_identity
    return normalized_node_id


# Reconnect backoff. Measured WAN numbers (scripts/wan_bench.py, Indiana ->
# Germany) leave every timeout in this file with huge headroom — registration is
# 218 ms against a 10 s timeout — so latency is not what needed hardening. The
# retry *pattern* did.
#
# The old loop slept a flat 10 s forever. That is fine for one node and wrong for
# a network: when the orchestrator restarts, every connected node drops at the
# same instant and then retries in lockstep, hammering a box that is still
# booting. A redeploy is exactly this event, and the launch plan targets 3-5
# external nodes to start.
_RECONNECT_BASE_S = 2.0
_RECONNECT_CAP_S = 60.0


def reconnect_delay(attempt: int, rng=None) -> float:
    """Seconds to wait before retry number `attempt` (0-based).

    Exponential to a cap, with +/-25% jitter so simultaneous drops do not stay
    synchronised. Jitter is the part that actually breaks the thundering herd —
    without it, backoff just moves every node's retry to the same later instant.

    The result is clamped to the cap *after* jitter, so the cap means what it
    says. Applying jitter last let it reach 75 s against a stated 60 s cap.
    """
    import random as _random

    rng = rng or _random
    # Clamp the exponent, not just the result: a node left running through a
    # long outage reaches attempt counts where 2**attempt is an integer big
    # enough to raise OverflowError on the float multiply. Found by a test
    # asserting the cap holds for attempt=5000 — roughly three days of retries,
    # which is an ordinary laptop left open over a weekend.
    raw = min(_RECONNECT_CAP_S, _RECONNECT_BASE_S * (2 ** min(max(0, attempt), 30)))
    return min(_RECONNECT_CAP_S, raw * (0.75 + rng.random() * 0.5))


async def poll_and_execute(server: str, node_id: str, session: dict, secret: str = "") -> str | None:
    """Poll the orchestrator for tasks, execute them, return task_id or None.

    The server long-polls up to 25s before returning 204, so this call
    blocks for up to ~25s when there's no work — no tight polling loop needed.
    """
    session_token = str(session.get("session_token", ""))
    legacy_secret = secret if not bool(session.get("enrolled", False)) else ""
    async with httpx.AsyncClient(timeout=600, trust_env=False) as client:
        resp = await client.get(
            f"{server}/tasks/next",
            params={"node_id": node_id},
            headers=_auth_headers(legacy_secret, session_token),
        )
        if _session_rejected(resp):
            raise NodeSessionRejected("coordinator rejected the node session")
        if resp.status_code == 204:
            return None  # No work available (long-poll timed out)
        if resp.status_code == 429:
            # Circuit breaker tripped — back off for the indicated duration
            retry_after = resp.json().get("retry_after", 60)
            console.print(f"[yellow]Circuit breaker open — sitting out {retry_after}s[/yellow]")
            await asyncio.sleep(retry_after)
            return None

        resp.raise_for_status()
        task = resp.json()

        task_id = task["task_id"]
        title = task.get("title", "unnamed")
        prompt = task["prompt"]
        system = task.get("system", "")
        try:
            max_output_bytes = int(task.get("max_output_bytes", 1_048_576))
        except (TypeError, ValueError):
            max_output_bytes = 1_048_576
        max_output_bytes = min(
            _MAX_WORKER_OUTPUT_BYTES, max(1, max_output_bytes)
        )

        console.print(Panel(
            f"[dim]{task_id}[/dim]",
            title=f"[bold yellow]TASK[/bold yellow]  {title}",
            border_style="yellow",
        ))

        start = time.time()
        try:
            # Stream tokens from Ollama, batch them, and relay to the orchestrator
            # so the dashboard shows live output from remote workers.
            _BATCH_TOKENS = 20      # flush after this many tokens …
            _BATCH_INTERVAL = 0.3   # … or after this many seconds, whichever comes first

            collected: list[str] = []
            collected_bytes = 0
            batch: list[str] = []
            last_flush = time.time()
            limit_error: str | None = None

            async def _flush_batch(client_: httpx.AsyncClient, batch_: list[str]):
                if not batch_:
                    return None
                text = "".join(batch_)
                try:
                    stream_response = await client_.post(
                        f"{server}/tasks/{task_id}/tokens",
                        json={
                            "node_id": node_id,
                            "tokens": text,
                            "contract_version": task.get("contract_version"),
                            "attempt_id": task.get("attempt_id"),
                            "nonce": task.get("nonce"),
                            "execution_id": task.get("execution_id"),
                            "execution_unit_id": task.get("execution_unit_id"),
                            "execution_unit_kind": task.get("execution_unit_kind"),
                        },
                        headers=_auth_headers(legacy_secret, session_token),
                    )
                    if _session_rejected(stream_response):
                        raise NodeSessionRejected(
                            "coordinator rejected the node session while streaming"
                        )
                    if stream_response.status_code in (413, 429):
                        try:
                            payload = stream_response.json()
                        except Exception:
                            payload = {}
                        return str(
                            payload.get("error") or "stream_limit_exceeded"
                        )
                except NodeSessionRejected:
                    raise
                except Exception:
                    # Live projection remains best-effort for network failures.
                    # Session and limit outcomes are handled explicitly above.
                    pass
                return None

            async with httpx.AsyncClient(timeout=600, trust_env=False) as stream_client:
                async for token in generate_stream(prompt, system=system):
                    token_bytes = len(token.encode("utf-8"))
                    if collected_bytes + token_bytes > max_output_bytes:
                        limit_error = "output_limit_exceeded"
                        break
                    collected.append(token)
                    collected_bytes += token_bytes
                    batch.append(token)
                    now_t = time.time()
                    if len(batch) >= _BATCH_TOKENS or (now_t - last_flush) >= _BATCH_INTERVAL:
                        relay_error = await _flush_batch(stream_client, batch)
                        batch = []
                        last_flush = now_t
                        if relay_error:
                            limit_error = relay_error
                            break
                # flush any remaining tokens
                if batch:
                    relay_error = await _flush_batch(stream_client, batch)
                    if relay_error:
                        limit_error = relay_error

            result = None if limit_error else "".join(collected)
            elapsed = time.time() - start
            result_error = None
            if limit_error:
                result_error = _bounded_utf8(
                    f"{limit_error}: generation stopped at the server-issued "
                    f"max_output_bytes={max_output_bytes}",
                    _MAX_WORKER_ERROR_BYTES,
                )

            submit_resp = await client.post(
                f"{server}/tasks/{task_id}/result",
                json={
                    "node_id": node_id,
                    "output": result,
                    "error": result_error,
                    "elapsed_seconds": elapsed,
                    # Issued with this task. An unbound result is rejected and
                    # quarantined; it can never satisfy operational execution.
                    "attempt_id": task.get("attempt_id"),
                    "nonce": task.get("nonce"),
                    "contract_version": task.get("contract_version"),
                    "execution_id": task.get("execution_id"),
                    "execution_unit_id": task.get("execution_unit_id"),
                    "execution_unit_kind": task.get("execution_unit_kind"),
                },
                headers=_auth_headers(legacy_secret, session_token),
            )
            if _session_rejected(submit_resp):
                raise NodeSessionRejected(
                    "coordinator rejected the node session while submitting a result"
                )
            if submit_resp.status_code != 200:
                try:
                    detail = submit_resp.json().get("detail", submit_resp.text)
                except Exception:
                    detail = submit_resp.text
                console.print(
                    f"[red bold]FAILED[/red bold]  {title}: "
                    f"orchestrator rejected result ({submit_resp.status_code}): {detail}\n"
                )
                return None
            credits = submit_resp.json().get("credits_earned", 0)

            session["tasks"] += 1
            session["credits"] += credits

            if limit_error:
                console.print(
                    f"[red bold]FAILED[/red bold]  {title}: "
                    f"{limit_error} (limit {max_output_bytes} UTF-8 bytes)\n"
                )
                return None

            console.print(
                f"[bold green]DONE[/bold green]  {title} "
                f"[dim]({elapsed:.0f}s)[/dim]  "
                    f"[bold yellow]+{credits} contribution points[/bold yellow]  "
                    f"[dim]session total: {session['credits']} points[/dim]"
            )
            console.print()
            return task_id

        except NodeSessionRejected:
            raise
        except Exception as original_error:
            report_problem = ""
            try:
                error_response = await client.post(
                    f"{server}/tasks/{task_id}/result",
                    json={
                        "node_id": node_id,
                        "attempt_id": task.get("attempt_id"),
                        "nonce": task.get("nonce"),
                        "contract_version": task.get("contract_version"),
                        "execution_id": task.get("execution_id"),
                        "execution_unit_id": task.get("execution_unit_id"),
                        "execution_unit_kind": task.get("execution_unit_kind"),
                        "output": None,
                        "error": _bounded_utf8(
                            str(original_error), _MAX_WORKER_ERROR_BYTES
                        ),
                        "elapsed_seconds": time.time() - start,
                    },
                    headers=_auth_headers(legacy_secret, session_token),
                )
                if _session_rejected(error_response):
                    raise NodeSessionRejected(
                        "coordinator rejected the node session while reporting an error"
                    )
                if error_response.status_code != 200:
                    report_problem = (
                        f"; error report was rejected with HTTP "
                        f"{error_response.status_code}"
                    )
            except NodeSessionRejected:
                raise
            except Exception as report_error:
                report_problem = (
                    f"; error report also failed: "
                    f"{type(report_error).__name__}: {report_error}"
                )
            console.print(
                f"[red bold]FAILED[/red bold]  {title}: "
                f"{original_error}{report_problem}\n"
            )
            return None


async def main():
    parser = argparse.ArgumentParser(description="Join the network as a worker node")
    parser.add_argument("--server", required=True, help="Orchestrator URL (e.g. http://192.168.1.50:8000)")
    parser.add_argument("--node-id", default=None, help="Custom node ID (defaults to hostname)")
    parser.add_argument(
        "--identity-file",
        type=Path,
        help=(
            "Private durable worker identity JSON (default: a coordinator-hashed "
            "file in the current user's Mycelium configuration directory)"
        ),
    )
    parser.add_argument(
        "--secret",
        default="",
        help=(
            "Shared network-admission secret (node_secret); registration then "
            "issues a process-local node session"
        ),
    )
    parser.add_argument("--capabilities", default="", help="Comma-separated capability tags, e.g. 'gpu,large-context' (auto-detected if omitted)")
    args = parser.parse_args()

    try:
        node_id = normalize_worker_node_id(args.node_id or platform.node())
        server = normalize_coordinator(args.server)
        identity_file = (
            args.identity_file.expanduser()
            if args.identity_file is not None
            else default_identity_file(server)
        )
    except WorkerIdentityError as exc:
        console.print(f"[red bold]Invalid worker identity configuration:[/red bold] {exc}")
        return
    secret = args.secret
    capabilities = [c.strip() for c in args.capabilities.split(",") if c.strip()] if args.capabilities else None

    # Pre-flight: check local Ollama
    status = await check_ollama()
    if not status["ok"]:
        console.print(f"[red bold]ERROR:[/red bold] {status['error']}")
        return

    try:
        # A new credential is durably written before the bootstrap request. If
        # the response is lost, the same credential makes the retry converge on
        # the enrollment already committed by the coordinator.
        identity = load_or_create_worker_identity(
            identity_file,
            coordinator=server,
            node_id=node_id,
        )
    except WorkerIdentityError as exc:
        console.print(f"[red bold]Worker identity unavailable:[/red bold] {exc}")
        return

    _caps_display = ", ".join(capabilities) if capabilities else "[dim]auto-detect[/dim]"
    console.print(Panel(
        f"[bold]Node ID:[/bold]      {node_id}\n"
        f"[bold]Model:[/bold]        {DEFAULT_MODEL}\n"
        f"[bold]Capabilities:[/bold] {_caps_display}\n"
        f"[bold]Orchestrator:[/bold] {server}\n"
        f"[bold]Identity file:[/bold] {identity_file}",
        title="[bold cyan]Distributed AI Node[/bold cyan]",
        border_style="cyan",
    ))

    # Registration returns an ephemeral server-issued bearer session.  Keep it
    # only in memory; coordinator or worker restart intentionally requires a new
    # registration.
    session = {
        "tasks": 0,
        "credits": 0,
        "session_token": "",
        "session_id": None,
        "session_expires_at": None,
        "enrollment_id": identity.enrollment_id,
        "enrolled": False,
        "worker_identity": identity,
    }
    try:
        enrollment_action = (
            "returning" if identity.enrollment_id is not None else "bootstrap"
        )
        reg = await register(
            server,
            node_id,
            secret=secret,
            capabilities=capabilities,
            enrollment_action=enrollment_action,
            enrollment_credential=identity.enrollment_credential,
        )
        node_id = _apply_registration(
            session,
            reg,
            identity=identity,
            identity_file=identity_file,
        )
        identity = session["worker_identity"]
        # After registration the server may have added auto-detected caps (model:<name>, etc.)
        # Show what was actually registered so the user knows what the node is offering.
        registered_caps = reg.get("capabilities", capabilities or [])
        visible_caps = [c for c in (registered_caps or []) if not c.startswith("model:")]
        caps_str = ", ".join(visible_caps) if visible_caps else "none"
        console.print(f"[green]Connected.[/green] {reg.get('message', '')}")
        console.print(f"[dim]Capabilities: {caps_str}[/dim]\n")
    except (NodeRegistrationRejected, WorkerIdentityError) as e:
        console.print(f"[red bold]Could not register with orchestrator at {server}[/red bold]")
        console.print(f"[dim]{e}[/dim]")
        return
    except Exception as e:
        console.print(f"[red bold]Could not connect to orchestrator at {server}[/red bold]")
        console.print(f"[dim]{e}[/dim]")
        console.print("\nMake sure the orchestrator is running:")
        console.print("  [dim]python -m uvicorn server:app --host 0.0.0.0 --port 8000[/dim]")
        return

    console.print("[dim]Waiting for tasks... (Ctrl+C to stop)[/dim]\n")

    registered = True
    attempt = 0  # consecutive connection failures, drives the backoff

    while True:
        try:
            # Re-register if we lost and regained connection
            if not registered:
                # Rotation handoff replaces the private identity file while
                # the old process-local session is being invalidated. Reload
                # before returning registration so a safely installed bundle
                # takes effect without reusing cached credential material.
                identity = load_worker_identity(
                    identity_file,
                    coordinator=server,
                    node_id=node_id,
                )
                session["worker_identity"] = identity
                enrollment_action = (
                    "returning"
                    if identity.enrollment_id is not None
                    else "bootstrap"
                )
                reg = await register(
                    server,
                    node_id,
                    secret=secret,
                    capabilities=capabilities,
                    session_token=str(session.get("session_token", "")),
                    enrollment_action=enrollment_action,
                    enrollment_credential=identity.enrollment_credential,
                )
                node_id = _apply_registration(
                    session,
                    reg,
                    identity=identity,
                    identity_file=identity_file,
                )
                console.print(f"[green]Reconnected.[/green] {reg.get('message', '')}\n")
                registered = True

            # Server long-polls up to 25s — this call already blocks while waiting.
            # No sleep needed between polls; just loop immediately.
            await poll_and_execute(server, node_id, session, secret=secret)

            attempt = 0  # a clean poll means the link is healthy again

        except httpx.ConnectError:
            if registered:
                console.print("[yellow]Lost connection to orchestrator. Retrying...[/yellow]")
            registered = False
            delay = reconnect_delay(attempt)
            attempt += 1
            console.print(f"[dim]Reconnecting in {delay:.0f}s...[/dim]")
            await asyncio.sleep(delay)

        except NodeSessionRejected:
            if registered:
                console.print(
                    "[yellow]Node session expired or was invalidated; "
                    "registering again...[/yellow]"
                )
            registered = False
            # No backoff is needed for an explicit authentication response.  The
            # next loop idempotently refreshes or obtains a replacement token.
            attempt = 0

        except NodeRegistrationRejected as exc:
            if not exc.retryable:
                console.print(f"[red bold]Enrollment rejected:[/red bold] {exc}")
                break
            delay = reconnect_delay(attempt)
            attempt += 1
            console.print(
                f"[yellow]Registration temporarily unavailable:[/yellow] {exc}. "
                f"Retrying in {delay:.0f}s..."
            )
            await asyncio.sleep(delay)

        except WorkerIdentityError as exc:
            console.print(f"[red bold]Worker identity rejected:[/red bold] {exc}")
            break

        except KeyboardInterrupt:
            _print_session_summary(node_id, session)
            break

        except Exception as e:
            delay = reconnect_delay(attempt)
            attempt += 1
            console.print(f"[red]Error:[/red] {e}. Retrying in {delay:.0f}s...")
            await asyncio.sleep(delay)


def _print_session_summary(node_id: str, session: dict):
    table = Table(title="Session Summary", box=box.SIMPLE, border_style="dim")
    table.add_column("Node")
    table.add_column("Tasks Completed", justify="center")
    table.add_column("Contribution Points", justify="right", style="yellow")
    table.add_row(node_id, str(session["tasks"]), str(session["credits"]))
    console.print()
    console.print(table)
    console.print("[dim]Thanks for contributing to the network.[/dim]\n")


if __name__ == "__main__":
    asyncio.run(main())
