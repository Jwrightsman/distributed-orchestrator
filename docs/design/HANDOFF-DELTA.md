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

### 8.9 — the extend was taken. The run-detail timeline draws its units.

**Decision: render it from `/v1/executions/{id}`, and say `+—` with a reason for
the rest. Nothing fetches the audit bundle.** The *extend* below was taken:
`ExecutionUnitSummaryV1` now carries `started_at` and `completed_at`, so the
`+—` row that stood for the units is a per-unit row each. The rest of this
section stands as written.

The design sources the timeline from `full_log.json`, which ships inside the
**audit** bundle. That bundle is deliberately a separate download scope, asked
for by name so a handoff never carries the run's own paperwork by accident.
Auto-fetching it to draw a panel would undo that separation quietly and on every
page view, which is worse than the separation never existing — a reader who
clicked nothing would still have caused the transfer.

What the execution record timestamps is drawn: `created_at` as `+0.0s`,
`started_at`, `completed_at`, the manifest's `sealed_at`, and now one row per
unit — `unit 01 ran to +1m 30s` — off the two ends the unit summary carries.
A unit that did not complete says the word rather than changing colour, the
same rule the unit cards follow.

A record that has neither end for any unit keeps the `+—` row and the sentence
that named the absence; a record that has them for some says how many it is
missing, because "not recorded" printed under three drawn rows reads as
applying to all of them. A unit with one end and not the other is counted in
that line rather than placed: one end is not an interval, and a row drawn from
it would sit on the timeline looking like every other row while meaning
something weaker.

`/run/{id}` reads the run directory's own `full_log.json` off disk, as it always
has — that is how it loads the run at all. The distinction is the *bundle*, not
the file: no surface reaches for the packaged artifact, by HTTP or by opening the
zip. `tests/test_run_detail.py::test_nothing_fetches_the_audit_bundle_to_render_run_detail`
holds that.

*Extend — taken.* `ExecutionUnitSummaryV1.started_at` / `.completed_at`, wall-clock
UTC, stamped the same way `execution/service.py` stamps the execution's own
moments so a unit's start is comparable to the run's `created_at`. No new
endpoint and no bundle fetch: the dispatcher had both moments at every return
and was not writing them down.

**They are recorded, never derived.** Nothing adds `duration_ms` to a start to
invent a finish, and nothing subtracts one timestamp from the other to restate
a duration — `duration_ms` stays a monotonic-clock reading, so a host clock
that steps mid-unit cannot change the length a unit reports. The two therefore
disagree by a fraction of a millisecond by design, and by more than that under
a clock step, which is the reason for the split rather than an argument
against it.

On a unit that fell back to local execution the pair brackets the local leg,
the same leg `duration_ms` covers, and not the whole time the unit was
outstanding. `Dispatcher.execute` sums the attempt counts across both legs and
does not widen the interval: a reader who subtracts the two must not get a
number that contradicts the duration beside them. The failed remote attempt
stays visible as `fallback_reason` and in the counts.

*The panel was not dropped.* The four run-level moments it already carried —
submission committed, started, terminal state committed, manifest sealed — are
the four the durability story turns on, and nothing else on the surface shows
that the terminal state was committed *before* the manifest was sealed.

**One thing opening the page found that no test had.** The gutter was `50px`,
which holds seven mono characters at 11px. `_offset` prints eight from ten
minutes onward, so every run past ten minutes had already been wrapping its own
`terminal state committed` row onto two lines at double height — and per-unit
rows multiply that by the unit count. It is `60px` now, sized for the nine
characters the formatter can reach (`+123h 45m`, 58.0px measured in Chromium),
and `test_the_timeline_gutter_fits_the_longest_offset_it_can_print` ties the
column to what the formatter prints rather than to a remembered number.

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

### 8.11 — the extend was taken. The replay line is drawn.

**Decision: extend.** The design draws a line for a run that was *returned
rather than re-run* — "pitched again under the same idempotency key, so nothing
was built twice" — and §13.7 cites ADR 0008 and `execution/idempotency.py` for
it. Nothing served it to a reader, so it was this document's last explicit
*extend or drop*. It is extended, and the line renders in the console.

**What tipped it.** The argument for dropping was that the pitcher already
knows and the link recipient does not need to be told. That holds for the link
recipient, and the placement below follows it. It does not hold for the
operator: `replayed` was a property of the POST response only —
`SubmittedExecution.replayed`, surfaced as the `Idempotency-Replayed` header
and then gone — so nothing could answer whether retry-safety had ever saved a
rebuild on this machine. In a system whose thesis is durable truth about
execution, that was the one guarantee with no durable record.

**One claim in the original section was wrong, and source won.** It said
`execution_submissions` "is not reachable from an execution id."
`idx_execution_submissions_execution_id` (`execution/persistence.py`) has
existed since the table did, and `EXPLAIN QUERY PLAN` confirms a lookup by
execution id searches that index rather than scanning. The mapping is one row
per execution, because the only path that writes one also creates the execution
it names. That is what made the extend cheap: no new table, and the read path
was already indexed.

**The durable fact.** `execution_submissions` grows `replay_count` (NOT NULL
DEFAULT 0) and `last_replayed_at` (nullable), incremented inside the same
`BEGIN IMMEDIATE` that resolves the submission, so the count cannot disagree
with the header that call returns. A **recovered creation is not counted**: it
proves the caller's own first commit landed rather than handing finished work
to a later pitch, and it answers `Idempotency-Replayed: false`.

It stays off `ExecutionResultV1`, as the original *extend* note said it must.
Terminal state is monotonic under ADR 0009 and a replay can arrive after the
run it returns is terminal, so this is a separate durable fact about the
*submission* — the same shape the provenance envelope uses to reference an
execution without living on one.
`tests/test_run_detail.py::test_the_replay_line_is_not_drawn_from_a_field_nothing_writes`
still holds both halves of that: the panel reads no invented log key, and a
replay field appearing on `ExecutionResultV1` fails the test.

**Absent, zero, and counted are three answers, not two.** A run pitched without
an idempotency key has no mapping row; `replay_count: 0` for it would state
that it was submitted under a key and never replayed. So the route answers
`404 keyed_submission_not_found` there — the rule §8.7 set for the envelope —
and the panel draws nothing for both the absent case and the recorded zero. The
two are separately tested, because poisoning the zero guard left the absent
test green: it never reaches the count.

**The route, and why its gate places the panel.**
`GET /v1/operator/executions/{id}/submission`, serving `execution_id`,
`submitted_at`, `replay_count` and `last_replayed_at`, and **no digest** — the
mapping's keys are irreversible but correlatable, since two executions sharing
a `requester_scope_hash` came from one requester and an `idempotency_key_hash`
over a low-entropy key is guessable.

`deploy/Caddyfile.public` refuses `/v1/operator/*` whole at the edge, so this
is the chain's gate rather than the envelope's, and the panel follows it the
same way §8.8 argued: the line renders in the console and never on `/run/{id}`.
`routes_history.py` passes the record and `routes_run.py` does not, and both
halves are tested — the Caddyfile line is read out of the file rather than
asserted in prose, so opening that prefix fails loudly instead of quietly
ending the argument.

**An absolute stamp, not an offset.** Every other row on the panel is inside
the run, and the gutter is sized for what `_offset` can print across a run's
own length — nine characters, six weeks (§8.9). A replay has no such ceiling:
the same task pitched again months later would either overflow that column or
force a width no real row needs. So the line is not a row, carries no offset,
and sits outside the grid.

**Two things opening the page found that no test had.** The stamp broke at its
own hyphen, splitting one date across two lines; it is held on one line now,
and at 375px it is 112.9px inside a 262px column, so holding it costs no
overflow. And the endpoint label read 4.13:1 in the light theme, because it
sits on `--surface-sunken` rather than on the panel, where every other faint
use on this page sits. One token stronger reads 4.65:1 light and 5.99:1 dark;
the endpoint is still distinguished from the sentence, by being mono and
smaller.

---

## 9. The Evals view — what it shows instead of a score

The archived design builds this view around `~57%` (`Mycelium Console.dc.html`,
the `isEvals` block and the `evalCategories` / `evalRuns` / `showcases` data).
§4.1 retires that figure, so the view needed a design pass before any markup:
not a restyle of the old one with the number removed, but an answer to what a
reader should see in its place. This section is that answer and the contract
the view is held to.

### 9.1 Decision: what was measured, and what the instrument can resolve

The view shows **each task's recorded outcome in each committed run, and what
the instrument can and cannot tell apart**, and declines to summarise either
into a pass rate. Four parts, in this order:

1. **A strip of three counts.** Tasks that flipped between the
   identical-configuration pair (`18 of 28`); the smallest net change the
   instrument would notice four runs in five (`11 of 28`); runs recorded.
2. **The noise floor.** The identical pair's paired table, in the order
   `evals/stats.py::render_paired` sanctions: the 2×2 table, n, the discordant
   count with its interval, improved / regressed / net, the p-value, then the
   significance threshold. Never the p-value first, never alone.
3. **Every task, every run.** A grid of the 28 legacy tasks against the five
   committed runs. **No row and no column is totalled.** A column total is a
   pass rate with the percent sign removed.
4. **The corpus.** What has *not* been measured: 100 items, the
   development / confirmatory split and whether its lock still holds, the band
   distribution, and how many confirmatory items have ever run (none).

**No `%` sign renders on this surface at all.** That is stronger than §4.1's
rule, and it is the version a test can hold without a list of exceptions:
interval bounds print as proportions (`0.46–0.79`) and power as "four runs in
five". A bare regex for a quality percentage would have to decide whether
"95% interval" is one; a rule against the character does not.

**Why not the legitimate alternatives.** The discordant rate alone as the
headline is right about which number matters and too abstract on its own — two
adjacent v3 columns disagreeing on eighteen rows says it without the
statistic, so the grid carries it and the strip names it. An aggregate with its
interval does not survive §1.1 of `docs/eval-methodology.md`: the check under
the number was weaker than the claim, and a narrower interval around the wrong
quantity is still the wrong quantity. Per-category rates are dropped
outright — at four to six tasks a category cannot reach significance in any
split, which `evals/compare.py` already prints.

### 9.2 What each cell says

- **The pass decision is `evals/scoring.py::is_success(record,
  require_judge=False)`** — the mechanical grade `eval-methodology.md` §4 makes
  primary, called rather than re-implemented, so the view cannot disagree with
  the harness about what passed. The model judge's score is recorded and gates
  nothing on this view. The noise floor is 18 of 28 under either grade
  (`eval-methodology.md` §1.4),
  and the cells come out 8 / 10 / 8 / 2 under this one against 7 / 10 / 8 / 3
  under the judged `success` field.
- **A pass whose run check was `browser_ok` says `loaded`, not `pass`.** That
  is §1.1 at cell resolution: the word states what was checked. It is not
  rare — **31 of the 75 mechanical passes** across the five committed runs rest
  on "no uncaught JS error and a non-empty body", and `web-snake` reads
  `loaded` in all five columns.
- **A record whose grade could not be decided says `ungraded`**, never `fail`
  (`eval-methodology.md` §4, rule 6), and a pair holding one computes no
  statistic. The committed records contain none; the branch exists so that a
  future record with a missing field is not scored as a failure it never had.
- **A task a run did not include says `not in run`.**

### 9.3 "Identical configuration" means one thing

Same `prompt_set` — the definition `evals/compare.py` and
`scripts/eval_power.py` use. The view does not introduce a stricter or looser
one, because a second definition is a second thing that can disagree with the
published noise floor.

Every identical pair gets its own table. With more than one, **the strip neither
picks one nor pools them**: `stats.MEASURED_DISCORDANT_RATE` is documented as
"the one measured value", and averaging a new pair into it is a decision for
`eval-methodology.md`, not a side effect of opening a view. With none, the two
cells that depend on a pair say the floor is not estimable, and say why.

### 9.4 The route, and absent against empty

`GET /evals`, viewer-gated by default — not in `_PUBLIC_EXACT`, and not under
`/v1/operator/`. The operator prefix exists for facts that should not reach the
edge (§8.8, §8.11); every byte this route reads is committed to a public
repository. It returns the record plus `evals_html`, the fragment built once in
`evals_view.py`, the same shape `/history/{timestamp}` uses for `detail_html`.

**Absent is not empty.** The Docker image copies neither `evals/` nor its
results, so a deployed coordinator has no eval record, and the view says the
server carries none — not "no runs yet", which would claim the harness had
never been run. `tests/test_evals_view.py` reads the Dockerfile's `COPY` lines
rather than asserting this in prose, so copying `evals/` in fails loudly.

### 9.4.1 Four things opening the page found that no test had

Measured in Chromium at 1440, 1024, 768 and 375px in both themes, against the
assembled console page with the real fragment in it. Each is now held by a
test, and each test was poisoned.

- **The paired table scrolled sideways at desktop width.** Every column
  header carried a date and a word (`Aug 11 pass`), which made the 2×2 table
  318px in a 302px side column. The run names sit in a spanning header now.
- **On a phone the task names scrolled away.** The grid is 592px inside a
  311px panel at 375px and scrolls inside it, which is allowed; the page
  itself never overflows. Unpinned, a scrolled row was outcomes with no name.
  The task column and category names are sticky.
- **"two-sided" broke at its own hyphen**, the date fault of §8.11 in a new
  place. Checked by walking every hyphenated word in the view and asking
  whether its range spans more than one line: none do at any width, and with
  the rule switched off in the page the walk finds `two-sided` split at 1440.
- **Two strip labels wrapped at 1024px** beside one that did not. Shortened,
  with the notes beneath carrying the rest. At 375px the cells stack two
  across and `SMALLEST VISIBLE CHANGE` takes two lines; that is left.

Lowest contrast anywhere in the view is **4.74:1**, light theme, muted words
on the shaded identical-pair columns (`--text-muted` on `--surface-hover`);
5.54:1 dark. Computed against each element's composited ground rather than
against the panel, which is the mistake §8.11 recorded. Nothing renders below
11px.

### 9.5 What the archived design states that source contradicts

Checked against `evals/results/*/summary.json` and `results.jsonl`, not by eye.

| The design says | Source says |
|---|---|
| `20260809_0533` — "Re-run of v3 — disagrees with the above on 18 prompts" | Prompt set **v4**. It is a comparison, not a re-run. |
| `20260810_0414` — "Third v3 run. Spread is the instrument, not the model." | Prompt set **v5**. Only two v3 runs exist. |
| The identical runs "differ by 2 points" | They differ by **two tasks** (net −2), about seven points. |
| "cannot resolve a change smaller than about six prompts" | Retracted by `eval-methodology.md` §1.2. At 80% power n = 28 needs 10.8 tasks. |
| `20260811_0523` scored `55%` | 15 of 28 under the judged grade. |
| Category `api`: `—` → `fixed` | 2 of 4 → 3 of 4 under the judged grade. |
| Showcase reliability: Snake `2/10` | The committed record, `scripts/showcase_results/showcase_20260808_162106.jsonl`, has `playable: false` on **all ten** rows. `docs/showcase-ceiling.md` states both numbers correctly — 0/10 met the strict bar, 2/10 were playable once someone pressed start — but the 2 were found by hand and no committed field holds them. |
| Showcase reliability: chart `10/10` | 4 of 4 and 6 of 6 across two committed logs. Holds. |
| Showcase reliability: expense tracker `2/3` | Not in `scripts/showcase_results/`. `SPRINT_PHASE2.md` already withdrew it as n = 3 and re-measured 6/8. |

**The showcase panel is not ported, and not because its data is missing** — the
three logs are committed and a view could read them. It is not ported because
it is a different instrument over different
prompts, and faithfully rendering it needs its own design pass: the record
serves 0 of 10 for the game, the prose everywhere else, including §4.1 above
and `eval-methodology.md` §1.1, says 2 of 10, and a panel printing either one
without the other states half a finding. Within the eval instrument the same
point is carried by the `loaded` cells. **Open.**

**Also dropped, all under §4.1:** the `~57%` card, the `+25pts` v1 → v3 card
(two percentages and an across-prompt-set comparison), the category bars, the
eval-history `SCORE` column, and the 80% target, which the design already kept
out of the metric cards and which has nothing left to sit beside.

---

## 10. The Config view — what it shows, and what it never sends

The archived brief (`HANDOFF-UI.md` §3, "Config — new view") is four sentences:
`config.load()`, never send secret values, `"set"` / `"off"` per key, and a list
of names. Every name on that list resolves in `config.py`. What went stale is
everything around the list: the design file's `configGroups` notes describe
behaviour several keys no longer have, and two words for a credential hide a
third state preflight already distinguishes. This section is the design pass
and the contract `config_view.py` is held to.

### 10.1 Decision: the route, and why its gate is the operator prefix

`GET /v1/operator/config`, returning the record plus `config_html`, the same
shape `/evals` uses. Read-only; the brief's §10 non-goal stands.

**Not viewer-gated like `/evals`.** §9.4 put Evals outside the operator prefix
because every byte it reads is committed to a public repository. Nothing here
is. The route says which authorities are open, which addresses this machine
talks to, where its state lives, and what preflight warned about. So it sits
under `/v1/operator/`, which `deploy/Caddyfile.public` refuses at the edge, and
the view renders in the console, which the same line refuses. The handler calls
`require_viewer` itself, as §8.8's chain route does, so it is refused by its own
code and not only by the middleware. Both halves are tested, and the Caddyfile
line is read out of the file.

**With `viewer_key` empty the route is readable by anyone who can reach the
address**, like every other private route in that state. It was not given a
stricter rule of its own. In that state `/v1/operator/health` already serves
the preflight warnings that name the disabled authorities. What this route adds
to it is the origins of the three configured addresses, the state directory,
and the instance id. That is the exposure, stated rather than implied away, and
it is the state the view's own banner exists to name.

### 10.2 What never leaves the module

- **A credential becomes one of three words.** `viewer_key`, `node_secret`,
  `pitch_key` and `provider_api_key` render `off`, `set`, or `set · short`. No
  length, prefix or mask. `off` means exactly "the gate is open", because
  enforcement treats any non-empty value as a gate. `short` is below
  `MIN_STATIC_CREDENTIAL_LENGTH` (32), the policy `trusted_alpha` refuses to
  start under. The brief's two words would print `set` over a three-character
  key that would stop a deployment.
- **Two authorities sharing a value are named, not valued.** They are compared
  stripped, the way preflight compares them. Naming them tells a key-holder
  nothing a key-holder could not learn by trying the key at the other gate.
- **An address shows its origin.** `ollama_url`, `provider_base_url` and
  `tracing_endpoint` print scheme, host and port. Credentials, path, query and
  fragment are dropped, and the row says `origin only` when something was.
- **Keys `config.json` carries that this version does not read are named,
  never valued.** A misspelt credential is exactly this case: it leaves its gate
  open without a word.

`tests/test_config_view.py` plants a fresh random value in every credential and
in every credential-shaped place: URL userinfo, path, query and fragment, plus
an unknown key. It then requires that no six-character piece of any of them
appears in the response bytes. Separately, every key in `config.DEFAULTS` must
be placed in exactly one group. A key whose name ends like a credential or an
address must be classified as one, so a new setting cannot reach the page
unclassified.

### 10.3 What the archived design states that source contradicts

Checked against the code that reads each key, not against `config.py`'s
comments. Two of those comments are among the stale claims.

A reader is `.get("key"` or `["key"]` in tracked Python outside `tests/`,
`config.py`, the view, and `status.py`. `status.py` prints settings for a
person, and the rows that mention it say so. A looser quoted-string match
counted `"port"` in an unrelated diagnostic dict in
`scripts/deploy_preflight.py`, which is why the pattern is a read. Under it,
exactly three keys have no reader. The same scan finds at least one for each of
the other 44, so a zero means something. The test repeats the scan and fails if
the set and `INERT_KEYS` disagree in either direction.

| The design says | Source says |
|---|---|
| `role_model_map` — "Soft routing — prefers matching models, never blocks work when none match." | **Nothing reads it.** `6483696` (2026-08-21, "route execution through canonical service") removed the only reader, the distributed builder dispatch in `routes_pitch.py`. `status.py` still prints it as "Role routing", and `config.py`'s comment still says the dispatcher prefers matching nodes. The row says the value changes nothing. |
| (`config.py`) `tracing_endpoint` — "OTLP collector endpoint, used only when tracing_export is true." | **Nothing reads it.** `tracing.export_enabled()` checks the two flags and whether the OpenTelemetry SDK imports; spans go to whatever tracer provider the process already has. |
| `port` — `8000`, no note | Read only by `status.py`, to find the server. The coordinator listens where the launch command's `--port` put it. |
| `model` — "Auto-detects down a ladder if missing: gemma4 → phi4-mini → …" | `ollama_client.auto_detect_model` is called by `cli.py` only. The coordinator does not walk a ladder. Note dropped. |
| `last_backup` — `never` | **Nothing records a backup.** `scripts/backup.py` refuses a destination inside the state directory and writes nothing there, so the server cannot know whether one exists. The row says `not recorded`; `never` would be a claim with nothing under it. |
| `workers` — `1` | Not a setting. `validate_single_worker` refuses `WEB_CONCURRENCY`, `UVICORN_WORKERS` or `--workers` other than 1, and the lock refuses a second process. A runtime row that says `one`. |
| `coordinator_lock` — "pid 18244 · since 6d ago. Read from /v1/operator/health" | Health serves the instance id and a boolean. `pid` and `started_at` are on `CoordinatorIdentity`, and this route serves them. |
| `preflight` — "viewer_cookie_secure is false with no TLS in front" as a warning | That pair agrees, and preflight passes it. It warns when `viewer_cookie_secure` and `https_enabled` **disagree**. |
| `deployment_mode` — trusted_alpha refuses to start unless "the paths agree with each other" | No preflight check compares paths. It probes that the state directory and its three subdirectories are writable and that an existing database passes `quick_check`. |
| `verify_rate` — "The only mechanism that notices a node returning plausible-looking garbage." | Agreement is not correctness (`config.py`, ADR 0012), sampling never moves a task, and `trusted_alpha` forces it to 0 whatever it says. |
| Written down — "Pitch keys, by hash, so a repeated pitch finds its run" | **Idempotency keys**, in `execution_submissions`. The pitch key is a credential and is stored nowhere. |
| "A backup captures exactly this set" | It also copies `config.json`, so a backup holds all three keys. The panel says so. |
| `artifact_retention_seconds` — "The run record and its share survive — artifact links then 404" | Not established by this pass. The note keeps only what `docs/ARTIFACTS.md` and `execution/artifacts.py` state: a terminal run's registered files may be deleted after this. |

Both lists on the durability panel are tied to source. Every table a
"written down" item names must be created somewhere, and every phrase in
`scripts/backup.py`'s `not_included` list must be covered by a "gone on
restart" item.

**Out of scope and recorded:** `status.py`'s "Role routing" line and the two
stale comments in `config.py`. Whether to delete the dead keys or wire them
again is a decision for a sprint, not for a view.

### 10.4 The fail-open banner on Config

The design suppressed the global fail-open banner on Config, "where the more
actionable version already lives", and `_dashboard.js` has carried
`BANNER_SUPPRESSED_ON = ['config']` since phase 1, inert until the view existed.
It is live now, **but only once the Config fragment has drawn its own
warning** (`[data-gate-open]`). A Config view that failed to load carries none,
and suppressing unconditionally would leave that page silent about an open
server. The loader re-evaluates the banner after it fills the view.

The view's banner names which of the three keys are off and what each leaves
open: read, join, spend. It is `is-danger` when reading is open, which is the
state the global banner covers. With only joining or spending open it is
`is-warn` and carries no `data-gate-open`, because in that state there is no
global banner for it to replace.

### 10.5 Deliberately not done

- **Writing.** The brief's non-goal holds; the one key that gates every private
  read would be editable from the page it gates.
- **A "changed from default" marker.** Useful, and a separate decision about
  what the view is for.
- **Any claim of live values.** `config.get()` caches for the life of the
  process, so the lede says a change takes effect on restart.
- **Group-level summaries.** No "3 of 7 set" count: the banner says what is
  open, by name.

### 10.6 Two things opening the page found that no test had

Measured in Chromium at 1440, 1024, 768 and 375px in both themes. The page was
the assembled console with the real fragment in two states: everything off,
and a mixed configuration with long values, a shared key, a short key and
unknown keys.

- **A state directory broke at its own hyphen**, in the 11rem value column at
  1440px: the date fault of §8.11 and §9.4.1 in a third place. A value longer
  than the column now spans the value and note columns, on a phone too. A
  hyphenated path segment is held together, and the slashes stay breakable.
  With the fix switched off in the live page, the hyphen walk finds the split
  again.
- **"abuse-risk" ended a line at 1024px.** The view's hyphenated copy is
  reworded or held, and a test requires that of every hyphenated run in the
  visible text, with no allowlist.

No page overflow, nothing clipped inside a panel and nothing escaping its row
at any width. Nothing renders below 11px. Lowest contrast is **5.00:1**, light
theme, panel labels (`--text-muted` on `--surface`); 5.67:1 dark, the banner
body. Both were computed against each element's composited ground. The three
longest keys (36–39 characters) wrap after an underscore at 1024px and above.
That is left: widening the key column to hold them costs every note its width.

---

## 11. The Connect view — joining, pitching, and what each one costs

The archived brief (`HANDOFF-UI.md` §3, "Invite — new view") is two sentences:
static copy plus config flags for the warning panel, with consent bullets lifted
from `AGENTS.md`. The design file calls the view **Connect** in its sidebar and
`invite` internally. The console follows it: the tab reads Connect and the ids
are `invite`. `invite_view.py` builds the fragment and `GET /v1/operator/invite`
serves it with its record, the same arrangement Config uses.

### 11.1 Decision: the route, and why its gate is the operator prefix

The static copy could be a template. The warning panel can't be. It says which
of the three authorities are off, whether HTTPS is declared, and whether a
machine may join without an enrolment. That is a subset of what
`/v1/operator/config` serves, so the route sits under the same prefix, is
refused at the edge by `deploy/Caddyfile.public` and calls `require_viewer`
itself. A key becomes a word through `config_view.credential_state`, so the
two views cannot disagree about whether a key is short.

### 11.2 What the archived design states that source contradicts

| The design says | Source says |
|---|---|
| `python join.py https://mycelium.jw.dev` is the command | `worker_installer.py` replaced it (#74, #75). `docs/JOIN.md` gives clone, `cd`, `python worker_installer.py`. The installer asks for the address and reads the invitation code with the echo off, so the command carries neither. The view prints no address, because the coordinator can't know the one visitors reach it at. |
| The address is sent as-is | Plain `http://` is refused for any host that isn't loopback, and nothing overrides that. The panel says so and warns when `https_enabled` is false. |
| "Nothing is installed until they press a key" | They must type `yes`. `join.py` has `--yes`; the installer's `--yes` is for uninstall only, so the installer can't skip its consent. |
| "No cloud inference, no API keys, no telemetry" | Prohibited by the handoff's own §7, and `provider` exists. The item now says what the installer's paragraph says: nothing is reported to anybody but this coordinator. |
| Warning: "Two settings are off right now: `node_secret`, `pitch_key`" | Read from settings. It lists every authority that is off (reading included), any that is short, `https_enabled` when false, and `node_enrollment_mode` when not `required`. With none of those it says the coordinator is ready to share. With `node_secret` off the installer still asks for a code, but it accepts anything typed. |
| `/status.json` — "node count, tasks completed, uptime and active model" | Also inference status, runs built and contributors. It serves no task text and no hostnames. |
| "They all present the same `pitch_key` … there is one lever" | **Two keys, two levers.** `mcp_server.py` sends `VIEWER_KEY` as well, because `/jobs/{id}` and `/projects` are private. An app that can read its result back can read every run, machine and project. Changing `viewer_key` also ends every browser session. `docs/MCP.md` did not mention `VIEWER_KEY` either, so with a viewer key set `pitch_task` worked and every read after it was refused. It now names it and lists that failure under troubleshooting. |
| "Every machine on this network has its own enrolment" | Only with `node_enrollment_mode` at `required`. In `compat` an older worker may register on the shared code alone. The installer always enrols. |
| "Five pitches a minute, per address" | `pitch_rate_max` / `pitch_rate_window`, read from settings. |

Unchanged and still true: nothing records which door a pitch came through
(handoff §8.6).

### 11.3 Tied to source

`tests/test_invite_view.py` checks each consent item against a phrase in
`worker_installer.consent_text`, with the rich markup stripped and the
whitespace collapsed. It checks that the tool list matches the tools
`mcp_server.py` registers, in both directions. It checks that the MCP server
still sends a viewer key, that the routes it reads are still private, and that
the join commands are the ones `docs/JOIN.md` gives. Any one of those changing
fails a test that names the sentence it undermines.

The Nodes view's empty state said `python join.py http://YOUR_IP:8000`, which
is refused for any host that isn't loopback. It now links to Connect, and the
Nodes header carries the design's "Invite a machine" button. `templates/index.html`
still offers `python join.py <origin>` as a copy button. The landing page has
its own port in the handoff's order, so it is recorded here and not changed.

### 11.4 What opening the page found

Measured in Chromium at 1440px and 375px, in both themes, with the default
settings (every authority off). The page does not overflow at either width, no
element sticks out of its panel, and nothing renders below 11px. Copy, "Open
config" and the Nodes "Invite a machine" button were each clicked and did what
they say.

- **"WHAT IT CAN THEN DO" measured 4.44:1** in the light theme: `--text-faint`
  on `--surface`, below AA. It is `--text-muted` now. The lowest ratio left in
  the view is 4.72:1, the section dividers on `--bg` in the light theme.

### 11.5 The Sept 18 design carries the same copy

The Sept 18 bundle (`docs/design/console-2026-09-18/`) redraws this view with
sans section labels and 10px corners, but every sentence in it is the one
§11.2 corrects. Those two style changes belong to the shell pass (that
handoff's §15.1–15.2), which moves every view's panel labels at once. They are
left for that pass so this view matches its neighbours until then.

---

## 12. The Sept 18 theme — ported, with six repairs

The Sept 18 handoff (`docs/design/console-2026-09-18/`) says to copy
`spec/_theme.html` over `templates/_theme.html` as-is. That would have broken
every page, as the Aug 28 file did (§4.4). `templates/_theme.html` is that file
with six repairs, each named in its header. The palette, radii, lift, keyframes
and element rules are the handoff's, and so is every colour value but one.

### 12.1 The repairs

| # | As received | Why it could not ship | Now |
|---|---|---|---|
| 1 | Line 2 names the marker as `` `<!-- THEME -->` `` inside the header comment | The same fault as §4.4, reintroduced: HTML comments do not nest, so about forty lines of designer notes rendered as text at the top of every page | "at the THEME marker comment" |
| 2 | The light block has no `color-scheme` | Scrollbars, form controls and the date picker stay dark on a light page | `color-scheme: light` |
| 3 | Mono stack `…, Menlo, monospace` | On Windows, without Plex, that falls through to Courier New | The previous stack's `'SF Mono'`, `'Cascadia Code'` and `Consolas` kept, and the sans stack's `Roboto, system-ui` |
| 4 | The floating theme-switch script is gone | Pages without their own switch (`/status`, `/try`, the landing page) lose any way to change theme | Kept verbatim from the previous file |
| 5 | `--accent-text: var(--accent)` in both themes | The previous light theme gave text a darker green, `#12692E`. Without it every green label on the accent wash is 4.44:1: the landing page's step numbers and the dashboard's Pitch button | Light block sets `--accent-text: var(--accent-strong)`, which is `#12692E` in the new palette too. No new colour |
| 6 | Light `--text-faint: #74757A` | 4.21:1 on `--surface` and 4.40:1 on `--bg`, under a header that promises 4.5:1 on both | `#6F7075`: the smallest darkening on the same hue that clears every ground (4.53:1 on `--surface`), and still lighter than `--text-muted` |

Repair 6 is the one colour value changed. `tests/test_theme.py` now computes
the promise itself: `--text-muted` and `--text-faint` against `--bg`,
`--surface` and `--bg-elevated`, in both themes. Against the file as received
it fails on exactly the two light-theme pairs above and passes the other ten.

Separately, `/try`'s INVITE ONLY badge set warning *fill* colour as text:
4.40:1 in dark on master, 3.74:1 on the lighter Sept 18 surface. It uses
`--warn-text` now.

### 12.2 The element rules were measured, not assumed

The new file adds page-wide rules the old one did not carry: `body`
(13.5px, tabular figures), `a`, `a:hover`, form-control font inheritance,
`::placeholder`, `:focus-visible`, `::selection` and the scrollbar. Every page
has its own reset and `body` rule, but none of them sets a font size. So on
`/`, `/try`, `/status` and `/dashboard`, in both themes, every element's
computed style was recorded with the theme's element rules in place and again
with them deleted from the live stylesheet:

- **No visible text changes size.** 13.5px reaches containers only. Every
  element that holds text sets its own size, except a screen-reader-only label.
- The brand links (`a.brand`, `a.nav-brand`) were the browser's default link
  blue on master, and the console's icon buttons were Arial. `a { color }` and
  the form-control inheritance fix both. `a.btn-quiet` loses an underline it was
  never meant to have.
- The 2.5px shift in centred columns is the new 10px scrollbar.

### 12.3 Contrast, measured on the served pages

Every visible text element was measured against its composited ground: 1,994
elements across `/`, `/try`, `/status` and all eight console views, at 1280px,
in both themes. The theme was set before load, because a colour transition on
a switched page does not advance in a hidden frame, and the first pass read
half-transitioned colours as failures on master too.

| | failing | lowest |
|---|---|---|
| master | 3 (the INVITE ONLY badge in both themes, the Nodes "MAP" label in light) | 3.85:1 |
| as received | 6 (repairs 5 and 6, the badge, and the Nodes points footnote) | 3.74:1 |
| as shipped | **0** | 4.78:1 dark, 4.53:1 light |

There is no sideways scroll at 375px on any page, no leaked comment text, a
theme switch on every page, and `color-scheme` follows the theme.

### 12.4 Not done here

The shell (the handoff's §15–18: sans section labels, collapsed rail,
full-bleed Overview, status bar), the node map (§16.6, §19) and the
per-view restyles. The new radius tokens (8 / 6 / 10 / 16px) move every panel
that reads `var(--radius*)`; markup with a radius written into it (for
example `rd-panel`, 5px) moves with its view's port.

---

## 13. The Sept 18 redesign — what was ported, and where it departs

> **Subject:** [`docs/design/console-2026-09-18/`](console-2026-09-18/), a
> whole-console design received 2026-09-18 and archived unchanged. §12 covers
> its theme; this section covers everything built on top of that theme.

The design is a set of `.dc.html` component files plus a spec. It is a picture
of a console, drawn without access to the running server, so it states things
about this repo that are not true and shows numbers no route returns. It was
treated the way §0 says to treat a handoff: **non-normative**. Every figure on
every ported surface now comes from a route, or is labelled as not coming from
one.

### 13.1 What landed, in order

| Commit | Surface |
|---|---|
| `494afa9` | The theme, with the six repairs §12 records |
| `962373b` | The shell: rail, identity block, contextual topbar, command palette, status bar |
| `5c81f12` | Overview — the thread, the step timeline, the floating pitch window |
| `07ee186` | Runs — the hairline table, grouped by day |
| `f6c4c26` | Nodes — the stats line, the live map, the machine rows |
| `826cfb1` | Gallery, Projects, Guild |
| `c532529` | Evals, Config, Connect, run detail |
| `14184fe` | Evidence — a view the design adds, fed from the operator route |
| `2c704c6` | The landing page |
| `690f304` | The Try page, and the share card at `/s/{token}` |
| `fffbd04` | `/status` and each machine's page |
| `f9b180e` | The run page's header |
| `af42c09` | The map's two drawn scales |

### 13.2 Design claims about this repo that are false, and what replaced them

These are the ones worth recording, because each was drawn as a confident fact
and each would have shipped as one.

| The design says | This repo | What shipped |
|---|---|---|
| The circuit breaker holds `/work` at 429 for **45s** | `server_state._BLACKLIST_DURATION = 60`, on `/tasks/next` | "three consecutive failures, so `/tasks/next` answers 429 for 60s" — and `tests/test_node_map.py` now reads both constants |
| The planner produces **3-5 subtasks** | `prompts/v2.py` *asks* for 3-5; the model may return fewer | "the planner is asked for 3-5 subtasks" — a ceiling, never a count |
| A quality figure (`~57%`) on the landing page | Nothing measures it | Withdrawn everywhere. Counts, not rates, below the evidence sample floor |
| `/status` answers "without a sign-in" | It sits behind the viewer key; only `/status.json` is public | The sentence is gone; the page says which of the two is public |
| Nodes' empty state offers an `http://` join | The edge refuses it | The installer command, and `node.py --server` |
| A "built" claim on unchecked runs | Assurance is one of five labels | The recent strip takes its verdict from the same derivation as everywhere else |

### 13.3 Deviations from the design, and why

* **The WALL column stays dropped** (§8.1). The design restores it; no route
  returns wall time per stage that is not the animation clock.
* **Chips count loaded runs, not totals.** The design's chips read as network
  totals. `/history` is paged, so they would be wrong the moment a page did not
  hold everything. They are labelled as loaded.
* **One derivation, many indicators** (§12.1, §17.2). The design paints verdict,
  lamp and chip from three different places. They now all read one function, and
  `tests/test_theme.py` fails a second one.
* **Points, not credits**, throughout — and no drawn surface carries either.
* **The map's drawn scales are labelled drawn**, in five places: the switcher,
  the head, a DATA row on every panel, a dashed outline on every drawn bubble,
  and the caption in the warning colour. The design labels them once.
* **The swarm scale draws a federation this repo does not have**, and says so in
  its own caption rather than leaving it to be inferred from the shape.
* **No drawn bubble links to `/node/{id}`.** A drawn machine has no page; a link
  to one would 404 at best.

### 13.4 Measured, not eyeballed

Every ported surface was measured in the browser rather than looked at: contrast
against the composited ground, `scrollWidth` against the viewport, and the set
of elements escaping it, per theme, per width (1280 and 375). The harness and
its two traps are in §12.3 — a hidden frame freezes a colour transition, and a
fixed element looks guilty in an overflow report until you hide it and re-measure.

Label collision on the map is measured the same way: in screen pixels after pan
and zoom, against both the names already placed and the bubbles themselves.

### 13.5 Not done here

The bundle's six design files are not ported to the same depth, and three of
them are the remaining work. All three draw the run page, which the console
already has.

`Ledger Chain.dc.html` and `Provenance Envelope.dc.html` describe surfaces that
exist: both are rendered by `run_detail.py` on `/run/{id}`, fed by
`/v1/operator/ledger-chain` and `/v1/executions/{id}/provenance`. `c532529`
moved them onto the design's cards, corners and section labels through the
shared panel rules, so they are not on the old theme — but neither was compared
against its own drawing element by element, which is what the other views got.
`Run Detail Surface.dc.html` is a second drawing of the same page, and `f9b180e`
ports its header only.

So what is left is a restyle of one page against three drawings, not a new view.
An earlier revision of this section claimed those surfaces did not exist at all;
they do, and the claim was wrong.

The design's own hardcoded figures are not ported anywhere. `support.js` in both
folders is the design tool's React runtime, not data, and nothing in it runs
here.
