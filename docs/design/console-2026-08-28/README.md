> **Status:** Point-in-time design handoff; non-normative.
> **Handoff date:** 2026-08-28
> **Repository revision when archived:** `e91561f01e348e84884ea8cc5d367ee73a870568` (`master`, 2026-09-06)
> **Authority:** Current source code, protocol documentation, and accepted ADRs
> supersede this handoff when they conflict. Master merged PRs #62–#76 after this
> document was written, and parts of it are stale — see
> [`docs/design/HANDOFF-DELTA.md`](../HANDOFF-DELTA.md), which is the contract for
> the console port. **Where this document and the delta disagree, the delta wins.**
> **Archive note:** The five files are archived as received. `_theme.html`,
> `Mycelium Console.dc.html` and `support.js` are byte-identical to the handoff and
> carry no header, because the first is production code meant to be copied verbatim
> and the other two must still render in a browser. `support.js` exists only to make
> the design file render; it is not production code and must not be copied into
> `templates/` or `static/`.

# Mycelium console — design handoff

Read this file first, then `HANDOFF-UI.md`. This one says how to work; that one
says what to build, element by element.

## What is in this folder

| File | What it is |
|---|---|
| `README.md` | You are here. Rules of engagement. |
| `HANDOFF-UI.md` | The specification. Port order, element→endpoint map, copy rules, known API gaps. ~750 lines; it is the real document. |
| `_theme.html` | **Production code.** A drop-in replacement for `templates/_theme.html`. Copy it in as-is. |
| `Mycelium Console.dc.html` | **Design reference.** Every screen. Open it in a browser. |
| `support.js` | Makes the design file render. Keep it next to it; never copy it into the repo. |

Open `Mycelium Console.dc.html` directly in a browser — no server, no build. The
sidebar switches views; the footer of the sidebar switches to the public pages
(landing, try, share card, status, machine).

## The one thing to get right

**The design file is a reference, not source.** It is a single self-contained
HTML prototype written in a React-flavoured component format. The target is
Jinja templates plus vanilla JS. So this is a translation, not a copy:

- **Take** the markup structure, the exact copy, the token names, the spacing,
  the states, and the decisions in `HANDOFF-UI.md`.
- **Do not take** the component class, `renderVals()`, `sc-for` / `sc-if`, or
  `support.js`. Their equivalents in the repo are Jinja loops and the existing
  `showTab()` / `refresh()` / `loadHistory()` functions in `_dashboard.js`.
- Every value in the design is already a `var(--token)` from `_theme.html`, so
  markup transfers without a colour audit.

Where the design shows data, `HANDOFF-UI.md` §3 names the endpoint that serves
it. Six things have no endpoint — §8 lists them. **Extend the endpoint or drop
the element; do not invent a value.**

## Constraints that will fail the test suite

- **No hardcoded colours.** `tests/test_theme.py::test_no_page_hardcodes_a_colour`
  rejects any literal hex or `rgba()` in a template or in `_dashboard.css`.
  Everything is a token.
- **11px type floor.** The dashboard is filmed for the demo.
- **44px hit targets** on the public pages (landing, try, status) — most
  visitors arrive on a phone.
- **`--accent` is status only** (pass / connected / ok). `--mark` is the brand
  colour and is deliberately neutral. Never use green as decoration.
- **Prohibited language** — `HANDOFF-UI.md` §7 has the table. Three banned
  phrases still ship in templates today; the table names them and their
  replacements.

## Suggested order

1. `templates/_theme.html` ← copy `_theme.html` from this folder. Nothing else
   works until the tokens exist.
2. `templates/dashboard.html` + `_dashboard.css` + `_dashboard.js` — view by
   view. Keep every existing `id` and the existing JS entry points.
3. `templates/index.html`, `templates/try.html`.
4. `templates/run.html`, `templates/status.html` — see §5.5. `/status` needs
   redacting **before** it is added to the public allowlist, not after.
5. The share page — §5. Structural, not a restyle.

Ship one file per commit and run `pytest` between them.
