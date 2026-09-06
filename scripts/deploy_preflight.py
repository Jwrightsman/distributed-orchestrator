#!/usr/bin/env python3
"""Host preflight for a Mycelium coordinator, run on the coordinator.

`scripts/preflight.py` reads `config.json` and answers "is this configuration
coherent". It cannot see the machine. This script answers the other half: is
this *host* in a state where inviting somebody is a reasonable thing to do.

It is read-only. It opens no file for writing, changes no setting, installs
nothing, and starts nothing. Every finding names the exact command that fixes
it and says, in a sentence, what goes wrong if you don't. The intended reader
has never administered a server.

It never prints a credential value. Secrets are reported as presence, an
entropy class, and a file mode -- never content, never a prefix, never an
excerpt.

Network use is one optional HTTPS GET to a coordinator address you supply, to
see whether a worker could read the protocol window. It opens no shell
anywhere, connects to no host you did not name, and there is no
remote-administration path in this file.

    python3 scripts/deploy_preflight.py --state-dir data
    python3 scripts/deploy_preflight.py --state-dir data --url https://your-domain
    python3 scripts/deploy_preflight.py --state-dir data --json

Exit status is 0 when nothing failed, 1 when any check failed. Warnings alone
do not fail the run; anything that would expose a credential is a failure
rather than a warning.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import socket
import ssl
import stat
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: The port the coordinator listens on inside the container and on loopback.
DEFAULT_APP_PORT = 8000

#: Below this, a certificate is close enough to expiry that workers will start
#: failing before an operator notices. Let's Encrypt renews at 30 days.
CERT_WARN_DAYS = 21

#: Shannon entropy, in bits, that an admission secret needs. 128 is the floor
#: below which an attacker's guessing budget starts to matter at all;
#: `secrets.token_urlsafe(32)` scores about 220 under the conservative
#: estimator below, so a generated value clears this comfortably and a
#: hand-typed passphrase usually does not.
MIN_SECRET_ENTROPY_BITS = 128

PASS = "pass"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"


@dataclass(frozen=True)
class Finding:
    """One checked fact, why it matters, and the command that fixes it."""

    name: str
    status: str
    summary: str
    why: str = ""
    fix: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


# -- Listening sockets -------------------------------------------------------
#
# Read from /proc rather than from `ss`, because /proc is the kernel's own
# answer, needs no package installed and no root, and cannot be confused by a
# firewall's opinion. `ss -tlnp` is what the operator is told to run, because
# it also names the process holding the socket.

_LOOPBACK_V4_PREFIX = "127."
_TCP_LISTEN = "0A"  # the kernel's hex code for TCP_LISTEN


def _decode_proc_address(raw: str, *, ipv6: bool) -> tuple[str, int] | None:
    """Turn one ``/proc/net/tcp`` address field into ``(host, port)``."""

    hex_host, _, hex_port = raw.partition(":")
    if not hex_port:
        return None
    try:
        port = int(hex_port, 16)
        packed = bytes.fromhex(hex_host)
    except ValueError:
        return None
    if ipv6:
        if len(packed) != 16:
            return None
        # Each four-byte group is little-endian in this file; the groups
        # themselves are in order.
        ordered = b"".join(packed[i : i + 4][::-1] for i in range(0, 16, 4))
        return socket.inet_ntop(socket.AF_INET6, ordered), port
    if len(packed) != 4:
        return None
    return socket.inet_ntop(socket.AF_INET, packed[::-1]), port


def listening_sockets() -> list[tuple[str, int]] | None:
    """Every TCP socket in LISTEN, or ``None`` where /proc is unavailable."""

    found: set[tuple[str, int]] = set()
    readable = False
    for name, ipv6 in (("tcp", False), ("tcp6", True)):
        try:
            text = (Path("/proc/net") / name).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
        readable = True
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != _TCP_LISTEN:
                continue
            decoded = _decode_proc_address(fields[1], ipv6=ipv6)
            if decoded is not None:
                found.add(decoded)
    return sorted(found) if readable else None


def running_in_a_container() -> bool:
    """Is this process inside a container rather than on the host?

    It matters for exactly one thing, and getting it wrong is worse than not
    checking. A container has its own network namespace: the coordinator binds
    0.0.0.0 *inside* it, which is correct and necessary, while Docker publishes
    it to 127.0.0.1 on the host. Reading /proc/net/tcp from in here therefore
    reports a "publicly bound" port on a deployment that is bound to loopback,
    and an operator who follows the documented
    `docker compose exec ... deploy_preflight.py` would be shown an alarming
    failure about a correct configuration.

    The state directory and credential checks stay meaningful from inside,
    because /data is the same bind mount either way. Only the socket table is
    namespaced, so only the socket table is withheld.
    """

    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(marker in cgroup for marker in ("docker", "containerd", "kubepods"))


def is_public_bind(host: str) -> bool:
    """Can another machine reach a socket bound to this address?

    A wildcard can. Loopback cannot. A specific address counts as public,
    because a Tailscale or LAN address is still some other machine's route to
    the port.
    """

    if host in {"0.0.0.0", "::", "::0"}:
        return True
    if host.startswith(_LOOPBACK_V4_PREFIX) or host == "::1":
        return False
    if host.startswith("::ffff:") and host[7:].startswith(_LOOPBACK_V4_PREFIX):
        return False
    return True


def check_listening_ports(app_port: int) -> list[Finding]:
    if running_in_a_container():
        return [
            Finding(
                "listening_ports",
                SKIP,
                "this is a container's network namespace, not the host's",
                why=(
                    "Inside the container the coordinator binds 0.0.0.0, which "
                    "is correct: the container has its own private network, "
                    "and Docker publishes that port to 127.0.0.1 on the host. "
                    "Reading the socket table from in here would report a "
                    "public port on a deployment that is bound to loopback, so "
                    "it is not reported at all.\n"
                    "Run the two commands below on the host itself. That is "
                    "the namespace the Internet reaches."
                ),
                fix=(
                    f"ss -tlnp | grep :{app_port}   # expect 127.0.0.1 only\n"
                    "sudo iptables -L DOCKER -n    # expect no rule for it\n"
                    "# or run this script on the host rather than in the container"
                ),
            )
        ]

    sockets = listening_sockets()
    if sockets is None:
        return [
            Finding(
                "listening_ports",
                SKIP,
                "cannot read /proc/net/tcp on this platform",
                why=(
                    "This check reads the kernel's own socket table, which "
                    "only exists on Linux. Run this script on the coordinator."
                ),
                fix="ss -tlnp",
            )
        ]

    public = sorted({(host, port) for host, port in sockets if is_public_bind(host)})
    public_ports = sorted({port for _host, port in public})
    findings = [
        Finding(
            "listening_ports",
            PASS,
            "publicly bound TCP ports: "
            + (", ".join(str(port) for port in public_ports) or "none"),
            why=(
                "Every port in this list is a way in. A port you did not "
                "intend to publish is the ordinary way a machine gets taken "
                "over -- not a clever attack on the application, but an old "
                "service nobody remembered was running."
            ),
            fix="ss -tlnp",
            detail={
                "public": [f"{host}:{port}" for host, port in public],
                "public_ports": public_ports,
            },
        )
    ]

    unexpected = [port for port in public_ports if port not in (22, 80, 443)]
    if unexpected:
        findings.append(
            Finding(
                "unexpected_public_ports",
                WARN,
                "publicly bound ports beyond 22/80/443: "
                + ", ".join(str(port) for port in unexpected),
                why=(
                    "A coordinator needs 22 for you and 80 and 443 for the "
                    "reverse proxy. Anything else is reachable by strangers "
                    "and is probably not meant to be."
                ),
                fix=(
                    "ss -tlnp   # find out what it is first, then:\n"
                    "sudo systemctl stop THE-SERVICE && "
                    "sudo systemctl disable THE-SERVICE"
                ),
                detail={"ports": unexpected},
            )
        )
    else:
        findings.append(
            Finding(
                "unexpected_public_ports",
                PASS,
                "nothing beyond 22/80/443 is bound publicly",
                why="22 for you, 80 and 443 for the reverse proxy, nothing else.",
            )
        )

    app_public = [f"{host}:{port}" for host, port in public if port == app_port]
    if app_public:
        findings.append(
            Finding(
                "coordinator_port_private",
                FAIL,
                f"the coordinator's own port {app_port} is bound publicly on "
                + ", ".join(app_public),
                why=(
                    "Anyone on the Internet can talk to the application "
                    "directly, going around the proxy and therefore around "
                    "TLS. The certificate, the security headers and the "
                    "request limits are all decoration while this is true, and "
                    "an invitation code sent to this port crosses the network "
                    "in clear text.\n"
                    f"`ufw deny {app_port}` does NOT fix this when the port is "
                    "published by Docker. Docker writes its own iptables "
                    "rules, and the kernel evaluates them before the ones ufw "
                    "writes, so ufw reports 'deny' while the port keeps "
                    "answering. Check with the commands below, not with "
                    "`ufw status`."
                ),
                fix=(
                    "In docker-compose.yml publish the port as "
                    f'"127.0.0.1:{app_port}:{app_port}" (this repository '
                    "does), then:\n"
                    "docker compose up -d\n"
                    f"ss -tlnp | grep :{app_port}      # expect 127.0.0.1 only\n"
                    "sudo iptables -L DOCKER -n       # expect no rule for it"
                ),
                detail={"bound": app_public},
            )
        )
    else:
        findings.append(
            Finding(
                "coordinator_port_private",
                PASS,
                f"port {app_port} is not bound on any public interface",
                why=(
                    "The application can only be reached through the reverse "
                    "proxy, which is what makes the certificate mean anything."
                ),
                fix=f"ss -tlnp | grep :{app_port}",
            )
        )
    return findings


# -- SSH ---------------------------------------------------------------------

_SSHD_CONFIG = Path("/etc/ssh/sshd_config")
_SSHD_CONFIG_DIR = Path("/etc/ssh/sshd_config.d")


def sshd_directives(
    config: Path = _SSHD_CONFIG, config_dir: Path = _SSHD_CONFIG_DIR
) -> dict[str, str] | None:
    """Last-wins reading of the sshd configuration files we can read.

    `sshd -T` is the authoritative answer but wants root and a running daemon.
    Reading the files gives the same answer for the two settings that matter
    here, and a host with no readable configuration is reported rather than
    assumed to be safe.
    """

    files: list[Path] = []
    if config.is_file():
        files.append(config)
    if config_dir.is_dir():
        files.extend(sorted(config_dir.glob("*.conf")))
    if not files:
        return None
    directives: dict[str, str] = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split(None, 1)
            if len(parts) != 2:
                continue
            directives[parts[0].lower()] = parts[1].strip().split()[0].lower()
    return directives


_ROOT_LOGIN_ACCEPTABLE = {
    "no",
    "prohibit-password",
    "without-password",
    "forced-commands-only",
}


def check_ssh(
    config: Path = _SSHD_CONFIG, config_dir: Path = _SSHD_CONFIG_DIR
) -> list[Finding]:
    directives = sshd_directives(config, config_dir)
    if directives is None:
        return [
            Finding(
                "ssh_config",
                SKIP,
                "no readable sshd configuration on this host",
                why=(
                    "This check needs /etc/ssh/sshd_config. Run the script on "
                    "the coordinator, as a user that can read it."
                ),
                fix="sudo sshd -T | grep -E 'passwordauthentication|permitrootlogin'",
            )
        ]

    findings: list[Finding] = []
    password_auth = directives.get("passwordauthentication", "yes")
    if password_auth == "no":
        findings.append(
            Finding(
                "ssh_password_authentication",
                PASS,
                "SSH password authentication is off",
                why=(
                    "Only your key can log in, so a guessed password is not a "
                    "way in."
                ),
                detail={"value": password_auth},
            )
        )
    else:
        findings.append(
            Finding(
                "ssh_password_authentication",
                FAIL,
                f"SSH password authentication is {password_auth}",
                why=(
                    "Every server with a public address is being tried, "
                    "constantly, against lists of common passwords. A password "
                    "is a thing that can be guessed; a key is not. Someone who "
                    "gets in this way can read config.json, which holds all "
                    "three of this deployment's credentials in clear text."
                ),
                fix=(
                    "Confirm your key works in a SECOND terminal first, or you "
                    "can lock yourself out:\n"
                    "echo 'PasswordAuthentication no' | "
                    "sudo tee /etc/ssh/sshd_config.d/99-mycelium.conf\n"
                    "sudo systemctl reload ssh"
                ),
                detail={"value": password_auth},
            )
        )

    root_login = directives.get("permitrootlogin", "prohibit-password")
    if root_login in _ROOT_LOGIN_ACCEPTABLE:
        findings.append(
            Finding(
                "ssh_root_login",
                PASS if root_login == "no" else WARN,
                f"PermitRootLogin is {root_login}",
                why=(
                    "Root can change anything on the machine. `no` is the "
                    "setting to want; `prohibit-password` still allows a key "
                    "login as root, which is survivable but unnecessary."
                ),
                fix=(
                    "echo 'PermitRootLogin no' | "
                    "sudo tee -a /etc/ssh/sshd_config.d/99-mycelium.conf\n"
                    "sudo systemctl reload ssh"
                ),
                detail={"value": root_login},
            )
        )
    else:
        findings.append(
            Finding(
                "ssh_root_login",
                FAIL,
                f"PermitRootLogin is {root_login}",
                why=(
                    "Root login with a password is the most attacked door on "
                    "the Internet, and root can read and change everything on "
                    "this machine, including every credential this deployment "
                    "has."
                ),
                fix=(
                    "echo 'PermitRootLogin no' | "
                    "sudo tee -a /etc/ssh/sshd_config.d/99-mycelium.conf\n"
                    "sudo systemctl reload ssh"
                ),
                detail={"value": root_login},
            )
        )
    return findings


# -- Unattended upgrades -----------------------------------------------------

_AUTO_UPGRADES = Path("/etc/apt/apt.conf.d/20auto-upgrades")
_UNATTENDED_ENABLED = re.compile(r'APT::Periodic::Unattended-Upgrade\s+"1"\s*;')


def check_unattended_upgrades(
    apt_dir: Path = Path("/etc/apt"), auto_upgrades: Path = _AUTO_UPGRADES
) -> list[Finding]:
    if not apt_dir.is_dir():
        return [
            Finding(
                "unattended_upgrades",
                SKIP,
                "not a Debian or Ubuntu host",
                why="This check reads Debian and Ubuntu's apt configuration.",
            )
        ]
    text = ""
    if auto_upgrades.is_file():
        try:
            text = auto_upgrades.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    if _UNATTENDED_ENABLED.search(text):
        return [
            Finding(
                "unattended_upgrades",
                PASS,
                "unattended security upgrades are enabled",
                why=(
                    "Security fixes install themselves, so a coordinator you "
                    "forget about for three months stays patched."
                ),
            )
        ]
    return [
        Finding(
            "unattended_upgrades",
            WARN,
            "unattended security upgrades are not enabled",
            why=(
                "Security fixes arrive only when you remember to run apt. The "
                "realistic failure here is not an exotic attack -- it is a "
                "hole that was published and patched months ago, on a machine "
                "nobody logged into since."
            ),
            fix=(
                "sudo apt install unattended-upgrades && "
                "sudo dpkg-reconfigure -plow unattended-upgrades"
            ),
        )
    ]


# -- File permissions --------------------------------------------------------


def _octal(path: Path) -> str:
    return format(stat.S_IMODE(path.stat().st_mode), "04o")


def _who(offending: int) -> str:
    """Name the accounts a permission bit actually hands access to."""

    who = []
    if offending & 0o070:
        who.append("its group")
    if offending & 0o007:
        who.append("every other account on this machine")
    return " and ".join(who)


def check_file_modes(state_dir: Path, config_path: Path) -> list[Finding]:
    if os.name != "posix":
        return [
            Finding(
                "file_permissions",
                SKIP,
                "POSIX file modes are not meaningful on this platform",
                why=(
                    "Windows uses ACLs rather than modes. Run this script on "
                    "the coordinator, which is Linux."
                ),
            )
        ]

    findings: list[Finding] = []

    # The directory is checked first, and the answer changes how the files
    # inside it are judged. A 0700 directory denies traversal to every other
    # account, so a file at 0644 underneath it is not actually reachable by
    # them. SQLite creates events.db at 0644 and nothing in this project can
    # stop it, so treating that as an exposure regardless of the directory
    # would make every correct deployment fail this check forever -- which is
    # how a preflight teaches people to ignore it.
    directory_open = True
    if not state_dir.exists():
        findings.append(
            Finding(
                "state_directory",
                SKIP,
                f"{state_dir} does not exist yet",
                why="Nothing to check until the coordinator has run once.",
            )
        )
    else:
        offending = stat.S_IMODE(state_dir.stat().st_mode) & 0o077
        directory_open = bool(offending)
        why = (
            "Everything recoverable lives here: the settings file, the "
            "database of every enrollment, and every run's output."
        )
        if offending:
            findings.append(
                Finding(
                    "state_directory",
                    FAIL,
                    f"{state_dir} is mode {_octal(state_dir)}, reachable by "
                    + _who(offending),
                    why=(
                        why + " While this directory is open, every file "
                        "underneath it is only as protected as its own mode, "
                        "and some of them are written by SQLite rather than by "
                        "this project. Closing the directory protects all of "
                        "them at once."
                    ),
                    fix=f"chmod 700 {state_dir}",
                    detail={"mode": _octal(state_dir)},
                )
            )
        else:
            findings.append(
                Finding(
                    "state_directory",
                    PASS,
                    f"{state_dir} is mode {_octal(state_dir)}, owner only",
                    why=(
                        why + " No other account on this machine can traverse "
                        "into it, which protects everything underneath "
                        "whatever its own mode happens to be."
                    ),
                    detail={"mode": _octal(state_dir)},
                )
            )

    files: tuple[tuple[str, Path, str], ...] = (
        (
            "config_file",
            config_path,
            "This file holds viewer_key, pitch_key and node_secret in clear "
            "text. Any other account that can read it holds all three.",
        ),
        (
            "events_database",
            state_dir / "events.db",
            "The database holds enrollment records, task text and results. "
            "Reading it is reading everything this network has ever done.",
        ),
    )

    for name, path, why in files:
        if not path.exists():
            findings.append(
                Finding(
                    name,
                    SKIP,
                    f"{path} does not exist yet",
                    why="Nothing to check until the coordinator has run once.",
                )
            )
            continue
        offending = stat.S_IMODE(path.stat().st_mode) & 0o077
        if not offending:
            findings.append(
                Finding(
                    name,
                    PASS,
                    f"{path} is mode {_octal(path)}, owner only",
                    why=why,
                    detail={"mode": _octal(path)},
                )
            )
        elif directory_open:
            findings.append(
                Finding(
                    name,
                    FAIL,
                    f"{path} is mode {_octal(path)} inside an open directory, "
                    "so it is readable by " + _who(offending),
                    why=(
                        why + " A credential another local account can read is "
                        "a credential you have to assume is known."
                    ),
                    fix=f"chmod 700 {state_dir}\nchmod 600 {path}",
                    detail={"mode": _octal(path)},
                )
            )
        else:
            findings.append(
                Finding(
                    name,
                    WARN,
                    f"{path} is mode {_octal(path)}, but the directory above "
                    "it is owner-only",
                    why=(
                        why + " Nothing can reach it today, because no other "
                        "account can traverse the directory. Tightening the "
                        "file as well means one loosened directory does not "
                        "expose it later."
                    ),
                    fix=f"chmod 600 {path}",
                    detail={"mode": _octal(path)},
                )
            )
    return findings


# -- Admission secret --------------------------------------------------------


def shannon_entropy_bits(value: str) -> float:
    """Entropy of a string treated as a sample from its own characters.

    Correct in one direction only, and it matters which. For a generated
    token it under-reports -- a 43-character `token_urlsafe` value scores
    around 220 rather than its true 256 -- which is the safe direction.

    For *prose* it wildly over-reports, because character frequency knows
    nothing about words: a nine-word English phrase scores over 200 bits here
    while its real guessing entropy is nearer 100. That is the unsafe
    direction, and it is why `looks_generated` runs in front of this rather
    than this number being trusted on its own.
    """

    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for character in value:
        counts[character] = counts.get(character, 0) + 1
    length = len(value)
    per_character = -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )
    return per_character * length


_PLACEHOLDER_MARKERS = (
    "changeme",
    "change-me",
    "your-",
    "example",
    "placeholder",
    "replace",
    "password",
    "xxxx",
    "<",
)


def looks_like_a_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


_WORD_SEPARATOR = re.compile(r"[\s._-]+")


def looks_generated(value: str) -> bool:
    """Does this look like it came out of a generator rather than a keyboard?

    The check exists because character-frequency entropy cannot tell a random
    token from an English sentence -- the sentence scores higher. Rather than
    trying to price a passphrase, this refuses to accept one: the admission
    secret is never something a person needs to type or remember, `deploy.sh`
    generates it, and "you typed this yourself" is both easy to detect and
    honest advice.

    A value is taken as generated when it has no whitespace and does not
    decompose into a run of ordinary alphabetic words.
    """

    if any(character.isspace() for character in value):
        return False
    segments = [segment for segment in _WORD_SEPARATOR.split(value) if segment]
    if len(segments) >= 2 and all(segment.isalpha() for segment in segments):
        return False
    return True


def classify_secret(value: object) -> tuple[str, str, float]:
    """Return ``(status, entropy class, bits)`` for one configured authority.

    The value itself never leaves this function.
    """

    if not isinstance(value, str) or not value.strip():
        return FAIL, "empty or unset", 0.0
    candidate = value.strip()
    bits = shannon_entropy_bits(candidate)
    if looks_like_a_placeholder(candidate):
        return FAIL, "a placeholder from the documentation", bits
    if len(candidate) < 32:
        return FAIL, "shorter than the 32-character minimum", bits
    if len(set(candidate)) < 8:
        return FAIL, "very low entropy", bits
    if not looks_generated(candidate):
        return FAIL, "typed rather than generated", bits
    if bits < MIN_SECRET_ENTROPY_BITS:
        return FAIL, "low entropy", bits
    return PASS, "strong", bits


_AUTHORITIES: tuple[tuple[str, str, str], ...] = (
    (
        "node_secret",
        "the invitation code",
        "This is the only thing between the Internet and an enrolled worker. "
        "Somebody who guesses it enrolls a machine of their own, which then "
        "receives the task text your network is working on. Nothing "
        "rate-limits guesses at it, so its strength is the whole defence.",
    ),
    (
        "pitch_key",
        "the key that spends your compute",
        "Whoever holds it can queue work onto every machine that volunteered "
        "for you.",
    ),
    (
        "viewer_key",
        "the key that reads everything",
        "It opens every task, result, artifact and node page on this "
        "coordinator.",
    ),
)


def check_secrets(config_path: Path) -> list[Finding]:
    if not config_path.is_file():
        return [
            Finding(
                "admission_secret",
                SKIP,
                f"{config_path} does not exist",
                why="Nothing to check until the coordinator is configured.",
                fix="./deploy.sh",
            )
        ]
    try:
        settings = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [
            Finding(
                "admission_secret",
                FAIL,
                f"{config_path} could not be read as JSON ({type(exc).__name__})",
                why=(
                    "The coordinator refuses to start on a broken settings "
                    "file, so this has to be fixed before anything else here "
                    "means anything."
                ),
                fix=f"python3 -m json.tool {config_path} > /dev/null",
            )
        ]
    if not isinstance(settings, dict):
        settings = {}

    regenerate = (
        'python3 -c "from config import ensure_trusted_alpha_config as e; '
        f"e('{config_path}')\"\n"
        "# then send the new invitation code to your workers"
    )

    findings: list[Finding] = []
    values: dict[str, str] = {}
    for authority, label, why in _AUTHORITIES:
        raw = settings.get(authority)
        status, entropy_class, bits = classify_secret(raw)
        if isinstance(raw, str) and raw.strip():
            values[authority] = raw.strip()
        findings.append(
            Finding(
                f"secret_{authority}",
                status,
                # The bit count is only quoted where it means something. For a
                # typed phrase this estimator reads high and would reassure
                # exactly the reader who should not be reassured.
                f"{authority} ({label}): {entropy_class}"
                + (
                    f", about {bits:.0f} bits"
                    if entropy_class in {"strong", "low entropy"}
                    else ""
                ),
                why=(
                    why
                    if status == PASS
                    else why + " A value that is empty, a placeholder, short "
                    "or guessable is not protecting any of that."
                ),
                fix="" if status == PASS else regenerate,
                detail={
                    "entropy_class": entropy_class,
                    "entropy_bits": round(bits),
                    "minimum_bits": MIN_SECRET_ENTROPY_BITS,
                },
            )
        )

    if len(values) == 3 and len(set(values.values())) < 3:
        findings.append(
            Finding(
                "authority_separation",
                FAIL,
                "two or more of the three authorities share one value",
                why=(
                    "The three keys exist so that giving somebody one does not "
                    "give them the others. Reusing a value collapses that: a "
                    "contributor's invitation code would also read every run "
                    "on the coordinator."
                ),
                fix=regenerate,
            )
        )
    elif len(values) == 3:
        findings.append(
            Finding(
                "authority_separation",
                PASS,
                "the three authorities are distinct values",
                why="Handing out one of them does not hand out the other two.",
            )
        )
    return findings


# -- Certificate and reachability --------------------------------------------


def parse_certificate_time(value: str) -> datetime | None:
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _verifying_context(ca_bundle: Path | None = None) -> ssl.SSLContext:
    """A verifying client context, optionally against a named trust store.

    ``ca_bundle`` exists so the test suite can point these checks at a
    throwaway CA instead of the public Internet. It does not relax
    verification -- it only changes which roots count -- and an operator never
    passes it.
    """

    context = ssl.create_default_context()
    if ca_bundle is not None:
        context.load_verify_locations(cafile=str(ca_bundle))
    return context


def peer_certificate(
    host: str,
    port: int,
    timeout: float,
    *,
    ca_bundle: Path | None = None,
) -> dict[str, Any]:
    """Complete a verified TLS handshake and return the peer certificate.

    Verification is the default context's, untouched. A certificate a stock
    client would reject raises here, which is precisely the answer wanted: a
    contributor's installer is a stock client.
    """

    context = _verifying_context(ca_bundle)
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            return tls.getpeercert() or {}


_CERTIFICATE_ADVICE = (
    "Path A: tailscale cert YOUR-HOST.YOUR-TAILNET.ts.net\n"
    "Path B: let Caddy issue it -- see deploy/Caddyfile.public"
)


def check_certificate(
    url: str, timeout: float, *, ca_bundle: Path | None = None
) -> list[Finding]:
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 443
    if parts.scheme != "https" or not host:
        return [
            Finding(
                "certificate",
                FAIL,
                f"{url} is not an https:// address with a host",
                why=(
                    "A worker refuses plaintext http:// to anything but its "
                    "own machine, with no way to turn that off. Until the "
                    "address starts with https:// nobody can join at all."
                ),
                fix="Pass the address you would give a contributor: "
                "--url https://your-domain",
            )
        ]

    try:
        certificate = peer_certificate(host, port, timeout, ca_bundle=ca_bundle)
    except ssl.SSLCertVerificationError as exc:
        return [
            Finding(
                "certificate",
                FAIL,
                f"the certificate for {host} was rejected: {exc.verify_message}",
                why=(
                    "This is exactly what a contributor's installer will do. A "
                    "Mycelium worker trusts the certifi CA bundle and nothing "
                    "else, and it builds its HTTP client with trust_env=False, "
                    "so no environment variable can add your own CA to it. A "
                    "self-signed certificate can never be made to work here -- "
                    "the certificate has to come from a public CA."
                ),
                fix=_CERTIFICATE_ADVICE,
                detail={"host": host, "error": exc.verify_message},
            )
        ]
    except (OSError, ssl.SSLError) as exc:
        return [
            Finding(
                "certificate",
                FAIL,
                f"could not complete a TLS handshake with {host}:{port} "
                f"({type(exc).__name__})",
                why=(
                    "Nothing answered, or something answered but not with "
                    "TLS. A worker cannot join a coordinator it cannot reach."
                ),
                fix=(
                    "sudo systemctl status caddy\n"
                    "sudo caddy validate --config /etc/caddy/Caddyfile\n"
                    "ss -tlnp | grep -E ':(80|443)'"
                ),
                detail={"host": host, "port": port},
            )
        ]

    not_after = parse_certificate_time(str(certificate.get("notAfter", "")))
    if not_after is None:
        return [
            Finding(
                "certificate",
                WARN,
                f"the certificate for {host} is trusted, but its expiry date "
                "could not be read",
                why=(
                    "A stock client accepted it, which is the half that "
                    "decides whether anybody can join."
                ),
                detail={"host": host},
            )
        ]

    days = (not_after - datetime.now(timezone.utc)).days
    if days >= CERT_WARN_DAYS:
        return [
            Finding(
                "certificate",
                PASS,
                f"the certificate for {host} is trusted and valid for another "
                f"{days} days",
                why=(
                    "A stock client -- which is what a contributor's installer "
                    "is -- accepts this certificate. When it expires every "
                    "worker stops being able to connect at once, so renewal "
                    "has to be automatic rather than remembered."
                ),
                detail={"host": host, "days_remaining": days},
            )
        ]
    return [
        Finding(
            "certificate",
            WARN,
            f"the certificate for {host} expires in {days} days",
            why=(
                "Renewal normally happens at 30 days and has not happened. "
                "When this certificate expires every worker disconnects at "
                "once and the error they see will not explain why."
            ),
            fix=(
                "sudo systemctl restart caddy\n"
                "sudo journalctl -u caddy --since '1 hour ago'"
            ),
            detail={"host": host, "days_remaining": days},
        )
    ]


def check_worker_protocol(
    url: str, timeout: float, *, ca_bundle: Path | None = None
) -> list[Finding]:
    """Read the protocol window the way a joining worker reads it first."""

    endpoint = url.rstrip("/") + "/v1/worker-protocol"
    request = urllib.request.Request(
        endpoint, headers={"User-Agent": "mycelium-deploy-preflight"}
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_verifying_context(ca_bundle)
        ) as response:
            body = response.read(64 * 1024)
            code = response.status
    except urllib.error.HTTPError as exc:
        return [
            Finding(
                "worker_protocol_window",
                FAIL,
                f"GET {endpoint} returned HTTP {exc.code}",
                why=(
                    "This is the very first request a joining worker makes, "
                    "and it carries no credential. If it does not answer, "
                    "nobody can join, and the message they see will not say "
                    "why."
                ),
                fix=(
                    "sudo caddy validate --config /etc/caddy/Caddyfile\n"
                    "docker compose logs --tail 50 orchestrator"
                ),
                detail={"status": exc.code},
            )
        ]
    except (OSError, ValueError) as exc:
        return [
            Finding(
                "worker_protocol_window",
                FAIL,
                f"GET {endpoint} failed ({type(exc).__name__})",
                why=(
                    "The first request a joining worker makes did not "
                    "complete, so nobody can join."
                ),
                fix="sudo systemctl status caddy && docker compose ps",
            )
        ]

    try:
        window = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        window = None
    if (
        not isinstance(window, dict)
        or "supported_worker_protocol_versions" not in window
    ):
        return [
            Finding(
                "worker_protocol_window",
                FAIL,
                f"GET {endpoint} answered HTTP {code} but not with a protocol "
                "window",
                why=(
                    "Something answered -- most likely the proxy's own error "
                    "page -- but not the coordinator. A worker fails here with "
                    "a confusing message."
                ),
                fix="docker compose logs --tail 50 orchestrator",
            )
        ]
    versions = window["supported_worker_protocol_versions"]
    return [
        Finding(
            "worker_protocol_window",
            PASS,
            "the worker protocol window answers over HTTPS: version "
            + ", ".join(str(version) for version in versions),
            why=(
                "This is the exact unauthenticated call a contributor's "
                "installer makes first. It answering over HTTPS, from a host "
                "whose certificate a stock client trusts, is the closest thing "
                "to proof that somebody else can join."
            ),
            detail={"window": window},
        )
    ]


def check_external_reachability(url: str) -> list[Finding]:
    """Say plainly what the checks above did and did not establish."""

    host = urlsplit(url).hostname or url
    return [
        Finding(
            "external_reachability",
            WARN,
            f"{host} was reached from this host, not from outside it",
            why=(
                "A request made on the coordinator itself can succeed through "
                "a route a stranger does not have: an /etc/hosts entry, a "
                "split-horizon DNS answer, or a router folding the connection "
                "back inside. That is good evidence and it is not proof.\n"
                "The proof is somebody else running the command below, on a "
                "machine that is not yours and a network that is not yours. "
                "Do that before you invite anybody."
            ),
            fix=f"curl -sS {url.rstrip('/')}/v1/worker-protocol",
            detail={"host": host},
        )
    ]


# -- Report ------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    ok: bool
    findings: tuple[Finding, ...]

    def as_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "findings": [asdict(finding) for finding in self.findings],
            },
            indent=2,
            sort_keys=True,
        )


_ORDER = {FAIL: 0, WARN: 1, SKIP: 2, PASS: 3}


def run_host_preflight(
    *,
    state_dir: Path,
    config_path: Path | None = None,
    url: str | None = None,
    app_port: int = DEFAULT_APP_PORT,
    timeout: float = 10.0,
    ca_bundle: Path | None = None,
) -> Report:
    """Every host check, in one read-only pass."""

    resolved_config = config_path or (state_dir / "config.json")
    findings: list[Finding] = []
    findings += check_listening_ports(app_port)
    findings += check_ssh()
    findings += check_unattended_upgrades()
    findings += check_file_modes(state_dir, resolved_config)
    findings += check_secrets(resolved_config)
    if url:
        findings += check_certificate(url, timeout, ca_bundle=ca_bundle)
        findings += check_worker_protocol(url, timeout, ca_bundle=ca_bundle)
        findings += check_external_reachability(url)
    else:
        findings.append(
            Finding(
                "certificate",
                SKIP,
                "no coordinator address was given",
                why=(
                    "Pass the address you would give a contributor and this "
                    "checks the certificate and the protocol window the same "
                    "way their installer will."
                ),
                fix="python3 scripts/deploy_preflight.py --url https://your-domain",
            )
        )
    return Report(
        ok=not any(finding.status == FAIL for finding in findings),
        findings=tuple(findings),
    )


def _wrap(text: str, width: int, indent: str) -> Iterator[str]:
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if len(indent) + len(candidate) > width and line:
                yield indent + line
                line = word
            else:
                line = candidate
        if line:
            yield indent + line


def render(report: Report, *, width: int = 78) -> str:
    lines = [
        "Mycelium host preflight: " + ("PASS" if report.ok else "FAIL"),
        "",
        "Read-only: nothing on this machine was changed, and no credential "
        "value is printed below.",
        "",
    ]
    for finding in sorted(report.findings, key=lambda f: _ORDER.get(f.status, 4)):
        lines.append(f"[{finding.status.upper():4}] {finding.name}")
        lines.append(f"       {finding.summary}")
        if finding.why:
            lines.append("")
            lines.extend(_wrap(finding.why, width, "       "))
        if finding.fix and finding.status in {FAIL, WARN}:
            lines.append("")
            lines.append("       fix:")
            lines.extend(
                f"         {fix_line}" for fix_line in finding.fix.split("\n")
            )
        lines.append("")

    counts = dict.fromkeys((FAIL, WARN, SKIP, PASS), 0)
    for finding in report.findings:
        counts[finding.status] = counts.get(finding.status, 0) + 1
    lines.append(
        f"{counts[FAIL]} failed, {counts[WARN]} warnings, {counts[PASS]} passed, "
        f"{counts[SKIP]} not applicable on this machine."
    )
    if counts[FAIL]:
        lines.append("")
        lines.append("Do not invite anybody until the failures above are fixed.")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only host checks for a Mycelium coordinator."
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("data"),
        help="the coordinator's state directory (default: data)",
    )
    parser.add_argument(
        "--config", type=Path, help="settings file (default: STATE-DIR/config.json)"
    )
    parser.add_argument(
        "--url", help="the https:// address you would give a contributor"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_APP_PORT,
        help=f"the coordinator's own port (default: {DEFAULT_APP_PORT})",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_host_preflight(
        state_dir=args.state_dir,
        config_path=args.config,
        url=args.url,
        app_port=args.port,
        timeout=args.timeout,
    )
    print(report.as_json() if args.json_output else render(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
