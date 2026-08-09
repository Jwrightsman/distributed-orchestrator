"""Versioned prompt sets.

The four system prompts decide the entire quality of the output, so tuning them
is the core of Phase 2. Editing them in place makes runs incomparable and
regressions invisible — you cannot tell whether last week's number came from
this wording or a different one.

A prompt set is a frozen, named bundle of the four. Add a new one, measure it
against the current one, and only promote it if the score moves:

    python evals/run_evals.py --only web_app                    # active set
    python evals/run_evals.py --only web_app --prompt-set v2    # candidate

Selection order: an explicit `--prompt-set` / `apply_prompt_set()` call, then
the `PROMPT_SET` environment variable (so a server subprocess inherits it),
then `prompt_set` in config.json, then `v1`.

**v1 is the baseline and must never be edited.** Every recorded score refers to
it. Changing it silently invalidates the history.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSet:
    name: str
    description: str
    planner: str
    builder: str
    reviewer: str
    reviser: str


from prompts import v1 as _v1  # noqa: E402
from prompts import v2 as _v2  # noqa: E402
from prompts import v3 as _v3  # noqa: E402

_SETS: dict[str, PromptSet] = {
    _v1.PROMPTS.name: _v1.PROMPTS,
    _v2.PROMPTS.name: _v2.PROMPTS,
    _v3.PROMPTS.name: _v3.PROMPTS,
}

DEFAULT_SET = "v3"  # promoted Aug 8: 61% vs v1 36% on the 28-prompt eval set


def list_prompt_sets() -> list[PromptSet]:
    return [_SETS[k] for k in sorted(_SETS)]


def get_prompt_set(name: str) -> PromptSet:
    if name not in _SETS:
        available = ", ".join(sorted(_SETS))
        raise KeyError(f"Unknown prompt set {name!r}. Available: {available}")
    return _SETS[name]


def resolve_default_name() -> str:
    """Which set to load at import time, before anyone selects one explicitly."""
    env = os.environ.get("PROMPT_SET", "").strip()
    if env:
        return env
    try:
        from config import get as get_config

        configured = str(get_config().get("prompt_set", "") or "").strip()
        if configured:
            return configured
    except Exception:
        pass
    return DEFAULT_SET
