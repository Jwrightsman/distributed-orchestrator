# Eval run — 20260810_041455

**Model:** `qwen3.5:4b` · **Prompt set:** `v5` · **Mode:** real

**Success rate: 57%** (16/28) — target is 80%

Mean judge score: 4.61 · Mean wall clock: 1112.4s · Mean subtasks: 2.46

## By category

| Category | Pass | Total | Rate |
| --- | ---: | ---: | ---: |
| algorithm | 3 | 4 | 75% |
| api | 2 | 4 | 50% |
| cli_tool | 3 | 5 | 60% |
| data_processing | 1 | 5 | 20% |
| vague | 2 | 4 | 50% |
| web_app | 5 | 6 | 83% |

## Where runs failed

| Failure stage | Count |
| --- | ---: |
| no files extracted | 0 |
| parse failed | 0 |
| execution failed | 10 |
| wrong artifact kind | 1 |
| missing keywords | 2 |
| judged below bar | 1 |
| judge unparseable | 0 |
| pipeline error | 0 |

## Per prompt

| Prompt | Category | Files | Parses | Executes | Judge | Secs | Pass |
| --- | --- | ---: | :---: | :---: | :---: | ---: | :---: |
| `web-snake` | web_app | 1 | ✓ | ✓ | 4 | 1216 | ✅ |
| `web-pomodoro` | web_app | 1 | ✓ | ✓ | 5 | 892 | ✅ |
| `web-todo` | web_app | 1 | ✓ | ✓ | 5 | 624 | ✅ |
| `web-calculator` | web_app | 1 | ✓ | ✓ | 5 | 476 | ❌ |
| `web-memory-game` | web_app | 1 | ✓ | ✓ | 4 | 920 | ✅ |
| `web-markdown-preview` | web_app | 1 | ✓ | ✓ | 4 | 662 | ✅ |
| `cli-todo` | cli_tool | 1 | ✓ | ✗ | 5 | 1574 | ❌ |
| `cli-file-organizer` | cli_tool | 1 | ✓ | ✓ | 5 | 775 | ✅ |
| `cli-password-gen` | cli_tool | 1 | ✓ | ✓ | 5 | 458 | ✅ |
| `cli-word-count` | cli_tool | 2 | ✓ | ✗ | 5 | 694 | ❌ |
| `cli-unit-convert` | cli_tool | 1 | ✓ | ✓ | 4 | 1628 | ✅ |
| `data-sales-csv` | data_processing | 1 | ✓ | ✗ | 5 | 276 | ❌ |
| `data-log-parser` | data_processing | 1 | ✓ | ✗ | 5 | 1680 | ❌ |
| `data-json-to-csv` | data_processing | 1 | ✓ | ✓ | 5 | 261 | ✅ |
| `data-dedupe` | data_processing | 1 | ✓ | ✗ | 5 | 1448 | ❌ |
| `data-temperature-stats` | data_processing | 1 | ✓ | ✗ | 3 | 1080 | ❌ |
| `api-todo-fastapi` | api | 1 | ✓ | ✗ | 5 | 665 | ❌ |
| `api-url-shortener` | api | 1 | ✓ | ✓ | 5 | 999 | ✅ |
| `api-http-server` | api | 2 | ✓ | ✓ | 5 | 344 | ✅ |
| `api-weather-mock` | api | 1 | ✓ | ✗ | 5 | 862 | ❌ |
| `algo-fizzbuzz-tests` | algorithm | 1 | ✓ | ✗ | 4 | 223 | ❌ |
| `algo-binary-search` | algorithm | 1 | ✓ | ✓ | 5 | 989 | ✅ |
| `algo-matrix` | algorithm | 1 | ✓ | ✓ | 5 | 897 | ✅ |
| `algo-roman` | algorithm | 1 | ✓ | ✓ | 5 | 654 | ✅ |
| `vague-something-useful` | vague | 1 | ✓ | ✗ | 4 | 2404 | ❌ |
| `vague-make-a-game` | vague | 1 | ✓ | ✓ | 5 | 1165 | ✅ |
| `vague-help-with-homework` | vague | 2 | ✓ | ✓ | 3 | 3919 | ❌ |
| `vague-organize-my-life` | vague | 1 | ✓ | ✓ | 4 | 3362 | ✅ |

## Failure detail

- **web-calculator** — missing keywords: addeventlistener
- **cli-todo** — exec error: NameError: name 'parser' is not defined
- **cli-word-count** — wrong artifact kind (wanted python) · missing keywords: argparse · exec nothing_executable:
- **data-sales-csv** — exec error: FileNotFoundError: [Errno 2] No such file or directory: 'sales_data.csv'
- **data-log-parser** — exec error: exit 1
- **data-dedupe** — exec error: FAILED (errors=1)
- **data-temperature-stats** — exec error: NameError: name 'hottest_record' is not defined · judge 3/5
- **api-todo-fastapi** — exec error: TypeError: FastAPI.delete() got an unexpected keyword argument 'status_codes'. Did you mean 'status_code'?
- **api-weather-mock** — exec error: AttributeError: 'FastAPI' object has no attribute 'run'
- **algo-fizzbuzz-tests** — exec missing_dependency: ModuleNotFoundError: No module named 'fizzbuzz_logic'
- **vague-something-useful** — exec error: NameError: name 'ArgumentParser' is not defined
- **vague-help-with-homework** — judge 3/5
