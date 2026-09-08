> **Status:** Point-in-time design handoff; non-normative as a whole.
> **Handoff date:** 2026-09-07
> **Repository revision when archived:** `94700f22341545ee578e1b2d1d66f0d3cb383507` (`master`)
> **Authority:** Current source code, protocol documentation, and accepted ADRs
> supersede this handoff when they conflict.
> [`docs/design/HANDOFF-DELTA.md`](../HANDOFF-DELTA.md) remains the contract for
> the console port; **where this document and the delta disagree, the delta wins,
> and where the delta and source disagree, source wins.**
> **Archive note:** All eleven files are archived as received. `support.js` exists
> only to make the design files render in a browser; it is not production code and
> must not be copied into `templates/` or `static/`. The two copies here are
> byte-identical to each other and are duplicated so each directory opens on its
> own; both differ from the older copy in
> [`../console-2026-08-28/`](../console-2026-08-28/), which is the runtime the
> earlier design files were authored against.
> **Two corrections to this document, found while porting §13:**
> `spec/_theme.html` is token-for-token identical to the shipped
> `templates/_theme.html` — same 54 names, same values in both themes — so it is a
> no-op rather than a replacement, and no token was added for this pass. The
> "Design tokens" colour table below is **not** those values: it cites a different
> palette (`--bg` `#030405` against the shipped `#08090A`, `--border` `#262A30`
> against `#1E2126`, light `--bg` `#F1F3F5` against `#FFFFFF`, and eight more).
> Read contrast off the shipped theme, never off that table. See
> [`../HANDOFF-DELTA.md`](../HANDOFF-DELTA.md) §8.10.

# Handoff: Mycelium run detail, provenance envelope, ledger chain

Phase 3 of the Mycelium console port. Target repo: `distributed-orchestrator`,
branch `master`.

## Overview

Three things ship together:

1. **Run detail** — one structure rendered on three surfaces: the dashboard's
   run modal, the console's `#view-run`, and the server-rendered `/run/{id}`.
2. **The provenance envelope** — a binding of identity for a run's output.
3. **The tamper-evident ledger chain** — verification state for the contribution
   ledger.

Plus one bug fix (the modal header at narrow widths) and a re-audit of Overview
and Runs, which are **not** redrawn.

## About the design files

The files in `design/` are **design references written in HTML** — prototypes
showing intended structure, styling and behaviour. They are not production code
to copy. The task is to **recreate them in this repo's existing environment**:
Jinja templates under `templates/`, `templates/_dashboard.css`,
`templates/_dashboard.js`, and the token layer in `templates/_theme.html`. No
build step, no framework, no npm — match what is already there.

Open any file in `design/` directly in a browser. Each is self-contained apart
from `support.js`, which must sit beside them (it is included).

- `Mycelium Run Detail.dc.html` — the full design document, §01–§08, with the
  reasoning for each decision. **Read this first.**
- `Run Detail Surface.dc.html` — the run detail surface itself. Renders in two
  modes; `surface: "console"` and `surface: "server"`.
- `Provenance Envelope.dc.html` — the envelope panel. `mode: "summary"` (what a
  run detail page shows by default) and `mode: "full"` (opened). `plural: true`
  shows the ensemble path.
- `Ledger Chain.dc.html` — `state: "intact" | "genesis" | "broken"`.

`context/` carries the surrounding designs already ported or spec'd: the console
shell and its views, the status bar and work card, and a faithful recreation of
today's `templates/dashboard.html` as the before-picture.

`spec/HANDOFF-UI.md` is the authoritative written spec. **§13 is this pass.**
§8.7 and §8.8 are the two blocking API gaps. `spec/_theme.html` is a drop-in
replacement for `templates/_theme.html`.

## Fidelity

**High-fidelity.** Final colours, typography, spacing and states. Recreate
pixel-for-pixel using the repo's existing CSS. Every colour in the design is
already a `var(--token)` that `_theme.html` declares — there are no new tokens
and no literal hex values to transcribe.

## Non-negotiable constraints

These are not style preferences. `tests/test_theme.py` enforces the first one,
and the rest are product commitments the repo has spent 26 PRs holding.

1. **Tokens only.** `test_no_page_hardcodes_a_colour` fails on any literal hex
   or `rgba()` in a template. Every colour is `var(--token)`.
2. **11px type floor.** The dashboard appears on camera during the demo
   recording. Nothing renders smaller.
3. **44px hit targets** on anything public-facing. Console-internal controls are
   28px.
4. **Green is status, never brand.** `--accent` means PASS / connected / ok and
   nothing else. `--mark` is the brand colour and is neutral.
5. **Filled vs hollow carries meaning**, so every state survives greyscale and a
   reader who does not see green. Never colour alone.
6. **No number that is not in the repo.** Where a value has no endpoint, the
   element is dropped or labelled — never estimated.
7. **Words nothing on these surfaces may use:** *verified*, *trustless*,
   *tamper-proof*, *proof of correct execution*, *signature*, *signed*,
   *attestation*, *attested*. Also prohibited: any quality percentage, anything
   implying capability evidence affects routing, and any presentation of
   agreement between runs as correctness.

## Blocking API gaps — read before estimating

**Neither new surface has an HTTP route on `master`.**

- **Provenance envelope (§8.7).** `provenance.py` stores one envelope per
  execution, append-only, with a deterministic digest and an offline checker.
  `ProvenanceEnvelopeStore.get()` exists and nothing calls it over HTTP. Today
  the only way a person sees an envelope is to download the **audit** bundle and
  open `mycelium-provenance.json` inside the zip.
  *Extend:* `GET /v1/executions/{id}/provenance`, viewer-gated, returning
  `as_export()` — the shape the bundle already carries, so no new contract.
  *Or drop:* keep the panel to the summary line built from what
  `/v1/executions/{id}` already serves, and make "Open the envelope" the
  audit-bundle download.
- **Ledger chain (§8.8).** `verify_ledger_chain()` returns first-break index,
  entry ID, reason and both digests, and `as_dict()` is already the right
  response shape; its only caller is `scripts/ledger_chain_admin.py verify`.
  `/ledger` projects entries without `entry_index`, `previous_digest` or
  `entry_digest`, so a client cannot walk the chain itself either.
  *Extend:* `GET /v1/operator/ledger-chain`, viewer-gated, returning
  `as_dict()`. Content-free by construction — no prompts, outputs or credentials
  live in the chained columns.
  *Or drop:* show no chain state in the console.

Everything else in this pass ships without either route.

## Build order

1. **The modal header fix.** Four CSS declarations, unblocks reading the modal
   at any width. Do it first.
2. **Run detail in the console** (`#view-run`), replacing the output modal's
   contents.
3. **`templates/run.html`** — the same structure server-rendered.
4. **The envelope and chain panels** — last, and only after the route decision
   above.

---

## Screen 1 — Run detail

**Purpose.** A person opens a finished run to get the thing they asked for, then
to understand how it was produced and how much of that is established fact.

**Order is load-bearing: the deliverable leads, the plan follows.** Plan is
process; the code is the product. The plan sits directly beneath it because the
waves are the parallelism claim. Do not restore the old process-first order.

### Layout, top to bottom

Outer container: `1px solid var(--border)`, `border-radius: 5px`,
`background: var(--bg-elevated)`, `overflow: hidden`.

1. **Header** — `display: flex; align-items: flex-start; flex-wrap: wrap;
   gap: 12px; padding: 12px 14px`, bottom hairline `1px solid
   var(--border-subtle)`.
   - Left column `flex: 1 1 250px; min-width: 0`, `gap: 7px`:
     - Chip row: verdict chip, then a hardware badge (`11px` mono, `1px solid
       var(--border)`, `radius 3px`, `padding 2px 7px`), then relative age
       (`11px` mono, `--text-faint`).
     - Title: `15px / 600 / line-height 1.4`, `--text`, `overflow-wrap:
       anywhere`, `text-wrap: pretty`.
     - Meta line: `11px` mono, `--text-faint` — execution ID, project ID,
       deliverable count, audit-record count.
   - Right: actions, `margin-left: auto; display: flex; gap: 8px; flex-wrap:
     wrap`. Buttons `radius 4px`, `12px` sans, `padding 0 12px`, height **28px**
     in the console. First is primary (`background: var(--surface-hover)`,
     `color: var(--text)`, `border: 1px solid var(--border-strong)`); the rest
     ghost (`transparent`, `--text-dim`, `1px solid var(--border)`).
     `:hover` → `border-color: var(--border-strong)`.
2. **Replay line** — only when the run was returned rather than re-run.
   `background: var(--info-wash)`, `padding 10px 14px`, a `7px` square in
   `--info`, then `12px` text in `--text-dim`. Copy: "Returned, not re-run: this
   task was pitched again under the same idempotency key, so nothing was built
   twice. The same key with a changed task is refused instead."
3. **The three axes** — a flex row of three cells, each `flex: 1 1 180px;
   padding: 12px 14px`, divided by `1px solid var(--border-subtle)`.
   Per cell: label (`11px` mono, `letter-spacing .11em`, `--text-muted`), then a
   `7px` marker + value (`13.5px` mono), then a note (`11.5px`, `--text-muted`).
   - `LIFECYCLE` — `completed`, filled marker, `--accent`.
   - `VALIDATION` — `passed`, filled marker, `--accent`.
   - `ASSURANCE` — `unverified`, filled `--text-faint` marker with
     `--border-strong` edge, value in `--text`.
   **Do not collapse these into one badge.** The compatibility `status` field
   already flattens *completed+passed* → `completed` and every other completed
   outcome → `unverified`, which destroys the distinction this strip exists to
   show. Read the three fields separately.
4. **Placement line** — `padding 10px 14px`, `11px` mono:
   `asked for any → planned distributed → ran distributed`, with the values in
   `--text-dim` and the final one in `--text`. Right-aligned: a `6px`
   `--accent` square and "consent recorded to leave this machine".
5. **Metric strip** — four cells, `flex: 1 1 130px`, value `15px` mono in
   `--text`, label `11px` mono `letter-spacing .1em` in `--text-muted`.
   `UNITS`, `WAVES`, `DELIVERABLES`, `AUDIT RECORDS`. **Counts only** — no wall
   clock (unserved) and no speed multiplier (both its terms are unserved).
6. **Body** — `display: flex; flex-wrap: wrap; gap: 16px; padding: 16px 14px`.
   Main column `flex: 100 1 400px`, side column `flex: 1 1 300px`, both
   `min-width: 0`. On a narrow viewport the side column wraps beneath.

### Main column

**Deliverable panel.** Header `DELIVERABLE` + right-aligned "2 files ·
deliverable role". A prose summary (`12.5px`, `line-height 1.7`, `--text-dim`),
then a file card: a filename bar (`background: var(--surface-hover)`,
`radius 4px 4px 0 0`, filename `11.5px` mono, right side "3.1 KB · parses ·
imports clean") above a `<pre>` in `background: var(--surface-sunken)`,
`11.5px` mono, `line-height 1.7`, `overflow-x: auto`, `radius 0 0 4px 4px`.

**How it was split.** Header `HOW IT WAS SPLIT` + "4 units · 2 waves · from
depends_on". Then one block per wave: a wave bar (`background:
var(--surface-hover)`, `WAVE 1` in `11px` mono `--text-dim`, then the dependency
note in `--text-muted`), and beneath it the unit cards as
`display: flex; flex-wrap: wrap; gap: 9px; padding: 11px 13px`.

Unit card: `flex: 1 1 190px`, `1px solid var(--border)`, `radius 4px`,
`padding 9px 10px`, `background: var(--bg)`. Row one is the unit ID (`11px`
mono, `--text-faint`), the title (`12px`, ellipsised, `white-space: nowrap`),
and a `6px` state marker pushed right. Row two is the unit prompt (`11.5px`,
`--text-muted`). Row three is the machine line (`11px` mono).

**Waves come from per-unit `depends_on`, grouped by dependency depth.** Wave 2
showing three units that depend only on unit 01 *is* the task-level-parallelism
claim. Do not render `depends_on` as a column of IDs.

**Per-unit machine assignment is unserved** (gap 8.2). Each card reads
`machine not recorded` in `--text-faint`. Do not borrow a plausible machine from
`/nodes`.

### Side column

**Manifest panel.** Header carries `MANIFEST`, then the state chip — `SEALED` at
`11px` mono in `--accent` with `1px solid var(--accent-dim)`, `radius 3px` —
then "7 files · 54.0 KB". Below, a sunken block with the manifest hash (`11px`
mono, `--text-faint`) and one sentence of limit. Then two groups:

- `DELIVERABLE` / `GET /download`
- `AUDIT` / `GET /audit-download`

Each row: filename (`11.5px` mono, ellipsised) with size right-aligned, then a
second line with role and short hash. **The two downloads are different scopes
and stay separate** — the plain download hands over deliverables; the run's own
paperwork must be asked for by name so a handoff never carries it by accident.

**Envelope panel** — see Screen 2. It sits directly below the manifest panel and
must remain visually a sibling, not a child.

**Timeline.** `50px` mono timestamp column + description, `11px`, one hairline
per row. Sourced from `full_log.json` in the audit bundle — per-unit timings live
there, not in the API. A row with no served value renders `+—` and says so.

### Footer

A `--surface-sunken` band with a `--slate` marker and one paragraph naming what
this surface shares with the other two.

---

## Screen 2 — The provenance envelope

The envelope is ~20 identity fields. Almost no reader wants twenty fields, so
**the default is one sentence plus a count of what is missing.**

### Default (on the run detail page)

Container: `1px solid var(--border)`, `radius 5px`, `background:
var(--surface)`.

1. **Header** — `background: var(--surface-sunken)`, `padding 10px 13px`. Label
   **`PRODUCED BY`** (`11px` mono, `letter-spacing .11em`, `--text-muted`), then
   `envelope v1 · digest sha256 3c9ab7d0…f21e` in `--text-faint`.
   The label is `PRODUCED BY` and **not** `PROVENANCE`: most readers have met
   the second word in a supply-chain context where it means signed and
   third-party-checked, and this is neither.
2. **The sentence** — `12.5px`, `line-height 1.6`, `--text-dim`:
   > Built by 1 enrolled machine on qwen3.5:4b, checked by 2 validators.

   Then, `12px` in `--text-muted`:
   > Binds who produced these files, under which enrolled identity, with which
   > model, and which validators ran. It does not establish that the output is
   > correct, useful, or honest.
3. **Footer** — "Open the envelope" as a text link (`11px` mono, `--text-dim`,
   `border-bottom: 1px solid var(--border-strong)`), and right-aligned a hollow
   `7px` marker with "4 facts not recorded".

**No chip, no lamp, no colour on this panel.** See "Two claims" below — this is
the single most important rule in the pass.

### Opened

Field groups, each with a `--surface-hover` group bar (group label left, source
collection right) and rows as
`grid-template-columns: 15px minmax(0, 172px) minmax(0, 1fr)`:
a `7px` marker, the field name (`11px` mono, `--text-muted`, **no**
`overflow-wrap` — field names must render whole), the value (`11.5px` mono,
`overflow-wrap: anywhere` — values are hashes), and where needed a note spanning
column 3 (`11.5px`, `--text-muted`).

Groups in order: `EXECUTION`, `PRODUCER IDENTITY`, `CAPABILITY AND EXECUTOR`,
`MODEL`, `VALIDATORS · IN ORDER`, `ARTIFACTS`, `SAMPLING`, `RESERVED`.

### Absence is a value, never a blank

- **Recorded fact** — filled `7px` marker in `--text-faint`, value in
  `--text-dim`.
- **Not recorded** — hollow `7px` marker (`background: transparent`, `1px solid
  var(--text-faint)`), value in `--text-muted`, the field named, and a note
  saying why.

Never a dash. Never `--warn` or `--danger`: not writing something down is
neither a fault nor fine, and the *shape* carries it so it survives greyscale.

Keep the repo's three sampling absences distinct rather than collapsing them to
"unknown": `sampling_parameters` (nothing pinned), `sampling_seed_honoured` (a
seed was set but is not shown to be honoured), `producer_sampling` (a distributed
machine sampled and the worker protocol does not carry it back).

### Plural producers is the primary case

The ensemble path settles several accepted receipts, carries one `producers`
entry each, and leaves the singular fields `null` rather than electing a winner.
So `producers` is **always** a list and one producer is a list of one. The
singular fields render `not applicable` with a hollow marker, not blank.

A layout built for the single case would need a rule for choosing between
receipts, and no such rule exists.

### The reserved signature slot

`RESERVED` / `signature` / `reserved · empty`, hollow marker. A slot, not a
feature: no key, no key management, no transparency log, no third party. It
appears in the opened envelope and nowhere else.

---

## Screen 3 — The ledger chain

**Intact is not green.** Green means PASS / connected / ok. An intact chain
means no entry changed *without every link after it also being recomputed* —
which a full rewrite satisfies. A green tick here would be the exact class of
claim this repo refuses.

### Structure

1. **Header** — `LEDGER CHAIN` + `chain v1 · 61 entries · walked 2s ago`.
2. **Verdict row** — a `8px` marker, the verdict in `12.5px` mono
   `letter-spacing .08em`, and a right-aligned walked count in `11px` mono
   `--text-muted`.
3. **The chain strip** — a horizontally scrollable flex row
   (`overflow-x: auto`, inner `min-width: max-content`). Each cell is a
   `min-width: 34px; height: 26px` box (`1px solid`, `11px` mono, index inside)
   with a `7px` marker beneath, joined by `13px × 1px` link segments. Ellipsis
   cells elide runs.
4. **Legend** — `11px` mono, marker + label per entry.
5. **Break report** — broken state only. `background: var(--danger-wash)`,
   header `FIRST BREAK` in `--danger-text`, rows as
   `grid-template-columns: minmax(0,132px) minmax(0,1fr)`.
6. **Footer, on every state** — `WHAT THIS DOES NOT ESTABLISH`, then:
   > Tamper evidence, not tamper proofing. An operator with write access to this
   > database can rewrite every entry **and** every link, and this will then
   > report intact. There is no consensus here, no external anchor, and nobody
   > outside this machine attesting to anything. A walked chain is not proof
   > that any recorded work happened, was correct, or is owed anything.

   This limitation is asserted by a test in the repo rather than admitted in a
   doc. It belongs on screen where the verdict is read.

### Three states

| State | Verdict | Cells | Colours |
| --- | --- | --- | --- |
| `intact` | `LINKS INTACT` · `61 walked · 0 unlinked` | all linked | box `--bg` / `--border-strong`, index `--text-dim`, filled `--text-faint` marker, link `--border-strong`. **Never `--accent`.** |
| `genesis` | `LINKS INTACT · 12 ENTRIES PREDATE THE CHAIN` | unlinked head, then linked | unlinked: `transparent` box, `--border` edge, `--text-muted` index, hollow marker |
| `broken` | `LINK BROKEN AT 43` | linked → break → not walked | break: `--danger-wash` box, `--danger-line` edge, `--danger-text` index, filled `--danger` marker. Not walked: `transparent`, `--border-subtle`, `--text-faint`, hollow |

**Entries after a break render `not walked`, not broken.** Verification returns
at the first break, so nothing past it was checked. Drawing them as broken claims
more than the walk found; drawing them as intact claims the opposite.

**The genesis boundary is not a break.** Entries written before the chain existed
have no link and are never retrofitted with one. Show them unlinked at the head
and count them separately.

**The break report is content-free by construction** — index, entry ID, reason,
expected digest, observed digest. No prompts, outputs, credentials or artifact
contents are in the chained columns, so the report is safe to paste into an
issue.

---

## Two claims that must never read as one

This is the hardest constraint in the pass and the easiest to break by accident.

| | Sealed manifest | Provenance envelope |
| --- | --- | --- |
| Establishes | these bytes are the bytes frozen when the run ended, recorded locally | who produced them, under which enrolled identity, with which model, which validators ran |
| Does not | say who made them, or whether they are right | say whether they are right; **not** a signature, **not** an attestation |
| Varies | `sealed` · `legacy_live` — and which matters | present or absent; when present the *contents* matter, the presence does not |
| Drawn as | a chip in the panel header, `--accent` when sealed | a **sentence** — no chip, no lamp, no colour |
| Checkable | re-hashed against the sealed row on every read, by this coordinator | recomputable offline from the audit bundle with no coordinator, network or credential |

Three implementation rules:

1. **Different grammatical class, not a second badge.** Two chips side by side is
   a row of ticks, and ticks get counted as one stronger claim. The manifest gets
   a chip because its state genuinely varies; the envelope gets a sentence.
2. **No group header over both.** There is no `INTEGRITY` panel and no `TRUST`
   section anywhere in this design. A shared header is precisely what invites a
   reader to add up what sits under it.
3. **They vary independently.** A sealed manifest with a legacy producer and no
   model digest is an ordinary run; so is an envelope over a `legacy_live` file
   list. Neither is evidence for the other.

---

## The five-chip vocabulary

Five values, no sixth, no percentage.

| Chip | From | Means | Colours |
| --- | --- | --- | --- |
| `PASS` | `rating: PASS` and no precheck error | reviewer passed it **and** the mechanical check found no defects | `--accent` on `--accent-wash`, `--accent-dim` edge, filled dot |
| `NEEDS WORK` | `rating: NEEDS_WORK` | returned with named problems; still downloadable | `--warn-text` on `--warn-wash`, `--warn-line` edge, `--warn` dot |
| `FAIL` | `rating: FAIL` | terminal without a usable deliverable | `--danger-text` on `--danger-wash`, `--danger-line` edge, `--danger` dot |
| `UNCHECKED` | `code_precheck_error` set | the check did not reach a verdict — unchecked, not known good | `--text-muted`, transparent, `--border-strong` edge, `--text-faint` dot |
| `NO VERDICT` | `rating: "?"` | nothing recorded; says nothing about the work | same as `UNCHECKED` |

Chip geometry: `11px` mono, `letter-spacing .06em`, `radius 3px`,
`padding 3px 7px`, a `6px` square dot, `gap 6px`.

**Worst wins** when both halves speak:
`FAIL → NEEDS WORK → NO VERDICT → UNCHECKED → PASS`. A precheck error can never
soften a named negative verdict, and `PASS` requires both halves of its claim.
A record carrying both a runner failure and a problem list cannot be constructed
— `ParsePrecheckResult.__post_init__` raises — so the ladder only ever resolves a
rating against a precheck error.

**`_dashboard.js` currently renders two of these five.** It reads `code_files`
and neither `code_problems` nor `code_precheck_error`, so a run whose check never
reached a verdict is shown as if it had passed. `/history` and `/gallery` now
both carry `code_precheck_error` per row, so all five can render today with no
endpoint change.

---

## The hardware badge degrades and does not guess

1. `8 CPU · 16 GB · Apple M2` — work a machine is holding **now**, read from the
   node's `current_task` (machine → task, the direction the API serves).
   Claimed at registration, never measured.
2. `3 machines` / `this machine` — finished work, from `mode` and `nodes_used`.
   Placement and a count is all that is recorded.
3. `machine not recorded` — finished distributed work with no node list. Say it
   in words.

---

## Screen 4 — `/run/{id}`, server-rendered

Same structure, three differences, all forced by the medium rather than chosen:

1. **Nothing polls.** No client, so the page is a snapshot. It must never render
   a live cell or a relative age that stops being true — a run still going
   renders as still going with its own timestamp, not "2h 14m ago".
2. **It is shareable.** It carries an OG title and description, so the envelope's
   one-line summary has to survive being pasted with no page around it. That
   constraint is what chose the sentence.
3. **It is indexed by nobody.** Viewer-gated like the rest: no sitemap entry, no
   assumption a crawler ever resolves it. A permalink for a person who was given
   the link, not a public record.

**Controls are 44px tall here** (most visitors arrive on a phone from a link in a
post) against 28px in the console. Actions differ too: `Fork this run`,
`Download deliverables`, `See what else was built`.

---

## The modal header bug

Observed and deferred in the Phase 2 port. `.modal-head` is a non-wrapping flex
row with `space-between`; `.modal-title` has `min-width: 0` and no wrapping rule,
so a long unbreakable execution ID overflows its squeezed box while
`.modal-actions` (`flex-shrink: 0`) holds its width. The title runs under the
buttons.

```css
.modal-head    { flex-wrap: wrap; }                /* the row may break      */
.modal-title   { flex: 1 1 200px;                  /* it may claim a line    */
                 overflow-wrap: anywhere; }        /* long ids break         */
.modal-actions { margin-left: auto; }              /* replaces space-between */
```

No geometry change above the breakpoint: with room, `margin-left: auto` puts the
actions exactly where `space-between` put them. Both modals share the rule, so
the node modal is fixed by the same change.

---

## Element → endpoint map

| Element | Source | Note |
| --- | --- | --- |
| Title, task text, timestamps | `GET /v1/executions/{id}` | |
| `lifecycle` / `validation` / `assurance` | same | read separately — `status` flattens them |
| Verdict chip | `rating` + `code_precheck_error` | five values |
| Placement line | `placement_requested` / `placement_planned` / `mode` | asked → planned → ran |
| Waves | per-unit `depends_on` | grouped by dependency depth |
| Unit machine line | — | **unserved.** Renders `machine not recorded` |
| Metric strip | counts from the execution record | counts only |
| Deliverable preview | `code_files` + artifact read | first deliverable-role file |
| Manifest panel + `SEALED` chip | `ArtifactManifestV1` (spec §6) | re-hashed on read |
| Deliverable download | `GET /download` | deliverable role only |
| Audit download | `GET /audit-download` | different scope, deliberately separate |
| Provenance summary | **no route** (§8.7) | built from the execution record |
| Provenance full panel | **no route** (§8.7) | `as_export()` if extended |
| Ledger chain panel | **no route** (§8.8) | `verify_ledger_chain().as_dict()` if extended |
| Replay line | ADR 0008, `execution/idempotency.py` | returned, not re-run |
| Timeline | `full_log.json` in the audit bundle | per-unit timings live here |

---

## Overview and Runs — audit findings, not a redraw

Neither is redrawn. Fix these while porting:

- **Overview · "running now" — stale citation.** The archived handoff reads it
  from `/health` as an active execution count; `/health` has no such field. It
  returns `status`, `ollama`, `models`, `nodes_online`, `tasks_pending`,
  `node_enrollment_required`, `private_routes_protected`, `warnings`.
  → Read `/metrics.jobs_running`, the same source as the status bar's `RUNNING`
  cell, so the two can never disagree.
- **Overview · "queued" — wrong number.** It reads `/health.tasks_pending`, which
  is the *subtask* queue. Ported as written, two cells on one screen carry the
  label QUEUED over different numbers. → `/metrics.jobs_queued`, or keep
  `tasks_pending` and relabel the cell `SUBTASKS PENDING`. Not both under one
  word.
- **Overview · the vital strip is now redundant.** Its job is answering "is it
  working right now" with nodes, inference, running, queued — and the status bar
  carries all four, lamped, on every console page. Recorded as a removal for
  Overview's own phase; **not** part of this pass.
- **Overview · pass rate — unchanged.** Deliberately absent, and now retired
  outright rather than relocated. Do not reintroduce it as a bare number.
- **Runs · `WALL` column — gap stands.** Per-run wall clock is unserved. The
  design drops the column rather than estimating it.

---

## Design tokens

Every colour is a token declared in `spec/_theme.html`. **No new tokens.** Both
themes render from the same markup — a colour picked for dark is invisible only
in light, so verify every state in both.

### Colour

| Token | Dark | Light |
| --- | --- | --- |
| `--bg` | `#030405` | `#F1F3F5` |
| `--bg-elevated` | `#0D0F12` | `#FFFFFF` |
| `--surface` | `#15181C` | `#F7F8FA` |
| `--surface-hover` | `#1D2126` | `#EDEFF3` |
| `--surface-sunken` | `#08090B` | `#F0F2F5` |
| `--border-subtle` | `#1A1D21` | `#E7EAEE` |
| `--border` | `#262A30` | `#D4D9DF` |
| `--border-strong` | `#333841` | `#B7BEC6` |
| `--text` | `#E8E9EB` | `#16181D` |
| `--text-dim` | `#9CA1A8` | `#4A5057` |
| `--text-muted` | `#949BA3` | `#656C74` |
| `--text-faint` | `#878E97` | `#6E747C` |
| `--mark` | `#E8E9EB` | `#16181D` |
| `--accent` | `#3FB950` | `#1A7F37` |
| `--accent-dim` | `#2C8C3C` | `#157030` |
| `--accent-strong` | `#4ACB5D` | `#12692E` |
| `--accent-wash` | `rgba(63,185,80,.08)` | `rgba(26,127,55,.07)` |
| `--on-accent` | `#06210C` | `#FFFFFF` |
| `--warn` | `#C9922B` | `#9A6700` |
| `--warn-text` | `#E8C88A` | `#6B4A00` |
| `--warn-body` | `#B5A681` | `#6E5A28` |
| `--warn-wash` | `#14100A` | `#FFF8E6` |
| `--warn-line` | `#3B3222` | `#E8D9A8` |
| `--danger` | `#E5534B` | `#CF222E` |
| `--danger-text` | `#F0938C` | `#A0111B` |
| `--danger-body` | `#C29691` | `#7A2E33` |
| `--danger-wash` | `rgba(229,83,75,.10)` | `rgba(207,34,46,.08)` |
| `--danger-line` | `rgba(229,83,75,.28)` | `rgba(207,34,46,.30)` |
| `--info` | `#5B8DEF` | `#0969DA` |
| `--info-wash` | `rgba(91,141,239,.08)` | `rgba(9,105,218,.07)` |
| `--violet` | `#9A7BE0` | `#8250DF` |
| `--slate` | `#949AA2` | `#57606A` |

### Type

`--mono: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace`
`--sans: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`

All data is mono. Prose is sans. `font-variant-numeric: tabular-nums` on `body`.

| Role | Size / weight |
| --- | --- |
| Section heading | 18px / 600 / `-0.25px` |
| Run title | 15px / 600 / lh 1.4 |
| Metric value | 15px mono |
| Axis value | 13.5px mono |
| Body prose | 12.5px / lh 1.7 |
| Table cell, note | 12px / lh 1.6 |
| Small note, field value | 11.5px |
| Label, chip, meta | **11px** mono, `letter-spacing .1–.12em` |

**11px is the floor.** No exceptions.

### Spacing, radius, misc

Panel padding `10–14px`. Gaps `7 / 8 / 9 / 10 / 12 / 14 / 16px`.
Radius: `5px` panels, `4px` buttons and inner cards, `3px` chips.
Markers: `6px` chip dots, `7px` row/state markers, `8px` verdict markers.
Dividers are always `1px solid var(--border-subtle)`; panel edges
`1px solid var(--border)`.

No shadows anywhere except `--lift` on modals. No gradients.

## Assets

None. No images, no icon font, no SVG — every marker is a styled `<span>`, which
is why the states survive greyscale. Webfonts: the token stacks name IBM Plex
first with a system fallback; the repo does not phone home, so to actually get
Plex, self-host the woff2 files under `static/` and add `@font-face` to the theme
partial. Until then pages render in system UI fonts, which is fine.

## Checks after porting

- Grep the three surfaces for the banned words listed above.
- Open the run modal at 330px. Title wraps above the actions; nothing overlaps.
  Same for the node modal.
- A run with `code_precheck_error` set and an empty problem list renders
  `UNCHECKED` on the list card **and** in detail.
- A legacy execution with no envelope renders the panel absent — not an empty
  panel, not an error.
- An execution with three accepted receipts lists three producers and leaves
  `attempt_id` / `receipt_id` / `unit_id` as `not applicable`.
- Every state in both themes.
- `pytest tests/test_theme.py` — no literal colour in any template.

## Files in this bundle

```
design/    Mycelium Run Detail.dc.html     the design document, §01–§08
           Run Detail Surface.dc.html      the surface, console + server modes
           Provenance Envelope.dc.html     summary + full, single + plural
           Ledger Chain.dc.html            intact / genesis / broken
           support.js                      runtime for the above

context/   Mycelium Console.dc.html        the console shell and its views
           Mycelium Status System.dc.html  status bar, ambient pill, work card
           Mycelium Dashboard - Current.dc.html   today's dashboard, recreated

spec/      HANDOFF-UI.md                   the written spec; §13 is this pass
           _theme.html                     drop-in for templates/_theme.html
```
