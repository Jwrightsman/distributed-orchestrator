"""The guided installer, against a stub coordinator. No network, no Ollama.

The installer is the first thing a contributor runs and the only thing most of
them will read. These tests hold the promises it makes on screen: that the
invitation code never leaves the process except as one header on one request,
that nothing durable is written before somebody agrees, that a failure at any
step leaves the machine as it was, and that a plaintext address is refused with
no way around it.

Everything runs through `httpx.MockTransport`, so the real request-building code
— headers, redaction, the descriptor, the registration handshake — is exercised.
The only things stubbed are the two peers: the coordinator and local Ollama.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from pathlib import Path

import httpx
import pytest

import worker_installer
from node_capabilities import (
    NodeCapabilityDescriptorV1,
    capability_descriptor_digest,
)


COORDINATOR = "https://coordinator.example"
INVITATION = "invitation-code-4Kq9ZfWn2LbTx7Rv"
MODEL = "qwen3.5:4b"

_OLLAMA_HOST = "localhost:11434"


class StubCoordinator:
    """A coordinator and an Ollama, and a record of everything asked of them."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.registrations: list[dict] = []
        self.drains: list[httpx.Request] = []
        self.window = {
            "node_protocol_min": "1",
            "node_protocol_max": "1",
            "supported_worker_protocol_versions": ["1"],
            "server_version": "0.3.0",
        }
        self.registration_status = 200
        self.registration_detail: dict | None = None
        self.protocol_status = 200
        self.enrolled = True
        self.descriptor_hash_override: str | None = None
        #: Replaces every coordinator answer (Ollama still works normally), so a
        #: test can make the far side redirect, hang up, or vanish.
        self.override = None

    @property
    def hosts(self) -> set[str]:
        return {request.url.netloc.decode() for request in self.requests}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        host = request.url.netloc.decode()

        if host != _OLLAMA_HOST and self.override is not None:
            return self.override(request)

        if host == _OLLAMA_HOST:
            if path == "/api/tags":
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {
                                "name": MODEL,
                                "model": MODEL,
                                "digest": "sha256:" + "ab" * 32,
                                "details": {"quantization_level": "Q4_K_M"},
                            }
                        ]
                    },
                )
            if path == "/api/version":
                return httpx.Response(200, json={"version": "0.5.0"})
            if path == "/api/show":
                return httpx.Response(200, json={"capabilities": []})
            return httpx.Response(404, json={})

        if path == "/v1/worker-protocol":
            if self.protocol_status != 200:
                return httpx.Response(self.protocol_status, json={})
            return httpx.Response(200, json=self.window)

        if path == "/nodes/register":
            body = json.loads(request.content.decode())
            self.registrations.append(body)
            if self.registration_status != 200:
                return httpx.Response(
                    self.registration_status,
                    json={"detail": self.registration_detail or {}},
                )
            descriptor = NodeCapabilityDescriptorV1.model_validate(
                body["capability_descriptor"]
            )
            return httpx.Response(
                200,
                json={
                    "node_id": body["node_id"],
                    "message": "welcome",
                    "capabilities": body.get("capabilities", []),
                    "session_token": "session-token-value",
                    "session_id": str(uuid.uuid4()),
                    "session_expires_at": None,
                    "enrolled": self.enrolled,
                    "enrollment_action": body.get("enrollment_action"),
                    "enrollment_id": str(uuid.uuid4()),
                    "credential_version": 1,
                    "capability_descriptor_version": descriptor.descriptor_version,
                    "capability_descriptor_hash": (
                        self.descriptor_hash_override
                        or capability_descriptor_digest(descriptor)
                    ),
                },
            )

        if path.endswith("/drain"):
            self.drains.append(request)
            return httpx.Response(200, json={"ok": True, "draining": True})

        return httpx.Response(404, json={})


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """Wire both peers to a mock transport and point config at a temp directory."""

    coordinator = StubCoordinator()
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(coordinator.handle)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setenv("MYCELIUM_WORKER_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        worker_installer,
        "get_config",
        lambda: {"model": MODEL, "ollama_url": f"http://{_OLLAMA_HOST}"},
    )
    monkeypatch.setattr(worker_installer, "running_privileged", lambda: False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    return coordinator


@pytest.fixture
def answers(monkeypatch):
    """Feed the interactive prompts. Typed lines and the hidden code."""

    def _install(*lines: str, invitation: str = INVITATION):
        queue = list(lines)

        def _input(*_args, **_kwargs):
            assert queue, "installer asked more questions than the test answered"
            return queue.pop(0)

        monkeypatch.setattr("builtins.input", _input)
        monkeypatch.setattr(
            worker_installer.getpass, "getpass", lambda *a, **k: invitation
        )
        return queue

    return _install


def _flat(text: str) -> str:
    """Captured Rich output with its line wrapping taken back out."""

    return " ".join(text.split())


def _identity_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*.json") if path.is_file()]


def _run_install(**kwargs) -> int:
    import asyncio

    return asyncio.run(worker_installer.install(**kwargs))


# ── The happy path ───────────────────────────────────────────────────────────


def test_a_complete_join_writes_one_identity_file_and_registers_once(
    stub, answers, tmp_path, capsys
):
    answers("yes")
    assert _run_install(coordinator=COORDINATOR, node_id="laptop") == worker_installer.EXIT_OK

    written = _identity_files(tmp_path / "config")
    assert len(written) == 1, f"expected exactly one identity file, got {written}"
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["coordinator"] == COORDINATOR
    assert payload["node_id"] == "laptop"
    assert payload["enrollment_id"] is not None, "the enrolment must be remembered"

    assert len(stub.registrations) == 1
    assert stub.registrations[0]["enrollment_action"] == "bootstrap"

    out = capsys.readouterr().out
    assert "python node.py --server" in out, "must say how to start working"
    assert "uninstall" in out, "must say how to remove everything"
    assert "drain" in out.lower(), "must say how to drain"


def test_loopback_http_still_works_for_local_development(stub, answers, tmp_path):
    answers("yes")
    assert (
        _run_install(coordinator="http://localhost:8000", node_id="laptop")
        == worker_installer.EXIT_OK
    )
    assert len(_identity_files(tmp_path / "config")) == 1


# ── The secret ───────────────────────────────────────────────────────────────


def test_the_invitation_code_reaches_exactly_one_header_and_nowhere_else(
    stub, answers, tmp_path, capsys
):
    """THE ONE THAT MATTERS. A leaked invitation code admits anybody."""

    answers("yes")
    _run_install(coordinator=COORDINATOR, node_id="laptop")
    captured = capsys.readouterr()

    assert INVITATION not in captured.out
    assert INVITATION not in captured.err

    for path in (tmp_path / "config").rglob("*"):
        if path.is_file():
            assert INVITATION not in path.read_text(encoding="utf-8", errors="replace"), (
                f"the invitation code was written to {path}"
            )

    carrying = [
        request
        for request in stub.requests
        if INVITATION in " ".join(f"{k}: {v}" for k, v in request.headers.items())
        or INVITATION in request.content.decode("utf-8", errors="replace")
    ]
    assert len(carrying) == 1, "the code should travel once, on the registration"
    assert carrying[0].url.path == "/nodes/register"
    assert carrying[0].headers.get("X-Node-Secret") == INVITATION
    assert INVITATION not in carrying[0].content.decode(), "never in a body"


def test_the_identity_credential_is_generated_here_not_supplied(stub, answers, tmp_path):
    answers("yes")
    _run_install(coordinator=COORDINATOR, node_id="laptop")

    payload = json.loads(_identity_files(tmp_path / "config")[0].read_text(encoding="utf-8"))
    credential = payload["enrollment_credential"]
    assert credential != INVITATION
    assert len(credential) >= 32
    # It went out in the registration body, which is how the coordinator learns
    # it — but it was minted locally before that request existed.
    assert credential in stub.registrations[0]["enrollment_credential"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_identity_file_is_owner_only(stub, answers, tmp_path):
    answers("yes")
    _run_install(coordinator=COORDINATOR, node_id="laptop")
    written = _identity_files(tmp_path / "config")[0]
    assert stat.S_IMODE(written.stat().st_mode) & 0o077 == 0


def test_no_command_line_option_accepts_a_secret():
    """Arguments are visible in ps and land in shell history."""

    parser = worker_installer.build_parser()
    options = {
        option
        for action in parser._actions  # noqa: SLF001 - inspecting the CLI is the point
        for option in action.option_strings
    }
    for spelling in ("--secret", "--invitation", "--token", "--key", "--password"):
        assert spelling not in options, f"{spelling} would put a secret in argv"
    # The file form takes a path, which is not a secret.
    assert "--invitation-file" in options


def test_an_invitation_file_must_not_be_readable_by_others(stub, answers, tmp_path):
    invitation_file = tmp_path / "code.txt"
    invitation_file.write_text(INVITATION, encoding="utf-8")
    if os.name == "posix":
        invitation_file.chmod(0o644)
        answers("yes")
        code = _run_install(
            coordinator=COORDINATOR, node_id="laptop", invitation_file=invitation_file
        )
        assert code == worker_installer.EXIT_BAD_COORDINATOR
        assert not _identity_files(tmp_path / "config")
    else:
        # Windows has no portable POSIX mode to assert; the readable path still
        # has to work, which is what this branch checks.
        invitation_file.chmod(0o600)
        answers("yes")
        assert (
            _run_install(
                coordinator=COORDINATOR,
                node_id="laptop",
                invitation_file=invitation_file,
            )
            == worker_installer.EXIT_OK
        )


# ── Refusals ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    [
        "http://coordinator.example",  # plaintext to a real host
        "http://192.168.1.50:8000",  # a LAN is still a network
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://coordinator.example",
        "https://user:pass@coordinator.example",
        "https://coordinator.example/some/path",
        "https://coordinator.example?code=x",
        "not a url at all",
        "",
    ],
)
def test_an_unusable_address_is_refused_before_anything_happens(
    stub, answers, tmp_path, address
):
    answers("yes")
    with pytest.raises(worker_installer.InstallerError) as raised:
        worker_installer.ask_coordinator(address)
    assert raised.value.exit_code == worker_installer.EXIT_BAD_COORDINATOR
    assert not _identity_files(tmp_path / "config")


def test_a_redirect_is_not_followed_while_carrying_a_credential(
    stub, answers, tmp_path
):
    stub.override = lambda request: httpx.Response(
        302, headers={"Location": "https://elsewhere.example/"}
    )
    answers("yes")
    with pytest.raises(worker_installer.InstallerError) as raised:
        _run_install(coordinator=COORDINATOR, node_id="laptop")

    assert raised.value.exit_code == worker_installer.EXIT_BAD_COORDINATOR
    assert not _identity_files(tmp_path / "config")
    assert not stub.registrations, "no credential was offered to the redirecting host"
    assert not any(
        "elsewhere.example" in request.url.netloc.decode() for request in stub.requests
    ), "the installer followed a redirect to another host"


def test_running_as_root_is_refused(stub, answers, monkeypatch, tmp_path):
    monkeypatch.setattr(worker_installer, "running_privileged", lambda: True)
    answers("yes")
    with pytest.raises(worker_installer.InstallerError) as raised:
        _run_install(coordinator=COORDINATOR, node_id="laptop")
    assert raised.value.exit_code == worker_installer.EXIT_PRIVILEGED
    assert not _identity_files(tmp_path / "config")


@pytest.mark.parametrize(
    ("system", "version", "reason"),
    [("Haiku", (3, 14), "platform"), ("Linux", (3, 9), "python")],
)
def test_an_unsupported_machine_is_refused_with_a_link(system, version, reason):
    with pytest.raises(worker_installer.InstallerError) as raised:
        worker_installer.check_platform(system=system, version=version)
    assert raised.value.exit_code == worker_installer.EXIT_PLATFORM
    assert "http" in raised.value.hint, f"the {reason} refusal must link somewhere"


def test_missing_ollama_stops_rather_than_installing_it(stub, answers, tmp_path, monkeypatch):
    async def absent():
        return {"ok": False, "models": [], "error": "not running"}

    monkeypatch.setattr(worker_installer, "check_ollama", absent)
    answers("yes")
    with pytest.raises(worker_installer.InstallerError) as raised:
        _run_install(coordinator=COORDINATOR, node_id="laptop")
    assert raised.value.exit_code == worker_installer.EXIT_OLLAMA_MISSING
    assert "ollama.com" in raised.value.hint
    assert "will not install it for you" in raised.value.hint
    assert not _identity_files(tmp_path / "config")


# ── The protocol window, in the contributor's terms ──────────────────────────


def test_a_worker_behind_the_window_is_told_to_update_itself():
    with pytest.raises(worker_installer.InstallerError) as raised:
        worker_installer.check_protocol_window(
            {"node_protocol_min": "3", "node_protocol_max": "4", "server_version": "0.9"},
            "1",
        )
    assert raised.value.exit_code == worker_installer.EXIT_PROTOCOL_MISMATCH
    assert "yours to fix" in raised.value.hint
    assert "git pull" in raised.value.hint


def test_a_worker_ahead_of_the_window_is_told_to_tell_the_operator():
    with pytest.raises(worker_installer.InstallerError) as raised:
        worker_installer.check_protocol_window(
            {"node_protocol_min": "1", "node_protocol_max": "1", "server_version": "0.1"},
            "4",
        )
    assert raised.value.exit_code == worker_installer.EXIT_PROTOCOL_MISMATCH
    assert "not yours to fix" in raised.value.hint
    assert "whoever runs the coordinator" in raised.value.hint


def test_the_window_is_checked_before_the_credential_is_created(
    stub, answers, tmp_path
):
    stub.window = {
        "node_protocol_min": "7",
        "node_protocol_max": "9",
        "server_version": "9.9.9",
    }
    answers("yes")
    with pytest.raises(worker_installer.InstallerError) as raised:
        _run_install(coordinator=COORDINATOR, node_id="laptop")
    assert raised.value.exit_code == worker_installer.EXIT_PROTOCOL_MISMATCH
    assert not _identity_files(tmp_path / "config"), (
        "a version mismatch must not leave a credential behind"
    )
    assert not stub.registrations, "no credential should have been offered"


# ── Consent, and its ordering ────────────────────────────────────────────────


def test_declining_leaves_the_machine_untouched(stub, answers, tmp_path, capsys):
    answers("")  # just pressing Enter
    with pytest.raises(worker_installer.InstallerError) as raised:
        _run_install(coordinator=COORDINATOR, node_id="laptop")
    assert raised.value.exit_code == worker_installer.EXIT_DECLINED
    assert not _identity_files(tmp_path / "config")
    assert not stub.registrations


def test_consent_is_asked_before_the_model_is_downloaded(stub, monkeypatch, tmp_path):
    """A 2.5 GB download is itself something to agree to. ROADMAP §2."""

    order: list[str] = []

    def _input(*_a, **_k):
        order.append("consent")
        return ""  # decline, so nothing else runs

    async def _model(model, present):
        order.append("model")
        return None

    monkeypatch.setattr("builtins.input", _input)
    monkeypatch.setattr(worker_installer, "ensure_model", _model)
    with pytest.raises(worker_installer.InstallerError):
        _run_install(coordinator=COORDINATOR, node_id="laptop")
    assert order == ["consent"], "the model must not be pulled before agreement"


def test_the_consent_screen_says_what_it_can_and_cannot_see(stub):
    text = worker_installer.consent_text(
        COORDINATOR, MODEL, Path("/home/someone/.config/mycelium/nodes/x.json")
    ).lower()
    for claim in (
        "prompts",
        "cannot",
        "can",
        "stop at any time",
        "firewall",
        "2.5 gb",
        "inbound port",
    ):
        assert claim in text, f"the consent screen never mentions {claim!r}"


def test_an_unattended_run_refuses_rather_than_assuming(stub, monkeypatch, tmp_path):
    """No terminal means nobody consented. AGENTS.md, ROADMAP §2."""

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("prompted with no terminal")
    )
    with pytest.raises(worker_installer.InstallerError) as raised:
        _run_install(coordinator=COORDINATOR, node_id="laptop")
    assert raised.value.exit_code == worker_installer.EXIT_DECLINED
    assert not _identity_files(tmp_path / "config")


# ── Nothing half-written ─────────────────────────────────────────────────────


def test_a_rejected_registration_removes_the_credential_it_just_wrote(
    stub, answers, tmp_path, capsys
):
    stub.registration_status = 403
    stub.registration_detail = {
        "code": "invalid_node_secret",
        "message": "bad invitation",
    }
    answers("yes")
    with pytest.raises(worker_installer.InstallerError) as raised:
        _run_install(coordinator=COORDINATOR, node_id="laptop")
    assert raised.value.exit_code == worker_installer.EXIT_REGISTRATION
    assert not _identity_files(tmp_path / "config"), (
        "a failed join must not leave a dead credential on disk"
    )
    assert INVITATION not in capsys.readouterr().out


def test_a_coordinator_that_disagrees_about_the_descriptor_leaves_nothing(
    stub, answers, tmp_path
):
    stub.descriptor_hash_override = "0" * 64
    answers("yes")
    with pytest.raises(worker_installer.InstallerError) as raised:
        _run_install(coordinator=COORDINATOR, node_id="laptop")
    assert raised.value.exit_code == worker_installer.EXIT_REGISTRATION
    assert not _identity_files(tmp_path / "config")


def test_an_unreachable_coordinator_is_a_sentence_not_a_traceback(
    stub, answers, tmp_path, capsys
):
    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    stub.override = _refuse
    answers("yes")
    code = worker_installer.run(
        ["install", "--coordinator", COORDINATOR, "--node-id", "laptop"]
    )
    assert code == worker_installer.EXIT_UNREACHABLE
    out = _flat(capsys.readouterr().out)
    assert "Traceback" not in out, "a traceback must never be the error surface"
    assert "Could not reach" in out
    assert not _identity_files(tmp_path / "config")


# ── Uninstall ────────────────────────────────────────────────────────────────


def _install_then(stub, answers, tmp_path):
    answers("yes")
    assert _run_install(coordinator=COORDINATOR, node_id="laptop") == worker_installer.EXIT_OK
    return _identity_files(tmp_path / "config")[0]


def _run_uninstall(**kwargs) -> int:
    import asyncio

    return asyncio.run(worker_installer.uninstall(**kwargs))


def test_uninstall_drains_first_and_leaves_no_credential(stub, answers, tmp_path, capsys):
    identity = _install_then(stub, answers, tmp_path)
    assert identity.exists()

    assert _run_uninstall(coordinator=COORDINATOR, assume_yes=True) == worker_installer.EXIT_OK

    assert not identity.exists(), "the credential is still on disk"
    assert not _identity_files(tmp_path / "config")
    assert stub.drains, "the coordinator was never told this machine is leaving"
    assert stub.drains[0].headers.get("X-Node-Session"), "drain must carry its session"

    out = capsys.readouterr().out
    assert "Ollama" in out, "must say what it did not remove"
    assert "ollama rm" in out, "must say how to remove the model"


def test_uninstall_still_removes_the_credential_when_the_coordinator_is_gone(
    stub, answers, tmp_path, capsys
):
    identity = _install_then(stub, answers, tmp_path)

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gone", request=request)

    stub.override = _refuse
    assert _run_uninstall(coordinator=COORDINATOR, assume_yes=True) == worker_installer.EXIT_OK
    assert not identity.exists(), "leaving must not depend on the other side answering"
    assert "could not reach the coordinator" in _flat(capsys.readouterr().out)


def test_uninstall_on_a_machine_that_never_joined_says_so(stub, tmp_path, capsys):
    assert _run_uninstall(coordinator=COORDINATOR, assume_yes=True) == worker_installer.EXIT_OK
    assert "Nothing to remove" in capsys.readouterr().out


def test_uninstall_asks_before_deleting(stub, answers, tmp_path, monkeypatch):
    identity = _install_then(stub, answers, tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert _run_uninstall(coordinator=COORDINATOR) == worker_installer.EXIT_DECLINED
    assert identity.exists(), "declining must keep the credential"


# ── What it never does ───────────────────────────────────────────────────────


def test_the_installer_talks_to_the_coordinator_and_ollama_and_nobody_else(
    stub, answers, tmp_path
):
    """No telemetry, no update check, no third party."""

    answers("yes")
    _run_install(coordinator=COORDINATOR, node_id="laptop")
    assert stub.hosts <= {"coordinator.example", _OLLAMA_HOST}, (
        f"the installer contacted something else: {stub.hosts}"
    )


def test_the_installer_never_changes_the_hosts_security_posture():
    """No firewall rule, no trust store, no boot service."""

    source = Path(worker_installer.__file__).read_text(encoding="utf-8").lower()
    for forbidden in (
        "netsh",
        "iptables",
        "ufw",
        "firewall-cmd",
        "update-ca-certificates",
        "add-trusted-cert",
        "certutil",
        "systemctl",
        "launchctl",
        "schtasks",
        "crontab",
        "setuid",
        "chown",
    ):
        assert forbidden not in source, (
            f"the installer references {forbidden}; it must not change the host"
        )


def test_the_installer_never_uses_a_shell():
    import ast

    tree = ast.parse(Path(worker_installer.__file__).read_text(encoding="utf-8"))
    spawns = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Name) or owner.id not in {"subprocess", "os"}:
            continue
        assert node.func.attr != "system", "os.system is a shell by definition"
        if node.func.attr not in {"run", "Popen", "call", "check_output", "check_call"}:
            continue
        spawns += 1
        for keyword in node.keywords:
            assert keyword.arg != "shell", "no subprocess may take shell="
        assert node.args and isinstance(node.args[0], ast.List), (
            "a subprocess must be given an argument list, never a string"
        )
    assert spawns == 1, f"expected exactly one subprocess (the model pull), found {spawns}"


@pytest.mark.parametrize(
    "hostile",
    [
        "model; rm -rf ~",
        "model && curl evil.example",
        "$(whoami)",
        "`id`",
        "model\nrm -rf /",
        "../../etc/passwd",
    ],
)
def test_a_hostile_model_name_is_refused_before_any_process_starts(hostile):
    with pytest.raises(worker_installer.InstallerError) as raised:
        worker_installer._validate_model_name(hostile)
    assert raised.value.exit_code == worker_installer.EXIT_MODEL_FAILED
