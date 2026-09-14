"""Real, isolated Caddy logging/listener tests; never run the coordinator.

Set MYCELIUM_TEST_CADDY to a Caddy executable, or put it on PATH. This suite
does not install/download it, issue public certificates, or contact the network.
Only authored responses, credentials and a disposable certificate are used.
"""

import contextlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOST = "audit.example.test"
HEADERS = {
    name: f"AUDIT-CREDENTIAL-{index}"
    for index, name in enumerate((
        "X-Node-Secret", "X-Node-Session", "X-Pitch-Key", "X-Viewer-Key",
        "Authorization", "Proxy-Authorization", "Cookie", "Idempotency-Key",
    ))
}
SHARE = "AUDIT-SHARE-CAPABILITY"


@pytest.fixture
def caddy():
    binary = os.environ.get("MYCELIUM_TEST_CADDY") or shutil.which("caddy")
    if not binary:
        pytest.skip("Caddy executable unavailable; set MYCELIUM_TEST_CADDY for emitted-log/listener checks")
    return binary


def _adapt(caddy, template, tmp_path, addresses="127.0.0.1"):
    text = (ROOT / "deploy" / template).read_text(encoding="utf-8")
    text = text.replace("YOUR-HOST.YOUR-TAILNET.ts.net", HOST)
    text = text.replace("YOUR-DOMAIN.example.com", HOST)
    text = text.replace("YOUR-TAILNET-IPV4", addresses)
    path = tmp_path / "Caddyfile"
    path.write_text(text, encoding="utf-8")
    adapted = subprocess.run(
        [caddy, "adapt", "--config", str(path), "--adapter", "caddyfile"],
        capture_output=True, text=True, timeout=15,
    )
    assert adapted.returncode == 0, adapted.stderr
    return json.loads(adapted.stdout)


def _certificate(tmp_path):
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return cert_path, key_path


@contextlib.contextmanager
def _running(caddy, config, tmp_path, *, preserve_bind=False):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, dict(self.headers)))
            if self.path.startswith("/proxy-error"):
                self.close_connection = True
                return
            self.send_response(401 if self.path.startswith("/deny") else 200)
            self.send_header("Location", f"/v1/shares/{SHARE}")
            self.send_header("Set-Cookie", "AUDIT-RESPONSE-COOKIE")
            self.end_headers()
            self.wfile.write(b"authored local response")

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    servers = config["apps"]["http"]["servers"]
    assert len(servers) == 1
    server = next(iter(servers.values()))
    if preserve_bind:
        hosts = [value.rsplit(":", 1)[0] for value in server["listen"]]
        # Refuse to start a wildcard/non-loopback listener, even for regression.
        assert set(hosts) <= {"127.0.0.1", "[::1]"} and hosts
        server["listen"] = [f"{host}:{port}" for host in hosts]
    else:
        server["listen"] = [f"127.0.0.1:{port}"]
    server["automatic_https"] = {"disable": True}
    server["tls_connection_policies"] = [{}]
    # These probes cover TCP/TLS only; do not allocate an unrelated QUIC port.
    server["protocols"] = ["h1", "h2"]
    cert, key = _certificate(tmp_path)
    config["apps"]["tls"] = {"certificates": {"load_files": [
        {"certificate": str(cert), "key": str(key)}
    ]}}
    config["admin"] = {"disabled": True, "config": {"persist": False}}

    def replace_upstream(obj):
        if isinstance(obj, dict):
            if obj.get("handler") == "reverse_proxy":
                obj["upstreams"] = [{"dial": f"127.0.0.1:{upstream.server_port}"}]
            for value in obj.values():
                replace_upstream(value)
        elif isinstance(obj, list):
            for value in obj:
                replace_upstream(value)

    replace_upstream(config)
    log_path = tmp_path / "access.log"
    for name, logger in config["logging"]["logs"].items():
        if name != "default":
            logger["writer"] = {"output": "file", "filename": str(log_path)}
    path = tmp_path / "caddy.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "data"),
           "XDG_CONFIG_HOME": str(tmp_path / "config")}
    runtime_path = tmp_path / "runtime.log"
    process = None
    try:
        with runtime_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                [caddy, "run", "--config", str(path)], stdout=output, stderr=output,
                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                assert process.poll() is None, runtime_path.read_text(encoding="utf-8")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.02)
            else:
                pytest.fail("isolated Caddy did not start")
            yield port, log_path, received, runtime_path
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)


def _request(port, path, certificate, address="127.0.0.1"):
    context = ssl.create_default_context(cafile=str(certificate))
    with socket.create_connection((address, port), timeout=1) as connection:
        with context.wrap_socket(connection, server_hostname=HOST) as tls:
            headers = {**HEADERS, "Host": HOST, "Referer": f"https://{HOST}/v1/shares/{SHARE}"}
            request = "\r\n".join(f"{key}: {value}" for key, value in headers.items())
            tls.sendall(f"GET {path} HTTP/1.1\r\n{request}\r\nConnection: close\r\n\r\n".encode())
            response = http.client.HTTPResponse(tls)
            response.begin()
            response.read()
            return response.status


@pytest.mark.parametrize("template", ["Caddyfile.public", "Caddyfile.tailscale"])
def test_emitted_logs_remove_credentials_and_share_capabilities(caddy, tmp_path, template):
    config = _adapt(caddy, template, tmp_path)
    with _running(caddy, config, tmp_path) as (port, log_path, received, runtime):
        paths = ["/health", "/deny", f"/proxy-error?token={SHARE}", f"/v1/shares/{SHARE}",
                 f"/v1/shares/{SHARE}/artifacts/file?token=AUDIT-QUERY-CAPABILITY",
                 f"/v1/%73hares/{SHARE}"]
        for path in paths:
            assert _request(port, path, tmp_path / "cert.pem") in (200, 401, 502)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            lines = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
            if len(lines) == len(paths):
                break
            time.sleep(.02)
        assert len(lines) == len(paths), "every ordinary/denied/share request must emit a record"
        records = [json.loads(line) for line in lines]
        assert {record["status"] for record in records} == {200, 401, 502}
        emitted = "\n".join(lines) + runtime.read_text(encoding="utf-8")
        for marker in (*HEADERS.values(), SHARE, "AUDIT-QUERY-CAPABILITY", "AUDIT-RESPONSE-COOKIE"):
            assert marker not in emitted
        assert len(received) == len(paths)
        assert received[0][1]["X-Viewer-Key"] == HEADERS["X-Viewer-Key"], "redact logs, preserve auth forwarding"


@pytest.mark.parametrize("dual_stack", [False, True])
def test_private_listener_denies_other_interfaces_with_correct_host_and_sni(caddy, tmp_path, dual_stack):
    if dual_stack:
        try:
            with socket.socket(socket.AF_INET6) as probe:
                probe.bind(("::1", 0))
        except OSError:
            pytest.skip("IPv6 loopback unavailable")
    addresses = "127.0.0.1 ::1" if dual_stack else "127.0.0.1"
    config = _adapt(caddy, "Caddyfile.tailscale", tmp_path, addresses)
    server = next(iter(config["apps"]["http"]["servers"].values()))
    assert set(server["listen"]) == ({"127.0.0.1:443", "[::1]:443"} if dual_stack else {"127.0.0.1:443"})
    with _running(caddy, config, tmp_path, preserve_bind=True) as (port, *_):
        assert _request(port, "/health", tmp_path / "cert.pem") == 200
        with pytest.raises(OSError):
            _request(port, "/health", tmp_path / "cert.pem", "127.0.0.2")
        if dual_stack:
            assert _request(port, "/health", tmp_path / "cert.pem", "::1") == 200
        else:
            with pytest.raises(OSError):
                _request(port, "/health", tmp_path / "cert.pem", "::1")
