# Mycelium console — UI handoff

Design source: `Mycelium Console.dc.html` (this project). Before-picture:
`Mycelium Dashboard — Current.dc.html`. Drop-in token layer: `handoff/_theme.html`.

Written for whoever ports this into the repo. Jett is non-technical — make the
technical calls yourself and don't ask him to choose between approaches.

_Updated Aug 28, 2026: the repo now has two pages this file had never accounted
for — `templates/run.html` (`/run/{id}`) and `templates/status.html` (`/status`
and `/node/{id}`) — and `dashboard.html`'s CSS and JS have moved into
`_dashboard.css` / `_dashboard.js` partials. §2, §5.5 and §9 changed for that.
Previously updated Aug 27, 2026 against `master` after the trusted-alpha RC1
sprint and the two Codex themes that followed it (durable execution truth,
durable node enrollment). Five things in this file changed with that work: artifacts are now
sealed and role-scoped with a separate audit download, submission is
idempotent, terminal state commits before anything is published, one
coordinator owns one state directory, and shares can be listed and revoked.
`HANDOFF.md` §"Claude Code frontend/API handoff" is the contract; where this
file disagrees with it, that file wins._

---

## 1. What changed, in one paragraph

The dashboard's six tabs, the landing page and the try page were rebuilt as a
developer console: hairline tables instead of card stacks, one type scale
(11 / 11.5 / 12.5 / 14 / 16 / 20-22px), IBM Plex Sans + Mono, and colour
reserved for state. Four surfaces were added that had no design at all: a run
detail view (replacing the output modal), a read-only config view, an
output-quality view built on `evals/results`, and an invite/consent view. Green
no longer doubles as the brand colour. Everything is themed through tokens, so
light mode works.

**Then the backend changed what the UI is allowed to say.** A single `PASS`
badge is no longer truthful: lifecycle, validation outcome and assurance level
are three independent fields, artifacts are delivered through an authenticated
API instead of filesystem paths, sharing is an explicit expiring capability, and
private routes require a viewer credential. The design was updated for all four.
§7 is the language contract — it is the part most likely to drift back.

**Then RC1 hardened what the UI is allowed to imply.** A manifest is now sealed
at the end of a run and every entry carries a role, so "the files" is two
lists with two endpoints rather than one zip. A keyed pitch can be replayed,
so a run you are looking at may be one you were *returned* rather than one you
just started. Terminal state commits to SQLite before the UI is told anything,
which means there is now an honest degraded state where the server refuses to
claim a transition at all. And one coordinator owns one state directory, which
makes deployment mode and lock ownership operator-visible facts rather than
implementation detail. §6 and §6.5 are new; §3's Config block grew a deployment
group.

---

## 2. Port order

Do it in this order; each step is independently shippable.

1. **`templates/_theme.html` ← `handoff/_theme.html`.** Straight file swap. It
   is a superset of the current token set, so `index.html`, `dashboard.html`
   and `try.html` keep working untouched and immediately pick up the new
   palette. `test_theme.py` still passes: `:root {`, `[data-theme="light"]`,
   `prefers-color-scheme`, `localStorage` and `--accent:` are all present, and
   the pre-paint script still runs in `<head>`. Verified Aug 22: every token the
   design references is declared here, so no page can reference a missing one.
2. **Viewer auth in the client — do this before any view work.** Every private
   page and fetch can now answer `401`, and `/ws/events` can close with `4401`.
   A dashboard that doesn't handle those looks broken rather than locked. See §4.
3. **Fonts.** The token stacks name IBM Plex first with a system fallback, but
   no webfont is loaded — this project doesn't phone home and a Google Fonts
   request per visitor would break that. To actually get Plex, self-host the
   woff2 files under `static/` and add `@font-face` to the theme partial. Until
   then pages render in system UI fonts, which is fine.
4. **`templates/dashboard.html`** — and note that its styles and script are no
   longer in it. `dashboard.py` pastes three partials into markers:
   `<!-- THEME -->`, `<!-- DASHBOARD_CSS -->` (`_dashboard.css`) and
   `<!-- DASHBOARD_JS -->` (`_dashboard.js`). So the port is: markup view by
   view in `dashboard.html`, styles in `_dashboard.css`, behaviour in
   `_dashboard.js` — three files, no build step. The design file's markup is
   already `var(--token)` throughout, and `test_no_page_hardcodes_a_colour` now
   covers the two partials as well as the pages, so a colour cannot be smuggled
   in by moving it out of the page. Keep every existing `id` and the existing
   `showTab()` / `refresh()` / `loadHistory()` JS; only markup and CSS change.
   New views need new `id`s and new entries in `TABS` / `TAB_TITLES`.
5. **`templates/index.html`** and **`templates/try.html`**. The try page's copy
   is now load-bearing — see §3. Neither mentions the MCP server; the Connect
   view in the design does, and `docs/MCP.md` is the source for that copy.
6. **`templates/run.html` and `templates/status.html`** — see §5.5. Both are
   server-rendered, both are in `test_theme.py`'s `PAGES`, and both currently
   ship pre-RC1 language.
7. **The share page** — see §5. Structural change, not a restyle.
8. **The status bar and the unified status model** — see §12. Do it with step 2,
   not after the views: it replaces the fail-open banner, the
   persistence-degraded banner and the footer's inference line, so porting
   those three separately is work you would then delete.
9. **Run detail, then the provenance envelope and the ledger chain** — see §13.
   Run detail is the last view with real structural work in it, and it is the
   one three surfaces share: the dashboard's output modal, the console view and
   `templates/run.html`. Do the modal-header fix (§13.6) first — it is four CSS
   declarations and it unblocks reading the modal at any width. The envelope and
   chain panels come last because **neither has an HTTP route today** (§8.7,
   §8.8); everything above them ships without either.

---

## 3. Element → endpoint map

New clients should read canonical `/v1/executions*`. The legacy `/history*`,
`/jobs*`, `/gallery`, `/projects*` and `/standings` routes still work and are
now viewer-protected; where a screen still reads one, it is called out.

### Access-control shape that applies to everything below

| Fact | Consequence for the UI |
|---|---|
| Viewer middleware is deny-by-default when `viewer_key` is set | any private fetch can `401`; `/ws/events` closes `4401` |
| `/health` is public but thin — `nodes_online: integer` only | **`/nodes` is not part of public health**; the public pages may show the count and nothing more |
| `/health` carries `private_routes_protected` and `warnings[]` | drives the fail-open banner; see §4 |
| Events are flat objects — `id`, `type`, `time` are peers of the payload | no nested `data`; tolerate unknown event names |
| `pitch_key` and `node_secret` are separate authorities | a viewer session does **not** let the page submit work |

### Overview — `#view-overview`

The metric strip is **four vital signs** (nodes online, inference, running now,
queued) — things that answer *is it working right now* — with the all-time
figures on one quiet line beneath. Pass rate deliberately does **not** appear
here: stating `57%` flatly implies a precision it doesn't have. It lives in
Evals next to its confidence interval, and the summary line links there.

| Element | Source |
|---|---|
| Vital: nodes online | `GET /health` → `nodes_online` (integer) |
| Vital: inference ready/offline | `GET /health` → `ollama` |
| Vital: running now | `GET /health` → active execution count |
| Vital: queued | `GET /health` → `tasks_pending` |
| Fail-open banner | `GET /health` → `private_routes_protected`, `warnings[]` |
| Summary line: built, avg latency | `GET /metrics` (viewer) |
| Summary line: uptime | `GET /status.json` → `uptime_seconds` |
| Summary line: quality `~57% ±12` | `evals/results` — see the Evals view |
| Sidebar footer: ollama state + model | `GET /health` → `ollama`, `models[0]` |
| Topbar points | `GET /metrics` → orchestrator contribution total |
| Composer rate hint (`5 / min`) | `config.pitch_rate_max` / `pitch_rate_window` |
| Submit a task | `POST /v1/executions` with **`pitch_key`** → `202` + execution id |
| Active run panel + stage table | `GET /v1/executions/{id}`; stage transitions from events |
| Token stream | `type: "token"` frames on `/ws/events` (flat objects) |
| Activity log | `/ws/events`, polling fallback `GET /events?since=N` |
| Nodes panel | `GET /nodes` (**viewer required**) |
| Recent runs | `GET /history?limit=4` (viewer) |

### Runs — `#view-runs`

`GET /history` / `?search=` for the filter box, or canonical execution reads.
Row click → run detail.

**The OUTCOME column is two fields, not one rating.** Top line is
`validation_outcome` (`passed` / `partial` / `failed` / `not_run`) — or the
`lifecycle_status` word when the run never got to be judged (`cancelled`,
`interrupted`). Second line is the `assurance_level` that backs it. Filter chips
are `passed` / `partial` / `failed`, not `pass` / `needs work` / `fail`.

`cancelled` and `interrupted` are ordinary states now, not edge cases: a
coordinator restart moves anything mid-flight to `interrupted` and marks it
retryable. Design them as normal rows, in `--slate`, not as errors.

Rows are grouped under day headers with a one-line summary per group, computed
client-side. **`wall` is still not served** — see §8.

### Run detail — new view

`GET /v1/executions/{id}` returns lifecycle, validation, assurance, requested /
planned / observed placement, consent, units, candidates, winner explanation,
validation summaries, artifact references and bounded previews.

| Element | Source |
|---|---|
| LIFECYCLE / VALIDATION / ASSURANCE triad | `lifecycle_status`, `validation_outcome`, `assurance_level` |
| Triad's third line (`5 checks run · 5 passed…`) | `validation_summary` — checks run / passed / failed / not run |
| PLACEMENT bar | `placement_requested` → `placement_planned` → `observed_placements`; render `mixed` when mixed — `placement_selected` is null then |
| Consent line | remote-consent fields. The canonical default is local / local-only / no consent, so for most runs the honest line is "stayed on this machine" |
| Manifest panel | `GET /v1/executions/{id}/artifacts` → `ArtifactManifestV1` |
| Per-file hash + size | `entries[].sha256`, `.size_bytes`, `.media_type` |
| `Download .zip` | `GET /v1/executions/{id}/download` |
| One file | `GET /v1/executions/{id}/artifacts/{relative_path}` |
| `Share…` dialog | `POST /v1/executions/{id}/shares` — see §5 |
| `cancel` on the active run (Overview) | `POST /v1/executions/{id}/cancel`, idempotent |
| `retry` on cancelled / interrupted rows (Runs) | resubmit; startup marks non-resumable executions retryable |
| Timeline | `full_log.json` via the manifest |

Do **not** project `status` into the badge. `status` is a compatibility
projection — lifecycle `completed` + validation `passed` becomes
`"completed"`, and *every other* completed outcome becomes `"unverified"`,
which flattens exactly the distinction this view exists to show.

Two layout choices carried over:

- **The deliverable comes first, the plan second.** Plan is process; the code is
  the product. The old modal led with process.
- **`depends_on` is rendered as waves, not a column.** Group units by dependency
  depth. Wave 2 showing three subtasks side by side on two machines *is* the
  task-level-parallelism claim the project rests on. The `1.6× faster than
  sequential` metric is the sum of unit durations over wall clock; only show it
  when per-unit durations are actually available.

### Nodes — `#view-nodes`

`GET /nodes` — **viewer required.** Fields as before, plus `credits_earned`,
which the UI labels `POINTS` (§7).

The view opens with the **map**, then the rows.

**The map draws machines only, laid out as the pipeline.** Left to right is
plan → waves of building → review, so the coordinator sits at the **edge**, not
the centre: planning and review are its work, and the hardware doing the
building belongs to other people. An earlier version put "this machine" in the
middle with everything orbiting it, which draws a client/server hub and reads as
a hierarchy — the wrong story for a collectively operated network, and not even
the right diagram for a DAG. Keep the coordinator peripheral. The white halo
stays as an identity marker ("this is your machine"), never as a position.

Every bubble is a machine; every line is one task, drawn from the machine that
produced its input to the machine building on it. Hovering a line highlights it
and shows the task plus the prompt the planner wrote for it; hovering a bubble
shows the machine; the shaded polygon is the run, and shows what was pitched.
Machines off the flow are holding nothing. Machines never connect to each other
directly — the coordinator brokers every handoff — so a line is a dependency,
not a socket. Source it from `observed_placements` plus per-unit `depends_on`
and node assignment (§8.2 — the assignment is still not served).

Layout is a **deterministic grid**, not a force solve: each node carries a
`gx` (pipeline column) and `gy` (lane), and the stage maps them to pixels. The
earlier force-directed version settled differently on each open, which is wrong
for a view that appears on camera during the demo recording.

There are three scales, each answering a different question.

**live** — real: one run, three machines, laid out as the pipeline.

**simulated · 18** — one run at its widest. The planner emits 3–5 subtasks, so
five builders is the ceiling on what a single run can occupy, and the fifteen
machines holding nothing (thirteen never touched, two freed after wave 1) are
the honest consequence. Do not widen a run past the
plan bound to make the picture look busier.

**swarm · 125** — a field of small independent swarms, each its own coordinator
with its own triangle of handoffs, loose unclaimed machines between them. The
shape follows from two documented facts: a coordinator can keep only about five
machines busy, and it is a single point of failure with no standby — so a large
fleet could not be one swarm, it would have to be many. That requires
federation, which the repo explicitly does not do, and the caption says so.
Your machine carries the ring: one swarm among them, not the centre.

Hover inspection is off on the swarm (`inert: true` on that model). `inert` is
deliberately separate from `fixed` — every scale is grid-placed, only this one
is non-interactive. Conflating the two silently killed hover everywhere once.

The zoom control and drag-to-pan apply a transform on a viewport wrapper and
bypass React; the wheel listener is attached natively with `passive: false`,
since React's synthetic wheel handler is passive and cannot `preventDefault`.

**Rows, not an 8-column table.** A table wins when you scan many rows for an
outlier; you have 2–5 nodes and each is an entity you read individually — whose
machine, what is it building, can I trust it. The live-status line needs room to
be a sentence (`Three failures in a row — no work offered for 45s, then it
retries on its own`), which no column width allows.

Routing weight shows `—  NOT SAMPLED` until `verified_samples > 0`, and the
label underneath carries `agreement_score` and sample count instead of mashing
`0.96 98%/6` into one cell.

### Gallery — `#view-gallery`

`GET /gallery` (viewer). Each card previews **code**, not prose — the artifact
is the interesting thing, and it differentiates Gallery from Runs. Source the
preview from the manifest's primary entry rather than a `code_files[]` path.
Card ratings use the §7 vocabulary (`CHECKS PASSED` / `PARTIAL`), not `PASS`.

### Projects — `#view-projects`

`GET /projects`, `POST /projects` (viewer). **Show the iteration chain, not a
count.** A project's whole point is that it remembers what it already built —
rendering that as a number in an ITERATIONS column hides the feature that makes
projects worth having. Each card lists its iterations (task, outcome, age) and
one line describing what `memory.md` is carrying forward.

**The composer treats strategy and project as one decision.** Only DAG accepts
`project_id`; ensemble and direct reject it outright rather than accepting it and
ignoring the memory. So picking "several tries" or "one shot" detaches the
project chip in front of the user and says what that costs (memory unread, the
result not filed as an iteration) — the pair can never be assembled into a
request that 422s. Do not implement these as two independent controls that
validate on submit.

### Guild — `#view-guild`

`GET /standings` (viewer). The SHARE bar is `total / max(total)`, client-side.
The column is `POINTS`, and the footnote under the table is not optional — see
§7. The REVIEWS column still needs a `reviews` count in the payload.

### Evals — new view

Reads `evals/results/*/summary.json` — **no endpoint exists.** Add a read-only
`GET /evals` that walks `evals/results/`, or generate a static JSON at eval time
and serve it. All the numbers currently in the design are lifted from
`SPRINT_PHASE2.md` and are real; don't invent new ones.

The 80% target is deliberately **not** a metric card. Showing it as a fourth
number next to the measurements implies it is measurable progress, and the
paragraph directly below then explains that it sits inside the confidence
interval and can be hit or missed by luck. The target is stated in that
paragraph instead, where the caveat travels with it.

### Config — new view

`config.load()`. **Never send secret values** — send only `"set"` / `"off"` per
key. The view now lists three independent authorities plus
`viewer_cookie_secure`, `public_pitch`, and the artifact quota block
(`artifact_max_files`, `artifact_max_file_bytes`,
`artifact_max_aggregate_bytes`, `artifact_retention_seconds`).

The design's `viewerAuth` tweak flips the whole console between configured and
fail-open so both states can be checked without editing config.

### Try — `templates/try.html`

**The copy is load-bearing and was wrong before.** `POST /public/pitch` is
disabled by default and, when enabled, runs the server's own fixed profile: one
`direct` candidate, concurrency 1, `local` placement, `local_only`, no project,
120-second total deadline, 64 KiB output cap. It accepts **only** `task` —
strategy, candidates, placement, project, validators and confidentiality are
rejected rather than honoured. So the page must not promise decomposition,
volunteer machines or a reviewer pass; the design states the four terms above
the input instead.

Admission: 2 requests per source IP per hour, 1 active public execution per
source, 3 globally, 1 global inference slot. The response carries a **one-hour,
node-redacted share token with artifact download enabled** — that is the only
handle the public caller gets, and the page says so.

### Invite — new view

Static copy plus config flags for the warning panel. The consent bullets are
lifted verbatim in substance from `AGENTS.md`; keep them accurate if that file
changes.

---

## 4. Viewer auth in the client

Three transports, all equivalent: `X-Viewer-Key`, `Authorization: Bearer`, or a
signed HttpOnly cookie from `POST /v1/viewer/session` (8-hour default, clamped
1 minute – 7 days). `DELETE /v1/viewer/session` logs out. Sessions are
stateless — there is no per-session revocation, so **rotating `viewer_key` is
the only way to invalidate outstanding cookies**, and it invalidates all of them.

For a browser page, use the cookie: the static key should never be in page JS.

What the UI must handle:

- **`401` on any private fetch** → show the locked screen (designed: `viewerAuth: 'locked'` in the design file), exchange the key at `/v1/viewer/session`, retry. `WWW-Authenticate: Bearer` is set. It must read as *locked*, not broken — the screen states the server is up and shows the public health line.
- **`4401` close on `/ws/events`** → same screen; don't silently reconnect-loop.
- **`private_routes_protected: false`** → show the fail-open banner. The design
  puts it on every console view, suppressed on Config where the more actionable
  version already lives. It is not decoration: in this state the server is
  serving tasks, results, projects and machine records to anyone who can reach
  the address, on purpose, for local development.
- **`403` on a share artifact route** → that share was created without file
  access. Distinct from `404`.

Do not use viewer auth as a substitute for the protocol credentials. Submitting
still needs `pitch_key`; node registration and polling still need
`node_secret`. If either is empty, its route is open even when viewer protection
is on.

---

## 5. Sharing — replaces the old share-page finding

The previous revision of this file flagged `GET /share/{timestamp}` in
`routes_history.py` for building its own HTML with a hardcoded neon palette
(`#00FF88`, `#00FFAA`, `#E8FF47`, Segoe UI, 16px radius) that `test_theme.py`
never covered. **The API side of that is now resolved differently:** public
sharing is an explicit capability, not a permalink on a run id, and legacy
`/share/*` is a viewer-protected redirect.

Create:

```http
POST /v1/executions/{execution_id}/shares
X-Viewer-Key: …

{ "expires_in_seconds": 604800, "allow_artifact_download": false,
  "redact_node_identity": true, "include_candidate_details": false }
```

`expires_in_seconds` is 60s–30d, `null` means never, default 7 days. The token
is 32 random bytes, **returned only in this response** — SQLite stores its
SHA-256. Revoke with `DELETE /v1/executions/{id}/shares/{share_id}`.

The design's share dialog maps 1:1 onto those four fields and says the token is
shown once. Keep that sentence; it is the whole reason the dialog exists.

**Shares are now administrable, and the dialog shows it.** Viewer routes list
active share metadata (share ID, created / expires / revoked / last-accessed,
and the artifact / node / candidate flags) with **no token**, revoke one, or
revoke all. The design lists them under the create form with a per-row `revoke`
and a `revoke all`, and states that revoking closes future reads but cannot
take back a page someone already opened. The list is metadata-only by
construction — there is no token to leak, because the server kept a hash.

Public read is `GET /v1/shares/{token}` → `PublicExecutionShareV1`, an
allowlist-built projection. It contains task text and a bounded output preview
because that is the point; it omits job/project ids, filesystem paths, attempt
ids, nonces, credit detail, private telemetry, raw logs and unbounded validator
diagnostics. Invalid, expired and revoked tokens deliberately return the **same
`404` shape** — don't distinguish them in the UI, because the server won't.

Shares are live views, not snapshots: revocation stops future reads but cannot
retract what someone copied, and artifact retention can remove files while the
share itself stays readable (artifact routes then `404`). The design says both.

Port work still needed: move the page to `templates/share.html` rendered through
`dashboard._page()` so it inherits tokens, add `"share.html"` to `PAGES` /
`ROUTES` in `test_theme.py`, and keep the OG/Twitter meta tags. The redesigned
card is in the design file under the `share` page.

---

## 5.5 The two server-rendered pages this file had missed

`templates/run.html` and `templates/status.html` exist on `master` and had no
design. They are not detail views of the dashboard — they are the pages people
land on from a link, rendered on the server precisely because an OpenGraph
crawler does not run JavaScript. Designs are now in the design file:
`status ↗` and `machine ↗` in the sidebar footer, or the command palette.

### `/status` — the public page (new design)

This is the only console-adjacent page whose job is to answer a stranger, and
the access class has to match that job. **Today it does not:** the viewer
middleware is deny-by-default and `/status` is not in `_PUBLIC_EXACT`, so with
`viewer_key` set the page a stranger is meant to check returns a bare `401`
JSON body. Two things follow, in this order:

1. **Redact the page first.** As written, `routes_status.py` renders a table of
   every connected machine (`node_id`, model, platform, tasks, credits) and a
   recent-work table carrying **task text** and a link to each run page. The
   redesign replaces both: counts only, a machine *count* with the per-machine
   detail named as private, and a recent-work list of outcome + assurance +
   placement + age with no task text and no run links. Task descriptions are
   somebody's writing about work they wanted done, and a run is publishable
   only through a share capability someone deliberately created (§5).
2. **Then add `("GET", "/status")` to `_PUBLIC_EXACT`** — one line, and the
   only security decision in this section. It is defensible because the
   redacted page carries strictly less than `/status.json`, which is already
   public and has a test asserting it leaks nothing. Do it in the other order
   and one commit publishes every machine name and every task title.

The figures on the redesigned page are the ones already public in
`/status.json` plus two derived counts (contributors, subtasks executed) that
come from `/standings`. `_built_since()` is correct to count run directories
rather than the ledger — `get_history()`'s default limit is why `/status.json`
has been reporting a capped figure.

### `/node/{id}` — one machine's page (new design)

Stays viewer-gated; the design says so with an `operator page` chip in the
header. It renders hostname, CPU, GPU, RAM, enrolment id and `current_task`,
which is exactly the material `/nodes` is protected for — do not add this path
to the allowlist. `README.md` already calls it a "Private machine contribution
page"; the middleware agrees, and this section exists so a later refactor does
not quietly disagree with both.

Two changes from the current template: the column is `POINTS`, not `CREDITS`
(§7), and the points footnote from Guild travels with it — a page whose whole
subject is what one machine earned is the last place to drop the sentence
saying what earning does not mean. A disconnected machine keeps its page and
its ledger; the design's hardware block says hardware is only knowable while
connected rather than printing dashes.

### `/run/{id}` — restyle against run detail, don't redesign it

`run.html` predates the RC1 work and shows it: one `rating` badge
(`is-pass` / `is-needs-work` / `is-fail`), extracted-file chips, a `Credits
settled` section, and no lifecycle / validation / assurance triad, no manifest,
no integrity chip, no role split. Port run detail's structure into it (§3, §6)
and keep the page's own good instincts: it degrades without JS, it is readable
at 380px, and its `.unrecorded` block is the right pattern for a field that did
not exist when the run was recorded.

Its footer currently reads "Built by a swarm of ordinary computers running
local models" — three words of that are prohibited language (§7) shipping in a
template today. Replace with: _Built with Mycelium — local models on computers
you trust._

## 6. Artifacts — sealed and role-scoped

`ArtifactManifestV1` is the only artifact contract the UI should know:
`execution_id`, `created_at`, `file_count`, `aggregate_size_bytes`,
`integrity_mode`, `sealed_manifest_hash`, and `entries[]` of `relative_path`,
`role`, `media_type`, `size_bytes`, `sha256`, optional `source_candidate_id` /
`source_execution_unit_id`, `created_at`.

**Two lists, two endpoints — keep them separate.** `artifact_manifest_url` and
`/download` are deliverables only, and that is the default. Audit material
(`role=audit`, `audit_manifest_url`, `/audit-download`) has to be asked for by
name. The design renders this as two labelled groups inside one panel, each
with its own download link, precisely so a handoff cannot quietly carry the
run's own paperwork. Do not merge them back into a single `Download .zip`;
that button is the thing that was wrong.

**Role is a rendered field, not a filename heuristic.** `deliverable`,
`provenance`, `log`, `candidate_source`, `internal`. Never infer role from the
extension or the path — `plan.json` is provenance because the manifest says so.

**`integrity_mode` is a state with five values** — `none`, `active`, `sealed`,
`legacy_live`, `invalid` — and the design shows it as a chip next to the panel
title. `legacy_live` must never render as sealed: it is a root from before
sealing existed, rescanned on every read, and the design's caption says that in
words. The `integrityMode` tweak flips between the two so the wrong one is
checkable.

**`sealed_manifest_hash` is labelled as what it is.** Local sealed-baseline
integrity. Not a signature, not an independent timestamp, not provenance
attestation, and no defence against a host that can change SQLite and the files
together. The design's line under the hash states all four exclusions; it is
the sentence most likely to be trimmed in a port, and it is the reason the hash
is allowed on screen at all.

- **Never render a server path.** The internal root is not in either public
  model, and authenticated legacy payloads may still carry `project_dir` —
  don't surface it. `output_reference` points at the artifact API.
- Every read re-hashes the live bytes against the sealed row. Drift, a missing
  file, a symlink and a traversal all fail closed — `409` on integrity, not a
  partial file. Reads never rewrite a sealed baseline.
- `413` means a quota was hit (file count, single file, or aggregate). `400` is
  a rejected path and is deliberately generic. `404` is unknown execution or
  missing entry.
- **Share manifests are filtered by role.** Deliverables by default;
  `candidate_source` only with candidate detail on; `provenance`, `log` and
  `internal` never. No-winner candidate entries are excluded outright. The
  design shows per-entry whether a link would carry it, and the
  `candidate_source` row's answer is wired live to the share dialog's toggle —
  that pairing is the only place you can check before sending.
- Terminal artifact delivery needs a committed terminal execution (§6.5), and a
  sealed root additionally needs its manifest hash to match the one bound into
  that snapshot. A run whose terminal state did not commit has no downloadable
  files, by design.

## 6.5 Durable commit, replay, and one coordinator

Three RC1/Theme-1 invariants the client cannot paper over.

**Nothing is announced before it is written.** Queued, running and terminal
snapshots commit to SQLite before the live cache, the lifecycle event, the
callback, the response or any artifact/share read. When that commit permanently
fails, the active HTTP boundary returns `503` with
`detail.code=execution_persistence_unavailable` and the server does **not**
claim the transition. The design has this as a first-class state — the
`persistenceDegraded` tweak raises a banner on every console view saying that a
run counts once it is written down, that pitching answers `503`, and that
finished work stays readable at its last written state. Do not render this as a
generic network error or a spinner; it is the one failure where the truthful
message is "the server is refusing to tell you something it cannot back up."

**A keyed pitch can be replayed.** `Idempotency-Key` (1–128 printable ASCII) on
`POST /v1/executions`; a matching retry returns the existing execution with
`Idempotency-Replayed: true` and schedules nothing, a changed request returns
`409 idempotency_conflict`, an invalid key `422 invalid_idempotency_key`. The
design's `submission` tweak shows the replay case as a line on run detail: this
run was returned, not re-run. Never label it exactly-once execution or
resumption — replaying an interrupted execution returns the interrupted run, it
does not restart the lost work.

**One coordinator, one state directory.** An OS advisory lock is taken before
migrations or background work; a second process fails closed and names the
holder. Multi-worker launches are rejected at startup. `/v1/operator/health`
(viewer) carries instance ID, deployment mode, lock state and preflight
warnings. The design surfaces these in Config as a `DEPLOYMENT & OWNERSHIP`
group — `deployment_mode`, `state_dir`, `coordinator_lock`, `workers`,
`preflight`, `last_backup` — with `workers: 1` annotated as not a tuning knob.
`deployment_mode: local` renders in `--warn`: it is the compatibility default
and it is not safe for a reachable address.

**The durability boundary is drawn on screen, not just documented.** Config ends
with two lists — what is written down (runs, verdicts, sealed file lists,
leases, receipts, points, enrolments, share hashes, idempotency mappings) and
what is gone on restart (the queue, anything mid-flight, worker sessions, live
model calls, open streams, the connected-node registry). It is there because
"interrupted" only reads as honest rather than broken if the user has been told
once that nothing resumes. Keep both columns; a single reassuring "your data is
safe" line is the failure mode.

Backups are explicit, unscheduled, and validated before they mutate anything.
The design says `last_backup: never` rather than hiding the row, because that is
the true state of a fresh install.

---

## 7. Language rules

`HANDOFF.md` fixes the vocabulary. These are the substitutions the design makes;
keep them through the port, because each one is a claim the system cannot back.

| Don't say | Say |
|---|---|
| `PASS` | lifecycle `completed`, and separately `checks passed` |
| "working code", "it runs" | "structural checks passed — extracts, parses, imports" |
| "verified" as a bare word | one of the five assurance labels below |
| "volunteer" or "anonymous" machines | "invited machines", "computers you trust" |
| "every request is split into pieces" | only the planning strategy decomposes; the other two generate complete attempts |
| "no cloud, no API keys" | local Ollama is the default; an external OpenAI-compatible provider is optional and off |
| a single `Download .zip` | deliverables by default, audit records asked for by name |
| "the file hash proves it" | "local sealed-baseline integrity" — not a signature or attestation |
| "it won't run twice" | a matching keyed retry returns the same run; it is not exactly-once |

**The five assurance labels, and nothing else:** `Not checked`,
`Structure checked`, `Contract validated`, `Behavior tested`, `AI reviewed`.
Each maps to evidence that actually ran. The old `structural / deterministic /
model_judged` wording is superseded — the design already uses the new set in
Runs, run detail and Gallery.

| "worker identity verified" | "accepted from an active server-issued attempt" |
| credits, earnings, payment | **compute contribution points** |
| "public link to this run" | "shared with an explicit expiring, revocable link" |
| a filesystem path | "available through authenticated download" |
| "private" unconditionally | "private when viewer auth is configured" + the health warning |
| "a swarm of ordinary computers" (`run.html` footer, today) | "local models on computers you trust" |
| "volunteer machines" (`status.html` empty state, today) | "invited machines" |
| `CREDITS` as a column (`status.html`, `/node/{id}`) | `POINTS` |

**Approved primary statement** (landing page, verbatim in substance): _run
auditable local-AI jobs across computers you trust_. Supporting: break work into
coordinated components or generate multiple complete attempts; Mycelium
dispatches work to local models, applies explicit checks, and records how each
result was produced. The previous hero ("a swarm of ordinary computers", "no
cloud, no API keys") made three prohibited claims at once and has been replaced
in the design — but the same phrase still ships in `run.html`'s footer (§5.5).

Points mean a nonempty, attempt-bound worker result was accepted. They do not
mean the candidate was selected, that validation passed, or that the output is
correct — and they are not money, a token, or a claim on future value. The Guild
footnote states this; don't trim it to fit.

Also still prohibited anywhere in the UI: public-network readiness,
trustlessness, permissionless nodes, cryptographic worker identity, confidential
compute, enforced no-network execution, sandboxed generated code, Sybil
resistance, durable queue resume, multi-user authorization, **multi-coordinator
operation, host-independent artifact attestation, exactly-once external side
effects, and open-mode peer identity**. `network_policy` is
**recorded intent, not enforcement** — never present it as a security boundary.

---

### Error and degraded states the client must handle

The design has a screen or a state for each:
`401` private HTTP → locked screen; `4401` on `/ws/events` → same screen;
`503 execution_persistence_unavailable` → the degraded banner (§6.5);
`409 idempotency_conflict` and `409` artifact-integrity → refuse rather than
show a partial result; `413` → a quota, named; `422 invalid_idempotency_key`;
`429` rate limits; uniform public-share `404`.

## 8. Known gaps in the API

The design shows a few things nothing serves yet. Extend the endpoint or drop
the element — don't fake it.

1. **Per-run wall-clock duration** — the `WALL` column in Runs and the metric in
   run detail. Add a duration to the history/execution payload, or drop it.
2. **Per-subtask node assignment and duration** — the wave cards' `node` /
   `wall` lines. They exist in `full_log.json`. The wave grouping itself works
   from `depends_on` alone, so the cards degrade cleanly without them.
3. **Project `updated_at`** — the `last run` line. Derive it from the newest
   iteration instead.
4. **Eval results** — no endpoint at all; see the Evals view.
5. **Reviews count** in `/standings` — the REVIEWS column.
6. **Which door a pitch came through.** Nothing records it. Four surfaces can
   submit work — this console, the MCP server, `cli.py`, and the public `/try`
   endpoint — and a run from any of them is indistinguishable from a run Jett
   typed himself. The design therefore has **no** origin column, and the
   Connect view says the absence out loud rather than implying a filter that
   cannot exist. To close it: record the submitting surface on the execution
   at canonical submission (`/pitch/async` already knows whether a pitch key
   was presented) and it becomes one facet in Runs. Until then, a `pitch_key`
   shared with a teammate's AI app produces compute spend with no attribution.
   That is the gap most worth closing of the six.

Closed since the last revision: artifact roles, manifest integrity mode, the
sealed hash, the audit download, share listing and revocation, and
`/v1/operator/health` are all served now and the design reads them rather than
inventing them.
7. **The provenance envelope has no HTTP route.** `provenance.py` stores one
   envelope per execution, append-only, keyed by execution ID, with a
   deterministic digest and an offline checker. `ProvenanceEnvelopeStore.get()`
   exists and nothing calls it over HTTP — the only way a person sees an
   envelope is to download the **audit** bundle and open
   `mycelium-provenance.json` inside the zip.
   *Extend:* `GET /v1/executions/{id}/provenance`, viewer-gated, returning
   `as_export()` — the same shape the bundle already carries, so no new
   contract and the offline checker keeps working unchanged.
   *Or drop:* keep the panel to the one-line summary built from what
   `/v1/executions/{id}` already serves, and make "Open the envelope" the
   audit-bundle download.
8. **Chain verification has no HTTP route, and `/ledger` drops the chain
   columns.** `verify_ledger_chain()` returns first-break index, entry ID,
   reason and both digests, and `as_dict()` is already the right response
   shape; its only caller is `scripts/ledger_chain_admin.py verify`. `/ledger`
   projects entries without `entry_index`, `previous_digest` or
   `entry_digest`, so a client cannot walk the chain itself either.
   *Extend:* `GET /v1/operator/ledger-chain`, viewer-gated, returning
   `as_dict()`. It is content-free by construction — no prompts, outputs or
   credentials live in the chained columns — so it leaks nothing `/nodes` does
   not.
   *Or drop:* show no chain state in the console and let Guild carry one line
   saying verification is a command an operator runs, naming it.

---

## 9. Other things worth fixing while you're in there

- **Duplicate headings.** `dashboard.html` renders "Runs" then "History",
  "Network Nodes" twice, and "Projects" twice, because each view got a
  `.view-head` when it already had an `<h2>`. Fixed in the design: the top bar
  is the *only* place a view is named. It carries title + subtitle, and each
  view begins straight into content or a slim toolbar. That is also the answer
  to "should there be a top bar" — there is one, it is contextual rather than
  global, and it now earns its height instead of repeating the view name that
  sits 14px below it. A second full-width global bar would only be justified by
  an org / environment / region switcher, and Mycelium has no such concept.
- **`.stats-bar` geometry.** It carries `padding: 16px 32px` inside `.content`,
  which already has `padding: 20px 28px`, so the strip is inset from the
  content it belongs to and overflows on narrow windows. Both rules now live in
  `_dashboard.css`.
- **`.content` was declared twice** in `dashboard.html`; the CSS extraction
  moved both into `_dashboard.css` and they are still both there. Collapse
  them — the second wins today, which is not a decision anyone made.
- **The two new pages each carry their own `<style>` block** (`run.html`,
  `status.html`) rather than using `_dashboard.css`. That is correct — they are
  not the app shell — but it means three places now define a table, a badge and
  a code block. When porting, keep the design file's values in all three rather
  than letting them drift.
- **`focusPitch()` calls `scrollIntoView`.** Works, but it's the one place the
  dashboard scrolls the window out from under a live log.
- **`--accent-dim` is defined twice** in the light block of the current
  `_theme.html` (`#15703015` then `#157030`) — the first is a typo'd 8-digit
  hex. Fixed in the replacement.

---

## 10. Deliberate non-goals

- **Config stays read-only.** Making it writable means the browser can write
  `config.json`, which is a bigger door than this threat model wants open —
  more so now that one of those values is the key gating every private read.
- **No login for people, only for the instance.** There is one shared viewer
  role: no accounts, no per-project ACL, no per-execution owner, no session
  list, no individual revocation. Don't design UI that implies otherwise —
  no "shared with", no avatars, no per-user history.
- **No new colours for decoration.** If something needs to stand out and isn't
  a state, it should get weight or space, not hue.
- **The demo flourishes are toned down, not removed.** `video-setup.md` shows
  the recording is terminal-first (`cli.py --demo-live`, full-screen terminal,
  1920×1080) and the dashboard's on-camera job is the "2 nodes connected"
  verification glance. So node state stays unmistakable — a filled square plus
  a colour plus a word — but the 22px glow pulse and the credit-pop animation
  are gone. The smallest type in the console is 11px for that same reason.

---

## 11. Checks after porting

```
py -m pytest -q          # theme tests are the relevant ones
ruff check .
py -c "from server import app; print('ok')"
py status.py
```

Then, with `viewer_key` **set**:

- open `/dashboard`, `/`, `/try`, `/status`, one `/run/<id>`, one `/node/<id>`
  and one `/v1/shares/<token>` in both themes and
  confirm no element disappears in light mode. That is the failure this whole
  token layer exists to prevent: a hardcoded colour is invisible in dark mode,
  because it was picked for dark mode, and only breaks in light;
- confirm a private page with no credential renders the locked screen rather
  than a broken shell, and that `/ws/events` closing `4401` does the same;
- confirm `/health` reports `private_routes_protected: true` and the banner is
  gone;
- with no credential, confirm `/status` renders the redacted public page and
  that it contains no machine name, no hostname and no task text — grep the
  response, don't eyeball it — and that `/node/<id>` and `/run/<id>` still
  `401`.

Then with `viewer_key` **empty**, confirm the fail-open banner appears on every
console view except Config, and that Config's own banner names all three keys.
The banner's wording and placement are unchanged by §12; only its derivation
moves — it becomes the `gate` facet of one status model rather than its own
read of `/health`.

---

## 12. One status model, the status bar, and the work card

Design: `Mycelium Status System.dc.html` (this folder's sibling in the project).
Both themes are rendered side by side in that file on purpose — a colour picked
for dark is invisible only in light, and that is the failure the token layer
exists to catch.

### 12.1 Four derivations become one

Today four indicators each decide for themselves what is true: the fail-open
banner (`/health.private_routes_protected`), the persistence-degraded banner (a
`503` from a write), the locked screen (a `401` on a fetch) and the rail's
inference line (`/health.ollama`). They can disagree, and three of them never
look at whether the poll succeeded at all — which is why `_dashboard.js` paints
from the last successful poll and reports a coordinator that died five minutes
ago as connected.

One poller, four facets, three values each:

| Facet | good | bad | not heard back | read from |
|---|---|---|---|---|
| `link` | `up` | `refused` | `silent` | the poll itself |
| `inference` | `ready` | `offline` | `unheard` | `GET /health` · `ollama` |
| `commit` | `durable` | `unavailable` | `unheard` | `503 execution_persistence_unavailable` on a write |
| `gate` | `protected` | `open` | `unheard` | `GET /health` · `private_routes_protected` |

**Dependency rule.** When `link` leaves `up`, every facet the coordinator serves
goes to not-heard-back — never to bad. A silent coordinator cannot report that
inference is down; saying so is inventing the answer.

**Hysteresis.** Poll `/health` every 5 s and `/v1/operator/health` every 30 s.

| Transition | Condition |
|---|---|
| good → bad (a probe) | two consecutive polls saying no — `/health`'s Ollama probe has a 5 s timeout and one miss is not an outage |
| good → bad (a statement) | one poll, when the payload states it: `private_routes_protected: false`, or a `503` from a write |
| good → unknown | two consecutive misses, or 15 s since the last answer, whichever is first |
| bad/unknown → good | two consecutive good polls; one good poll reads `recovering`, never green |
| unknown → banner | 60 s with no answer |

On load the state is not-heard-back, **never** ok. Green is never a cached
value: a filled lamp requires a confirmation from this poll or the last, and the
age of that confirmation is on screen at all times.

**Severity — worst facet wins, and the word is the condition's own name.** The
indicator never says "degraded", which says nothing: `unprotected` →
`not committing` → `inference offline` → `no answer` → `recovering` → `ok`.
`unprotected` ranks first because it is the one state in which the console looks
perfectly healthy and is not.

**Shape carries the third value.** Filled square = confirmed this poll; hollow
square = no answer. Colour says which answer. So not-heard-back can never be
mistaken for good, in greyscale, on camera, or by a reader who does not see
green.

**Which states get words.** A banner appears only when the reader's next action
will fail or when something is true they cannot infer from a lamp: `gate open`
(every view), `commit unavailable` (every view), `inference offline` (Overview
and Try only — elsewhere the bar carries it), and 60 s of silence. Deliberately
no banner for a single missed poll, for an expired session (the locked screen
already says it, and it is about this browser rather than the coordinator), or
for tracing/evidence being off — both are defaults, not faults.

**Outside the model.** The viewer session is not a health facet: a `401` says
the coordinator will not answer *you*. And the coordinator lock is held for the
life of the process, so a running console can never observe it lost — it is an
at-load fact, and the two-coordinator case surfaces as
`/v1/operator/health.preflight_warnings` instead.

### 12.2 The status bar

30px, pinned to the bottom of the console shell, spanning rail and view, under
every console page (Docker Desktop's placement). The rail's own inference line
is **deleted**, not duplicated.

Two classes of cell, told apart by whether they carry a lamp:

| Cell | Served by | Lamp |
|---|---|---|
| lamp + one word | derived (§12.1) | — |
| `INFERENCE ready · qwen3.5:4b` | `/health.ollama` + config `model` | yes |
| `NODES` | `/health.nodes_online` | yes |
| `RUNNING` | `/metrics.jobs_running` | yes |
| `QUEUED` | `/metrics.jobs_queued` | yes |
| `MODE` | `/v1/operator/health.deployment_mode` | no |
| `LOCK` | `/v1/operator/health.single_coordinator_lock` | no |
| `TRACING` | config `tracing_enabled` / `tracing_export` | no |
| `EVIDENCE` | config `capability_evidence_mode` | no |
| `as of Ns` | age of the last successful poll | — |

Three traps in that table:

- **The model name is the configured one**, read at load. `/health.models` is
  every model installed on the host and `/status.json.model` is whichever tag
  came back first; neither is the model this coordinator will use. When
  inference is offline the cell still names the configured model, which is a
  config fact — it never implies a model is loaded.
- **`RUNNING` and `QUEUED` are jobs**, from `/metrics`. `/health.tasks_pending`
  is the subtask queue and is a different number; rendering it under `QUEUED`
  would be wrong.
- **A cell with no lamp is making no claim about this second.** `TRACING` has
  three states in `tracing.py` — `off`, `propagating` (enabled, no SDK) and
  `exporting` — and the bar shows which, not a boolean.

**No CPU or memory graph.** A node's capability descriptor *claims* CPU count,
physical memory and GPU at registration; nothing measures them afterwards and no
endpoint serves a sample. A load line drawn from a claim is a drawing.

**Ambient indicator.** The bar collapsed: one lamp, one word, one age, same
derived state, 28px in the console and 44px on the public pages, which is where
most visitors are on a phone. It is the only status surface the public pages get.

### 12.3 The work card

For Gallery and Projects. Title, status chip, hardware badge, age — and cards
stack **full width in one column**, not in a grid: one run is usually active, so
a tiled grid is mostly tiles of nothing.

| Chip | From | Means |
|---|---|---|
| `PASS` | `rating: PASS` | reviewer passed it, mechanical check found no defects |
| `NEEDS WORK` | `rating: NEEDS_WORK` | returned with named problems; still downloadable |
| `FAIL` | `rating: FAIL` | terminal without a usable deliverable |
| `UNCHECKED` | `code_precheck_error` set | the mechanical check did not reach a verdict — **not** a pass |
| `NO VERDICT` | `rating: "?"` | nothing recorded; says nothing about the work |

`UNCHECKED` is the third state the delta's §5 asks for: an empty `code_problems`
list beside a `code_precheck_error` means "not checked", not "checked clean", and
`_dashboard.js` reads neither field today. Five values and no sixth — no
percentage, no score, and agreement is never presented as correctness.

**The hardware badge degrades in three steps** rather than guessing:

1. `8 CPU · 16 GB · Apple M2` — work a machine is holding **now**. The node
   reports its `current_task`, so the descriptor is read in the direction the API
   actually serves: machine → task. Claimed at registration, never measured.
2. `3 machines` / `this machine` — finished work, from `mode` and `nodes_used`.
   Placement and a count is all that is recorded.
3. `machine not recorded` — finished distributed work with no node list. Say it
   in words; do not borrow a plausible machine from `/nodes`.

Gallery cards carry the code file names (`code_files`) as the last line, because
the artifact is the point and it is what separates Gallery from Runs. Projects
cards replace the age line with the iteration chain — memory across runs is the
feature, and a count hides it.

### 12.4 Gaps this adds to §8

- **Per-unit node assignment** (already §8.2) now has a visible consequence: the
  badge in step 2 above. Extend the run record with the node that ran each unit,
  or keep the degraded badge.
- **Projects have no `updated_at`.** The card's age line is derived from the
  newest `/history` run carrying that `project_id`, and the design labels it as
  derived. Add `updated_at`, or keep the label.
- **Tracing and evidence are config-only.** No endpoint serves either, so both
  cells are read at page load and carry no lamp. If they should be live, add
  them to `/v1/operator/health`; do not invent a poll.

### 12.5 Checks specific to this section

- **Pull the plug.** With the console open, stop `uvicorn`. The bar must hold
  its values and its green lamp for one missed poll, go hollow within 15 s, and
  raise the silence banner at 60 s. If it is still green after a minute, the old
  derivation is still in there.
- **Restart it.** The lamp must read `recovering` for one poll before it returns
  to `ok`.
- **Empty `viewer_key`.** The lamp reads `unprotected` — not `no answer`, and
  not `ok`.
- **Both themes, every state**, per §11's first bullet.
- **Grep the rendered console for `%`.** No status surface may render a quality
  percentage.

---

## 13. Run detail, the provenance envelope, and the ledger chain

Design: `Mycelium Run Detail.dc.html` (this project), with three child
components: `Run Detail Surface.dc.html`, `Provenance Envelope.dc.html`,
`Ledger Chain.dc.html`. Both themes render from the same markup throughout.

Audited against `master`: `provenance.py`, `ledger.py`, `routes_access.py`,
`routes_history.py`, `routes_run.py`, `access_control.py`,
`docs/adr/0017-*`, and `docs/design/HANDOFF-DELTA.md`. Where the delta and the
archived `console-2026-08-28/` handoff disagree, the delta wins; where source
and the delta disagree, source wins and it is written down here.

### 13.1 One structure, three surfaces

The dashboard's run modal, the console's `#view-run`, and the server-rendered
`/run/{id}` are **one structure in three shells** — §5.5 says port this
structure into `templates/run.html` rather than inventing a parallel layout.

Order, unchanged from the August direction: **deliverable first, then the plan.**
Plan is process; the code is the product. The plan still sits directly beneath
it, because the waves are the parallelism claim.

Three differences on the server-rendered page, all forced by the medium:

1. **Nothing polls.** No client, so the page is a snapshot. It must never render
   a live cell or a relative age that stops being true; a run still going
   renders as still going with its own timestamp, not "2h 14m ago".
2. **It is shareable.** It carries an OG title and description, so the
   envelope's one-line summary has to survive being pasted with no page around
   it. That constraint is what chose the sentence in §13.3.
3. **It is indexed by nobody.** Viewer-gated like the rest — no sitemap entry,
   no assumption a crawler ever resolves it. It is a permalink for a person who
   was given the link, not a public record.

Controls are 44px on the server-rendered page (most visitors arrive on a phone
from a link in a post) and compact in the console, where the pointer is a mouse
and vertical space is the scarce thing.

### 13.2 The two claims must not read as one

| | Sealed manifest | Provenance envelope |
| --- | --- | --- |
| Establishes | these bytes are the bytes frozen when the run ended, recorded locally | who produced them, under which enrolled identity, with which model, which validators ran |
| Does not | say who made them, or whether they are right | say whether they are right; it is **not** a signature and **not** an attestation |
| Varies | `sealed` · `legacy_live` — and which one matters | present or absent; when present, the contents matter and the presence does not |
| Drawn as | a chip in the panel header, accent when sealed | a **sentence** — no chip, no lamp, no colour |
| Checkable | re-hashed against the sealed row on every read, by this coordinator | recomputable offline from the audit bundle with no coordinator, network or credential |

Three rules follow, and they are the whole of this section:

- **Different grammatical class, not a second badge.** Two chips side by side is
  a row of ticks, and ticks get counted. The manifest gets a chip because its
  state genuinely varies; the envelope gets a sentence.
- **No group header over both.** There is no `INTEGRITY` panel and no `TRUST`
  section anywhere in this design. A shared header is precisely the thing that
  invites a reader to add up what sits under it.
- **They vary independently.** A sealed manifest with a legacy producer and no
  model digest is an ordinary run; so is an envelope over a `legacy_live` file
  list. Neither is evidence for the other.

### 13.3 The provenance envelope

~20 identity fields, and almost no reader wants twenty fields. **Default shows a
sentence and a count of what is missing**; everything else is one disclosure
away.

The one line, which is most of the design work because it is the only part most
readers ever see:

> Built by 1 enrolled machine on `qwen3.5:4b`, checked by 2 validators. Binds
> who produced these files — not whether they are right.

Producer, model and checking in the first clause; the limit in the second, in
the same breath rather than in a tooltip. Rejected: anything with a tick
("PROVENANCE ✓" — two prohibited words in five characters); anything with
"signed" (nothing is signed); and `1 producer · 2 validators · 4 unknown`,
which is accurate, unreadable, and makes the reader do the interpreting.

The panel label is **`PRODUCED BY`**, not `PROVENANCE`. Most readers have met
the second word in a supply-chain context where it means signed and
third-party-checked. This is neither.

**Unknown is a value, never a blank.** Hollow marker, muted ink, the field
named, and why. Never a dash; never `--warn` or `--danger` — not writing
something down is neither a fault nor fine, and the *shape* carries it so it
survives greyscale. Keep the repo's three absences distinct rather than
collapsing them: `sampling_parameters` (nothing pinned), `sampling_seed_honoured`
(a seed was set but is not shown to be honoured), `producer_sampling` (a
distributed machine sampled and the worker protocol does not carry it back).

**Plural is the primary case.** The ensemble path settles several accepted
receipts, carries one `producers` entry each, and leaves the singular fields
`null` rather than electing a winner. So `producers` is always a list and one
producer is a list of one. A layout built for the single case would need a rule
for choosing between receipts, and no such rule exists.

The **signature slot is reserved and empty** — a slot, not a feature. No key, no
key management, no transparency log, no third party. Render it as `reserved` in
the opened envelope and nowhere else.

### 13.4 The ledger chain

**Intact is not green.** Green means PASS / connected / ok; an intact chain is
none of those. It means no entry changed *without every link after it also being
recomputed* — which a full rewrite satisfies. The passing state is ordinary ink,
a filled marker, a walked count, and the limit in the same box as the verdict:

> Tamper evidence, not tamper proofing. An operator with write access to this
> database can rewrite every entry **and** every link, and this will then report
> intact. No consensus, no external anchor, nobody outside this machine
> attesting to anything.

That limitation is asserted by a test in the repo rather than admitted in a doc,
so it belongs on screen where the verdict is read — not in a footnote.

Three states, all drawn:

- **Intact** — every entry linked and walked.
- **Genesis boundary** — entries written before the chain existed have no link
  and are never retrofitted with one. Shown unlinked at the head, counted
  separately, and **not a break**.
- **Broken** — verification returns at the first break, so entries past it were
  never checked. They render hollow and muted, labelled `not walked`. Drawing
  them as broken claims more than the walk found; drawing them as intact claims
  the opposite.

The break report is index, entry ID, reason, expected digest, observed digest —
**content-free by construction**, so it is safe to paste into an issue.

### 13.5 Chips, carried in unchanged

The five values from §12.3, with no sixth and no percentage. Worst wins when
both halves speak: `FAIL → NEEDS WORK → NO VERDICT → UNCHECKED → PASS`. A
precheck error can never soften a named negative verdict, and `PASS` requires
both halves of its claim. A record carrying both a runner failure and a problem
list cannot be constructed — `ParsePrecheckResult.__post_init__` raises — so the
ladder only ever resolves a rating against a precheck error.

### 13.6 The modal header bug

Observed and deferred in the Phase 2 port. `.modal-head` is a non-wrapping flex
row with `space-between`; `.modal-title` has `min-width: 0` and no wrapping
rule, so a long unbreakable execution ID overflows its squeezed box while
`.modal-actions` (`flex-shrink: 0`) holds its width. The title runs under the
buttons.

```css
.modal-head    { flex-wrap: wrap; }                /* the row may break     */
.modal-title   { flex: 1 1 200px;                  /* it may claim a line   */
                 overflow-wrap: anywhere; }        /* long ids break        */
.modal-actions { margin-left: auto; }              /* replaces space-between */
```

No geometry change above the breakpoint: with room, `margin-left: auto` puts the
actions exactly where `space-between` put them. Both modals share the rule, so
the node modal is fixed by the same change.

### 13.7 Element → endpoint map

| Element | Source | Note |
| --- | --- | --- |
| Title, task text, timestamps | `GET /v1/executions/{id}` | |
| `lifecycle` / `validation` / `assurance` | same | read separately — the compatibility `status` field flattens *completed+passed* to `completed` and every other completed outcome to `unverified` |
| Verdict chip | `rating` + `code_precheck_error` | five values, §13.5 |
| Placement line | `placement_requested` / `placement_planned` / `mode` | asked → planned → ran |
| Waves | per-unit `depends_on` | grouped by dependency depth; **no** node per unit (§8.2) |
| Unit machine line | — | **unserved.** Renders `machine not recorded` |
| Metric strip | counts from the execution record | counts only — no wall clock (§8.1), no speed multiplier |
| Deliverable preview | `code_files` + artifact read | first deliverable-role file |
| Manifest panel + `SEALED` chip | `ArtifactManifestV1` (§6) | re-hashed on read |
| Deliverable download | `GET /download` | deliverable role only |
| Audit download | `GET /audit-download` | different scope, deliberately separate |
| Provenance summary line | **no route** (§8.7) | built from execution record; full envelope is in the audit zip |
| Provenance full panel | **no route** (§8.7) | `as_export()` if extended |
| Ledger chain panel | **no route** (§8.8) | `verify_ledger_chain().as_dict()` if extended |
| Replay line | `docs/adr/0008`, `execution/idempotency.py` | returned, not re-run |
| Timeline | `full_log.json` in the audit bundle | per-unit timings live here, not in the API |

### 13.8 Overview and Runs — re-audit, not a redraw

Neither is redrawn. Checked against `master` and against the status bar, which
now carries four of the things Overview says.

- **Overview · "running now" — stale citation.** The handoff reads it from
  `/health` as an active execution count; `/health` has no such field. It
  returns `status`, `ollama`, `models`, `nodes_online`, `tasks_pending`,
  `node_enrollment_required`, `private_routes_protected` and `warnings`.
  → read `/metrics.jobs_running`, the same source as the bar's `RUNNING` cell,
  so the two can never disagree. `/health` is public and `/metrics` is
  viewer-gated, which is fine inside the already-gated dashboard.
- **Overview · "queued" — wrong number.** It reads `/health.tasks_pending`,
  which is the *subtask* queue — exactly the trap §12.2 named for the bar's
  `QUEUED` cell. Ported as written, two cells on one screen carry the label
  QUEUED over different numbers. → `/metrics.jobs_queued`, or keep
  `tasks_pending` and relabel the cell `SUBTASKS PENDING`. Not both under one
  word.
- **Overview · the vital strip is now redundant.** Its stated job is answering
  "is it working right now" with nodes, inference, running and queued. The
  status bar carries all four, lamped, on every console page. → drop the strip
  and let Overview lead with what the bar cannot carry: the active run and the
  composer. **Recorded, not drawn** — Overview is its own phase.
- **Overview · pass rate — unchanged.** Still deliberately absent, and the delta
  has since retired the figure outright rather than relocating it.
- **Runs · rating chip — gap closed.** `/history` and `/gallery` now both carry
  `code_precheck_error` per row. All five chips can render in the list today,
  with no endpoint change.
- **Runs · `WALL` column — gap stands.** Still unserved (§8.1). The design drops
  the column, including in run detail.
- **Runs → run detail · defects — port work.** `_dashboard.js` reads
  `code_files` and neither `code_problems` nor `code_precheck_error`, so the
  dashboard shows no defects and no not-checked state — the delta's §5 open gap.
  `routes_run.py` already words the third state correctly; match that wording
  rather than inventing a chip.

### 13.9 Deliberately not drawn

- **Per-unit machine assignment** (§8.2) — waves draw dependency structure and
  decline to place a unit on a machine.
- **Any quality percentage** — retired by the delta §4.1.
- **A signed envelope** — the slot is reserved and empty.
- **Gallery, Projects, Guild, Evals, Config, Connect, the public pages, the
  share card** — out of scope; the work card in §12.3 already covers Gallery and
  Projects.

### 13.10 Checks specific to this section

- **Grep the three surfaces for the banned words**: `verified`, `trustless`,
  `tamper-proof`, `proof of correct execution`, `signature`, `signed`,
  `attestation`, `attested`.
- **Open the run modal at 330px.** Title wraps above the actions; nothing
  overlaps. Same for the node modal.
- **A run with `code_precheck_error` set and an empty problem list** must render
  `UNCHECKED` on the list card *and* in detail — not `PASS` in one and
  `UNCHECKED` in the other.
- **A legacy execution with no envelope** renders the panel absent, not an empty
  panel and not an error.
- **An execution with three accepted receipts** lists three producers and leaves
  `attempt_id` / `receipt_id` / `unit_id` as `not applicable`.
- **Both themes, every state**, per §11's first bullet.

---

## 14. The animated pitch on Overview

Design: the composer and thread in `Mycelium Console.dc.html`, Overview.
Pressing **Pitch** (or a suggestion chip, or Enter) runs the pipeline in the
thread instead of swapping in a finished snapshot.

It is not a progress bar. It is the server's own event sequence — `pitch` →
`plan` → `build` per subtask → `review_start` → `complete` — drawn at the
granularity that sequence implies, and each strategy gets a different **shape**,
because the shape is the explanation:

| Strategy | What the animation shows |
| --- | --- |
| `dag` | planner streams subtask titles, then the plan snaps into waves; wave 1 goes to two machines at once; wave 2 sits visibly **held** on `depends_on [01, 02]` until both land |
| `ensemble` | three whole attempts, no plan; two start at once because two machines are healthy, the third waits for a **free machine** — capacity, not a dependency |
| `direct` | one machine, one attempt, one card, and the note that there is nothing to compare it against |
| no machines connected | same plan, but every piece builds on this machine **in turn** — the parallelism claim withdrawn in front of you, which is the argument for lending a machine |
| Ollama down | accepted, queued, then `PLAN` fails — no verdict, because nothing was produced to judge |
| cancelled | running units read `signalled to stop`, queued units `pulled from the queue`, finished work stays |

The lamp vocabulary is unchanged: waiting is `--border-strong`, building is
`--warn`, built is `--accent`, stopped is `--danger`. Green still means only
PASS / connected / ok — a *built* subtask is `built`, not green-as-approval, and
the verdict line stays `RUNNING` until the checks have actually run.

### 14.1 What the stream carries today

Two transports, both viewer-gated: `GET /events?since=` for history and
`/ws/events` for live (`authorize_viewer_websocket`, close `4401`). A WebSocket
library must be installed or the upgrade 404s and the dashboard silently loses
live updates — `tests/test_runtime_deps.py` already guards this.

The payload allowlist is `_SAFE_EVENT_FIELDS` in `server_state.py`. Newly added
fields stay private until deliberately classified, which is why most of the gaps
below are classification decisions rather than plumbing:

| Event | Fields today |
| --- | --- |
| `pitch` | `trace_id`, `job_id`, `mode` |
| `strategy_selected` | `execution_id`, `strategy_requested`, `strategy_selected` |
| `plan` | `trace_id`, `job_id`, `subtask_count` |
| `build` | `trace_id`, `job_id`, `subtask_id` |
| `node_task_queued` | `trace_id`, `job_id`, `task_id`, `verification` |
| `review_start` | `trace_id`, `job_id` |
| `complete` | `execution_id`, `trace_id`, `job_id`, `project_dir` |
| `error` | `trace_id`, `job_id`, `error_type` |
| `cancelling` / `cancelled` | `dropped`, `still_running` / `stage`, `completed`, `credits` |
| `token` | ephemeral, WebSocket only, never persisted |

Two of those already do real work for this design. `strategy_selected` is why
the header can say what **ran** rather than what was asked — draw
`asked → ran` whenever the two differ. And `cancelling.dropped` /
`still_running` is exactly the split the cancel state draws, so the cancelled
frame needs no new field at all.

### 14.2 Gaps, all of them exposure rather than measurement

1. **`plan` carries `subtask_count` and nothing else.** No titles, no
   `depends_on`, so **the waves cannot be drawn from the stream** — the one
   element the animation is built around. Subtask titles are model output, so
   this is a privacy classification call, not a schema one. Options: add a plan
   digest (`id`, `title`, `depends_on` per subtask) to the `plan` event; or
   serve the plan on a route and fetch it when `plan` arrives; or draw
   `n subtasks · structure pending` until `complete` and accept that the
   centrepiece only appears in retrospect.
2. **`build` carries `subtask_id` with no `node_id`.** So "which machine is
   building this" is unavailable live — §8.2 again, in its live form. Note the
   data exists: `dispatch_results[].node_id` is on the execution result, and
   `participating_nodes`, `units_local` and `units_distributed` are already
   computed in `execution/service.py`. `node_id` is also already in
   `_GENERIC_SAFE_EVENT_FIELDS`, so adding it to `build`'s allowlist is a
   one-line classification. Without it, the cards must read
   `machine not recorded`, matching run detail.
3. **No per-subtask completion or timing event.** `build` fires at start;
   nothing fires at end. Progress bars and per-card durations in this design are
   the animation's own clock. Either emit `build_completed { subtask_id,
   node_id, status }`, or replace the bar with an indeterminate lamp — do **not**
   derive a percentage from elapsed wall time, which is a fabricated number
   wearing a progress bar.
4. **The local fallback is inferred, not read.** The design switches to the
   one-builder shape from `nodes_online == 0`. The real signal is better:
   `fell_back_to_local`, `fallback_count` and `fallback_reason` are on the
   execution record, and `fallback_reason` is a human string already assembled
   from per-unit and placement reasons. Drive the shape off those and the
   console can say *why* it ran locally instead of guessing from a count.
5. **`error.error_type` may not distinguish "model unreachable".** The offline
   frame blames the planner's call to the local model specifically. If
   `error_type` cannot carry that, word it as a planner failure and let the
   status bar's inference lamp carry the cause.

### 14.3 Element → source map

| Element | Source | Note |
| --- | --- | --- |
| Ask bubble, job id | `pitch` event | `mode` gives the strategy chip |
| Strategy header | `strategy_selected` | show `asked → ran` when they differ |
| Stage rail: ACCEPTED | `pitch` | |
| Stage rail: PLAN | `plan` | count only today (§14.2.1) |
| Planner streaming lines | **unserved** (§14.2.1) | drop to a plain "decomposing" row if unavailable |
| Wave grouping | per-unit `depends_on` | same derivation as run detail §13.7 |
| Card machine line | **unserved** (§14.2.2) | degrade to `machine not recorded` |
| Card progress + duration | **unserved** (§14.2.3) | degrade to a building lamp |
| Stage rail: REVIEW | `review_start` | |
| Named checks | `code_precheck_error` + problem list on completion | the three names are the real checks; they resolve at `complete`, not one by one |
| Verdict line | `rating` + `code_precheck_error` | five values, §13.5 |
| Files | `code_files` | |
| Cancel state | `cancelling.dropped` / `still_running` | pulled vs signalled |
| Failure state | `error.error_type` | lifecycle word, no verdict (§14.2.5) |
| SWARM panel live rows | `build` + `node_id` (§14.2.2) | running beats finished, so a machine never reads idle while building |
| Vital strip `RUNNING NOW` | `/metrics.jobs_running` | per §13.8, not `/health` |

### 14.4 The map

Above the stage rail, the thread draws the run as a topology rather than a
list: **your pitch → this machine (planner · reviewer) → the builders**, with a
labelled packet travelling the line it is actually on.

The geometry is the claim, so keep it:

- **Orchestrator left, builders right, and every line runs between the two.**
  There are no builder-to-builder edges, because there is no such path — units
  go out and results come back through the orchestrator. A mesh drawing would
  imply peer-to-peer transfer that does not exist.
- **One box per machine, not per subtask.** The box carries the machine's
  identity, its platform line, what it is holding right now and a bar for that
  unit. Subtask-level structure stays in the wave grid below, where
  `depends_on` can be stated.
- **`this machine` appears once on the left even when it is also the builder**
  (no machines connected). The note says so in words — the box on the right is
  the same computer wearing its builder hat — because drawing one box would
  lose the planner/builder distinction and drawing two unlabelled would imply
  two computers.
- **Line state is the lamp vocabulary, not new colour.** Idle is a sparse dash
  at low opacity, dispatching/building is `--warn` flowing outward, returning is
  `--accent` flowing *inward* (`dashBack`), stopped is `--danger`, settled is a
  solid dim `--accent`. Direction is carried by the animation, not by an
  arrowhead, so there is nothing to mirror in RTL.
- **The stream box under the map is the reasoning, chosen not concatenated.**
  It shows exactly one producer at a time — planner while planning, the builder
  whose unit is furthest along while building, reviewer during review — with its
  name and a progress figure in the header. It reveals a fixed sample
  proportionally to that unit's progress; it is *not* a token feed, and the
  planner sample is the plan JSON it would actually emit (`id`, `title`,
  `depends_on`), which is the same shape §14.2.1 asks the event to carry.
- **A cancelled run does not keep streaming.** The box states that the run was
  signalled and no further output is accepted, rather than freezing mid-token,
  which would read as a hung UI.

Sizes: fixed 524px canvas, centred, horizontal scroll only below that. The box
heights are fixed (`work` is a 32px clamp) so a long subtask title cannot
reflow the topology mid-run. Type floor holds at 11px.

### 14.5 Checks specific to this section

- **Pitch with no machines connected.** Every card must name `this machine`, the
  wave-1 note must say the pieces take turns, and nothing may claim parallelism.
- **Pitch with Ollama down.** Accepted and queued, `PLAN` red, no verdict word,
  no checks row, no files.
- **Cancel mid-wave.** Running units `signalled to stop`, queued units
  `pulled from the queue`, and the already-built units keep their durations.
- **The checks row must not tick one at a time if the API resolves them at
  once** — three chips that resolve together are honest; three that resolve in
  sequence imply per-check timing the server does not report.
- **Playback speed** (`pitchSpeed`) changes pacing only. The durations on the
  cards are the run's own clock and must not scale with it.
- **Both themes, every phase**, per §11.

---

## 15. The Sept 13 softening pass, and the shell it changed

No layout was redrawn and no claim moved. This section records what a porter
must carry across so the ported templates don't read as the older, denser
console. Design: `Mycelium Console.dc.html`. The pre-pass copy is kept as
`Mycelium Console v3 (dense hairline).dc.html` for comparison.

### 15.1 Type: mono is now a data typeface

Mono is for **IDs, numbers, code and paths only**. Uppercase section labels are
the sans at the same size and the same tracking, with weight carrying what
mono's width used to carry. Type floor is unchanged at 11px (§ the on-camera
glance). When porting a template, the test is per-string, not per-element: a
label like `RUNNING NOW` is sans, the `7` under it is mono.

### 15.2 Surfaces separate by fill and lift, not by outline

`--border` and `--border-subtle` both moved closer to their backgrounds, and
cards carry a new `--card-lift` shadow token. Two deliberate exceptions:

- **Table hairlines stay.** At 61 rows they are load-bearing — they are how a
  reader keeps a row.
- **The pitch map's boxes have no idle outline.** Border appears only when it
  carries state: `--warn` while building, `--danger` when stopped. A resting box
  is fill alone. This is why the map reads as a topology rather than a diagram.

All of this is token-level, so it ports through `handoff/_theme.html` rather
than through each template. `tests/test_theme.py::test_no_page_hardcodes_a_colour`
still applies, and `--card-lift` must be added to the theme layer, not inlined.

### 15.3 Vital signs became a line, not four boxes

Overview's four vital signs are **one quiet lowercase line** with the same four
signs and the same lamps — the thread, not the metrics, leads the page. Nodes'
five stats read the same way. Boxed strips remain where the numbers *are* the
content: run detail and evals. Pass rate is still absent from the line (§3,
Overview) and still lives in Evals beside its interval.

### 15.4 The rail starts collapsed

The sidebar mounts **collapsed at 58px, icons only**, with the toggle in its own
header and the state in `railOpen`. Navigation is not the page. Porting notes:

- **Every rail item needs a `title`/`aria-label` in the collapsed state** — the icon
  alone is not a label.
- The rail footer's page switcher (`status ↗`, `machine ↗`) collapses to icons
  too and must keep its 44px hit targets.
- The collapsed rail is the default for a fresh session; persist the user's
  toggle if you persist anything, but do not expand it on first load.

### 15.5 Overview is full-bleed

No page padding on `#view-overview`. The vital line is a bar across the top, the
thread fills the middle, and the right column is a **sidebar with a left border
and no card chrome** — not a stack of cards.

**ACTIVITY leads that sidebar** and is the panel the column opens on, because it
is the live `/ws/events` feed and therefore the one panel that earns a
persistent window; SWARM, the active run and the legend are a tab away (§15.5b).

The other views keep their page padding. Overview is full-bleed because its
content is a stream; nothing else here is.

### 15.5b The right column is a window, not a stack

Overview's right column is a **floating window** — 316px, 16px of air on all
sides, `--border`, 14px radius, `--lift`, its own scroll — and it shows **one
panel at a time**, chosen by tabs that sit **in its 34px title bar**:
`ACTIVITY` · `SWARM` · `RUN` · `HOW`. It used to stack all four, with ACTIVITY
capped at 44vh so the ones below stayed reachable; the cap is gone and the
selected panel takes the whole window.

Tab labels never truncate: the tabs are `flex: 0 0 auto` and the meta is what
yields when the bar is tight. Ellipsised nav labels were the most visible flaw
in the panel while the tabs were allowed to shrink.

The title bar carries the mark, the tabs, the right-aligned live meta
(`live` / `reconnecting`) and a collapse control that shrinks the window to the
bar alone. There is no separate title line — the active tab *is* the title, so
naming it twice was the thing that made this read as a sidebar with a header.

Gutters inside the window are 12px, not the 18px the full-height sidebar used,
and **event rows are stacked** (agent + time on one mono line, message beneath)
rather than a three-column mono grid — at 316px the grid wrapped every message.

**The window is vertically centred and stays centred through scroll.** It is
centred by a sticky full-height wrapper (`top: 0; height: 100%`;
`align-items: center`), never by a transform — sticky cannot clamp a transform,
so `top: 50% + translateY(-50%)` let the window ride up out of its column and
paint over the security notice.

For that to hold at scroll 0, **the shell is the app viewport**: the shell column
is `height: 100vh; overflow: hidden`, the page itself does not scroll, and each
view scrolls inside it (Overview's content row; every other view's `<main>`).
The wrapper's flow origin is then the scrollport top, so the window is centred
in the content area at every scroll position. Don't reintroduce page scroll
here, and don't offset the wrapper by the vital bar's height — it wraps.

**The window is sized by its content**, capped rather than pinned: `height:
auto` with `max-height: 58vh` on ACTIVITY (the only panel that can outgrow it,
and it scrolls inside) and the viewport height on the others. A pinned height
left ~300px of bare fill under the short panels, and a window that reaches both
edges of the viewport is a column again.

The thread column sits on `--bg` with no fill of its own. It previously used
`--bg-elevated`, which ended at the last message rather than at the bottom of
the page — a lighter panel that stops halfway is worse than no panel. Its
context row (project chip, memory note) lost its hairline too, like the top bar.

- **ACTIVITY is the default** — it is the live `/ws/events` feed, so it earns the
  window; it fills the height with its own scroll.
- **A tab whose panel has nothing to say is absent, not empty.** ACTIVITY only
  when the stream is reachable, RUN only when a run is live or inference is
  down (it holds both the active-run panel and the `NO ACTIVE RUN` state), HOW
  behind `showFlowLegend`.
- **The active tab falls back** when its panel disappears mid-session (a run
  ends, the socket drops) — store the choice as a preference and resolve it
  against availability at render, or the column will go blank.
- The panel headers lost the labels the tabs now carry; the right-aligned meta
  (`websocket · live`, the node summary) stays.

The left rail is unchanged by this pass: still a full-height column, still
collapsed by default per §15.4.

### 15.6 Node state has one precedence order

Lamp, bar and edge previously each decided for themselves and could disagree.
One order, worst-first: **stopped → running → waiting → last built**. This is the
same principle as §12.1 (one derivation, many indicators) applied to the node
rows, and it is the rule a porter should implement once and read from three
places.

### 15.7 Checks specific to this section

- **Grep the ported templates for a mono font-family on a non-data string** —
  section labels, nav items, button text and prose must be sans.
- **Fresh session lands on a collapsed rail**, with every icon reachable by
  keyboard and named to a screen reader.
- **Overview at 1280×800**: the vital bar, some thread, and the right window's
  tab row are all on screen, with ACTIVITY scrolling inside the window.
- **A resting pitch-map box has no border**; a building one is `--warn`, a
  stopped one `--danger`, and no third colour appears.
- **A node that is stopped shows stopped in all three indicators**, even if it
  also has a last-built time.
- **Both themes**, per §11.


---

## 16. The Sept 14 calm-agent pass

Design: `Mycelium Console.dc.html`. Again no claim moved and no number changed;
this is surface, spacing and one restructured component. Pre-pass copy kept as
`Mycelium Console v4 -green console-.dc.html`.

### 16.1 Theme layer

Warm-neutral ink replaces the blue-black in both themes, under the **same token
names** — so this ports as a `_theme.html` swap and nothing in a template
changes. Status colours (`--accent`, `--warn`, `--danger`, `--info`) are
untouched: green still means PASS / connected / ok and nothing else.

Radius scale: `3→8`, `6→10`, `12→16`, `14→18`. Body type `13→13.5px`. Type floor
holds at 11px. If you keep radii in the theme layer rather than per-template,
add four radius tokens while you are in there.

### 16.2 The agent-step timeline

The run's stages were four table rows inside a bordered card; they are now a
**vertical timeline** — a 1px spine, round step markers, sans label over a 13px
detail line, duration right-aligned — and it **leads the agent's reply**, above
the topology map. Both the resting thread (`chat.stages`) and the live run
(`live.stages`) use it; keep them identical or the live run will look like a
different object than the one the thread just showed.

**The markers are round on purpose.** Every status lamp in this design is a
square; a step marker reports progress, not status, so it must not read as a
lamp. Don't unify them.

The spine is `--border-strong` (`--border` is invisible at normal zoom), and the
**last step has no segment** — a step list's connector joins steps, so a segment
after the final marker implies a step that isn't there. Both copies take the
colour per step from the model (`s.spine`), not from the markup.

### 16.3 Chrome removed

- The rail is borderless, sits on `--bg`, and its items are pills — the
  inset 2px state marks are gone, the pill carries selection.
- The top bar lost its hairline, grew to 68px, and its title to 17px.
- The composer floats on `--bg` rather than docking on a hairline.
- Table hairlines and the 61-row grid are untouched (§15.2 still applies).

### 16.4 Checks

- **Grep the ported templates for literal colour** — this pass changed only
  token values, so `test_no_page_hardcodes_a_colour` must still pass.
- **A step marker and a status lamp must not be the same shape** on any screen.
- **Both themes**, per §11 — the light theme is warm paper now, so re-check
  contrast on `--text-muted` over `--surface`.


### 16.5 The pitch map fits its column

The map canvas was authored at a fixed 524px with `overflow-x: auto` around it.
The thread column is commonly ~420px, so the topology side-scrolled: the
builders sat off-screen and the claim the map exists to make — work goes out to
machines and comes back — was invisible without dragging.

The canvas now **measures itself** (a `ResizeObserver` on the relative box, width
in state) and the model solves the geometry from that width:

- card width `min(150, max(112, (W − lane − 56) / 2))`, so cards shrink before
  they collide and stop at a legible floor;
- the orchestrator→builder gap never drops below 56px — under that the packets
  have nowhere to travel;
- the pitch entry stub (34px) is dropped only below 360px, and the old 84px
  *text* lane is gone: the moving `pitch` packet names it, so the entry point
  survives narrow columns instead of being the first thing cut.

Measure the **canvas**, not its padded wrapper — `clientWidth` includes padding,
which made the first cut of this 22px too wide and reintroduced the scrollbar.

Two related fixes in the same pass: node cards are now a fixed 100px tall so
`HH` (half-height, 50) is exact and the lines meet card centres rather than
approximately; and **packets fade over the first and last fifth of each leg**,
because every lane's leg starts and ends on the orchestrator's edge and two
labels were otherwise stacking on the same pixel.

Coordinates stay plain pixels on a `0 0 W H` viewBox — do not switch to
percentage coordinates with `preserveAspectRatio="none"`; it distorts stroke
weight on the diagonals.


### 16.6 The node map: three scales, three shapes

**Layout is now a fit.** The seed read `stage.clientWidth/Height` once at mount
and baked pixel positions from it. Any later size — a wider window, a stage that
mounted before layout settled, a rail toggle — left the graph sitting in a
corner of the stage with dead space around it, which is what made every scale
look broken regardless of its own geometry. Models describe positions in their
own units; each paint maps the model's real bounding box onto the current stage,
and a `ResizeObserver` on the stage invalidates the fit. Padding is derived from
the largest bubble radius plus label room, so nothing clips at 100%.

All three scales also shared one routine that stretched `gx`/`gy`
independently to fill the stage. Shape therefore carried no meaning — it was
whatever the container's aspect ratio happened to impose. Models that describe
a shape now set `uniform: true` and are laid out with a single scale and
centred; **live keeps the old stretch** (its axes mean different things — stage
left-to-right, lane top-to-bottom — so distortion costs nothing there).

**Simulated (18 machines).** The thirteen idle machines were packed into a
0.68-unit-wide band — a cramped column of near-overlapping bubbles.

The subtler fault was the edge bundles. Both waves are handed out by the
coordinator, so both bundles start at the same point; with the waves in one
column they ran along the same rays, wave 2's dashed line lay exactly over
wave 1's, and the wave-1 machines disappeared underneath. **The waves are now
separated by direction** — wave 1 fans up, wave 2 fans down, one clean bundle
each — and wave-1 machines are drawn at the live scale's idle size (22) so a
machine that ran and went free still reads as a machine.

Then the **field**: 13 machines as 5 · 4 · 4 with each row centred (a ragged
last row read as an accident), set off by the widest gap in the layout. That gap
is the claim — five machines is the ceiling on one plan, so the rest is hardware
the plan cannot reach.

**Wave-2 edges were drawn builder→builder** (`m<wave1 id>` → `m<wave2 id>`),
which asserts a peer-to-peer path that does not exist. Wave 2 *depends on*
wave 1 but is still handed out by the coordinator, so every line now runs
orchestrator↔builder — the same rule the pitch map follows. The dependency
stays stated in the edge's `DEPENDS ON` row, which is where it belongs.

**Swarm (125 drawn).** Three iterations, and the lesson is a sizing constraint:
**a run has to be about as large as the live scale's shape to read as a run.**
A jittered 5×4 grid implied rows and columns nothing has; twenty swarms on three
rings were individually illegible at 14px; six were better but left an empty
band between the runs and the rim. The disc now carries **three runs in the
middle and the unused hardware around them**, which is the only split that lets
both halves be legible at this stage size.

**Shape carries strategy here.** The three middle runs are the strategies that
differ most — a fan (one plan split across machines), a DAG (two tiers, the far
one still held on `depends_on`), and several tries (the same subtask on three
machines, as near-parallel lines at one radius). Colour still carries only
state, so this adds a channel without touching the status vocabulary. Every
line is still coordinator→builder.

Idle machines are placed by **even rejection sampling** — uniform candidates,
area-weighted radius, kept only when clear of every run and of each other, the
separation relaxed in passes so all 112 land, with a wider berth around "this
machine" whose label hangs below its bubble. They are allowed in *between* the
runs, not only outside them, or the disc reads as a ring plus some shapes rather
than one conglomerate. Phyllotaxis was the first attempt and was wrong: at that
count it draws visible spiral arms, and an arm is structure these machines do
not have.

**No run-face polygons at this scale** — three passes confirmed it. The
translucent triangle is a murky blob at this size and the four-key DAG face
self-intersects; the handoff lines already say "one run". Faces stay on live and
simulated, where the triangle is big enough to read. The same edge rule applies — coordinator to each of its two builders,
never builder to builder.

**"This machine" sits on the outer ring**, not at the disc's centre. A centred
"you" would claim a hub that federation would have to provide and this repo
explicitly does not. The caption's `drawn, not measured · needs federation,
which does not exist` still carries the rest.


---

## 17. The bottom status bar, as ported

Spec: §12 and `Mycelium Status System.dc.html` §02. Now live in
`Mycelium Console.dc.html`, driven by the console's own state rather than the
demo rig's.

**Placement.** The shell is a column: a row holding the rail and the view, then
the bar beneath both. The rail is `height: 100%` inside that row, not
`100vh` — with `100vh` it reached past the bar and the bar's border met
nothing. The bar is outside the view's scroll container, so it never scrolls
away, and the public pages (landing, try, share) do not get one.

### 17.1 Three changes from the spec, and why

**Nine cells became four.** MODE, LOCK, TRACING and EVIDENCE do not change while
you watch — they are configuration, and in a permanent bar they were furniture
competing with the four facts that do move (inference, nodes, running, queued).
They are now in the **disclosure drawer**, each with a sentence saying what the
value means for you: `local` says nothing leaves the machine, `shadow` says a
failing check will not block a run yet. That sentence is the reason to keep them
at all, and there was no room for it in a 30px strip.

**The age is the disclosure trigger.** The spec had an age cell plus a separate
chevron — two targets for one intent. The whole right-hand group is now one
button, so "how old is this" and "where does it come from" are the same gesture.

**The bar collapses to the ambient pill.** A permanent 30px strip is right when
you are watching a run and wrong when you are reading a 61-row table, so the
minus control drops it to the corner pill and the pill restores it. The pill
carries severity plus one meta fact, which is the spec's own §04 behaviour.

### 17.2 Rules that carry over unchanged

- **Worst facet wins, and the word is the condition's own name.** `ok`,
  `inference down`, `no machines`. **Never "degraded"** — it names nothing
  actionable.
- **The age is held, not counted up.** A missed poll shows a stale age
  (`no answer for 34s`), never a fresh one. Hysteresis per §12.1: one failed
  poll changes nothing on screen except the age.
- **Lamps are squares**, like every other status lamp in the design — the round
  markers belong to the agent-step timeline (§16.2) and must not be confused
  with these.
- **One poll behind every cell** (`/api/status`), stated in the drawer, so no two
  cells can disagree.
- **One derivation, three indicators.** The four signs are derived once (a
  `signs` array) and the vital line, the bar's cells and the pill's severity all
  read from it. The first cut of this port re-derived them in the bar — running
  from `_pitchModel()`, nodes from `isLive` — and the bar said `RUNNING 0` while
  the vital line beside it said `1`, with nodes and queued both set to disagree
  in the offline state. Never give an indicator its own derivation; §12.1 is the
  whole reason the status model exists.
- **Machines stay registered while inference is down.** `nodes` is not a
  function of `isLive` — the offline state still reports 3 with a `--warn` lamp,
  because that is the design's own claim about what happens when Ollama stops
  answering.

### 17.3 Checks

- **The bar sits flush at the bottom** with no gap, at 30px, in every view.
- **Expanding does not scroll the view** — the drawer takes its height from the
  content row.
- **Collapse → pill → restore** returns the same bar state.
- **The public pages have no bar.**
- **Both themes**, per §11.


---

## 18. The machine identity block (bottom-left)

Shaped like the account menu every chat app puts in that corner, because that is
where people look for it — but **Mycelium has no user accounts**, so it does not
pretend to have one.

The only identity in the repo is the machine's: `worker_identity.py` binds a
`node_id` to exactly one coordinator URL and stores it in an owner-only file
(`read_owner_only_text`, permissive modes fail closed). There is no user table,
no profile, no session. The block therefore shows:

- **the node label and what it is bound to** (`node_id`, coordinator URL), with
  one sentence in the menu header saying there are no accounts and where the
  identity lives;
- **version and update state** — `Up to date · 0.3.0`, the `server_version` in
  `docs/DEPLOY.md`. An available release is a prop (`latestRelease`), never an
  asserted number, per the repo's no-invented-numbers rule;
- **the operator switches that belong here**: Settings (`config.json`), Invite a
  machine (with the enrollment mode as its meta), Viewer key (set / not set),
  Points earned, Appearance.

No avatar image, no person's name, **no sign-out** — there is no session to end.

### 18.1 The update note carries a consequence

`Update available` is not just a version delta. Workers declare
`worker_protocol_version` and the coordinator enforces a compatibility window
(ADR 0015): a worker above the window is refused with
`worker_protocol_version_too_new` and `action: upgrade_coordinator`. So the note
says **update the coordinator first**. An update prompt that doesn't say this
invites the failure it causes.

### 18.2 The rail's inference line is gone

Deleted, not duplicated — the status bar now carries `INFERENCE`, and the
status-system spec's §02 placement note calls for exactly this. Two indicators
for one facet is the §12.1 failure in miniature.

### 18.3 Porting notes

- **44px minimum hit target** on the trigger, and it keeps working collapsed
  (58px rail) where only the avatar square and the update dot are visible — the
  dot is the whole collapsed affordance for "something needs attention".
- The menu is **capped at `calc(100vh - 124px)` and scrolls**; uncapped it ran
  off the top of a laptop viewport.
- Click-away is a full-viewport transparent button behind the menu
  (`z-index: 60`, menu at `61`), so any outside click closes it.
- Every item navigates and closes in one `setState` — no item leaves the menu
  open behind a view change.
- **Both themes**, per §11.

## 19. The Sept 18 fidelity pass — node map, and every control in the console

Two jobs in one pass. The node map got the same inspection depth at all three
scales, and every control in the console was audited until it either does
something or says why it cannot. No layout was redrawn and no claim changed;
this section is additive to §16.6 and §17.

### 19.1 The three scales are one instrument

Before this pass the live scale was a map and the other two were pictures:
`simulated` had machines you could hover but lines that said "wave 1 · subtask
2", and `swarm` was returned with `inert: true` — no hover, no panel, nothing.
That made the widest claim in the product the only one you could not
interrogate. `inert` is gone.

Every node, every line and every run ground at every scale now opens the same
panel, with the same row vocabulary:

| Row | On a machine | On a line |
|---|---|---|
| `STATE` | building / built, now free / waiting on the tier before it / circuit open | — |
| `IN THIS RUN` / `HOLDING` | the named subtask it holds | — |
| `HANDED TO` | — | the machine id |
| `DEPENDS ON` | `nothing` or the tier it waits on | same |
| `COORDINATOR` | which coordinator handed it out | — |
| `PLATFORM` / `MODEL` / `HARDWARE` | from `_simSpec(i)` | — |
| `DATA` | `simulated` / `drawn, not measured` / `you are here` | same |

Subtasks are **named**, per scale, and each line carries the **prompt** a
planner would have written for it — `W1T`/`W2T` + `W1P`/`W2P` on simulated,
`TASKS` + `PROMPTS` on swarm. The "why this strategy handed it out this way"
sentence is a separate `HANDING` note under the prompt, so the prompt slot holds
a prompt on all three scales.

`_simSpec(i)` is deterministic from the index, so a bubble reads the same on
every paint. **It deliberately omits points, tasks and weight.** Those mean
something specific in the repo and 125 invented ones would be the one thing this
map must never do; every drawn panel ends with the `DATA` row that says so.

Three correctness fixes while porting — carry all three:

1. **Swarm coordinator ids were wrong.** `label` came from `hex(id++)` and
   `title` from `hex(id)`, so the panel named a different machine than the
   bubble you clicked. One id, computed once (`oid`).
2. **The DAG's far tier now waits.** It was drawn `building`, which said the
   opposite of its own caption. `waiting = sh.s === 'dag' && j >= 2`, tone
   `wait`, dashed edge — being held on `depends_on` is the whole claim the
   two-tier shape makes.
3. **Rim machines can be in backoff.** At 125 machines some machine is always
   failing its way out of the pool, so `i % 37 === 11` gets `circuit open ·
   429 for 45s`. The legend's fourth state should exist at every scale that can
   show it.

Hit targets floor at 20px (`hit: Math.max(n.d, 20)`) so an 8px rim machine is
clickable. The mark is centred inside that box; the box is what takes the
pointer.

### 19.2 The run ground is a polygon on the machines

The ground used to be the polygon through the run's node centres, which only
worked for the live scale's three: four keys in two tiers self-intersected. The
fix is **vertex order, not a different shape** — sort the vertices by angle
around their centroid and you get a simple polygon for any count, corners still
exactly on the machines. Live's triangle is now just the three-machine case; a
five-machine run is a pentagon.

```js
const out = ks
  .map((k) => ({ x: p[k].x, y: p[k].y, a: Math.atan2(p[k].y - cy, p[k].x - cx) }))
  .sort((u, v) => u.a - v.a)
  .map((q) => q.x.toFixed(1) + ',' + q.y.toFixed(1));
```

**The area is the pitch.** Every ground carries a `prompt` — the one sentence
somebody typed — so the shape says "this is one run" and its area says what the
run is. Every corner is a machine that sentence reached.

Two layout nudges exist only so the polygons enclose an area, and both are also
truer pictures:

- Simulated's five builders **bow into an arc** (`bowX`) instead of sitting in
  one straight column. Five corners on a line is a zero-area sliver; the arc
  also shows that machines furthest from the middle of a bundle have the
  longest lines.
- Several-tries staggers its radius (`rr = sh.lr * (j === 1 ? 1.12 : 0.9)`).
  Still three near-parallel attempts, but the four corners enclose something.

Grounds do not touch, at any scale. On swarm that is the federation point made
without a caption: runs sit 0.90 apart centre-to-centre and reach 0.34, so they
clear each other by 0.22.

### 19.3 Swarm geometry: runs as large as runs

The three runs were in a 0.27-radius knot at the middle of the disc with the
idle machines holding the rest of the stage — three runs you could not read
surrounded by hardware you could. Centres moved to **0.52**, builder radius to
**0.34**, disc radius to **1.15**, stage to **560px**, so each run is about a
third of the stage.

Idle machines are now sampled over the **whole disc**, `rr = Math.sqrt(u) * R`,
including the middle. The old annulus left a hole in the centre, and a hole
draws a hub that does not exist. Rejection still keeps them out of every run's
reach. Six passes, separation `[0.15 … 0.052]`.

### 19.4 Zoom changes size, not just magnification

```js
_markScale() { const z = this._G.view.z; return Math.pow(z, 0.62) / z; }
```

Marks grow as `z^0.62` while distances grow as `z`, so zooming in **opens the
graph up** — clusters separate, the rim thins, small machines become real
targets. Labels and stroke widths divide that back out (`inv = 1 / (z * s)`) so
they hold their on-screen size at every zoom level. The polygon stroke is in the
same `[data-w]` sweep as the lines.

Labels are all in the DOM and revealed by zoom: `0` for the ones that carry the
claim (coordinator, this machine, anything lit or in a run), `1.8` for builders,
`3.2` for the rim. Plus a **collision cull** — a label is dropped when one
already shown sits within `78/z × 15/z` model units of it, so the test tightens
as you zoom and more of them survive the further in you go. Without it, 125 rim
labels printed over each other and over the builders'.

**Symmetry fix:** a mark's box used to add 11px of room for the label
underneath, which put every circle 11px above the end of its own line — visible
on every edge. The box is now the hit target with the node point at its exact
centre (`anchor = hit/2`), and the label hangs off the box
(`position: absolute; top: 100%`). Lines drawn centre-to-centre meet every
circle dead centre.

### 19.5 Hover tells, click goes

The panel already answered all of this; what was missing was the immediate
answer and the click-through.

- **Node** → `node-a1f3 — building · click for this machine's own page`. A drawn
  machine says it is drawn and has no page.
- **Line** → `Filter rows by date — building · click to pin the prompt it carries`.
- **Ground** → `Your run — plan & split · the area is the run — click to pin what was pitched`.

On SVG geometry a tooltip is a `<title>` child, not a `title=` attribute.

**Clicking a machine on the live scale opens `/node/{id}`** (§5.5). Only live
nodes carry `pageId`; drawn ones pin the panel instead, because they have no
page to go to. In the repo this is an `<a href="/node/{{ id }}">` around the
mark — not a JS navigation.

### 19.6 `/node/{id}` is about the machine you clicked

The page was written for one machine with its values inline. It now derives
everything from the same `NODE_BASE` the Nodes view reads, so **the two surfaces
cannot disagree about a machine** — title, dot, intro sentence, the four
figures, platform, hardware, GPU, enrolment, claim, session, current state.

Two rules that came out of it:

- **GPU is reported, never inferred.** A spec segment counts as a GPU only if it
  names one (`/rtx|geforce|radeon|arc |quadro|tesla|gtx/i`). Taking the last
  segment turned `Apple M2` — an SoC — into an asserted graphics card. No named
  card means `none — CPU inference`. A named card is stripped from `HARDWARE`
  so the two rows stop repeating each other.
- **Per-machine ledger rows exist for one machine only.** `node-a1f3` has six.
  Every other machine states where the list comes from
  (`/ledger?node=node-7c22`) rather than showing six invented entries. In the
  repo this row set comes from the real query and the note disappears.

### 19.7 Every control in the console

**100 controls gained a short hover description** saying what they do. Four
rules, all of which matter in the port:

1. A control that calls an endpoint names it: `Stop this run · POST /run/{id}/cancel`.
2. A control with **no endpoint** says so and cites the gap:
   `Starts a new run from this output — needs a fork endpoint (HANDOFF-UI.md §3)`.
   Those seven are `aria-disabled="true"` with `cursor: default` so they stop
   reading as live. **When the endpoint lands, remove both, not just the
   tooltip.**
3. A security-relevant control says the consequence in plain language:
   `No key is set, so anybody who can reach this address can read the console`.
4. Inert variants say **why**: `A compatibility session has no durable identity
   to revoke. Require enrollment in Configuration and this becomes a
   per-machine control.`

What became functional:

| Control | Behaviour |
|---|---|
| Runs chips + search | Filter the loaded page, with active state, clear button, empty state. Chip counts stay the ledger's totals for all 61 — repo figures, not a row count — so the corner reads `10 of 61 loaded`. |
| Runs column headers | Sort. Any sort other than `WHEN` **drops the day headings** and says so in the group label — a list grouped by day and ordered by wall clock is two orderings claiming to be one. |
| `rotate credential` | Flips to `credential rotated ✓`. Tooltip states what is untouched: points, history, name. |
| `revoke` | **Arms on the first click, fires on the second** (`armRevoke`). The row it leaves says what survived: finished work and points stay on the ledger, and the name is retired rather than freed. |
| Three copy buttons | Copy the command in their own box (`copyNear` reads the sibling `<code>`). |
| Panel tabs, machine menu | Describe themselves from their own data. |
| `Continue →` on a project | Goes to the composer. |

The two-click revoke is the only destructive control in the console and should
stay two-click in the port; a single click on a row action is too cheap for an
irreversible offboarding.

### 19.8 Checks specific to this section

- Every `<button>` and every `<a>` in a template has a `title`, or is a plain
  text link whose label already says where it goes.
- No control without a handler is left with `cursor: pointer`.
- On all three scales: hovering a machine, a line and a ground each opens a
  panel, and each panel ends with a `DATA` row.
- Swarm: the three coordinator bubbles' labels match their panel titles.
- Swarm: the DAG's far tier is dashed and reads `waiting on the tier before it`.
- Zoom to 3× on swarm: no two labels overlap; stroke widths and label text have
  not visibly grown.
- Every run ground is a simple polygon (no crossed edges) with a corner on each
  of its machines, and no two grounds intersect.
- `/node/{id}` for a machine with no named GPU reads `none — CPU inference`.
- `/node/{id}` for a machine other than `node-a1f3`: no invented ledger rows.
- `pytest -q` and `ruff check .` clean; `test_no_page_hardcodes_a_colour` still
  passes (this pass added no literals).
