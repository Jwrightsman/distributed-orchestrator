"""Worker CLI must report authoritative result-submission failures honestly."""

from __future__ import annotations

import pytest

import node


class _Response:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        # Real `httpx.Response` always has these. Without them the worker's
        # trace-context echo would be skipped here rather than exercised, and
        # `test_enrolled_worker_sends_only_session_on_normal_operations` would
        # be asserting about headers the double never produced.
        self.headers = dict(headers or {})

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, task, *, reject_result=False, fail_result=False, handout_headers=None):
        self.task = task
        self.reject_result = reject_result
        self.fail_result = fail_result
        self.handout_headers = dict(handout_headers or {})
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, *args, **kwargs):
        return _Response(200, self.task, headers=self.handout_headers)

    async def post(self, url, *args, **kwargs):
        self.posts.append((url, kwargs.get("json"), kwargs.get("headers")))
        if url.endswith(("/stream", "/tokens")):
            return _Response(200, {"ok": True})
        if self.fail_result:
            raise RuntimeError("result endpoint offline")
        if self.reject_result:
            return _Response(403, {"detail": "attempt is cancelled"})
        return _Response(200, {"status": "accepted", "credits_earned": 0})


class _RegistrationClient:
    def __init__(self, response):
        self.response = response
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return self.response


def _task():
    return {
        "task_id": "task-1",
        "title": "Build",
        "prompt": "prompt",
        "system": "system",
        "attempt_id": "attempt-1",
        "nonce": "nonce-1",
        "contract_version": "1",
        "execution_id": "e" * 32,
        "execution_unit_id": "candidate-1",
        "execution_unit_kind": "candidate",
    }


def _multi_model_descriptor():
    return node.NodeCapabilityDescriptorV1(
        executor=node.ExecutorDescriptorV1(
            kind="ollama", worker_protocol_version="1"
        ),
        models=[
            node.ModelDescriptorV1(
                provider="ollama",
                name="configured:latest",
                digest="a" * 64,
                context_tokens=8192,
            ),
            node.ModelDescriptorV1(
                provider="ollama",
                name="selected:latest",
                digest="b" * 64,
                context_tokens=8192,
            ),
        ],
        hardware=node.HardwareDescriptorV1(),
        limits=node.NodeLimitDescriptorV1(
            max_concurrent_execution_units=1,
            max_output_bytes=1_048_576,
        ),
        isolation=node.IsolationDescriptorV1(kind="none"),
    )


@pytest.mark.asyncio
async def test_worker_executes_the_server_bound_advertised_model(monkeypatch):
    task = {
        **_task(),
        "selected_model": {
            "provider": "ollama",
            "name": "selected:latest",
            "digest": "sha256:" + "b" * 64,
        },
    }
    client = _Client(task)
    monkeypatch.setattr(node.httpx, "AsyncClient", lambda **kwargs: client)
    used_models = []

    async def generated(*args, **kwargs):
        used_models.append(kwargs.get("model"))
        yield "complete output"

    monkeypatch.setattr(node, "generate_stream", generated)

    completed = await node.poll_and_execute(
        "http://server",
        "worker",
        {"tasks": 0, "credits": 0},
        model="configured:latest",
        capability_descriptor=_multi_model_descriptor(),
    )

    assert completed == "task-1"
    assert used_models == ["selected:latest"]


@pytest.mark.asyncio
async def test_worker_rejects_a_model_binding_outside_its_immutable_descriptor(
    monkeypatch,
):
    task = {
        **_task(),
        "selected_model": {
            "provider": "ollama",
            "name": "selected:latest",
            "digest": "sha256:" + "c" * 64,
        },
    }
    client = _Client(task)
    monkeypatch.setattr(node.httpx, "AsyncClient", lambda **kwargs: client)
    generated = False

    async def should_not_generate(*args, **kwargs):
        nonlocal generated
        generated = True
        yield "unexpected"

    monkeypatch.setattr(node, "generate_stream", should_not_generate)

    completed = await node.poll_and_execute(
        "http://server",
        "worker",
        {"tasks": 0, "credits": 0},
        model="configured:latest",
        capability_descriptor=_multi_model_descriptor(),
    )

    assert completed is None
    assert generated is False
    result_payloads = [
        payload for url, payload, _headers in client.posts if url.endswith("/result")
    ]
    assert len(result_payloads) == 1
    assert "immutable capability descriptor" in result_payloads[0]["error"]


@pytest.mark.asyncio
async def test_rejected_result_submission_is_not_reported_as_done(monkeypatch):
    client = _Client(_task(), reject_result=True)
    monkeypatch.setattr(node.httpx, "AsyncClient", lambda **kwargs: client)

    async def generated(*args, **kwargs):
        yield "complete output"

    monkeypatch.setattr(node, "generate_stream", generated)
    messages = []
    monkeypatch.setattr(node.console, "print", lambda value="": messages.append(str(value)))
    session = {"tasks": 0, "credits": 0}

    completed = await node.poll_and_execute("http://server", "worker", session)

    assert completed is None
    assert session == {"tasks": 0, "credits": 0}
    rendered = "\n".join(messages)
    assert "FAILED" in rendered
    assert "rejected result" in rendered
    assert "DONE" not in rendered


@pytest.mark.asyncio
async def test_failed_error_report_does_not_hide_generation_exception(monkeypatch):
    client = _Client(_task(), fail_result=True)
    monkeypatch.setattr(node.httpx, "AsyncClient", lambda **kwargs: client)

    async def generated(*args, **kwargs):
        raise ValueError("model exploded")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(node, "generate_stream", generated)
    messages = []
    monkeypatch.setattr(node.console, "print", lambda value="": messages.append(str(value)))

    completed = await node.poll_and_execute(
        "http://server",
        "worker",
        {"tasks": 0, "credits": 0},
    )

    assert completed is None
    rendered = "\n".join(messages)
    assert "model exploded" in rendered
    assert "result endpoint offline" in rendered


@pytest.mark.asyncio
async def test_worker_stops_before_byte_budget_and_reports_limit_failure(monkeypatch):
    task = {**_task(), "max_output_bytes": 5}
    client = _Client(task)
    monkeypatch.setattr(node.httpx, "AsyncClient", lambda **kwargs: client)

    async def generated(*args, **kwargs):
        yield "abc"
        yield "éé"  # four more UTF-8 bytes would exceed the five-byte budget
        raise AssertionError("worker continued generating after its output limit")

    monkeypatch.setattr(node, "generate_stream", generated)
    messages = []
    monkeypatch.setattr(node.console, "print", lambda value="": messages.append(str(value)))
    session = {
        "tasks": 0,
        "credits": 0,
        "session_token": "session-token",
    }

    completed = await node.poll_and_execute(
        "http://server", "worker", session
    )

    assert completed is None
    result_posts = [item for item in client.posts if item[0].endswith("/result")]
    assert len(result_posts) == 1
    payload = result_posts[0][1]
    assert payload["output"] is None
    assert payload["error"].startswith("output_limit_exceeded:")
    assert result_posts[0][2]["X-Node-Session"] == "session-token"
    assert "DONE" not in "\n".join(messages)


@pytest.mark.asyncio
async def test_enrolled_worker_sends_only_session_on_normal_operations(monkeypatch):
    client = _Client(_task(), reject_result=True)
    monkeypatch.setattr(node.httpx, "AsyncClient", lambda **kwargs: client)

    async def generated(*args, **kwargs):
        yield "complete output"

    monkeypatch.setattr(node, "generate_stream", generated)
    session = {
        "tasks": 0,
        "credits": 0,
        "session_token": "session-token",
        "enrolled": True,
    }

    await node.poll_and_execute(
        "http://server", "worker", session, secret="bootstrap-secret"
    )

    for _url, _payload, headers in client.posts:
        assert headers["X-Node-Session"] == "session-token"
        assert "X-Node-Secret" not in headers


@pytest.mark.asyncio
async def test_worker_echoes_the_trace_context_it_was_handed(monkeypatch):
    """The worker's half of propagation: two headers back, nothing of its own."""
    traceparent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    client = _Client(
        _task(),
        handout_headers={"traceparent": traceparent, "tracestate": "vendor=1"},
    )
    monkeypatch.setattr(node.httpx, "AsyncClient", lambda **kwargs: client)

    async def generated(*args, **kwargs):
        yield "complete output"

    monkeypatch.setattr(node, "generate_stream", generated)
    session = {"tasks": 0, "credits": 0, "session_token": "session-token", "enrolled": True}

    await node.poll_and_execute("http://server", "worker", session, secret="s")

    assert client.posts, "the worker made no request to echo anything on"
    for _url, _payload, headers in client.posts:
        assert headers["traceparent"] == traceparent
        assert headers["tracestate"] == "vendor=1"
        assert headers["X-Node-Session"] == "session-token"


@pytest.mark.asyncio
async def test_a_worker_launders_nothing_it_cannot_revalidate(monkeypatch):
    """A malformed handout header is dropped, not passed along.

    The worker revalidates rather than copying, so a coordinator - or anything
    between them - cannot use a worker to carry a broken value into the
    coordinator's next request.
    """
    client = _Client(_task(), handout_headers={"traceparent": "00-nonsense-!!"})
    monkeypatch.setattr(node.httpx, "AsyncClient", lambda **kwargs: client)

    async def generated(*args, **kwargs):
        yield "complete output"

    monkeypatch.setattr(node, "generate_stream", generated)
    session = {"tasks": 0, "credits": 0, "session_token": "session-token", "enrolled": True}

    await node.poll_and_execute("http://server", "worker", session, secret="s")

    assert client.posts
    for _url, _payload, headers in client.posts:
        assert "traceparent" not in headers
        assert headers["X-Node-Session"] == "session-token"


@pytest.mark.asyncio
async def test_a_worker_adds_no_trace_header_when_it_was_handed_none(monkeypatch):
    """A coordinator with tracing off gets nothing back it did not ask for."""
    client = _Client(_task())
    monkeypatch.setattr(node.httpx, "AsyncClient", lambda **kwargs: client)

    async def generated(*args, **kwargs):
        yield "complete output"

    monkeypatch.setattr(node, "generate_stream", generated)
    session = {"tasks": 0, "credits": 0, "session_token": "session-token", "enrolled": True}

    await node.poll_and_execute("http://server", "worker", session, secret="s")

    assert client.posts
    for _url, _payload, headers in client.posts:
        assert "traceparent" not in headers
        assert "tracestate" not in headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expect_secret"),
    [("bootstrap", True), ("returning", False)],
)
async def test_registration_sends_secret_only_for_bootstrap(
    monkeypatch, action, expect_secret
):
    client = _RegistrationClient(_Response(200, {"ok": True}))
    client_options = {}

    def client_factory(**kwargs):
        client_options.update(kwargs)
        return client

    monkeypatch.setattr(node.httpx, "AsyncClient", client_factory)

    await node.register(
        "https://coordinator.example",
        "worker",
        secret="bootstrap-secret",
        enrollment_action=action,
        enrollment_credential="c" * 43,
    )

    _url, request = client.request
    assert request["json"]["enrollment_action"] == action
    assert request["json"]["enrollment_credential"] == "c" * 43
    assert ("X-Node-Secret" in request["headers"]) is expect_secret
    assert client_options["trust_env"] is False


@pytest.mark.asyncio
async def test_actionable_registration_error_redacts_every_credential(monkeypatch):
    credential = "c" * 43
    bootstrap = "bootstrap-secret"
    client = _RegistrationClient(
        _Response(
            409,
            {
                "detail": {
                    "code": f"node_enrollment_conflict-{credential}-{bootstrap}",
                    "message": f"bad {credential} {bootstrap}",
                    "action": f"restore {credential}",
                }
            },
        )
    )
    monkeypatch.setattr(node.httpx, "AsyncClient", lambda **kwargs: client)

    with pytest.raises(node.NodeRegistrationRejected) as raised:
        await node.register(
            "https://coordinator.example",
            "worker",
            secret=bootstrap,
            enrollment_action="bootstrap",
            enrollment_credential=credential,
        )

    assert credential not in str(raised.value)
    assert bootstrap not in str(raised.value)
    assert credential not in raised.value.code
    assert bootstrap not in raised.value.code
    assert "node_enrollment_conflict" in str(raised.value)


def test_registration_error_redacts_before_bounding_cutoff_fragments():
    credential = "CredentialCutoff" + "c" * 32
    bootstrap = "BootstrapCutoff" + "b" * 32
    session = "SessionCutoff" + "s" * 32
    response = _Response(
        409,
        {
            "detail": {
                "code": "A" * 80 + credential,
                "action": "B" * 150 + session,
                "message": "C" * 500 + bootstrap,
            }
        },
    )

    error = node._bounded_registration_error(
        response,
        sensitive_values=(bootstrap, credential, session),
    )
    rendered = f"{error.code} {error.action} {error}"

    assert "CredentialCut" not in rendered
    assert "BootstrapCu" not in rendered
    assert "SessionCutof" not in rendered
    assert "<redacted>" in rendered

@pytest.mark.asyncio
async def test_worker_automatically_reregisters_after_session_rejection(
    monkeypatch, tmp_path
):
    registrations = []

    async def healthy_ollama():
        return {"ok": True, "models": [node.DEFAULT_MODEL]}

    async def no_runtime_metadata(*_args, **_kwargs):
        return None, None, None

    async def register(
        server,
        node_id,
        secret="",
        capabilities=None,
        capability_descriptor=None,
        model=node.DEFAULT_MODEL,
        session_token="",
        enrollment_action=None,
        enrollment_credential="",
    ):
        registrations.append(
            (
                session_token,
                enrollment_action,
                bool(enrollment_credential),
                capability_descriptor,
                model,
            )
        )
        index = len(registrations)
        return {
            "message": "registered",
            "node_id": str(node_id).casefold(),
            "capabilities": [],
            "enrolled": True,
            "enrollment_action": enrollment_action,
            "enrollment_id": "11111111111141118111111111111111",
            "credential_version": 1,
            "session_id": f"session-{index}",
            "session_token": f"token-{index}",
            "session_expires_at": "2099-01-01T00:00:00+00:00",
            "capability_descriptor_version": capability_descriptor.descriptor_version,
            "capability_descriptor_hash": node.capability_descriptor_digest(
                capability_descriptor
            ),
        }

    polls = 0
    polled_models = []

    async def poll(*args, **kwargs):
        nonlocal polls
        polls += 1
        polled_models.append(kwargs.get("model"))
        if polls == 1:
            raise node.NodeSessionRejected("expired")
        raise KeyboardInterrupt

    monkeypatch.setattr(node, "check_ollama", healthy_ollama)
    monkeypatch.setattr(node, "_detect_ollama_metadata", no_runtime_metadata)
    monkeypatch.setattr(node, "register", register)
    monkeypatch.setattr(node, "poll_and_execute", poll)
    monkeypatch.setattr(node.console, "print", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "node.py",
            "--server",
            "http://server",
            "--node-id",
            "Worker",
            "--identity-file",
            str(tmp_path / "identity.json"),
        ],
    )

    await node.main()

    assert [registration[:3] for registration in registrations] == [
        ("", "bootstrap", True),
        ("token-1", "returning", True),
    ]
    assert registrations[0][3] is registrations[1][3]
    assert registrations[0][4] == registrations[1][4] == node.DEFAULT_MODEL
    assert polled_models == [node.DEFAULT_MODEL, node.DEFAULT_MODEL]
    assert polls == 2


def test_worker_refuses_legacy_downgrade_after_requesting_enrollment(tmp_path):
    identity_path = tmp_path / "identity.json"
    identity = node.load_or_create_worker_identity(
        identity_path,
        coordinator="https://coordinator.example",
        node_id="worker",
        credential_factory=lambda: "c" * 43,
    )
    session = {}

    with pytest.raises(
        node.WorkerIdentityError,
        match="did not confirm the requested durable enrollment",
    ):
        node._apply_registration(
            session,
            {
                "node_id": "worker",
                "session_id": "legacy-session",
                "session_token": "legacy-token",
                "enrolled": False,
            },
            identity=identity,
            identity_file=identity_path,
        )

    assert session == {}
