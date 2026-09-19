> **Status:** Point-in-time design handoff; non-normative.
> **Handoff date:** 2026-09-18
> **Authority:** Current source code, protocol documentation and accepted ADRs
> supersede this handoff where they conflict. Its spec repeats claims that
> [`docs/design/HANDOFF-DELTA.md`](../HANDOFF-DELTA.md) already corrects (§4.1 on
> the quality figure, §11 on the Invite view, among others). **Where this bundle
> and the delta disagree, the delta wins.**
> **Archive note:** Archived as received, with one rename. The bundle's
> `CLAUDE.md` is `PORTING.md` here, so that it can't be loaded as standing
> instructions for anyone working in this folder. Its rules for the port are
> the same. The `.dc.html` files and `support.js` are byte-identical so they
> still render in a browser. `support.js` is not production code and must not
> be copied into `templates/` or `static/`. The design files load Google Fonts;
> the server templates must not (README, "Assets").

# Handoff: Mycelium console — the whole thing

## Overview

Mycelium is a distributed orchestrator: you pitch a task, a coordinator plans it
into subtasks, and other people's computers build them. This bundle is the
complete UI for it — ten authenticated console views, five public pages, one
shared theme layer, and the design decisions behind all of them.

The target is `distributed-orchestrator`: FastAPI + Jinja templates + vanilla
JS, no build step, no framework.

## About the design files

**The files in `design/` are design references created in HTML.** They are
prototypes showing intended look and behaviour — not production code to copy.
They are written in a React-flavoured single-file component format and need
`support.js` (included) to render.

Your job is to **recreate them in this repo's existing environment**: Jinja
templates, `_dashboard.css`, `_dashboard.js`. Take the structure, the copy, the
token names, the spacing, the states and the behaviour. Leave the component
class, `renderVals()`, `<sc-for>`, `<sc-if>` and `support.js` behind.

Open any `design/*.dc.html` directly in a browser — no server, no build. In
`Mycelium Console.dc.html` the left rail switches views and the identity block
at its bottom-left switches to the public pages.

`spec/_theme.html` is the exception: it is **production code**, a drop-in
replacement for `templates/_theme.html`. Copy it in as-is.

## Fidelity

**High-fidelity.** Final colours, typography, spacing, copy, states and
interactions. Recreate it closely, using this repo's own patterns for the
plumbing. Every colour in the design is already a `var(--token)` from
`spec/_theme.html`, so markup transfers without a colour audit.

## Read in this order

| File | What it is |
|---|---|
| `CLAUDE.md` | Standing instructions. Drop at the repo root. Rules, port order, constraints. Read first. |
| `README.md` | You are here. The map: screens, tokens, behaviour, state. |
| `spec/HANDOFF-UI.md` | **The real document.** ~2000 lines, §1–19: port order, element→endpoint map for every element, copy rules, known API gaps, and a numbered record of each design pass with its reasons. |
| `spec/_theme.html` | Production. Drop-in `templates/_theme.html`. |
| `design/*.dc.html` | The designs. Open in a browser. |
| `context/*.dc.html` | Before-pictures and superseded passes, for comparison only. Do not port from these. |

## Files in this bundle

```
CLAUDE.md                                  standing instructions for the port
README.md                                  this file
spec/HANDOFF-UI.md                         the specification (§1–19)
spec/_theme.html                           production theme layer
design/Mycelium Console.dc.html            every console view + public pages
design/Mycelium Status System.dc.html      status model, status bar, work card
design/Mycelium Run Detail.dc.html         run detail across three surfaces
design/Run Detail Surface.dc.html          child of the above
design/Provenance Envelope.dc.html         child of the above
design/Ledger Chain.dc.html                child of the above
design/support.js                          makes the design files render
context/Mycelium Dashboard - Current.dc.html   the existing dashboard, recreated
context/Mycelium Console v4 -green console-.dc.html   pre-Sept-14 pass
context/support.js
```

## Screens

### Console views (authenticated)

| View | Purpose | Form, and why |
|---|---|---|
| **Overview** | Pitch work, watch it happen | Full-bleed. A vital line across the top, the chat thread filling the middle, a floating window on the right. Leads with **four** vital signs, not six equal metrics. |
| **Runs** | Every completed pipeline | Hairline table, 61 rows, grouped by day. Filter chips + search + sortable columns. |
| **Run detail** | One run, in full | Leads with the **deliverable**, then the plan. Plan is process; the code is the product. |
| **Nodes** | The machines, and the topology | **Rows, not a table** — at 2–5 nodes each machine is read individually and its status needs to be a sentence. Plus the node map (below). |
| **Gallery** | What runs produced | Previews **code**, not prose. That is the artifact, and it is what separates Gallery from Runs. |
| **Projects** | Runs that iterate on each other | Shows the **iteration chain**, not a count. Memory across runs is the feature; a number hides it. |
| **Guild** | Points per machine | Standings table. |
| **Evals** | Output quality | Every figure beside its confidence interval. Pass rate lives here, not on Overview — stating `57%` flatly implies precision it does not have. |
| **Config** | What this coordinator is running with | Grouped rows, each with a plain-language note on the consequence. |
| **Invite** | Adding a machine | The join command, viewer keys, and what a machine agrees to. |

The **top bar is contextual, not global**: it carries the view's title and
subtitle, so no view names itself twice. A full-width global bar exists to hold
an org/environment/region switcher; Mycelium has no such concept, so that bar
would be empty chrome.

The **rail starts collapsed** (58px, icons only, toggle in its header, state in
`railOpen`). Navigation is not the page; the thread is.

### Public pages (server-rendered)

| Page | Route | Notes |
|---|---|---|
| Landing | `/` | The pitch, plus an honest-capability section. Every number from the repo. |
| Try | `/try` | One attempt, on this machine. Rate-limited. |
| Share card | `/s/{token}` | What somebody without a viewer key sees. Spec §5 — structural, not a restyle. |
| Status | `/status` | Spec §5.5. **Redact before adding to the public allowlist.** |
| Machine | `/node/{id}` | One machine's own page. Spec §5.5 and §19.6. |

44px minimum hit targets on all of these.

## The node map

Three scales of the same instrument, in the Nodes view. Spec §16.6 and §19 are
the full account; this is the shape of it.

| Scale | Shows | Shape |
|---|---|---|
| **Live** | This coordinator right now, from `/nodes` | The pipeline left to right. Axes mean different things, so the fit is allowed to stretch. |
| **Simulated** | One run at its widest — 18 machines | Two fans (wave 1 up, wave 2 down, both from the coordinator) and then a 5·4·4 field of thirteen idle machines. |
| **Swarm** | Three runs in a field of 125 | Runs inside a rim: three runs at radius 0.52, the machines nobody is using filling the disc around them. |

Rules that hold at every scale:

- **Shape carries strategy, colour carries only state.** A fan is one plan split
  across machines; two tiers is a DAG with the far one still held on
  `depends_on`; three near-parallel lines is the same subtask on three machines.
- **A run's ground is a polygon on its machines.** Vertices are the node
  centres, sorted by angle around the centroid so it can never self-intersect.
  Live's triangle is the three-machine case; a five-machine run is a pentagon.
- **The area of the ground is the pitch** — the one sentence somebody typed.
  Every corner is a machine that sentence reached.
- **Grounds never touch.** On the swarm scale that is the federation point made
  without a caption: no coordinator knows about the others.
- **Every line runs between a coordinator and one of its builders**, because
  that is the only path work takes. There is no peer-to-peer path. Direction is
  carried by flowing dashes (out `--warn`, back `--accent`), never by a new
  colour.
- **Layout is a fit, not a remembered grid.** Models describe positions in their
  own units; every paint maps the model's real bounding box onto the *current*
  stage, re-fitting on resize via a `ResizeObserver` on the stage. Models that
  describe a shape set `uniform: true` for one scale plus centring — without it
  a circle comes out an ellipse.
- **Zoom changes size, not just magnification.** Marks grow as `z^0.62` while
  distances grow as `z`, so zooming in opens the graph up. Labels and strokes
  divide that back out to hold their on-screen size. Labels reveal by zoom
  threshold (0 / 1.8 / 3.2) and are culled on collision.
- **Everything is inspectable to the same depth**: hover a machine, a line or a
  ground and the same panel opens with the same row vocabulary. Drawn data ends
  with a `DATA` row saying so. Clicking a live machine opens `/node/{id}`.
- **No invented ledger figures.** Points, tasks and weight mean something
  specific in the repo; drawn machines carry platform/model/hardware and
  nothing that looks like a measurement.

## Interactions & behaviour

- **The Overview composer runs the pipeline, it does not fake a spinner.** It
  animates the server's own event sequence (pitch → plan → build → review →
  complete), and **each strategy gets a different shape**: plan & split shows
  waves with wave 2 held on `depends_on`; several tries shows three whole
  attempts with the third waiting for a free machine; one shot shows one card;
  no machines connected shows the same plan built serially here. The shape is
  the explanation — do not flatten them into one progress bar. **Timings are the
  animation's own clock and are never presented as throughput** (spec §14).
- **Agent steps are a vertical timeline**, not table rows: a spine with round
  step markers leading the reply. Round is deliberate — a step marker is
  progress, not status, so it must not read as one of the square lamps.
- **The bottom status bar** spans the rail and the view, 30px, at the very
  bottom, and collapses to an ambient pill. MODE / LOCK / TRACING / EVIDENCE are
  configuration rather than status and live in a disclosure drawer; the **age is
  the disclosure trigger**. The severity word is always the condition's own name
  — never "degraded". Spec §12 and §17.
- **Runs**: four filter chips + text search + sortable columns. Any sort other
  than `WHEN` drops the day headings and says so — a list grouped by day and
  ordered by wall clock is two orderings claiming to be one. Chip counts are the
  ledger's totals for all 61; the corner states how many this page holds.
- **Revoke is two clicks.** It arms on the first and fires on the second. The
  row it leaves says what survived: finished work and points stay on the ledger,
  and the name is retired rather than freed. It is the only destructive control
  in the console — keep it two clicks.
- **Every control has a short hover description.** One that calls an endpoint
  names it (`POST /run/{id}/cancel`). One with no endpoint says so and cites the
  gap. One with a security consequence says the consequence in plain words.
- Hysteresis on status: a facet must hold its new value before the UI moves.
  `Mycelium Status System.dc.html` §03 is a live rig that breaks the backend so
  it can be watched.
- `prefers-reduced-motion: reduce` kills every animation.

## State

Client state the console needs, by name as the design holds it:

| State | Values |
|---|---|
| `page` | `app` / `landing` / `try` / `share` / `status` / `machine` |
| `view` | `overview` / `runs` / `run` / `nodes` / `gallery` / `projects` / `guild` / `evals` / `config` / `invite` |
| `railOpen` | collapsed by default |
| `theme` | `dark` / `light`, persisted in `localStorage` as `mycelium-theme`, applied before paint |
| `machineId` | which machine `/node/{id}` is about |
| `mapScale` | `live` / `demo` / `swarm` |
| `mapHover`, `mapSel` | `{t: 'n'\|'e'\|'f', k}` — hovered / pinned map subject |
| `runFilter`, `runQ`, `runSortK`, `runSortDir` | the Runs table |
| `armRevoke`, `revokedNodes`, `rotatedNodes` | node row actions |
| `statusBarOpen`, `statusCollapsed` | the bottom bar and its drawer |
| `viewerAuth`, `enrollMode` | read from config; they change what several views say |

Data comes from `/nodes`, `/history`, `/run/{id}`, `/ledger`, `/status`,
`/projects`, `/pitch` and the `/ws/events` websocket. Spec §3 maps **every
element** to its endpoint; §8, §12.4, §13.7 and §14.2 list the gaps.

## Design tokens

All of them are defined in `spec/_theme.html`, both themes. Do not redeclare
them; do not add literals.

### Colour — dark (light values in the file)

```
--bg #0B0B0C   --bg-elevated #121214   --surface #191A1C
--surface-hover #212225   --surface-sunken #0E0E10
--border-subtle #1C1D20   --border #24262A   --border-strong #34373C
--card-lift 0 1px 2px rgba(0,0,0,.45), 0 8px 24px rgba(0,0,0,.28)
--text #EDEDEF   --text-dim #A8AAB0   --text-muted #92959C   --text-faint #83868E
--mark #EDEDEF                          brand, neutral on purpose
--accent #3FB950                        PASS / connected / ok, nothing else
--warn #C9922B    --danger #E5534B    --info #5B8DEF
--violet #9A7BE0  --slate #949AA2
```

`--text-muted` and `--text-faint` carry every 11px uppercase label and are tuned
to clear 4.5:1 against both `--bg` and `--surface`. Do not darken them.

### Type

- `--sans` IBM Plex Sans, `--mono` IBM Plex Mono, both falling back to system.
- Body 13.5px. **Floor 11px.** `font-variant-numeric: tabular-nums` globally.
- **Mono is for IDs, numbers, code and paths only.** Uppercase section labels
  are the sans at the same size and tracking, with weight carrying what mono's
  width used to (Sept 13).

### Spacing, radius, motion

- Radius scale 8 / 10 / 16 / 18px. Tokens `--radius` 8, `--radius-sm` 6,
  `--radius-lg` 10, `--radius-xl` 16.
- Rail 58px collapsed / 212px open. Top bar 68px, 17px title, no hairline.
  Status bar 30px. Right window 316px.
- Keyframes ship in the theme file: `dashFlow` / `dashBack` (direction on a
  line), `liveBlink`, `runPulse`, `cursorBlink`, `cardIn`, `railIn`.

## Assets

None. No images, no icon font, no SVG illustrations. Every mark is text, a CSS
shape, or SVG geometry generated from data. The only external request in the
design files is the Google Fonts link, which **must not** be carried into the
server templates — self-host Plex under `static/` or accept the system-font
fallback.

## Checks after porting

- `pytest -q` and `ruff check .` clean, on Python 3.14.
- `test_no_page_hardcodes_a_colour` passes — no literal hex or `rgba()` in any
  template or in `_dashboard.css`.
- No type below 11px anywhere; no hit target below 44px on a public page.
- `--accent` appears only on PASS / connected / ok.
- Both themes, every view, at 1280px and at 380px.
- The prohibited-language table (spec §7) returns nothing.
- Nothing on screen that no endpoint serves.
- Per-section checks: spec §11, §12.5, §13.10, §14.5, §15.7, §16.4, §17.3,
  §19.8.
