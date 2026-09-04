"""Prove the coordinator runs on a machine with no OpenTelemetry at all.

Run as a subprocess by `tests/test_tracing.py`. It exists as a file rather than
an embedded string because it has to install an import blocker *before* the
first `import server`, and because a claim about a clean machine should be
readable as ordinary code.

`opentelemetry-api` is present in this repository's environment as a transitive
dependency of `mcp`, so simply asserting "it works here" would be asserting it
on an environment that cannot falsify it. This refuses every `opentelemetry`
import and then does the things a coordinator has to be able to do.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Run as a script, so sys.path[0] is tests/ rather than the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _RefuseOpenTelemetry:
    """A meta-path finder that makes the optional extra genuinely absent."""

    def find_spec(self, name, path=None, target=None):
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError(f"{name} is blocked for this test")
        return None


def main() -> int:
    for name in [n for n in sys.modules if n.startswith("opentelemetry")]:
        del sys.modules[name]
    sys.meta_path.insert(0, _RefuseOpenTelemetry())

    import server
    import tracing

    assert server.app is not None, "the coordinator did not import"

    tracing.reset_bridge_for_tests()
    tracing.get_config = lambda: {"tracing_enabled": True, "tracing_export": True}

    # Propagation works with nothing installed; export honestly reports off.
    assert tracing.propagation_enabled() is True
    assert tracing.export_enabled() is False, "export claimed to work with no SDK"

    with tracing.span("probe", attributes=tracing.attributes(task_id="t")) as handle:
        assert handle.headers()["traceparent"].startswith("00-")
        assert handle.record.attributes == {"mycelium.task_id": "t"}

    # And off is still off.
    tracing.get_config = lambda: {"tracing_enabled": False}
    with tracing.span("probe") as disabled:
        assert disabled.headers() == {}

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
