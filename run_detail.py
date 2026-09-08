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
    return f"+{int(delta // 60)}m {int(delta % 60):02d}s"


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

    return f"""
              <div class="rd-unit">
                <div class="rd-unit-head">
                  <span class="rd-unit-id">{esc(_unit_label(unit.get("unit_id")))}</span>
                  <span class="rd-unit-title">{esc(unit.get("title") or "Untitled")}</span>
                  {_marker(tone, "is-6")}
                </div>
                <div class="rd-unit-prompt">{esc(unit.get("prompt") or "No unit prompt recorded.")}</div>
                <div class="{machine_cls}">{esc(machine)}</div>
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
        <div class="rd-envelope-foot">
          <a class="rd-envelope-open" href="{esc(ctx["audit_href"])}">Open the envelope</a>
          <span class="rd-envelope-unknown">{_marker("is-absent")}{esc(count)}</span>
        </div>
      </section>"""


# ── timeline ─────────────────────────────────────────────────────────

def _timeline_panel(ctx: dict) -> str:
    """Rendered from the execution record, and from nothing else.

    Per-unit start and finish times live in `full_log.json`, which ships inside
    the audit bundle. The bundle is deliberately a separate scope that has to
    be asked for by name, so fetching it to draw a panel would quietly undo the
    separation the two downloads exist to keep. The rows the execution record
    does timestamp are drawn; the rest render `+—` and say why.
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
        {body}
      </section>"""


def _timeline_rows(durable: Any, manifest: Any) -> list[tuple[str, str, str]]:
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
    rows.append((
        "+—",
        "per-unit start and finish times are not timestamped on the execution "
        "record; only each unit's own duration is",
        "is-absent",
    ))
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

def _replay(ctx: dict) -> str:
    if not ctx["replayed"]:
        return ""
    return f"""
    <div class="rd-replay">
      {_marker("is-info")}
      <span>Returned, not re-run: this task was pitched again under the same idempotency key,
        so nothing was built twice. The same key with a changed task is refused instead.</span>
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
    audit_href = (
        f"/v1/executions/{execution_id}/audit-download" if execution_id
        else f"/history/{run_id}/download"
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
        actions = [
            ("Download deliverables", download_href, True),
            ("Audit bundle", audit_href, False),
            ("Open run page ↗", f"/run/{run_id}", False),
        ]

    return {
        "log": log,
        "durable": durable,
        "envelope": envelope,
        "manifest": manifest,
        "surface": surface,
        "run_id": run_id,
        "relative_age": relative_age,
        "execution_label": f"exec {execution_id}" if execution_id else f"run {run_id}",
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
        "replayed": bool((log.get("idempotency") or {}).get("replayed")),
        "timeline": _timeline_rows(durable, manifest),
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
  {_replay(ctx)}
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
