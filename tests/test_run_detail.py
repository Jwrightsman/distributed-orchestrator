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


def test_the_timeline_is_drawn_from_the_execution_record(client):
    run = _published()
    for surface, html in _surfaces(client, run).items():
        panel = _panel(html, "TIMELINE")
        assert "GET /v1/executions/{id}" in panel, f"{surface}: the source is not named"
        assert "+0.0s" in panel and "submission committed to disk" in panel
        assert "terminal state committed" in panel
        assert "+—" in panel, (
            f"{surface}: what is not timestamped has to say so rather than be dropped"
        )


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


def test_no_surface_uses_a_prohibited_word(client):
    """The words are prohibited as claims, so this reads the rendered text.

    `unverified` is the assurance level's own value and is the opposite claim,
    so the check is on whole words.
    """
    run = _published()
    for surface, html in _surfaces(client, run).items():
        text = re.sub(r"<[^>]+>", " ", html).lower()
        for word in BANNED:
            assert not re.search(rf"(?<![a-z-]){re.escape(word)}(?![a-z])", text), (
                f"{surface}: renders the prohibited word {word!r}"
            )


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
    """`replayed` is a property of the POST response, not of the run.

    It is `SubmittedExecution.replayed` -- the `Idempotency-Replayed` header --
    and it never reaches `ExecutionResultV1`, which has no idempotency field at
    all. Reading it off a plausible-looking log key would render a line that is
    always absent, look like it worked, and start lying the moment someone
    wrote that key for another reason. See HANDOFF-DELTA §8.11.
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
        "ExecutionResultV1 now carries a replay field -- the line can be drawn, "
        "and HANDOFF-DELTA §8.11 should be closed"
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
