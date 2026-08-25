"""Tests for the contribution ledger (runs against a temp CWD — see conftest)."""

import json
import sqlite3
from pathlib import Path

import pytest

import orchestrator
import routes_events
import routes_pitch
import server_state
from ledger import (
    ensure_contribution_schema,
    get_history,
    get_standings,
    log_contribution,
)


def test_log_creates_ledger_file():
    log_contribution("node-a", "compute", credits=5, task="build")
    data = json.loads(Path("ledger.json").read_text())
    assert len(data) == 1
    assert data[0]["contributor"] == "node-a"
    assert data[0]["credits"] == 5


def test_standings_aggregates_credits():
    log_contribution("node-a", "compute", credits=5)
    log_contribution("node-a", "compute", credits=5)
    log_contribution("node-b", "pitch", credits=1)
    standings = get_standings()
    assert standings[0]["contributor"] == "node-a"
    assert standings[0]["total_credits"] == 10
    assert standings[0]["compute_tasks"] == 2
    assert standings[1]["contributor"] == "node-b"
    assert standings[1]["pitches"] == 1


def test_standings_sorted_by_credits_desc():
    log_contribution("small", "compute", credits=1)
    log_contribution("big", "compute", credits=100)
    standings = get_standings()
    assert [s["contributor"] for s in standings] == ["big", "small"]


def test_standings_empty_ledger():
    assert get_standings() == []


def test_history_filter_and_limit():
    for i in range(5):
        log_contribution("node-a", "compute", credits=i)
    log_contribution("node-b", "pitch", credits=1)
    assert len(get_history("node-a")) == 5
    assert len(get_history("node-a", limit=2)) == 2
    assert all(e["contributor"] == "node-b" for e in get_history("node-b"))


def test_standings_use_enrollment_or_legacy_session_not_reusable_label():
    log_contribution("shared", "compute", credits=1)
    log_contribution(
        "shared",
        "compute",
        credits=2,
        enrollment_id="enrollment-a",
        node_id="shared",
        session_id="session-enrolled-a",
    )
    log_contribution(
        "shared",
        "compute",
        credits=3,
        enrollment_id="enrollment-b",
        node_id="shared",
        session_id="session-enrolled-b",
    )
    log_contribution(
        "shared",
        "compute",
        credits=4,
        node_id="shared",
        session_id="legacy-session-a",
    )
    log_contribution(
        "shared",
        "compute",
        credits=5,
        node_id="shared",
        session_id="legacy-session-b",
    )

    standings = get_standings()
    assert len(standings) == 5
    by_key = {
        (item["enrollment_id"], item["session_id"], item["attribution"]): item
        for item in standings
    }
    assert by_key[("enrollment-a", None, "enrollment")]["total_points"] == 2
    assert by_key[("enrollment-b", None, "enrollment")]["total_points"] == 3
    assert by_key[(None, "legacy-session-a", "legacy_session")][
        "total_points"
    ] == 4
    assert by_key[(None, "legacy-session-b", "legacy_session")][
        "total_points"
    ] == 5
    assert by_key[(None, None, "historical_node")]["total_points"] == 1


def test_contribution_schema_migration_is_additive_and_does_not_infer_identity():
    with sqlite3.connect("events.db") as con:
        con.execute(
            """
            CREATE TABLE contributions (
                contribution_id TEXT PRIMARY KEY,
                contributor TEXT NOT NULL,
                contribution_type TEXT NOT NULL,
                points REAL NOT NULL,
                task TEXT NOT NULL,
                details TEXT NOT NULL,
                basis TEXT NOT NULL,
                points_are_monetary INTEGER NOT NULL DEFAULT 0,
                attempt_id TEXT UNIQUE,
                created_at REAL NOT NULL
            )
            """
        )
        con.execute(
            """
            INSERT INTO contributions VALUES (
                'historical', 'old-label', 'compute', 5,
                'compute_contribution', '', 'compute_contribution', 0,
                'old-attempt', 1
            )
            """
        )
        ensure_contribution_schema(con)
        ensure_contribution_schema(con)
        row = con.execute(
            "SELECT enrollment_id, node_id, session_id FROM contributions"
        ).fetchone()
    assert row == (None, None, None)

    entry = get_history("old-label")[0]
    assert entry["enrollment_id"] is None
    assert entry["node_id"] is None
    assert entry["session_id"] is None


def test_corrupt_ledger_treated_as_empty():
    Path("ledger.json").write_text("{not valid json")
    assert get_standings() == []
    # And logging still works afterwards
    log_contribution("node-a", "compute", credits=5)
    assert len(get_standings()) == 1


@pytest.mark.asyncio
async def test_pipeline_contributions_never_persist_prompt_or_model_text(
    tmp_path,
    monkeypatch,
    caplog,
):
    prompt_sentinel = "PRIVATE_REQUEST_SENTINEL_8f61"
    output_sentinel = "PRIVATE_MODEL_OUTPUT_SENTINEL_4ad2"
    plan = json.dumps(
        [
            {
                "id": 1,
                "title": f"{output_sentinel} generated title",
                "prompt": f"{output_sentinel} generated subtask",
                "depends_on": [],
            }
        ]
    )
    builder_output = (f"{output_sentinel} generated builder output. " * 5).strip()
    review_output = (
        "## Quality Rating\nPASS\n\n"
        "## Issues Found\nNone\n\n"
        f"## Final Assembled Output\n{output_sentinel} generated review output."
    )

    async def fake_generate(_prompt, system="", **_kwargs):
        if system == orchestrator.PLANNER_SYSTEM:
            return plan
        if system == orchestrator.REVIEWER_SYSTEM:
            return review_output
        return builder_output

    async def fake_stream(*_args, **_kwargs):
        yield builder_output

    monkeypatch.setattr(orchestrator, "generate", fake_generate)
    monkeypatch.setattr(orchestrator, "generate_stream", fake_stream)
    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(server_state, "pipeline_events", [])
    server_state._init_db()
    callbacks = routes_pitch._callbacks(prompt_sentinel, "privacy-regression")

    await orchestrator.run_pipeline(
        f"Build {prompt_sentinel}",
        on_plan=callbacks["on_plan"],
        on_build=callbacks["on_build"],
        on_review_start=callbacks["on_review_start"],
        on_token=callbacks["on_token"],
        revision_enabled=False,
    )

    with sqlite3.connect("events.db") as con:
        contributions = con.execute(
            "SELECT contribution_type, points, task, details FROM contributions "
            "ORDER BY created_at, contribution_id"
        ).fetchall()
        persisted_events = con.execute(
            "SELECT type, data FROM events ORDER BY id"
        ).fetchall()

    assert [(row[0], row[1]) for row in contributions] == [
        ("pitch", 1.0),
        ("compute", 5.0),
        ("compute", 3.0),
    ]
    assert [row[2] for row in contributions] == [
        "pipeline_submission",
        "pipeline_subtask",
        "pipeline_review",
    ]
    assert all(row[3] == "" for row in contributions)

    route_payload = await routes_events.ledger(limit=50)
    persisted_surfaces = [
        json.dumps(contributions),
        Path("ledger.json").read_text(encoding="utf-8"),
        json.dumps(persisted_events),
        json.dumps(server_state.pipeline_events),
        json.dumps(route_payload),
        caplog.text,
    ]
    for sentinel in (prompt_sentinel, output_sentinel):
        assert all(sentinel not in surface for surface in persisted_surfaces)


@pytest.mark.asyncio
async def test_startup_redacts_sqlite_and_imported_legacy_contribution_text():
    sqlite_prompt = "HISTORICAL_SQLITE_PROMPT_18d5"
    json_prompt = "HISTORICAL_JSON_PROMPT_c703"
    with sqlite3.connect("events.db") as con:
        ensure_contribution_schema(con)
        con.execute(
            """
            INSERT INTO contributions (
                contribution_id, contributor, contribution_type, points,
                task, details, basis, points_are_monetary, attempt_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                "sqlite-contribution",
                "node-sqlite",
                "compute",
                7.0,
                sqlite_prompt,
                f"details {sqlite_prompt}",
                "compute_contribution",
                "attempt-sqlite",
                1.0,
            ),
        )
        con.commit()
    Path("ledger.json").write_text(
        json.dumps(
            [
                {
                    "contribution_id": "json-contribution",
                    "contributor": "node-json",
                    "type": "pitch",
                    "credits": 1,
                    "task": json_prompt,
                    "details": f"details {json_prompt}",
                    "contribution_basis": "pitch",
                    "timestamp": 2.0,
                }
            ]
        ),
        encoding="utf-8",
    )

    server_state._init_db()
    server_state._init_db()

    with sqlite3.connect("events.db") as con:
        rows = con.execute(
            """
            SELECT contribution_id, points, task, details, basis, attempt_id
            FROM contributions ORDER BY contribution_id
            """
        ).fetchall()
    assert rows == [
        (
            "json-contribution",
            1.0,
            "pipeline_submission",
            "",
            "pitch",
            None,
        ),
        (
            "sqlite-contribution",
            7.0,
            "compute_contribution",
            "",
            "compute_contribution",
            "attempt-sqlite",
        ),
    ]

    projection = json.loads(Path("ledger.json").read_text(encoding="utf-8"))
    route_payload = await routes_events.ledger(limit=50)
    assert {entry["contribution_id"] for entry in projection} == {
        "json-contribution",
        "sqlite-contribution",
    }
    for serialized in (json.dumps(projection), json.dumps(route_payload)):
        assert sqlite_prompt not in serialized
        assert json_prompt not in serialized
