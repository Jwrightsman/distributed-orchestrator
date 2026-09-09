# Console handoff — delta against master

> **Status:** Normative for the console port.
> **Audited:** 2026-09-06 against `master` `e91561f01e348e84884ea8cc5d367ee73a870568`.
> **Subject:** [`docs/design/console-2026-08-28/`](console-2026-08-28/), written
> against `master` on 2026-08-28.
> **Rule:** Where the archived handoff and this document disagree, **this document
> wins.** Where this document and current source disagree, **the source wins** and
> this document is the thing to fix.

Master merged PRs #62–#76 after the handoff was written. This records what went
stale, what landed with no design at all, and what the handoff states that is now
false. It is the contract for this PR and every later console phase.

---

## 1. Names the handoff cites — all of them resolve

Two earlier PRs found design briefs citing files that do not exist, so every path
and endpoint in `HANDOFF-UI.md` was checked mechanically rather than by eye.

**Result: no stale names.** Every file path resolves, and every endpoint resolves
to a route registered on `server.app`.

Specifically confirmed present: `templates/_theme.html`, `templates/_dashboard.css`,
`templates/_dashboard.js`, `routes_status.py`, `tests/test_theme.py::PAGES` (a
5-tuple), `showTab()` (`_dashboard.js:135`), `refresh()` (`_dashboard.js:193`, plus
a separate one at `index.html:282`), `loadHistory()` (`_dashboard.js:738`).

Four citations look absent to a naive grep and are correct on inspection:

| Citation | Why it is fine |
|---|---|
| `/status.json` | A route (`routes_events.py:107`), not a file. |
| `config.json`, `full_log.json`, `plan.json`, `memory.md` | Runtime artifacts. `config.json` is gitignored by design. |
| `/download`, `/audit-download` | Path suffixes of `/v1/executions/{id}/download` and `/v1/executions/{id}/audit-download`, both registered. |
| `templates/share.html` | Cited as *work still needed* (§5, "move the page to"), not as an existing file. |

**Limit of this check:** it covers names the handoff writes in backticks. Prose
references to concepts were not mechanically verified.

---

## 2. Surfaces that landed after 2026-08-28 and have no design

None of these appear in the design file. Each needs a design pass before the view
that would show it is built. Listed with the module that owns the truth.

| Surface | Owner | Note for whoever designs it |
|---|---|---|
| Provenance envelope | `provenance.py` | **Not** a signature and **not** an attestation — see §4.6. |
| Ledger hash chain (Theme 3C) | `ledger.py` | Tamper-**evident**, not tamper-proof. ADR-0017. |
| Worker protocol window, `GET /v1/worker-protocol` (4A) | `worker_protocol.py` | Already public in `_PUBLIC_EXACT`; exposes versions only. |
| Trace propagation (4B) | `tracing.py`, `tracing_middleware.py` | |
| Durable verification evidence (3B-1) | `verification_evidence.py` | |
| Typed capability descriptors (2B) | `node_capabilities.py` | `NodeCapabilityDescriptorV1` + `capability_descriptor_digest()` → `descriptor_hash`. |
| Shadow capability evidence (2C) | `capability_evidence.py` | Shadow-only. Never routes. See §4.2. |
| Worker installer + `docs/JOIN.md` (#74, #75) | `worker_installer.py` | |

Durable node enrollment also predates the design's vocabulary: a node now has an
`enrollment_id` (`node_enrollments.py`, TEXT PRIMARY KEY) that is distinct from its
label and from its session.

---

## 3. Sequencing

Phase 1 (this PR): the theme swap, viewer-auth and degraded states in the client,
the `/status` redaction, the Nodes view, and the language fixes.

Later phases, in the handoff's own order (§2), each gated on a design pass for any
§2 surface it would show: `dashboard.html` view by view — Overview, Runs, run
detail, Gallery, Projects, Guild, Evals, Config, Invite — then `try.html`, then
`run.html`, then the share page (§5, structural).

`templates/index.html` is **not** deferred: it ships a prohibited phrase and the
withdrawn quality figure, both fixed here (§4.1, §4.3).

---

## 4. Handoff statements that are now false

### 4.1 The quality figure — do not render any percentage

The handoff's §3 Overview summary line and its Evals view carry `~57% ±12`.

Two findings retire it:

- **PR #71.** `browser_ok` — "no uncaught JS error and a non-empty body" — was
  doing the work of "runs". Under it `web-snake` passed 5/5;
  `scripts/showcase_reliability.py`, same model and machine, measured a *playable*
  game at 2/10, because it also required that something was drawn, that the frame
  changed, and that the arrow keys did anything. **Every published `web_app` figure
  rests on the weaker check** (`docs/eval-methodology.md` §1.1).
- **PR #72.** The discordant pair rate is ψ = 0.643, 95% CI 0.46–0.79
  (`eval-methodology.md` §1.3) — 18 of 28 tasks flip outcome between two runs that
  differ by nothing. And §1.2 records that the six-prompt resolution claim "was a
  rule of thumb, not a computation" — it was never computed.

**Rule: no surface renders a quality percentage.** The Overview summary line ships
without one. This is not a display preference; the number does not currently mean
what a reader would take it to mean.

Live instance found: `templates/index.html:233` — "`~57%` of 28 varied test tasks
come back runnable and on-spec (95% confidence: 44–69%)" — on the **public landing
page**. Removed in this PR and asserted against by
`tests/test_console_language.py`.

Out of scope here but recorded: the same figure appears in `AGENTS.md:131`,
`docs/JOIN.md:319` and throughout `docs/community-pitch.md`. Those are prose
documents, not templates, and the test does not reach them.

### 4.2 The Nodes view's routing-weight vocabulary predates 2C

The handoff §3 Nodes says routing weight shows `— NOT SAMPLED` until
`verified_samples > 0`, with `agreement_score` and a sample count underneath.

**Neither field exists.** `verified_samples` and `agreement_score` have zero
occurrences repo-wide. More importantly the concept is wrong in two ways:

- **Shadow evidence never routes.** `capability_evidence.py` is shadow-only —
  `SHADOW_POLICY_VERSION`, `ShadowOperationalPhase = admission | evaluation`. No
  observation changes which machine is offered work. A "routing weight" column
  would imply a mechanism that does not exist.
- **Agreement is not correctness.** The real outcome type is
  `ShadowOutcome = same | different | no_preference`. Two runs agreeing says they
  agreed, not that either was right.

**Correct vocabulary for the Nodes view:**

| Show | Field | Rule |
|---|---|---|
| Capability descriptor | `NodeCapabilityDescriptorV1` | Show it and its `descriptor_hash`. |
| Observations | shadow outcome counts + sample count | Never as a score. |
| Too few samples | an explicit insufficient-evidence state | Not `0.00`, not an empty cell. |
| Identity | `enrollment_id`, distinct from label and session | |
| Points | `credits_earned`, labelled `POINTS` | Footnote travels with it. |

Never render a global score. Never imply evidence affects placement.

### 4.3 Corrections to the handoff's own §7 language table

The table's diagnosis is right and three of its file attributions are wrong. The
phrases ship where this says, not where the handoff says:

| §7 row | Handoff says | Actually |
|---|---|---|
| "a swarm of ordinary computers" | `run.html` footer | `run.html:323` **and `index.html:183`** (the landing-page `<h1>`). Two sites, not one. |
| "volunteer machines" | `status.html` empty state | `routes_status.py:146`. `status.html` contains neither phrase — it is slots only. Also `try.html:7`, `try.html:266`. |
| `CREDITS` column | `status.html`, `/node/{id}` | `routes_status.py:173` and `:283`. Same reason. |

Anyone following §7 literally would edit `status.html`, find nothing, and conclude
the phrases were already fixed.

**The wider finding: most of this copy is not in a template at all.** Adding the
prohibited-phrase test surfaced four more sites, every one of them in Python:

| Phrase | Where it actually lived |
|---|---|
| "no cloud, no API keys" | `templates/index.html` meta + hero, **and** `routes_run.py:382` |
| "across volunteer machines" | `routes_run.py:377` |
| "a handful of volunteer machines" | `routes_try.py:102` |
| "on volunteer hardware" | `mcp_server.py:43` (the MCP tool description) |

So the test has a third layer covering `routes_status.py`, `routes_run.py` and
`routes_try.py`. A language rule that only reads `templates/` would have passed
green over all four.

Two instances were left deliberately, both outside the UI vocabulary this rule
governs: `node.py:871` uses "volunteer" for a *person*, and
`scripts/deploy_preflight.py:802` is an operator security warning rather than
product copy.

### 4.4 The theme file is not safely drop-in, and two claims about it are wrong

`README.md` says to copy `_theme.html` in as-is and handoff §2 says
"`test_theme.py` still passes". Neither holds.

**Line 2 of the theme file breaks every page that includes it.** Its header
comment documents itself with a literal nested marker:

```html
<!-- Shared theme layer — …
     Injected into every page by dashboard.py at the `<!-- THEME -->` marker.
```

HTML comments do not nest. The inner `-->` terminates the outer comment, so
roughly twenty lines of designer prose — "This is a SUPERSET of the previous
token set…" — parse as text and render on the page. Confirmed with
`html.parser` against the served `/dashboard`, not by reading.

It also fails `test_theme.py::test_served_pages_carry_the_tokens`, which asserts
the raw marker is absent from the served body: the marker *is* in the body,
inside the theme's own comment. Three of the twenty theme tests failed on the
unmodified file.

**Fix applied:** line 2 now reads "at the THEME marker comment", with no nested
delimiter. Zero tokens and zero colour values changed — the palette is
byte-identical to the handoff. The archived copy keeps the original.

**A separate claim that is simply not true of this repo:** the brief introducing
this port says the new theme "fixes the duplicated `--accent-dim` in the light
block". Master's `templates/_theme.html` declares `--accent-dim` exactly twice —
once in dark `:root` (line 47) and once in `[data-theme="light"]` (line 106),
which is one per block and correct. There was no duplication. Both blocks of the
new file were checked for repeated declarations: none, in either.

### 4.5 The locked screen was unreachable as specified

Handoff §4 says "`401` on any private fetch → show the locked screen", and §11
asks you to "confirm a private page with no credential renders the locked screen
rather than a broken shell". Those two cannot both hold as written: `/dashboard`
is itself a private route, so with `viewer_key` set a browser navigating there
got a raw JSON `401` body and never loaded the page that would have drawn the
screen. The design treats `viewerAuth: 'locked'` as a state of a console that
has already loaded.

Making `/dashboard` public would have fixed it and was rejected — the owner had
just declined a comparable exposure for `/status` (§6), and a second one should
not arrive as a side effect.

**What shipped instead:** the refusal keeps its `401` and its
`WWW-Authenticate: Bearer` and changes only its *body*. A request that prefers
`text/html` gets `templates/locked.html`; a `fetch()` (`Accept: */*`) still gets
the JSON object it parses. No route moved into `_PUBLIC_EXACT`, the page carries
no data, and `/dashboard`, `/status`, `/node/{id}` and `/run/{id}` all still
answer `401`.

The in-console locked screen still exists and still matters — it is what a
session expiring mid-use looks like, where the page is already loaded and a
fetch is what fails.

### 4.6 Additions to the prohibited list

From work that landed after the handoff. `docs/adr/0017` is the normative source
for the first two and already forbids "verified, trustless, tamper-proof, proof of
correct execution".

- The **ledger chain** is tamper-**evident**, never tamper-proof, and never proof
  of correct execution. An operator with database access can rewrite it.
- The **provenance envelope** is a binding of identity. Never a signature, never an
  attestation.
- **Nothing may imply capability evidence affects routing** (§4.2).
- **Nothing may state a quality percentage** (§4.1).

---

## 5. PR #73 — can the UI still read `validator_timeout` as a code defect?

**No.** Asked because #73 made a starved validator runner a distinct channel from a
code defect. The separation holds all the way to the UI, at three levels:

1. **Structural.** `execution/validators.py` `ParsePrecheckResult.__post_init__`
   raises if `runner_failure` and `problems` are both set. A record carrying both
   cannot be constructed. It is deliberately not iterable, indexable or sized, so a
   caller still treating the verdict as a bare list fails loudly.
2. **At the producer.** `orchestrator.py` returns `[]` problems when
   `not precheck.reached_a_verdict` and writes `code_precheck_error` as a separate
   `full_log.json` key.
3. **At the boundary.** `routes_history.py:130` exposes `code_problems` and
   `code_precheck_error` as distinct fields. `routes_run.py:304` renders the
   precheck error as *"The mechanical check did not run to a verdict on this run
   (…), so these files are unchecked rather than known good"* — prose, not a defect
   chip.

**Open gap for the run-detail phase, not this one:** `templates/_dashboard.js`
reads `code_files` (`:534`, `:877`) and reads **neither** `code_problems` nor
`code_precheck_error`. The dashboard shows no defects and no not-checked state. An
empty problem list beside a precheck error means "not checked", not "checked
clean" — whoever builds run detail must render the third state, not two.

---

## 6. `/status` — redaction landed, exposure did not

The handoff §5.5 prescribes two steps: redact `routes_status.py`, then add
`("GET", "/status")` to `_PUBLIC_EXACT`.

**Step one is done in this PR. Step two was deliberately not taken.** The
repository owner was asked and chose to keep `/status` behind the viewer key. Even
redacted, a public `/status` tells a stranger the machine count, the task volume
and the model name.

So: `/status` is redacted **and still viewer-gated**. The redaction is complete and
the allowlist line is a one-line change whenever that decision changes. Do not add
it without asking again.

`/node/{id}` stays viewer-gated permanently — it renders hostname, CPU, GPU, RAM,
`enrollment_id` and `current_task`, which is exactly what `/nodes` is protected
for. This is not a pending decision.

---

## 7. API gaps that still bind the Nodes view

> **Superseded in part by §8.2 below.** Per-unit node assignment *is* served —
> `ExecutionUnitSummaryV1.node_id`, populated from the accepted receipt. The
> paragraph below stands for the node **map**, which draws live machine state
> rather than a finished run's placement, and the rule it ends on stands
> everywhere: degrade, do not invent an assignment.

Handoff §8.2 is still open: **per-unit node assignment is not served.**
`observed_placements` (`execution/contracts.py:483`, `max_length=2`) and per-unit
`depends_on` (`:388`) are. The node map therefore draws dependency structure and
declines to draw machine-to-machine lines, because the datum that would place a
unit on a machine does not exist. Degrade; do not invent an assignment.

---

## 8. The handoff's §8 gap list, corrected and extended

Written while porting §13 (run detail) against
[`run-detail-2026-09-07/`](run-detail-2026-09-07/). Two of the gaps that list
records as open are closed in source; two are new. The rule the archive header
states applies here too: **where source and either document disagree, source
wins, and it gets written down here.**

### 8.1 — closed. Per-run duration is served.

The list says per-run wall-clock duration is unserved and the `WALL` column
should be dropped or the payload extended. `ExecutionResultV1.duration_ms`
exists (`execution/contracts.py`) and `execution/service.py` sets it on every
completion path, alongside `created_at`, `started_at` and `completed_at`.
Measured on a real strategy run: `duration_ms=249`, with all three timestamps
populated.

Run detail still shows **no wall clock in the metric strip**, and that is now a
design decision rather than a gap: the strip is four counts, and the duration
belongs in the timeline where it is a timestamp rather than a headline figure.
The `WALL` column in Runs stays dropped for the same reason and can be
reinstated whenever someone wants it — the number is there.

**The speed multiplier is still not computable**, and that has not changed: it
needs a serial baseline, and nothing records what a task would have taken on one
machine. Its absence is a different kind of absence from the duration's and the
two should not be reported together.

### 8.2 — closed for the node half. Per-unit machine assignment is served.

Both the handoff's §8.2 and this document's own §7 say per-unit node assignment
is not served, and the design draws `machine not recorded` on every unit card
because of it. That is stale.

`ExecutionUnitSummaryV1.node_id` (`execution/contracts.py`) is populated by
`_unit_summary` in `execution/strategies.py` from `DispatchResult.node_id`,
which `Dispatcher._distributed` sets from `receipt.assigned_node_id`. It
survives `ExecutionStore`'s `result_json` round trip and is served by
`GET /v1/executions/{id}`. Verified by instrumenting
`tests/test_execution_strategies.py`, which already asserts the sibling fields
`enrollment_id` and `capability_descriptor_hash` arrive by the same path:
`node_id` came through as `"worker"` in the served JSON.

Per-unit **duration** is served too, on the same object.

So run detail renders what is there: a distributed unit names its machine, a
local unit says `this machine`, and only a unit with neither says
`machine not recorded`. Printing "not recorded" over a run whose machine *is*
recorded would be a false statement on the surface whose whole discipline is not
making them. It is read off the unit and **never** borrowed from `/nodes`, which
is the part of the original rule that still binds — `/nodes` says what a machine
is doing now, not what it did on a finished run.

The Nodes view's node map is unaffected by this note and is not redrawn here.

### 8.7 — closed. The provenance envelope has a route.

`GET /v1/executions/{id}/provenance`, viewer-gated by the same middleware that
gates every other route on an execution, returning `as_export()`. That is the
same object `mycelium-provenance.json` already carries inside the audit bundle,
so it is not a new contract and `check_envelope_against_files` keeps working
unchanged against either copy.

**An absent envelope is a 404, not an empty object.** A legacy run that predates
envelopes has none, and `{}` would say "there is one and it records nothing" —
a different and false statement. The panel renders absent on the strength of it.

The panel is reachable wherever `/run/{id}` is, which is the point: the envelope
travels with the artifacts to whoever was handed the link.

### 8.8 — closed. Chain verification has a route, and it is operator-gated.

`GET /v1/operator/ledger-chain`, returning `verify_ledger_chain().as_dict()`
plus the age of the walk that produced it.

**The prefix is the gate, and the panel's placement follows it.** The archived
handoff and this document both said "viewer-gated" for this route. That is not
what `/v1/operator/` means here: `deploy/Caddyfile.public` refuses that whole
prefix at the edge alongside `/dashboard` and `/metrics`, so a valid viewer key
is not enough to reach it from the public Internet. That is a stricter gate than
`/run/{id}` has. So the chain panel renders in the console — itself behind the
same edge refusal — and never on the shareable run page.
`tests/test_envelope_and_chain_routes.py` reads that line out of the Caddyfile
rather than asserting it in prose, so if the prefix is ever opened the placement
argument fails loudly instead of silently.

**The walk is never shortened.** No checkpoint, no "verified up to index N", no
skipped prefix. The failure being detected is a rewrite of entries that were
already walked once, so any of those would blind the check to exactly the case
it exists for. `test_the_walk_reads_every_chained_entry_every_time_it_walks`
counts the digest recomputations and requires every index from zero, on every
walk.

What is bounded is the *frequency*: one complete verdict is cached for
`LEDGER_CHAIN_WALK_TTL_SECONDS` (30s) and served with `walk_age_seconds`, so the
panel's "walked 12s ago" is a fact about the cache rather than decoration.
`?fresh=1` forces a new walk and is what the panel's own control asks for. The
consequence is stated rather than hidden: a ledger edited inside the TTL still
reads intact until it expires, which is why the age is on screen.

**`/ledger` is unchanged.** It still projects entries without `entry_index`,
`previous_digest` or `entry_digest`. A second way to walk the chain is a second
thing that can disagree with the first.

### 8.9 — new. The run-detail timeline.

**Decision: render it from `/v1/executions/{id}`, and say `+—` with a reason for
the rest. Nothing fetches the audit bundle.**

The design sources the timeline from `full_log.json`, which ships inside the
**audit** bundle. That bundle is deliberately a separate download scope, asked
for by name so a handoff never carries the run's own paperwork by accident.
Auto-fetching it to draw a panel would undo that separation quietly and on every
page view, which is worse than the separation never existing — a reader who
clicked nothing would still have caused the transfer.

What the execution record does timestamp is drawn: `created_at` as `+0.0s`,
`started_at`, `completed_at`, and the manifest's `sealed_at`. What it does not
is one row reading `+—` and naming the absence: per-unit start and finish times
are not timestamped, only each unit's own duration is.

`/run/{id}` reads the run directory's own `full_log.json` off disk, as it always
has — that is how it loads the run at all. The distinction is the *bundle*, not
the file: no surface reaches for the packaged artifact, by HTTP or by opening the
zip. `tests/test_run_detail.py::test_nothing_fetches_the_audit_bundle_to_render_run_detail`
holds that.

*Extend:* add per-unit `started_at` / `completed_at` to `ExecutionUnitSummaryV1`.
The dispatcher already measures the interval it reports as `duration_ms`, so
this is recording two numbers it has rather than deriving a new one, and the
timeline then draws per-unit rows with no new endpoint and no bundle fetch.

*Or drop:* remove the panel. Four timestamps and one stated absence is a thin
timeline, and a panel that is mostly `+—` may be worth less than the space. The
argument against dropping is that the four it does carry — submission committed,
started, terminal state committed, manifest sealed — are exactly the four
moments the durability story turns on, and nothing else on the surface shows
that the terminal state was committed *before* the manifest was sealed.

### 8.10 — new. The archived README's colour table is not the shipped palette.

`run-detail-2026-09-07/README.md` carries a "Design tokens" table, and its
values are not the ones in `templates/_theme.html` — nor the ones in its own
`spec/_theme.html`, which is token-for-token identical to the shipped file (same
54 names, same values in both themes, differing only in one comment and its line
endings).

Eleven of the table's entries disagree with what renders, including `--bg`
(`#030405` against the shipped `#08090A`), `--border` (`#262A30` against
`#1E2126`), `--text-muted` (`#949BA3` against `#868C95`), `--slate` (`#949AA2`
against `#7D848D`) and light `--bg` (`#F1F3F5` against `#FFFFFF`).

This is harmless to the port, which only ever writes `var(--token)`. It is not
harmless to anyone checking contrast: measuring against that table measures a
palette that is not on screen. **Read contrast off the shipped theme, or off the
browser, and never off that table.** The archive's header says so at the top of
the file where the table is.

No token was added, changed or removed in this pass.

### 8.11 — new. The replay line has no source at read time.

The design draws a line for a run that was *returned rather than re-run* —
"pitched again under the same idempotency key, so nothing was built twice" —
and §13.7 cites ADR 0008 and `execution/idempotency.py` for it.

Nothing serves it to a reader. `replayed` is a property of the **POST
response**: `SubmittedExecution.replayed`, surfaced as the
`Idempotency-Replayed` header and then gone. It never reaches
`ExecutionResultV1`, which has no idempotency field of any kind. The durable
`execution_submissions` table stores digest-only rows keyed by
`(requester_scope_hash, idempotency_key_hash)` and is not reachable from an
execution id, and it records the *mapping* rather than the fact that a later
pitch replayed it.

So the line is **not drawn**. This is the gap most likely to be closed by
accident: reading it off a plausible-looking log key renders a line that is
always absent, looks like it works, and would start lying the moment someone
wrote that key for another reason.

*Extend:* add `replayed_at` (or a counter) to `ExecutionResultV1` and set it on
the replay branch of `ExecutionService.submit_idempotent`, which already knows.
Terminal state is monotonic under ADR 0009, so this has to be written as a
separate durable fact about the *submission* rather than as a mutation of the
returned execution — the same shape the provenance envelope uses to reference an
execution without living on one.

*Or drop:* leave it undrawn. A person who pitched the same task twice under one
key already knows; the reader who was handed a link does not need to be told
that the run they are looking at was cheap to produce.
