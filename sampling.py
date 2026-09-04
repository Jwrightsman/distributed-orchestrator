"""Generator sampling parameters, and the difference between set and honoured.

`config.json` had no temperature and no seed, so every run this project has
ever made took whatever Ollama defaults to — documented as temperature 0.8 and
seed 0. `docs/eval-methodology.md` measures the run-to-run noise floor at
psi = 0.643 and `evals/runrecord.py` records both parameters as *unknown*,
which is honest but leaves the generator's own settings uncontrolled across
arms. This module is the setting that closes that gap.

**Set is not the same as honoured, and the distinction is the whole point.**

* *Verified*, from the Ollama API documentation and from the request path in
  `ollama_client.py`: `POST /api/generate` takes an `options` object, and
  `seed` and `temperature` are both valid keys in it. This project already
  sends `options: {"num_ctx": ...}` on every generate and every stream, so a
  configured value reaches the request the client actually sends, and
  `tests/test_sampling.py` asserts exactly that against the outbound body.
  Ollama documents the defaults as temperature 0.8 and seed 0, and documents
  seed as "setting this to a specific number will make the model generate the
  same text for the same prompt".
* *Assumed, and therefore not claimed*: that the runner **honours** the seed
  to the point of reproducing an identical completion for `qwen3.5:4b` on this
  hardware. That is a property of a model and a runtime, not of a request
  field. Batch size, thread count, KV-cache reuse and continuous batching can
  all move logits between two runs that sent the same seed, and none of that
  has been measured here.

So `SEED_HONOURING` is `"assumed"`, and every consumer treats an assumed seed
as **unpinned**: `Sampling.pinned` is False, and `evals/runrecord.py` lists
`model_seed_honoured` among the facts a run could not establish. A seed that
was accepted but never shown to be honoured is not a pinned generator, and
recording it as one would be the same class of mistake as calling `browser_ok`
an execution check.

`docs/experiments/noise-floor-under-pinned-sampling.md` says what measurement
moves this constant to `"verified"`. It is deliberately a constant here rather
than a configuration key: honouring is something that gets measured, not
something an operator asserts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "MAX_SEED",
    "MAX_TEMPERATURE",
    "MIN_SEED",
    "MIN_TEMPERATURE",
    "OLLAMA_DEFAULT_SEED",
    "OLLAMA_DEFAULT_TEMPERATURE",
    "SEED_HONOURING",
    "SEED_HONOURING_ASSUMED",
    "SEED_HONOURING_UNSET",
    "SEED_HONOURING_VERIFIED",
    "Sampling",
    "UNKNOWN_SEED",
    "UNKNOWN_SEED_HONOURED",
    "UNKNOWN_TEMPERATURE",
    "from_config",
    "valid_seed",
    "valid_temperature",
]

# Ollama's own documented defaults, recorded so a reader can tell what an unset
# parameter means without going to look. They are NOT written into config as if
# somebody had chosen them: unset stays unset and records as unknown, because
# "we took the default" and "we chose 0.8" are different situations and only one
# of them supports a comparison.
OLLAMA_DEFAULT_TEMPERATURE = 0.8
OLLAMA_DEFAULT_SEED = 0

MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0
MIN_SEED = 0
MAX_SEED = 2**31 - 1

SEED_HONOURING_UNSET = "unset"
SEED_HONOURING_ASSUMED = "assumed"
SEED_HONOURING_VERIFIED = "verified"

# The current state of the evidence. See the module docstring: accepted by the
# API is established, honoured by the runner is not.
SEED_HONOURING = SEED_HONOURING_ASSUMED

UNKNOWN_TEMPERATURE = "model_temperature"
UNKNOWN_SEED = "model_seed"
UNKNOWN_SEED_HONOURED = "model_seed_honoured"


def valid_temperature(value: object) -> bool:
    """Whether a configured temperature is usable. None means unset, not zero."""
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return MIN_TEMPERATURE <= float(value) <= MAX_TEMPERATURE


def valid_seed(value: object) -> bool:
    """Whether a configured seed is usable. None means unset, not zero.

    Zero is a legal seed and is Ollama's documented default, which is exactly
    why unset has to be None rather than 0 — otherwise "nobody set a seed" and
    "somebody deliberately chose Ollama's default" become the same record.
    """
    if value is None:
        return True
    if isinstance(value, bool) or type(value) is not int:
        return False
    return MIN_SEED <= value <= MAX_SEED


@dataclass(frozen=True)
class Sampling:
    """What this run asked the generator for, and how strong that claim is."""

    temperature: float | None = None
    seed: int | None = None
    seed_honouring: str = SEED_HONOURING_UNSET

    @property
    def pinned(self) -> bool:
        """True only when the generator is genuinely fixed.

        A seed whose honouring is `assumed` does not make a run pinned. The
        study that needs a pinned generator is entitled to know the difference.
        """
        return (
            self.temperature is not None
            and self.seed is not None
            and self.seed_honouring == SEED_HONOURING_VERIFIED
        )

    def ollama_options(self) -> dict[str, Any]:
        """The keys to merge into `options` on `POST /api/generate`.

        Empty when nothing is set — an unset parameter is left off the request
        entirely so Ollama applies its own default, rather than this project
        writing that default in and later being unable to tell which happened.
        """
        options: dict[str, Any] = {}
        if self.temperature is not None:
            options["temperature"] = float(self.temperature)
        if self.seed is not None:
            options["seed"] = int(self.seed)
        return options

    def provider_fields(self) -> dict[str, Any]:
        """The same two parameters for an OpenAI-compatible chat completion.

        Top-level there rather than nested in `options`. Honouring is at least
        as unverified on a hosted provider as it is on Ollama, so nothing about
        `pinned` changes on this path.
        """
        fields: dict[str, Any] = {}
        if self.temperature is not None:
            fields["temperature"] = float(self.temperature)
        if self.seed is not None:
            fields["seed"] = int(self.seed)
        return fields

    def unknown_facts(self) -> list[str]:
        """The sampling facts this run could not establish.

        A set-but-unverified seed appears here, because the run cannot support
        the claim that its generator was pinned.
        """
        missing: list[str] = []
        if self.temperature is None:
            missing.append(UNKNOWN_TEMPERATURE)
        if self.seed is None:
            missing.append(UNKNOWN_SEED)
        elif self.seed_honouring != SEED_HONOURING_VERIFIED:
            missing.append(UNKNOWN_SEED_HONOURED)
        return missing

    def as_record(self) -> dict[str, Any]:
        """The serialised form used by run records and the provenance envelope."""
        return {
            "temperature": None if self.temperature is None else float(self.temperature),
            "seed": None if self.seed is None else int(self.seed),
            "seed_honouring": self.seed_honouring,
            "pinned": self.pinned,
            "unknown_facts": self.unknown_facts(),
        }


def from_config(settings: Mapping[str, Any] | None = None) -> Sampling:
    """Read the configured sampling parameters, refusing to invent either one.

    An invalid value is treated as unset rather than clamped. `config.load`
    already rejects or warns about one; arriving here with a bad value means
    something bypassed that, and silently coercing it to a number is how a run
    ends up recording a temperature nobody chose.
    """
    if settings is None:
        import config

        settings = config.get()
    temperature = settings.get("temperature")
    seed = settings.get("seed")
    if not valid_temperature(temperature):
        temperature = None
    if not valid_seed(seed):
        seed = None
    return Sampling(
        temperature=None if temperature is None else float(temperature),
        seed=None if seed is None else int(seed),
        seed_honouring=SEED_HONOURING_UNSET if seed is None else SEED_HONOURING,
    )
