"""Trusted-alpha interface contracts consumed by the command-line client."""

import cli


def test_cli_consumes_sanitized_health_node_count():
    assert cli._health_node_count({"nodes_online": 3}) == 3
    assert cli._health_node_count({"nodes_online": "2"}) == 2
    assert cli._health_node_count({"nodes": [{"node_id": "legacy"}]}) == 0


def test_cli_renders_the_flattened_event_schema(monkeypatch):
    rendered = []
    monkeypatch.setattr(cli.console, "print", lambda *parts, **kwargs: rendered.append(" ".join(map(str, parts))))

    cli._render_event(
        {"id": 1, "type": "plan", "time": "now", "job_id": "job-1", "subtasks": ["Build API"]},
        set(),
    )

    assert any("Build API" in line for line in rendered)


def test_cli_renders_sanitized_plan_and_build_events(monkeypatch):
    rendered = []
    monkeypatch.setattr(
        cli.console,
        "print",
        lambda *parts, **kwargs: rendered.append(" ".join(map(str, parts))),
    )
    seen = set()

    cli._render_event(
        {
            "id": 2,
            "type": "plan",
            "time": "now",
            "job_id": "job-1",
            "subtask_count": 2,
        },
        seen,
    )
    cli._render_event(
        {
            "id": 3,
            "type": "build",
            "time": "now",
            "job_id": "job-1",
            "subtask_id": 1,
        },
        seen,
    )

    output = "\n".join(rendered)
    assert "2 subtasks planned" in output
    assert "BUILDER 1" in output
