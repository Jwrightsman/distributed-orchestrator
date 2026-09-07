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

Handoff §8.2 is still open: **per-unit node assignment is not served.**
`observed_placements` (`execution/contracts.py:483`, `max_length=2`) and per-unit
`depends_on` (`:388`) are. The node map therefore draws dependency structure and
declines to draw machine-to-machine lines, because the datum that would place a
unit on a machine does not exist. Degrade; do not invent an assignment.
