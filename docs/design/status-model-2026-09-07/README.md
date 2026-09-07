> **Status:** Point-in-time design handoff; non-normative as a whole.
> **Handoff date:** 2026-09-07
> **Repository revision when archived:** `708b7b310eb4a872617471f7e0e549001a2b1cc0` (`master`)
> **Authority:** Current source code, protocol documentation, and accepted ADRs
> supersede this handoff when they conflict.
> [`docs/design/HANDOFF-DELTA.md`](../HANDOFF-DELTA.md) remains the contract for
> the console port; **where this document and the delta disagree, the delta wins.**
> **Archive note:** Both files are archived as received. `support.js` exists only
> to make the design file render in a browser; it is not production code and must
> not be copied into `templates/` or `static/`. It is byte-identical to the copy
> in [`../console-2026-08-28/`](../console-2026-08-28/) and is duplicated here so
> the design file opens on its own.

# Mycelium — status model, status bar, work card

Three additions to the console designed at
[`../console-2026-08-28/`](../console-2026-08-28/), in that file's own
vocabulary. Open `Mycelium Status System.dc.html` directly in a browser — no
server, no build.

| Section | What it specifies | Ported |
|---|---|---|
| §01 State machine | Four ternary facets, the dependency rule, hysteresis constants, worst-facet-wins severity | Yes — `templates/_status_model.js` |
| §02 Status bar | 30px bar, lamped vs at-load cells, the age cell, placement below rail and view | Yes — `templates/dashboard.html`, `_dashboard.css` |
| §03 Live rig | Six transports exercising the hysteresis | Yes — as tests, `tests/test_status_model.py` |
| §04 Ambient indicator | The bar collapsed to a pill; 28px console, 44px public | Yes — console pill and `templates/locked.html` |
| §05 Banners | The four states that earn words, and the three that deliberately do not | Yes |
| §06 Work card | Gallery and Projects cards | **No** — waits for those views. Its chip vocabulary (§06 `chipRows`) is ported to the run modal. |
| §07 Port notes | Where every value comes from, and the four gaps | Reference |

## The three value traps §07 names

Each one is a plausible field that means something else. Each has a test in
`tests/test_status_bar.py`.

1. **Model name** is `config · model`. `/health.models` lists everything
   installed on the host and `/status.json.model` is whichever tag answered
   first — neither is the model this coordinator will use.
2. **RUNNING / QUEUED** are `/metrics · jobs_running` and `jobs_queued`.
   `/health.tasks_pending` is the subtask queue: a different number, and wrong
   under this label.
3. **Tracing** has three states — `off`, `propagating`, `exporting` — from
   `config · tracing_enabled` and `tracing_export` plus SDK availability
   (`tracing.py`). It is not a boolean.

## What this design refuses to draw

No utilization figure of any kind. A node's capability descriptor claims CPU,
memory and GPU at registration; nothing samples them afterwards and no endpoint
serves a sample, so a load line drawn from a claim would be a drawing rather
than a measurement. See §07 `gapCards`.
