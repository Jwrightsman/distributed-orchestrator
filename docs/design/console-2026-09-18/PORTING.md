# Porting the Mycelium console — standing instructions

Drop this file at the root of `distributed-orchestrator` (or merge it into the
`CLAUDE.md` already there) before starting the port. It is the short version of
the rules; `spec/HANDOFF-UI.md` is the long one.

## What you are doing

Recreating the designs in `design/*.dc.html` as Jinja templates + vanilla JS in
this repo. **Translation, not transcription.** The design files are a
React-flavoured prototype format; the target is
`templates/*.html` + `templates/_dashboard.css` + `templates/_dashboard.js`.

Take: markup structure, exact copy, token names, spacing, states, interaction
behaviour. Do not take: the component class, `renderVals()`, `<sc-for>`,
`<sc-if>`, `<x-import>`, `support.js`. Their equivalents here are Jinja loops
and the existing `showTab()` / `refresh()` / `loadHistory()` functions.

## Start here, in this order

1. **`templates/_theme.html` ← `spec/_theme.html`, copied verbatim.** Nothing
   looks right until the tokens exist. This file was regenerated Sept 18 from
   the current design; if a previous port used an older copy, re-copy it.
2. `templates/dashboard.html` + `_dashboard.css` + `_dashboard.js`, one view per
   commit. Keep every existing `id` and every existing JS entry point.
3. `templates/index.html`, `templates/try.html`.
4. `templates/run.html`, `templates/status.html` — spec §5.5. **`/status` must
   be redacted before it goes on the public allowlist, not after.**
5. The share page — spec §5. Structural, not a restyle.

Run `pytest -q` and `ruff check .` between files. Ship one file per commit.

## Hard constraints — these fail CI or the demo

- **No hardcoded colours.** `tests/test_theme.py::test_no_page_hardcodes_a_colour`
  rejects any literal hex or `rgba()` in a template or in `_dashboard.css`.
  Every colour is `var(--token)`. The design obeys this already, so markup
  transfers without a colour audit.
- **11px type floor.** The dashboard is filmed for the demo recording as the
  "nodes connected" verification glance.
- **44px hit targets** on the public pages (`index.html`, `try.html`,
  `status.html`) — most visitors arrive on a phone from a link in a post.
- **`--accent` is status only** (PASS / connected / ok). `--mark` is the brand
  colour and is deliberately neutral. Green is never decoration.
- **CI runs Python 3.14.** Code that passes on 3.11 can fail there, `asyncio`
  especially.

## Never invent a number

Every figure in Evals, the landing page's honest-capability section and the
config notes is lifted from the repo (`SPRINT_PHASE2.md`, `config.py`,
`AGENTS.md`, `evals/`). If a figure is not in the repo, it does not go on
screen.

Where the design shows data that no endpoint serves, spec §3 and §8 name the
gap. **Extend the endpoint or drop the element.** Do not fill it with a
placeholder, and do not derive a percentage from wall-clock time.

The design marks its own unserved actions: seven controls are
`aria-disabled="true"` with a tooltip naming the missing route. When you land
the route, remove the `aria-disabled` and the cursor override too — not just the
tooltip.

## Two claims that must never read as one

The **sealed manifest** proves the files match a locally recorded baseline. The
**provenance envelope** binds who produced them, under which identity, with
which model, checked by which validators. Neither says the output is correct,
and they do not combine. So they get different grammatical classes: the manifest
keeps its chip; the envelope gets a sentence with **no chip, no lamp, no
colour**. Nothing may put a shared `INTEGRITY` or `TRUST` header over both.

An intact **ledger chain is not green.** Green means PASS / connected / ok, and
"no entry changed without every link after it also being recomputed" is none of
those. Never *tamper-proof*, never *proof of correct execution*, never
*signature* or *attestation* for the envelope.

Spec §7 has the full prohibited-language table, including three banned phrases
that still ship in templates today and what to replace them with.

## Say it in plain language

The audience for this console includes its owner, who is not a developer. A
security-relevant control states its consequence in words: "No key is set, so
anybody who can reach this address can read the console", not "viewer auth:
disabled". A control that cannot act says why: "A compatibility session has no
durable identity to revoke."

## When in doubt

Each view's form was chosen for what it shows, not for consistency with the
others — spec §15–19 record what each trade was. If you change a layout, know
what you are trading. If a decision looks arbitrary, it is probably in the spec
with a reason; search it before overriding it.
