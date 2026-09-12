"""The Evals view — what the eval harness recorded, and what it can resolve.

`docs/design/HANDOFF-DELTA.md` §9 is the design this module implements, and
§4.1 is the reason it needed one. The archived console built this view around a
single quality figure; that figure is retired, because the check under it was
weaker than the claim it carried and two runs with nothing changed between them
disagree on most of the tasks. So the view shows the record instead, and says
what the instrument can and cannot tell apart.

Rules this file holds, each of them easy to undo by accident:

1. **Nothing is totalled.** No row total, no column total, no pass rate. A
   column total over these tasks is a pass rate with the percent sign removed.
   The strip is three counts about the *instrument*, never about the output.

2. **No percent sign renders at all.** Interval bounds print as proportions and
   power as "four runs in five". A rule against the character needs no list of
   exceptions, which is why it is the rule and not a narrower one.

3. **The pass decision is the harness's own.** `evals/scoring.py::is_success`
   with the judge gate off, called, never re-implemented here, so this view and
   the harness cannot disagree about what passed. This module only refines the
   two sides of that answer: a pass whose run check was `browser_ok` says
   `loaded`, and a non-pass with an undecidable field says `ungraded` rather
   than `fail`.

4. **A paired result is printed the way `stats.render_paired` prints one.**
   Table, n, discordant count with its interval, then p, then the threshold.
   Never p first and never p alone.

5. **Absent is not empty.** The Docker image carries neither `evals/` nor its
   results. A server without the record says it has none; a checkout whose
   harness has never recorded a run says that instead.

Colour is never carried inline. Panel primitives come from
`templates/_run_detail.css`, which the console already injects, and the parts
specific to this view from `templates/_dashboard.css`, so `tests/test_theme.py`
holds the no-hardcoded-colour rule over this module too.
"""

from __future__ import annotations

import calendar
import html as _html
import json
import math
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(__file__).resolve().parent / "evals" / "results"

# The cell vocabulary. Words, because colour is never the only signal.
PASS = "pass"
LOADED = "loaded"
FAIL = "fail"
UNGRADED = "ungraded"
NOT_IN_RUN = "not in run"

PASSED = (PASS, LOADED)

# The stages `is_success` requires, in the order the harness runs them. Read
# only to say *why* something did not pass — the pass decision itself is never
# made from this tuple.
GRADE_STAGES = ("extracted", "parses", "executes", "artifact_match", "keywords_ok")

# `exec_outcome` for an HTML artifact that loaded without an uncaught JS error
# and had a non-empty body. `docs/eval-methodology.md` §1.1.
LOAD_ONLY_OUTCOME = "browser_ok"

POWER = 0.80
POWER_WORDS = "four runs in five"


def esc(value: Any) -> str:
    return _html.escape(str(value if value is not None else ""), quote=True)


def _instrument():
    """The harness's own modules, or None where this install does not carry them.

    Imported late and on purpose: the deployed image has no `evals/` package,
    and a coordinator must still start and answer this route there.
    """
    try:
        from evals import corpus, scoring, stats
    except ImportError:
        return None
    return corpus, scoring, stats


# ── the record ───────────────────────────────────────────────────────


def cell_grade(record: dict, is_success) -> str:
    """One result record, as the word its cell prints."""
    if is_success(record, require_judge=False):
        return LOADED if record.get("exec_outcome") == LOAD_ONLY_OUTCOME else PASS
    for stage in GRADE_STAGES:
        value = record.get(stage)
        if value is None:
            return UNGRADED
        if not value:
            return FAIL
    # The harness said no and every stage this module knows of held, so the
    # two disagree about what passing requires. That is not a failure anyone
    # observed, so it is not printed as one.
    return UNGRADED


def load_runs(results_dir: Path, is_success) -> list[dict]:
    """Every committed run, oldest first, with each task's cell.

    The record shape is `evals/compare.py::load_run`'s: one JSON object per
    line keyed by task id, and `summary.json`'s `meta` or, in the two oldest
    runs, the file's own top level.
    """
    runs: list[dict] = []
    for directory in sorted(p for p in results_dir.iterdir() if p.is_dir()):
        log = directory / "results.jsonl"
        if not log.exists():
            continue
        records: dict[str, dict] = {}
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                records[str(record.get("id"))] = record
        if not records:
            continue
        meta: dict = {}
        summary = directory / "summary.json"
        if summary.exists():
            blob = json.loads(summary.read_text(encoding="utf-8"))
            meta = blob.get("meta", blob)
        runs.append(
            {
                "run_id": directory.name,
                "prompt_set": meta.get("prompt_set"),
                "model": meta.get("model"),
                "started_at": meta.get("started_at"),
                "cells": {tid: cell_grade(r, is_success) for tid, r in records.items()},
                "categories": {tid: str(r.get("category") or "") for tid, r in records.items()},
            }
        )
    return runs


def identical_pairs(runs: list[dict]) -> list[tuple[dict, dict]]:
    """Runs sharing a prompt set — the definition compare.py and eval_power.py use.

    Not stricter and not looser: a second definition is a second thing that
    can disagree with the published noise floor.
    """
    pairs = []
    for index, a in enumerate(runs):
        for b in runs[index + 1:]:
            if a["prompt_set"] and a["prompt_set"] == b["prompt_set"]:
                pairs.append((a, b))
    return pairs


def smallest_visible_change(n: int, discordant: int, stats) -> int | None:
    """The net change, in tasks, this many items would notice at POWER.

    Computed at this pair's own measured churn. Rounded up, because a change
    of 10.8 tasks is not one anyone can make, and rounding down would claim a
    resolution the arithmetic does not give. None when no change the design
    can express reaches POWER.
    """
    if n <= 0:
        return None
    effect = stats.min_detectable_effect(n, discordant / n, power=POWER)
    if effect is None:
        return None
    return math.ceil(round(effect * n, 6))


def pair_view(a: dict, b: dict, stats) -> dict:
    shared = sorted(set(a["cells"]) & set(b["cells"]))
    view: dict[str, Any] = {
        "a": a["run_id"],
        "b": b["run_id"],
        "prompt_set": a["prompt_set"],
        "n": len(shared),
        "flipped": sorted(
            t for t in shared if (a["cells"][t] in PASSED) != (b["cells"][t] in PASSED)
        ),
    }
    ungraded = [t for t in shared if UNGRADED in (a["cells"][t], b["cells"][t])]
    if ungraded or not shared:
        # `eval-methodology.md` §4 rule 6: no statistic over a study holding an
        # ungraded item. The flips are not reported either, because an
        # ungraded cell cannot be said to have flipped or held.
        view.update(computed=False, ungraded=ungraded, flipped=[])
        return view
    result = stats.paired_test(
        {t: a["cells"][t] in PASSED for t in shared},
        {t: b["cells"][t] in PASSED for t in shared},
    )
    low, high = stats.wilson(result.discordant, result.n)
    view.update(
        computed=True,
        ungraded=[],
        both_pass=result.both_pass,
        a_only=result.a_only,
        b_only=result.b_only,
        both_fail=result.both_fail,
        discordant=result.discordant,
        discordant_rate=result.discordant / result.n,
        interval=[low, high],
        p_one_sided=result.p_one_sided,
        p_two_sided=result.p_two_sided,
        alpha=result.alpha,
        needed_one_way=stats.min_detectable(result.discordant, result.alpha),
        smallest_visible_change=smallest_visible_change(result.n, result.discordant, stats),
    )
    return view


def corpus_view(corpus, runs: list[dict]) -> dict:
    """What has not been measured, read off the corpus and its split lock."""
    try:
        items = corpus.load_corpus()
        lock_problems = corpus.check_split_lock(items)
    except (OSError, ValueError) as exc:
        return {"readable": False, "reason": type(exc).__name__}
    ever_run = {t for run in runs for t in run["cells"]}
    confirmatory = {i.id for i in items if i.split == "confirmatory"}
    return {
        "readable": True,
        "items": len(items),
        "development": sum(1 for i in items if i.split == "development"),
        "confirmatory": len(confirmatory),
        "split_lock_holds": not lock_problems,
        "split_lock_problems": lock_problems,
        "bands": corpus.band_distribution(items),
        "known_suspect_bands": sum(
            1 for i in items if i.band is not None and i.band.get("known_suspect")
        ),
        "confirmatory_ever_run": len(confirmatory & ever_run),
        "never_run": sum(1 for i in items if i.id not in ever_run),
    }


def build_record(results_dir: Path | None = None) -> dict:
    """The whole view as data. `render` turns it into the fragment."""
    results_dir = RESULTS_DIR if results_dir is None else results_dir
    instrument = _instrument()
    if instrument is None or not results_dir.is_dir():
        return {"recorded": False, "runs": []}
    corpus, scoring, stats = instrument
    runs = load_runs(results_dir, scoring.is_success)

    tasks: list[dict] = []
    seen: set[str] = set()
    for run in runs:
        for task_id, category in run["categories"].items():
            if task_id not in seen:
                seen.add(task_id)
                tasks.append({"id": task_id, "category": category})

    return {
        "recorded": True,
        "grade": "mechanical",
        "runs": [
            {k: run[k] for k in ("run_id", "prompt_set", "model", "started_at")}
            for run in runs
        ],
        "tasks": [
            dict(task, cells={run["run_id"]: run["cells"].get(task["id"], NOT_IN_RUN) for run in runs})
            for task in tasks
        ],
        "pairs": [pair_view(a, b, stats) for a, b in identical_pairs(runs)],
        "corpus": corpus_view(corpus, runs),
    }


# ── rendering ────────────────────────────────────────────────────────


def _proportion(value: float) -> str:
    return f"{value:.2f}"


def _p(value: float) -> str:
    return "below 0.001" if value < 0.001 else f"{value:.3f}"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def run_labels(runs: list[dict]) -> dict[str, str]:
    """`Aug 8` from `20260808_050610`, with the time added only where two collide."""

    def day(run_id: str) -> str:
        try:
            month, date = int(run_id[4:6]), int(run_id[6:8])
            return f"{calendar.month_abbr[month]} {date}"
        except (ValueError, IndexError):
            return run_id

    days = [day(r["run_id"]) for r in runs]
    labels = {}
    for run, label in zip(runs, days):
        rid = run["run_id"]
        if days.count(label) > 1 and len(rid) >= 13:
            label = f"{label} {rid[9:11]}:{rid[11:13]}"
        labels[rid] = label
    return labels


def _metric(value: str, label: str, note: str) -> str:
    return f"""
      <div class="rd-metric">
        <div class="rd-metric-v">{esc(value)}</div>
        <div class="rd-metric-l">{esc(label)}</div>
        <div class="ev-metric-note">{esc(note)}</div>
      </div>"""


def _strip(record: dict) -> str:
    runs, pairs = record["runs"], record["pairs"]
    sets = {r["prompt_set"] for r in runs if r["prompt_set"]}
    computed = [p for p in pairs if p["computed"]]

    if len(pairs) == 1 and computed:
        pair = computed[0]
        flipped = _metric(
            f"{pair['discordant']} of {pair['n']}",
            "TASKS THAT FLIPPED",
            f"between two identical runs · prompt set {pair['prompt_set']}",
        )
        visible = pair["smallest_visible_change"]
        change = _metric(
            f"{visible} of {pair['n']}" if visible is not None else "none",
            "SMALLEST VISIBLE CHANGE",
            f"net tasks, noticed {POWER_WORDS}"
            if visible is not None
            else f"no change is noticed {POWER_WORDS} at this churn",
        )
    elif len(pairs) > 1:
        # Neither picked nor pooled. `stats.MEASURED_DISCORDANT_RATE` is the one
        # measured value; folding a second pair into it is a methodology
        # decision, not something a view does on load.
        flipped = _metric(
            _plural(len(pairs), "identical pair"),
            "TASKS THAT FLIPPED",
            "each pair has its own table · none is pooled",
        )
        change = _metric(
            "per pair",
            "SMALLEST VISIBLE CHANGE",
            "read it off each pair's table",
        )
    else:
        if not pairs:
            reason = "no two recorded runs share a prompt set"
        elif pairs[0]["ungraded"]:
            reason = "the identical pair holds an ungraded task"
        else:
            reason = "the identical pair shares no task"
        flipped = _metric("not estimable", "TASKS THAT FLIPPED", reason)
        change = _metric("not estimable", "SMALLEST VISIBLE CHANGE", reason)

    recorded = _metric(
        str(len(runs)),
        "RUNS RECORDED",
        f"{_plural(len(sets), 'prompt set')} · {_plural(len(pairs), 'identical pair')}",
    )
    return f'<div class="rd-metrics">{flipped}{change}{recorded}</div>'


def _lede(record: dict) -> str:
    """Why there is no score, in the record's own counts."""
    cells = [c for task in record["tasks"] for c in task["cells"].values()]
    passed = sum(1 for c in cells if c in PASSED)
    loaded = sum(1 for c in cells if c == LOADED)
    sentences = [
        "This view prints no pass rate, and no row or column below is totalled."
    ]
    if passed:
        sentences.append(
            f"{loaded} of the {passed} passes in this record rest on a run check that only "
            "asked whether a page loaded without an error — those cells say "
            "<em>loaded</em>."
        )
    computed = [p for p in record["pairs"] if p["computed"]]
    if len(record["pairs"]) == 1 and computed:
        pair = computed[0]
        sentences.append(
            f"And two runs with nothing changed between them disagree on "
            f"{pair['discordant']} of {pair['n']} tasks, so a single figure moves that far "
            "by chance."
        )
    return f"""
      <p class="ev-lede">{' '.join(sentences)} See
        <code class="rd-code">docs/eval-methodology.md</code> §1.1 and §1.3.</p>"""


def _pair_panel(pair: dict, labels: dict[str, str], index: int, count: int) -> str:
    a, b = labels.get(pair["a"], pair["a"]), labels.get(pair["b"], pair["b"])
    title = "NOISE FLOOR" if count == 1 else f"NOISE FLOOR · PAIR {index + 1}"
    head = f"""
        <div class="rd-panel-head">
          <span class="rd-panel-label">{title}</span>
          <span class="rd-panel-meta">prompt set {esc(pair['prompt_set'])}</span>
        </div>
        <div class="rd-panel-note">Two runs with nothing changed between them:
          <code class="rd-code">{esc(pair['a'])}</code> and
          <code class="rd-code">{esc(pair['b'])}</code>.</div>"""

    if not pair["computed"]:
        why = (
            f"{_plural(len(pair['ungraded']), 'task')} in this pair could not be graded, and "
            "no statistic is computed over a comparison holding one — an ungraded task "
            "scored as a failure is a result nobody measured."
            if pair["ungraded"]
            else "These runs share no task, so there is nothing to pair."
        )
        return f"""
      <section class="rd-panel ev-pair" data-pair="{esc(pair['a'])}:{esc(pair['b'])}">{head}
        <div class="rd-panel-note">{esc(why)}</div>
      </section>"""

    needed = pair["needed_one_way"]
    # A noise floor has no direction, so the larger side is the one compared.
    threshold = (
        f"{needed} of the {pair['discordant']} had to move one way; "
        f"the larger side was {max(pair['a_only'], pair['b_only'])}"
        if needed is not None
        else f"{pair['discordant']} flips cannot clear {pair['alpha']} in any split"
    )
    low, high = pair["interval"]
    return f"""
      <section class="rd-panel ev-pair" data-pair="{esc(pair['a'])}:{esc(pair['b'])}">{head}
        <div class="ev-scroll">
          <table class="ev-table">
            <caption class="sr-only">Outcomes of the {pair['n']} tasks both runs graded</caption>
            <thead>
              <tr><td rowspan="2" colspan="2"></td><th scope="colgroup" colspan="2">{esc(b)}</th></tr>
              <tr><th scope="col">pass</th><th scope="col">fail</th></tr>
            </thead>
            <tbody>
              <tr><th scope="rowgroup" rowspan="2">{esc(a)}</th><th scope="row">pass</th>
                <td>{pair['both_pass']}</td><td>{pair['a_only']}</td></tr>
              <tr><th scope="row">fail</th><td>{pair['b_only']}</td><td>{pair['both_fail']}</td></tr>
            </tbody>
          </table>
        </div>
        <dl class="ev-facts">
          <dt>paired</dt><dd>{pair['n']} tasks</dd>
          <dt>flipped</dt><dd><span class="ev-nowrap">{pair['discordant']} of {pair['n']}</span> ·
            <span class="ev-nowrap">rate {_proportion(pair['discordant_rate'])}</span> ·
            <span class="ev-nowrap">interval {_proportion(low)}–{_proportion(high)}</span></dd>
          <dt>moved</dt><dd><span class="ev-nowrap">{pair['b_only']} up</span> ·
            <span class="ev-nowrap">{pair['a_only']} down</span> ·
            <span class="ev-nowrap">net {pair['b_only'] - pair['a_only']:+d}</span></dd>
          <dt>McNemar exact</dt><dd><span class="ev-nowrap">one-sided p {_p(pair['p_one_sided'])}</span> ·
            <span class="ev-nowrap">two-sided p {_p(pair['p_two_sided'])}</span></dd>
          <dt>to clear {pair['alpha']}</dt><dd>{threshold}</dd>
        </dl>
        <div class="rd-panel-note">Nothing differed between these runs but chance, so a
          difference between any two runs smaller than this churn cannot be told apart from
          it. The interval is Wilson's, at the conventional 95 in 100.</div>
      </section>"""


_CELL_CLASS = {
    PASS: "is-pass",
    LOADED: "is-loaded",
    FAIL: "is-fail",
    UNGRADED: "is-ungraded",
    NOT_IN_RUN: "is-absent",
}


def _grid_panel(record: dict, labels: dict[str, str]) -> str:
    runs, pairs = record["runs"], record["pairs"]
    pair_runs = {p["a"] for p in pairs} | {p["b"] for p in pairs}
    shown_pairs = [p for p in pairs if p["computed"]]

    head = '<th scope="col" class="ev-task-col">task</th>'
    for run in runs:
        rid = run["run_id"]
        in_pair = rid in pair_runs
        prompt_set = run["prompt_set"] or "unknown set"
        head += (
            f'<th scope="col" class="ev-run{" is-pair" if in_pair else ""}" title="{esc(rid)}">'
            f'<span class="ev-run-day">{esc(labels[rid])}</span>'
            f'<span class="ev-run-set">{esc(prompt_set)}{" · pair" if in_pair else ""}</span></th>'
        )
    for index, pair in enumerate(shown_pairs):
        name = f"{pair['prompt_set']} pair" if len(shown_pairs) == 1 else f"pair {index + 1}"
        head += f'<th scope="col" class="ev-run is-flip">{esc(name)}</th>'

    width = 1 + len(runs) + len(shown_pairs)
    body = ""
    category = None
    for task in record["tasks"]:
        if task["category"] != category:
            category = task["category"]
            body += (
                f'<tr class="ev-group"><th scope="colgroup" colspan="{width}">'
                f'<span class="ev-group-name">{esc(category or "uncategorised")}</span></th></tr>'
            )
        row = f'<th scope="row" class="ev-task">{esc(task["id"])}</th>'
        for run in runs:
            word = task["cells"][run["run_id"]]
            mark = " is-pair" if run["run_id"] in pair_runs else ""
            row += f'<td class="ev-cell {_CELL_CLASS[word]}{mark}">{esc(word)}</td>'
        for pair in shown_pairs:
            flipped = task["id"] in pair["flipped"]
            word = "flipped" if flipped else "same"
            row += f'<td class="ev-cell ev-flip{" is-flipped" if flipped else ""}">{word}</td>'
        body += f"<tr>{row}</tr>"

    others = len(runs) - len(pair_runs)
    if pairs and others:
        across = (
            f"Only the {_plural(len(pair_runs), 'column')} marked as a pair share a "
            f"configuration. The other {others} differ in prompt set, and a difference "
            "between them read by eye is the mistake <code class=\"rd-code\">evals/compare.py"
            "</code> was written to stop."
        )
    elif pairs:
        across = "Every column here belongs to an identical pair."
    else:
        across = (
            "No two columns share a configuration, so no difference between any of them "
            "can be told apart from run-to-run chance."
        )

    return f"""
      <section class="rd-panel ev-grid-panel">
        <div class="rd-panel-head">
          <span class="rd-panel-label">EVERY TASK, EVERY RUN</span>
          <span class="rd-panel-meta">mechanical grade</span>
        </div>
        <div class="ev-scroll">
          <table class="ev-grid">
            <caption class="sr-only">The recorded outcome of each task in each committed run</caption>
            <thead><tr>{head}</tr></thead>
            <tbody>{body}</tbody>
          </table>
        </div>
        <dl class="ev-legend">
          <dt class="ev-cell is-pass">pass</dt><dd>every mechanical check held</dd>
          <dt class="ev-cell is-loaded">loaded</dt><dd>every check held, but the run check only
            asked whether the page loaded without an error</dd>
          <dt class="ev-cell is-fail">fail</dt><dd>a check did not hold</dd>
          <dt class="ev-cell is-ungraded">ungraded</dt><dd>a check could not be decided, which
            is not a failure</dd>
        </dl>
        <div class="rd-panel-note">{across} The model judge's score is recorded in each
          result and gates nothing on this view.</div>
      </section>"""


def _corpus_panel(corpus: dict) -> str:
    head = """
        <div class="rd-panel-head">
          <span class="rd-panel-label">THE CORPUS</span>
          <span class="rd-panel-meta">evals/prompts.json</span>
        </div>"""
    if not corpus.get("readable"):
        return f"""
      <section class="rd-panel ev-corpus">{head}
        <div class="rd-panel-note">The corpus or its split lock could not be read
          ({esc(corpus.get('reason'))}), so nothing is said about what has not been
          measured.</div>
      </section>"""

    bands = corpus["bands"]
    lock = (
        "held out; the split lock holds"
        if corpus["split_lock_holds"]
        else "the split lock does not hold, so this set can no longer be called held out"
    )
    return f"""
      <section class="rd-panel ev-corpus">{head}
        <dl class="ev-facts">
          <dt>items</dt><dd>{corpus['items']}</dd>
          <dt>development</dt><dd>{corpus['development']} · for debugging and iteration</dd>
          <dt>confirmatory</dt><dd>{corpus['confirmatory']} · {lock}</dd>
          <dt>confirmatory run</dt><dd>{corpus['confirmatory_ever_run']} of {corpus['confirmatory']}</dd>
          <dt>never run</dt><dd>{corpus['never_run']} of {corpus['items']}</dd>
          <dt>bands</dt><dd>{bands.get('discriminating', 0)} discriminating ·
            {bands.get('ceiling', 0)} ceiling · {bands.get('floor', 0)} floor ·
            {bands.get('unbanded', 0)} unbanded</dd>
          <dt>suspect bands</dt><dd>{corpus['known_suspect_bands']} · computed under the
            load-only check</dd>
        </dl>
        <div class="rd-panel-note">The tasks in the grid are the ones that have run. Bands are
          provisional, drawn from runs that used different prompt sets rather than one
          fixed arm.</div>
      </section>"""


def render(record: dict) -> str:
    """The fragment the console puts in `#evals-body`."""
    if not record["recorded"]:
        return """
      <div class="empty-state"><p>This server carries no eval record.<br>The harness
        writes to <code class="code-inline">evals/results/</code> in a source checkout, and
        the deployed image does not include it.</p></div>"""
    if not record["runs"]:
        return """
      <div class="empty-state"><p>No eval run has been recorded yet.<br>Run
        <code class="code-inline">py evals/run_evals.py</code> from a source checkout and
        its record lands here.</p></div>"""

    labels = run_labels(record["runs"])
    pairs = record["pairs"]
    pair_panels = "".join(
        _pair_panel(pair, labels, index, len(pairs)) for index, pair in enumerate(pairs)
    )
    if not pairs:
        pair_panels = """
      <section class="rd-panel ev-pair">
        <div class="rd-panel-head"><span class="rd-panel-label">NOISE FLOOR</span></div>
        <div class="rd-panel-note">Not estimable. No two recorded runs share a prompt set,
          and a floor measured across a prompt change confounds chance with the change.</div>
      </section>"""

    return f"""
    <div class="rd ev" data-evals>
      {_strip(record)}
      {_lede(record)}
      <div class="rd-body">
        <div class="rd-main">{_grid_panel(record, labels)}
        </div>
        <div class="rd-side">{pair_panels}{_corpus_panel(record['corpus'])}
        </div>
      </div>
    </div>"""
