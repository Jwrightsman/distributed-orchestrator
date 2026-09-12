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

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from tests.dependency_pins import read_pins

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


# ── Dev dependencies a contributor is told to use ────────────────────

def _dev_requirements() -> list[str]:
    lines = (REPO / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def test_test_and_lint_tools_are_declared():
    """CONTRIBUTING says to run `pytest -q` and `ruff check .`.

    These used to be hardcoded inside .github/workflows/ci.yml and declared
    nowhere else, so a fresh clone following CONTRIBUTING answered `pytest -q`
    with ModuleNotFoundError. Verified on a real clean venv, Aug 12 2026.
    """
    declared = " ".join(_dev_requirements()).lower()
    for tool in ("pytest", "pytest-asyncio", "ruff"):
        assert tool in declared, f"{tool} missing from requirements-dev.txt"


def test_ci_installs_the_declared_dev_requirements():
    """The guard that matters: if CI keeps its own hardcoded list, the two drift
    again and only a stranger finds out."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "requirements-dev.txt" in ci, (
        "CI must install from requirements-dev.txt, not a hardcoded package list, "
        "or contributors get a different environment from CI"
    )


# ── Exact pins: what CI and the image actually install ───────────────
#
# requirements.txt only sets minimums, so until 2026-09-12 every CI run and
# image build took the newest release of everything. FastAPI 0.137 changed
# `app.routes` from a flat list into a tree; CI had it, this machine did not,
# and a test that passed locally failed CI on PR #88. constraints.txt now pins
# the tested versions, and these guards keep the pins from quietly leaking.

CANARY = "dependency-canary.yml"


def _dependency_installs() -> list[tuple[str, str]]:
    """Every `pip install -r requirements...` line in CI workflows and the Dockerfile."""
    installs = []
    for workflow in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        if workflow.name == CANARY:
            continue
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if "pip install" in line and "-r requirements" in line:
                installs.append((workflow.name, line.strip()))
    for line in (REPO / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if "pip install" in line and "-r requirements" in line:
            installs.append(("Dockerfile", line.strip()))
    return installs


def test_every_declared_dependency_has_an_exact_pin_that_satisfies_it():
    """A dependency added to requirements without a pin would float in CI again."""
    pins = read_pins()
    for line in _requirements() + _dev_requirements():
        requirement = Requirement(line)
        name = canonicalize_name(requirement.name)
        assert name in pins, (
            f"{requirement.name} is declared but has no pin in constraints.txt, "
            "so CI and the image would install whatever is newest"
        )
        assert requirement.specifier.contains(pins[name], prereleases=True), (
            f"constraints.txt pins {name}=={pins[name]}, outside the declared {line!r}"
        )


def test_transitive_packages_that_change_behaviour_are_pinned_too():
    """Nothing declares these, which is exactly why a direct-only pin misses them.

    FastAPI accepts any Starlette from 0.46 up with no ceiling, and Starlette's
    TestClient runs on httpx2 when it is installed and on httpx when it is not.
    """
    pins = read_pins()
    for package in ("starlette", "httpx2", "pydantic-core", "anyio"):
        assert package in pins, f"{package} is not pinned in constraints.txt"


def test_ci_and_the_image_install_with_the_constraints():
    installs = _dependency_installs()
    where = {source for source, _ in installs}
    assert {"ci.yml", "trusted-alpha-nightly.yml", "Dockerfile"} <= where, (
        f"expected installs in ci.yml, the nightly, and the Dockerfile; found {sorted(where)}"
    )
    unpinned = [f"{source}: {line}" for source, line in installs if "-c constraints.txt" not in line]
    assert not unpinned, "these installs ignore constraints.txt:\n" + "\n".join(unpinned)

    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY\b.*\bconstraints\.txt\b", dockerfile, re.MULTILINE), (
        "the Dockerfile installs with -c constraints.txt but never copies the file in"
    )


def test_the_canary_alone_installs_the_newest_releases():
    """Without it the pins would only ever go stale; with the pins it cannot break a PR."""
    canary = (REPO / ".github" / "workflows" / CANARY).read_text(encoding="utf-8")
    assert "pip install -r requirements.txt -r requirements-dev.txt" in canary
    assert "-c constraints.txt" not in canary, "the canary must test unpinned releases"
    assert "pytest -q" in canary and "schedule:" in canary
