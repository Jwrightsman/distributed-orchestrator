"""Transport policy for the Mycelium worker: TLS, or loopback, or nothing.

A worker sends a bearer enrollment credential to its coordinator and receives
task text back. Over plaintext HTTP across a network, both are readable and
both are rewritable by anything on the path. Every earlier version of this
project treated that as an operator judgement call — the deploy documentation
recommended TLS or a private overlay, and the worker accepted whatever origin
it was handed.

That is not a judgement a contributor is in a position to make. Somebody being
invited to donate a laptop cannot audit the operator's overlay ACL, and an
opt-out escape hatch is used by exactly the people who most need it not to
exist. So the policy is mechanical and has no override: **plaintext HTTP is
refused for every host except loopback.**

Loopback stays open because a developer running a coordinator and a worker on
one machine has no network to protect, and closing that would only push people
toward self-signed certificates and disabled verification — the failure this
module exists to prevent.

There is deliberately no flag, no environment variable, and no configuration key
that relaxes this. Adding one would defeat the entire purpose of the module; a
test asserts that none exists.
"""

from __future__ import annotations

import ipaddress


class InsecureTransportError(RuntimeError):
    """A coordinator origin would send credentials in clear text."""


#: The one message a contributor sees. It has to say what was refused, why it
#: matters to *them*, and which person can fix it — because the contributor
#: usually cannot: the certificate belongs to whoever runs the coordinator.
INSECURE_TRANSPORT_HELP = (
    "This address starts with http://, which sends everything in the clear. "
    "Your invitation code and the work your machine does would both be "
    "readable by anyone between you and the coordinator.\n"
    "Ask whoever invited you for an https:// address. Nothing about this can "
    "be turned off from your side, and that is deliberate."
)


def is_loopback_host(hostname: str) -> bool:
    """Is this host unambiguously the local machine?

    Two forms count. An IP literal the standard library calls loopback
    (``127.0.0.0/8`` and ``::1``, plus the IPv4-mapped IPv6 spelling of the
    former, which ``ipaddress`` does not resolve on its own). And the reserved
    ``localhost`` name from RFC 6761, which resolvers are required to keep off
    the wire, together with names beneath it such as ``app.localhost``.

    Anything a resolver could point somewhere else is not loopback, however
    local it looks. ``mycoordinator.local`` is mDNS, not this.
    """

    host = (hostname or "").strip().rstrip(".").lower()
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or host.endswith(".localhost")
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback


def require_secure_transport(scheme: str, hostname: str) -> None:
    """Raise unless this scheme/host pair can carry a credential safely.

    ``https`` is accepted for every host: certificate verification is httpx's
    default and nothing in this project turns it off. ``http`` is accepted only
    for loopback.
    """

    normalized = (scheme or "").strip().lower()
    if normalized == "https":
        return
    if normalized == "http":
        if is_loopback_host(hostname):
            return
        raise InsecureTransportError(
            f"refusing plaintext http:// to {hostname}. {INSECURE_TRANSPORT_HELP}"
        )
    raise InsecureTransportError(
        f"coordinator URL must use https (or http for loopback), not {normalized or 'an empty scheme'}"
    )
