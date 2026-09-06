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

## 4a. What each deployment path protects, and what it does not

Two deployment shapes are documented in [DEPLOY.md](DEPLOY.md). They are not
two flavours of the same protection; they defend against different things, and
each leaves different things open.

### Common to both

Both put a TLS reverse proxy on the public socket and keep the application
published on `127.0.0.1:8000` only. That placement is load-bearing rather than
tidy: if the application port is reachable directly, a client can bypass the
proxy entirely, and the certificate, the security headers and the request-body
limit all become decoration while enrollment credentials cross the network in
clear text.

`docker-compose.yml` therefore publishes a literal `127.0.0.1:8000:8000` with
no variable to override it. **A host firewall is not a substitute for that
bind.** Docker writes published ports into its own iptables chain, evaluated
before the chain ufw manages, so `ufw deny 8000` reports "deny" on a port that
is still answering the Internet, and `ufw status` never reveals it. The trap and
the two commands that expose it (`ss -tlnp`, `sudo iptables -L DOCKER -n`) are
documented in
[DEPLOY.md](DEPLOY.md#the-trap-docker-does-not-consult-ufw).

Neither path changes anything about worker-result integrity, attempt authority,
enrollment, settlement, or the protocol window. Neither adds a generated-code
sandbox. Neither makes the coordinator multi-tenant.

### Path A — private overlay (Tailscale)

**Protects against:** every unauthenticated party who is not on the tailnet.
`POST /nodes/register` — which has no rate limit (§14) — cannot be reached at
all without tailnet membership, so the unlimited guessing surface is removed
rather than merely narrowed. Port scanning, opportunistic exploitation and
mass-scanned vulnerabilities do not apply to a coordinator with no public
socket.

**Does not protect against:** anyone already on the tailnet. A tailnet is a
network, not an authorization boundary for this application: any device on it
reaches the coordinator and is then subject only to the same three credentials
as anybody else. It also does not protect against a compromised or shared
tailnet account, and it does not make plaintext acceptable — the worker refuses
`http://` to an overlay address exactly as it does to a public one, because an
overlay ACL is an operator assertion that a contributor cannot verify.

**Two independent authorities, and both must be revoked.** Tailnet membership
decides whether a machine can reach the address; a Mycelium enrollment
credential decides whether it is admitted once it can. Removing a device from
the tailnet does not revoke its enrollment — that credential is a bearer token
and still works if the machine ever regains tailnet access. Revoking the
enrollment does not remove the device from the tailnet, where it retains
whatever else that network reaches. Neither action implies the other and
neither is sufficient alone.

**`private_overlay: true` is an assertion, not a detection.** Preflight records
it and cannot inspect an ACL.

### Path B — public domain (Caddy)

**Protects against:** passive interception and tampering on the path, through a
publicly-trusted certificate with automatic renewal; downgrade to plaintext,
through an HTTP-to-HTTPS redirect and HSTS; oversized request bodies, through a
limit set just above the coordinator's own 10 MiB result ceiling so the
coordinator refuses them with a protocol error rather than the proxy with an
opaque 413; and — in the shipped configuration — public reach to `/dashboard`,
`/v1/operator/*` and `/metrics`, which are refused at the edge so a viewer key
never has to travel over the Internet.

**Does not protect against:** anything reachable by anyone. Every
unauthenticated route in §4 is exposed to the whole Internet, and the only
thing standing between a stranger and an enrollment is the entropy of
`node_secret`, guessable without limit and without a log line an operator would
notice (§14). The certificate authenticates the *server* to workers; nothing
authenticates a worker to the server except a bearer credential. Caddy's
access log records request paths, so share tokens are filtered out of it in the
shipped configuration — an unfiltered proxy log is a credential store.

### What proxying changes about what the application sees

Behind either proxy every request arrives from `127.0.0.1`. The coordinator does
not consume `X-Forwarded-For`; `trust_proxy_headers` defaults to `false` and
trusted-alpha preflight rejects `true`. Two behaviours follow from this and are
documented rather than left to be discovered:

- **Open-mode idempotency scoping** hashes the direct peer host and
  deliberately ignores forwarding headers. Behind a proxy every caller shares
  one scope. This does not affect either documented path, because both
  configure a `pitch_key` and a configured key becomes the scope; it matters
  only for open mode behind a proxy, which is not a supported shape. The scope
  was never authorization or identity, and behind a proxy it stops being a
  useful duplicate boundary either.
- **The pitch rate limiter** buckets by the same address, so behind a proxy the
  configured limit is global rather than per-visitor. For an invited alpha that
  is acceptable; with `public_pitch` enabled it means one visitor can consume
  the whole allowance.

### Self-signed certificates cannot work, by construction

A worker trusts the certifi CA bundle and nothing else, and builds its HTTP
client with `trust_env=False` — the same decision that stops an ambient
`HTTPS_PROXY` inheriting enrollment bearers also stops `SSL_CERT_FILE` adding a
private CA. There is no flag anywhere that relaxes it. Both documented paths
obtain publicly-trusted certificates for this reason, and
`scripts/tls_local_check.py` lets an operator confirm theirs is acceptable
before inviting anybody rather than during somebody's failed install.

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

## 6a. What a contributor is and is not exposed to

Section 6 asks what a bad worker can do to the network. This one asks the
question a contributor actually has: *what can this do to my machine?* They are
not the same question and the second one has a worse historical answer, because
until now it was only ever answered in prose.

### The coordinator is the adversary here

Not a hypothetical intruder — the person whose invitation the contributor
accepted. That is the honest framing, and everything below is written against
it.

**A coordinator cannot, through the worker protocol:**

- run code on the contributor's machine. The worker receives a prompt, hands it
  to a local model, and returns text. It never executes, compiles, or evaluates
  anything it was sent. `tests/test_contributor_safety.py` drives the worker
  with a task carrying Python, shell fragments, command substitution, and a
  path traversal, and asserts the filesystem is byte-for-byte unchanged;
- cause a file to be written anywhere. The task-execution path contains no
  filesystem call at all, which is asserted structurally so it holds for inputs
  no test thought of;
- name a model the worker did not advertise. A server model binding is checked
  against the worker's own immutable capability descriptor and refused
  otherwise, so the one server-supplied value reaching a local runtime is
  bounded by something the contributor's machine chose;
- open an inbound port, read files, or reach anything on the contributor's
  network. The worker dials out; nothing dials in;
- read the credential of any other node. Each enrolment is separate and
  revocable, and it lives in a file created readable only by its owner;
- change the contributor's firewall, certificate trust store, startup programs,
  or any other security setting. The installer touches none of them, and a test
  asserts it does not so much as reference the commands that would. On macOS
  that list explicitly includes Gatekeeper: nothing here strips a quarantine
  attribute, re-signs anything, edits a shell profile, or alters the search
  path. `tests/test_macos_worker.py` holds that across every worker module.

**A coordinator can:**

- read every answer the contributor's machine produces, and every prompt it
  chose to send;
- see that the machine is connected, what it claimed about itself, and how much
  work it has done;
- occupy the contributor's processor for as long as they leave it running;
- stop trusting or paying the contributor, which is not a security property.

**A coordinator could try, and gets much less than it looks:**

- *sending markup in a task title.* Task titles are coordinator-controlled text
  printed on the contributor's screen. They are escaped before rendering, so a
  title cannot colour a volunteer's terminal or plant a clickable link in it.
  Found and closed while writing the contributor-safety tests;
- *supplying a hostile model name to the pull step.* Model names come from the
  contributor's own config, are validated against a strict pattern, and reach
  `ollama` as one element of an argument list. There is no shell anywhere in
  the worker or the installer;
- *redirecting the registration.* The installer does not follow redirects while
  carrying a credential, and says so rather than failing obscurely.

### What still protects nobody

- **There is no longer a `curl … | bash` install**, which used to be the first
  thing the README offered. The download and the execution were one command,
  so there was no moment at which the person running it could read what they
  were about to run, and nothing to check the bytes against. `install.sh` and
  `install.ps1` were deleted on 2026-09-05; a contributor clones the repository
  and runs the installer out of it. This does not make the code trustworthy —
  see the next two bullets — it makes it *readable before it runs*, which is
  the most a project without code signing can honestly offer.
- **The model runs as the contributor's user account.** Nothing here sandboxes
  Ollama. A flaw in the model runtime is a flaw on their machine.
- **The protections live in the contributor's copy of the code.** They are only
  worth what "this is the real copy" is worth. There is no code signing.
- **The operator sees the output.** A contributor who would not want their
  operator reading what their machine produces should not join.
- **`join.py --secret` and `node.py --secret` still exist**, and a secret in an
  argument list is readable by every other user on the machine through `ps` and
  is written to shell history. Since 2026-09-05 they are no longer the only
  way in: both commands take `--ask-secret` (typed with the echo off) and
  `--secret-file PATH` (read through the same owner-only check that guards the
  identity file), and `--secret` prints a warning naming both exposures and
  both alternatives. It is kept rather than removed because setups already
  script it. The guided installer still accepts no code on the command line at
  all, and enrolment through any of these leaves the worker needing no secret
  afterwards.
- **Nothing reads a credential out of the environment any more.**
  `install.ps1` took the invitation code from `$env:SWARM_SECRET` and appended
  it to a child process's argument list — an environment variable is inherited
  by everything a process starts, so the code was exposed twice over. That
  script is gone, and a test asserts no worker module writes to the
  environment at all.
- **Transport is no longer the contributor's problem to assess**, which is the
  point: since 2026-09-05 the worker refuses plaintext HTTP to any non-loopback
  host, with no flag, environment variable, or configuration key that permits
  it. Previously a contributor was implicitly asked to evaluate whether an
  operator's overlay was really private, which is not a judgement they were in
  a position to make.

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

## 12a. Durable verification evidence is not correctness

Post-hoc verification evidence is durable as of Theme 3B-1. Three properties bound
what an attacker gains from it, and one limitation is worth stating plainly.

It cannot reach terminal state. Evidence is append-only, has no foreign key into
executions or attempts, and refuses update and delete at the database. An attacker
who could write arbitrary evidence still could not reclassify a completed
execution, unsettle an attempt, alter an artifact seal, or change contribution
points.

It cannot be laundered into a correctness claim. Deterministic-check and
agreement outcomes have disjoint vocabularies and separate scopes, structural
contract-floor validators cannot be recorded here at all, and nothing in the
schema or the read surface is named for correctness. Sampled agreement remains
what §11 and ADR 0012 say it is: two outputs of comparable shape, from a
coordinator that cannot tell which of them is wrong.

It cannot absorb a security event. Malformed or mismatched authority credentials
are refused as an attribution, so an authentication rejection can never be
recorded as evidence against a node. Requester cancellation, coordinator
shutdown, coordinator persistence failure, and verifier unavailability can only
record that the run did not happen, and are excluded from the sample count.

The limitation: an admitted worker that shapes its output to pass a deterministic
check, or coordinates with another admitted worker to agree, will produce evidence
that looks clean. That is the same limitation §6 already states about attempt
authority, and durability does not change it. Nothing consumes this evidence for
routing, so today the practical consequence is nil; it becomes a real question
only if a future assurance ladder acts on it, which is why ADR 0014 defers that
decision with an explicit list of what would have to be true first.

## 11a. What provenance and the ledger chain do not defend against

Two mechanisms landed in Theme 3C, and both are narrower than their names invite
people to assume.

**A provenance envelope binds identity to artifacts. It does not establish
correctness.** It says which enrolled worker produced these bytes, under which
descriptor, executor, and model, with which validators run and what they returned.
An admitted worker that returns plausible-looking garbage produces an envelope
that is entirely accurate about a bad output. §6's limitation is unchanged, and
provenance does not narrow it.

**The ledger chain is tamper-evident, not tamper-proof.** It detects accidental
corruption, a partial restore, and a casual edit - a single changed entry surfaces
at a known index. It does *not* defend against the party this threat model has
always identified as holding the most power: whoever runs the coordinator. An
operator with write access can edit an entry and recompute every downstream link,
and verification will report a clean chain. That is asserted by a test rather than
hoped for, so nobody later mistakes silence for protection.

Neither mechanism involves consensus, an external anchor, a transparency log, or a
third party attesting to anything, and no signature is implemented - only a
reserved slot. Nothing here makes this a verifiable-compute system, and the
temptation to describe it that way is the reason both statements appear in the
code, the operator command's own output, and every document that mentions them.

What they do buy: a recipient of an audit bundle can check offline that the bytes
they hold are the bytes that were sealed and see under whose identity they were
produced, and an operator can detect ledger corruption with one command. Both are
worth having. Neither is proof.

## 11b. What a contributor is and is not sending when tracing is on

Two separate things, and conflating them would be the kind of quiet expansion
ROADMAP section 2 exists to prevent.

**Propagation.** The coordinator sends a `traceparent` with a handout; a worker
echoes it on that task's later requests. The value was minted by the
coordinator. It describes nothing about the contributor's machine - not its
hostname, its hardware, its model, its load, or its errors - and it is the
coordinator reading its own incident. This is on whenever the *operator* enables
tracing, and a worker that never echoes anything is not penalised and loses no
work.

**Export.** A worker sending its own spans to the operator's collector is
telemetry leaving a machine somebody else owns. It is a separate setting,
`tracing_export`, off by default, and it is **never a condition of joining**. A
contributor who leaves it off takes work, settles, and earns credit exactly as
one who turns it on.

**What a span can carry at all** is an allowlist of identifiers, bounded enums,
and version strings, enforced by a keyword-only signature rather than by
scanning for forbidden text. There is no field for a prompt, a model output,
artifact contents, a schema, a credential, a session token, an attempt nonce, an
idempotency key, worker error text, or a node-supplied hostname - the last
because a worker-supplied hostname is finding F2 from Theme 4A.

**What this does not defend against.** An operator who enables export is
collecting data about the workers that opted in, and nothing here stops them
correlating it. The protection is that the contributor decides, not that the
operator is constrained afterwards. And tracing tells an operator *where* a job
failed, never *whether the output is right* - it is a diagnostic, and section 6's
limitation on worker honesty is unchanged by it.

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

- **`POST /nodes/register` is not rate-limited.** The coordinator has a per-IP
  limiter (`_check_rate_limit` in `server_state.py`, configured by
  `pitch_rate_max`/`pitch_rate_window`) but it is wired only into the pitch
  routes and `POST /v1/executions`; the registration route does not call it.
  Invitation codes can therefore be guessed as fast as the network allows, and
  the entropy of `node_secret` is the entire defence — which is why the
  generator produces 256 bits and `scripts/deploy_preflight.py` fails a value
  below 128. A refused bootstrap creates nothing durable, and on Path A the
  route is unreachable without tailnet membership. **A failed bootstrap is also
  not logged in a way an operator would notice:** `_check_node_auth` raises a
  plain 401 with no application log line, no event, and no counter; the only
  trace is the access log. Both halves are deliberately recorded here rather
  than fixed in a deployment change, because the existing limiter is
  pitch-scoped and coupling registration to it would alter worker-protocol
  behaviour — stock workers re-register automatically after a session expires.
- Shared static secrets still provide instance-wide bootstrap, pitch, or viewer
  authority within their separate roles.
- Enrollment uses a symmetric bearer credential, not public-key identity or a
  signature, and identity-file recovery is an operator responsibility.
- Node sessions are process-local incarnation credentials, not durable
  scheduling state.
- No built-in HTTPS. The coordinator still needs an external TLS terminator;
  what changed on 2026-09-05 is that the *worker* now refuses plaintext to any
  non-loopback host, so an operator cannot invite anyone until one is in place.
  A private authenticated overlay is no longer sufficient on its own — put a
  certificate on the overlay address. Two configurations are shipped in
  `deploy/`; §4a describes what each does and does not defend.
- The deployment configuration in `deploy/` and [DEPLOY.md](DEPLOY.md) **has
  not been reviewed by a security professional.** It is tested and it is
  written carefully; that is not the same thing.
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
before binding beyond localhost. Use TLS — the worker will not join without it, on an overlay or
otherwise — and configure access logs to redact share capabilities and never capture
idempotency, admission, enrollment, session, or attempt credentials; keep
keyless pitching off unless you explicitly acknowledge and accept its compute
risk; take and verify backups; protect worker identity files; review generated
code; rotate or revoke the narrowest affected authority after disclosure; and
treat shared prompts as disclosed to every worker that receives them.

Do not describe this implementation as trustless, permissionless,
confidential-compute, sandboxed, behaviorally verified, or safe for anonymous
stranger nodes.

Reporting a vulnerability: [SECURITY.md](../SECURITY.md).
