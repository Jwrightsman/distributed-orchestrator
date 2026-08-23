# ADR 0005: Server-issued node registration sessions

- Status: Accepted for trusted-alpha RC1
- Date: 2026-08-23

## Context

`node_secret` is one instance-wide network-admission credential. Every invited
worker knows the same value, so it cannot distinguish two admitted machines or
prevent them from accidentally choosing the same `node_id`. Previously, a new
registration silently replaced the process-local record for an existing label.
Polling also recreated an evicted label with placeholder metadata. Together,
those behaviors allowed session collisions and made attempt ownership unclear.

Trusted alpha does not require public-key identity. It does require a live
coordinator to distinguish concurrent registrations and bind worker operations
to the registration that received an attempt.

## Decision

Successful `POST /nodes/register` returns:

```json
{
  "node_id": "worker-a",
  "session_id": "non-secret-session-id",
  "session_token": "plaintext-returned-only-to-the-worker",
  "session_expires_at": "2026-08-24T12:00:00+00:00"
}
```

The token is random and high entropy. The coordinator retains only its SHA-256
digest and compares digests in constant time. The worker keeps the plaintext in
memory and sends it as `X-Node-Session` on:

- `GET /tasks/next`;
- `POST /tasks/{task_id}/tokens` (and the compatibility `/stream` alias);
- `POST /tasks/{task_id}/result`;
- `POST /nodes/{node_id}/heartbeat`;
- `POST /nodes/{node_id}/drain`.

`X-Node-Secret` remains independently required when node admission is enabled.
The shared secret answers “may this client join?”; the session answers “which
current registration is making this worker request?”

Sessions are process-local and have a 24-hour absolute expiry. Coordinator
restart invalidates every session. A machine that wakes from sleep, reconnects,
or receives a machine-readable session rejection automatically registers again.
No token is written to worker configuration or server persistence.

## Registration and reclaim rules

`node_id` is stripped, case-folded, limited to 64 characters, and restricted to
ASCII letters, digits, `.`, `_`, `:`, and `-`. Other registration fields and the
capability list have explicit length and count limits.

- Presenting the exact live `X-Node-Session` during registration is idempotent.
  It retains the same `session_id` and session counters. The coordinator can
  echo the presented token without storing its plaintext.
- A different claimant for a recently seen, unexpired `node_id` receives HTTP
  `409` with `detail.code=node_id_in_use`.
- A claim becomes replaceable after its absolute expiry or the documented
  90-second node-staleness interval.
- Replacement invalidates the old token and reclaims attempts issued to the old
  `session_id`. The replacement session cannot stream or settle those attempts.
- An unregistered or session-less poll receives HTTP `401` with
  `action=register_again`; polling never creates a placeholder admitted node.

Every task handout records the non-secret `assigned_session_id` in its durable
attempt. Session validation is additional to, not a replacement for, the
authoritative attempt ID, nonce digest, node ID, execution ID, unit ID, unit
kind, contract version, and lease checks.

## Statistics

Connected-node metadata distinguishes:

- `session_tasks_completed` and `session_contribution_points`, which reset when
  a replacement session starts;
- `lifetime_tasks_completed` and `lifetime_contribution_points`, which are read
  from durable contribution rows;
- `session_started_at` and `last_seen`.

Contribution points record accepted compute contribution. They do not assert
that a candidate was selected, that a final output was validated, or that the
work was correct. Attempt acceptance, candidate acceptance, candidate selection,
and final validation remain separate lifecycle facts.

## Consequences and limits

This prevents silent active-label replacement and stale-session polling,
streaming, or settlement. It also gives laptops an automatic recovery path after
sleep or coordinator restart.

It does **not** create long-term cryptographic node identity. Any holder of
`node_secret` may still register an available label, and the coordinator cannot
prove that the same physical machine returned after a session expired. Session
tokens are bearer credentials and must not be logged or shared. Workers receive
their assigned prompts as readable text. Public-key identity, attestation, and
anonymous or permissionless participation remain out of scope.
