# Eval run — 20260806_142528

**Model:** `qwen3.5:4b` · **Prompt set:** `v1` · **Mode:** real

**Success rate: 33%** (3/9) — target is 80%

Mean judge score: 4.2 · Mean wall clock: 2028.0s · Mean subtasks: 4.0

## By category

| Category | Pass | Total | Rate |
| --- | ---: | ---: | ---: |
| cli_tool | 1 | 3 | 33% |
| web_app | 2 | 6 | 33% |

## Where runs failed

| Failure stage | Count |
| --- | ---: |
| no files extracted | 4 |
| parse failed | 0 |
| execution failed | 1 |
| wrong artifact kind | 0 |
| missing keywords | 1 |
| judged below bar | 0 |
| judge unparseable | 0 |
| pipeline error | 4 |

## Per prompt

| Prompt | Category | Files | Parses | Executes | Judge | Secs | Pass |
| --- | --- | ---: | :---: | :---: | :---: | ---: | :---: |
| `web-snake` | web_app | 1 | ✓ | ✓ | 5 | 3020 | ✅ |
| `web-pomodoro` | web_app | 1 | ✓ | ✓ | 4 | 2389 | ✅ |
| `web-todo` | web_app | 0 | ✗ | ✗ | – | 2045 | ❌ |
| `web-calculator` | web_app | 0 | ✗ | ✗ | – | 1699 | ❌ |
| `web-memory-game` | web_app | 0 | ✗ | ✗ | – | 2806 | ❌ |
| `web-markdown-preview` | web_app | 1 | ✓ | ✓ | 4 | 3196 | ❌ |
| `cli-todo` | cli_tool | 1 | ✓ | ✗ | 4 | 689 | ❌ |
| `cli-file-organizer` | cli_tool | 0 | ✗ | ✗ | – | 1602 | ❌ |
| `cli-password-gen` | cli_tool | 1 | ✓ | ✓ | 4 | 806 | ✅ |

## Failure detail

- **web-todo** — pipeline error: UnicodeEncodeError: 'charmap' codec can't encode character '\u2705' in position 489: character maps to <undefined> · no files extracted · wrong artifact kind (wanted html) · exec ?:
- **web-calculator** — pipeline error: UnicodeEncodeError: 'charmap' codec can't encode character '\u2716' in position 2915: character maps to <undefined> · no files extracted · wrong artifact kind (wanted html) · exec ?:
- **web-memory-game** — pipeline error: UnicodeEncodeError: 'charmap' codec can't encode character '\U0001f389' in position 5098: character maps to <undefined> · no files extracted · wrong artifact kind (wanted html) · exec ?:
- **web-markdown-preview** — missing keywords: innerhtml
- **cli-todo** — exec error: AttributeError: '_SubParsersAction' object has no attribute 'add_argument'
- **cli-file-organizer** — pipeline error: UnicodeEncodeError: 'charmap' codec can't encode characters in position 5068-5070: character maps to <undefined> · no files extracted · wrong artifact kind (wanted python) · exec ?:
