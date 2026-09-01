# Threat model

_Implemented state on August 31, 2026. This describes current controls and
current gaps, not the intended end state._

**One-sentence version:** Mycelium is suitable for a small private trusted
alpha whose operators and node holders are known. It is not safe as an open,
permissionless compute network.

Trusted-alpha hardening requires a worker result to settle a server-issued
durable, session-bound attempt before execution can consume it; protects
sensitive read routes in deployment mode; seals role-scoped artifact manifests;
enforces one coordinator per state directory; commits lifecycle truth before
publication; deduplicates requester-scoped canonical retries; and adds durable
per-node bearer enrollment with independent revocation and attempt attribution,
and contains parser-heavy built-in validators in bounded child processes.
It did not add public-key/physical-machine identity, TLS, a hostile generated-code
sandbox, multi-user accounts,
durable scheduling, attestation, Sybil resistance, or a hostile-host trust
boundary.

## 1. Assets

| Asset | Where it lives | Realistic failure |
| --- | --- | --- |
| Task and project text | execution/job SQLite, project files, coordinator memory, assigned worker prompts | disclosure to an unauthorized reader or worker operator |
| Generated output and artifacts | SQLite previews/receipts, `output/`, `execution_artifacts/` | disclosure, tampering, retention beyond intent, or unsafe code execution by an operator |
| Attempt authority | SQLite attempt rows and accepted receipts | an unbound, late, or duplicate result entering execution or earning points |
| Node enrollments | SQLite identity/status/credential digest plus worker-owned plaintext identity file | credential theft, label takeover, failed revocation, or false attribution |
| Node sessions | process memory plus session identifiers/digests attached to node and attempt state | stale incarnation use, bearer-token disclosure, or restart invalidation |
| Capability claims and snapshots | live registry plus canonical enrollment-scoped SQLite snapshots; descriptor/requirement digests on attempts | false hardware/model/isolation claims, private inventory disclosure, or assigning work under the wrong snapshot |
| Scoped capability observations and shadow decisions | append-only SQLite rows derived from coordinator-owned attempt state; decisions use the isolated shadow database | false attribution, replay conflict, evidence poisoning, private inventory disclosure, or accidental use in production routing |
| Viewer, pitch, and admission secrets | local config/environment and HTTP headers | private reads, unwanted compute use, or initial worker admission |
| Canonical submission identity | digest-only SQLite mapping to an execution | duplicate execution after retry, key conflict, or a mapping whose execution is missing |
| Artifact integrity baseline | sealed manifest rows/hash in SQLite plus files on disk | ordinary file drift, database/file loss, or host-level joint tampering |
| Validator runner inputs and evidence | bounded protocol metadata, a fixed hash-and-size-bound temporary output reference, temporary `code_parse` copies or validated metadata-only logical names, process memory, and durable parent-owned evidence envelope/metadata with bounded child detail | parser resource exhaustion, output-reference substitution, stage escape, secret or host-path disclosure, orphan process, or false assurance metadata |
| Contribution history | SQLite plus `ledger.json` compatibility projection | misattribution or misleading claims about correctness/value |
| Contributor hardware | worker machines | sustained model inference, disk use for the model, prompt disclosure |
| Orchestrator availability and backups | one locked state directory, SQLite/files, and operator-created archives | total service interruption, stale restore, or lost process-local queue/session work |

There are no full user accounts, payment data, wallets, or token balances. A
deployment may still hold confidential task content, project memory, node
hardware descriptions, hostnames, and generated artifacts, so "no accounts"
does not mean "no sensitive data."

## 2. Trust boundaries

```text
Requester ── pitch_key + optional scoped retry key ──▶ ORCHESTRATOR
                                                       │
                                                       └─ node_secret admission ──▶ Worker
                              │       │                                 │
                              │       └─ issues current node session ───┤
                              │ viewer_key                              ├─ sees assigned prompt
                              ▼                                         └─ returns model text
                         Private readers
                              │
                              └─ explicit share token ──▶ capability holder
```

- The orchestrator process and host filesystem are inside the primary trust
  boundary.
- A validator child is a separate failure and resource-containment process, but
  it runs as the orchestrator's operating-system user. It is not outside a
  mandatory-access-control or filesystem-confidentiality boundary.
- Requesters, viewers, workers, and share holders are outside it.
- The three configured static secrets represent different authority. Possessing
  one must not imply possessing the other two.
- Every bootstrap worker may initially receive the same `node_secret`, but that
  secret authorizes creation only. Each durable enrollment has a different
  worker-generated bearer credential and can be revoked independently.
- `enrollment_id` is stable attribution inside this coordinator. A server
  session identifies one live incarnation. `node_id` remains a display label;
  none of these prove a physical machine or unique human operator.
- A share token is a bearer capability. Anyone who receives it can use its
  exact redacted scope until expiry or revocation.

## 3. Controls implemented today

| Control | What it enforces | What it does not enforce |
| --- | --- | --- |
| `viewer_key` | private HTTP and WebSocket access via header, Bearer token, or signed expiring HttpOnly cookie | users, roles, per-execution ACLs, TLS |
| `pitch_key` | canonical and compatibility task submission when configured | private reads or public-share revocation |
| Scoped canonical idempotency | one queued execution per requester-scope/key/canonical-request mapping, with digest-only storage | user identity, workflow resumption, or exactly-once external side effects |
| `node_secret` | instance-wide permission to bootstrap a previously unused enrollment | returning identity, label replacement, or normal enrolled operations |
| Digest-only enrollment credentials | durable per-node attribution, returning registration, independent revocation/rotation | physical-machine identity, attestation, or Sybil resistance |
| Digest-only node sessions | one live enrollment incarnation, stale reclaim, and restart invalidation | durable scheduling or identity by themselves |
| Versioned capability claims and one hard matcher | bounded deterministic eligibility, immutable per-session descriptor identity, and exact descriptor/requirement binding on attempts | truth of any claim, attestation, performance evidence, trust, correctness, or ranking among eligible nodes |
| Scoped capability evidence | exact enrollment/descriptor/executor/model/task-class/role scopes, bounded typed observations, append-only replay-safe rows, protected aggregates, and shadow-only counterfactuals | descriptor truth, semantic correctness, trust, reputation, Sybil resistance, or production routing |
| Server-owned attempts | active lease and exact task/execution/unit/kind/enrollment/node/session/version/nonce/output-cap binding | truthfulness or quality of the returned model output |
| Atomic settlement | one accepted attempt transition, receipt, response, and compute contribution; durable exact-replay state across database reopen | durable scheduling, worker resumption, or reuse of a restart-invalidated node session |
| Worker I/O bounds | per-attempt result/stream byte cap, error cap, cumulative stream batch/rate limits, bounded fanout | semantic safety of allowed output or transport-level denial of service |
| Result quarantine | 500 bounded diagnostics with hash and at most 4 KiB preview outside operational execution | malware analysis or a complete forensic archive |
| Total execution deadline | shared remaining budget for strategy, local calls, worker waits, validation, and finalization | forcibly stopping an external process that ignores cancellation |
| Bounded validator runner | closed built-in allowlist; strict V2 control-only stdin and bounded stdout; canonical-budgeted output staged at one fixed reserved path and bound by exact size/SHA-256/UTF-8; bounded regular-file copies for `code_parse`; validated logical names and an empty private directory for metadata-only checks; process-group cleanup; best-effort Windows Job Object with kill-on-close and one-active-process limit; available POSIX CPU/memory/file/descriptor/process limits; parent-owned authoritative metadata with bounded child detail | hostile native-code isolation, same-user filesystem confidentiality, reliable network denial, guaranteed Windows Job assignment or POSIX-equivalent resource limits, behavioral correctness, or generated-code execution safety |
| Restart reconciliation | truthful `interrupted` state for non-resumable executions/jobs and active attempts | resuming lost process-local work |
| Durable-before-publication lifecycle | required commit before live snapshot, normal event, project-memory/legacy mirror, callback, response, or terminal state/artifact publication through canonical shares and legacy run/history/demo surfaces | durable event delivery, external transactionality, or coordinator HA |
| Structural event retention | per-event allowlists before memory, SQLite, broadcast, and replay; startup redaction of historical payloads; generated tokens live-stream-only | removing sensitive text from pre-upgrade backups, proxy logs, or already copied event data |
| Sealed artifact registry | role/winner scope, root confinement, normalized paths, symlink rejection, immutable local baseline, live re-hash, quotas, streaming delivery, retention | content safety, hostile-host tampering, signature, or sandboxing |
| Explicit shares | unguessable hash-only bearer tokens, expiry, list/revoke, redaction, scoped deliverable/candidate-source permission | preventing redistribution by a token holder or access-log capture |
| Public-pitch profile | one local candidate with short timeout/output and compute-aware admission | strong abuse prevention or semantic content moderation |
| SQLite contribution ledger | concurrent-safe, idempotent non-monetary records | tamper evidence against the host operator |
| SQLite/ownership policy | one OS-locked coordinator, WAL/foreign keys/busy timeout/bounded retry, serialized migrations, transactional boundaries | multi-coordinator operation or failover |
| Verified backup/restore | SQLite online snapshot plus bounded archive manifest/checksums and validate-before-install restore | live process state, independent attestation, or off-site backup scheduling |
| Legacy post-hoc status fields | explicit report that trusted-alpha execution-level duplicate verification is disabled | scoped operational evidence, duplicate execution, or stronger correctness evidence |

Secret comparisons use constant-time comparison where static credentials or
signatures are checked. This reduces one narrow side channel. It does not make a
shared secret equivalent to cryptographic identity.

Contribution rows use fixed task labels and omit free-form details. Startup
redacts historical live SQLite/`ledger.json` projections, but backups or copies
created before the upgrade can retain their earlier contents and require
operator-managed rotation.

Event persistence retains only allowlisted identifiers, states, counts, and
other bounded structural fields. Startup rewrites historical live event rows
before HTTP or WebSocket replay, but pre-upgrade backups and exports remain an
operator-controlled historical-retention risk.

## 4. Public and private reachability

When `viewer_key` is configured, the deliberate unauthenticated surface is:

- `GET /` and `GET /try`;
- static assets under `GET /static/*`;
- `GET /health` and `GET /status.json`;
- `GET /v1/shares/{token}` and token-scoped share artifact routes;
- `POST /public/pitch` only when the operator enables it;
- viewer session exchange/logout endpoints.

Pitch and worker protocol routes are exempt from the viewer gate because they
use their own credentials. Poll, result/stream/token, heartbeat, and drain also
require the current node session. In local mode an empty `pitch_key` opens pitch
admission, while `node_enrollment_mode=compat` plus an empty `node_secret` opens
only the explicitly unenrolled legacy worker path.

Canonical `Idempotency-Key` does not bypass pitch authentication, canonical
validation, or rate limiting. Configured pitch-key holders share one requester
scope. Open mode hashes the direct peer host and ignores forwarding headers;
that address is a best-effort duplicate boundary, not authorization or user
identity.

Everything else is viewer-protected, including canonical execution reads and
cancellation, jobs, events, WebSockets, node details, history, gallery, run
pages, projects, ledger, standings, metrics, artifact APIs, share creation/list/
revocation, private operator health, and the dashboard.

`/status.json` is deliberately narrow: service/inference state, model name,
counts, uptime, repository, and build fingerprint. It does not include task
text, result text, hostnames, hardware detail, attempt identifiers, nonces, or
project ids. `/health` includes liveness, model names, queue/node counts, and
whether private routes are protected. It warns when they are not.

### Fail-open local-development mode

All three deployment secrets default to empty in `deployment_mode=local` for
compatibility, and `node_enrollment_mode=compat` may admit an explicitly
unenrolled legacy session. Most importantly, when `viewer_key` is empty the
viewer middleware allows private routes. Startup logs and `/health` explicitly
say so. Anyone who can reach that deployment can then read tasks, results,
projects, events, node detail, and artifacts. A compatibility session is not
durable identity and cannot claim a label already in the enrollment table.

In this mode, keyed canonical submissions are scoped to the direct ASGI peer
host. NAT can merge callers, address changes can split one caller, and proxies
can hide the original peer. The scope must not be used for ownership,
attribution, or security policy.

`deployment_mode=trusted_alpha` changes this posture to fail closed: preflight
and startup require independent 32+-character viewer, pitch, and node secrets,
`node_enrollment_mode=required`, coherent HTTPS/cookie intent, a declared TLS
or private authenticated overlay transport, safe config/state paths, and
explicit public-pitch acknowledgement when that endpoint is enabled. `/health`
exposes safe protection/enrollment bits; viewer-authorized operator endpoints
expose mode, process lock, and secret-free enrollment state. A private-overlay
configuration value is an operator assertion: preflight cannot inspect overlay
ACLs or supply transport security.

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
or unit kind is a rejection. The assigned enrollment, node, session,
credential version, URL task, lease, state, output cap, and all applicable
bindings must match. A worker cannot downgrade validation by omitting fields or
by reconnecting under a different enrollment or session.

Settlement uses a SQLite write transaction and uniqueness constraints. The
active attempt becomes settled, an immutable accepted receipt is inserted, and
any compute contribution is inserted in the same transaction. The dispatcher
consumes only a receipt matching its expected execution and unit. Exact replay
returns the stored response; a changed replay fails.

Unknown, queued-but-unleased, expired, reclaimed, cancelled, superseded,
interrupted, wrong-node, wrong-execution, wrong-unit, wrong-kind, and malformed
submissions never enter the accepted-result broker. Output over the attempt cap
is also rejected before settlement. Rejected output may be quarantined as a
reason, hash, and at most 4 KiB preview, capped at 500 rows. It
does not satisfy dispatch, update normal success statistics, earn points, or
emit normal completion.

### What attempt authority does not prove

An admitted worker can still return plausible but wrong, malicious, copied, or
low-quality text. Attempt binding proves which active lease admitted a byte
sequence; it does not prove who physically controlled the node or whether the
bytes satisfy the user's intent.

Likewise, a descriptor hash proves which canonical self-reported claim was used
for eligibility and assignment. It does not prove that claimed CPUs, memory,
GPUs, executor/model version or digest, context capacity, features, limits, or
isolation exist. Best-effort stock-worker detection and operator overrides are
both inside the worker's trust boundary. Unknown data is safer than invention,
but an actively malicious admitted worker may still submit a syntactically
valid lie.

Bootstrap consumes shared admission plus a worker-generated high-entropy
credential and creates an immutable enrollment. Returning registration proves
that per-node bearer without the shared secret. The coordinator stores only a
domain-separated digest; the worker stores plaintext in a private identity
file. Registration then returns a random session token once and retains only
its digest. Restart invalidates sessions, not enrollments.

Revocation and rotation are independently durable. Each authenticated operation
checks status/version, and settlement checks active enrollment inside its write
transaction. This prevents one enrolled session from settling another
enrollment's attempt and prevents shared admission from replacing an existing
label. It still does not prevent an authorized bootstrap holder from creating
many unused-label enrollments. Public keys, signed envelopes, attestation, and
Sybil defenses remain prerequisites for a less-trusted network.

## 6. What a malicious admitted worker can do

It can:

- read every task, contract, dependency context, or project-derived context
  assigned to it;
- return arbitrary text that passes structural checks while being behaviorally
  wrong;
- hold work until its lease expires and waste capacity;
- bootstrap many unused labels while holding the shared admission secret;
- report misleading hardware/capability metadata;
- change claims between new sessions to seek different hard-constraint work;
- send bounded but high-rate model text up to the enforced protocol limits;
- consume operator time through repeated failures or plausible bad output.

It cannot, through the worker protocol alone:

- settle a queued-but-unleased or inactive task;
- downgrade a v1 attempt into legacy settlement;
- settle with a missing or mismatched nonce, contract, execution, or unit;
- replace an existing or revoked enrollment with shared admission alone;
- settle an attempt assigned to another enrollment;
- exceed the assigned cumulative stream/output cap and still settle that output;
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
flags. Share artifact access is deliverable-only by default; candidate source
requires the candidate-detail flag; provenance, logs, and internal roles are
never shared. If there is no winner, candidate-scoped entries are excluded.

Public share responses set no-store, no-referrer, and nosniff headers. The
application's unhandled-error path logging redacts the token segment. Uvicorn
and reverse-proxy access logs remain operator-controlled and must be configured
not to retain raw capability URLs.

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
SHA-256 for every entry. Terminal finalization applies winner prefix/entry
roles and seals immutable manifest rows plus a canonical hash. Every download
then resolves and re-hashes the live bytes against that baseline. Active and
historical `legacy_live` roots are rescanned; legacy roots are not mislabeled
sealed. ZIPs are built in a bounded temporary file and streamed.

These controls stop path confusion, ordinary post-seal drift, and unbounded
artifact delivery. They do not make content safe. SHA-256 is not a signature,
independent timestamp, provenance proof, malware scan, or protection against a
host that can alter both file and SQLite baseline. Private delivery defaults to
the `deliverable` role; explicit audit views may retrieve provenance, logs,
candidate source, and internal records. Public shares use the narrower role
policy above.

Retention applies to registered roots in both storage families. Active roots
and canonical executions still queued/running are skipped. Pruning deletes the
artifact directory and manifest rows, not the durable execution result. A share
may therefore remain valid after its artifact files have expired.

## 9. Generated code and `network_policy`

Generated code is not sandboxed. Production `code_parse` treats it as data: it
does not import a generated module, run top-level statements, invoke a shell or
build script, install packages, run tests, start a browser, or execute generated
networking. Structural parsing and JSON Schema checks are validation evidence,
not behavioral proof. Do not execute generated code without review.

In the default `auto` mode, parser-heavy built-ins (`code_parse`,
`structured_json`, and `json_schema`) run in a bounded child process. The
V2 request contains strict size-bounded control metadata and the response is
strict and bounded. The generated output body is not embedded in that request.
For output-consuming checks, the parent writes the exact strict UTF-8 bytes to
one fixed reserved file in the fresh workspace and binds its literal path,
encoding, exact byte length, and lowercase SHA-256 in the request. The
execution's canonical `max_output_bytes` remains the authoritative output limit
up to 10 MiB; the default 2 MiB request limit applies only to control metadata.
V1 is retained for explicit parsing/tests, never emitted by new parent calls or
used as a downgrade after V2 failure. `code_parse` receives bounded
file copies from only the authoritative candidate subtree. Metadata-only
validators forced through `subprocess` receive validated normalized logical
names and an empty private directory, without copying or rehashing artifact
content. The same root/subtree, regular-file, symlink/special-file,
snapshot-membership, path, and count validation applies to those names. The
environment is sanitized, and timeout/cancellation requests process-tree
cleanup and counts inability to confirm it. Simple bounded checks remain inline.
Forced `subprocess` supports every current built-in; explicit `inline`
is a weaker local-development mode rejected by trusted-alpha preflight.

The launcher fixes child `cwd` to the parent-created private validator directory.
The runner resolves it once and supplies that explicit `stage_root` to the
closed dispatcher; strict request data cannot choose or replace the root. An
ambient or operator launch directory is therefore not protocol authority.

The reserved `__mycelium_validator_input__` namespace and all descendants are
rejected as candidate artifact paths. The parent uses exclusive creation,
no-follow and descriptor-relative operations where supported, private POSIX
modes, and inherited Windows temporary-root ACLs; it writes/hashes the exact
bytes and closes the file before spawn. The child accepts no alternate path,
rejects links/reparse points and nonregular targets, opens the file once, and
checks confinement, descriptor identity, hard and declared size, constant-time
digest equality, and strict UTF-8 before invoking the allowlisted built-in.
These checks reduce accidental substitution and race exposure. They do not
prevent a malicious same-user process from attempting access to the workspace.

This boundary contains trusted parser failures; it is not a hostile-code
sandbox. The child shares the coordinator's OS user and can therefore attempt
filesystem access outside the stage unless a separately administered host
policy prevents it. POSIX resource limits are defense in depth. Windows retains
wall time, bounded I/O, staging, and best-effort process cleanup. A per-run Job
Object is best-effort configured with kill-on-close and an active-process limit
of one, then assigned immediately after spawn. Successful assignment prevents
additional active processes in that Job and improves parent-exit cleanup, but
does not provide POSIX-equivalent CPU, address-space, output-file, or descriptor
limits. Containers, VMs, WASM, gVisor,
Firecracker, arbitrary validator plugins, and behavioral code execution remain
out of scope.

Job creation/configuration or assignment may fail, including inside a
restrictive enclosing Job Object, and the runner is not suspended during the
spawn-to-assignment window. A process or descendant outside successful Job
assignment retains only the live-parent process-group/`taskkill /T` fallback.
The containment label remains `windows_process_tree_best_effort`; it is not
proof of Job membership. The implementation has no Linux parent-death signal,
Windows child-side alarm, or durable runner PID registry. After abrupt
coordinator loss, the POSIX child has an early hard alarm of about 125 seconds;
an unassigned Windows runner or pre-assignment escape can outlive the
coordinator, and restart does not discover it. Operators must check for a
confirmed prior runner after a crash.

Staged artifact copies and output references live under the operating-system
temporary directory, with private POSIX modes where supported. On Windows they
inherit the temporary root's ACL;
mode bits do not establish a private DACL, so operators must secure that root.
The implementation attempts process-tree cleanup before stage removal, but
an abrupt coordinator/host failure or a deletion failure can leave generated
source in a `mycelium-validator-*` directory. A handled deletion failure records
`validator_stage_cleanup_failed` evidence and increments
`staging_cleanup_failures`; an abrupt host loss may preclude either record.
There is no startup stale-stage sweeper or secure-erasure guarantee; incident response must include
temporary-storage review after confirming no live runner owns the directory.

Output-reference failure reasons and counters are content-free categories. They
do not retain the generated body or excerpts, output JSON keys/values, schema,
reserved private path, expected or observed digest, credentials, child
environment, raw stderr, absolute workspace path, or arbitrary exception text.
Runner metadata added to public executions or shares likewise contains none of
the private reference fields; existing result disclosure rules are unchanged.

`network_policy` is recorded intent only. No OS firewall, container policy,
syscall filter, tool broker, or worker enforcement consumes it today. In
particular, `network_policy="disabled"` must not be advertised as guaranteed
network isolation. The validator runner does not make that field enforceable.
The worker normally runs only inference, but parser dependencies, custom model
providers, and later operator execution are outside this field's control.

## 10. Lifecycle and availability

Canonical lifecycle is durable, but the scheduler is not. Queues, node sessions,
connected-node state, in-flight coroutines, and breaker state are process-local. On restart,
queued/running canonical executions and legacy jobs become retryable
`interrupted`; active attempts become interrupted and reject late output. This
prevents false forever-running state but does not resume work.

Required queued, running, and terminal snapshots commit before their public
live copy, normal lifecycle event, callback, compatibility mirror, response, or
terminal state/artifact publication through a share. Permanent persistence
failure leaves the last durable snapshot authoritative and suppresses normal
publication. A diagnostic event
can report a safe phase and attempt count, but it is not lifecycle truth. Once
a terminal snapshot commits, later telemetry or callback failure does not undo
it.

Requester-scoped submission mappings survive restart and backup. A replay
returns the same execution, including an interrupted one, without scheduling
replacement work. This closes duplicate creation by canonical client retry; it
does not make model calls, worker calls, callbacks, or filesystem effects
exactly once.

Deadlines and cancellation remove queued units, signal local tasks, and cancel
active attempts. External calls may not stop immediately if a dependency ignores
cancellation, but their late output cannot settle. Exactly one coordinator may
hold a state directory: an OS lock is acquired before migration/background work,
and a second process fails startup. SQLite uses the common WAL/foreign-key/busy-
timeout/retry policy and immediate transactions at integrity boundaries. This is
one-process concurrency, not failover or multi-coordinator safety.

Backup uses SQLite's online backup API and a versioned, checksummed ZIP of
durable state. Restore validates layout, types, path/collision safety, JSON,
SQLite, and checksums before same-filesystem installation with rollback. It does
not capture process-local queues, node sessions, in-flight work, or breaker
state, and it does not schedule or move archives off-host.

## 11. Contribution points are not correctness or money

The authoritative trusted-alpha ledger is SQLite. Worker settlement records
`basis=compute_contribution` and `points_are_monetary=false`. A worker may earn
compute points for an accepted bound attempt even if its candidate is later not
selected or the final execution fails validation. Those are different events.

`credits` and `ledger.json` remain compatibility names/projections. There is no
token, wallet, transfer, redemption, price, or payment. The host operator can
still alter SQLite or its compatibility file; the ledger is concurrent-safe and
idempotent, not tamper-evident against the machine owner.

## 12. Scoped capability evidence is not reputation

The evidence subsystem records coordinator-observed operational outcomes. Its
scope includes the immutable enrollment and descriptor, executor and worker
protocol, selected model including optional digest/variant, task class, and
evidence role. Missing, corrupt, historical-only, or inconsistent bindings are
excluded rather than guessed. Descriptor, selected-model, task-class, and role
changes therefore cold-start a new scope. This limits accidental history mixing;
it does not stop an admitted worker from lying in a descriptor, changing claims
between sessions, or deliberately shaping its output to game future observations.

Only bounded server-owned facts are recorded: settlement category, deadline
completion, wall time, output bytes/effective throughput, terminal contract-floor
outcome, sampled output-shape agreement, lease expiry, and stale-node disconnect.
The last two are the only worker-attributable terminal failures. Payload/stream
limits, caller cancellation, execution deadline, receipt-binding failure,
enrollment reclaim, session replacement, coordinator restart, supersession,
unknown causes, and free-form error text are excluded. This conservative
attribution avoids charging coordinator or caller failures to a worker, but can
also omit real worker harm whose cause cannot be proven from typed state.
Sampled comparison additionally requires a durable exact primary-attempt
binding; another unit in the same execution is not an interchangeable pair.

Deterministic domain-separated IDs make exact replay idempotent and conflicting
content an error; SQLite triggers reject update/delete. Evidence failure is
contained so it cannot roll back accepted settlement, receipt, or contribution
credit. Bounded repair selects missing attributable observations and terminal
executions lacking an append-only contract-floor projection receipt, so
already-complete rows do not starve the batch. These properties defend against
ordinary retries and partial failures, not a malicious coordinator host that
can alter SQLite or code.

`capability_evidence_mode` is `off` by default and permits only `off` or
`shadow`. Shadow evaluation runs after real assignment, considers only
hard-eligible candidates, freezes their exact descriptor/model scopes at
assignment time, uses an assignment-time evidence cutoff, and never changes
production routing. Below the configured minimum (default 5), evidence
is explicitly insufficient. Sampled agreement is shape agreement, not truth,
and is not a preference dimension. The circuit breaker remains separate.

Viewer-protected `GET /v1/operator/capability-evidence` exposes aggregates and
grouped shadow counts, not raw rows. `GET /nodes`, `/health`, and `/status.json`
carry no evidence score, reputation, trust flag, or routing weight. Evidence
rows omit prompts, output bodies, worker-error text, free-form reasons,
credentials, nonces, session secrets, and arbitrary telemetry, but their scoped
hardware/model and timing aggregates remain private operational inventory and
are included in coordinator backups.

## 13. Keyless public pitch

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

## 14. Remaining weaknesses

- Shared static secrets still provide instance-wide bootstrap, pitch, or viewer
  authority within their separate roles.
- Enrollment uses a symmetric bearer credential, not public-key identity or a
  signature, and identity-file recovery is an operator responsibility.
- Node sessions are process-local incarnation credentials, not durable
  scheduling state.
- No built-in HTTPS; enrollment/session secrets and content require external
  TLS or a private authenticated overlay.
- No generated-code or model-executor sandbox. The validator child contains
  allowlisted parser failures only; it is not same-user filesystem isolation.
- Windows lacks the runner's POSIX CPU, address-space, output-file, descriptor,
  and unconditional child-process resource-limit guarantees. Job Object
  assignment, cleanup, and path-race resistance remain best effort.
- Coordinator crash has no universal cross-platform parent-death enforcement or
  restart orphan discovery; an unassigned Windows validator or pre-assignment
  escape may survive until operator cleanup.
- `network_policy` is not enforced.
- Process-local scheduler, node sessions, connected-node state, and breaker state.
- One orchestrator and no failover.
- No Sybil resistance or trustworthy worker hardware attestation.
- Capability descriptors and their SHA-256 hashes are node claims, not
  observed evidence, hardware/model attestation, or trust. The matcher only
  excludes, and scoped observations do not validate the claim.
- Evidence can be sparse, strategically gamed, or omitted by conservative fault
  attribution. It is diagnostic only and never changes production routing.
- Structural/deterministic contract validation does not prove arbitrary
  behavioral correctness.
- Viewer auth is one role for the whole instance, not multi-user authorization.
- All holders of one pitch key share an idempotency scope; open peer scoping is
  not durable identity.
- Idempotent submission does not resume work or make external effects exactly
  once.
- Share revocation cannot retract already downloaded content.
- Uvicorn/reverse-proxy access logs can expose share URLs unless configured.
- Sealed manifests are local baselines, not externally anchored attestations.
- Trusted-alpha execution-level post-hoc duplicate verification is explicitly
  disabled; sampled shape agreement, when separately enabled, is not correctness.
- The orchestrator host can alter SQLite, artifacts, or configuration.

## 15. Safe deployment posture

Run Mycelium on hardware you control, or among a small invited group whose node
operators you trust. Use `deployment_mode=trusted_alpha`, run preflight, set
independent strong `node_secret`, `pitch_key`, and `viewer_key` values, require
durable enrollment, keep `validator_execution_mode` at `auto` or
`subprocess`, and allow exactly one process to own the state directory
before binding beyond localhost. Use TLS or a private authenticated overlay;
configure access logs to redact share capabilities and never capture
idempotency, admission, enrollment, session, or attempt credentials; keep
keyless pitching off unless you explicitly acknowledge and accept its compute
risk; take and verify backups; protect worker identity files; review generated
code; rotate or revoke the narrowest affected authority after disclosure; and
treat shared prompts as disclosed to every worker that receives them.

Do not describe this implementation as trustless, permissionless,
confidential-compute, sandboxed, behaviorally verified, or safe for anonymous
stranger nodes.

Reporting a vulnerability: [SECURITY.md](../SECURITY.md).
