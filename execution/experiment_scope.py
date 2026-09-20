"""Internal experiment controls; no request fields or production strategies."""

from contextlib import contextmanager
from contextvars import ContextVar

_single_attempt = ContextVar("experiment_single_attempt", default=False)


def single_attempt_enabled():
    return _single_attempt.get()


@contextmanager
def single_attempt_calls():
    token = _single_attempt.set(True)
    try:
        yield
    finally:
        _single_attempt.reset(token)
