# Security policy

## Reporting a vulnerability

**Email <wrightsmanjett@gmail.com>.** Put "SECURITY" in the subject line.

Do not open a public issue for anything exploitable. Public issues are the right
place for everything else, including things that merely look alarming — if you
are unsure which you have, email it and it can always be moved into the open.

Useful things to include, none of them required:

- What the problem is, and what an attacker gets out of it
- How to reproduce it — a command, a request, a short script
- Which version you tested (`git rev-parse HEAD`, or the `build` value from
  `/status.json` on a running orchestrator)
- Whether you think it is already covered by
  [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)

## What to expect back

This is a project run by **one person**, not a company with a security team.
Being honest about that up front is more useful than a response-time promise
that gets broken:

| | |
| --- | --- |
| **First reply** | Within about 7 days. If you have not heard back in 14, email again — assume it got lost, not ignored |
| **Assessment** | A plain-language answer: whether it reproduces, whether it is already a known limitation, and what happens next |
| **Fix** | For something exploitable in a normal deployment, as fast as one person can. For a hardening item, it goes on the roadmap with the reasoning written down |
| **Credit** | Your name in the commit and the release note, if you want it. Say if you would rather stay anonymous |
| **Disclosure** | Coordinated. Roughly 90 days, or as soon as a fix ships, whichever is first. If you have a deadline, say so and it will be respected |

There is **no bug bounty** and no money involved. Nothing here is a legal
agreement.

## Before you report: much of it is already written down

[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) describes what this system does
and does not defend against **today**. A lot of what looks like a vulnerability
is a documented Phase 0 limitation, and saying so is not a brush-off — the
whole point of writing the threat model down was to make the honesty checkable.

Known and documented, so probably not news:

- **`node_secret` is shared bootstrap admission, not per-node identity.** Each
  enrolled worker has a separate digest-only bearer credential and independent
  revocation, but an admitted worker can still return arbitrary model output.
  Enrollment is not physical-machine identity, attestation, or Sybil defense.
- **Local compatibility mode deliberately fails open.** When `viewer_key` is
  empty, `/history`, `/events`, `/gallery`, `/ledger`, and other private routes
  remain readable for loopback development. Trusted-alpha preflight requires a
  separate viewer authority and durable enrollment; do not expose local mode.
- **Worker nodes see the text of the tasks they are given.** Inherent — you
  cannot ask a machine to work on a prompt without showing it the prompt.
- **Generated code is not sandboxed** and must not be executed unreviewed.
- **No HTTPS out of the box.** Bearer admission, enrollment, session, pitch,
  viewer, and share credentials require TLS or an authenticated private overlay.
- **Credits are contribution points, not currency.** No token, no wallet, no
  monetary value, so no credit bug costs anyone money.
- **Capability evidence is diagnostic, not trust or correctness.** It records
  scoped coordinator-observed operational outcomes. Sampled agreement compares
  output shape only. It does not attest a descriptor, detect plausible bad
  output, establish reputation, or change production routing.

**Still worth reporting even though they are listed:** a way to make any of
these *worse* than documented — reading data the threat model says is private,
getting a node to execute something, getting credit settled for work not done,
bypassing `pitch_key` on a write endpoint, causing remote code execution on the
orchestrator, or extracting any admission, enrollment, session, pitch, viewer,
attempt, or share credential from outside the box. Also report any path that
lets evidence delay, rank, reorder, or exclude production work, attributes a
caller/coordinator fault to a worker, mutates append-only evidence, or exposes
raw evidence outside viewer protection.

**Definitely report** anything the threat model does not mention at all. A gap
in that document is itself the finding.

## Scope

Versioned worker capability descriptors are self-reported claims. Their
canonical SHA-256 hashes identify the exact claim bound to a session and
attempt; they do not attest hardware, model bytes, isolation, operator identity,
performance, trust, or correctness. An admitted worker can submit a valid but
false descriptor. Typed requirements reduce accidental misrouting, not this
malicious-worker risk.

Full normalized descriptors may reveal private hardware/model inventory. They
are available only through viewer-protected operator diagnostics; public health
and status surfaces must remain descriptor-free. Reports should include a
descriptor hash and stable exclusion/error code when sufficient, not the full
claim, hostname, override file, enrollment credential, or session token.

Capability observations are scoped by enrollment, descriptor, executor,
selected model, task class, and evidence role. Missing or inconsistent bindings
are excluded, and a descriptor/model/task-class/role change starts a cold scope.
Only server-owned typed outcomes are attributable: accepted settlement facts,
contract-floor outcome, sampled shape agreement, and worker terminal causes
`lease_expired` or `node_stale`. Caller cancellation, execution deadline,
payload/stream limits, receipt binding, enrollment reclaim, session replacement,
coordinator restart, supersession, unknown causes, and free-form error text are
not worker evidence.
Sampled comparisons require a durable exact primary-attempt binding.

`capability_evidence_mode` permits only `off` or `shadow` and defaults to `off`.
Shadow evaluation runs after real assignment over already hard-eligible
candidates, freezes their exact descriptor/model scopes before background work,
and cannot affect queue order, eligibility, settlement, contribution credit, or
the circuit breaker. Below the configured sample minimum, evidence is
insufficient rather than adverse. Exact observation replay is idempotent;
conflicting immutable content is rejected. Missing-only reconciliation and
content-free contract-floor projection receipts prevent completed rows from
starving bounded startup repair.

Viewer-protected `GET /v1/operator/capability-evidence` returns aggregates, not
raw observations. Evidence rows contain no prompt, output body, worker-error
text, free-form reason, credential, nonce, session secret, or arbitrary
telemetry. They still reveal scoped operational inventory and are stored in
`events.db`, so database backups and exports must be protected. Contribution
points are separate from evidence, assurance, correctness, and routing.

**In scope:** this repository, and the orchestrator, node, MCP server and
installers it contains.

**Out of scope:** the model's *output* — that a 4B model writes wrong or
insecure code is a measured quality property (about 57% of eval tasks come back
runnable, and that number is published precisely so nobody is surprised), not a
vulnerability. Also out of scope: third-party dependencies, which should go to
their own projects, and Ollama itself.

**Please do not** test against the public orchestrator or anyone else's node
without asking first. Run your own — `docs/DEPLOY.md` Path 1 takes a few
minutes on a laptop — and attack that instead. Testing against a stranger's
hardware is the one thing that would make this project's contributors
regret opting in.

## Supported versions

`master` only. This is Phase 0: there are no release branches and no backports.
Fixes land on `master` and deployments update with a `git pull` and rebuild —
`python scripts/verify_deploy.py <url>` will tell you whether a given
orchestrator is actually running the code you think it is.
