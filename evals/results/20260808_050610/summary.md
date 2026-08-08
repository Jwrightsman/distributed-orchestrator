# Eval run — 20260808_050610

**Model:** `qwen3.5:4b` · **Prompt set:** `v3` · **Mode:** real

**Success rate: 61%** (17/28) — target is 80%

Mean judge score: 4.54 · Mean wall clock: 1371.3s · Mean subtasks: 3.68

## By category

| Category | Pass | Total | Rate |
| --- | ---: | ---: | ---: |
| algorithm | 3 | 4 | 75% |
| api | 3 | 4 | 75% |
| cli_tool | 3 | 5 | 60% |
| data_processing | 3 | 5 | 60% |
| vague | 2 | 4 | 50% |
| web_app | 3 | 6 | 50% |

## Where runs failed

| Failure stage | Count |
| --- | ---: |
| no files extracted | 2 |
| parse failed | 1 |
| execution failed | 7 |
| wrong artifact kind | 0 |
| missing keywords | 1 |
| judged below bar | 1 |
| judge unparseable | 0 |
| pipeline error | 0 |

## Per prompt

| Prompt | Category | Files | Parses | Executes | Judge | Secs | Pass |
| --- | --- | ---: | :---: | :---: | :---: | ---: | :---: |
| `web-snake` | web_app | 1 | ✓ | ✓ | 5 | 2094 | ✅ |
| `web-pomodoro` | web_app | 1 | ✓ | ✓ | 5 | 1430 | ✅ |
| `web-todo` | web_app | 0 | ✗ | ✗ | 1 | 641 | ❌ |
| `web-calculator` | web_app | 1 | ✓ | ✓ | 5 | 1655 | ✅ |
| `web-memory-game` | web_app | 1 | ✓ | ✓ | 1 | 2419 | ❌ |
| `web-markdown-preview` | web_app | 2 | ✗ | ✗ | 4 | 2762 | ❌ |
| `cli-todo` | cli_tool | 1 | ✓ | ✓ | 5 | 758 | ✅ |
| `cli-file-organizer` | cli_tool | 1 | ✓ | ✓ | 5 | 1224 | ✅ |
| `cli-password-gen` | cli_tool | 2 | ✓ | ✗ | 4 | 1153 | ❌ |
| `cli-word-count` | cli_tool | 1 | ✓ | ✗ | 5 | 736 | ❌ |
| `cli-unit-convert` | cli_tool | 1 | ✓ | ✓ | 5 | 1341 | ✅ |
| `data-sales-csv` | data_processing | 1 | ✓ | ✓ | 5 | 1023 | ✅ |
| `data-log-parser` | data_processing | 1 | ✓ | ✗ | 4 | 855 | ❌ |
| `data-json-to-csv` | data_processing | 1 | ✓ | ✓ | 5 | 440 | ✅ |
| `data-dedupe` | data_processing | 1 | ✓ | ✗ | 5 | 659 | ❌ |
| `data-temperature-stats` | data_processing | 1 | ✓ | ✓ | 5 | 696 | ✅ |
| `api-todo-fastapi` | api | 1 | ✓ | ✓ | 4 | 1001 | ✅ |
| `api-url-shortener` | api | 1 | ✓ | ✗ | 4 | 1146 | ❌ |
| `api-http-server` | api | 1 | ✓ | ✓ | 5 | 973 | ✅ |
| `api-weather-mock` | api | 1 | ✓ | ✓ | 5 | 787 | ✅ |
| `algo-fizzbuzz-tests` | algorithm | 1 | ✓ | ✓ | 5 | 1117 | ✅ |
| `algo-binary-search` | algorithm | 1 | ✓ | ✓ | 5 | 549 | ✅ |
| `algo-matrix` | algorithm | 1 | ✓ | ✗ | 5 | 765 | ❌ |
| `algo-roman` | algorithm | 3 | ✓ | ✓ | 5 | 627 | ✅ |
| `vague-something-useful` | vague | 1 | ✓ | ✓ | 5 | 3346 | ✅ |
| `vague-make-a-game` | vague | 0 | ✗ | ✗ | 5 | 2400 | ❌ |
| `vague-help-with-homework` | vague | 1 | ✓ | ✗ | 5 | 3406 | ❌ |
| `vague-organize-my-life` | vague | 1 | ✓ | ✓ | 5 | 2395 | ✅ |

## Failure detail

- **web-todo** — no files extracted · wrong artifact kind (wanted html) · missing keywords: localstorage, addeventlistener · exec no_files: · judge 1/5
- **web-memory-game** — judge 1/5
- **web-markdown-preview** — index.html: missing closing </html> tag — the document is truncated; index.html: unbalanced <script> tags — the file is likely cut off · missing keywords: addeventlistener, innerhtml · exec browser_ok: no JS errors
- **cli-password-gen** — exec missing_dependency: ModuleNotFoundError: No module named 'password_generator'
- **cli-word-count** — exec error: NameError: name 'sys' is not defined. Did you forget to import 'sys'?
- **data-log-parser** — exec missing_dependency: ModuleNotFoundError: No module named 'log_analyzer'
- **data-dedupe** — exec error: IndexError: list index out of range
- **api-url-shortener** — exec missing_dependency: ModuleNotFoundError: No module named 'sqlalchemy'
- **algo-matrix** — exec error: ValueError: Matrices must be of the same dimensions
- **vague-make-a-game** — no files extracted · wrong artifact kind (wanted any) · exec no_files:
- **vague-help-with-homework** — exec js_error: Identifier 'showQuizAnswer' has already been declared
