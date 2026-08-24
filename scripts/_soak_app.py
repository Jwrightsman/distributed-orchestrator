"""Server app with the model stubbed out — for scripts/soak_test.py only.

The soak test is looking for infrastructure leaks (memory, SQLite, event
buffers, orphaned tasks), none of which involve the model. Stubbing inference
turns a 20-hour CPU run into a 20-second one and makes the numbers readable,
because nothing is dominated by generation time.

Production code is untouched: the patches live here and apply only to the
process this module is loaded into.

    uvicorn scripts._soak_app:app
"""

import gc
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ollama_client  # noqa: E402
import orchestrator  # noqa: E402
import routes_pitch  # noqa: E402

_FAKE_HTML = (
    "<!DOCTYPE html>\n<html><head><title>Soak</title></head><body>"
    "<div id='app'>soak</div>"
    "<script>document.addEventListener('keydown',function(e){});</script>"
    "</body></html>"
)

_PLAN = json.dumps([
    {"id": 1, "title": "Build it", "prompt": "make the thing", "depends_on": []},
    {"id": 2, "title": "Wire it", "prompt": "connect the thing", "depends_on": [1]},
])


async def _fake_generate(prompt, system="", model=None, role=None, format=None):
    if system == orchestrator.PLANNER_SYSTEM:
        return _PLAN
    if system in (orchestrator.REVIEWER_SYSTEM, orchestrator.REVISER_SYSTEM):
        return (
            "## Quality Rating\nPASS\n\n## Issues Found\nNone\n\n"
            f"## Final Assembled Output\n\n```html\n{_FAKE_HTML}\n```\n"
        )
    return f"```html\n{_FAKE_HTML}\n```"


async def _fake_stream(*a, **k):
    yield "token"


orchestrator.generate = _fake_generate
orchestrator.generate_stream = _fake_stream
ollama_client.generate = _fake_generate
routes_pitch.generate = _fake_generate

from server import app  # noqa: E402,F401


@app.post("/_soak/collect")
async def _soak_collect():
    """Settle collectable cycles before the harness compares RSS samples."""

    collected = gc.collect()
    from execution.service import get_execution_service
    from server_state import jobs

    service = get_execution_service()
    return {
        "collected": collected,
        "jobs": len(jobs),
        "service_live_results": len(service._live_results),
        "service_requests": len(service._requests),
        "service_controls": len(service._controls),
        "service_background": len(service._background),
    }
