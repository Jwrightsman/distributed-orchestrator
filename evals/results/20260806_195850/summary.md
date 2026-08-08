# Eval run — 20260806_195850

**Model:** `qwen3.5:4b` · **Prompt set:** `v1` · **Mode:** real

**Success rate: 36%** (10/28) — target is 80%

Mean judge score: 4.29 · Mean wall clock: 3144.1s · Mean subtasks: 3.86

## By category

| Category | Pass | Total | Rate |
| --- | ---: | ---: | ---: |
| algorithm | 0 | 4 | 0% |
| api | 2 | 4 | 50% |
| cli_tool | 3 | 5 | 60% |
| data_processing | 3 | 5 | 60% |
| vague | 0 | 4 | 0% |
| web_app | 2 | 6 | 33% |

## Where runs failed

| Failure stage | Count |
| --- | ---: |
| no files extracted | 1 |
| parse failed | 0 |
| execution failed | 14 |
| wrong artifact kind | 0 |
| missing keywords | 2 |
| judged below bar | 1 |
| judge unparseable | 0 |
| pipeline error | 0 |

## Per prompt

| Prompt | Category | Files | Parses | Executes | Judge | Secs | Pass |
| --- | --- | ---: | :---: | :---: | :---: | ---: | :---: |
| `web-snake` | web_app | 1 | ✓ | ✓ | 4 | 2581 | ✅ |
| `web-pomodoro` | web_app | 1 | ✓ | ✓ | 3 | 1813 | ❌ |
| `web-todo` | web_app | 1 | ✓ | ✗ | 5 | 2136 | ❌ |
| `web-calculator` | web_app | 1 | ✓ | ✓ | 5 | 1651 | ❌ |
| `web-memory-game` | web_app | 1 | ✓ | ✓ | 5 | 1065 | ✅ |
| `web-markdown-preview` | web_app | 1 | ✓ | ✗ | 4 | 3615 | ❌ |
| `cli-todo` | cli_tool | 1 | ✓ | ✓ | 4 | 2172 | ✅ |
| `cli-file-organizer` | cli_tool | 1 | ✓ | ✓ | 4 | 801 | ✅ |
| `cli-password-gen` | cli_tool | 1 | ✓ | ✗ | 4 | 1357 | ❌ |
| `cli-word-count` | cli_tool | 0 | ✗ | ✗ | 1 | 4057 | ❌ |
| `cli-unit-convert` | cli_tool | 1 | ✓ | ✓ | 4 | 2250 | ✅ |
| `data-sales-csv` | data_processing | 1 | ✓ | ✓ | 5 | 666 | ✅ |
| `data-log-parser` | data_processing | 1 | ✓ | ✓ | 4 | 944 | ❌ |
| `data-json-to-csv` | data_processing | 1 | ✓ | ✗ | 5 | 1356 | ❌ |
| `data-dedupe` | data_processing | 1 | ✓ | ✓ | 5 | 1505 | ✅ |
| `data-temperature-stats` | data_processing | 1 | ✓ | ✓ | 4 | 2687 | ✅ |
| `api-todo-fastapi` | api | 1 | ✓ | ✓ | 5 | 1902 | ✅ |
| `api-url-shortener` | api | 1 | ✓ | ✗ | 5 | 38134 | ❌ |
| `api-http-server` | api | 1 | ✓ | ✗ | 4 | 865 | ❌ |
| `api-weather-mock` | api | 1 | ✓ | ✓ | 5 | 2009 | ✅ |
| `algo-fizzbuzz-tests` | algorithm | 1 | ✓ | ✗ | 5 | 1215 | ❌ |
| `algo-binary-search` | algorithm | 1 | ✓ | ✗ | 5 | 1004 | ❌ |
| `algo-matrix` | algorithm | 1 | ✓ | ✗ | 4 | 1676 | ❌ |
| `algo-roman` | algorithm | 1 | ✓ | ✗ | 5 | 309 | ❌ |
| `vague-something-useful` | vague | 1 | ✓ | ✗ | 5 | 1789 | ❌ |
| `vague-make-a-game` | vague | 1 | ✓ | ✗ | 5 | 2804 | ❌ |
| `vague-help-with-homework` | vague | 14 | ✓ | ✗ | 5 | 3475 | ❌ |
| `vague-organize-my-life` | vague | 2 | ✓ | ✗ | 1 | 2198 | ❌ |

## Failure detail

- **web-pomodoro** — judge 3/5
- **web-todo** — exec js_error: Cannot set properties of null (setting 'innerHTML')
- **web-calculator** — missing keywords: addeventlistener
- **web-markdown-preview** — exec js_error: pre.querySelectorAll is not a function
- **cli-password-gen** — exec error: AttributeError: 'builtin_function_or_method' object has no attribute 'flush'
- **cli-word-count** — no files extracted · wrong artifact kind (wanted python) · missing keywords: argparse · exec no_files: · judge 1/5
- **data-log-parser** — missing keywords: open
- **data-json-to-csv** — exec error: exit 1
- **api-url-shortener** — exec error: NameError: name 'Query' is not defined
- **api-http-server** — exec error: NameError: name 'dataclass' is not defined
- **algo-fizzbuzz-tests** — exec error: FAILED (failures=1)
- **algo-binary-search** — exec error: AssertionError
- **algo-matrix** — exec error: IndexError: list index out of range
- **algo-roman** — exec error: AssertionError: Conversion mismatch for 4
- **vague-something-useful** — exec missing_dependency: ModuleNotFoundError: No module named 'sqlmodel'
- **vague-make-a-game** — exec missing_dependency: ModuleNotFoundError: No module named 'pygame'
- **vague-help-with-homework** — exec nothing_executable:
- **vague-organize-my-life** — exec nothing_executable: · judge 1/5
