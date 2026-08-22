# Threat model

_Implemented state on August 21, 2026. This describes current controls and
current gaps, not the intended end state._

**One-sentence version:** Mycelium is suitable for a small private trusted
alpha whose operators and node holders are known. It is not safe as an open,
permissionless compute network.

Trusted-alpha hardening changed two important defaults: a worker result must
settle a server-issued durable attempt before execution can consume it, and
sensitive read routes can be protected by a viewer credential. It did not add
per-node cryptographic identity, TLS, sandboxing, or multi-user accounts.

## 1. Assets

| Asset | Where it lives | Realistic failure |
| --- | --- | --- |
| Task and project text | execution/job SQLite, project files, coordinator memory, assigned worker prompts | disclosure to an unauthorized reader or worker operator |
| Generated output and artifacts | SQLite previews/receipts, `output/`, `execution_artifacts/` | disclosure, tampering, retention beyond intent, or unsafe code execution by an operator |
| Attempt authority | SQLite attempt rows and accepted receipts | an unbound, late, or duplicate result entering execution or earning points |
| Viewer, pitch, and node secrets | local config/environment and HTTP headers | private reads, unwanted compute use, or worker admission |
| Contribution history | SQLite plus `ledger.json` compatibility projection | misattribution or misleading claims about correctness/value |
| Contributor hardware | worker machines | sustained model inference, disk use for the model, prompt disclosure |
| Orchestrator availability | one process and its SQLite/disk | total service interruption or lost process-local queue work |

There are no full user accounts, payment data, wallets, or token balances. A
deployment may still hold confidential task content, project memory, node
hardware descriptions, hostnames, and generated artifacts, so "no accounts"
does not mean "no sensitive data."

## 2. Trust boundaries

```text
Requester ── pitch_key ──▶ ORCHESTRATOR ── node_secret ──▶ Worker
                              │                              │
                              │ viewer_key                  ├─ sees assigned prompt
                              ▼                              └─ returns model text
                         Private readers
                              │
                              └─ explicit share token ──▶ capability holder
```

- The orchestrator process and host filesystem are inside the primary trust
  boundary.
- Requesters, viewers, workers, and share holders are outside it.
- The three configured static secrets represent different authority. Possessing
  one must not imply possessing the other two.
- Every worker holding `node_secret` shares the same admission credential.
  `node_id` is a claimed label, not a cryptographic identity.
- A share token is a bearer capability. Anyone who receives it can use its
  exact redacted scope until expiry or revocation.

## 3. Controls implemented today

| Control | What it enforces | What it does not enforce |
| --- | --- | --- |
| `viewer_key` | private HTTP and WebSocket access via header, Bearer token, or signed expiring HttpOnly cookie | users, roles, per-execution ACLs, TLS |
| `pitch_key` | canonical and compatibility task submission when configured | private reads or public-share revocation |
| `node_secret` | registration, polling, result, and token-stream admission when configured | individual node identity or revocation |
| Server-owned attempts | active lease and exact task/execution/unit/kind/node/version/nonce binding | truthfulness or quality of the returned model output |
| Atomic settlement | exactly-once attempt transition, receipt, response, and compute contribution; exact replay after restart | durable scheduling or worker resumption |
| Result quarantine | bounded diagnostics for rejected output outside operational execution | malware analysis or a complete forensic archive |
| Total execution deadline | shared remaining budget for strategy, local calls, worker waits, validation, and finalization | forcibly stopping an external process that ignores cancellation |
| Restart reconciliation | truthful `interrupted` state for non-resumable executions/jobs and active attempts | resuming lost process-local work |
| Artifact registry | root confinement, normalized paths, symlink rejection, hashes, quotas, streaming delivery, retention | content safety, immutability, sandboxing |
| Explicit shares | unguessable hashed bearer tokens, expiry, revocation, redaction, artifact permission | preventing redistribution by a token holder |
| Public-pitch profile | one local candidate with short timeout/output and compute-aware admission | strong abuse prevention or semantic content moderation |
| SQLite contribution ledger | concurrent-safe, idempotent non-monetary records | tamper evidence against the host operator |

Secret comparisons use constant-time comparison where static credentials or
signatures are checked. This reduces one narrow side channel. It does not make a
shared secret equivalent to cryptographic identity.

## 4. Public and private reachability

When `viewer_key` is configured, the deliberate unauthenticated surface is:

- `GET /` and `GET /try`;
- static assets under `GET /static/*`;
- `GET /health` and `GET /status.json`;
- `GET /v1/shares/{token}` and token-scoped share artifact routes;
- `POST /public/pitch` only when the operator enables it;
- viewer session exchange/logout endpoints.

Pitch and worker protocol routes are exempt from the viewer gate because they
use their own credentials. If `pitch_key` or `node_secret` is empty, that
separate control is open.

Everything else is viewer-protected, including canonical execution reads and
cancellation, jobs, events, WebSockets, node details, history, gallery, run
pages, projects, ledger, standings, metrics, artifact APIs, share creation and
revocation, and the dashboard.

`/status.json` is deliberately narrow: service/inference state, model name,
counts, uptime, repository, and build fingerprint. It does not include task
text, result text, hostnames, hardware detail, attempt identifiers, nonces, or
project ids. `/health` includes liveness, model names, queue/node counts, and
whether private routes are protected. It warns when they are not.

### Fail-open local-development mode

All three secrets default to empty for local compatibility. Most importantly,
when `viewer_key` is empty the viewer middleware allows private routes. Startup
logs and `/health` explicitly say so. Anyone who can reach that deployment can
then read tasks, results, projects, events, node detail, and artifacts.

An operator must not bind such a configuration to an untrusted network. Set all
three secrets as appropriate, put TLS in front, and prefer a private network
such as Tailscale. Viewer access control without transport encryption still
sends bearer credentials and private content in clear text.

## 5. Worker-result integrity

### The invariant

A worker result is operational only after it settles the current durable
server-issued attempt. The server record, not the submitted payload, determines
whether contract-v1 binding is mandatory.

For v1, omission of contract version, attempt id, nonce, execution id, unit id,
or unit kind is a rejection. The assigned node, URL task, lease, state, and all
bindings must match. A worker cannot downgrade validation by omitting fields.

Settlement uses a SQLite write transaction and uniqueness constraints. The
active attempt becomes settled, an immutable accepted receipt is inserted, and
any compute contribution is inserted in the same transaction. The dispatcher
consumes only a receipt matching its expected execution and unit. Exact replay
returns the stored response; a changed replay fails.

Unknown, queued-but-unleased, expired, reclaimed, cancelled, superseded,
interrupted, wrong-node, wrong-execution, wrong-unit, wrong-kind, and malformed
submissions never enter the accepted-result broker. Rejected output may be
quarantined as a reason, hash, and at most 4 KiB preview, capped at 500 rows. It
does not satisfy dispatch, update normal success statistics, earn points, or
emit normal completion.

### What attempt authority does not prove

An admitted worker can still return plausible but wrong, malicious, copied, or
low-quality text. Attempt binding proves which active lease admitted a byte
sequence; it does not prove who physically controlled the node or whether the
bytes satisfy the user's intent.

The shared node secret permits any holder to register any `node_id`, create many
claimed identities, and impersonate another label on a future registration.
Attempt binding prevents settlement under the wrong active assignment, but not
admission-level Sybil behavior. Per-node keypairs, individual revocation,
rotation, and signed result envelopes remain prerequisites for a less-trusted
network.

## 6. What a malicious admitted worker can do

It can:

- read every task, contract, dependency context, or project-derived context
  assigned to it;
- return arbitrary text that passes structural checks while being behaviorally
  wrong;
- hold work until its lease expires and waste capacity;
- register many claimed node labels while holding the shared secret;
- report misleading hardware/capability metadata;
- consume operator time through repeated failures or plausible bad output.

It cannot, through the worker protocol alone:

- settle a queued-but-unleased or inactive task;
- downgrade a v1 attempt into legacy settlement;
- settle with a missing or mismatched nonce, contract, execution, or unit;
- settle the same attempt twice with changed output;
- turn a quarantined result into operational output or points;
- execute generated code on the requester machine.

Workers run a local model and return text. They do not execute the generated
artifact. That last boundary depends on operators continuing not to run
unreviewed generated code.

## 7. Read access and shares

`viewer_key` is one instance-wide reader authority. Every holder can read every
private route; there are no owners, roles, groups, or per-project ACLs. Browser
sessions are signed rather than server-stored, default to eight hours, and are
capped at seven days. Rotating `viewer_key` invalidates them. The cookie is
HttpOnly and SameSite=Lax; it is Secure when the request is HTTPS or the operator
sets `viewer_cookie_secure`.

Shares are deliberately narrower. A random 32-byte token is returned once at
creation; SQLite stores only its SHA-256 hash. Invalid, expired, and revoked
tokens all look like `404`. The response is built from an allowlist and omits
project/job ids, absolute paths, attempt credentials, raw logs, credit records,
private telemetry, and unbounded validator failures. Node identity is redacted
by default. Candidate details and artifact downloads require separate share
flags.

Share output is not an immutable snapshot. It reflects the current durable
execution record and currently retained artifacts. Revocation stops future
server access but cannot retract content already copied by a token holder. A
leaked token remains usable until expiry or revocation, so share URLs should be
treated like passwords.

## 8. Artifact safety

The artifact registry accepts only existing non-symlink directories strictly
below `output/` or `execution_artifacts/`. One root cannot belong to multiple
executions and an execution cannot switch roots. Public models contain only
normalized POSIX-relative paths.

Manifest scans and every open reject absolute paths, drives, colons,
backslashes, NULs, dot segments, parent traversal, encoded traversal, duplicate
normalized paths, symlink components, and resolved paths outside the root. The
server enforces file-count, per-file, and aggregate-byte limits and computes a
SHA-256 for every entry. Downloads re-scan and re-hash. ZIPs are built in a
bounded temporary file and streamed.

These controls stop path confusion and unbounded artifact delivery. They do not
make content safe. SHA-256 detects a changed file at access time but is not a
signature, provenance proof, malware scan, or content-addressed immutable store.
Private viewers can retrieve internal run files; public shares filter logs,
plans, transcripts, and hidden candidate detail.

Retention applies to registered roots in both storage families. Active roots
and canonical executions still queued/running are skipped. Pruning deletes the
artifact directory and manifest rows, not the durable execution result. A share
may therefore remain valid after its artifact files have expired.

## 9. Generated code and `network_policy`

Generated code is not sandboxed. Structural parsing and JSON Schema checks are
validation evidence, not security boundaries and not behavioral proof. Do not
execute generated code without review.

`network_policy` is recorded intent only. No OS firewall, container policy,
syscall filter, tool broker, or worker enforcement consumes it today. In
particular, `network_policy="disabled"` must not be advertised as guaranteed
network isolation. The worker normally runs only inference, but custom model
providers and later operator execution are outside this field's control.

## 10. Lifecycle and availability

Canonical lifecycle is durable, but the scheduler is not. Queues, connected
nodes, in-flight coroutines, and breaker state are process-local. On restart,
queued/running canonical executions and legacy jobs become retryable
`interrupted`; active attempts become interrupted and reject late output. This
prevents false forever-running state but does not resume work.

Deadlines and cancellation remove queued units, signal local tasks, and cancel
active attempts. External calls may not stop immediately if a dependency ignores
cancellation, but their late output cannot settle. The orchestrator is still one
process on one machine with no failover.

## 11. Contribution points are not correctness or money

The authoritative trusted-alpha ledger is SQLite. Worker settlement records
`basis=compute_contribution` and `points_are_monetary=false`. A worker may earn
compute points for an accepted bound attempt even if its candidate is later not
selected or the final execution fails validation. Those are different events.

`credits` and `ledger.json` remain compatibility names/projections. There is no
token, wallet, transfer, redemption, price, or payment. The host operator can
still alter SQLite or its compatibility file; the ledger is concurrent-safe and
idempotent, not tamper-evident against the machine owner.

## 12. Keyless public pitch

The optional public endpoint is off by default. When enabled it accepts only a
short task and rejects caller-controlled strategy, candidates, placement,
project, validators, and confidentiality. It uses one local direct candidate,
one global inference slot, a 120-second deadline, 64 KiB output cap, per-source
rate and active limits, and a global active cap. It cannot send a public task to
contributors.

This is bounded demo admission, not a hardened public service. Source identity
is derived from the request IP as seen by the application and can be distorted
by proxy configuration. The content filter is a coarse substring filter. A
request-count and concurrency cap reduce abuse; they do not eliminate it.

## 13. Remaining weaknesses

- Shared static secrets with instance-wide authority.
- No per-node cryptographic identity, rotation, or individual revocation.
- No built-in HTTPS; bearer secrets and content require external TLS.
- No generated-code or model-executor sandbox.
- `network_policy` is not enforced.
- Process-local scheduler, connected-node state, and breaker state.
- One orchestrator and no failover.
- No Sybil resistance or trustworthy worker hardware attestation.
- Structural/deterministic contract validation does not prove arbitrary
  behavioral correctness.
- Viewer auth is one role for the whole instance, not multi-user authorization.
- Share revocation cannot retract already downloaded content.
- The orchestrator host can alter SQLite, artifacts, or configuration.

## 14. Safe deployment posture

Run Mycelium on hardware you control, or among a small invited group whose node
operators you trust. Set `node_secret`, `pitch_key`, and `viewer_key` before
binding beyond localhost; use TLS or a private overlay network; keep keyless
pitching off unless you intentionally accept the compute cost; review generated
code; rotate a viewer key after suspected disclosure; and treat shared prompts
as disclosed to every worker that receives them.

Do not describe this implementation as trustless, permissionless,
confidential-compute, sandboxed, behaviorally verified, or safe for anonymous
stranger nodes.

Reporting a vulnerability: [SECURITY.md](../SECURITY.md).
