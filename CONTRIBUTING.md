# Contributing

Thanks for looking. This is a small project run by one person, so the most
useful contributions are usually the least glamorous ones.

## The most valuable thing you can do

**Join a node and tell us where you got stuck.** The whole point is running
across other people's hardware, and every rough edge you hit is one that would
otherwise hit the next hundred people. A bug report titled "your install
instructions don't work on Fedora" is worth more than a refactor.

```bash
python join.py http://ORCHESTRATOR_ADDRESS:8000
```

If it fails, open an issue with the **Node won't connect** template — it asks
for the four things needed to diagnose it.

## Setup

```bash
git clone https://github.com/Jwrightsman/distributed-orchestrator.git
cd distributed-orchestrator
pip install -r requirements.txt
ollama pull qwen3.5:4b       # or anything on the auto-detect ladder
python status.py             # confirms Ollama and your model
```

Python 3.12+ (CI runs 3.14). No virtualenv is enforced; use one if you like.

## Before you open a pull request

```bash
pytest -q        # ~250 tests, no Ollama needed — this is the fast signal
ruff check .     # CI fails on this
```

Both must pass. CI runs them on **Python 3.14**, and that has bitten us: code
that passes on 3.11 can fail on 3.14, `asyncio` especially. If you can, check
against the version CI uses.

If the server or worker protocol is involved, these two catch things the unit
tests cannot, and neither needs a model:

```bash
python scripts/restart_recovery.py   # SIGKILLs a real server, checks recovery
python scripts/soak_test.py          # 20 back-to-back pitches, watches for leaks
```

## Changing a prompt

**Prompt changes are judged by measurement, not by reading.** The planner,
builder, reviewer and reviser prompts decide the entire quality of the output,
and "this reads better" has repeatedly turned out to mean "this scores worse".

Add a new prompt set rather than editing the current one in place, then compare:

```bash
python evals/run_evals.py --only web_app                      # current
python evals/run_evals.py --only web_app --prompt-set yours   # yours
```

Include both numbers in the pull request. See [`evals/README.md`](evals/README.md)
for what is scored and how long a run takes. A change that does not move the
score does not get merged, however nice it looks.

## What this project will not take

- **Tokens, coins, blockchain, or anything on-chain.** This is a deliberate,
  permanent design constraint, not an unexplored idea. The contribution ledger
  is an append-only JSON file and will stay one.
- Framework rewrites of things that already work.
- New dependencies, unless the alternative is genuinely worse. The runtime is
  five packages and the goal is to keep it small enough to install on a
  stranger's laptop without argument.
- Features that add user-facing surface area before the current one is
  reliable. Reliability is the bottleneck, not capability.

## Style

Match the file you are editing. Comments explain *why*, not *what* — the
existing code tends to record the reasoning behind non-obvious choices, and
that is deliberate. Keep it.

## Reporting a security issue

Do not open a public issue for anything exploitable. Email
wrightsmanjett@gmail.com instead.

Note the honest trust model: worker nodes authenticate with a shared secret, so
a node that authenticates can return whatever it likes. That is a known
limitation of this phase rather than a vulnerability — see the README's
Limitations section.
