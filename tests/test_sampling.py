"""Pinning the generator: what reaches the request, and what is only claimed.

Three properties, and the third is the one that matters:

1. **A configured temperature and seed reach the request the client sends.**
   Not the config object, not a helper's return value — the JSON body handed to
   `POST /api/generate`. A setting that stops one layer short of the wire is
   the shape of a control that quietly does nothing.

2. **An unset parameter records as unknown, never as a default.** Ollama
   documents its own defaults as temperature 0.8 and seed 0. Writing those in
   would make "nobody chose" indistinguishable from "somebody chose", which is
   exactly the distinction a study needs.

3. **A seed that was set but not shown to be honoured is recorded as
   unpinned.** Ollama accepting the field is established from its API docs and
   from the request below. The runner reproducing an identical completion for
   this model on this hardware is not, and nothing here has measured it. A
   record that called that pinned would be `browser_ok` again: a weaker check
   standing in for the one everybody assumes was made.

Plus the guard that keeps this change honest: **production defaults did not
move.** Both parameters ship unset, so the request carries neither key and the
shipping behaviour is bit-for-bit what it was.
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import config  # noqa: E402
import ollama_client  # noqa: E402
import sampling  # noqa: E402


# -- what ships -------------------------------------------------------------

def test_production_defaults_ship_unpinned():
    """The whole point of the change is that it changes nothing by default."""
    assert config.DEFAULTS, "config has no defaults — this test read nothing"
    assert "temperature" in config.DEFAULTS
    assert "seed" in config.DEFAULTS
    assert config.DEFAULTS["temperature"] is None
    assert config.DEFAULTS["seed"] is None


def test_no_other_generation_default_moved():
    """The settings this change sits next to, pinned so a later edit is visible."""
    assert config.DEFAULTS["model"] == "qwen3.5:4b"
    assert config.DEFAULTS["context_tokens"] == 8192
    assert config.DEFAULTS["timeout"] == 1800
    assert config.DEFAULTS["think"] is False
    assert config.DEFAULTS["planner_retries"] == 3


def test_the_shipping_default_sends_no_sampling_keys():
    loaded = config.load()
    assert loaded, "config.load returned nothing — this test read nothing"
    options = sampling.from_config(loaded).ollama_options()
    assert options == {}


# -- validation -------------------------------------------------------------

@pytest.mark.parametrize(
    "value,ok",
    [(None, True), (0.0, True), (0.8, True), (2.0, True), (2.1, False),
     (-0.1, False), ("0.5", False), (True, False)],
)
def test_temperature_validation(value, ok):
    assert sampling.valid_temperature(value) is ok


@pytest.mark.parametrize(
    "value,ok",
    [(None, True), (0, True), (7, True), (2**31 - 1, True), (2**31, False),
     (-1, False), (1.5, False), ("7", False), (True, False)],
)
def test_seed_validation(value, ok):
    assert sampling.valid_seed(value) is ok


def test_zero_is_a_seed_and_none_is_not(tmp_path):
    """Ollama's default seed is 0, so unset cannot be represented by 0."""
    assert sampling.from_config({"seed": 0}).seed == 0
    assert sampling.from_config({"seed": 0}).ollama_options() == {"seed": 0}
    assert sampling.from_config({}).seed is None
    assert sampling.from_config({}).ollama_options() == {}


def test_an_out_of_range_value_is_rejected_rather_than_clamped(tmp_path, caplog):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"temperature": 9.0, "seed": -5}), encoding="utf-8")
    loaded = config.load(path)
    assert loaded, "config.load returned nothing — this test read nothing"
    assert loaded["temperature"] is None
    assert loaded["seed"] is None


def test_a_trusted_alpha_installation_refuses_an_invalid_value(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"temperature": 9.0}), encoding="utf-8")
    with pytest.raises(config.ConfigError, match="temperature"):
        config.load(path, strict=True)
    path.write_text(json.dumps({"seed": "abc"}), encoding="utf-8")
    with pytest.raises(config.ConfigError, match="seed"):
        config.load(path, strict=True)


def test_a_configured_value_survives_a_round_trip_through_the_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"temperature": 0.0, "seed": 20260904}), encoding="utf-8")
    loaded = config.load(path)
    assert loaded["temperature"] == 0.0
    assert loaded["seed"] == 20260904
    resolved = sampling.from_config(loaded)
    assert resolved.ollama_options() == {"temperature": 0.0, "seed": 20260904}


# -- the outbound request ---------------------------------------------------

class _Response:
    """Serves both shapes: Ollama's `response` and a chat completion's `choices`."""

    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "response": "ok",
            "choices": [{"message": {"content": "ok"}}],
        }


def _client_capturing(captured):
    """An httpx.AsyncClient stand-in that records the generate request body."""

    class _Capabilities:
        def raise_for_status(self):
            pass

        def json(self):
            return {"capabilities": ["completion"]}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            if url.endswith("/api/show"):
                return _Capabilities()
            captured.append({"url": url, "body": json})
            return _Response()

        def stream(self, method, url, json=None):
            captured.append({"url": url, "body": json})

            class _Stream:
                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, *a):
                    return False

                def raise_for_status(self_inner):
                    pass

                async def aiter_lines(self_inner):
                    yield '{"response": "ok", "done": true}'

            return _Stream()

    return _Client


@pytest.mark.asyncio
async def test_a_configured_temperature_and_seed_reach_the_generate_request(monkeypatch):
    captured = []
    settings = dict(config.DEFAULTS, temperature=0.0, seed=20260904)
    monkeypatch.setattr(ollama_client, "get_config", lambda: settings)
    monkeypatch.setattr(ollama_client.httpx, "AsyncClient", _client_capturing(captured))

    await ollama_client.generate("hello")

    assert captured, "no request was captured — this test asserted nothing"
    body = captured[-1]["body"]
    assert captured[-1]["url"].endswith("/api/generate")
    assert body["options"]["temperature"] == 0.0
    assert body["options"]["seed"] == 20260904
    # The context window setting is still there — sampling was merged in, not
    # written over the top of it.
    assert body["options"]["num_ctx"] == settings["context_tokens"]


@pytest.mark.asyncio
async def test_a_configured_temperature_and_seed_reach_the_stream_request(monkeypatch):
    """The streaming path is a separate payload and has to be checked separately.

    A worker node generates through `generate_stream`, so a setting that only
    reached `generate` would pin the coordinator and leave every distributed
    builder unpinned.
    """
    captured = []
    settings = dict(config.DEFAULTS, temperature=0.2, seed=11)
    monkeypatch.setattr(ollama_client, "get_config", lambda: settings)
    monkeypatch.setattr(ollama_client.httpx, "AsyncClient", _client_capturing(captured))

    tokens = [chunk async for chunk in ollama_client.generate_stream("hello")]

    assert tokens == ["ok"]
    assert captured, "no request was captured — this test asserted nothing"
    body = captured[-1]["body"]
    assert body["stream"] is True
    assert body["options"]["temperature"] == 0.2
    assert body["options"]["seed"] == 11


@pytest.mark.asyncio
async def test_an_unset_parameter_is_absent_from_the_request(monkeypatch):
    """Absent, not defaulted. Ollama then applies its own 0.8 and 0."""
    captured = []
    settings = dict(config.DEFAULTS)
    assert settings["temperature"] is None and settings["seed"] is None
    monkeypatch.setattr(ollama_client, "get_config", lambda: settings)
    monkeypatch.setattr(ollama_client.httpx, "AsyncClient", _client_capturing(captured))

    await ollama_client.generate("hello")

    assert captured, "no request was captured — this test asserted nothing"
    options = captured[-1]["body"]["options"]
    assert "temperature" not in options
    assert "seed" not in options
    assert options["num_ctx"] == settings["context_tokens"]


@pytest.mark.asyncio
async def test_an_external_provider_gets_the_same_two_parameters(monkeypatch):
    captured = []
    settings = dict(
        config.DEFAULTS,
        temperature=0.0,
        seed=5,
        provider="openai",
        provider_api_key="k",
        provider_model="gpt-4o-mini",
        provider_roles=["planner"],
    )
    monkeypatch.setattr(ollama_client, "get_config", lambda: settings)
    monkeypatch.setattr(ollama_client.httpx, "AsyncClient", _client_capturing(captured))

    await ollama_client.generate("hello", role="planner")

    assert captured, "no request was captured — this test asserted nothing"
    body = captured[-1]["body"]
    assert captured[-1]["url"].endswith("/chat/completions")
    # Top level here, not nested in an options object.
    assert body["temperature"] == 0.0
    assert body["seed"] == 5


# -- set is not honoured ----------------------------------------------------

def test_a_set_seed_is_not_a_pinned_generator():
    resolved = sampling.from_config({"temperature": 0.0, "seed": 7})
    assert resolved.seed == 7
    assert resolved.seed_honouring == sampling.SEED_HONOURING_ASSUMED
    assert resolved.pinned is False
    assert sampling.UNKNOWN_SEED_HONOURED in resolved.unknown_facts()


def test_only_verified_honouring_counts_as_pinned():
    verified = sampling.Sampling(
        temperature=0.0, seed=7, seed_honouring=sampling.SEED_HONOURING_VERIFIED
    )
    assert verified.pinned is True
    assert verified.unknown_facts() == []


def test_the_honouring_status_is_not_an_operator_setting():
    """It is measured, not asserted, so config cannot flip it.

    `sampling.SEED_HONOURING` is a module constant with the evidence written
    next to it. If it were a config key, a study could declare its generator
    pinned without anyone running the check that would establish it.
    """
    assert sampling.SEED_HONOURING == sampling.SEED_HONOURING_ASSUMED
    assert "seed_honouring" not in config.DEFAULTS
    assert "seed_honouring_verified" not in config.DEFAULTS


def test_an_unset_temperature_and_seed_record_as_unknown():
    resolved = sampling.from_config({})
    record = resolved.as_record()
    assert record["temperature"] is None
    assert record["seed"] is None
    assert record["pinned"] is False
    assert sampling.UNKNOWN_TEMPERATURE in record["unknown_facts"]
    assert sampling.UNKNOWN_SEED in record["unknown_facts"]
    # Not the honouring fact — no seed was set, so there is nothing to honour.
    assert sampling.UNKNOWN_SEED_HONOURED not in record["unknown_facts"]


def test_ollamas_documented_defaults_are_recorded_but_never_written_in():
    """They are reference, not values this project chose."""
    assert sampling.OLLAMA_DEFAULT_TEMPERATURE == 0.8
    assert sampling.OLLAMA_DEFAULT_SEED == 0
    assert sampling.from_config({}).ollama_options() == {}
