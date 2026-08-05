## What this changes

<!-- One or two sentences. What is different after this merges? -->

## Why

<!-- The problem it solves. Link an issue if there is one. -->

## Checks

- [ ] `pytest -q` passes
- [ ] `ruff check .` passes
- [ ] I ran this on Python 3.12+ (CI uses 3.14)

## If this touches a prompt

Prompt changes are judged by measurement — see [`evals/README.md`](../evals/README.md).
Add a new prompt set rather than editing one in place, and paste both numbers:

| | Success rate | Prompts run |
| --- | --- | --- |
| Before | | |
| After  | | |

<!-- A change that doesn't move the score won't be merged, however nice it reads. -->

## If this touches the server or worker protocol

- [ ] `python scripts/restart_recovery.py` still passes
- [ ] `python scripts/soak_test.py` still passes

## Anything reviewers should know

<!-- Trade-offs, things you're unsure about, things you deliberately left out. -->
