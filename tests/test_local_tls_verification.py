"""Prove the TLS path locally, before anyone points a real machine at it.

The failure this file exists to prevent is social rather than technical: an
operator obtains a certificate, sends a friend an invitation, and then debugs
the handshake over text message while the friend's install keeps failing. The
operator has no way to check first, because checking needs a worker and the
worker is the friend.

So: stand up a TLS-terminated stub of the one endpoint a joining worker reads
first, and run the **real** installer against it. Not a copy of the request,
not a mock of the client -- `worker_installer.fetch_protocol_window`, which is
the function that actually runs on a contributor's machine.

Two halves, and the second is what makes the first worth anything:

  * with the stub's CA trusted, the installer gets through; and
  * with it not trusted, the installer refuses.

Without the second, a test that passed because verification was accidentally
disabled would look identical to one that passed because the certificate was
good.
"""

from __future__ import annotations

import http.server
import shutil
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import worker_installer  # noqa: E402
from scripts.tls_local_check import handshake_against  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="openssl is needed to mint a throwaway CA for the stub",
)

_WINDOW = (
    b'{"node_protocol_min": "1", "node_protocol_max": "1", '
    b'"supported_worker_protocol_versions": ["1"], "server_version": "0.3.0"}'
)


def _openssl(*arguments: str) -> None:
    completed = subprocess.run(
        ["openssl", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "openssl " + " ".join(arguments) + ": "
            + completed.stderr.decode("utf-8", "replace")
        )


class Authority:
    """A throwaway CA and one certificate it signed, on disk."""

    def __init__(
        self, directory: Path, common_name: str, sans: str, days: int = 2
    ):
        self.directory = directory
        self.ca_certificate = directory / "ca.crt"
        self.certificate = directory / "leaf.crt"
        self.key = directory / "leaf.key"

        ca_key = directory / "ca.key"
        csr = directory / "leaf.csr"
        extensions = directory / "leaf.ext"
        extensions.write_text(
            f"subjectAltName={sans}\nbasicConstraints=CA:FALSE\n", encoding="utf-8"
        )

        # basicConstraints and keyUsage are not optional decoration: OpenSSL 3
        # rejects a signing certificate that omits them with "CA cert does not
        # include key usage extension", which is a confusing way to be told
        # the CA is malformed rather than the leaf.
        _openssl(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(ca_key), "-out", str(self.ca_certificate),
            "-days", "2", "-subj", "/CN=Mycelium Throwaway Test CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        )
        _openssl(
            "req", "-new", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(self.key), "-out", str(csr),
            "-subj", f"/CN={common_name}",
        )
        _openssl(
            "x509", "-req", "-in", str(csr),
            "-CA", str(self.ca_certificate), "-CAkey", str(ca_key),
            "-CAcreateserial", "-out", str(self.certificate),
            "-days", str(days), "-extfile", str(extensions),
        )


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.path != "/v1/worker-protocol":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_WINDOW)))
        self.end_headers()
        self.wfile.write(_WINDOW)

    def log_message(self, *_arguments):
        return  # keep pytest's output readable


class _QuietServer(http.server.ThreadingHTTPServer):
    """A client rejecting our certificate is the expected result, not an error.

    ThreadingHTTPServer prints a traceback when a handshake fails, which in the
    negative-control tests is the case being asserted. Printing it makes a
    passing run look like a broken one.
    """

    def handle_error(self, *_arguments):
        return


class TLSStub:
    """The coordinator's first endpoint, terminated with a real certificate."""

    def __init__(self, authority: Authority):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(authority.certificate), str(authority.key))
        self._server = _QuietServer(("127.0.0.1", 0), _Handler)
        self._server.socket = context.wrap_socket(
            self._server.socket, server_side=True
        )
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )

    def __enter__(self) -> TLSStub:
        self._thread.start()
        return self

    def __exit__(self, *_exception) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)

    @property
    def origin(self) -> str:
        return f"https://localhost:{self.port}"


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    return Authority(tmp_path, "localhost", "DNS:localhost,IP:127.0.0.1")


def _trust(monkeypatch, bundle: Path) -> None:
    """Point the worker's HTTP client at this CA and nothing else.

    httpx builds its context from `certifi.where()` when `verify` is left
    alone, which the worker does. Patching that function is how a test can
    supply a trust store without any production code learning a way to relax
    verification -- there is deliberately no such way, and this test must not
    become the one that introduces it.
    """

    import certifi

    monkeypatch.setattr(certifi, "where", lambda: str(bundle))


# -- The real installer, against a real certificate ---------------------------


@pytest.mark.asyncio
async def test_the_real_installer_reads_the_window_over_tls(monkeypatch, authority):
    _trust(monkeypatch, authority.ca_certificate)

    with TLSStub(authority) as stub:
        window = await worker_installer.fetch_protocol_window(stub.origin)

    assert window["supported_worker_protocol_versions"] == ["1"]


@pytest.mark.asyncio
async def test_an_untrusted_certificate_stops_the_installer(monkeypatch, authority):
    """The control. Without this, a passing test above proves nothing."""

    import certifi

    monkeypatch.setattr(certifi, "where", certifi.where)  # the real bundle

    with TLSStub(authority) as stub:
        with pytest.raises(worker_installer.InstallerError) as raised:
            await worker_installer.fetch_protocol_window(stub.origin)

    assert raised.value.exit_code == worker_installer.EXIT_UNREACHABLE


@pytest.mark.asyncio
async def test_the_installer_sends_no_credential_before_the_handshake(
    monkeypatch, authority
):
    """A refused certificate must fail closed, with nothing on the wire."""

    import certifi

    monkeypatch.setattr(certifi, "where", certifi.where)
    received: list[str] = []

    class _Recording(_Handler):
        def do_GET(self):  # noqa: N802
            received.append(self.path)
            super().do_GET()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(authority.certificate), str(authority.key))
    server = _QuietServer(("127.0.0.1", 0), _Recording)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(worker_installer.InstallerError):
            await worker_installer.fetch_protocol_window(
                f"https://localhost:{server.server_address[1]}"
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    assert received == [], "the request was sent despite the certificate failing"


# -- The operator's own check, on names that do not resolve -------------------


@pytest.mark.parametrize(
    "name",
    ["coordinator.example.com", "orchestrator.tail9f3c2.ts.net"],
)
def test_tls_local_check_accepts_a_good_certificate_for_a_name(tmp_path, name):
    """No DNS involved: the name is checked against the certificate, not looked up."""

    authority = Authority(tmp_path, name, f"DNS:{name}")

    result = handshake_against(
        authority.certificate,
        authority.key,
        name,
        ca_bundle=authority.ca_certificate,
    )

    assert result.trusted is True
    assert result.server_name == name
    assert name in result.subject_alt_names
    assert result.days_remaining is not None


def test_tls_local_check_reports_a_certificate_for_the_wrong_name(tmp_path):
    authority = Authority(tmp_path, "right.example.com", "DNS:right.example.com")

    result = handshake_against(
        authority.certificate,
        authority.key,
        "wrong.example.com",
        ca_bundle=authority.ca_certificate,
    )

    assert result.trusted is False
    assert "wrong.example.com" in (result.error or "") or "match" in (
        result.error or ""
    )


# -- The host preflight's network checks, against the same stub ---------------


def test_the_preflight_accepts_a_trusted_certificate_and_reads_its_expiry(
    tmp_path,
):
    from scripts.deploy_preflight import PASS, check_certificate

    long_lived = Authority(
        tmp_path, "localhost", "DNS:localhost,IP:127.0.0.1", days=90
    )

    with TLSStub(long_lived) as stub:
        findings = check_certificate(
            stub.origin, timeout=10.0, ca_bundle=long_lived.ca_certificate
        )

    assert findings[0].status == PASS
    assert findings[0].detail["days_remaining"] > 21


def test_the_preflight_warns_about_a_certificate_close_to_expiry(authority):
    """The default fixture is issued for two days, which is the point here."""

    from scripts.deploy_preflight import WARN, check_certificate

    with TLSStub(authority) as stub:
        findings = check_certificate(
            stub.origin, timeout=10.0, ca_bundle=authority.ca_certificate
        )

    assert findings[0].status == WARN
    assert findings[0].detail["days_remaining"] < 21
    assert "every worker disconnects at once" in findings[0].why


def test_the_preflight_explains_a_rejected_certificate(authority):
    """No ca_bundle: the throwaway CA is not trusted, as in real life."""

    from scripts.deploy_preflight import FAIL, check_certificate

    with TLSStub(authority) as stub:
        findings = check_certificate(stub.origin, timeout=10.0)

    assert findings[0].status == FAIL
    assert "self-signed certificate can never be made to work" in findings[0].why


def test_the_preflight_reads_the_protocol_window_over_https(authority):
    from scripts.deploy_preflight import PASS, check_worker_protocol

    with TLSStub(authority) as stub:
        findings = check_worker_protocol(
            stub.origin, timeout=10.0, ca_bundle=authority.ca_certificate
        )

    assert findings[0].status == PASS
    assert findings[0].detail["window"]["supported_worker_protocol_versions"] == ["1"]


def test_the_preflight_fails_when_something_else_answers(authority, tmp_path):
    """A proxy error page is not a coordinator, and must not read as one."""

    from scripts.deploy_preflight import FAIL, check_worker_protocol

    class _NotACoordinator(_Handler):
        def do_GET(self):  # noqa: N802
            body = b"<html>Bad Gateway</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(authority.certificate), str(authority.key))
    server = _QuietServer(("127.0.0.1", 0), _NotACoordinator)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        findings = check_worker_protocol(
            f"https://localhost:{server.server_address[1]}",
            timeout=10.0,
            ca_bundle=authority.ca_certificate,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    assert findings[0].status == FAIL
    assert "not with a protocol window" in findings[0].summary


def test_a_plaintext_address_is_refused_before_any_connection():
    from scripts.deploy_preflight import FAIL, check_certificate

    findings = check_certificate("http://coordinator.example.com", timeout=1.0)

    assert findings[0].status == FAIL
    assert "https://" in findings[0].why


def test_tls_local_check_reports_a_self_signed_certificate_as_rejected(tmp_path):
    """The case an operator is most likely to try, and it cannot be made to work."""

    authority = Authority(tmp_path, "coordinator.example.com", "DNS:coordinator.example.com")

    # No ca_bundle: the default trust store, which is what a worker uses.
    result = handshake_against(
        authority.certificate, authority.key, "coordinator.example.com"
    )

    assert result.trusted is False
    assert result.error
