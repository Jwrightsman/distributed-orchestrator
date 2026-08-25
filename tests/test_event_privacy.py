"""Persisted lifecycle events retain structure, never prompts or outputs."""

import json
import sqlite3

import pytest
from starlette.websockets import WebSocketDisconnect

import routes_events
import server_state as state


@pytest.fixture(autouse=True)
def clean_event_state():
    state.pipeline_events.clear()
    yield
    state.pipeline_events.clear()


@pytest.mark.asyncio
async def test_future_event_writes_cache_and_broadcast_are_sanitized(monkeypatch):
    prompt_sentinel = "FUTURE_PRIVATE_PROMPT_97c1"
    output_sentinel = "FUTURE_PRIVATE_OUTPUT_48ae"
    broadcasts = []
    monkeypatch.setattr(state.ws_manager, "publish", broadcasts.append)
    state._init_db()

    state._emit(
        "pitch",
        {
            "task": prompt_sentinel,
            "trace_id": "trace-safe",
            "job_id": "job-safe",
        },
    )
    state._emit(
        "winner_selected",
        {
            "execution_id": "execution-safe",
            "candidate_id": "candidate-1",
            "verified": True,
            "reason": output_sentinel,
        },
    )

    with sqlite3.connect("events.db") as con:
        persisted = con.execute(
            "SELECT id, type, time, data FROM events ORDER BY id"
        ).fetchall()

    assert json.loads(persisted[0][3]) == {
        "job_id": "job-safe",
        "trace_id": "trace-safe",
    }
    assert json.loads(persisted[1][3]) == {
        "candidate_id": "candidate-1",
        "execution_id": "execution-safe",
        "verified": True,
    }
    assert state.pipeline_events == broadcasts
    serialized_surfaces = (
        json.dumps(persisted),
        json.dumps(state.pipeline_events),
        json.dumps(broadcasts),
    )
    for sentinel in (prompt_sentinel, output_sentinel):
        assert all(sentinel not in surface for surface in serialized_surfaces)


def test_operational_events_preserve_nonsecret_enrollment_attribution():
    enrollment_a = "11111111111141118111111111111111"
    enrollment_b = "22222222222242228222222222222222"

    assert state._sanitize_event_payload(
        "node_blacklisted",
        {
            "node_id": "worker-a",
            "enrollment_id": enrollment_a,
            "failure_count": 3,
            "blacklist_seconds": 300,
            "credential": "must-not-survive",
        },
    ) == {
        "node_id": "worker-a",
        "enrollment_id": enrollment_a,
        "failure_count": 3,
        "blacklist_seconds": 300,
    }
    assert state._sanitize_event_payload(
        "verification",
        {
            "job_id": "job-1",
            "agreed": True,
            "nodes": ["worker-a", "worker-b"],
            "enrollment_id_a": enrollment_a,
            "enrollment_id_b": enrollment_b,
            "reason": "free-form comparison detail",
        },
    ) == {
        "job_id": "job-1",
        "agreed": True,
        "nodes": ["worker-a", "worker-b"],
        "enrollment_id_a": enrollment_a,
        "enrollment_id_b": enrollment_b,
    }


@pytest.mark.asyncio
async def test_generated_tokens_remain_live_only_and_are_never_replayed(monkeypatch):
    prompt_sentinel = "TOKEN_EVENT_PRIVATE_PROMPT_d614"
    output_sentinel = "TOKEN_EVENT_PRIVATE_OUTPUT_a7c3"
    broadcasts = []
    monkeypatch.setattr(state.ws_manager, "publish", broadcasts.append)
    state._init_db()

    state._emit(
        "token",
        {
            "task": prompt_sentinel,
            "token": output_sentinel,
            "job_id": "job-safe",
            "trace_id": "trace-safe",
            "subtask_id": 1,
        },
    )

    assert broadcasts == [
        {
            "type": "token",
            "time": broadcasts[0]["time"],
            "job_id": "job-safe",
            "trace_id": "trace-safe",
            "subtask_id": 1,
            "token": output_sentinel,
        }
    ]
    assert prompt_sentinel not in json.dumps(broadcasts)
    assert state.pipeline_events == []
    with sqlite3.connect("events.db") as con:
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert await routes_events.get_events() == {"events": []}


@pytest.mark.asyncio
async def test_startup_redacts_historical_events_before_http_and_ws_replay(
    monkeypatch,
):
    prompt_sentinel = "HISTORICAL_PRIVATE_PROMPT_6e93"
    output_sentinel = "HISTORICAL_PRIVATE_OUTPUT_b516"
    reason_sentinel = "HISTORICAL_FREE_FORM_REASON_3fa8"
    historical = [
        (
            41,
            "pitch",
            "2026-01-01T00:00:01+00:00",
            {
                "task": prompt_sentinel,
                "trace_id": "trace-safe",
                "job_id": "job-safe",
            },
        ),
        (
            42,
            "plan",
            "2026-01-01T00:00:02+00:00",
            {
                "plan": prompt_sentinel,
                "subtasks": [output_sentinel],
                "trace_id": "trace-safe",
            },
        ),
        (
            43,
            "build",
            "2026-01-01T00:00:03+00:00",
            {
                "subtask": prompt_sentinel,
                "output": output_sentinel,
                "subtask_id": 1,
                "trace_id": "trace-safe",
            },
        ),
        (
            44,
            "review_start",
            "2026-01-01T00:00:04+00:00",
            {
                "review": output_sentinel,
                "message": prompt_sentinel,
                "trace_id": "trace-safe",
            },
        ),
        (
            45,
            "result_rejected",
            "2026-01-01T00:00:05+00:00",
            {
                "task_id": "task-safe",
                "claimed_by": "node-a",
                "assigned_to": "node-b",
                "reason": reason_sentinel,
                "error_code": "binding_mismatch",
            },
        ),
    ]
    with sqlite3.connect("events.db") as con:
        con.execute(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                time TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        con.executemany(
            "INSERT INTO events (id, type, time, data) VALUES (?, ?, ?, ?)",
            [
                (event_id, event_type, event_time, json.dumps(data))
                for event_id, event_type, event_time, data in historical
            ],
        )
        con.commit()

    state.pipeline_events.append(
        {
            "id": 99,
            "type": "plan",
            "time": "2026-01-01T00:00:06+00:00",
            "task": prompt_sentinel,
            "subtasks": [output_sentinel],
            "subtask_count": 3,
            "trace_id": "trace-memory",
        }
    )

    state._init_db()
    state._init_db()

    with sqlite3.connect("events.db") as con:
        rows = con.execute(
            "SELECT id, type, time, data FROM events ORDER BY id"
        ).fetchall()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (event_id, event_type, event_time)
        for event_id, event_type, event_time, _data in historical
    ]
    assert json.loads(rows[0][3]) == {
        "job_id": "job-safe",
        "trace_id": "trace-safe",
    }
    assert json.loads(rows[1][3]) == {
        "subtask_count": 1,
        "trace_id": "trace-safe",
    }
    assert json.loads(rows[2][3]) == {
        "subtask_id": 1,
        "trace_id": "trace-safe",
    }
    assert json.loads(rows[3][3]) == {"trace_id": "trace-safe"}
    assert json.loads(rows[4][3]) == {
        "assigned_to": "node-b",
        "claimed_by": "node-a",
        "error_code": "binding_mismatch",
        "task_id": "task-safe",
    }
    assert state.pipeline_events == [
        {
            "id": 99,
            "type": "plan",
            "time": "2026-01-01T00:00:06+00:00",
            "subtask_count": 3,
            "trace_id": "trace-memory",
        }
    ]

    http_payload = await routes_events.get_events()

    class ReplayWebSocket:
        def __init__(self):
            self.replayed = []

        async def accept(self):
            return None

        async def send_json(self, data):
            self.replayed.append(data)

        async def receive_text(self):
            raise WebSocketDisconnect()

    async def authorize(_websocket):
        return True

    monkeypatch.setattr(routes_events, "authorize_viewer_websocket", authorize)
    websocket = ReplayWebSocket()
    await routes_events.ws_events(websocket)

    assert [event["id"] for event in http_payload["events"]] == [41, 42, 43, 44, 45]
    assert websocket.replayed == http_payload["events"]
    serialized_surfaces = (
        json.dumps(rows),
        json.dumps(state.pipeline_events),
        json.dumps(http_payload),
        json.dumps(websocket.replayed),
    )
    for sentinel in (prompt_sentinel, output_sentinel, reason_sentinel):
        assert all(sentinel not in surface for surface in serialized_surfaces)
