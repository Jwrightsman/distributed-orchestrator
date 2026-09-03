"""The worker-protocol compatibility window.

A distributed node population cannot be upgraded at once. Before external
operators depend on this coordinator, a worker needs a defined answer to "what
happens when the other side has changed" — and that answer has to be the same
answer every time, machine-readable, and delivered before anything durable is
created.

This module is the whole of that mechanism: a window, a classifier, and three
stable error codes. It has no configuration key, selects no implementation, and
holds no state. `routes_nodes` calls `classify` twice per registration — once
before enrolment and once before the session grant — and nothing calls it per
request.

Bumping `NODE_PROTOCOL_MAX` is a deliberate act with a documented process behind
it; see ADR 0015 and the deprecation policy in `docs/PROTOCOL.md`. This module
defines the mechanism and deliberately does not exercise it: min and max are both
1, so the window admits exactly what it admitted before.
"""

from __future__ import annotations

from typing import Literal


# The coordinator's own version, advertised beside the window so an operator can
# say which coordinator refused them. It is a version string and nothing else:
# no build fingerprint, no host, no deployment mode.
SERVER_VERSION = "0.3.0"

# The inclusive window of worker protocol versions this coordinator admits.
# Both ends are the same today, which is the honest state of a protocol with one
# shipped version.
NODE_PROTOCOL_MIN = 1
NODE_PROTOCOL_MAX = 1

# The version a descriptor is assumed to declare when it omits the field. It
# must stay inside the window: an existing worker that never sent the field is
# not a worker that has become incompatible.
DEFAULT_WORKER_PROTOCOL_VERSION = "1"

MAX_VERSION_TOKEN_LENGTH = 8

WorkerProtocolVerdict = Literal["supported", "too_old", "too_new", "malformed"]

# Stable machine-readable codes. Too-old and too-new are deliberately distinct:
# an operator running behind needs to upgrade their worker, and an operator
# running ahead needs to know the coordinator is the stale side. Telling them
# apart is the difference between useful advice and a shrug.
CODE_TOO_OLD = "worker_protocol_version_too_old"
CODE_TOO_NEW = "worker_protocol_version_too_new"
CODE_MALFORMED = "invalid_worker_protocol_version"

ACTION_UPGRADE_WORKER = "upgrade_worker"
ACTION_UPGRADE_COORDINATOR = "upgrade_coordinator"


def supported_versions() -> tuple[str, ...]:
    """Every version inside the window, low to high."""

    return tuple(
        str(value) for value in range(NODE_PROTOCOL_MIN, NODE_PROTOCOL_MAX + 1)
    )


def parse_version(value: object) -> int | None:
    """Return the integer form of a declared version, or None if malformed.

    Deliberately strict. A version is a short decimal integer with no sign, no
    padding, and no whitespace, because anything looser turns a comparison into a
    guess.
    """

    if not isinstance(value, str):
        return None
    if not value or len(value) > MAX_VERSION_TOKEN_LENGTH:
        return None
    if not value.isdigit():
        return None
    if value != value.lstrip("0") and value != "0":
        return None
    parsed = int(value)
    return parsed if parsed >= 0 else None


def classify(value: object) -> WorkerProtocolVerdict:
    """Decide one declared worker protocol version against the window."""

    parsed = parse_version(value)
    if parsed is None:
        return "malformed"
    if parsed < NODE_PROTOCOL_MIN:
        return "too_old"
    if parsed > NODE_PROTOCOL_MAX:
        return "too_new"
    return "supported"


def window() -> dict[str, object]:
    """The advertised window. Versions only — nothing about this deployment."""

    return {
        "node_protocol_min": str(NODE_PROTOCOL_MIN),
        "node_protocol_max": str(NODE_PROTOCOL_MAX),
        "supported_worker_protocol_versions": list(supported_versions()),
        "server_version": SERVER_VERSION,
    }


def refusal_detail(verdict: WorkerProtocolVerdict, declared: object) -> dict[str, object]:
    """Build the refusal body for an unsupported peer.

    The declared value is echoed only when it is a well-formed version token, so
    a malformed declaration cannot reflect arbitrary text back to its sender.
    """

    detail: dict[str, object] = {
        "node_protocol_min": str(NODE_PROTOCOL_MIN),
        "node_protocol_max": str(NODE_PROTOCOL_MAX),
        "server_version": SERVER_VERSION,
    }
    if verdict == "malformed":
        detail["code"] = CODE_MALFORMED
        detail["message"] = (
            "worker_protocol_version must be a short decimal integer. Supported "
            f"versions are {NODE_PROTOCOL_MIN} through {NODE_PROTOCOL_MAX}."
        )
        return detail

    detail["declared_worker_protocol_version"] = str(declared)
    if verdict == "too_old":
        detail["code"] = CODE_TOO_OLD
        detail["action"] = ACTION_UPGRADE_WORKER
        detail["message"] = (
            f"This worker speaks protocol {declared}, which this coordinator no "
            f"longer supports. Supported versions are {NODE_PROTOCOL_MIN} through "
            f"{NODE_PROTOCOL_MAX}. Upgrade the worker and register again."
        )
        return detail

    detail["code"] = CODE_TOO_NEW
    detail["action"] = ACTION_UPGRADE_COORDINATOR
    detail["message"] = (
        f"This worker speaks protocol {declared}, which is newer than this "
        f"coordinator supports. Supported versions are {NODE_PROTOCOL_MIN} "
        f"through {NODE_PROTOCOL_MAX}. Upgrade the coordinator, or run a worker "
        "at a supported version."
    )
    return detail
