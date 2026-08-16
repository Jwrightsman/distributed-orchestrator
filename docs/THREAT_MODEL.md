# Threat model

_What is true of this system today — August 2026, Phase 0. Not what it should
be, not what it will be. Where a protection is missing, this says so._

A public protocol without a written threat model accumulates incompatible
assumptions: one person believes nodes are untrusted, another writes code that
trusts them, and nobody notices until it matters. This file exists so the
assumptions are legible.

**The one-sentence version:** Mycelium today is a **trusted-network** system.
It assumes everyone holding the shared secret is non-hostile, and it is not
safe to run as an open, permissionless network. Everything below elaborates
that sentence.

---

## 1. What is being protected

| Asset | Where it lives | Worst realistic outcome today |
| --- | --- | --- |
| Task text (the prompts people submit) | Orchestrator memory, `output/`, and **every worker that gets a subtask** | Disclosure to node operators and to anyone who can reach the HTTP port |
| Generated output | `output/`, served over HTTP | Disclosure; and it is **unreviewed code** that someone may run |
| The contribution ledger | `data/ledger.json` on the orchestrator | Mis-credit — points misattributed between contributors |
| `node_secret` / `pitch_key` | `data/config.json` on the orchestrator | Full node access / full submit access to whoever obtains one |
| Contributor hardware | Volunteers' own machines | CPU and disk consumed; a bad model pull; nothing executes on them (see §4) |
| Orchestrator availability | One VM, no failover | The network stops until it is restarted |

There is **no user data, no accounts, no payment information, and no personal
data** in the system. That is a deliberate scope limit and it removes a large
class of risk.

---

## 2. Trust boundaries

```
   Requester ──HTTP──▶ ORCHESTRATOR ──HTTP──▶ Worker node
   (pitch_key)         (trusted: runs        (node_secret)
                        planner + reviewer,   ── runs a local model
                        holds ledger,         ── returns TEXT only
                        holds both secrets)   ── never executes what it gets

                              │
                              └──▶ Anyone who can reach the port
                                   (no credential required — see §5)
```

**Inside the boundary:** the orchestrator process and the machine it runs on.
Everything else is outside it, including every worker node.

---

## 3. What is enforced today

| Control | What it actually does | Where |
| --- | --- | --- |
| `node_secret` | A single shared password in an `X-Node-Secret` header, checked on node registration, task poll, result submission and token stream | `server_state._check_node_auth` |
| `pitch_key` | A single shared password required on `/pitch`, `/pitch/async`, `/pitch/distributed` | `server_state._check_pitch_key` |
| Attempt binding | Every handout mints an `attempt_id` + nonce; settlement requires the assigned node, matching nonce, unexpired lease (900 s), unsettled attempt. Idempotent on retry | `server_state`, `routes_nodes` |
| Rate limiting | Pitches per IP per window (`pitch_rate_max`, default 5/min) | `routes_pitch` |
| Disk cap | `output_max_mb` (default 500) with oldest-run pruning | `server_state._prune_output_dir` |
| Circuit breaker | A node failing 3× is benched 60 s | `routes_nodes` |
| Sampled verification | Duplicate a fraction of subtasks to a second node and compare shape; reputation feeds routing order | `verification.py` — **off by default** (`verify_rate: 0`), and needs ≥2 nodes |
| Generic 500s | Unhandled errors return `{"detail": "internal server error"}` rather than the exception text | `server.py` |

**Both secrets default to empty, which disables both checks.** That is correct
for a laptop on your own LAN and wrong for anything reachable from the
internet. `docs/DEPLOY.md` Path 3 requires both.

---

## 4. What a malicious worker node can do

A node is any machine holding `node_secret`. Assume it is fully hostile.

**It can:**

- **Read the text of every subtask it is given.** This is inherent — you cannot
  ask a machine to work on a prompt without showing it the prompt. Do not pitch
  anything confidential to a network whose operators you do not trust.
- **Return anything at all** as a result. The circuit breaker catches a node
  that *fails*; nothing catches a node that returns plausible, wrong output.
  Sampled verification raises the cost of that, and it is off by default.
- **Claim any `node_id` it likes at registration**, including one that looks
  like somebody else's, and so appear on the dashboard and standings under a
  chosen name.
- **Refuse work, or hold it until the lease expires**, forcing reassignment and
  wasting network capacity.
- **Register many identities** (Sybil) — nothing limits identities per secret.
- **See task text for subtasks it did not win**, indirectly, because the read
  endpoints in §5 are open.

**It cannot:**

- **Execute code on the requester's machine.** A node returns text. Whether
  anything is ever run is the requester's decision on the requester's hardware.
- **Take another node's credit.** This is what attempt binding fixed: a result
  is settled only for the node the attempt was issued to, with the matching
  nonce, before the lease expires, once. Submitting under another node's id is
  rejected 403 and raises a `result_rejected` event.
- **Get paid twice for one attempt.** Settlement is idempotent; a retry replays
  the original outcome.
- **Read the orchestrator's secrets, ledger file, or filesystem.**

**What attempt binding does NOT do, stated plainly:** it does not establish
*identity*. `node_secret` is network *admission* — everyone presenting it
presents the same credential. Anyone holding the shared secret can still join
under a name of their choosing; binding only stops an admitted node stealing a
*different* node's credit for a specific attempt. Per-node keypairs, signed
result envelopes, revocation and rotation are the real answer and are deferred
— see [ROADMAP.md](../ROADMAP.md) §5.

---

## 5. What an unauthenticated stranger can do

Anyone who can reach the port, with no credential at all:

- **Read every past task's text and output** — `/history`, `/history/{id}`,
  `/gallery`, `/run/{id}` (and the `/share/{id}` redirect kept for old links),
  `/history/{id}/download`.
- **Read the live event stream** — `/events`, `/ws/events` — including task
  text as it runs.
- **Read the ledger and standings** — `/ledger`, `/standings`.
- **Read operational metrics** — `/metrics`, `/status.json`, `/health`.

**None of these require `pitch_key` or `node_secret`.** `pitch_key` gates
*submitting* work; it does not gate *reading* anything. This is the single
most under-appreciated fact about the current deployment, and the practical
consequence is simple: **treat everything you pitch, and everything the swarm
produces, as public.**

`/status.json` is deliberately public and deliberately narrow — counts, uptime,
model, no task text — so that a stranger can verify the network is real without
an invite. The rest of the list above is not deliberate design so much as
Phase 0 not having grown access control yet.

---

## 6. Generated code is not sandboxed

The pipeline writes runnable files to `output/`. It checks that Python parses
and that HTML loads in a headless browser. **It does not sandbox anything, and
those checks are not a security boundary** — parsing proves syntax, not intent.

- **On the orchestrator**, the eval harness executes generated Python in a
  subprocess with a scrubbed environment. That is a speed bump, not a jail: no
  container, no seccomp, no filesystem or network restriction.
- **On worker nodes**, nothing generated is executed at all. Nodes run a model
  and return text.
- **On your machine**, nothing runs unless you run it.

**Do not execute generated code you have not read.** The README says this, the
docs say this, and it is the single most likely way this project hurts someone.
Real sandboxing — disposable containers or microVMs, no network by default,
read-only base, resource caps — is in [ROADMAP.md](../ROADMAP.md) §5 and is a
prerequisite for accepting code from untrusted nodes.

---

## 7. The ledger is contribution points, not currency

`ledger.json` is an append-only JSON file recording who contributed what.
Credits are **contribution points denominated in work**. They are not a token,
not tradable, not redeemable, and there is no monetary value attached to them
anywhere in the system. There is no wallet, no transfer, and no settlement
against anything of value.

Consequences that matter for a threat model:

- The economic incentive to attack the ledger is currently **zero**, which is
  the main reason its integrity properties can be weak without being urgent.
- Ledger entries are recorded on a non-empty, verified-settlement result. They
  do not carry a verifier version, evidence, or a signature, and there are no
  provisional/disputed/reversed states.
- The file is not tamper-evident. Anyone with write access to the orchestrator's
  disk can rewrite history undetectably. Hash-chaining is the cheap fix and is
  in [ROADMAP.md](../ROADMAP.md) §5.
- **No credit-related bug can cost anyone money, because credits are not
  money.** That will stop being true the moment they buy anything, and the
  hardening in §5 of the roadmap is what has to land first.

---

## 8. Known weaknesses, stated rather than implied

- **Shared-secret admission.** One credential for all nodes. Rotating it means
  restarting every node. A leaked secret is a full compromise of node access
  with no way to revoke one holder.
- **The secret comparison is not constant-time** (`provided != secret`).
  Remote timing attacks on a string compare across a network are impractical in
  practice; it is listed because it is true, not because it is urgent.
- **No transport security.** No HTTPS out of the box. Task text, results and
  both secrets travel in clear text over any network between the parties. On
  the public internet, assume they are readable. HTTPS via a domain and Caddy
  is [ROADMAP.md](../ROADMAP.md) §4.
- **No per-node identity, no revocation, no signed results.** §4 above.
- **Process-local scheduler state.** Nodes, queues, in-flight assignments,
  reputation and breaker state live in memory. A restart loses them. Jobs and
  event history survive in SQLite; the rest does not.
- **Unbounded memory growth.** ~1.25 MB per pitch, measured, source not found.
  Not a denial-of-service risk at ~800 pitches per GB, but real for a
  long-lived public orchestrator.
- **No Sybil resistance.** Nothing limits how many identities one secret-holder
  registers.
- **Verification is a spot check, not a proof.** Duplicate-and-compare measures
  output *shape*, and when two results disagree the coordinator cannot tell
  which is wrong — it lowers both reputations and lets the pattern emerge over
  samples.
- **One orchestrator, no failover.** Availability depends on one VM.

---

## 9. The trusted-network assumption, plainly

**Everything above adds up to one assumption: every party holding a credential
is non-hostile.** The controls that exist raise the cost of casual misbehaviour
and make accidents visible. They do not withstand a determined adversary, and
they are not designed to.

That is a reasonable posture for what this is — a Phase 0 system run by one
person, with a handful of invited testers, no user data and no money. It is not
a reasonable posture for a public permissionless network, and the project does
not claim otherwise.

**Run it this way:** on your own hardware, on a network you control, or with
people you have vetted, with both secrets set the moment it leaves your LAN,
and with everything it produces treated as public.

---

## 10. What would have to change for a permissionless network

Not duplicated here, because a second copy of a list is a list that drifts.
[ROADMAP.md](../ROADMAP.md) §5 has it in full, ordered by severity: per-node
cryptographic identity, server-issued expiring leases with signed result
envelopes, a durable attempt-based scheduler, layered verification
(deterministic checks → canaries → redundancy → tie-breaking → semantic judging
→ reputation), a transactional ledger with hash-chaining, real sandboxing,
confidentiality classes, and adversarial protocol tests. §5 also covers the
verification endgame — trusted execution environments, then zero-knowledge
proofs of inference — which is what genuinely untrusted compute would
eventually require.

The short version: **identity first, because everything else depends on it.**

---

## 11. Non-goals

Stated so nobody builds toward them by accident:

- **Confidential computing.** Task text is visible to node operators by design.
- **Anonymity.** No attempt is made to hide who pitched or who built what.
- **Byzantine fault tolerance.** There is no consensus mechanism and no plan
  for one; see §7 of the roadmap on why a coordinator, not a chain.
- **Protecting the network from its own operator.** Whoever runs the
  orchestrator holds the queue, the ledger and the routing. The check on that
  is the fork right, not a technical control.

---

_Reporting a vulnerability: [SECURITY.md](../SECURITY.md)._
