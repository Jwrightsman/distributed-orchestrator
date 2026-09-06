#!/usr/bin/env python3
"""Will a Mycelium worker accept this certificate? Answered locally.

The question an operator cannot otherwise answer without a volunteer: does the
certificate I just obtained actually work, or am I about to find out over text
message while somebody's install fails?

This script answers it on the coordinator, with no worker, no contributor and
no DNS involved. It loads the certificate and key into a TLS listener bound to
127.0.0.1, connects to that listener with a **fully verifying** client -- the
same trust store and the same hostname check a worker uses -- and reports what
happened.

    python3 scripts/tls_local_check.py \\
        --cert /var/lib/caddy/HOST.TAILNET.ts.net.crt \\
        --key  /var/lib/caddy/HOST.TAILNET.ts.net.key \\
        --name HOST.TAILNET.ts.net

Nothing is written, no configuration is read, and no remote host is contacted:
the only socket opened is a loopback socket to this process itself.

What it establishes: the chain in --cert is complete, is signed by a CA that a
stock client trusts, is valid for --name, and has not expired. That is every
certificate failure a joining worker can hit.

What it does not establish: that the certificate is installed in the proxy,
that DNS points at this machine, or that the port is open. Those are
`scripts/deploy_preflight.py --url https://...`, run after the proxy is up.

One thing worth knowing before you generate anything: a Mycelium worker trusts
the certifi CA bundle and nothing else. It builds its HTTP client with
`trust_env=False`, so SSL_CERT_FILE and friends cannot add a private CA to it,
and there is deliberately no flag anywhere that relaxes it. A self-signed
certificate therefore cannot be made to work on a contributor's machine, and
this script will tell you so rather than let you discover it later. Use
`tailscale cert` (Path A) or Caddy's automatic issuance (Path B); both produce
certificates that are already trusted.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Renewal normally happens at 30 days; below this something has gone wrong.
CERT_WARN_DAYS = 21


class LocalTLSCheckError(RuntimeError):
    """The certificate or key could not be used at all."""


@dataclass(frozen=True)
class HandshakeResult:
    """What a stock client saw when it connected to this certificate."""

    trusted: bool
    server_name: str
    certificate: dict[str, Any] | None = None
    error: str | None = None

    @property
    def not_after(self) -> datetime | None:
        if not self.certificate:
            return None
        raw = str(self.certificate.get("notAfter", ""))
        for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    @property
    def days_remaining(self) -> int | None:
        expiry = self.not_after
        if expiry is None:
            return None
        return (expiry - datetime.now(timezone.utc)).days

    @property
    def subject_alt_names(self) -> tuple[str, ...]:
        if not self.certificate:
            return ()
        return tuple(
            value
            for kind, value in self.certificate.get("subjectAltName", ())
            if kind == "DNS"
        )


def _server_context(certificate: Path, key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(certfile=str(certificate), keyfile=str(key))
    except (OSError, ssl.SSLError) as exc:
        raise LocalTLSCheckError(
            f"the certificate and key could not be loaded together: {exc}"
        ) from exc
    return context


def _client_context(ca_bundle: Path | None) -> ssl.SSLContext:
    """A verifying client context: the worker's trust store, unmodified.

    ``ca_bundle`` exists for the test suite, which needs to point this at a
    throwaway CA. An operator never passes it, and passing it does not relax
    verification -- it only changes which roots count as trusted.
    """

    context = ssl.create_default_context()
    if ca_bundle is not None:
        context.load_verify_locations(cafile=str(ca_bundle))
    return context


def handshake_against(
    certificate: Path,
    key: Path,
    server_name: str,
    *,
    ca_bundle: Path | None = None,
    timeout: float = 10.0,
) -> HandshakeResult:
    """Serve ``certificate`` on loopback and connect to it as a stock client.

    The client checks the chain and the hostname exactly as a worker does.
    Nothing leaves this machine: both ends of the connection are this process.
    """

    server_context = _server_context(certificate, key)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(timeout)
    port = listener.getsockname()[1]

    def _serve() -> None:
        try:
            connection, _address = listener.accept()
        except OSError:
            return
        with connection:
            try:
                with server_context.wrap_socket(connection, server_side=True) as tls:
                    tls.recv(1)
            except (OSError, ssl.SSLError):
                # The client rejecting us is the answer, not an error here.
                pass

    thread = threading.Thread(target=_serve, name="tls-local-check", daemon=True)
    thread.start()
    try:
        client_context = _client_context(ca_bundle)
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw:
            with client_context.wrap_socket(raw, server_hostname=server_name) as tls:
                peer = tls.getpeercert() or {}
                tls.send(b"\x00")
        return HandshakeResult(True, server_name, certificate=peer)
    except ssl.SSLCertVerificationError as exc:
        return HandshakeResult(
            False, server_name, error=exc.verify_message or str(exc)
        )
    except (OSError, ssl.SSLError) as exc:
        return HandshakeResult(
            False, server_name, error=f"{type(exc).__name__}: {exc}"
        )
    finally:
        thread.join(timeout=timeout)
        listener.close()


_SELF_SIGNED_NOTE = (
    "If this is a self-signed certificate, or one from a CA you created "
    "yourself, that is the reason and it cannot be fixed from the worker's "
    "side. A worker trusts the certifi bundle and nothing else, and builds "
    "its client with trust_env=False, so no environment variable adds a "
    "private CA to it. Obtain a publicly trusted certificate instead:\n"
    "  Path A:  tailscale cert YOUR-HOST.YOUR-TAILNET.ts.net\n"
    "  Path B:  let Caddy issue one -- see deploy/Caddyfile.public"
)


def render(result: HandshakeResult) -> str:
    lines: list[str] = []
    if not result.trusted:
        lines.append("REJECTED -- a worker would refuse this certificate.")
        lines.append("")
        lines.append(f"  name checked : {result.server_name}")
        lines.append(f"  reason       : {result.error}")
        lines.append("")
        lines.append(
            "A contributor running the installer against this coordinator "
            "would see it fail here, before any credential is sent."
        )
        lines.append("")
        lines.append(_SELF_SIGNED_NOTE)
        return "\n".join(lines)

    days = result.days_remaining
    lines.append("ACCEPTED -- a stock client trusts this certificate.")
    lines.append("")
    lines.append(f"  name checked : {result.server_name}")
    names = result.subject_alt_names
    if names:
        lines.append(f"  valid for    : {', '.join(names)}")
    if days is not None:
        lines.append(f"  expires in   : {days} days")
    lines.append("")
    lines.append(
        "The chain is complete, a public CA signed it, and it matches the "
        "name above. That is every certificate failure a joining worker can "
        "hit, so the installer will get past this point."
    )
    if days is not None and days < CERT_WARN_DAYS:
        lines.append("")
        lines.append(
            f"Warning: {days} days is short. Renewal normally happens at 30. "
            "Check that whatever renews this is actually running:\n"
            "  sudo systemctl status caddy"
        )
    lines.append("")
    lines.append(
        "Still to confirm, once the proxy is running: that the certificate is "
        "the one the proxy serves, that DNS points here, and that 443 is "
        "open. Run:\n"
        "  python3 scripts/deploy_preflight.py --state-dir data "
        f"--url https://{result.server_name}"
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check locally whether a Mycelium worker would accept a "
            "certificate."
        )
    )
    parser.add_argument("--cert", required=True, type=Path, help="certificate chain (PEM)")
    parser.add_argument("--key", required=True, type=Path, help="private key (PEM)")
    parser.add_argument(
        "--name",
        required=True,
        help="the hostname a contributor would be given, e.g. mycelium.example.com",
    )
    parser.add_argument(
        "--ca-bundle",
        type=Path,
        help=argparse.SUPPRESS,  # test-suite use; see _client_context
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    for label, path in (("certificate", args.cert), ("key", args.key)):
        if not path.is_file():
            print(f"No {label} at {path}", file=sys.stderr)
            return 2
    try:
        result = handshake_against(
            args.cert,
            args.key,
            args.name,
            ca_bundle=args.ca_bundle,
            timeout=args.timeout,
        )
    except LocalTLSCheckError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(render(result))
    return 0 if result.trusted else 1


if __name__ == "__main__":
    raise SystemExit(main())
