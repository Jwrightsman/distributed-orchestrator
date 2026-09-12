"""Read constraints.txt, the exact versions CI and the Docker image install.

Shared by the drift report at the end of every pytest run (conftest.py) and the
guards in test_runtime_deps.py, so both agree on what a pin is.
"""

from pathlib import Path

from packaging.utils import canonicalize_name

CONSTRAINTS = Path(__file__).resolve().parent.parent / "constraints.txt"


def read_pins() -> dict[str, str]:
    """Canonical package name -> pinned version. Every entry must be `name==version`."""
    pins: dict[str, str] = {}
    for raw in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, separator, version = line.partition("==")
        if not separator or not version.strip():
            raise ValueError(f"constraints.txt entry is not an exact pin: {raw!r}")
        pins[canonicalize_name(name)] = version.strip()
    return pins
