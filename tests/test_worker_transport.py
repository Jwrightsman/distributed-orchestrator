"""Plaintext HTTP to anything but loopback must be impossible, not discouraged.

Every previous version of this rule was a recommendation in a deployment
document, and a recommendation is worth what the least careful operator makes of
it. The contributor being invited to donate a laptop is not the person who can
evaluate whether an operator's overlay is really private.

So these tests assert two things. That the policy is right — the easy half. And
that there is **no way around it**: no flag, no environment variable, no
configuration key, and no disabled certificate verification anywhere in the
tree. That second half is what makes the first half worth anything.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import worker_identity
import worker_transport
from worker_transport import (
    InsecureTransportError,
    is_loopback_host,
    require_secure_transport,
)


REPO_ROOT = Path(__file__).resolve().parent.parent

#: Modules a contributor's join actually runs. The policy has to hold across all
#: of them, because a hole in any one is a hole in the property.
WORKER_MODULES = (
    "worker_transport.py",
    "worker_identity.py",
    "worker_installer.py",
    "worker_secret.py",
    "join.py",
    "node.py",
)

#: Files allowed to contain the banned spellings, because their job is to name
#: them in order to forbid them.
_PATTERN_ALLOWLIST = {"test_worker_transport.py", "test_contributor_safety.py"}


def _module_source(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _code_without_prose(text: str) -> str:
    """Source with comments and docstrings removed, string literals kept.

    Prose is allowed to discuss the override this project refuses to implement.
    Code is not. String literals stay because an argparse flag *is* a string
    literal, and that is exactly what must not appear.
    """

    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    # ast.unparse drops comments on its own.
    return ast.unparse(tree)


# ── The policy ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.5.5.5",  # all of 127/8, not only the famous one
        "::1",
        "::ffff:127.0.0.1",  # the IPv4-mapped spelling ipaddress will not resolve
        "localhost",
        "LOCALHOST",
        "localhost.",  # a trailing root dot is the same name
        "app.localhost",  # RFC 6761 reserves everything under it
    ],
)
def test_loopback_is_recognised(host):
    assert is_loopback_host(host) is True
    require_secure_transport("http", host)  # must not raise


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "192.168.1.50",  # a LAN is a network
        "100.101.102.103",  # a private overlay address is a network
        "10.0.0.5",
        "coordinator.local",  # mDNS resolves somewhere
        "localhost.example.com",  # the prefix proves nothing
        "notlocalhost",
        "::ffff:192.168.1.50",
        "0.0.0.0",
        "",
    ],
)
def test_everything_else_is_not_loopback_and_is_refused_over_http(host):
    assert is_loopback_host(host) is False
    with pytest.raises(InsecureTransportError):
        require_secure_transport("http", host)


@pytest.mark.parametrize(
    "host", ["example.com", "192.168.1.50", "127.0.0.1", "localhost"]
)
def test_https_is_accepted_everywhere(host):
    require_secure_transport("https", host)  # must not raise


@pytest.mark.parametrize("scheme", ["ftp", "file", "javascript", "ws", "", "HTTPX"])
def test_no_other_scheme_is_a_transport(scheme):
    with pytest.raises(InsecureTransportError):
        require_secure_transport(scheme, "example.com")


def test_the_refusal_tells_the_contributor_who_can_fix_it():
    """A contributor cannot install a certificate on somebody else's server."""

    with pytest.raises(InsecureTransportError) as raised:
        require_secure_transport("http", "coordinator.example")
    message = str(raised.value)
    assert "coordinator.example" in message, "say which address was refused"
    assert "https://" in message, "say what to ask for"
    assert "invited you" in message, "say whose problem it is to fix"


# ── No way around it ─────────────────────────────────────────────────────────

_PLAUSIBLE_OVERRIDE_ENV = (
    "MYCELIUM_ALLOW_HTTP",
    "MYCELIUM_INSECURE",
    "MYCELIUM_ALLOW_PLAINTEXT",
    "MYCELIUM_SKIP_TLS",
    "PYTHONHTTPSVERIFY",
    "CURL_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
)


def test_the_policy_never_consults_the_environment(monkeypatch):
    """An override would have to be readable; the module reads nothing."""

    code = _code_without_prose(inspect.getsource(worker_transport)).lower()
    assert "environ" not in code
    assert "getenv" not in code

    for name in _PLAUSIBLE_OVERRIDE_ENV:
        monkeypatch.setenv(name, "1")

    with pytest.raises(InsecureTransportError):
        require_secure_transport("http", "example.com")
    with pytest.raises(worker_identity.WorkerIdentityError):
        worker_identity.normalize_coordinator("http://example.com:8000")


#: Spellings of "turn the safety off". Deliberately does not include the bare
#: word "insecure": `InsecureTransportError` is the name of the *refusal*, and
#: banning the word would only push the next author to a vaguer one.
_OVERRIDE_SPELLINGS = (
    "allow_http",
    "allow-http",
    "allow_plaintext",
    "allow-plaintext",
    "allow_insecure",
    "allow-insecure",
    "--insecure",
    "skip_tls",
    "skip-tls",
    "no_tls",
    "no-tls",
    "disable_tls",
    "disable-tls",
    "unverified",
    "trust_insecure",
)


@pytest.mark.parametrize("module_name", WORKER_MODULES)
def test_no_worker_module_offers_an_override(module_name):
    lowered = _code_without_prose(_module_source(module_name)).lower()
    offenders = [word for word in _OVERRIDE_SPELLINGS if word in lowered]
    assert not offenders, (
        f"{module_name} contains override-shaped code for {offenders}. There is "
        "deliberately no way to permit plaintext to a non-loopback host."
    )


@pytest.mark.parametrize("module_name", WORKER_MODULES)
def test_no_worker_cli_declares_a_transport_flag(module_name):
    """Stronger than grep: read the option strings the CLI actually declares."""

    tree = ast.parse(_module_source(module_name))
    declared: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not isinstance(function, ast.Attribute) or function.attr != "add_argument":
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                declared.append(argument.value.lower())

    forbidden = [
        flag
        for flag in declared
        if any(
            token in flag
            for token in ("insecure", "http", "tls", "ssl", "cert", "verify", "plaintext")
        )
    ]
    assert not forbidden, (
        f"{module_name} declares transport-relaxing options {forbidden}"
    )


def test_no_configuration_key_can_relax_transport():
    import config

    keys = " ".join(str(key) for key in getattr(config, "DEFAULTS", {})).lower()
    for word in ("allow_http", "insecure", "plaintext", "skip_tls", "no_tls"):
        assert word not in keys, f"config exposes a transport override: {word}"


#: How certificate verification (or argument safety) gets disabled in Python, in
#: every spelling we could think of. None may appear anywhere in the tree.
_KILL_SWITCHES = (
    "verify=false",
    "verify = false",
    "cert_none",
    "_create_unverified_context",
    "check_hostname = false",
    "check_hostname=false",
    "--insecure",
    "--no-check-certificate",
    "curl -k",
    "shell=true",
)


def _scannable_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {
            ".py",
            ".sh",
            ".ps1",
            ".yml",
            ".yaml",
            ".toml",
            ".cfg",
        }:
            continue
        if set(path.parts) & {
            "__pycache__",
            ".git",
            ".claude",
            "node_modules",
            "output",
            ".venv",
        }:
            continue
        if path.name in _PATTERN_ALLOWLIST:
            continue
        yield path


def test_certificate_verification_is_never_disabled_anywhere_in_the_tree():
    """Repo-wide, not diff-wide. A hole outside the diff is still a hole."""

    offenders: list[str] = []
    for path in _scannable_files():
        lowered = path.read_text(encoding="utf-8", errors="replace").lower()
        offenders += [
            f"{path.relative_to(REPO_ROOT)}: {pattern}"
            for pattern in _KILL_SWITCHES
            if pattern in lowered
        ]
    assert not offenders, (
        "certificate verification or argument safety weakened: "
        + "; ".join(offenders)
    )


@pytest.mark.parametrize("module_name", WORKER_MODULES)
def test_no_worker_module_passes_a_verify_argument(module_name):
    """httpx verifies by default; the only way to lose that is to pass verify."""

    tree = ast.parse(_module_source(module_name))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                assert keyword.arg != "verify", (
                    f"{module_name} passes verify= to a client; leave the default alone"
                )
