"""Run detail, and the two records the design keeps apart.

Ported from `docs/design/run-detail-2026-09-07`. The design's own reasoning is
in that directory; what is asserted here is only the part a future edit could
break silently.

The single most important rule in the pass has its own section below: the
sealed manifest and the provenance envelope make different claims and must
never read as one. Two chips side by side is a row of ticks, and ticks get
counted as one stronger claim — so the manifest gets a chip, because its state
genuinely varies, and the envelope gets a sentence. No heading may span both.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard
import run_detail
from datetime import datetime, timezone

from execution.contracts import ExecutionRequestV1, ExecutionResultV1
from server import app

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

CSS = (TEMPLATES / "_dashboard.css").read_text(encoding="utf-8")
RUN_DETAIL_CSS = (TEMPLATES / "_run_detail.css").read_text(encoding="utf-8")
JS = (TEMPLATES / "_dashboard.js").read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    """The declaration block for one selector, or "" if it has none."""
    match = re.search(
        r"(?:^|[\n;}])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}",
        css,
        re.M,
    )
    return match.group(1) if match else ""


# ── The modal header at a narrow width ───────────────────────────────
# `.modal-head` was a non-wrapping flex row with `space-between`, and
# `.modal-title` had `min-width: 0` and no wrapping rule. A long unbreakable
# execution ID overflowed its squeezed box while `.modal-actions`
# (`flex-shrink: 0`) held its width, so the title ran under the buttons.
# Both modals share the rule, so the node modal is fixed by the same change.


def test_the_modal_header_may_wrap():
    assert "flex-wrap: wrap" in _rule(CSS, ".modal-head"), (
        "a non-wrapping header cannot put the title on its own line, which is "
        "the whole of the narrow-width fix"
    )


def test_the_modal_title_may_claim_a_line_and_break_a_long_id():
    rule = _rule(CSS, ".modal-title")
    assert "flex: 1 1 200px" in rule, "the title cannot claim a line of its own"
    assert "overflow-wrap: anywhere" in rule, (
        "an execution id has no break opportunity, so without this it overflows "
        "its box however much the box wraps"
    )


def test_the_modal_header_does_not_use_space_between():
    """`margin-left: auto` replaces it.

    They are equivalent while there is room — which is why this fix changes no
    geometry above the breakpoint — but `space-between` on a wrapped row pushes
    the actions to the far edge of their own line instead of following the
    title.
    """
    assert "margin-left: auto" in _rule(CSS, ".modal-actions")
    assert "justify-content: space-between" not in _rule(CSS, ".modal-head")


def test_the_action_row_shrinks_so_it_can_wrap():
    """The fifth declaration, which opening it at 330px found.

    `flex-shrink: 0` on the row held it at its max-content width inside a
    narrower panel, so it ran off the edge with the last control unreachable —
    its own `flex-wrap` could never engage, because a row that cannot be
    squeezed never needs a second line. The rule belongs on the children.
    """
    assert "flex-shrink: 0" not in _rule(CSS, ".modal-actions")
    assert "flex-shrink: 0" in _rule(CSS, ".modal-actions > *"), (
        "without this the buttons squeeze and clip their labels instead of wrapping"
    )


def test_both_modals_share_the_header_rule():
    """The node modal is fixed by the same four declarations, not a copy."""
    page = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    heads = page.count('class="modal-head"')
    assert heads == 2, f"expected the run and node modals to share the class, found {heads}"


# ── A run with a canonical record behind it ──────────────────────────
# The interesting states only exist when there is one: the three axes, the
# sealed manifest, the per-unit machine, the envelope. These helpers build a
# run the way the pipeline does — run directory, durable execution record,
# registered artifact root, sealed manifest, envelope — so the surface is
# exercised against real records rather than a mock of them.

RUN = "20260908_120000"
EXECUTION = "exec_" + "9f4c21e8" * 3


def _write_log(name: str = RUN, **overrides) -> Path:
    run_dir = Path("output") / name
    (run_dir / "code").mkdir(parents=True, exist_ok=True)
    review = "## Quality Rating\nPASS\n\n## Final Output\nA --since argument.\n"
    log = {
        "task": "Add a --since flag so the summary can cover a date range",
        "timestamp": name,
        "plan": [
            {"id": 1, "title": "Parse --since", "description": "Take an ISO date.",
             "depends_on": []},
            {"id": 2, "title": "Filter rows", "description": "Filter by date.",
             "depends_on": [1]},
            {"id": 3, "title": "Update summary", "description": "State the range.",
             "depends_on": [1]},
            {"id": 4, "title": "Cover the parser", "description": "Add tests.",
             "depends_on": [1]},
        ],
        "results": {"1": "a", "2": "b", "3": "c", "4": "d"},
        "review": review,
        "rating": "PASS",
        "code_files": [f"output/{name}/code/summary.py"],
        "code_problems": [],
        "mode": "distributed",
        "nodes_used": 3,
        "project_id": "proj_1c04de",
    }
    log.update(overrides)
    (run_dir / "full_log.json").write_text(json.dumps(log), encoding="utf-8")
    (run_dir / "review.md").write_text(review, encoding="utf-8")
    (run_dir / "output.md").write_text(
        "A --since argument on the summary command, validated as an ISO date "
        "before anything reads the file.\n\n"
        "```python\ndef parse_since(raw):\n    return raw\n```",
        encoding="utf-8",
    )
    (run_dir / "code" / "summary.py").write_text(
        "def parse_since(raw):\n    return date.fromisoformat(raw)\n", encoding="utf-8"
    )
    return run_dir


def _result(**overrides) -> ExecutionResultV1:
    base = dict(
        execution_id=EXECUTION,
        status="completed",
        lifecycle_status="completed",
        validation_outcome="passed",
        assurance_level="unverified",
        task="Add a --since flag so the summary can cover a date range",
        project_id="proj_1c04de",
        strategy_requested="dag",
        strategy_selected="dag",
        strategy_version="1",
        selector_reason="explicit request",
        selector_version="1",
        placement_requested="auto",
        placement_planned="distributed",
        placement_observed="distributed",
        remote_dispatch_consent=True,
        created_at="2026-09-08T12:00:00Z",
        started_at="2026-09-08T12:00:01Z",
        completed_at="2026-09-08T12:05:05Z",
        duration_ms=304000,
        participating_nodes=["node-7c22", "node-a1f3", "node-legacy-1"],
        execution_units=[
            {"unit_id": "dag-1", "kind": "dag_subtask", "title": "Parse --since",
             "depends_on": [], "status": "completed", "placement": "distributed",
             "node_id": "node-7c22", "duration_ms": 88200},
            {"unit_id": "dag-2", "kind": "dag_subtask", "title": "Filter rows",
             "depends_on": ["dag-1"], "status": "completed", "placement": "distributed",
             "node_id": "node-a1f3", "duration_ms": 41000},
            {"unit_id": "dag-3", "kind": "dag_subtask", "title": "Update summary",
             "depends_on": ["dag-1"], "status": "completed", "placement": "distributed",
             "node_id": "node-legacy-1", "duration_ms": 39000},
            {"unit_id": "dag-4", "kind": "dag_subtask", "title": "Cover the parser",
             "depends_on": ["dag-1"], "status": "completed", "placement": "local",
             "node_id": None, "duration_ms": 12000},
        ],
        validation_summary={
            "outcome": "passed", "assurance_level": "unverified",
            "checks_run": ["parse_precheck", "import_check"],
            "checks_passed": ["parse_precheck", "import_check"],
            "checks_failed": [], "checks_not_run": [],
            "explanation": "Mechanical checks only.",
        },
        review_metadata={"rating": "PASS"},
    )
    base.update(overrides)
    return ExecutionResultV1(**base)


def _receipts(count: int, *, legacy_last: bool = False) -> None:
    """Seed accepted receipts, which is where the envelope's producers come from."""
    from server_state import _DB_PATH

    rows = []
    for i in range(count):
        enrolled = not (legacy_last and i == count - 1)
        rows.append((
            f"{i:032d}", f"u{i}", EXECUTION, f"dag-{i + 1}", "dag_subtask",
            f"node-{i}", (f"enr_{i:06d}" if enrolled else None), "1", "d" * 64,
            "ollama", "qwen3.5:4b", "sha256:" + "c" * 64,
            "production", "1", "e" * 64, "1", "f" * 64, 1000.0 + i, "out", None,
            1.0, "settled_output",
        ))
    with sqlite3.connect(_DB_PATH) as con:
        con.executemany(
            """
            INSERT INTO accepted_result_receipts (
                attempt_id, task_id, execution_id, execution_unit_id,
                execution_unit_kind, assigned_node_id, assigned_enrollment_id,
                assigned_descriptor_version, assigned_descriptor_hash,
                assigned_model_provider, assigned_model_name, assigned_model_digest,
                evidence_role, requirement_version, requirement_digest,
                contract_version, result_hash, accepted_at, output, error,
                elapsed_seconds, terminal_cause
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        con.commit()


def _published(
    *,
    receipts: int = 1,
    legacy_last: bool = False,
    envelope: bool = True,
    result_overrides: dict | None = None,
    **log_overrides,
) -> str:
    """A run past its publication boundary, with everything behind it."""
    from execution.service import get_execution_service
    from server_state import attempt_store, provenance_envelope_store

    run_dir = _write_log(execution_id=EXECUTION, **log_overrides)
    service = get_execution_service()
    attempt_store.migrate()

    result = _result(**(result_overrides or {}))
    request = ExecutionRequestV1(task=result.task, strategy="dag", placement="auto")
    service.store.create(request, result)

    service.artifacts.register_root(EXECUTION, str(run_dir), strategy="dag", active=True)
    manifest = service.artifacts.seal_manifest(EXECUTION)

    result.sealed_manifest_hash = manifest.manifest_hash
    result.artifact_integrity_mode = manifest.integrity_mode
    result.produced_files = [e.relative_path for e in manifest.entries]
    result.primary_deliverables = [
        e.relative_path for e in manifest.entries if e.role == "deliverable"
    ]
    service.store.save(request, result)

    if envelope:
        _receipts(receipts, legacy_last=legacy_last)
        provenance_envelope_store.record(
            EXECUTION,
            manifest=manifest,
            validators=[
                {"name": "parse_precheck", "version": "3", "outcome": "passed"},
                {"name": "import_check", "version": "1", "outcome": "passed"},
            ],
        )
    return RUN


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _surfaces(client, run: str) -> dict[str, str]:
    """Both renderings of one run: the server page and the console fragment."""
    page = client.get(f"/run/{run}")
    assert page.status_code == 200, page.text[:300]
    detail = client.get(f"/history/{run}")
    assert detail.status_code == 200, detail.text[:300]
    return {"server": page.text, "console": detail.json()["detail_html"]}


def _panel(html: str, marker: str) -> str:
    """One panel's markup, from its label to the start of the next section."""
    start = html.index(marker)
    nxt = html.find("<section", start + len(marker))
    return html[start:nxt if nxt != -1 else len(html)]


def _flat(html: str) -> str:
    """Rendered text with its runs of whitespace collapsed.

    Source line wrapping is not a difference a reader sees, and a test that
    trips on it is testing the indentation.
    """
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


# ── The rule that is easiest to break by accident ────────────────────


def test_the_envelope_panel_carries_no_chip_lamp_or_colour(client):
    """A sentence, never a badge.

    A coloured badge here would sit beside the manifest's SEALED chip and read
    as a second tick in a row of ticks. The two records establish different
    things and vary independently, so they are given different grammatical
    classes rather than a matching pair.
    """
    run = _published()
    for surface, html in _surfaces(client, run).items():
        panel = _panel(html, "PRODUCED BY")
        assert "rd-chip" not in panel, f"{surface}: the envelope panel renders a chip"
        assert "lamp" not in panel, f"{surface}: the envelope panel renders a lamp"
        for tone in ("is-ok", "is-bad", "is-pass", "is-sealed", "is-info"):
            assert tone not in panel, (
                f"{surface}: the envelope panel renders the state class {tone!r}"
            )


def test_the_envelope_panel_uses_no_colour_token_beyond_text_and_border():
    """Checked against the stylesheet, not only the markup.

    A class with no state name could still be given `--accent`. Every rule that
    styles something inside the envelope panel may only reach for ink and
    hairlines.
    """
    allowed = {
        "--text", "--text-dim", "--text-muted", "--text-faint",
        "--border", "--border-subtle", "--border-strong",
        "--surface", "--surface-sunken", "--surface-hover", "--bg",
        # Type, not colour.
        "--mono", "--sans",
    }
    for block in re.findall(r"\.rd-envelope[^{}]*\{([^}]*)\}", RUN_DETAIL_CSS):
        for token in re.findall(r"var\((--[a-z0-9-]+)\)", block):
            assert token in allowed, (
                f"the envelope panel styles with {token}, which is a state colour. "
                "This panel is a sentence: no chip, no lamp, no colour."
            )


def test_no_heading_spans_the_manifest_and_the_envelope(client):
    """There is no INTEGRITY panel and no TRUST section in this design.

    A shared header is precisely what invites a reader to add up what sits
    beneath it, so the two records are siblings with nothing over them.
    """
    run = _published()
    for surface, html in _surfaces(client, run).items():
        # The whole region that contains both records, from the column that
        # holds them to the end of the envelope panel. A heading *above* the
        # pair spans it just as surely as one between them -- more so, which
        # is the case an earlier version of this test missed.
        column = html[html.index('class="rd-side"'):]
        column = column[: column.index("PRODUCED BY")] + column[
            column.index("PRODUCED BY"):
        ].split("</section>")[0]

        assert not re.search(r"<h[1-6][\s>]", column), (
            f"{surface}: a heading sits over the manifest and the envelope. "
            "There is no INTEGRITY panel and no TRUST section in this design: "
            "a shared header is what invites a reader to add up what is under it."
        )
        labels = re.findall(r'class="rd-panel-label">\s*([^<]*?)\s*<', column)
        assert labels == ["MANIFEST", "PRODUCED BY"], (
            f"{surface}: the labels over these two records are {labels}, which "
            "is not one label each"
        )
        # And the manifest closes before the envelope opens, so neither is
        # nested inside the other.
        between = html[html.index("MANIFEST"):html.index("PRODUCED BY")]
        assert between.count("</section>") >= 1, (
            f"{surface}: the manifest panel does not close before the envelope"
        )


def test_the_manifest_keeps_its_chip_because_its_state_varies(client):
    """The asymmetry is the design, so the other half is asserted too."""
    run = _published()
    for surface, html in _surfaces(client, run).items():
        panel = _panel(html, "MANIFEST")
        assert "SEALED" in panel, f"{surface}: the manifest lost its state chip"
        assert "rd-chip is-sealed" in panel, f"{surface}: the chip lost its state class"


# ── The three axes ───────────────────────────────────────────────────


def test_lifecycle_validation_and_assurance_are_read_separately(client):
    """`status` flattens exactly the distinction the strip exists to show.

    It projects *completed + passed* to `completed` and every other completed
    outcome to `unverified`. A surface that reads it cannot tell a run that was
    checked and passed from one that was never checked at all.
    """
    run = _published()
    for surface, html in _surfaces(client, run).items():
        strip = html[html.index("LIFECYCLE"):html.index("PLACEMENT")]
        assert "VALIDATION" in strip and "ASSURANCE" in strip
        assert "completed" in strip, f"{surface}: lifecycle_status is not rendered"
        assert "passed" in strip, f"{surface}: validation_outcome is not rendered"
        assert "unverified" in strip, f"{surface}: assurance_level is not rendered"
        assert "2 checks run" in strip, (
            f"{surface}: the validation note does not come from validation_summary"
        )


def test_the_compatibility_status_field_is_never_read():
    """Asserted against the source, because a future edit would reach for it.

    `status` is the one field that looks like it answers the question and does
    not. The three axes are separate fields on the same record.
    """
    source = (Path(__file__).resolve().parent.parent / "run_detail.py").read_text(
        encoding="utf-8"
    )
    code = "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#")
    )
    assert "durable.status" not in code and '["status"]' not in code, (
        "run_detail.py reads the compatibility status projection"
    )
    for field in ("lifecycle_status", "validation_outcome", "assurance_level"):
        assert field in code, f"run_detail.py does not read {field}"


def test_a_run_with_no_execution_record_says_so_on_all_three_axes(client):
    """Absence is a value, never a blank."""
    _write_log("20260101_000000")
    html = client.get("/run/20260101_000000").text
    strip = html[html.index("LIFECYCLE"):html.index("PLACEMENT")]
    assert strip.count("not recorded") == 3
    assert "rd-marker is-7 is-absent" in strip, (
        "an unrecorded axis needs the hollow marker, so it survives greyscale"
    )


# ── Waves ────────────────────────────────────────────────────────────


def test_waves_come_from_depends_on_grouped_by_depth():
    """The waves are the parallelism claim.

    Three units whose only dependency is unit 01 is the statement that three
    machines could have been offered work at once. A column of dependency IDs
    states the same facts and shows none of it.
    """
    units = [
        {"unit_id": "dag-1", "depends_on": []},
        {"unit_id": "dag-2", "depends_on": ["dag-1"]},
        {"unit_id": "dag-3", "depends_on": ["dag-1"]},
        {"unit_id": "dag-4", "depends_on": ["dag-2", "dag-3"]},
    ]
    waves = run_detail.units_in_waves(units)
    assert [[u["unit_id"] for u in w] for w in waves] == [
        ["dag-1"], ["dag-2", "dag-3"], ["dag-4"]
    ]


def test_a_dependency_cycle_renders_rather_than_raising():
    """This is a rendering path for records that already exist.

    A cycle cannot be produced by the planner, but it must not be the thing
    that makes an existing run unopenable.
    """
    units = [
        {"unit_id": "a", "depends_on": ["b"]},
        {"unit_id": "b", "depends_on": ["a"]},
    ]
    waves = run_detail.units_in_waves(units)
    assert sum(len(w) for w in waves) == 2


def test_the_surface_renders_waves_and_not_a_column_of_ids(client):
    run = _published()
    for surface, html in _surfaces(client, run).items():
        panel = _panel(html, "HOW IT WAS SPLIT")
        assert "WAVE 1" in panel and "WAVE 2" in panel, f"{surface}: no waves"
        assert "from depends_on" in panel
        assert "all 3 depend only on 01" in panel, (
            f"{surface}: the wave note does not name the shared dependency"
        )


# ── Per-unit machine ─────────────────────────────────────────────────


def test_a_units_machine_is_read_off_the_unit_and_never_from_nodes(client):
    """`ExecutionUnitSummaryV1.node_id` is populated and survives the round trip.

    The archived handoff records this as unserved (§8.2) and the design draws
    `machine not recorded` on every card. Source wins: for a distributed unit
    the machine *is* recorded, and printing "not recorded" over it would be the
    false statement — on a surface whose whole discipline is not making them.
    """
    run = _published()
    for surface, html in _surfaces(client, run).items():
        panel = _panel(html, "HOW IT WAS SPLIT")
        for node in ("node-7c22", "node-a1f3", "node-legacy-1"):
            assert node in panel, f"{surface}: unit machine {node} is not rendered"
        assert "this machine" in panel, (
            f"{surface}: a locally executed unit should say so"
        )


def test_a_unit_with_no_machine_recorded_says_so(client):
    run = _published(result_overrides={"execution_units": [
        {"unit_id": "dag-1", "kind": "dag_subtask", "title": "Parse --since",
         "depends_on": [], "status": "completed", "placement": "distributed",
         "node_id": None},
    ]})
    panel = _panel(client.get(f"/run/{run}").text, "HOW IT WAS SPLIT")
    assert "machine not recorded" in panel
    assert "rd-unit-machine is-absent" in panel


def test_the_node_id_a_unit_carries_survives_the_store(client):
    """The claim above rests on this, so it is checked rather than assumed."""
    from execution.service import get_execution_service

    _published()
    stored = get_execution_service().store.get(EXECUTION)
    assert stored is not None
    assert [u.node_id for u in stored.execution_units] == [
        "node-7c22", "node-a1f3", "node-legacy-1", None
    ]


# ── The five chips ───────────────────────────────────────────────────

LADDER = [
    ("FAIL", "boom", "FAIL"),
    ("NEEDS_WORK", "boom", "NEEDS_WORK"),
    ("?", "boom", "NO_VERDICT"),
    (None, None, "NO_VERDICT"),
    ("PASS", "runner timed out", "UNCHECKED"),
    ("PASS", None, "PASS"),
]


@pytest.mark.parametrize("rating,precheck,expected", LADDER)
def test_worst_wins_and_pass_needs_both_halves(rating, precheck, expected):
    assert run_detail.run_verdict(rating, precheck) == expected


@pytest.mark.parametrize("rating,precheck,expected", LADDER)
def test_the_console_ladder_agrees_with_the_server_ladder(rating, precheck, expected):
    """Two implementations, one ladder.

    `_dashboard.js` still renders the chips on the list cards, so the rule
    lives in two languages. They are held to the same table here rather than
    trusted to stay in step.
    """
    body = JS[JS.index("function runVerdict"):JS.index("function verdictChip")]
    order = re.findall(r"return '([A-Z_]+)'", body)
    assert order == ["FAIL", "NEEDS_WORK", "NO_VERDICT", "UNCHECKED", "PASS"], (
        f"the console ladder is {order}, which is not worst-wins"
    )


def test_unchecked_can_never_read_as_pass_on_any_surface(client):
    """A precheck error with an empty problem list is the trap.

    `PASS` means the reviewer passed it *and* the mechanical check found no
    defects. An empty problem list beside a precheck error means "not checked",
    not "checked clean".
    """
    run = _published(
        rating="PASS",
        code_problems=[],
        code_precheck_error="validator runner exceeded its budget",
    )

    listed = next(r for r in client.get("/history").json()["runs"]
                  if r["timestamp"] == run)
    card = next(c for c in client.get("/gallery").json()["cards"]
                if c["timestamp"] == run)
    assert listed["code_precheck_error"], "the list card cannot render UNCHECKED"
    assert card["code_precheck_error"], "the gallery card cannot render UNCHECKED"

    for surface, html in _surfaces(client, run).items():
        head = html[html.index('class="rd-head"'):html.index('class="rd-triad"')]
        assert "UNCHECKED" in head, f"{surface}: detail does not render UNCHECKED"
        assert "rd-verdict is-pass" not in head, f"{surface}: detail renders PASS"
        assert "unchecked rather than known good" in _flat(html), (
            f"{surface}: the reason the files are unchecked is not stated"
        )


def test_all_five_chips_have_a_style_and_unchecked_is_not_a_softer_pass():
    for cls in ("is-pass", "is-needs-work", "is-fail", "is-unchecked", "is-no-verdict"):
        assert f".rd-verdict.{cls}" in RUN_DETAIL_CSS, f"{cls} has no chip style"
    # UNCHECKED and NO VERDICT are deliberately identical, and neither borrows
    # the accent that means PASS.
    unchecked = RUN_DETAIL_CSS[RUN_DETAIL_CSS.index(".rd-verdict.is-unchecked,"):]
    unchecked = unchecked[: unchecked.index("\n\n")]
    assert "--accent" not in unchecked


# ── The envelope's sentence ──────────────────────────────────────────


def test_the_envelope_is_a_sentence_that_states_its_own_limit(client):
    run = _published(receipts=1)
    for surface, html in _surfaces(client, run).items():
        panel = _flat(_panel(html, "PRODUCED BY"))
        assert "Built by 1 enrolled machine on qwen3.5:4b, checked by 2 validators." in panel, (
            f"{surface}: the one line is not the design's sentence"
        )
        assert "It does not establish that the output is correct, useful, or honest." in panel, (
            f"{surface}: the limit does not travel with the claim"
        )
        assert "not recorded" in panel, f"{surface}: the unrecorded count is missing"


def test_plural_producers_is_the_primary_case(client):
    """The ensemble path settles several receipts and elects no winner.

    So `producers` is always a list, one producer is a list of one, and the
    singular fields stay null rather than naming an arbitrary receipt.
    """
    run = _published(receipts=3)
    from server_state import provenance_envelope_store

    record = provenance_envelope_store.get(EXECUTION)
    payload = dict(record.payload)
    assert len(payload["producers"]) == 3
    for singular in ("attempt_id", "receipt_id", "unit_id"):
        assert payload[singular] is None, (
            f"{singular} elected a winner among three accepted receipts"
        )
    assert "single_producer" in payload["unknown_facts"]

    sentence = run_detail.envelope_sentence(payload)
    assert sentence.startswith("Built by 3 enrolled machines")
    for surface, html in _surfaces(client, run).items():
        assert sentence in html, f"{surface}: the plural sentence is not rendered"


def test_a_producer_with_no_enrolment_is_named_rather_than_averaged(client):
    run = _published(receipts=3, legacy_last=True)
    from server_state import provenance_envelope_store

    payload = dict(provenance_envelope_store.get(EXECUTION).payload)
    sentence = run_detail.envelope_sentence(payload)
    assert "1 of the 3 has no enrolment recorded" in sentence
    assert sentence in _surfaces(client, run)["server"]


def test_a_legacy_execution_with_no_envelope_renders_the_panel_absent(client):
    """Absent — not an empty panel, and not an error."""
    run = _published(envelope=False)
    for surface, html in _surfaces(client, run).items():
        assert "PRODUCED BY" not in html, (
            f"{surface}: an empty envelope panel is worse than no panel"
        )
        assert "MANIFEST" in html, f"{surface}: the rest of the surface should still render"


def test_the_run_page_og_description_carries_the_envelope_sentence(client):
    """It has to survive being pasted with no page around it.

    That constraint is what chose a sentence over twenty fields, so this is
    where it is checked.
    """
    run = _published()
    html = client.get(f"/run/{run}").text
    og = re.search(r'<meta property="og:description" content="([^"]*)"', html).group(1)
    assert "Built by 1 enrolled machine" in og
    assert len(og) < 300, "a link preview truncates this anyway"


# ── The timeline, and the audit bundle it does not fetch ─────────────


# A record whose units carry their own moments, which is what every run
# written since `ExecutionUnitSummaryV1` grew them looks like. The fixture's
# default record predates them on purpose, so both states stay exercised.
TIMED_UNITS = [
    {"unit_id": "dag-1", "kind": "dag_subtask", "title": "Parse --since",
     "depends_on": [], "status": "completed", "placement": "distributed",
     "node_id": "node-7c22", "duration_ms": 88200,
     "started_at": "2026-09-08T12:00:02Z", "completed_at": "2026-09-08T12:01:30Z"},
    {"unit_id": "dag-2", "kind": "dag_subtask", "title": "Filter rows",
     "depends_on": ["dag-1"], "status": "completed", "placement": "distributed",
     "node_id": "node-a1f3", "duration_ms": 41000,
     "started_at": "2026-09-08T12:01:31Z", "completed_at": "2026-09-08T12:02:12Z"},
    {"unit_id": "dag-3", "kind": "dag_subtask", "title": "Update summary",
     "depends_on": ["dag-1"], "status": "failed", "placement": "distributed",
     "node_id": "node-legacy-1", "duration_ms": 39000,
     "started_at": "2026-09-08T12:01:31Z", "completed_at": "2026-09-08T12:02:10Z"},
    {"unit_id": "dag-4", "kind": "dag_subtask", "title": "Cover the parser",
     "depends_on": ["dag-1"], "status": "completed", "placement": "local",
     "node_id": None, "duration_ms": 12000,
     "started_at": "2026-09-08T12:02:12Z", "completed_at": "2026-09-08T12:02:24Z"},
]


def test_the_timeline_is_drawn_from_the_execution_record(client):
    run = _published()
    for surface, html in _surfaces(client, run).items():
        panel = _panel(html, "TIMELINE")
        assert "GET /v1/executions/{id}" in panel, f"{surface}: the source is not named"
        assert "+0.0s" in panel and "submission committed to disk" in panel
        assert "terminal state committed" in panel


def test_a_record_without_per_unit_moments_says_so(client):
    """The absence this panel has always named, and still has to.

    A record written before the unit summary carried the two timestamps has no
    per-unit moments. Dropping its units from the timeline would draw a run
    whose units took no time, which is a different statement from one that did
    not write them down.
    """
    run = _published()
    for surface, html in _surfaces(client, run).items():
        panel = _flat(_panel(html, "TIMELINE"))
        assert "+—" in panel, f"{surface}: the absence is dropped rather than named"
        assert "per-unit start and finish times are not recorded" in panel, (
            f"{surface}: the absence does not say which absence it is"
        )
        assert "ran to" not in panel, (
            f"{surface}: a unit row is drawn for a record that timestamps none"
        )


def test_a_unit_that_carries_its_moments_gets_a_row(client):
    run = _published(result_overrides={"execution_units": TIMED_UNITS})
    for surface, html in _surfaces(client, run).items():
        panel = _flat(_panel(html, "TIMELINE"))
        for label in ("unit 01 ran to", "unit 02 ran to", "unit 03 ran to",
                      "unit 04 ran to"):
            assert label in panel, f"{surface}: {label!r} is missing"
        # Both ends, off the record: +2.0s from `created_at` to the first
        # unit's start, and 88 seconds later to its finish -- which crosses a
        # minute, so it reads in the same units the run-level rows do.
        assert "+2.0s unit 01 ran to +1m 30s" in panel, (
            f"{surface}: the row does not carry both ends of the interval"
        )
        # A run that recorded everything draws no absence at all -- not the
        # blanket sentence, and not a count that happens to be zero. Poisoning
        # found that one: `0 of 4 units did not record both ends` reads as a
        # defect, ships silently, and passes a test that only bans the other
        # sentence.
        assert "not recorded" not in panel and "did not record" not in panel, (
            f"{surface}: an absence is claimed over units that recorded both"
        )
        assert "+—" not in panel, (
            f"{surface}: a run with every moment on the record still draws a "
            "gap somewhere"
        )


def test_a_unit_row_says_its_state_rather_than_colouring_it(client):
    """The rule the unit cards already follow.

    A timeline where the only difference between a finished unit and a failed
    one is a grey level is a distinction nobody should have to make, and it
    disappears entirely in greyscale.
    """
    run = _published(result_overrides={"execution_units": TIMED_UNITS})
    for surface, html in _surfaces(client, run).items():
        panel = _panel(html, "TIMELINE")
        assert "and failed" in _flat(panel), (
            f"{surface}: the failed unit does not say so in a word"
        )
        rows = re.findall(r'<span class="rd-tl-what([^"]*)">([^<]*)</span>', panel)
        unit_rows = [(cls, text) for cls, text in rows if "ran to" in text]
        assert len(unit_rows) == 4, f"{surface}: expected four unit rows, got {unit_rows}"
        assert {cls.strip() for cls, _ in unit_rows} == {"is-dim"}, (
            f"{surface}: a unit row carries a state colour: {unit_rows}"
        )


def test_the_unit_rows_sit_between_the_run_level_moments(client):
    """A timeline out of order is not a timeline."""
    run = _published(result_overrides={"execution_units": TIMED_UNITS})
    for surface, html in _surfaces(client, run).items():
        panel = _flat(_panel(html, "TIMELINE"))
        order = [
            panel.index("submission committed to disk"),
            panel.index("execution started"),
            panel.index("unit 01 ran to"),
            panel.index("unit 04 ran to"),
            panel.index("terminal state committed"),
        ]
        assert order == sorted(order), f"{surface}: the rows are out of order: {order}"


def test_a_half_timestamped_unit_is_counted_rather_than_placed(client):
    """One end is not an interval.

    A row drawn from a start with no finish sits on the timeline looking like
    every other row and means something weaker. And an absence sentence that
    reads "not recorded" over a panel that just drew three rows would be read
    as applying to all of them, so the one that stays says how many.
    """
    half = [dict(u) for u in TIMED_UNITS]
    half[2]["completed_at"] = None
    half[3]["started_at"] = None
    half[3]["completed_at"] = None
    run = _published(result_overrides={"execution_units": half})
    for surface, html in _surfaces(client, run).items():
        panel = _flat(_panel(html, "TIMELINE"))
        assert "unit 01 ran to" in panel and "unit 02 ran to" in panel
        assert "unit 03 ran to" not in panel, (
            f"{surface}: a unit with one end is placed as though it had two"
        )
        assert "unit 04 ran to" not in panel
        assert "2 of 4 units did not record both ends" in panel, (
            f"{surface}: the count is wrong or missing: {panel[:400]}"
        )
        assert "per-unit start and finish times are not recorded" not in panel, (
            f"{surface}: a blanket absence is claimed over units that recorded both"
        )


def test_the_timeline_gutter_fits_the_longest_offset_it_can_print():
    """Found by opening the page rather than by a test.

    The column was 50px, which holds seven mono characters at 11px. `_offset`
    prints eight from ten minutes onward, so every run past ten minutes wrapped
    its own `terminal state committed` row onto two lines at double height --
    and the per-unit rows multiply that by the unit count. Measured in
    Chromium: one character is 6.44px, so nine characters is 58.0px and the
    column is 60px.

    Nine is the ceiling for any run shorter than six weeks. Past that the cell
    wraps again, which is the right way for a number nobody will see to fail.
    """
    from datetime import timedelta

    start = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    spans = (0, 0.4, 9.9, 59.9, 60, 599, 600, 3599, 3600, 35999, 359999, 444600)
    printed = [run_detail._offset(start, start + timedelta(seconds=s)) for s in spans]
    longest = max(len(text) for text in printed)
    assert longest == 9, f"the formatter's width changed: {printed}"

    declared = re.search(
        r"grid-template-columns:\s*(\d+)px", _rule(RUN_DETAIL_CSS, ".rd-tl-row")
    )
    assert declared, "the timeline gutter is no longer a fixed column"
    assert int(declared.group(1)) >= 58, (
        f"a {longest}-character offset needs 58.0px and the column is "
        f"{declared.group(1)}px, so it wraps"
    )


def test_two_units_that_started_together_keep_a_stable_order(client):
    """`dag-2` and `dag-3` share a start instant, and a set iteration order is
    not a thing a reader should see change between two loads of one run."""
    run = _published(result_overrides={"execution_units": TIMED_UNITS})
    panel = _flat(_panel(_surfaces(client, run)["server"], "TIMELINE"))
    assert panel.index("unit 02 ran to") < panel.index("unit 03 ran to")


def test_nothing_fetches_the_audit_bundle_to_render_run_detail():
    """The bundle is a separate scope, asked for by name.

    Per-unit timings live in `full_log.json`, which ships inside it. Fetching
    the bundle to draw a timeline would undo the separation the two downloads
    exist to keep — quietly, and on every page view — so the panel is drawn
    from what the execution record serves and says `+—` for the rest.

    The distinction is the *bundle*, not the file. `/run/{id}` has always read
    the run directory's own `full_log.json` off disk; that is how it loads the
    run at all, and it is not a download. What must never happen is a surface
    reaching for the packaged artifact, by HTTP or by opening the zip.
    """
    root = Path(__file__).resolve().parent.parent
    for name in ("run_detail.py", "routes_run.py", "templates/_dashboard.js"):
        source = (root / name).read_text(encoding="utf-8")
        for reach in ("prepare_archive", "ZipFile", "zipfile"):
            assert reach not in source, (
                f"{name} opens an archive while rendering run detail"
            )
    js = (root / "templates/_dashboard.js").read_text(encoding="utf-8")
    assert "audit-download" not in js, (
        "the console fetches the audit bundle rather than linking to it"
    )


def test_the_audit_bundle_is_still_reachable_deliberately(client):
    """Separate scope, not a hidden one: the link is there to be clicked."""
    run = _published()
    html = _surfaces(client, run)["console"]
    assert f"/v1/executions/{EXECUTION}/audit-download" in html
    assert f"/v1/executions/{EXECUTION}/download" in html
    assert "asked for by name" in _flat(html), (
        "the reason the two downloads are separate is not stated"
    )


# ── What no surface may render ───────────────────────────────────────


def test_no_surface_renders_a_wall_clock_or_a_speed_multiplier(client):
    """Counts only.

    A speed multiplier needs a serial baseline and nothing records one, so it
    is not computable rather than merely absent. The run's duration is a
    timestamp in the timeline, not a headline figure in the strip.
    """
    run = _published()
    for surface, html in _surfaces(client, run).items():
        strip = html[html.index('class="rd-metrics"'):html.index('class="rd-body"')]
        assert [m for m in ("UNITS", "WAVES", "DELIVERABLES", "AUDIT RECORDS")
                if m in strip] == ["UNITS", "WAVES", "DELIVERABLES", "AUDIT RECORDS"]
        for banned in ("WALL", "SPEEDUP", "faster", "×", "x faster"):
            assert banned not in strip, f"{surface}: the metric strip renders {banned!r}"


def test_no_surface_renders_a_quality_percentage(client):
    run = _published()
    for surface, html in _surfaces(client, run).items():
        text = re.sub(r"<[^>]+>", " ", html)
        found = re.search(
            r"\d+\s*%[^.]{0,80}\b(task|runnable|on-spec|quality|success|pass)\b"
            r"|\b(task|runnable|on-spec|quality|success)\b[^.]{0,80}\d+\s*%",
            text,
            re.I,
        )
        assert not found, f"{surface}: renders a quality percentage {found!r}"


BANNED = (
    "verified", "trustless", "tamper-proof", "tamper proof",
    "proof of correct execution", "signature", "signed",
    "attestation", "attested",
)

# The one position where a prohibited word may appear, and it is the opposite
# of the claim the rule forbids.
#
# `signature` is a real, always-NULL column that ADR 0017 requires the export
# to carry so that filling it later is not a schema break. The opened envelope
# renders the record's own field names, and this row exists precisely to show
# that the slot is empty. Banning the word here would mean the one surface that
# could tell a reader nothing is signed is the one surface that may not.
#
# So the exception is a position, not a permission: the field-name cell of a
# row whose marker is hollow and whose value is `reserved · empty`. Everywhere
# else on both surfaces the blanket ban still holds, and
# `test_the_reserved_slot_is_a_slot_and_the_only_place_the_word_appears` is
# stricter than the ban was -- it checks the position, the value, the marker
# and the count rather than only the absence.
RESERVED_ROW = re.compile(
    r'<div class="rd-envelope-row">\s*'
    r'<i class="rd-marker is-7 is-absent"[^>]*>\s*</i>\s*'
    r'<span class="rd-envelope-field">signature</span>\s*'
    r'<span class="rd-envelope-value">reserved · empty</span>'
)


def test_no_surface_uses_a_prohibited_word(client):
    """The words are prohibited as claims, so this reads the rendered text.

    `unverified` is the assurance level's own value and is the opposite claim,
    so the check is on whole words.
    """
    run = _published()
    for surface, html in _surfaces(client, run).items():
        text = re.sub(r"<[^>]+>", " ", RESERVED_ROW.sub(" ", html)).lower()
        for word in BANNED:
            assert not re.search(rf"(?<![a-z-]){re.escape(word)}(?![a-z])", text), (
                f"{surface}: renders the prohibited word {word!r}"
            )


def test_the_reserved_slot_is_a_slot_and_the_only_place_the_word_appears(client):
    """The carve-out above, checked from the other side.

    Once, in the reserved row, hollow, beside an empty value, and described as
    a slot and nothing else. If the word ever reaches any other position on
    either surface this fails -- which is a stricter rule than the blanket ban
    it replaces, because the blanket ban could not have said where.
    """
    run = _published()
    for surface, html in _surfaces(client, run).items():
        matches = list(RESERVED_ROW.finditer(html))
        assert len(matches) == 1, (
            f"{surface}: expected exactly one reserved row, found {len(matches)}"
        )

        elsewhere = re.sub(r"<[^>]+>", " ", RESERVED_ROW.sub(" ", html)).lower()
        assert "signature" not in elsewhere, (
            f"{surface}: the word reaches a second position, outside the "
            "reserved row"
        )

        panel = _panel(html, "PRODUCED BY")
        assert "RESERVED" in panel, f"{surface}: the reserved group lost its label"
        assert "A slot, not a feature" in panel, (
            f"{surface}: the reserved row does not say it is a slot"
        )
        # Named as absent, one by one, so the row cannot be read as a feature
        # that merely has not run yet.
        for absent in ("no key", "no key management", "no transparency log",
                       "no third party"):
            assert absent in panel, f"{surface}: the slot does not say {absent!r}"


# ── The server-rendered page's three differences ─────────────────────


def test_the_run_page_renders_no_relative_age_and_no_live_cell(client):
    """It is a snapshot with no client.

    "2h 14m ago" on a page nothing refreshes is a sentence that stops being
    true the moment it is written, with nothing there to correct it.
    """
    run = _published()
    html = client.get(f"/run/{run}").text
    body = html[html.index("<body"):]
    for stale in (" ago<", "just now", "m ago", "h ago", "d ago"):
        assert stale not in body, f"/run/{{id}} renders a relative age: {stale!r}"
    assert "UTC" in body, "the page carries no timestamp of its own"
    for live in ("setInterval", "WebSocket", "is-live", "statusbar"):
        assert live not in body, f"/run/{{id}} carries a live cell: {live!r}"


def test_the_console_may_render_an_age_because_it_can_correct_it(client):
    run = _published()
    html = _surfaces(client, run)["console"]
    assert "rd-age" in html
    assert re.search(r"(just now|\d+[mhd] ago)", html), (
        "the console polls and re-renders, so an age it can correct is honest there"
    )


def test_controls_are_44px_public_and_28px_in_the_console(client):
    run = _published()
    assert 'data-surface="server"' in _surfaces(client, run)["server"]
    assert 'data-surface="console"' in _surfaces(client, run)["console"]
    assert 'height: 44px' in _rule(RUN_DETAIL_CSS, '.rd[data-surface="server"]  .rd-btn')
    assert 'height: 28px' in _rule(RUN_DETAIL_CSS, '.rd[data-surface="console"] .rd-btn')


def test_the_two_surfaces_are_one_structure(client):
    """Not two layouts that agree today.

    Both render the same fragment from run_detail.py, so every panel label the
    server page carries is on the console surface too, in the same order.
    """
    run = _published()
    surfaces = _surfaces(client, run)
    labels = [
        "LIFECYCLE", "VALIDATION", "ASSURANCE", "PLACEMENT", "UNITS",
        "DELIVERABLE", "HOW IT WAS SPLIT", "MANIFEST", "PRODUCED BY", "TIMELINE",
    ]
    for surface, html in surfaces.items():
        positions = [html.index(label) for label in labels]
        assert positions == sorted(positions), (
            f"{surface}: the panels are not in the design's order"
        )


def test_the_deliverable_leads_and_the_plan_follows(client):
    """Plan is process; the code is the product."""
    run = _published()
    for surface, html in _surfaces(client, run).items():
        assert html.index("DELIVERABLE") < html.index("HOW IT WAS SPLIT"), (
            f"{surface}: the process-first order is back"
        )


# ── The floor and the tokens ─────────────────────────────────────────


def test_nothing_in_the_run_detail_stylesheet_renders_below_11px():
    """The dashboard appears on camera during the demo recording."""
    sizes = [float(m) for m in re.findall(r"font-size:\s*([0-9.]+)px", RUN_DETAIL_CSS)]
    assert sizes, "no font sizes found, so this test is not checking anything"
    assert min(sizes) >= 11, f"{min(sizes)}px is below the floor"


def test_every_state_survives_greyscale():
    """Filled versus hollow carries the meaning, so colour is never alone."""
    hollow = _rule(RUN_DETAIL_CSS, ".rd-marker.is-absent")
    assert "background: transparent" in hollow, (
        "an unrecorded fact is told apart by shape, not only by ink"
    )
    for filled in ("is-ok", "is-bad", "is-neutral"):
        assert "background:" in _rule(RUN_DETAIL_CSS, f".rd-marker.{filled}")


# ── Overview's two stale citations ───────────────────────────────────


def test_overview_and_the_status_bar_never_put_one_word_over_two_numbers(client):
    """The archived handoff had Overview read "running now" and "queued" off
    `/health`. It has no running count at all, and its `tasks_pending` is the
    *subtask* queue rather than the job queue — so ported as written, one
    screen would have carried QUEUED over two different numbers with no way
    for a reader to tell which one was the queue they meant.

    RUNNING and QUEUED are the status bar's, both from `/metrics`, and there
    is exactly one of each on the page. Overview's cell is named after the
    number it actually holds.
    """
    page = dashboard._page("dashboard.html")
    body = page[page.index("<body"):]
    labels = re.findall(r'class="stat-label">([^<]*)<', body)
    cells = re.findall(r'class="statusbar-k">([^<]*)<', body)

    assert "Subtasks pending" in labels, (
        "Overview's subtask queue is not named after the number it holds"
    )
    assert "Tasks pending" not in labels, (
        "a cell called 'Tasks pending' beside a bar cell called QUEUED is two "
        "labels a reader takes for one thing, over two different numbers"
    )
    for word in ("RUNNING", "QUEUED"):
        assert cells.count(word) == 1, f"{word} appears {cells.count(word)} times"
        assert not any(word.lower() in label.lower() for label in labels), (
            f"Overview also carries {word}, so the page has two of them"
        )


def test_running_and_queued_come_from_the_same_source_as_the_status_bar():
    """`/health` has no running count, and its `tasks_pending` is a different
    number from `/metrics.jobs_queued`. Reading both cells off `/metrics` is
    what makes it impossible for the two to disagree.
    """
    block = JS[JS.index("const met = await apiJson('/metrics');"):]
    block = block[: block.index("} catch (e) {")]
    assert "statusText.running = String(met.jobs_running);" in block
    assert "statusText.queued = String(met.jobs_queued);" in block

    health = JS[JS.index("if (health) {"):JS.index("const met = await apiJson")]
    for wrong in ("statusText.running", "statusText.queued"):
        assert wrong not in health, (
            f"{wrong} is set from /health, which does not serve that number"
        )


def test_a_rendering_failure_costs_the_panel_and_not_the_endpoint(client, monkeypatch):
    """`/history/{timestamp}` had consumers before this pass.

    `evals/run_evals.py` reads its JSON on every completed remote run, so a
    bug in the run-detail renderer must not be able to take that away. The
    console shows its own could-not-build state instead.
    """
    run = _published()
    import run_detail as module

    def boom(*_args, **_kwargs):
        raise RuntimeError("a rendering bug")

    monkeypatch.setattr(module, "render", boom)
    body = client.get(f"/history/{run}")
    assert body.status_code == 200, "a rendering bug took down the endpoint"
    payload = body.json()
    assert payload["detail_html"] == ""
    for field in ("task", "plan", "rating", "code_files", "final_output"):
        assert field in payload, f"{field} was lost with the fragment"
    assert "This run\u2019s detail could not be built" in JS, (
        "the console opens a blank modal when the fragment is empty"
    )


def test_the_replay_line_is_not_drawn_from_a_field_nothing_writes():
    """The line is drawn, and still not from either place it must not come from.

    §8.11 is closed by a durable fact about the *submission*
    (`execution_submissions.replay_count`, served under `/v1/operator/`), so
    the two rules that made it a gap are permanent now rather than pending:

    The POST response is not a source. `SubmittedExecution.replayed` -- the
    `Idempotency-Replayed` header -- is gone the moment the response is read,
    and reading the fact off a plausible-looking log key instead renders a line
    that is always absent, looks like it worked, and starts lying the moment
    someone writes that key for another reason.

    The execution is not a source either. Terminal state is monotonic under ADR
    0009 and a replay can arrive long after the run reached it, so a replay
    field on `ExecutionResultV1` would mutate a settled record. If one ever
    appears here, the fix is to take it off, not to read it.
    """
    root = Path(__file__).resolve().parent.parent
    source = (root / "run_detail.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    for invented in ('"idempotency"', '"replayed"', "'replayed'"):
        assert invented not in code, (
            f"run_detail.py reads {invented}, which no record carries"
        )
    contract = (root / "execution" / "contracts.py").read_text(encoding="utf-8")
    body = contract[contract.index("class ExecutionResultV1"):]
    body = body[: body.index("\n\n\n")] if "\n\n\n" in body else body
    assert "replay" not in body.lower(), (
        "ExecutionResultV1 carries a replay field. Terminal state is monotonic "
        "under ADR 0009 -- the fact belongs beside the execution, which is "
        "where the submission mapping already keeps it"
    )

def test_the_audit_scope_is_only_offered_where_it_exists(client):
    """Two scopes, or one, never one URL wearing two labels.

    A legacy run with no execution record has a single `/history/{id}/download`
    bundle. Offering "Audit bundle" beside "Download deliverables" there would
    point both at it and imply a split this run does not have.
    """
    _write_log("20260101_000000")
    legacy = client.get("/history/20260101_000000").json()["detail_html"]
    assert "Download deliverables" in legacy
    assert "Audit bundle" not in legacy, (
        "the legacy download is one bundle, so there is no audit scope to offer"
    )
    assert "audit-download" not in legacy

    run = _published()
    served = _surfaces(client, run)["console"]
    assert "Audit bundle" in served
    assert f"/v1/executions/{EXECUTION}/audit-download" in served
    assert f"/v1/executions/{EXECUTION}/download" in served


def test_a_unit_that_did_not_complete_says_so_in_a_word(client):
    """Colour is never the only signal, and here it nearly was.

    The marker beside the unit title is the card's only state signal, and three
    of its four tones are filled — so in greyscale a failed unit and a
    completed one are a 6px square a few grey levels apart. Measured in the
    browser: `is-ok` 100, `is-bad` 72, `is-neutral` 115 in the light theme.
    That is not a distinction anyone should have to make, so a unit that did
    not complete carries the word.

    `completed` stays marker-only on purpose: a word on every card is noise
    that would make the one that matters harder to find.
    """
    run = _published(result_overrides={"execution_units": [
        {"unit_id": "dag-1", "kind": "dag_subtask", "title": "Parse --since",
         "depends_on": [], "status": "completed", "placement": "distributed",
         "node_id": "node-7c22"},
        {"unit_id": "dag-2", "kind": "dag_subtask", "title": "Filter rows",
         "depends_on": ["dag-1"], "status": "failed", "placement": "distributed",
         "node_id": "node-a1f3"},
        {"unit_id": "dag-3", "kind": "dag_subtask", "title": "Update summary",
         "depends_on": ["dag-1"], "status": "cancelled", "placement": "local",
         "node_id": None},
    ]})
    for surface, html in _surfaces(client, run).items():
        panel = _panel(html, "HOW IT WAS SPLIT")
        assert panel.count("rd-unit-state") == 2, (
            f"{surface}: expected the failed and cancelled units to say so, and "
            "the completed one not to"
        )
        flat = _flat(panel)
        assert "failed" in flat and "cancelled" in flat, f"{surface}: {flat[:200]}"
        # The completed unit is the unmarked case.
        first = panel[panel.index("Parse --since"):panel.index("Filter rows")]
        assert "rd-unit-state" not in first, (
            f"{surface}: a word on every card is noise"
        )


def test_a_cut_preview_says_it_was_cut(client):
    """A silent 14 lines of a 500-line file says "this file is 14 lines".

    The panel is a preview and the whole file is one authenticated download
    away, which is right — but the reader has to be able to tell the two apart,
    and the size beside the filename is not enough on its own.
    """
    from routes_run import PREVIEW_LINES

    long_file = "\n".join(f"line_{i} = {i}" for i in range(PREVIEW_LINES * 3))
    _write_log()
    (Path("output") / RUN / "code" / "summary.py").write_text(long_file, encoding="utf-8")

    html = client.get(f"/run/{RUN}").text
    bar = html[html.index('class="rd-file-bar"'):html.index('class="rd-file-body"')]
    assert f"first {PREVIEW_LINES} of {PREVIEW_LINES * 3} lines" in bar, bar[:400]

    body = html[html.index('class="rd-file-body"'):]
    body = body[: body.index("</pre>")]
    assert body.count("line_") == PREVIEW_LINES


def test_a_short_preview_says_nothing_about_being_cut(client):
    """The notice is a finding, not furniture."""
    from routes_run import PREVIEW_LINES

    _write_log()
    (Path("output") / RUN / "code" / "summary.py").write_text(
        "\n".join(f"line_{i} = {i}" for i in range(PREVIEW_LINES - 2)),
        encoding="utf-8",
    )
    html = client.get(f"/run/{RUN}").text
    bar = html[html.index('class="rd-file-bar"'):html.index('class="rd-file-body"')]
    assert "lines" not in bar, bar[:400]


# ── The opened envelope ──────────────────────────────────────────────
# Phase 3b. Opening the envelope puts eight field groups beside a manifest
# chip, which is when the two-claims rule is most likely to break by accident.
# The colour rules above already reach the opened state, because the groups
# render inside the same `.rd-envelope` section and under the same
# `.rd-envelope-*` class prefix — the first test here is what holds that true.

ENVELOPE_GROUPS = (
    "EXECUTION",
    "PRODUCER IDENTITY",
    "CAPABILITY AND EXECUTOR",
    "MODEL",
    "VALIDATORS · IN ORDER",
    "ARTIFACTS",
    "SAMPLING",
    "RESERVED",
)


def _opened(html: str) -> str:
    """The disclosure's contents, which must sit inside the envelope panel."""
    panel = _panel(html, "PRODUCED BY")
    return panel[panel.index("<details"):]


def test_the_opened_envelope_is_inside_the_panel_the_colour_rules_guard(client):
    """The reason the colour tests above still bite once it is opened.

    If the groups were rendered as a sibling section, or under a class prefix
    of their own, every rule holding "no chip, no lamp, no colour" would stop
    covering the state where breaking it matters most. So the structural fact
    is asserted directly rather than assumed.
    """
    run = _published()
    for surface, html in _surfaces(client, run).items():
        panel = _panel(html, "PRODUCED BY")
        assert "<details" in panel, f"{surface}: the envelope has no opened state"
        opened = _opened(html)
        assert "rd-envelope-group" in opened, (
            f"{surface}: the field groups are not inside the envelope panel"
        )
        # Every element the disclosure adds takes the prefix the stylesheet
        # rule scans, so growing the panel grows what that rule covers.
        classes = set(re.findall(r'class="(rd-[a-z-]+)', opened))
        stray = {
            name for name in classes
            if not name.startswith("rd-envelope") and name != "rd-marker"
        }
        assert not stray, (
            f"{surface}: the opened envelope renders {sorted(stray)}, which the "
            "no-colour rule over `.rd-envelope*` does not reach"
        )


def test_the_opened_envelope_carries_no_chip_lamp_or_state_colour(client):
    """The same rule as the summary mode, asserted against the opened one."""
    run = _published()
    for surface, html in _surfaces(client, run).items():
        opened = _opened(html)
        assert "rd-chip" not in opened, f"{surface}: the opened envelope renders a chip"
        assert "lamp" not in opened, f"{surface}: the opened envelope renders a lamp"
        for tone in ("is-ok", "is-bad", "is-pass", "is-sealed", "is-info",
                     "is-warn", "is-danger", "is-break", "is-linked"):
            assert tone not in opened, (
                f"{surface}: the opened envelope renders the state class {tone!r}"
            )


def test_the_eight_field_groups_render_in_order(client):
    """Eight, in this order. `PRODUCER IDENTITY` carries its own entry count,
    which is the plural case announcing itself, so the labels are compared by
    prefix rather than exactly."""
    run = _published()
    for surface, html in _surfaces(client, run).items():
        found = re.findall(r'class="rd-envelope-group-label">([^<]*)<', _opened(html))
        assert len(found) == len(ENVELOPE_GROUPS), f"{surface}: the groups are {found}"
        for label, expected in zip(found, ENVELOPE_GROUPS):
            assert label.startswith(expected), (
                f"{surface}: expected a {expected!r} group here, found {label!r}"
            )


def test_absence_is_a_value_and_never_a_blank(client):
    """Every row has a marker and something to read.

    A blank cell and a dash both say "there is nothing here" without saying
    whether it was never recorded or recorded as empty. The shape carries that
    difference — filled for a recorded fact, hollow for one that was not — so
    every row must have exactly one marker, and no row may fall back to a dash.
    """
    run = _published(receipts=3, legacy_last=True)
    for surface, html in _surfaces(client, run).items():
        opened = _opened(html)
        rows = re.findall(
            r'<div class="rd-envelope-row">(.*?)\n          </div>', opened, re.S
        )
        assert rows, f"{surface}: no field rows rendered"
        for row in rows:
            markers = re.findall(r"rd-marker is-7 (is-neutral|is-absent)", row)
            assert len(markers) == 1, (
                f"{surface}: a row carries {len(markers)} markers, not one: {row[:200]}"
            )
            value = re.search(r'class="rd-envelope-value">([^<]*)<', row)
            assert value and value.group(1).strip(), (
                f"{surface}: a row renders a blank value: {row[:200]}"
            )
            assert value.group(1).strip() not in ("—", "-", "--", "n/a", "?"), (
                f"{surface}: a row renders a dash instead of naming the absence"
            )
        # Not writing something down is neither a fault nor fine.
        assert "is-warn" not in opened, f"{surface}: an absence is drawn as a warning"
        assert "is-danger" not in opened, f"{surface}: an absence is drawn as a fault"


def test_the_three_sampling_absences_stay_distinct(client):
    """Three different facts, so three named rows, never one word for all.

    `sampling_parameters` is nothing pinned at all. `sampling_seed_honoured` is
    a seed that was set and is not shown to have been applied.
    `producer_sampling` is about a different machine entirely — the worker
    protocol does not carry it back. Collapsing them to "unknown" would be a
    fourth claim nobody made.
    """
    from provenance import (
        UNKNOWN_PRODUCER_SAMPLING,
        UNKNOWN_SAMPLING,
        UNKNOWN_SEED_HONOURED,
    )

    run = _published()
    for surface, html in _surfaces(client, run).items():
        opened = _opened(html)
        sampling = opened[opened.index("SAMPLING"):opened.index("RESERVED")]
        names = re.findall(r'class="rd-envelope-field">([^<]*)<', sampling)
        # This run pins nothing and has a producer, so two of the three are
        # live, and each has to be named on its own row.
        assert UNKNOWN_SAMPLING in names, f"{surface}: {names}"
        assert UNKNOWN_PRODUCER_SAMPLING in names, f"{surface}: {names}"

        notes = " ".join(re.findall(r'class="rd-envelope-note">([^<]*)<', sampling))
        assert "absence of a setting" in notes, (
            f"{surface}: sampling_parameters does not say which absence it is"
        )
        assert "different machine" in notes, (
            f"{surface}: producer_sampling does not say it is about another machine"
        )

    # The third lives on a different branch of the record, so it is exercised
    # directly rather than by contriving a run that produces all three at once.
    rows = run_detail._sampling_group(
        {
            "sampling": {"temperature": 0.7, "seed": 11, "pinned": False},
            "unknown_facts": [UNKNOWN_SEED_HONOURED],
        }
    )["rows"]
    named = {row["name"]: row for row in rows}
    assert UNKNOWN_SEED_HONOURED in named, sorted(named)
    assert named[UNKNOWN_SEED_HONOURED]["recorded"] is False
    assert "assumed" in named[UNKNOWN_SEED_HONOURED]["note"], (
        "the seed-honouring absence does not say it is assumed rather than checked"
    )
    assert UNKNOWN_SAMPLING not in named, (
        "a seed was set, so 'nothing pinned' is not the absence in play here — "
        "rendering both would collapse two different facts into one"
    )


def test_producers_is_always_a_list_and_one_producer_is_a_list_of_one(client):
    run = _published(receipts=1)
    for surface, html in _surfaces(client, run).items():
        opened = _opened(html)
        assert "1 accepted receipt" in opened, f"{surface}: {opened[:400]}"
        assert "PRODUCER IDENTITY · 1 ENTRY" in opened, surface


def test_plural_producers_leave_the_singular_fields_not_applicable(client):
    """The primary case, not an edge.

    The ensemble path settles several accepted receipts and leaves the singular
    fields null rather than electing a winner, because no rule for choosing
    between receipts exists. So they render `not applicable` with a hollow
    marker — never blank, and never one of the three picked arbitrarily.
    """
    run = _published(receipts=3)
    for surface, html in _surfaces(client, run).items():
        opened = _opened(html)
        assert "3 accepted receipts" in opened, surface
        assert "PRODUCER IDENTITY · 3 ENTRIES" in opened, surface

        execution = opened[opened.index("EXECUTION"):opened.index("PRODUCER IDENTITY")]
        rows = re.findall(
            r'<div class="rd-envelope-row">(.*?)\n          </div>', execution, re.S
        )
        by_field = {
            re.search(r'class="rd-envelope-field">([^<]*)<', row).group(1): row
            for row in rows
        }
        for field in ("attempt_id", "receipt_id", "unit_id"):
            assert field in by_field, f"{surface}: no row for {field}"
            row = by_field[field]
            value = re.search(r'class="rd-envelope-value">([^<]*)<', row).group(1)
            assert value == "not applicable", (
                f"{surface}: {field} renders {value!r} rather than `not "
                "applicable` — with three receipts there is no single attempt "
                "to name, and electing one would be a choice the record did "
                "not make"
            )
            assert "is-absent" in row, (
                f"{surface}: {field} is not applicable but carries a filled marker"
            )
        # All three producers are listed and none is elected.
        assert "01 · enrollment_id" in opened, surface
        assert "03 · enrollment_id" in opened, surface


def test_no_heading_spans_the_manifest_the_envelope_and_the_chain(client):
    """The 3a rule, extended to the panel Phase 3b adds.

    3a's version of this test failed its own poisoning because it looked only
    *between* the two panels and missed a heading above the pair. This one
    reads the whole side column, from the column element to the footer, so a
    heading anywhere over the three is a finding — and so is a shared label.
    """
    run = _published()
    for surface, html in _surfaces(client, run).items():
        column = html[html.index('class="rd-side"'):]
        column = column[: column.index('class="rd-foot"')]

        assert not re.search(r"<h[1-6][\s>]", column), (
            f"{surface}: a heading sits over the manifest, the envelope and the "
            "chain. There is no INTEGRITY panel and no TRUST section in this "
            "design: a shared header is what invites a reader to add them up."
        )
        # Read off the label positions rather than the raw text. The rule is
        # about a *heading* that spans the records, and `integrity_mode` is a
        # field name inside one of them -- a blunter check fails on the
        # envelope faithfully naming the manifest's own column.
        headings = (
            re.findall(r'class="rd-panel-label">\s*([^<]*?)\s*<', column)
            + re.findall(r'class="rd-envelope-group-label">\s*([^<]*?)\s*<', column)
            + re.findall(r'class="rd-group-label">\s*([^<]*?)\s*<', column)
            + re.findall(r"<h[1-6][^>]*>\s*([^<]*?)\s*<", column)
        )
        for banned in ("INTEGRITY", "TRUST"):
            offenders = [h for h in headings if banned in h.upper()]
            assert not offenders, (
                f"{surface}: the column carries the heading(s) {offenders}, and "
                f"there is no {banned} panel in this design"
            )
        labels = re.findall(r'class="rd-panel-label">\s*([^<]*?)\s*<', column)
        expected = (
            ["MANIFEST", "PRODUCED BY", "LEDGER CHAIN", "TIMELINE"]
            if surface == "console"
            else ["MANIFEST", "PRODUCED BY", "TIMELINE"]
        )
        assert labels == expected, (
            f"{surface}: the labels in the side column are {labels}, which is "
            "not one label per record"
        )


# ── The ledger chain panel ───────────────────────────────────────────
# Three states, and two rules that are easy to get backwards. Entries after a
# break render `not walked`, because verification returns at the first break
# and nothing past it was read. The genesis boundary is not a break: entries
# written before the chain existed have no link, are never retrofitted with
# one, and are counted separately at the head.
#
# And the rule the whole panel turns on: **intact is not green.** `--accent`
# means PASS / connected / ok. "No entry changed without every link after it
# also being recomputed" is none of those, because a full rewrite satisfies it.


def _seed_chain(count: int) -> None:
    """Real chained entries, written the way settlement writes them."""
    from ledger import log_contribution

    for index in range(count):
        log_contribution(
            f"node-{index:02d}",
            "compute",
            5,
            task="compute_contribution",
            contribution_id=f"contribution:{index:016d}",
            attempt_id=f"attempt-{index:04d}",
        )


def _seed_unchained(count: int) -> None:
    """Entries from before the chain existed: NULL in all three columns.

    Written directly, because there is no code path that produces one any more
    — which is the point. They are history, and history is not rewritten to
    fabricate links it never had.
    """
    from ledger import LEDGER_DB_FILE, ensure_contribution_schema

    with sqlite3.connect(LEDGER_DB_FILE) as con:
        ensure_contribution_schema(con)
        con.executemany(
            "INSERT INTO contributions (contribution_id, contributor, "
            "contribution_type, points, task, details, basis, "
            "points_are_monetary, created_at, entry_index, previous_digest, "
            "entry_digest) "
            "VALUES (?, ?, 'compute', 5, 'compute_contribution', '', "
            "'pre_chain', 0, ?, NULL, NULL, NULL)",
            [(f"legacy:{i:016d}", f"old-{i:02d}", 100.0 + i) for i in range(count)],
        )
        con.commit()


def _break_chain(index: int) -> None:
    """A real break, made by editing a chained column rather than mocking a verdict.

    `contributor` is one of the digested columns, so changing it makes the
    stored digest stop matching the recomputed one — which is exactly the
    accidental corruption and casual edit the chain exists to detect.
    """
    from ledger import LEDGER_DB_FILE

    with sqlite3.connect(LEDGER_DB_FILE) as con:
        con.execute(
            "UPDATE contributions SET contributor = ? WHERE entry_index = ?",
            ("edited-after-the-fact", index),
        )
        con.commit()


def _chain_panel(html: str) -> str:
    start = html.index("LEDGER CHAIN")
    rest = html[start:]
    return rest[: rest.index("</section>")]


def test_the_chain_panel_renders_in_the_console_and_not_on_the_run_page(client):
    """Placement follows gating, and the gate here is the prefix.

    `/v1/operator/ledger-chain` sits under a prefix `deploy/Caddyfile.public`
    refuses at the edge, next to `/dashboard` and `/metrics` — so a valid
    viewer key is not enough to reach it from the public Internet. `/run/{id}`
    is reachable with one. Drawing an operator-gated verdict on the shareable
    page would put it in front of a reader the route itself refuses.
    """
    _seed_chain(4)
    run = _published()
    surfaces = _surfaces(client, run)
    assert "LEDGER CHAIN" in surfaces["console"], (
        "the console does not render the chain panel"
    )
    assert "LEDGER CHAIN" not in surfaces["server"], (
        "the shareable run page renders an operator-gated verdict"
    )
    # The markup, not the page: one stylesheet serves both surfaces by design,
    # so the chain's rules are present on the public page and its elements
    # must not be.
    assert 'class="rd-panel rd-chain"' not in surfaces["server"]
    assert 'class="rd-chain-verdict"' not in surfaces["server"]


def test_an_intact_chain_is_never_drawn_in_accent(client):
    """Ordinary ink, a filled marker and a walked count. Never a tick.

    A green tick here would be the exact class of claim this repo refuses: an
    intact chain means no entry changed without every link after it also being
    recomputed, and a full rewrite satisfies that.
    """
    _seed_chain(5)
    run = _published()
    panel = _chain_panel(_surfaces(client, run)["console"])

    assert "LINKS INTACT" in panel, panel[:400]
    assert "5 walked · 0 unlinked" in panel, panel[:400]
    for tone in ("is-ok", "is-pass", "is-sealed", "accent"):
        assert tone not in panel, f"the intact chain renders {tone!r}"
    # The marker is filled, so the state survives greyscale without the colour.
    assert "rd-chain-verdict-marker is-linked" in panel


def test_the_chain_stylesheet_never_reaches_for_accent():
    """Checked against the stylesheet, not only the markup.

    A class with no state name in it could still be handed `--accent`. Every
    rule that styles something in the chain panel is read, and the only state
    colour any of them may take is the danger family — because a break is the
    one state on this panel that is a finding.
    """
    allowed = {
        "--text", "--text-dim", "--text-muted", "--text-faint",
        "--border", "--border-subtle", "--border-strong",
        "--surface", "--surface-sunken", "--surface-hover", "--bg",
        "--danger", "--danger-text", "--danger-body", "--danger-wash", "--danger-line",
        "--mono", "--sans",
    }
    blocks = re.findall(r"\.rd-chain[^{}]*\{([^}]*)\}", RUN_DETAIL_CSS)
    assert blocks, "the chain panel has no styles at all"
    for block in blocks:
        for token in re.findall(r"var\((--[a-z0-9-]+)\)", block):
            assert token in allowed, (
                f"the chain panel styles with {token}. Intact is not green: "
                "--accent means PASS / connected / ok, and a walked chain is "
                "none of those."
            )
    for block in blocks:
        assert "--accent" not in block, "the chain panel reaches for --accent"


def test_entries_after_a_break_render_not_walked_rather_than_broken(client):
    """Verification returns at the first break, so nothing past it was read.

    Drawing those entries broken claims more than the walk found; drawing them
    intact claims the opposite. They are a third thing, and the legend says so.
    """
    _seed_chain(6)
    _break_chain(3)
    run = _published()
    panel = _chain_panel(_surfaces(client, run)["console"])

    assert "LINK BROKEN AT 3" in panel, panel[:400]
    assert "walk stopped at the first break" in panel
    assert "not walked" in panel, "the legend does not name the not-walked state"

    tones = re.findall(r'class="rd-chain-box (is-[a-z]+)">([^<]*)<', panel)
    states = {label: tone for tone, label in tones}
    assert states.get("03") == "is-break", states
    for after in ("04", "05"):
        assert states.get(after) == "is-unwalked", (
            f"entry {after} is past the break and renders {states.get(after)!r}, "
            "which claims the walk reached it"
        )
    for before in ("00", "01"):
        assert states.get(before) == "is-linked", states


def test_the_genesis_head_is_distinguishable_from_a_break(client):
    """Not a break, and never drawn as one.

    Entries written before the chain existed have no link and are never
    retrofitted with one. They are shown unlinked at the head, counted
    separately in the verdict, and the chain is still reported intact.
    """
    _seed_unchained(3)
    _seed_chain(4)
    run = _published()
    panel = _chain_panel(_surfaces(client, run)["console"])

    assert "LINKS INTACT" in panel, panel[:500]
    assert "3 ENTRIES PREDATE THE CHAIN" in panel, panel[:500]
    assert "4 walked · 3 have no link to walk" in panel, panel[:500]
    assert "no link recorded" in panel, "the legend does not name the unlinked head"

    tones = [tone for tone, _ in re.findall(r'class="rd-chain-box (is-[a-z]+)">([^<]*)<', panel)]
    assert "is-prechain" in tones, tones
    assert "is-break" not in tones, "the genesis boundary is drawn as a break"
    assert "is-unwalked" not in tones, (
        "the genesis head is drawn as not walked, which is a different claim: "
        "these entries have no link to walk, rather than a link nobody reached"
    )
    # And the break report is a broken-state element only.
    assert "FIRST BREAK" not in panel


def test_the_limitation_footer_appears_on_every_state_including_intact(client):
    """Tamper evidence, not protection from tampering.

    An operator with write access can rewrite every entry and every link and
    this will then report intact. That limitation is asserted by a test in this
    repo rather than admitted in a doc, so it belongs on screen where the
    verdict is read — on the passing state most of all, which is the one a
    reader is most likely to over-read.
    """
    import ledger

    states = {}

    _seed_chain(4)
    run = _published()
    states["intact"] = _chain_panel(_surfaces(client, run)["console"])

    # The verdict is cached for a short TTL, so changing the ledger mid-test
    # and reading again would serve the previous answer -- which is the
    # behaviour, not a flaw, and is why the age is on screen and why there is
    # a control that forces a walk. Here that control is stood in for.
    _seed_unchained(2)
    ledger.reset_ledger_chain_cache()
    states["genesis"] = _chain_panel(_surfaces(client, run)["console"])

    _break_chain(2)
    ledger.reset_ledger_chain_cache()
    states["broken"] = _chain_panel(_surfaces(client, run)["console"])

    assert "LINKS INTACT" in states["intact"]
    assert "PREDATE THE CHAIN" in states["genesis"]
    assert "LINK BROKEN AT 2" in states["broken"]

    for name, panel in states.items():
        # Flattened: the copy wraps in source, and where a line breaks is not
        # a difference a reader sees.
        text = _flat(panel)
        assert "WHAT THIS DOES NOT ESTABLISH" in text, (
            f"the {name} state drops the limitation footer"
        )
        assert "can rewrite every entry" in text, name
        assert "no external anchor" in text, name
        assert "not proof that any recorded work happened" in text, name
        assert "not protection from it" in text, name


def test_the_break_report_is_index_id_reason_and_two_digests(client):
    """Content-free by construction, so it is safe to paste into an issue."""
    _seed_chain(5)
    _break_chain(2)
    run = _published()
    panel = _chain_panel(_surfaces(client, run)["console"])

    assert "FIRST BREAK" in panel
    keys = re.findall(r'class="rd-chain-report-key">([^<]*)<', panel)
    assert keys == [
        "break_at_index",
        "break_entry_id",
        "reason",
        "expected_digest",
        "observed_digest",
    ], keys
    values = re.findall(r'class="rd-chain-report-value">([^<]*)<', panel)
    assert values[0] == "2", values
    assert values[1] == "contribution:0000000000000002", values
    assert "does not match its recorded digest" in values[2], values
    assert re.fullmatch(r"[0-9a-f]{64}", values[3]), values
    assert re.fullmatch(r"[0-9a-f]{64}", values[4]), values


def test_the_walk_age_is_on_screen_beside_the_verdict(client):
    """The panel says how old its answer is, because the answer is cached.

    A verdict with no age is a verdict a reader assumes is live. The walk that
    produced it is complete every time, and how long ago it ran is the part
    that varies, so it is drawn rather than implied.
    """
    _seed_chain(3)
    run = _published()
    panel = _chain_panel(_surfaces(client, run)["console"])
    assert re.search(r"walked (just now|\d+[smh] ago)", panel), panel[:400]
    assert "3 entries" in panel, panel[:400]
    # And the control that forces a complete fresh walk sits in the same box.
    assert "/v1/operator/ledger-chain?fresh=1" in panel


def test_the_chain_panel_is_absent_rather_than_empty_when_it_cannot_be_read(
    client, monkeypatch
):
    """A panel that cannot say anything says nothing, and costs nothing else."""
    import ledger

    def _explode(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: contributions")

    monkeypatch.setattr(ledger, "walk_ledger_chain", _explode)
    run = _published()
    detail = client.get(f"/history/{run}")
    assert detail.status_code == 200
    html = detail.json()["detail_html"]
    assert html, "a chain failure took the whole fragment with it"
    assert "LEDGER CHAIN" not in html
    assert "MANIFEST" in html and "PRODUCED BY" in html


def test_the_three_panels_stack_as_three_different_things(client):
    """The check the design asks for out loud: do they add up?

    They must not. The manifest is a chip because its state genuinely varies;
    the envelope is a sentence because presence says nothing and only contents
    do; the chain is a verdict about a different record entirely. One chip
    across all three, and nothing above them.
    """
    _seed_chain(4)
    run = _published()
    column = _surfaces(client, run)["console"]
    column = column[column.index('class="rd-side"'):]
    column = column[: column.index('class="rd-foot"')]

    chips = re.findall(r'class="rd-chip ([a-z-]+)"', column)
    assert chips == ["is-sealed"], (
        f"the side column carries {len(chips)} chips ({chips}). Two chips side "
        "by side is a row of ticks, and ticks get counted as one stronger claim."
    )
    # Three different grammatical classes: a chip, a sentence, a verdict line.
    assert 'class="rd-envelope-line"' in column
    assert 'class="rd-chain-verdict-text' in column
    # And the chain's verdict is not a fourth badge: no chip markup in it.
    panel = _chain_panel(column)
    assert "rd-chip" not in panel
    assert "rd-verdict" not in panel, (
        "the chain reuses the run's verdict-chip class, which would put it in "
        "the same visual vocabulary as the five run outcomes"
    )


def test_a_ledger_edited_after_the_walk_shows_the_age_of_the_walk_it_has(client):
    """The cache is bounded in time, and the panel says how bounded.

    A verdict served from cache is a complete walk that happened a moment ago,
    not a partial walk that happened now. So a ledger broken after that walk
    still reads intact until the TTL expires or someone forces a fresh one --
    and the reader is told the age rather than left to assume it is live.
    """
    import ledger

    _seed_chain(4)
    run = _published()
    first = _chain_panel(_surfaces(client, run)["console"])
    assert "LINKS INTACT" in first

    _break_chain(1)
    stale = _chain_panel(_surfaces(client, run)["console"])
    assert "LINKS INTACT" in stale, (
        "the walk re-ran inside its own TTL, which is not what the cache is for"
    )
    assert re.search(r"walked (just now|\d+[smh] ago)", stale), (
        "a cached verdict with no age on screen reads as a live one"
    )

    ledger.reset_ledger_chain_cache()
    fresh = _chain_panel(_surfaces(client, run)["console"])
    assert "LINK BROKEN AT 1" in fresh, (
        "a fresh walk did not find a break that is really there"
    )


# ── Two findings from opening it in a browser ────────────────────────
# Neither was visible in the markup or the test suite. Both were found by
# measuring the shipped theme in the console's run modal, and both are held
# here so the next edit cannot quietly undo them.


def _declarations(selector: str) -> str:
    """Every declaration block whose selector contains `selector`."""
    return "".join(
        block
        for head, block in re.findall(r"([^{}]*)\{([^}]*)\}", RUN_DETAIL_CSS)
        if selector in head
    )


def test_a_hollow_marker_takes_ink_for_its_edge_and_never_a_hairline_token():
    """A filled marker is a block of ink; a hollow one is its outline.

    So the edge is the entire signal, and a hairline token is not enough to
    carry it. Measured on the panel in the shipped theme, the chain's two
    hollow markers were first drawn with --border and --border-subtle and came
    out at 1.06:1 (dark) and 1.10:1 (light) against their own ground: not a
    faint marker, no marker at all — and "every state survives greyscale" is
    the rule that was silently failing.

    `.rd-marker.is-absent` had this right already by reaching for --text-faint,
    an ink token. This holds every hollow marker to the same rule.
    """
    hollow = re.findall(
        r"\.(rd-marker|rd-chain-marker)\.is-[a-z]+\s*\{([^}]*)\}", RUN_DETAIL_CSS
    )
    assert hollow, "no marker rules found at all"
    checked = 0
    for _, block in hollow:
        if "background: transparent" not in block:
            continue
        checked += 1
        edge = re.search(r"border-color:\s*var\((--[a-z0-9-]+)\)", block)
        assert edge, f"a hollow marker has no edge colour: {block.strip()!r}"
        assert edge.group(1).startswith("--text"), (
            f"a hollow marker takes its edge from {edge.group(1)}, a hairline "
            "token. A hollow marker's outline is the whole of the signal, and "
            "a hairline against the panel measures near 1:1 — the state stops "
            "surviving greyscale. Use an ink token, as .rd-marker.is-absent does."
        )
    assert checked >= 3, f"expected several hollow markers, checked {checked}"


def test_the_elision_cell_carries_no_marker_at_all(client):
    """It stands for entries that *were* walked and are not drawn.

    Giving it a marker would put a fourth thing into a vocabulary of three
    states, and an invisible one would be a state nobody can see. It was the
    second: a transparent square with a transparent edge, measuring 0:1.
    """
    _seed_chain(20)
    run = _published()
    panel = _chain_panel(_surfaces(client, run)["console"])

    cells = re.findall(
        r'<span class="rd-chain-cell">(.*?)</span>\s*</span>\s*</span>', panel, re.S
    )
    assert cells, "no chain cells rendered"
    elisions = [c for c in cells if "is-gap" in c]
    assert elisions, "a 20-entry chain drew every cell instead of eliding"
    for cell in elisions:
        assert "rd-chain-marker" not in cell, (
            "the elision cell carries a marker. It stands for entries that were "
            "walked and are not drawn, so a marker there is a fourth thing in a "
            "vocabulary of three states."
        )
    walked = [c for c in cells if "is-linked" in c]
    assert walked and all("rd-chain-marker" in c for c in walked), (
        "a drawn entry lost its marker, which is the signal that survives greyscale"
    )


def test_the_field_rows_stack_when_the_panel_is_too_narrow_for_three_columns():
    """A container query, because a viewport media query cannot see this.

    The side column is `flex: 1 1 300px`, so the envelope panel is about 300px
    wide on a 1280px screen — a media query would report 1280 and change
    nothing. Measured in the run modal at that width, the design's 172px field
    column left roughly 55px for the value and `3 accepted receipts` wrapped to
    one word per line.

    The design file previews this panel at 560px, where three columns are
    right. The surface it ships on is narrower, so the row has to stack there.
    """
    assert "container-type: inline-size" in _declarations(".rd-envelope-more"), (
        "the envelope disclosure is not a query container, so nothing can "
        "respond to the width it is actually given"
    )
    assert "container-type: inline-size" in _declarations(".rd-chain"), (
        "the chain panel is not a query container, so the break report cannot "
        "stack — and the break report is the thing an operator pastes into an "
        "issue"
    )
    containers = re.findall(r"@container\s*\(([^)]*)\)\s*\{(.*?)\n\}", RUN_DETAIL_CSS, re.S)
    assert containers, "no container query in the stylesheet"
    stacked = "".join(body for _, body in containers)
    assert ".rd-envelope-row" in stacked, "the field rows never stack"
    assert ".rd-chain-report-row" in stacked, "the break report never stacks"


def test_a_field_name_is_never_broken_across_lines():
    """In either arrangement.

    A name broken mid-token stops being a name you can grep the record for,
    which is most of what the opened envelope is for. Values are hashes and do
    wrap; names do not.
    """
    field = _declarations(".rd-envelope-field")
    assert field, "the field-name column has no rule"
    assert "overflow-wrap" not in field, (
        "the field name column was given overflow-wrap, so a long field name "
        "can now break mid-token"
    )
    value = _declarations(".rd-envelope-value")
    assert "overflow-wrap: anywhere" in value, (
        "values are hashes and must wrap, or they run off the panel"
    )


def test_a_break_and_a_genesis_head_can_both_be_on_screen(client):
    """They are not exclusive, and the legend has to name whatever is drawn.

    A ledger that predates the chain can also be broken. The head is drawn in
    either case, so a hollow pre-chain cell would otherwise sit beside a break
    with nothing saying which is which -- and those are exactly the two a
    reader must not conflate: one entry has no link to walk, the other has a
    link that did not match.
    """
    import ledger

    _seed_unchained(3)
    _seed_chain(6)
    _break_chain(3)
    ledger.reset_ledger_chain_cache()

    run = _published()
    panel = _chain_panel(_surfaces(client, run)["console"])

    assert "LINK BROKEN AT 3" in panel
    tones = {
        label: tone
        for tone, label in re.findall(r'class="rd-chain-box (is-[a-z]+)">([^<]*)<', panel)
    }
    assert tones.get("—") == "is-prechain", tones
    assert tones.get("03") == "is-break", tones

    legend = re.findall(r'class="rd-chain-key">.*?</span>\s*([^<]*)<', panel)
    legend = [item.strip() for item in legend]
    assert "break" in legend, legend
    assert "not walked" in legend, legend
    assert "no link recorded" in legend, (
        f"the strip draws a pre-chain head and the legend is {legend}, which "
        "does not name it"
    )


# -- the replay line (delta 8.11) -------------------------------------


def _replay(times: int) -> None:
    """Replay the published run's keyed submission `times` times, for real.

    The mapping row is written by the same call that would answer a later
    pitch, and the count is incremented by that call rather than by this
    helper, so what the panel reads is what a real replay leaves behind.
    """
    import sqlite3 as _sqlite3
    import uuid

    from execution.idempotency import submission_identity
    from execution.service import get_execution_service

    service = get_execution_service()
    request = ExecutionRequestV1(task=_result().task, strategy="dag", placement="auto")
    identity = submission_identity(
        request,
        idempotency_key="run-detail-replay-key",
        requester_scope_kind="pitch-key",
        requester_scope_value="run-detail-requester",
    )
    with _sqlite3.connect(service.store.path) as con:
        con.execute(
            """
            INSERT INTO execution_submissions (
                requester_scope_hash, idempotency_key_hash, request_hash,
                request_hash_version, execution_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                identity.requester_scope_hash,
                identity.idempotency_key_hash,
                identity.request_hash,
                identity.request_hash_version,
                EXECUTION,
                "2026-09-08T12:00:00+00:00",
            ),
        )
    for _ in range(times):
        record = service.store.create_or_replay_submission(
            request,
            identity,
            lambda: service._new_result(request, uuid.uuid4().hex, None, "queued"),
        )
        assert record.replayed, "the fixture did not take the replay branch"


def test_a_run_with_no_keyed_submission_draws_no_replay_line(client):
    """The absent case: this run was never pitched under a key at all.

    There is no mapping row, the route would answer 404, and the question the
    line answers does not arise. A sentence about idempotency here would be
    about a mechanism this submission never used.
    """
    run = _published()
    panel = _panel(_surfaces(client, run)["console"], "TIMELINE")

    # The attribute, not the bare class name: `dashboard.py` inlines
    # `_run_detail.css` into the page, so the selector that styles this line is
    # in the body of every run whether or not an element uses it.
    assert 'class="rd-tl-replay"' not in panel, "a run with no replay drew the line"
    assert "idempotency" not in _flat(panel).lower()


def test_a_keyed_submission_never_replayed_draws_no_replay_line(client):
    """The recorded-zero case, which is not the absent case.

    This run *was* pitched under an idempotency key and nothing has replayed
    it, so the record exists and says zero. Printing "never replayed" on it
    would put a line on almost every keyed run to report that nothing happened.

    Kept separate from the test above because the absent case never reaches the
    count: poisoning the zero guard left that one green, since there was no
    record to guard.
    """
    run = _published()
    _replay(0)
    panel = _panel(_surfaces(client, run)["console"], "TIMELINE")

    from execution.service import get_execution_service

    record = get_execution_service().store.submission_replays(EXECUTION)
    assert record is not None, "the fixture did not write a mapping row"
    assert record.replay_count == 0

    assert 'class="rd-tl-replay"' not in panel, "a recorded zero drew the line"
    assert "later pitch" not in _flat(panel)


def test_a_replayed_run_says_so_with_the_count_and_the_moment(client):
    run = _published()
    _replay(3)
    panel = _panel(_surfaces(client, run)["console"], "TIMELINE")
    text = _flat(panel)

    assert 'class="rd-tl-replay"' in panel, "the replay line is not drawn"
    assert "3 later pitches" in text, text[-400:]
    assert "under the same idempotency key" in text
    assert "No further execution was started" in text


def test_one_replay_is_not_described_in_the_plural(client):
    """Grammar is not decoration on a surface whose discipline is precision."""
    run = _published()
    _replay(1)
    text = _flat(_panel(_surfaces(client, run)["console"], "TIMELINE"))

    assert "One later pitch under the same idempotency key was answered" in text
    assert "pitches" not in text
    assert "the last of them" not in text, "one replay has no last of them"


def test_the_replay_line_carries_an_absolute_stamp_and_never_an_offset(client):
    """The two clocks are printed differently because they are different.

    Every row above the line is inside the run, and the offset gutter is sized
    for what the formatter prints across a run's own length. A replay has no
    such ceiling -- the same task pitched again next month is one row whose
    offset would overflow that column -- so the replay line is not a row and
    does not carry an offset.
    """
    run = _published()
    _replay(2)
    panel = _panel(_surfaces(client, run)["console"], "TIMELINE")
    line = panel[panel.index('class="rd-tl-replay"'):]

    assert "UTC" in _flat(line), "the replay moment is not an absolute stamp"
    assert "rd-tl-at" not in line, "the replay line took a seat in the offset grid"
    assert "rd-tl-row" not in line, "the replay line is a timeline row"


def test_the_replay_line_names_its_own_endpoint(client):
    """It does not borrow the panel head's label.

    The head says `GET /v1/executions/{id}`, and this is not from there. A
    reader who wants to check the number has to be told where it came from,
    and the two sources sit under different gates.
    """
    run = _published()
    _replay(2)
    panel = _panel(_surfaces(client, run)["console"], "TIMELINE")

    assert "GET /v1/operator/executions/{id}/submission" in _flat(panel)
    assert "GET /v1/executions/{id}" in _flat(panel), "the head lost its source"


def test_the_replay_line_never_reaches_the_shareable_page(client):
    """Placement follows the gate, and the gate is the operator prefix.

    `/v1/operator/*` is refused at the public edge, so the fact behind this
    line is not reachable from the Internet with a viewer key alone. The page
    that travels with a link does not draw it -- who re-pitched a task is not a
    fact about the deliverable somebody was handed.
    """
    run = _published()
    _replay(4)
    surfaces = _surfaces(client, run)

    assert 'class="rd-tl-replay"' in surfaces["console"], "the console lost the line"
    # The inlined stylesheet puts the selector in the served body either way,
    # which is why this reads the attribute and not the name.
    assert 'class="rd-tl-replay"' not in surfaces["server"], (
        "the shareable run page draws the replay line"
    )
    assert "4 later pitches" not in _flat(surfaces["server"])
    assert "idempotency" not in _flat(surfaces["server"]).lower()


def test_a_replay_with_no_recorded_moment_still_reports_the_count(client):
    """Degrade to the fact that is there, rather than to silence.

    A row migrated from before the counter existed could carry a count with no
    moment. The count is the load-bearing half; a sentence that drops it
    because the stamp is missing would lose the fact to protect the decoration.
    """
    panel = run_detail._timeline_panel({
        "timeline": [("+0.0s", "submission committed to disk", "is-dim")],
        "submission": {
            "execution_id": EXECUTION,
            "submitted_at": "2026-09-08T12:00:00+00:00",
            "replay_count": 2,
            "last_replayed_at": None,
        },
    })
    text = _flat(panel)

    assert "2 later pitches" in text
    assert "UTC" not in text, "a moment was invented for a row that has none"
    assert " on ." not in text and "on  ." not in text, "a dangling clause"


def test_the_replay_stamp_is_one_line(client):
    """Found by opening the page, not by a test.

    The sentence wraps, and the stamp inside it was breaking at its own hyphen
    -- the year and month above the day and time -- which reads as two numbers
    rather than one date. It is one value, so it is held on one line, and the
    span that does it is asserted in both the markup and the stylesheet
    because either half alone is inert.

    Measured in Chromium at 375px, the narrowest width the console is built
    for: the stamp is 112.9px inside a 262px column, so holding it together
    costs no overflow.
    """
    run = _published()
    _replay(2)
    panel = _panel(_surfaces(client, run)["console"], "TIMELINE")

    assert 'class="rd-tl-replay-at"' in panel, (
        "the replay stamp is no longer held on one line, so it can break at "
        "its own hyphen"
    )
    assert "nowrap" in _rule(RUN_DETAIL_CSS, ".rd-tl-replay-at"), (
        "the markup holds the stamp but the rule that makes it hold is gone"
    )


def test_the_replay_line_reads_against_the_surface_it_sits_on():
    """Contrast, measured in a browser and pinned to the token here.

    The line sits on `--surface-sunken`, not on the panel, and that is what
    makes the difference: the faint token clears 4.5:1 against the panel in the
    light theme and falls below it against the sunken surface, where every
    other faint use on this page does not sit. Measured in Chromium, light
    theme: the sentence and the endpoint both read 4.65:1 on the token below,
    against 4.13:1 for faint. Dark reads 5.99:1.

    So the endpoint is separated from the sentence by being mono and smaller
    rather than by being paler -- which is the page's rule anyway, that colour
    is never the only signal.
    """
    endpoint = _rule(RUN_DETAIL_CSS, ".rd-tl-replay-endpoint")
    assert "var(--text-muted)" in endpoint, (
        "the replay endpoint's colour changed; re-measure it against "
        "--surface-sunken in the light theme before accepting a fainter token"
    )
    assert "var(--text-faint)" not in endpoint, (
        "faint falls below 4.5:1 on the sunken surface in the light theme"
    )
    assert "var(--mono)" in endpoint, (
        "the endpoint lost the mono family that distinguishes it without colour"
    )
    # 11px is the floor for this project, and both halves sit on it or above.
    for selector, floor in (
        (".rd-tl-replay-endpoint", 11.0),
        (".rd-tl-replay-note", 11.0),
    ):
        size = re.search(r"font-size:\s*([\d.]+)px", _rule(RUN_DETAIL_CSS, selector))
        assert size and float(size.group(1)) >= floor, (
            f"{selector} prints below {floor}px"
        )
