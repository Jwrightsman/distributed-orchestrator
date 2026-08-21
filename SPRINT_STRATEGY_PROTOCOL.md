# Execution Strategy Protocol v1 Sprint

_Bounded backend/protocol sprint authorized and implemented August 21, 2026._

## Scope

This sprint temporarily superseded earlier feature-freeze language only to make
execution strategy first-class before trusted alpha. It did not authorize UI,
marketing, map, research, consensus, debate, marketplace, token, blockchain, or
model-sharding work. The normal freeze resumes after this handoff.

## Baseline

- Branch base: `origin/master` at `b2ce65d7cc6f0299431afa8d3f758a45320d461d`
- `python -m pytest -q`: `475 passed, 1 skipped in 66.33s`
- `ruff check .`: executable was not on `PATH`
- `python -m ruff check .`: passed
- `python -c "import server"`: passed
- Docker client and Compose were installed; the Docker daemon was unavailable
  through the Windows named pipe
- `docker compose config --quiet`: passed

## Delivered

- [x] Strict `ExecutionRequestV1` and normalized `ExecutionResultV1`
- [x] Typed, bounded DAG and ensemble options
- [x] Strategy registry and deterministic `conservative-v1` selector
- [x] Direct normalization to one-candidate ensemble
- [x] Placement-independent local/distributed/auto dispatcher
- [x] Confidentiality, capability, approved-node, queue, timeout, and fallback rules
- [x] Existing DAG pipeline adapted without rewriting planning/review/revision/memory
- [x] Production ensemble with bounded concurrency and isolated candidate failure
- [x] Evidence-based winner selection and explicit unverified fallback
- [x] Validator registry and structured evidence
- [x] Durable idempotent SQLite execution migration
- [x] Canonical `POST/GET /v1/executions` API
- [x] Thin `/pitch`, `/pitch/async`, and `/pitch/distributed` adapters
- [x] CLI strategy, candidate, and placement flags
- [x] MCP optional strategy, placement, contract, verification, confidentiality, and requirements
- [x] Experiment wrapper routed through bounded production ensemble executions
- [x] Protocol-v1 result and stream binding to active attempts and leases
- [x] Constant-time shared-secret comparisons
- [x] Docker source packaging and build fingerprint coverage for `execution/`
- [x] Protocol, architecture, and ADR documentation
- [x] Model-free strategy, interface, persistence, and worker-invariant tests

## Compatibility

Legacy request bodies with only `task` and optional `project_id` still select
DAG. `/pitch` remains local by default, `/pitch/distributed` requests remote
builders with visible local fallback, and legacy async jobs retain job ids and
response fields. Normalized strategy metadata is additive.

## Explicit non-deliverables

- Durable worker queue or lease recovery
- A sandbox or general network-policy enforcement system
- Per-node public-key identity, revocation, or signed receipts
- Permissionless settlement
- Deterministic verification for arbitrary generated artifacts
- Research, map, debate, consensus, marketplace, token, blockchain, or sharding strategies
- Any frontend, template, dashboard JavaScript, CSS, visual, or marketing work

## Handoff verification

- `python -m pytest -q`: `527 passed, 1 skipped in 75.99s`
- `python -m ruff check .`: `All checks passed!`
- bare `ruff check .`: unavailable because the executable is not on this shell's `PATH`
- `python -c "import server"`: passed
- `docker compose config --quiet`: passed
- Docker client `28.1.1` and Compose `v2.36.0` are installed
- Docker daemon/build validation: unavailable because the Docker Desktop Linux
  engine named pipe was not present; `docker build --check .` could not connect
- Scope inspection: no template, CSS, dashboard JavaScript, visual component,
  or other frontend file changed

Do not copy a historical test count from this document into ongoing claims;
rerun the suite after future changes.
