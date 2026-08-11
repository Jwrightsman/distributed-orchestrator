# Eval run — 20260811_052310

**Model:** `qwen3.5:4b` · **Prompt set:** `v3` · **Mode:** real

**Success rate: 54%** (15/28) — target is 80%

Mean judge score: 4.46 · Mean wall clock: 1329.8s · Mean subtasks: 3.75

## By category

| Category | Pass | Total | Rate |
| --- | ---: | ---: | ---: |
| algorithm | 1 | 4 | 25% |
| api | 0 | 4 | 0% |
| cli_tool | 4 | 5 | 80% |
| data_processing | 3 | 5 | 60% |
| vague | 4 | 4 | 100% |
| web_app | 3 | 6 | 50% |

## Where runs failed

| Failure stage | Count |
| --- | ---: |
| no files extracted | 0 |
| parse failed | 1 |
| execution failed | 10 |
| wrong artifact kind | 0 |
| missing keywords | 2 |
| judged below bar | 1 |
| judge unparseable | 0 |
| pipeline error | 0 |

## Per prompt

| Prompt | Category | Files | Parses | Executes | Judge | Secs | Pass |
| --- | --- | ---: | :---: | :---: | :---: | ---: | :---: |
| `web-snake` | web_app | 1 | ✓ | ✓ | 4 | 2454 | ✅ |
| `web-pomodoro` | web_app | 1 | ✓ | ✓ | 5 | 1384 | ✅ |
| `web-todo` | web_app | 1 | ✓ | ✓ | 3 | 1894 | ❌ |
| `web-calculator` | web_app | 1 | ✓ | ✓ | 5 | 1385 | ❌ |
| `web-memory-game` | web_app | 1 | ✓ | ✓ | 4 | 2044 | ✅ |
| `web-markdown-preview` | web_app | 1 | ✗ | ✓ | 4 | 2613 | ❌ |
| `cli-todo` | cli_tool | 2 | ✓ | ✓ | 4 | 1491 | ✅ |
| `cli-file-organizer` | cli_tool | 1 | ✓ | ✓ | 5 | 987 | ✅ |
| `cli-password-gen` | cli_tool | 1 | ✓ | ✓ | 5 | 792 | ✅ |
| `cli-word-count` | cli_tool | 1 | ✓ | ✓ | 5 | 375 | ✅ |
| `cli-unit-convert` | cli_tool | 1 | ✓ | ✗ | 1 | 2202 | ❌ |
| `data-sales-csv` | data_processing | 1 | ✓ | ✗ | 4 | 885 | ❌ |
| `data-log-parser` | data_processing | 1 | ✓ | ✓ | 4 | 1044 | ✅ |
| `data-json-to-csv` | data_processing | 1 | ✓ | ✓ | 5 | 536 | ✅ |
| `data-dedupe` | data_processing | 1 | ✓ | ✓ | 5 | 1159 | ✅ |
| `data-temperature-stats` | data_processing | 1 | ✓ | ✗ | 5 | 614 | ❌ |
| `api-todo-fastapi` | api | 1 | ✓ | ✗ | 5 | 882 | ❌ |
| `api-url-shortener` | api | 1 | ✓ | ✗ | 3 | 946 | ❌ |
| `api-http-server` | api | 1 | ✓ | ✗ | 5 | 560 | ❌ |
| `api-weather-mock` | api | 1 | ✓ | ✗ | 5 | 789 | ❌ |
| `algo-fizzbuzz-tests` | algorithm | 1 | ✓ | ✗ | 5 | 1509 | ❌ |
| `algo-binary-search` | algorithm | 1 | ✓ | ✗ | 5 | 1147 | ❌ |
| `algo-matrix` | algorithm | 1 | ✓ | ✓ | 5 | 1454 | ✅ |
| `algo-roman` | algorithm | 1 | ✓ | ✗ | 5 | 1261 | ❌ |
| `vague-something-useful` | vague | 1 | ✓ | ✓ | 4 | 1692 | ✅ |
| `vague-make-a-game` | vague | 1 | ✓ | ✓ | 5 | 479 | ✅ |
| `vague-help-with-homework` | vague | 1 | ✓ | ✓ | 5 | 3567 | ✅ |
| `vague-organize-my-life` | vague | 1 | ✓ | ✓ | 5 | 1087 | ✅ |

## Failure detail

- **web-todo** — judge 3/5
- **web-calculator** — missing keywords: addeventlistener
- **web-markdown-preview** — index.html: missing closing </html> tag — the document is truncated; index.html: unbalanced <script> tags — the file is likely cut off · missing keywords: addeventlistener, innerhtml
- **cli-unit-convert** — exec error: Error: Unknown command. · judge 1/5
- **data-sales-csv** — exec error: FileNotFoundError: [Errno 2] No such file or directory: 'sales_data.csv'
- **data-temperature-stats** — exec error: FileNotFoundError: [Errno 2] No such file or directory: 'temperatures.csv'
- **api-todo-fastapi** — exec error: AttributeError: 'FastAPI' object has no attribute 'dependency_versions'. Did you mean: 'dependency_overrides'?
- **api-url-shortener** — exec missing_dependency: ImportError: cannot import name 'RedirectResponse' from 'fastapi' (C:\Users\wrigh\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\fastapi\__init__.py) · judge 3/5
- **api-http-server** — exec error: AttributeError: module 'socketserver' has no attribute 'ThreadingHTTPServer'. Did you mean: 'ThreadingTCPServer'?
- **api-weather-mock** — exec error: AttributeError: 'FastAPI' object has no attribute 'test_client'
- **algo-fizzbuzz-tests** — exec missing_dependency: ModuleNotFoundError: No module named 'main_solution'
- **algo-binary-search** — exec error: exit 1
- **algo-roman** — exec error: FAILED (errors=5)
