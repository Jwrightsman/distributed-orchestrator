"""Opt-in runtime attempt accounting; no prompt or response text is retained."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import inspect
import math
import time
import uuid


_collector = ContextVar("runtime_call_collector", default=None)
_identity = ContextVar("runtime_call_identity", default={})


@contextmanager
def call_identity(**identity):
    token = _identity.set({**_identity.get(), **identity})
    try:
        yield
    finally:
        _identity.reset(token)


@contextmanager
def capture_calls(collector, **identity):
    token = _collector.set(collector)
    try:
        with call_identity(**identity):
            yield collector
    finally:
        _collector.reset(token)


def current_collector():
    return _collector.get()


def _counter(value):
    return value if type(value) is int and value >= 0 else None


async def _callback(callback, *args):
    if callback:
        result = callback(*args)
        if inspect.isawaitable(result):
            await result


class CallCollector:
    def __init__(self, hardware_id, warm_cold="unknown", before_dispatch=None,
                 after_finish=None, allow_schema_retry=False):
        self.hardware_id = hardware_id
        self.warm_cold = warm_cold
        self.before_dispatch = before_dispatch
        self.after_finish = after_finish
        self.allow_schema_retry = allow_schema_retry
        self.records = []
        self.dispatch_blocked = False

    @property
    def reconciled(self):
        return all(r["end_monotonic"] is not None for r in self.records)

    @property
    def cost_capture_complete(self):
        return bool(self.records) and self.reconciled and all(
            r["termination_confirmed"] and all(r[k] is not None for k in
            ("input_tokens", "output_tokens", "inference_seconds"))
            and r["warm_cold"] in {"warm", "cold"}
            for r in self.records
        )

    async def start(self, payload, provider, role=None, attempt=0):
        if self.dispatch_blocked:
            raise RuntimeError("Runtime termination/cost unknown; further dispatch blocked")
        record = {
            "execution_id": None, "unit_id": None, "candidate_id": None, "attempt_id": None,
            **_identity.get(), "call_id": uuid.uuid4().hex,
            "role": role or _identity.get().get("role", "unknown"),
            "attempt": attempt, "provider": provider,
            "hardware_id": self.hardware_id,
            "warm_cold": self.warm_cold() if callable(self.warm_cold) else self.warm_cold,
            "start_monotonic": None, "end_monotonic": None,
            "input_tokens": None, "output_tokens": None,
            "inference_seconds": None, "termination_confirmed": False,
            "outcome": "started",
        }
        await _callback(self.before_dispatch, record, payload, provider)
        record["start_monotonic"] = time.monotonic()
        self.records.append(record)
        return record

    async def finish(self, record, data=None, outcome="completed", confirmed=False):
        if record["end_monotonic"] is not None:
            return
        record["end_monotonic"] = time.monotonic()
        record["outcome"] = outcome
        record["termination_confirmed"] = confirmed
        data = data if isinstance(data, dict) else {}
        if record["provider"] == "ollama":
            record["input_tokens"] = _counter(data.get("prompt_eval_count"))
            record["output_tokens"] = _counter(data.get("eval_count"))
            duration = _counter(data.get("total_duration"))
            record["inference_seconds"] = duration / 1e9 if duration is not None else None
        else:
            usage = data.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            record["input_tokens"] = _counter(usage.get("prompt_tokens"))
            record["output_tokens"] = _counter(usage.get("completion_tokens"))
            # Provider wall latency is not measured inference hardware work.
        if not confirmed or any(record[key] is None for key in ("input_tokens", "output_tokens", "inference_seconds")):
            self.dispatch_blocked = True
        await _callback(self.after_finish, record)


async def captured_post(client, url, *, payload, provider="ollama", role=None,
                        attempt=0, headers=None):
    collector = current_collector()
    kwargs = {"json": payload}
    if headers is not None:
        kwargs["headers"] = headers
    if collector is None:
        return await client.post(url, **kwargs)
    record = await collector.start(payload, provider, role, attempt)
    data = None
    confirmed = False
    outcome = "abandoned"
    try:
        timeout = record.get("timeout_seconds")
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise RuntimeError("Invalid runtime call deadline")
        async with asyncio.timeout(timeout):
            response = await client.post(url, **kwargs)
            try:
                data = response.json()
            except (ValueError, TypeError):
                pass
            confirmed = isinstance(data, dict) and (
                data.get("done") is True if provider == "ollama" else "choices" in data
            )
            outcome = "completed" if response.status_code < 400 else "rejected"
            return response
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        await collector.finish(record, data, outcome, confirmed)


@asynccontextmanager
async def captured_stream(client, url, *, payload, role=None):
    collector = current_collector()
    record = await collector.start(payload, "ollama", role) if collector else None
    state = {"data": None, "confirmed": False, "outcome": "abandoned"}
    try:
        async with asyncio.timeout(record.get("timeout_seconds") if record else None):
            async with client.stream("POST", url, json=payload) as response:
                yield response, state
    except asyncio.CancelledError:
        state["outcome"] = "cancelled"
        raise
    finally:
        if collector:
            await collector.finish(record, state["data"], state["outcome"], state["confirmed"])
