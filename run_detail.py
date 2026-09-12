"""Run detail — one structure, rendered once, shown on three surfaces.

The dashboard's run modal, the console's `#view-run` and the server-rendered
`/run/{id}` are one structure in three shells. `docs/design/HANDOFF-DELTA.md`
and the archived handoff both say to port the structure rather than write a
parallel layout, so this module builds the markup exactly once and both
surfaces ask it for the same fragment. The console gets it as a string on
`/history/{timestamp}`; `/run/{id}` embeds it directly. There is no second
layout that can drift from the first.

Order is load-bearing: **the deliverable leads and the plan follows.** Plan is
process; the code is the product. The plan sits directly beneath it because the
waves are the parallelism claim. The old process-first order is not restored.

Two rules this file exists to hold, and the second is the one that is easiest
to break by accident:

1. **The three axes stay separate.** `lifecycle_status`, `validation_outcome`
   and `assurance_level` are read individually. The compatibility `status`
   field flattens *completed + passed* to `completed` and every other completed
   outcome to `unverified`, which destroys the distinction the strip exists to
   show, so `status` is never read here.

2. **The sealed manifest and the provenance envelope must never read as one
   claim.** They establish different things and vary independently. The
   manifest gets a chip, because its state genuinely varies (`sealed` /
   `legacy_live`); the envelope gets a sentence — no chip, no lamp, no colour
   token beyond text and border. There is no `INTEGRITY` header and no `TRUST`
   header over both, because a shared header is exactly what invites a reader
   to add up what sits beneath it. Two chips side by side is a row of ticks,
   and ticks get counted. `tests/test_run_detail.py` fails on a chip, a lamp or
   a colour class inside the envelope panel, and on any heading that spans the
   two panels.

Colour is never carried inline. Every element takes a class from
`templates/_run_detail.css`, which is where the tokens live, so
`tests/test_theme.py` can hold the no-hardcoded-colour rule over both the
stylesheet and this module.
"""

from __future__ import annotations

import html as _html
from datetime import datetime, timezone
from typing import Any

# Field names, not copy. The opened envelope renders the record's own keys, so
# they are imported from the module that declares the columns rather than
# written again here.
from provenance import (
    RESERVED_SLOT_FIELD,
    RESERVED_SLOT_VALUE,
    UNKNOWN_PRODUCER_SAMPLING,
    UNKNOWN_SAMPLING,
    UNKNOWN_SEED_HONOURED,
)

# The five-value verdict vocabulary, worst-wins. Mirrors `runVerdict` in
# `templates/_dashboard.js`, which renders the same five on the list cards;
# `tests/test_run_detail.py` holds the two implementations to the same ladder.
VERDICTS = {
    "PASS": ("PASS", "is-pass"),
    "NEEDS_WORK": ("NEEDS WORK", "is-needs-work"),
    "FAIL": ("FAIL", "is-fail"),
    "UNCHECKED": ("UNCHECKED", "is-unchecked"),
    "NO_VERDICT": ("NO VERDICT", "is-no-verdict"),
}
_RECORDED_RATINGS = ("PASS", "NEEDS_WORK", "FAIL")

# Manifest entries split into the two downloads, which are deliberately
# different scopes: the plain download hands over deliverables, and the run's
# own paperwork has to be asked for by name so a handoff never carries it by
# accident.
_AUDIT_ROLES = ("provenance", "log")


def run_verdict(rating: str | None, precheck_error: Any) -> str:
    """Five values, worst wins: FAIL → NEEDS WORK → NO VERDICT → UNCHECKED → PASS.

    A precheck error can never soften a named negative verdict, and PASS
    requires both halves of its claim — the reviewer passed it *and* the
    mechanical check found no defects. There is no path through this function
    that renders an unchecked run as PASS.
    """
    if rating == "FAIL":
        return "FAIL"
    if rating == "NEEDS_WORK":
        return "NEEDS_WORK"
    # No rating at all is the more fundamental absence, so it is reported ahead
    # of "the check did not reach a verdict".
    if rating not in _RECORDED_RATINGS:
        return "NO_VERDICT"
    if precheck_error:
        return "UNCHECKED"
    return "PASS"


def esc(value: Any) -> str:
    return _html.escape(str(value if value is not None else ""), quote=True)


def _bytes(size: Any) -> str:
    try:
        n = float(size)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return f"{int(n)} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _short_hash(value: Any, head: int = 8, tail: int = 4) -> str:
    text = str(value or "")
    if len(text) <= head + tail + 1:
        return text
    return f"{text[:head]}…{text[-tail:]}"


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _offset(start: datetime | None, moment: datetime | None) -> str:
    """`+1.4s` from the submission, or `+—` when nothing timestamps it."""
    if start is None or moment is None:
        return "+—"
    delta = (moment - start).total_seconds()
    if delta < 0:
        return "+—"
    if delta < 60:
        return f"+{delta:.1f}s"
    if delta < 3600:
        return f"+{int(delta // 60)}m {int(delta % 60):02d}s"
    return f"+{int(delta // 3600)}h {int(delta % 3600 // 60):02d}m"


def _stamp(value: Any) -> str:
    """An absolute UTC timestamp.

    The server-rendered page must never carry a relative age: it is a snapshot
    with no client, so "2h 14m ago" is a sentence that stops being true the
    moment it is written and has nothing to correct it.
    """
    moment = _parse_iso(value)
    if moment is None:
        # The legacy run-directory name, which is a UTC stamp of its own.
        try:
            moment = datetime.strptime(str(value), "%Y%m%d_%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except (TypeError, ValueError):
            return ""
    return moment.strftime("%Y-%m-%d %H:%M UTC")


# ── markers ──────────────────────────────────────────────────────────
# Filled versus hollow carries the meaning, so every state survives greyscale
# and a reader who does not see green. Colour is never the only signal.

def _marker(tone: str, size: str = "is-7") -> str:
    return f'<i class="rd-marker {size} {tone}" aria-hidden="true"></i>'


# ── header ───────────────────────────────────────────────────────────

def _hardware_badge(log: dict, durable: Any) -> str:
    """Degrades in words; never guesses.

    Placement and a count is all that is recorded for a finished run, so that
    is all this says. A distributed run with no node list says so rather than
    borrowing a plausible machine from `/nodes`.
    """
    mode = log.get("mode") or (
        getattr(durable, "placement_observed", None) if durable else None
    )
    nodes = log.get("nodes_used")
    if durable is not None and not nodes:
        nodes = len(getattr(durable, "participating_nodes", None) or [])
    if isinstance(nodes, list):
        nodes = len(nodes)
    if mode == "distributed":
        if nodes:
            return f"{nodes} machine{'s' if nodes != 1 else ''}"
        return "machine not recorded"
    if mode in ("local", "none", None):
        return "this machine"
    return "machine not recorded"


def _header(ctx: dict) -> str:
    log, durable, surface = ctx["log"], ctx["durable"], ctx["surface"]
    verdict = run_verdict(log.get("rating"), log.get("code_precheck_error"))
    label, cls = VERDICTS[verdict]

    # The console can say "2h 14m ago" because it polls and can correct itself.
    # The server-rendered page cannot, so it carries its own timestamp instead.
    age = (
        f'<span class="rd-age">{esc(ctx["relative_age"])}</span>'
        if surface == "console" and ctx["relative_age"]
        else f'<span class="rd-age">{esc(_stamp(log.get("timestamp")))}</span>'
    )

    def plural(count: int, word: str) -> str:
        return f"{count} {word}{'' if count == 1 else 's'}"

    meta = [ctx["execution_label"]]
    if log.get("project_id"):
        meta.append(f"project {log['project_id']}")
    meta.append(plural(ctx["metrics"]["deliverables"], "deliverable"))
    meta.append(plural(ctx["metrics"]["audit_records"], "audit record"))

    # The server-rendered page is one run and nothing else, so the run's own
    # title is its h1. In the console the surface sits inside a dialog that
    # already carries an h2, so it steps down rather than opening a second
    # document outline.
    heading = "h1" if surface == "server" else "h3"

    actions = "".join(
        f'<a class="rd-btn{" is-primary" if primary else ""}" href="{esc(href)}">{esc(text)}</a>'
        for text, href, primary in ctx["actions"]
    )

    return f"""
  <div class="rd-head">
    <div class="rd-head-main">
      <div class="rd-chips">
        <span class="rd-verdict {cls}">{_marker("", "is-6")}{esc(label)}</span>
        <span class="rd-hardware">{esc(_hardware_badge(log, durable))}</span>
        {age}
      </div>
      <{heading} class="rd-title">{esc(log.get("task") or "Untitled task")}</{heading}>
      <span class="rd-meta">{esc(" · ".join(meta))}</span>
    </div>
    <div class="rd-actions">{actions}</div>
  </div>"""


# ── the three axes ───────────────────────────────────────────────────

_LIFECYCLE_NOTES = {
    "completed": "It ran to a terminal state and that state was committed before anything was published.",
    "failed": "It reached a terminal state without producing an accepted result.",
    "cancelled": "Cancellation was requested and the terminal state records it.",
    "interrupted": "The coordinator restarted while this was in flight; the interruption is recorded rather than guessed at.",
    "running": "Still in flight at the moment this page was rendered.",
    "queued": "Accepted and committed, but not yet started.",
}


def _axis(label: str, value: str, note: str, tone: str) -> str:
    return f"""
      <div class="rd-axis">
        <div class="rd-axis-label">{esc(label)}</div>
        <div class="rd-axis-value">{_marker(tone)}<span>{esc(value)}</span></div>
        <div class="rd-axis-note">{esc(note)}</div>
      </div>"""


def _triad(ctx: dict) -> str:
    """Three axes, read separately. `status` is never consulted."""
    durable = ctx["durable"]
    if durable is None:
        why = (
            "This run predates the canonical execution record, or its record is "
            "no longer readable, so the three axes were never written down for it."
        )
        cells = "".join(
            _axis(label, "not recorded", why, "is-absent")
            for label in ("LIFECYCLE", "VALIDATION", "ASSURANCE")
        )
        return f'<div class="rd-triad">{cells}</div>'

    lifecycle = str(durable.lifecycle_status)
    validation = str(durable.validation_outcome)
    summary = durable.validation_summary

    lifecycle_tone = (
        "is-ok" if lifecycle == "completed"
        else "is-bad" if lifecycle in ("failed", "cancelled", "interrupted")
        else "is-neutral"
    )
    validation_tone = (
        "is-ok" if validation == "passed"
        else "is-bad" if validation == "failed"
        else "is-neutral"
    )
    validation_note = (
        f"{len(summary.checks_run)} checks run · {len(summary.checks_passed)} passed"
        f" · {len(summary.checks_failed)} failed"
        f" · {len(summary.checks_not_run)} not run. Mechanical checks only."
    )

    return f"""
    <div class="rd-triad">
      {_axis("LIFECYCLE", lifecycle,
             _LIFECYCLE_NOTES.get(lifecycle, "A terminal state this page has no note for."),
             lifecycle_tone)}
      {_axis("VALIDATION", validation, validation_note, validation_tone)}
      {_axis("ASSURANCE", str(durable.assurance_level),
             "Nothing here establishes the output is correct. A run can be "
             "complete, checked, and still wrong.", "is-neutral")}
    </div>"""


# ── placement ────────────────────────────────────────────────────────

def _placement(ctx: dict) -> str:
    durable, log = ctx["durable"], ctx["log"]
    asked = getattr(durable, "placement_requested", None) if durable else None
    planned = getattr(durable, "placement_planned", None) if durable else None
    ran = (
        getattr(durable, "placement_observed", None) if durable else None
    ) or log.get("mode")
    consent = bool(getattr(durable, "remote_dispatch_consent", False)) if durable else False

    def cell(word: str, value: Any, strong: bool = False) -> str:
        text = str(value) if value else "not recorded"
        cls = "rd-place-now" if strong else "rd-place-was"
        return f'<span>{esc(word)} <span class="{cls}">{esc(text)}</span></span>'

    consent_html = (
        f'<span class="rd-consent">{_marker("is-ok", "is-6")}consent recorded to leave this machine</span>'
        if consent
        else '<span class="rd-consent">'
        + _marker("is-absent", "is-6")
        + "no consent to leave this machine recorded</span>"
    )

    return f"""
    <div class="rd-placement">
      <span class="rd-place-label">PLACEMENT</span>
      <span class="rd-place-flow">
        {cell("asked for", asked)}<span class="rd-arrow">&#8594;</span>
        {cell("planned", planned)}<span class="rd-arrow">&#8594;</span>
        {cell("ran", ran, strong=True)}
      </span>
      {consent_html}
    </div>"""


# ── metric strip ─────────────────────────────────────────────────────

def _metrics(ctx: dict) -> str:
    """Counts only.

    No speed multiplier: computing one needs a serial baseline, and nothing
    records what this task would have taken on one machine. The per-run
    duration the execution record does carry is shown in the timeline, where it
    is a timestamp rather than a headline figure.
    """
    m = ctx["metrics"]
    cells = "".join(
        f'<div class="rd-metric"><div class="rd-metric-v">{esc(m[key])}</div>'
        f'<div class="rd-metric-l">{esc(label)}</div></div>'
        for key, label in (
            ("units", "UNITS"),
            ("waves", "WAVES"),
            ("deliverables", "DELIVERABLES"),
            ("audit_records", "AUDIT RECORDS"),
        )
    )
    return f'<div class="rd-metrics">{cells}</div>'


# ── waves ────────────────────────────────────────────────────────────

def units_in_waves(units: list[dict]) -> list[list[dict]]:
    """Group units by dependency depth.

    The waves *are* the task-level-parallelism claim: a wave of three units
    whose only dependency is unit 01 is the statement that three machines could
    have been offered work at the same time. Rendering `depends_on` as a column
    of IDs would state the same facts and make none of that visible, so the
    grouping is the point rather than a presentation choice.

    A dependency naming a unit that is not in the list, and a cycle, both
    resolve to depth 0 rather than raising: this is a rendering path for
    records that already exist, and it must not be the thing that fails on one.
    """
    by_id = {str(u.get("unit_id")): u for u in units}
    depth: dict[str, int] = {}

    def resolve(unit_id: str, seen: frozenset[str]) -> int:
        if unit_id in depth:
            return depth[unit_id]
        unit = by_id.get(unit_id)
        if unit is None or unit_id in seen:
            return 0
        deps = [str(d) for d in (unit.get("depends_on") or []) if str(d) in by_id]
        value = 0 if not deps else 1 + max(
            resolve(dep, seen | {unit_id}) for dep in deps
        )
        depth[unit_id] = value
        return value

    for unit in units:
        resolve(str(unit.get("unit_id")), frozenset())

    waves: list[list[dict]] = []
    for unit in units:
        index = depth.get(str(unit.get("unit_id")), 0)
        while len(waves) <= index:
            waves.append([])
        waves[index].append(unit)
    return [wave for wave in waves if wave]


def _wave_note(wave: list[dict], index: int) -> str:
    if index == 0:
        return "depends on nothing"
    dep_sets = {tuple(sorted(str(d) for d in (u.get("depends_on") or []))) for u in wave}
    if len(dep_sets) == 1:
        deps = ", ".join(_unit_label(d) for d in next(iter(dep_sets)))
        if len(wave) == 1:
            return f"depends only on {deps}"
        return f"all {len(wave)} depend only on {deps}"
    every = sorted({str(d) for u in wave for d in (u.get("depends_on") or [])})
    return "depends on " + ", ".join(_unit_label(d) for d in every)


def _unit_label(unit_id: Any) -> str:
    """`dag-1` reads as `01` on screen; anything else is shown as it is."""
    text = str(unit_id or "")
    if text.startswith("dag-") and text[4:].isdigit():
        return f"{int(text[4:]):02d}"
    return text


def _unit_card(unit: dict) -> str:
    status = str(unit.get("status") or "")
    tone = (
        "is-ok" if status == "completed"
        else "is-bad" if status in ("failed", "cancelled")
        else "is-neutral"
    )
    # Per-unit machine assignment. The archived handoff records this as
    # unserved (§8.2) and the design draws `machine not recorded` on every
    # card, but `ExecutionUnitSummaryV1.node_id` is populated from the accepted
    # receipt and survives the SQLite round trip, so for a distributed unit the
    # machine *is* recorded and saying otherwise would be the false statement.
    # It is read off the unit itself and never borrowed from `/nodes`.
    node = unit.get("node_id")
    if node:
        machine = str(node)
        machine_cls = "rd-unit-machine"
    elif str(unit.get("placement") or "") == "local":
        machine = "this machine"
        machine_cls = "rd-unit-machine"
    else:
        machine = "machine not recorded"
        machine_cls = "rd-unit-machine is-absent"

    # The marker is the only state signal on this card, and three of its four
    # tones are filled -- so in greyscale a failed unit and a completed one are
    # a 6px square 28 grey levels apart, which is not a distinction anyone
    # should have to make. Measured in the browser: is-ok 100, is-bad 72,
    # is-neutral 115 in light; 151 / 113 / 131 in dark.
    #
    # So a unit that did not complete says so in a word. `completed` is the
    # unmarked case and stays marker-only, because a word on every card is
    # noise that would make the one that matters harder to see. The design's
    # own sample cards are all completed, which is how this got past it.
    state_word = (
        f'<span class="rd-unit-state">{esc(status or "state not recorded")}</span>'
        if status != "completed"
        else ""
    )

    return f"""
              <div class="rd-unit">
                <div class="rd-unit-head">
                  <span class="rd-unit-id">{esc(_unit_label(unit.get("unit_id")))}</span>
                  <span class="rd-unit-title">{esc(unit.get("title") or "Untitled")}</span>
                  {_marker(tone, "is-6")}
                </div>
                <div class="rd-unit-prompt">{esc(unit.get("prompt") or "No unit prompt recorded.")}</div>
                <div class="rd-unit-foot">
                  <span class="{machine_cls}">{esc(machine)}</span>
                  {state_word}
                </div>
              </div>"""


def _waves_panel(ctx: dict) -> str:
    waves = ctx["waves"]
    if not waves:
        return """
      <section class="rd-panel">
        <div class="rd-panel-head"><span class="rd-panel-label">HOW IT WAS SPLIT</span></div>
        <div class="rd-panel-note">No decomposition was recorded for this run, so there is
          nothing to group into waves.</div>
      </section>"""

    blocks = ""
    for index, wave in enumerate(waves):
        cards = "".join(_unit_card(u) for u in wave)
        blocks += f"""
        <div class="rd-wave">
          <div class="rd-wave-bar">
            <span class="rd-wave-label">WAVE {index + 1}</span>
            <span class="rd-wave-note">{esc(_wave_note(wave, index))}</span>
          </div>
          <div class="rd-wave-units">{cards}</div>
        </div>"""

    unit_count = sum(len(w) for w in waves)
    return f"""
      <section class="rd-panel">
        <div class="rd-panel-head">
          <span class="rd-panel-label">HOW IT WAS SPLIT</span>
          <span class="rd-panel-meta">{unit_count} units · {len(waves)} waves · from depends_on</span>
        </div>
        {blocks}
        <div class="rd-panel-note">The waves are grouped by dependency depth from each unit's
          own <span class="rd-code">depends_on</span>, which is what makes the parallelism claim
          visible: a wave of several units with one shared dependency is a statement that they
          could be offered at the same time. No speed multiplier is shown, because nothing
          records what this task would have taken on one machine.</div>
      </section>"""


# ── deliverable ──────────────────────────────────────────────────────

def _deliverable_panel(ctx: dict) -> str:
    deliverables = ctx["deliverables"]
    preview = ctx["preview"]
    prose = ctx["prose"]

    meta = (
        f"{len(deliverables)} file{'s' if len(deliverables) != 1 else ''} · deliverable role"
        if deliverables
        else "no file carries the deliverable role"
    )

    if preview is None:
        body = """
        <div class="rd-panel-note">No deliverable file was recorded for this run. The
          extractor writes fenced code into real files when the output carries any; this
          run produced prose, or nothing it could safely write to disk.</div>"""
    else:
        checks = " · ".join(preview["checks"]) if preview["checks"] else ""
        body = f"""
        <div class="rd-file">
          <div class="rd-file-bar">
            <span class="rd-file-name">{esc(preview["name"])}</span>
            <span class="rd-file-meta">{esc(checks)}</span>
          </div>
          <pre class="rd-file-body">{esc(preview["text"])}</pre>
        </div>"""

    # The two defect channels, and never both: `execution/validators.py`
    # refuses to construct a ParsePrecheckResult carrying a runner failure and
    # a problem list at once, because a starved runner and a defect in the code
    # are separate facts. An empty problem list beside a precheck error would
    # otherwise read as "checked, clean", which is the one thing it does not
    # mean, so the precheck error is said first and alone.
    if ctx["precheck_error"]:
        defects = f"""
        <div class="rd-defects">The mechanical check did not run to a verdict on this run
          ({esc(ctx["precheck_error"])}), so these files are unchecked rather than known
          good.</div>"""
    elif ctx["problems"]:
        listed = "".join(
            f'<span class="rd-problem">{esc(p)}</span>' for p in ctx["problems"][:8]
        )
        defects = f"""
        <div class="rd-defects">The mechanical check flagged these, and they are published
          rather than hidden:</div>
        <div class="rd-problems">{listed}</div>"""
    else:
        defects = ""

    prose_html = f'<p class="rd-prose">{esc(prose)}</p>' if prose else ""
    return f"""
      <section class="rd-panel">
        <div class="rd-panel-head">
          <span class="rd-panel-label">DELIVERABLE</span>
          <span class="rd-panel-meta">{esc(meta)}</span>
        </div>
        {prose_html}
        {body}
        {defects}
      </section>"""


# ── manifest ─────────────────────────────────────────────────────────

def _manifest_panel(ctx: dict) -> str:
    """The manifest carries a chip, because its state genuinely varies.

    This is the panel that is allowed a state label. The envelope panel below
    it is not, and the two must not be given a heading that spans them.
    """
    manifest = ctx["manifest"]
    if manifest is None:
        return """
      <section class="rd-panel">
        <div class="rd-panel-head"><span class="rd-panel-label">MANIFEST</span></div>
        <div class="rd-panel-sunken">
          <div class="rd-panel-note">This run predates the artifact manifest, so there is no
            frozen file list to re-hash these bytes against. That is a fact about when it ran,
            not a finding about the files.</div>
        </div>
      </section>"""

    mode = str(manifest.integrity_mode)
    chip = (
        '<span class="rd-chip is-sealed">SEALED</span>' if mode == "sealed"
        else f'<span class="rd-chip is-plain">{esc(mode.replace("_", " ").upper())}</span>'
    )
    sealed_line = (
        f"manifest_hash sha256 {_short_hash(manifest.manifest_hash, 8, 6)}"
        if manifest.manifest_hash
        else "no manifest hash recorded"
    )

    groups = ""
    for label, endpoint, roles in (
        ("DELIVERABLE", ctx["download_href"], ("deliverable",)),
        ("AUDIT", ctx["audit_href"], _AUDIT_ROLES),
    ):
        # A group is named after the endpoint that serves it. No endpoint, no
        # group -- a list of files under a heading that cannot be fetched is
        # worse than no list.
        if not endpoint:
            continue
        rows = ""
        for entry in manifest.entries:
            if str(entry.role) not in roles:
                continue
            rows += f"""
            <div class="rd-artifact">
              <span class="rd-artifact-name">{esc(entry.relative_path)}</span>
              <span class="rd-artifact-size">{esc(_bytes(entry.size_bytes))}</span>
              <span class="rd-artifact-sub">
                <span class="rd-artifact-role">{esc(entry.role)}</span>
                <span class="rd-artifact-hash">sha256 {esc(_short_hash(entry.sha256, 4, 2))}</span>
              </span>
            </div>"""
        if not rows:
            continue
        groups += f"""
        <div>
          <div class="rd-group-bar">
            <span class="rd-group-label">{esc(label)}</span>
            <span class="rd-group-meta">GET {esc(endpoint)}</span>
          </div>
          {rows}
        </div>"""

    return f"""
      <section class="rd-panel">
        <div class="rd-panel-head">
          <span class="rd-panel-label">MANIFEST</span>
          {chip}
          <span class="rd-panel-meta">{manifest.file_count} files · {esc(_bytes(manifest.aggregate_size_bytes))}</span>
        </div>
        <div class="rd-panel-sunken">
          <div class="rd-hash">{esc(sealed_line)}</div>
          <div class="rd-panel-note">The file list, the sizes and the hashes were frozen when the
            run ended. Local evidence that these bytes are the bytes that were sealed — not a
            claim about what the files do, and not a time anyone else vouches for.</div>
        </div>
        {groups}
        <div class="rd-panel-note">Two lists because there are two endpoints. The plain download
          hands over deliverables; the run's own paperwork has to be asked for by name, so a
          handoff never carries it by accident.</div>
      </section>"""


# ── the provenance envelope ──────────────────────────────────────────

def envelope_sentence(payload: dict) -> str:
    """One sentence, because it is the only part most readers ever see.

    It has to survive being pasted with no page around it — that is what chose
    a sentence over a field list — so the limit travels in the same breath as
    the claim rather than in a tooltip.
    """
    producers = payload.get("producers") or []
    enrolled = sum(
        1 for p in producers
        if str(((p.get("identity") or {}).get("identity_class"))) == "enrolled"
    )
    models = sorted({
        str((p.get("model") or {}).get("name"))
        for p in producers
        if (p.get("model") or {}).get("name")
    })
    validators = payload.get("validators") or []

    if not producers:
        who = "Built on this coordinator, with no distributed producer recorded"
    elif enrolled == len(producers):
        who = f"Built by {len(producers)} enrolled machine{'s' if len(producers) != 1 else ''}"
    else:
        who = f"Built by {len(producers)} machine{'s' if len(producers) != 1 else ''}"

    if len(models) == 1:
        on = f" on {models[0]}"
    elif models:
        on = " on " + ", ".join(models)
    else:
        on = ", with no model name recorded,"

    if validators:
        checked = f", checked by {len(validators)} validator{'s' if len(validators) != 1 else ''}."
    else:
        checked = ", with no validator recorded."

    line = f"{who}{on}{checked}"
    if producers and enrolled != len(producers):
        missing = len(producers) - enrolled
        line += (
            f" {missing} of the {len(producers)} "
            f"{'has' if missing == 1 else 'have'} no enrolment recorded."
        )
    return line


def _envelope_panel(ctx: dict) -> str:
    """A sentence, never a chip.

    A coloured badge here would sit beside the manifest's SEALED chip and read
    as a second tick in a row of ticks, and a reader who counts ticks concludes
    something neither record says. A sentence cannot be added up. When there is
    no envelope the panel is absent — not an empty panel and not an error.

    The rule holds in both modes. Opened, the panel grows eight groups of field
    names and stays exactly as colourless: the disclosure changes how much is
    on screen, never what is being claimed.
    """
    envelope = ctx["envelope"]
    if envelope is None:
        return ""

    payload = dict(envelope.payload)
    unknown = payload.get("unknown_facts") or []
    digest = (
        f"envelope v{envelope.envelope_version} · "
        f"digest sha256 {_short_hash(envelope.envelope_digest, 8, 4)}"
    )
    count = (
        f"{len(unknown)} fact{'s' if len(unknown) != 1 else ''} not recorded"
        if unknown
        else "every fact this envelope carries is recorded"
    )

    # The disclosure, not a link out. Until this pass the only way to read an
    # envelope was to download the audit bundle and open the file inside it, so
    # "Open the envelope" pointed at a zip. It now opens the envelope, in
    # place, on both surfaces -- <details> needs no client, which is what lets
    # the server-rendered page carry it too.
    return f"""
      <section class="rd-panel rd-envelope">
        <div class="rd-panel-head is-sunken">
          <span class="rd-panel-label">PRODUCED BY</span>
          <span class="rd-panel-meta">{esc(digest)}</span>
        </div>
        <div class="rd-envelope-body">
          <div class="rd-envelope-line">{esc(envelope_sentence(payload))}</div>
          <div class="rd-envelope-limit">Binds who produced these files, under which enrolled
            identity, with which model, and which validators ran. It does not establish that the
            output is correct, useful, or honest.</div>
        </div>
        <details class="rd-envelope-more">
          <summary class="rd-envelope-foot">
            <span class="rd-envelope-open">Open the envelope</span>
            <span class="rd-envelope-unknown">{_marker("is-absent")}{esc(count)}</span>
          </summary>
          {_envelope_opened(payload, str(payload.get("execution_id") or ""))}
        </details>
      </section>"""


# ── the opened envelope ──────────────────────────────────────────────
# Eight groups of the record's own field names, and one rule governing all of
# them: **absence is a value, never a blank.** A recorded fact takes a filled
# marker and ordinary ink; a fact that was not written down takes a hollow
# marker, is named, and says why. Never a dash, and never --warn or --danger:
# not writing something down is neither a fault nor fine, and the shape carries
# it so it survives greyscale.
#
# Still no chip, no lamp and no state colour. Opening the envelope puts eight
# groups beside the manifest's SEALED chip, which is exactly when the rule in
# this module's docstring is most likely to break by accident.

_RECORDED = "is-neutral"
_ABSENT = "is-absent"


def _field(name: Any, value: Any, note: str = "", *, recorded: bool = True) -> dict:
    return {"name": str(name), "value": str(value), "note": note, "recorded": recorded}


def _absent(name: Any, value: Any, note: str = "") -> dict:
    return _field(name, value, note, recorded=False)


def _across(producers, read, name: str, note: str = "", absent_note: str = "") -> list[dict]:
    """One row when every producer agrees, one row each when they do not.

    Never an average, and never a winner. With several accepted receipts there
    is no rule for choosing between them — that is the whole reason the
    singular fields are left null — so a field whose value differs is shown
    once per producer with its index, and a field they all agree on is shown
    once.
    """
    values = [read(p) for p in producers]
    if not values:
        return []
    if len(set(values)) == 1:
        only = values[0]
        if only is None:
            return [_absent(name, "not recorded", absent_note)]
        return [_field(name, only, note)]
    rows: list[dict] = []
    for index, value in enumerate(values, 1):
        label = f"{index:02d} · {name}"
        first = index == 1
        if value is None:
            rows.append(_absent(label, "not recorded", absent_note if first else ""))
        else:
            rows.append(_field(label, value, note if first else ""))
    return rows


def _execution_group(payload: dict, producers: list) -> dict:
    count = len(producers)
    if count:
        rows = [
            _field(
                "producers",
                f"{count} accepted receipt{'s' if count != 1 else ''}",
                "Always a list. One producer is a list of one, because the "
                "ensemble path settles several accepted receipts and a layout "
                "built for the single case would need a rule for choosing "
                "between them.",
            )
        ]
    else:
        rows = [
            _absent(
                "producers",
                "0 accepted receipts",
                "Executed on this coordinator, so no distributed attempt "
                "produced these files. That is a fact about where it ran, and "
                "the coordinator's own identity does not stand in for a "
                "producer.",
            )
        ]

    if count > 1:
        why = (
            "Left null deliberately. With several accepted receipts there is "
            "no single attempt to name, and electing one would be a choice "
            "the record did not make."
        )
    else:
        why = (
            "There was no distributed attempt on this execution, so there is "
            "no attempt, receipt or unit to name."
        )
    for index, key in enumerate(("attempt_id", "receipt_id", "unit_id")):
        value = payload.get(key)
        if value:
            rows.append(_field(key, value))
        else:
            rows.append(_absent(key, "not applicable", why if index == 0 else ""))
    return {
        "label": "EXECUTION",
        "meta": str(payload.get("execution_id") or "no execution id recorded"),
        "rows": rows,
    }


def _identity_group(producers: list) -> dict:
    count = len(producers)
    label = f"PRODUCER IDENTITY · {count} ENTR{'Y' if count == 1 else 'IES'}"
    meta = "accepted_result_receipts"
    legacy_note = (
        "A session that predates enrolment. The label is display metadata and "
        "is never a trust key, so it names the machine without establishing "
        "who it is."
    )
    if not count:
        return {
            "label": label,
            "meta": meta,
            "rows": [
                _absent(
                    "producer_identity",
                    "not recorded",
                    "No accepted receipt carries an identity for this "
                    "execution, and the coordinator's own identity is not "
                    "written in as a substitute.",
                )
            ],
        }

    if count == 1:
        identity = producers[0].get("identity") or {}
        enrollment = identity.get("enrollment_id")
        label_value = identity.get("node_id")
        return {
            "label": label,
            "meta": meta,
            "rows": [
                _field(
                    "enrollment_id",
                    enrollment,
                    "Immutable. Distinct from the label and from the session.",
                )
                if enrollment
                else _absent("enrollment_id", "not recorded", legacy_note),
                _field(
                    "node_id",
                    label_value or "not recorded",
                    "The label as it was at settlement. Display metadata, "
                    "never a trust key.",
                    recorded=bool(label_value),
                ),
                _field(
                    "identity_class",
                    identity.get("identity_class") or "not recorded",
                    recorded=bool(identity.get("identity_class")),
                ),
            ],
        }

    rows = []
    for index, producer in enumerate(producers, 1):
        identity = producer.get("identity") or {}
        enrollment = identity.get("enrollment_id")
        value = " · ".join(
            str(part)
            for part in (
                enrollment or "not recorded",
                identity.get("node_id") or "no label recorded",
                identity.get("identity_class") or "unrecorded",
            )
        )
        name = f"{index:02d} · enrollment_id"
        rows.append(
            _field(name, value) if enrollment else _absent(name, value, legacy_note)
        )
    return {"label": label, "meta": meta, "rows": rows}


def _capability_group(producers: list) -> dict:
    if not producers:
        rows = [
            _absent(
                "capability_descriptor",
                "not recorded",
                "The descriptor is read from the immutable snapshot a producer "
                "was admitted under, and there is no producer here.",
            )
        ]
    else:
        rows = (
            _across(
                producers,
                lambda p: (p.get("capability") or {}).get("descriptor_version"),
                "descriptor_version",
            )
            + _across(
                producers,
                lambda p: (p.get("capability") or {}).get("descriptor_hash"),
                "descriptor_hash",
                "The immutable snapshot the claim was read from.",
                "No snapshot matched this receipt's enrolment and hash.",
            )
            + _across(
                producers, lambda p: (p.get("executor") or {}).get("kind"), "executor.kind"
            )
            + _across(
                producers,
                lambda p: (p.get("executor") or {}).get("version"),
                "executor.version",
                "",
                "The descriptor recorded no executor version.",
            )
            + _across(
                producers,
                lambda p: (p.get("executor") or {}).get("worker_protocol_version"),
                "worker_protocol_version",
                "",
                "The descriptor recorded no worker protocol version.",
            )
        )
    return {
        "label": "CAPABILITY AND EXECUTOR",
        "meta": "node_capability_snapshots",
        "rows": rows,
    }


def _model_group(producers: list) -> dict:
    if not producers:
        rows = [
            _absent(
                "model",
                "not recorded",
                "The model is read off an accepted receipt, and there is no "
                "receipt here.",
            )
        ]
    else:
        rows = (
            _across(
                producers, lambda p: (p.get("model") or {}).get("provider"), "model.provider"
            )
            + _across(producers, lambda p: (p.get("model") or {}).get("name"), "model.name")
            + _across(
                producers,
                lambda p: (p.get("model") or {}).get("digest"),
                "model.digest",
                "",
                "The receipt carried no digest, so which weights answered "
                "cannot be established from this record.",
            )
            + _across(
                producers,
                lambda p: (p.get("model") or {}).get("variant"),
                "model.variant",
                "",
                "Variant is a capability-evidence scope field resolved from "
                "the descriptor's model list. Reading it back here would be a "
                "guess.",
            )
        )
    return {"label": "MODEL", "meta": "as recorded on the receipt", "rows": rows}


def _validator_group(payload: dict) -> dict:
    validators = payload.get("validators") or []
    if not validators:
        return {
            "label": "VALIDATORS · IN ORDER",
            "meta": "none ran",
            "rows": [
                _absent(
                    "validators",
                    "not recorded",
                    "No validator is listed on this envelope, which is not the "
                    "same as a validator running and finding nothing.",
                )
            ],
        }
    rows = []
    for index, item in enumerate(validators):
        version = item.get("version")
        outcome = item.get("outcome") or "no outcome recorded"
        rows.append(
            _field(
                item.get("name") or "unnamed validator",
                f"v{version} · {outcome}" if version else str(outcome),
                "An outcome of passed means a mechanical check ran and did not "
                "fail. It is not a claim that the artifact does what its "
                "requester wanted."
                if index == 0
                else "",
            )
        )
    return {
        "label": "VALIDATORS · IN ORDER",
        "meta": f"{len(validators)} ran",
        "rows": rows,
    }


def _artifact_group(payload: dict) -> dict:
    artifacts = payload.get("artifacts") or {}
    digest = artifacts.get("manifest_digest")
    recorded_mode = artifacts.get("integrity_mode")
    mode = recorded_mode or "not recorded"
    count = artifacts.get("file_count")
    rows = [
        _field(
            "manifest_digest",
            digest,
            "The same digest the manifest panel shows. Recorded here so the "
            "envelope and the file list can be checked against each other "
            "offline.",
        )
        if digest
        else _absent(
            "manifest_digest",
            "not recorded",
            "This envelope was sealed over a file list with no manifest hash.",
        ),
        _field("integrity_mode", mode, recorded=bool(recorded_mode)),
        _field(
            "file_count",
            "not recorded" if count is None else count,
            "Every entry carries its own sha256, size and role.",
            recorded=count is not None,
        ),
    ]
    return {
        "label": "ARTIFACTS",
        "meta": f"{count} files · {mode}" if count is not None else str(mode),
        "rows": rows,
    }


def _sampling_group(payload: dict) -> dict:
    """Three absences that mean three different things, kept apart.

    Collapsing them to one word would be a fourth claim nobody made.
    `sampling_parameters` is nothing pinned at all; `sampling_seed_honoured` is
    a seed that was set and is not shown to have been applied; and
    `producer_sampling` is about a different machine entirely. Each is named
    with the record's own key, and each says which of the three it is.
    """
    sampling = payload.get("sampling") or {}
    unknown = set(payload.get("unknown_facts") or [])

    temperature = sampling.get("temperature")
    seed = sampling.get("seed")
    rows = [
        _field("temperature", temperature)
        if temperature is not None
        else _absent("temperature", "not pinned"),
        _field("seed", seed) if seed is not None else _absent("seed", "not pinned"),
    ]
    honouring = sampling.get("seed_honouring")
    if honouring:
        rows.append(_field("seed_honouring", honouring))
    scope = sampling.get("scope")
    if scope:
        rows.append(_field("scope", "coordinator configuration at sealing", str(scope)))

    if UNKNOWN_SAMPLING in unknown:
        rows.append(
            _absent(
                UNKNOWN_SAMPLING,
                "nothing pinned",
                "Neither a temperature nor a seed was fixed, so the shipping "
                "default applies and the runner chooses. This is the absence "
                "of a setting.",
            )
        )
    if UNKNOWN_SEED_HONOURED in unknown:
        rows.append(
            _absent(
                UNKNOWN_SEED_HONOURED,
                "not shown to have been applied",
                "A different absence from the one above: a seed was set, and "
                "whether the runner applied it for this model is assumed "
                "rather than checked, so the generator is not established as "
                "fixed.",
            )
        )
    if UNKNOWN_PRODUCER_SAMPLING in unknown:
        rows.append(
            _absent(
                UNKNOWN_PRODUCER_SAMPLING,
                "not carried back",
                "A third absence, and about a different machine: a "
                "distributed producer reads its own configuration and the "
                "worker protocol does not report it, so what that machine "
                "sampled with is not in this record.",
            )
        )
    return {"label": "SAMPLING", "meta": "coordinator scope", "rows": rows}


def _reserved_group() -> dict:
    """A slot, and described as nothing else.

    The field name is the record's own key, imported from the module that
    declares the column rather than written here: an opened envelope renders
    the record's field names, and reading one out of the record is the record
    speaking rather than this surface making a claim. The row exists to show
    that the slot is empty, and it appears here and nowhere else on any
    surface.
    """
    return {
        "label": "RESERVED",
        "meta": "not implemented",
        "rows": [
            _absent(
                RESERVED_SLOT_FIELD,
                RESERVED_SLOT_VALUE,
                "A slot, not a feature. Nothing fills it: no key, no key "
                "management, no transparency log, no third party. It is "
                "carried into the export so that filling it later would not "
                "be a schema break for anyone already reading these bundles.",
            )
        ],
    }


def envelope_groups(payload: dict) -> list[dict]:
    """The eight groups, in order, built from the record and nothing else."""
    producers = payload.get("producers") or []
    return [
        _execution_group(payload, producers),
        _identity_group(producers),
        _capability_group(producers),
        _model_group(producers),
        _validator_group(payload),
        _artifact_group(payload),
        _sampling_group(payload),
        _reserved_group(),
    ]


def _envelope_row(row: dict) -> str:
    note = (
        f'<span class="rd-envelope-note">{esc(row["note"])}</span>' if row["note"] else ""
    )
    return f"""
          <div class="rd-envelope-row">
            {_marker(_RECORDED if row["recorded"] else _ABSENT)}
            <span class="rd-envelope-field">{esc(row["name"])}</span>
            <span class="rd-envelope-value">{esc(row["value"])}</span>
            {note}
          </div>"""


def _envelope_opened(payload: dict, execution_id: str) -> str:
    """The disclosure's contents: eight groups, then what they are and are not."""
    groups = ""
    for group in envelope_groups(payload):
        rows = "".join(_envelope_row(row) for row in group["rows"])
        groups += f"""
        <div>
          <div class="rd-envelope-group">
            <span class="rd-envelope-group-label">{esc(group["label"])}</span>
            <span class="rd-envelope-group-meta">{esc(group["meta"])}</span>
          </div>
          {rows}
        </div>"""

    producers = payload.get("producers") or []
    if len(producers) > 1:
        closing = (
            f"{len(producers)} accepted receipts means {len(producers)} "
            "producers, not one producer with footnotes. The singular fields "
            "stay null rather than electing a winner, and single_producer is "
            "listed as a fact not recorded — which is what it is."
        )
    else:
        closing = (
            "One producer is a list of one. The panel is built for several "
            "because the ensemble path settles several accepted receipts, and "
            "a layout that assumed one would have to invent a rule for "
            "choosing between them."
        )
    served = (
        f"GET /v1/executions/{execution_id}/provenance"
        if execution_id
        else "the audit bundle"
    )
    return f"""{groups}
        <div class="rd-envelope-note-block">{esc(closing)}</div>
        <div class="rd-envelope-note-block">Served by <span
          class="rd-envelope-endpoint">{esc(served)}</span>, and recomputable offline from
          the audit bundle with no coordinator, no network and no credential.</div>"""


# ── the ledger chain ─────────────────────────────────────────────────
# **Intact is not green.** `--accent` means PASS, connected, ok, and an intact
# chain is none of those: it means no entry changed *without every link after
# it also being recomputed*, which a full rewrite satisfies. A tick here would
# be the exact class of claim this repo refuses, so the passing state is
# ordinary ink, a filled marker and a walked count.
#
# Two rules that are easy to get backwards, and both are drawn rather than
# described:
#
# * **Entries after a break are `not walked`, not broken.** Verification
#   returns at the first break, so nothing past it was checked. Drawing them
#   broken claims more than the walk found; drawing them intact claims the
#   opposite.
# * **The genesis boundary is not a break.** Entries written before the chain
#   existed have no link, are never retrofitted with one, and are counted
#   separately at the head.

_CHAIN_LINKED = "is-linked"
_CHAIN_PRECHAIN = "is-prechain"
_CHAIN_BREAK = "is-break"
_CHAIN_UNWALKED = "is-unwalked"


def _walk_age(seconds: Any) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "just now"
    if value < 1:
        return "just now"
    if value < 60:
        return f"{int(value)}s ago"
    if value < 3600:
        return f"{int(value // 60)}m ago"
    return f"{int(value // 3600)}h ago"


def _chain_cells(labels: list[str], tone: str, head: int = 3, tail: int = 3) -> list[dict]:
    """A bounded strip. A ledger of four hundred entries is not four hundred boxes.

    The elision is a drawing decision and never a walking one: every entry was
    read, and the cells that are not drawn are drawn as an ellipsis rather than
    quietly dropped.
    """
    if len(labels) <= head + tail + 1:
        return [{"n": label, "tone": tone} for label in labels]
    return (
        [{"n": label, "tone": tone} for label in labels[:head]]
        + [{"n": "…", "tone": "is-gap"}]
        + [{"n": label, "tone": tone} for label in labels[-tail:]]
    )


def chain_view(chain: dict) -> dict:
    """Everything the panel draws, resolved from one walk's verdict.

    Split out from the markup so the three states can be asserted as values
    rather than by matching strings in HTML.
    """
    ok = bool(chain.get("ok"))
    chained = int(chain.get("chained_entries") or 0)
    genesis = int(chain.get("genesis_unchained_entries") or 0)
    break_at = chain.get("break_at_index")

    if not ok:
        state = "broken"
    elif genesis:
        state = "genesis"
    else:
        state = "intact"

    cells: list[dict] = []
    if genesis:
        cells += _chain_cells(["—"] * genesis, _CHAIN_PRECHAIN)

    if state == "broken":
        index = int(break_at or 0)
        cells += _chain_cells([f"{i:02d}" for i in range(index)], _CHAIN_LINKED, 2, 2)
        cells += [{"n": f"{index:02d}", "tone": _CHAIN_BREAK}]
        # Everything past the first break was never read. It is drawn as
        # neither broken nor intact, because the walk found neither.
        cells += _chain_cells(
            [f"{i:02d}" for i in range(index + 1, chained)], _CHAIN_UNWALKED, 2, 2
        )
        verdict = f"LINK BROKEN AT {index}"
        walked = "walk stopped at the first break"
        legend = [
            ("link walked", _CHAIN_LINKED),
            ("break", _CHAIN_BREAK),
            ("not walked", _CHAIN_UNWALKED),
        ]
        # A break and a genesis head are not exclusive: a ledger that predates
        # the chain can also have one. The head is drawn either way, so when it
        # is there the legend has to name it -- an undrawn hollow cell beside a
        # break is exactly the pair a reader would otherwise conflate.
        if genesis:
            legend.append(("no link recorded", _CHAIN_PRECHAIN))
    else:
        cells += _chain_cells([f"{i:02d}" for i in range(chained)], _CHAIN_LINKED)
        walked = (
            f"{chained} walked · {genesis} have no link to walk"
            if genesis
            else f"{chained} walked · 0 unlinked"
        )
        if genesis:
            verdict = (
                f"LINKS INTACT · {genesis} "
                f"{'ENTRY PREDATES' if genesis == 1 else 'ENTRIES PREDATE'} THE CHAIN"
            )
            legend = [("link walked", _CHAIN_LINKED), ("no link recorded", _CHAIN_PRECHAIN)]
        else:
            verdict = "LINKS INTACT"
            legend = [("link walked", _CHAIN_LINKED), ("genesis boundary", _CHAIN_PRECHAIN)]

    report: list[tuple[str, str]] = []
    if state == "broken":
        for key in (
            "break_at_index",
            "break_entry_id",
            "reason",
            "expected_digest",
            "observed_digest",
        ):
            value = chain.get(key)
            report.append((key, "not recorded" if value in (None, "") else str(value)))

    return {
        "state": state,
        "verdict": verdict,
        "walked": walked,
        "cells": cells,
        "legend": legend,
        "report": report,
        "meta": (
            f"chain v{chain.get('chain_version') or '1'} · "
            f"{chained + genesis} entries · "
            f"walked {_walk_age(chain.get('walk_age_seconds'))}"
        ),
    }


def _chain_panel(ctx: dict) -> str:
    """A third thing, and deliberately not a fourth badge in a row of ticks.

    It renders in the console and nowhere else. The route behind it lives under
    `/v1/operator/`, which `deploy/Caddyfile.public` refuses at the edge, so a
    panel reading it belongs on an operator surface — not on the shareable
    `/run/{id}` page, which a viewer key alone can reach.
    """
    chain = ctx.get("chain")
    if not chain:
        return ""
    view = chain_view(chain)

    cells = ""
    for cell in view["cells"]:
        # An elision gets no marker element at all. It stands for entries that
        # were walked and are not drawn, so giving it a marker would put a
        # fourth thing in a vocabulary of three states.
        marker = (
            ""
            if cell["tone"] == "is-gap"
            else f'<span class="rd-chain-marker {cell["tone"]}"></span>'
        )
        cells += f"""
            <span class="rd-chain-cell">
              <span class="rd-chain-link {cell["tone"]}"></span>
              <span class="rd-chain-stack">
                <span class="rd-chain-box {cell["tone"]}">{esc(cell["n"])}</span>
                {marker}
              </span>
            </span>"""

    legend = ""
    for text, tone in view["legend"]:
        legend += (
            f'<span class="rd-chain-key"><span class="rd-chain-marker {tone}"></span>'
            f"{esc(text)}</span>"
        )

    report = ""
    if view["report"]:
        rows = "".join(
            f"""
          <div class="rd-chain-report-row">
            <span class="rd-chain-report-key">{esc(key)}</span>
            <span class="rd-chain-report-value">{esc(value)}</span>
          </div>"""
            for key, value in view["report"]
        )
        report = f"""
        <div class="rd-chain-report">
          <div class="rd-chain-report-head">FIRST BREAK</div>
          {rows}
          <div class="rd-chain-report-note">Investigate before trusting any standings computed
            from this ledger. A break means an entry changed after it was written — disk
            corruption, a partial restore, or an edit. Which of those, this cannot tell you.</div>
        </div>"""

    return f"""
      <section class="rd-panel rd-chain">
        <div class="rd-panel-head is-sunken">
          <span class="rd-panel-label">LEDGER CHAIN</span>
          <span class="rd-panel-meta">{esc(view["meta"])}</span>
        </div>
        <div class="rd-chain-verdict">
          <span class="rd-chain-verdict-main">
            <span class="rd-chain-verdict-marker {_CHAIN_BREAK if view["state"] == "broken" else _CHAIN_LINKED}"></span>
            <span class="rd-chain-verdict-text is-{esc(view["state"])}">{esc(view["verdict"])}</span>
          </span>
          <span class="rd-chain-walked">{esc(view["walked"])}</span>
        </div>
        <div class="rd-chain-strip">
          <div class="rd-chain-track">{cells}</div>
          <div class="rd-chain-legend">{legend}</div>
        </div>
        {report}
        <div class="rd-chain-limit">
          <div class="rd-chain-limit-label">WHAT THIS DOES NOT ESTABLISH</div>
          <p class="rd-chain-limit-note">Evidence of tampering, not protection from it. An
            operator with write access to this database can rewrite every entry <em>and</em>
            every link, and this will then report intact. There is no consensus here, no
            external anchor, and nobody outside this machine attesting to anything. A walked
            chain is not proof that any recorded work happened, was correct, or is owed
            anything.</p>
          <a class="rd-chain-refresh" href="/v1/operator/ledger-chain?fresh=1">Walk it again now</a>
        </div>
      </section>"""


# ── timeline ─────────────────────────────────────────────────────────

def _timeline_panel(ctx: dict) -> str:
    """Rendered from the execution record, and from nothing else.

    `full_log.json` ships inside the audit bundle, and that bundle is
    deliberately a separate scope that has to be asked for by name, so fetching
    it to draw a panel would quietly undo the separation the two downloads
    exist to keep -- on every page view, for a reader who clicked nothing. So
    the panel draws only what `GET /v1/executions/{id}` serves.

    That is now four run-level moments and one row per unit, because
    `ExecutionUnitSummaryV1` carries the two ends of the interval it already
    measured. A record written before it did keeps the named absence instead.

    One thing below the rows comes from elsewhere, and says so rather than
    borrowing the head's label: the replay line is a fact about the submission,
    served under a stricter gate, and printed on a different clock.
    """
    rows = ctx["timeline"]
    if not rows:
        return ""
    body = "".join(
        f'<div class="rd-tl-row"><span class="rd-tl-at">{esc(at)}</span>'
        f'<span class="rd-tl-what {cls}">{esc(what)}</span></div>'
        for at, what, cls in rows
    )
    return f"""
      <section class="rd-panel">
        <div class="rd-panel-head">
          <span class="rd-panel-label">TIMELINE</span>
          <span class="rd-panel-meta">GET /v1/executions/{{id}}</span>
        </div>
        {body}{_replay_line(ctx)}
      </section>"""


_UNIT_STATE = {
    "completed": "",
    "failed": " and failed",
    "cancelled": " and was cancelled",
}


def _unit_rows(
    start: datetime | None, units: list[dict]
) -> list[tuple[str, str, str]]:
    """One row per unit, off the two ends of the interval the record measures.

    Nothing here is derived: a unit with only one of the two timestamps is not
    completed by adding its duration to the end it has, because a row invented
    that way is indistinguishable on screen from one that was recorded.

    No unit row carries a state colour. A unit that did not complete says the
    word, the same rule the unit cards follow -- a timeline where the only
    difference between a finished unit and a failed one is a grey level is a
    distinction nobody should have to make.
    """
    timed: list[tuple[datetime, datetime, dict]] = []
    untimed = 0
    for unit in units:
        began = _parse_iso(unit.get("started_at"))
        ended = _parse_iso(unit.get("completed_at"))
        # One end is not an interval. A row drawn from it would sit on the
        # timeline looking like every other row and mean something weaker, so
        # the unit is counted in the absence below instead.
        if began is None or ended is None:
            untimed += 1
            continue
        timed.append((began, ended, unit))

    timed.sort(key=lambda item: (item[0], _unit_label(item[2].get("unit_id"))))
    rows: list[tuple[str, str, str]] = []
    for began, ended, unit in timed:
        status = str(unit.get("status") or "")
        tail = _UNIT_STATE.get(status, f", recorded as {status or 'no state'}")
        label = _unit_label(unit.get("unit_id"))
        rows.append((
            _offset(start, began),
            f"unit {label} ran to {_offset(start, ended)}{tail}",
            "is-dim",
        ))

    if untimed:
        # The absence this panel has always named. A record written before the
        # unit summary carried these has no per-unit moments, and a timeline
        # that simply omitted its units would read as a run whose units took no
        # time rather than one that did not write them down. A record that has
        # them for some units says how many it is missing, because "not
        # recorded" over a panel that just drew three of them is the sentence a
        # reader would read as applying to all of them.
        scope = (
            "per-unit start and finish times are not recorded on this execution "
            "record; only each unit's own duration is"
            if not timed
            else f"{untimed} of {len(units)} units did not record both ends of "
            "their interval; only their durations are on this record"
        )
        rows.append(("+—", scope, "is-absent"))
    return rows


def _timeline_rows(
    durable: Any, manifest: Any, units: list[dict]
) -> list[tuple[str, str, str]]:
    if durable is None:
        return [(
            "+—",
            "No execution record for this run, so nothing here carries a timestamp.",
            "is-absent",
        )]
    start = _parse_iso(durable.created_at)
    rows: list[tuple[str, str, str]] = [
        ("+0.0s", "submission committed to disk", "is-dim"),
    ]
    if durable.started_at:
        rows.append((_offset(start, _parse_iso(durable.started_at)), "execution started", "is-info"))
    rows.extend(_unit_rows(start, units))
    if durable.completed_at:
        rows.append((
            _offset(start, _parse_iso(durable.completed_at)),
            "terminal state committed",
            "is-dim",
        ))
    sealed_at = getattr(manifest, "sealed_at", None) if manifest is not None else None
    if sealed_at:
        rows.append((_offset(start, _parse_iso(sealed_at)), "manifest sealed", "is-ok"))
    return rows


# ── replay ───────────────────────────────────────────────────────────

def _replay_line(ctx: dict) -> str:
    """The line for a run that was returned rather than re-run (delta 8.11).

    Drawn from the durable submission mapping and from nothing else. The POST
    response's `replayed` is not a source: it is gone the moment the response
    is read, and reading the fact off a plausible-looking log key instead would
    render a line that is always absent, look like it worked, and start lying
    the moment someone wrote that key for another reason.

    Nor does it come off the execution. Terminal state is monotonic under ADR
    0009 and a replay can arrive long after the run reached it, so the fact
    lives beside the execution rather than on it -- the shape the provenance
    envelope already uses.

    **An absolute stamp, not an offset.** Every other row on this panel is
    inside the run, so the offset gutter is sized for what the formatter can
    print across a run's own length. A replay is not bounded that way: the same
    task pitched again next month is one row whose offset would overflow the
    column, and widening the gutter for a value that has no ceiling is not a
    fix. The two clocks are different, so they are printed differently.

    Nothing is drawn when the count is zero, and nothing when the run carries
    no mapping at all. A zero is a recorded fact, but printing "never replayed"
    on every run says nothing a reader needed; an absent mapping is a run that
    was never pitched under a key, where the question does not arise.
    """
    record = ctx.get("submission")
    if not record:
        return ""
    count = int(record.get("replay_count") or 0)
    if count < 1:
        return ""

    moment = _stamp(record.get("last_replayed_at"))
    # The stamp is one value and is kept on one line. Opening the page found it
    # breaking at its own hyphen -- "2026-11-" above "14 22:05 UTC" -- which
    # reads as two numbers rather than one date, and a reader checking a run
    # against a clock should not have to reassemble it.
    when = ""
    if moment:
        lead = ", the last of them on " if count > 1 else ", on "
        when = f'{lead}<span class="rd-tl-replay-at">{esc(moment)}</span>'
    pitches = "one later pitch" if count == 1 else f"{count} later pitches"
    was = "was" if count == 1 else "were"
    return f"""
        <div class="rd-tl-replay">
          {_marker("is-slate", "is-6")}
          <div>
            <p class="rd-tl-replay-note">{esc(pitches.capitalize())} under the same
              idempotency key {was} answered with this run{when}. No further
              execution was started, which is the whole of what this records.</p>
            <span class="rd-tl-replay-endpoint">GET /v1/operator/executions/{{id}}/submission</span>
          </div>
        </div>"""

# ── assembly ─────────────────────────────────────────────────────────

_FOOTER = {
    "server": (
        "WHERE THIS PAGE MUST DIFFER",
        "Same structure as the console, three differences forced by the medium. There is no "
        "client, so nothing polls: this is a snapshot, and it carries its own timestamp rather "
        "than an age that stops being true. It is shareable, so the one-line summary above has "
        "to survive being pasted with no page around it. And it is viewer-gated, so it is "
        "indexed by nobody — a permalink for a person who was given the link, not a public "
        "record.",
    ),
    "console": (
        "THE SAME STRUCTURE, THREE TIMES",
        "This panel, the console view and the server-rendered page at /run/{id} are one "
        "structure in three shells, built by one renderer so they cannot drift apart. The "
        "server page differs only where the medium forces it: nothing polls there, so it "
        "carries a timestamp rather than an age, and its controls are sized for a phone.",
    ),
}


def build_view(
    log: dict,
    *,
    publication: Any = None,
    durable: Any = None,
    envelope: Any = None,
    chain: dict | None = None,
    submission: dict | None = None,
    surface: str = "console",
    run_id: str = "",
    relative_age: str = "",
    preview: dict | None = None,
    prose: str = "",
) -> dict:
    """Everything the surface renders, resolved once from the served records."""
    manifest = getattr(publication, "manifest", None) if publication is not None else None
    execution_id = (
        getattr(publication, "execution_id", None) if publication is not None else None
    ) or log.get("execution_id")

    if durable is not None and durable.execution_units:
        units = [
            {
                "unit_id": u.unit_id,
                "title": u.title,
                "prompt": "",
                "depends_on": list(u.depends_on),
                "status": u.status,
                "placement": u.placement,
                "node_id": u.node_id,
                "started_at": u.started_at,
                "completed_at": u.completed_at,
            }
            for u in durable.execution_units
        ]
        # The unit prompt is the planner's description, which lives on the
        # legacy plan rather than on the unit summary. Matched by id so a
        # record missing one still renders the other.
        by_label = {_unit_label(u["unit_id"]): u for u in units}
        for step in log.get("plan") or []:
            target = by_label.get(_unit_label(f"dag-{step.get('id')}"))
            if target is not None:
                target["prompt"] = step.get("description") or ""
    else:
        units = [
            {
                "unit_id": f"dag-{step.get('id')}",
                "title": step.get("title") or "Untitled",
                "prompt": step.get("description") or "",
                "depends_on": [f"dag-{d}" for d in (step.get("depends_on") or [])],
                "status": "completed",
                "placement": log.get("mode"),
                "node_id": None,
                # A legacy plan step is not an execution unit and was never
                # timestamped. The timeline names that rather than guessing.
                "started_at": None,
                "completed_at": None,
            }
            for step in (log.get("plan") or [])
        ]

    waves = units_in_waves(units)

    if manifest is not None:
        deliverables = [e for e in manifest.entries if str(e.role) == "deliverable"]
        audit_records = [e for e in manifest.entries if str(e.role) in _AUDIT_ROLES]
    else:
        deliverables = list(log.get("code_files") or [])
        audit_records = []

    download_href = (
        f"/v1/executions/{execution_id}/download" if execution_id
        else f"/history/{run_id}/download"
    )
    # None when there is no execution record: the legacy download is one bundle
    # rather than two scopes, so offering "Audit bundle" beside "Download
    # deliverables" would point both at the same URL and imply a split that
    # this run does not have.
    audit_href = (
        f"/v1/executions/{execution_id}/audit-download" if execution_id else None
    )

    # 44px on the server-rendered page, because most visitors arrive on a phone
    # from a link in a post; 28px in the console, where the pointer is a mouse
    # and vertical space is the scarce thing. The classes carry the height.
    if surface == "server":
        actions = [
            ("Fork this run", f"/history/{run_id}/fork-template", True),
            ("Download deliverables", download_href, False),
            ("See what else was built", "/dashboard#gallery", False),
        ]
    else:
        actions = [("Download deliverables", download_href, True)]
        if audit_href:
            actions.append(("Audit bundle", audit_href, False))
        actions.append(("Open run page ↗", f"/run/{run_id}", False))

    return {
        "log": log,
        "durable": durable,
        "envelope": envelope,
        # The chain is global rather than per-run, and its route is
        # operator-gated, so only the console passes one. The server-rendered
        # page leaves it None and the panel does not render there.
        "chain": chain,
        "manifest": manifest,
        "surface": surface,
        "run_id": run_id,
        "relative_age": relative_age,
        # The id, whole. It is the thing someone pastes into an issue, so it is
        # never shortened -- and never prefixed with a word it already starts
        # with, which read as "exec exec_9f4c...".
        "execution_label": (
            str(execution_id)
            if execution_id
            else f"run {run_id}"
        ),
        "waves": waves,
        "deliverables": deliverables,
        "preview": preview,
        "prose": prose,
        "download_href": download_href,
        "audit_href": audit_href,
        "actions": actions,
        "problems": [
            p if isinstance(p, str) else str(p)
            for p in (log.get("code_problems") or [])
        ],
        "precheck_error": log.get("code_precheck_error"),
        "timeline": _timeline_rows(durable, manifest, units),
        # Absent on the shareable page by design, not by omission: the route
        # that serves it is refused at the public edge, and the panel follows
        # its gate. `routes_run.py` never passes it.
        "submission": submission,
        "metrics": {
            "units": sum(len(w) for w in waves),
            "waves": len(waves),
            "deliverables": len(deliverables),
            "audit_records": len(audit_records),
        },
    }


def render(ctx: dict) -> str:
    """The whole surface, deliverable first."""
    label, body = _FOOTER.get(ctx["surface"], _FOOTER["console"])
    return f"""<div class="rd" id="view-run" data-surface="{esc(ctx["surface"])}">
  {_header(ctx)}
  {_triad(ctx)}
  {_placement(ctx)}
  {_metrics(ctx)}
  <div class="rd-body">
    <div class="rd-main">
      {_deliverable_panel(ctx)}
      {_waves_panel(ctx)}
    </div>
    <div class="rd-side">
      {_manifest_panel(ctx)}
      {_envelope_panel(ctx)}
      {_chain_panel(ctx)}
      {_timeline_panel(ctx)}
    </div>
  </div>
  <div class="rd-foot">
    {_marker("is-slate")}
    <div>
      <div class="rd-foot-label">{esc(label)}</div>
      <p class="rd-foot-note">{esc(body)}</p>
    </div>
  </div>
</div>"""
