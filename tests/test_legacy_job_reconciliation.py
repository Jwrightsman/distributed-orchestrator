"""Legacy async jobs cannot remain queued/running across coordinator restart."""

import sqlite3

import server_state as state


def test_jobs_migration_is_additive_and_idempotent():
    with sqlite3.connect("events.db") as con:
        con.execute(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY, task TEXT, project_id TEXT, status TEXT,
                submitted_at TEXT, finished_at TEXT, error TEXT,
                project_dir TEXT, rating TEXT, trace_id TEXT
            )
            """
        )
        con.execute(
            "INSERT INTO jobs(job_id, task, status, submitted_at) "
            "VALUES ('old-job', 'task', 'complete', '2026-01-01T00:00:00+00:00')"
        )

    state._init_db()
    state._init_db()

    with sqlite3.connect("events.db") as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
        preserved = con.execute(
            "SELECT task, status FROM jobs WHERE job_id = 'old-job'"
        ).fetchone()
    assert {
        "started_at",
        "interrupted_at",
        "interruption_reason",
        "coordinator_restart_marker",
        "retryable",
    }.issubset(columns)
    assert preserved == ("task", "complete")


def test_startup_reconciles_queued_and_running_jobs_once():
    state.jobs.clear()
    state._init_db()
    for job_id, status in (("queued-job", "queued"), ("running-job", "running")):
        state._db_write_job({
            "job_id": job_id,
            "task": "task",
            "status": status,
            "submitted_at": "2026-01-01T00:00:00+00:00",
            "started_at": "2026-01-01T00:00:01+00:00" if status == "running" else None,
        })
    state._db_write_job({
        "job_id": "complete-job",
        "task": "task",
        "status": "complete",
        "submitted_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:02+00:00",
    })

    state._db_load_jobs()

    for job_id in ("queued-job", "running-job"):
        job = state.jobs[job_id]
        assert job["status"] == "interrupted"
        assert job["interrupted_at"]
        assert job["interruption_reason"]
        assert job["coordinator_restart_marker"]
        assert job["retryable"] is True
    assert state.jobs["complete-job"]["status"] == "complete"

    first = {
        job_id: (
            state.jobs[job_id]["interrupted_at"],
            state.jobs[job_id]["coordinator_restart_marker"],
        )
        for job_id in ("queued-job", "running-job")
    }
    state.jobs.clear()
    state._db_load_jobs()
    second = {
        job_id: (
            state.jobs[job_id]["interrupted_at"],
            state.jobs[job_id]["coordinator_restart_marker"],
        )
        for job_id in ("queued-job", "running-job")
    }
    assert second == first
