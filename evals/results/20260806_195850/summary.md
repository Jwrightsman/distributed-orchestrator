# Eval run — 20260806_195850

**Model:** `qwen3.5:4b` · **Prompt set:** `v1` · **Mode:** real

**Success rate: 40%** (8/20) — target is 80%

Mean judge score: 4.25 · Mean wall clock: 3678.2s · Mean subtasks: 3.85

## By category

| Category | Pass | Total | Rate |
| --- | ---: | ---: | ---: |
| api | 1 | 4 | 25% |
| cli_tool | 1 | 5 | 20% |
| data_processing | 2 | 5 | 40% |
| web_app | 4 | 6 | 67% |

## Where runs failed

| Failure stage | Count |
| --- | ---: |
| no files extracted | 1 |
| parse failed | 0 |
| execution failed | 9 |
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
| `web-todo` | web_app | 1 | ✓ | ✓ | 5 | 2136 | ✅ |
| `web-calculator` | web_app | 1 | ✓ | ✓ | 5 | 1651 | ❌ |
| `web-memory-game` | web_app | 1 | ✓ | ✓ | 5 | 1065 | ✅ |
| `web-markdown-preview` | web_app | 1 | ✓ | ✓ | 4 | 3615 | ✅ |
| `cli-todo` | cli_tool | 1 | ✓ | ✓ | 4 | 2172 | ✅ |
| `cli-file-organizer` | cli_tool | 1 | ✓ | ✗ | 4 | 801 | ❌ |
| `cli-password-gen` | cli_tool | 1 | ✓ | ✗ | 4 | 1357 | ❌ |
| `cli-word-count` | cli_tool | 0 | ✓ | ✗ | 1 | 4057 | ❌ |
| `cli-unit-convert` | cli_tool | 1 | ✓ | ✗ | 4 | 2250 | ❌ |
| `data-sales-csv` | data_processing | 1 | ✓ | ✓ | 5 | 666 | ✅ |
| `data-log-parser` | data_processing | 1 | ✓ | ✓ | 4 | 944 | ❌ |
| `data-json-to-csv` | data_processing | 1 | ✓ | ✗ | 5 | 1356 | ❌ |
| `data-dedupe` | data_processing | 1 | ✓ | ✗ | 5 | 1505 | ❌ |
| `data-temperature-stats` | data_processing | 1 | ✓ | ✓ | 4 | 2687 | ✅ |
| `api-todo-fastapi` | api | 1 | ✓ | ✓ | 5 | 1902 | ✅ |
| `api-url-shortener` | api | 1 | ✓ | ✗ | 5 | 38134 | ❌ |
| `api-http-server` | api | 1 | ✓ | ✗ | 4 | 865 | ❌ |
| `api-weather-mock` | api | 1 | ✓ | ✗ | 5 | 2009 | ❌ |

## Failure detail

- **web-pomodoro** — judge 3/5
- **web-calculator** — missing keywords: addeventlistener
- **cli-file-organizer** — exec error: main.py: error: the following arguments are required: input_directory
- **cli-password-gen** — exec error: AttributeError: 'builtin_function_or_method' object has no attribute 'flush'
- **cli-word-count** — no files extracted · wrong artifact kind (wanted python) · missing keywords: argparse · exec no_files: · judge 1/5
- **cli-unit-convert** — exec error: main.py: error: the following arguments are required: value, source_unit, target_unit
- **data-log-parser** — missing keywords: open
- **data-json-to-csv** — exec error: exit 1
- **data-dedupe** — exec error: main.py: error: the following arguments are required: csv_file, output_file, column_name
- **api-url-shortener** — exec error: NameError: name 'Query' is not defined
- **api-http-server** — exec error: NameError: name 'dataclass' is not defined
- **api-weather-mock** — exec error: OSError: [WinError 10106] The requested service provider could not be loaded or initialized
