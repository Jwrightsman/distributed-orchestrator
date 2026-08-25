import asyncio
from io import StringIO

from rich.console import Console

import status


def test_status_reports_three_authorities_without_secret_fragments(monkeypatch):
    secrets = {
        "node_secret": "node-prefix-must-not-leak",
        "pitch_key": "pitch-prefix-must-not-leak",
        "viewer_key": "viewer-prefix-must-not-leak",
    }
    monkeypatch.setattr(
        status,
        "get_config",
        lambda: {
                **secrets,
                "deployment_mode": "trusted_alpha",
                "node_enrollment_mode": "required",
            "model": "test-model",
            "timeout": 30,
            "planner_retries": 1,
            "ollama_url": "http://127.0.0.1:11434",
            "port": 8000,
        },
    )

    async def ollama_down():
        return {"ok": False, "error": "not running", "models": []}

    monkeypatch.setattr(status, "check_ollama", ollama_down)
    monkeypatch.setattr(status.sys, "argv", ["status.py"])
    output = StringIO()
    monkeypatch.setattr(
        status,
        "console",
        Console(file=output, force_terminal=False, color_system=None),
    )

    asyncio.run(status.main())

    rendered = output.getvalue()
    assert "Mode:        trusted_alpha" in rendered
    assert "Enrollment:  required" in rendered
    assert "Bootstrap:   protected" in rendered
    assert "Pitch auth:  enabled" in rendered
    assert "Viewer auth: enabled" in rendered
    assert all(secret not in rendered for secret in secrets.values())
    assert "node-" not in rendered
    assert "pitch-" not in rendered
    assert "viewer-" not in rendered
