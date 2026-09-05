"""Waiting for background work without asserting on how fast it happens.

Several tests here start work on another task, thread, or process and then wait
a bounded amount of wall clock for it to land. The bound was usually a tenth of
a second to a couple of seconds, and when the machine missed it the test failed
on its *next* assertion -- "complete" not in the status, a queue that was empty,
an event sequence one short -- which reads as a defect in the code under test.

Under three competing pytest sessions this machine stretched comparable work by
about 45x, so those bounds were reachable by load rather than by regression.

The rule these helpers encode:

* a bound stays only where it is a deadlock guard, never as a statement about
  latency, which no test here is the subject of;
* it is sized so that reaching it means the work is never going to finish;
* and reaching it fails by naming the guard, so a stall can never be read as a
  functional failure.

`tests/conftest.py` applies the same reasoning to the parse-validator budget.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

# 120 seconds is the same ceiling `PARSE_VALIDATOR_TEST_TIMEOUT_SECONDS` uses,
# and for the same reason: it is orders of magnitude above the cost of the work
# being waited on, idle or under the storm above. Nothing here is waiting on a
# model or a network call.
BACKGROUND_WORK_DEADLOCK_GUARD_SECONDS = 120.0

_POLL_SECONDS = 0.01


def _stalled(what: str, guard_seconds: float) -> AssertionError:
    return AssertionError(
        f"the {guard_seconds}s deadlock guard expired waiting for {what}. "
        "The background work never finished, so nothing after this point is a "
        "statement about the behaviour under test — this is a stall, not a "
        "functional failure."
    )


async def await_condition(
    predicate: Callable[[], object],
    *,
    what: str,
    guard_seconds: float = BACKGROUND_WORK_DEADLOCK_GUARD_SECONDS,
    poll_seconds: float = _POLL_SECONDS,
) -> None:
    """Poll `predicate` until it is truthy, or fail naming the guard."""

    # Yield once before the first check, so a caller that has only just created
    # the background task gets the same "give the loop a turn" behaviour the
    # hand-written poll loops had.
    await asyncio.sleep(0)
    deadline = time.monotonic() + guard_seconds
    while True:
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise _stalled(what, guard_seconds)
        await asyncio.sleep(poll_seconds)


async def await_event(
    event: asyncio.Event,
    *,
    what: str,
    guard_seconds: float = BACKGROUND_WORK_DEADLOCK_GUARD_SECONDS,
) -> None:
    """`asyncio.wait_for` on an event, failing by name instead of TimeoutError."""

    try:
        await asyncio.wait_for(event.wait(), timeout=guard_seconds)
    except (asyncio.TimeoutError, TimeoutError) as expired:
        raise _stalled(what, guard_seconds) from expired


def await_condition_sync(
    predicate: Callable[[], object],
    *,
    what: str,
    guard_seconds: float = BACKGROUND_WORK_DEADLOCK_GUARD_SECONDS,
    poll_seconds: float = _POLL_SECONDS,
) -> None:
    """The blocking form, for tests that are not running an event loop."""

    deadline = time.monotonic() + guard_seconds
    while True:
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise _stalled(what, guard_seconds)
        time.sleep(poll_seconds)
