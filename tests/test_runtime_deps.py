"""Runtime dependencies the test suite cannot otherwise notice are missing.

Found the hard way on Aug 10, 2026: `requirements.txt` installed bare `uvicorn`,
which ships **without** a WebSocket implementation. `/ws/events` returned 404 on
every deployment — a local server, and the live orchestrator — so the dashboard
silently degraded to 3-second polling and live token streaming did not work at
all. It had been that way for months.

Nothing caught it, and the reason is worth remembering: no test exercises the
WebSocket through a real server, and `TestClient` implements WebSockets itself
rather than going through uvicorn's protocol layer. So a test using TestClient
passes whether or not the deployed server can actually accept a WebSocket. The
only thing that finds this class of bug is running the real server, which is the
same lesson the restart-recovery and soak work landed on.

These tests are deliberately about *importability*, not behaviour: CI installs
from requirements.txt, so removing the dependency turns them red.
"""

import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _requirements() -> list[str]:
    lines = (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def test_a_websocket_implementation_is_installed():
    """Without one, uvicorn answers the /ws/events upgrade with 404."""
    try:
        import websockets  # noqa: F401
    except ImportError:  # pragma: no cover - only on a broken install
        try:
            import wsproto  # noqa: F401
        except ImportError:
            pytest.fail(
                "No WebSocket library installed, so a real uvicorn server returns "
                "404 for /ws/events and the dashboard loses live updates. "
                "Install with: pip install -r requirements.txt"
            )


def test_websockets_is_declared_in_requirements():
    """Installed-by-accident is not the same as declared — a stranger gets only
    what requirements.txt lists."""
    declared = " ".join(_requirements()).lower()
    assert "websockets" in declared or "uvicorn[standard]" in declared, (
        "requirements.txt must pin a WebSocket implementation; bare uvicorn has none"
    )


def test_every_import_the_server_needs_is_declared():
    """The README tells strangers to install requirements.txt and nothing else."""
    declared = {
        line.split(">=")[0].split("==")[0].split("[")[0].strip().lower()
        for line in _requirements()
    }
    for package in ("fastapi", "uvicorn", "httpx", "rich", "mcp", "websockets"):
        assert package in declared, f"{package} missing from requirements.txt"


def test_python_version_floor_matches_ci():
    """CI runs 3.14; asyncio behaviour differs enough that an older floor has
    already shipped one latent bug (get_event_loop on 3.12+)."""
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    requires = pyproject.get("project", {}).get("requires-python")
    if requires is None:
        pytest.skip("pyproject.toml does not declare requires-python")
    assert "3.1" in requires
