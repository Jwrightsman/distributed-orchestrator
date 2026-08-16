"""The run permalink — /run/{id}.

This is the page meant to be pasted into a post, so the things that must not
break are: it renders from disk without JavaScript, its OpenGraph tags carry
the real numbers, it says "not recorded" instead of inventing a figure for a
run that predates a field, and it cannot be talked into reading a file
outside output/.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app


def _write_run(name: str, **overrides) -> Path:
    """Create a run directory on disk the way the pipeline would."""
    run_dir = Path("output") / name
    run_dir.mkdir(parents=True, exist_ok=True)
    log = {
        "task": "Build a CSV deduplication script",
        "timestamp": name,
        "plan": [
            {"id": 1, "title": "Read the CSV", "description": "Parse rows", "depends_on": []},
            {"id": 2, "title": "Drop duplicates", "description": "Hash rows", "depends_on": [1]},
        ],
        "results": {"1": "out one", "2": "out two"},
        "review": "## Quality Rating\nPASS\n\n## Final Output\n```python\nprint('hi')\n```",
        "rating": "PASS",
        "code_files": ["output/x/code/dedupe.py"],
        "code_problems": [],
        "mode": "local",
        "project_id": "",
    }
    log.update(overrides)
    (run_dir / "full_log.json").write_text(json.dumps(log), encoding="utf-8")
    (run_dir / "review.md").write_text(log["review"], encoding="utf-8")
    (run_dir / "output.md").write_text("Here is the script:\n\n```python\nprint('hi')\n```",
                                       encoding="utf-8")
    return run_dir


RECORDED = {
    "duration_seconds": 305.4,
    "model": "qwen3.5:4b",
    "review_seconds": 61.0,
    "subtask_stats": {
        "1": {"seconds": 88.2, "executor": "jetts-laptop", "chars": 1400, "credits": 5},
        "2": {"seconds": 141.0, "executor": "spare-thinkpad", "chars": 2100, "credits": 5},
    },
    "credits": [
        {"contributor": "orchestrator", "type": "pitch", "credits": 1, "for": "pitching the task"},
        {"contributor": "jetts-laptop", "type": "compute", "credits": 5, "for": "building Read the CSV"},
        {"contributor": "spare-thinkpad", "type": "compute", "credits": 5, "for": "building Drop duplicates"},
        {"contributor": "orchestrator", "type": "review", "credits": 3, "for": "reviewing and assembling the result"},
    ],
    "mode": "distributed",
    "nodes_used": ["jetts-laptop", "spare-thinkpad"],
}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_a_run_has_its_own_page(client):
    _write_run("20260815_120000")
    r = client.get("/run/20260815_120000")
    assert r.status_code == 200
    assert "Build a CSV deduplication script" in r.text


def test_the_page_needs_no_javascript(client):
    """A crawler building a link preview does not run scripts, and a page
    linked from a launch post should not go blank when a fetch fails."""
    _write_run("20260815_120001")
    body = client.get("/run/20260815_120001").text
    main = body[body.index("<main"):body.index("</main>")]
    assert "<script" not in main, "the run page renders its content with JavaScript"
    # The content is really there, not a placeholder waiting to be filled.
    assert "Read the CSV" in main and "Drop duplicates" in main


def test_open_graph_tags_describe_the_actual_run(client):
    _write_run("20260815_120002", **RECORDED)
    body = client.get("/run/20260815_120002").text
    for tag in ('property="og:title"', 'property="og:description"',
                'property="og:url"', 'name="twitter:card"'):
        assert tag in body, f"missing {tag} — the link preview will be blank"
    desc = body.split('name="description" content="')[1].split('"')[0]
    assert "2 subtasks" in desc
    assert "volunteer machines" in desc, "a distributed run should say so in its preview"
    assert "5m 05s" in desc, "the preview does not carry the real duration"


def test_a_long_pitch_does_not_blow_out_the_title(client):
    _write_run("20260815_120003", task="Build " + "a very long pitch " * 20)
    body = client.get("/run/20260815_120003").text
    title = body.split("<title>")[1].split("</title>")[0]
    assert len(title) < 90, f"title is {len(title)} chars — link previews truncate it anyway"
    assert "…" in title


def test_execution_names_the_machine_and_the_time(client):
    """The point of the page: which machine built which piece."""
    _write_run("20260815_120004", **RECORDED)
    body = client.get("/run/20260815_120004").text
    assert "jetts-laptop" in body and "spare-thinkpad" in body
    assert "1m 28s" in body, "88.2s should render as 1m 28s"
    assert "2m 21s" in body


def test_credits_are_itemised_and_totalled(client):
    _write_run("20260815_120005", **RECORDED)
    body = client.get("/run/20260815_120005").text
    assert "+5" in body and "+1" in body and "+3" in body
    assert ">14<" in body, "the settlement total is missing"


def test_a_run_from_before_these_fields_says_so(client):
    """62 runs on disk predate per-subtask timing. Deriving a plausible number
    is how this project once published a figure that had quietly stopped
    being reproducible."""
    _write_run("20260815_120006")     # no subtask_stats, no credits, no revision
    body = client.get("/run/20260815_120006").text
    assert "not recorded" in body.lower()
    assert "not itemised" in body.lower()
    # And it must not fabricate one.
    assert "0m 00s" not in body and "0s</td>" not in body


def test_the_reviser_reports_all_three_outcomes(client):
    never = {"fired": False, "passes": 0, "rating_before": "PASS", "rating_after": "PASS",
             "issues_raised": "", "chars_before": 10, "chars_after": 10,
             "cleared_the_rating": False, "stopped_because": "the reviewer raised no issues"}
    _write_run("20260815_120007", revision=never)
    assert "DID NOT FIRE" in client.get("/run/20260815_120007").text

    fixed = dict(never, fired=True, passes=1, rating_before="NEEDS_WORK", rating_after="PASS",
                 chars_before=100, chars_after=180, cleared_the_rating=True,
                 stopped_because="the reviewer's issues were gone")
    _write_run("20260815_120008", revision=fixed)
    body = client.get("/run/20260815_120008").text
    assert "FIRED" in body and "1 pass" in body and "grew by 80 characters" in body

    gave_up = dict(never, fired=True, passes=2, rating_before="FAIL", rating_after="FAIL",
                   chars_before=100, chars_after=90, cleared_the_rating=False,
                   stopped_because="it hit the 2-pass limit")
    _write_run("20260815_120009", revision=gave_up)
    body = client.get("/run/20260815_120009").text
    assert "2 passes" in body and "2-pass limit" in body, "a reviser that gave up must say so"


def test_a_cleared_rating_is_not_reported_as_the_reviewer_s_verdict(client):
    """review.md holds what the reviewer said *before* any revision pass. When
    the reviser clears the issues the run passes, but review.md still reads
    NEEDS_WORK — reading the rating off that file alone reports a successful
    run as a failed one."""
    _write_run(
        "20260815_120013",
        rating="PASS",
        review="## Quality Rating\nNEEDS_WORK\n\n## Issues Found\n1. Imports pandas.\n",
        revision={"fired": True, "passes": 1, "rating_before": "NEEDS_WORK",
                  "rating_after": "PASS", "issues_raised": "1. Imports pandas.",
                  "chars_before": 100, "chars_after": 150, "cleared_the_rating": True,
                  "stopped_because": "the reviewer's issues were gone"},
    )
    body = client.get("/run/20260815_120013").text
    head = body[body.index('class="facts"'):body.index("</header>")]
    assert "PASS" in head and "NEEDS_WORK" not in head, "the headline shows a stale rating"
    review = body[body.index('id="h-review"'):body.index('id="h-reviser"')]
    assert "NEEDS_WORK" in review, "the reviewer's own verdict has been overwritten"


def test_every_surface_reports_the_same_rating(client):
    """The list said PASS and the detail modal said FAIL, for the same run, on
    the same page — /history and /gallery read the log while /history/{id}
    read review.md. One rule now, in orchestrator.ratings_for."""
    _write_run(
        "20260815_120014",
        rating="PASS",
        review="## Quality Rating\nFAIL\n\n## Issues Found\n1. Broken.\n",
    )
    listed = next(r for r in client.get("/history").json()["runs"]
                  if r["timestamp"] == "20260815_120014")
    detail = client.get("/history/20260815_120014").json()
    card = next(c for c in client.get("/gallery").json()["cards"]
                if c["timestamp"] == "20260815_120014")
    assert listed["rating"] == detail["rating"] == card["rating"] == "PASS"
    assert detail["reviewer_rating"] == "FAIL", "the reviewer's own verdict is lost"


def test_the_cli_agrees_with_the_web(client):
    """`py cli.py --history` used to substring-match the whole review file, so
    a review whose prose contained "PASS" reported a PASS and a run the
    reviser rescued still reported the reviewer's original complaint. Four
    surfaces, four answers."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "cli.py").read_text(encoding="utf-8")
    history = src[src.index("def show_history"):]
    history = history[:history.index("\ndef ")]
    assert "ratings_for" in history, "the CLI derives its own rating"
    assert '"PASS" in review' not in history, "the CLI still substring-matches the review"


def test_output_code_fences_become_code_blocks(client):
    _write_run("20260815_120010")
    body = client.get("/run/20260815_120010").text
    assert "<pre>" in body and "print(&#x27;hi&#x27;)" in body
    assert "```" not in body, "raw fences leaked into the rendered output"


def test_task_text_cannot_inject_markup(client):
    _write_run("20260815_120011", task="<img src=x onerror=alert(1)>")
    body = client.get("/run/20260815_120011").text
    assert "<img src=x" not in body
    assert "&lt;img src=x" in body


@pytest.mark.parametrize("bad", ["..", "../../etc", "..%2f..%2fetc", "%2e%2e",
                                 "nope", "a/b", "....//etc"])
def test_a_path_cannot_escape_the_output_directory(client, bad):
    """Some of these never reach the route — the client normalises `/run/..`
    to `/` — so assert on what matters: no run is served for any of them."""
    r = client.get(f"/run/{bad}")
    assert r.status_code in (200, 404)
    assert 'class="run-head"' not in r.text, f"/run/{bad} rendered a run"


def test_an_unknown_job_id_is_a_404_not_a_500(client):
    assert client.get("/run/job_deadbeef").status_code == 404


def test_a_job_id_resolves_to_its_run(client):
    """An async pitch hands back a job id, so that has to be a working link."""
    import server_state
    _write_run("20260815_120012")
    server_state.jobs["job_abc123"] = {"job_id": "job_abc123", "status": "complete",
                                       "project_dir": "output/20260815_120012"}
    try:
        r = client.get("/run/job_abc123")
        assert r.status_code == 200
        assert "Build a CSV deduplication script" in r.text
    finally:
        server_state.jobs.pop("job_abc123", None)


def test_the_gallery_and_history_link_to_the_run_page(client):
    """A card with no URL of its own was the whole problem."""
    js = (Path(__file__).resolve().parent.parent / "templates" / "_dashboard.js").read_text(encoding="utf-8")
    assert "/run/${ts}" in js, "gallery cards do not link to the run page"
    assert 'href="/run/${encodeURIComponent(r.timestamp)}"' in js, "history rows do not link to the run page"
