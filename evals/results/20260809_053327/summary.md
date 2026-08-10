# Eval run — 20260809_053327

**Model:** `qwen3.5:4b` · **Prompt set:** `v4` · **Mode:** real

**Success rate: 39%** (11/28) — target is 80%

Mean judge score: 4.39 · Mean wall clock: 1817.8s · Mean subtasks: 3.89

## By category

| Category | Pass | Total | Rate |
| --- | ---: | ---: | ---: |
| algorithm | 0 | 4 | 0% |
| api | 3 | 4 | 75% |
| cli_tool | 1 | 5 | 20% |
| data_processing | 2 | 5 | 40% |
| vague | 2 | 4 | 50% |
| web_app | 3 | 6 | 50% |

## Where runs failed

| Failure stage | Count |
| --- | ---: |
| no files extracted | 2 |
| parse failed | 1 |
| execution failed | 13 |
| wrong artifact kind | 0 |
| missing keywords | 2 |
| judged below bar | 2 |
| judge unparseable | 0 |
| pipeline error | 0 |

## Per prompt

| Prompt | Category | Files | Parses | Executes | Judge | Secs | Pass |
| --- | --- | ---: | :---: | :---: | :---: | ---: | :---: |
| `web-snake` | web_app | 1 | ✓ | ✓ | 5 | 3034 | ✅ |
| `web-pomodoro` | web_app | 1 | ✓ | ✓ | 4 | 2555 | ✅ |
| `web-todo` | web_app | 1 | ✓ | ✓ | 5 | 2063 | ✅ |
| `web-calculator` | web_app | 1 | ✓ | ✓ | 5 | 1578 | ❌ |
| `web-memory-game` | web_app | 1 | ✓ | ✗ | 5 | 1797 | ❌ |
| `web-markdown-preview` | web_app | 1 | ✓ | ✓ | 3 | 3675 | ❌ |
| `cli-todo` | cli_tool | 1 | ✓ | ✓ | 5 | 1421 | ✅ |
| `cli-file-organizer` | cli_tool | 1 | ✓ | ✗ | 3 | 1716 | ❌ |
| `cli-password-gen` | cli_tool | 1 | ✓ | ✓ | 3 | 825 | ❌ |
| `cli-word-count` | cli_tool | 3 | ✗ | ✓ | 5 | 3429 | ❌ |
| `cli-unit-convert` | cli_tool | 1 | ✓ | ✗ | 5 | 1672 | ❌ |
| `data-sales-csv` | data_processing | 1 | ✓ | ✗ | 5 | 777 | ❌ |
| `data-log-parser` | data_processing | 1 | ✓ | ✓ | 4 | 755 | ✅ |
| `data-json-to-csv` | data_processing | 1 | ✓ | ✗ | 5 | 655 | ❌ |
| `data-dedupe` | data_processing | 0 | ✓ | ✗ | 4 | 2831 | ❌ |
| `data-temperature-stats` | data_processing | 1 | ✓ | ✓ | 5 | 1234 | ✅ |
| `api-todo-fastapi` | api | 1 | ✓ | ✓ | 5 | 1186 | ✅ |
| `api-url-shortener` | api | 1 | ✓ | ✓ | 5 | 1096 | ✅ |
| `api-http-server` | api | 1 | ✓ | ✓ | 5 | 784 | ✅ |
| `api-weather-mock` | api | 3 | ✓ | ✗ | 3 | 963 | ❌ |
| `algo-fizzbuzz-tests` | algorithm | 3 | ✓ | ✗ | 5 | 712 | ❌ |
| `algo-binary-search` | algorithm | 1 | ✓ | ✗ | 5 | 1088 | ❌ |
| `algo-matrix` | algorithm | 8 | ✓ | ✗ | 4 | 2185 | ❌ |
| `algo-roman` | algorithm | 0 | ✓ | ✗ | 4 | 830 | ❌ |
| `vague-something-useful` | vague | 1 | ✓ | ✓ | 5 | 2490 | ✅ |
| `vague-make-a-game` | vague | 1 | ✓ | ✓ | 5 | 3160 | ✅ |
| `vague-help-with-homework` | vague | 1 | ✓ | ✗ | 5 | 2640 | ❌ |
| `vague-organize-my-life` | vague | 1 | ✓ | ✗ | 1 | 3747 | ❌ |

## Failure detail

- **web-calculator** — missing keywords: addeventlistener
- **web-memory-game** — exec js_error: Cannot read properties of null (reading 'addEventListener')
- **web-markdown-preview** — judge 3/5
- **cli-file-organizer** — exec missing_dependency: FAILED (errors=3) · judge 3/5
- **cli-password-gen** — judge 3/5
- **cli-word-count** — main.py is not valid Python: invalid syntax (line 10) — the offending line is: def format_output(...): ...
- **cli-unit-convert** — missing keywords: argparse · exec error: exit 1
- **data-sales-csv** — exec error: exit 1
- **data-json-to-csv** — exec error: KeyError: 0
- **data-dedupe** — no files extracted · wrong artifact kind (wanted python) · missing keywords: csv · exec no_files:
- **api-weather-mock** — exec missing_dependency: ModuleNotFoundError: No module named 'mock_weather' · judge 3/5
- **algo-fizzbuzz-tests** — exec missing_dependency: ModuleNotFoundError: No module named 'fizzbuzz'
- **algo-binary-search** — exec error: exit 1
- **algo-matrix** — exec missing_dependency: ModuleNotFoundError: No module named 'matrix_ops'
- **algo-roman** — no files extracted · wrong artifact kind (wanted python) · missing keywords: def · exec no_files:
- **vague-help-with-homework** — exec js_error: Identifier 'SRSystem' has already been declared
- **vague-organize-my-life** — exec js_error: missing ) after argument list · judge 1/5
