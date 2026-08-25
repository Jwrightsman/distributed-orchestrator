# Sprint — Durable Node Enrollment (Theme 2A)

_Started August 25, 2026._

## Goal

Give every invited contributor an immutable, independently revocable durable
enrollment while retaining process-local sessions and server-issued attempt
authority.

```text
enrollment_id  durable contributor attribution
node_id        human-readable immutable label
session_id     one live process incarnation
attempt_id     one leased execution authority
```

Theme 1.1 merged in PR #57 before this branch began. The branch base is
`9979f681369fa69cfb35133f17e07ce3aac54abf` on `origin/master`.

## In scope

- Digest-only SQLite enrollment credentials with idempotent bootstrap and
  returning authentication.
- Independent durable revocation and atomic retry-safe rotation.
- Enrollment-bound process sessions and per-operation status enforcement.
- Nullable enrollment attribution for attempts, receipts, quarantine, and
  contribution records without backfilling historical labels.
- Enrollment-keyed accounting/verification identity with label metadata.
- Atomic private stock-worker identity files scoped by normalized coordinator.
- Explicit local `compat` versus trusted-alpha `required` configuration.
- Protected operator listing and local administration commands.
- Migration, restart, replay, revocation, rotation, permission, and secrecy
  tests plus protocol/ADR/runbook documentation.

## Explicitly out of scope

- Theme 2B typed resources, measured capability routing, or benchmark evidence.
- PKI, certificates, mTLS identity, signed result envelopes, or release signing.
- Hardware/model attestation, physical-machine identity, or Sybil resistance.
- Permissionless admission, DHT/NAT traversal, federation, marketplace/payment,
  global reputation, coordinator HA, or durable workflow resumption.

## Migration contract

- `node_enrollments` is created idempotently in the existing SQLite database.
- Attempt, accepted-receipt, quarantine, and contribution tables gain nullable
  enrollment fields additively.
- Historical node-label-only rows remain readable with `enrollment_id=null`.
- A previously enrolled or revoked label is never reused by another enrollment.
- Trusted alpha rejects legacy-only configuration/workers; local compatibility
  sessions are explicitly unenrolled and cannot inherit durable history.

## Security contract

- The stock worker generates and persists its high-entropy credential before
  bootstrap. The coordinator stores only a domain-separated digest.
- Credentials, digests, static secrets, session tokens, and attempt nonces are
  excluded from logs/events/operator listings; plaintext rotation goes only to
  a requested private identity file.
- Trusted alpha requires TLS or an operator-declared private authenticated
  overlay.
- Bearer enrollment is stable attribution and incident containment for an
  invited alpha, not machine identity, attestation, correctness reputation, or
  permissionless trust.

## Verification record

Final Theme 2A verification on August 25, 2026:

- `python -m pytest -q tests/test_node_enrollments.py tests/test_node_sessions.py tests/test_worker_identity.py tests/test_node_enrollment_admin.py tests/test_attempt_authority.py tests/test_attempt_dispatch.py tests/test_ledger.py tests/test_verification.py tests/test_verification_wiring.py tests/test_event_privacy.py tests/test_node_submission.py tests/test_preflight.py`
  — PASS, 170 passed and 1 skipped.
- `python -m pytest -q` — PASS, 974 passed and 3 skipped.
- `python -m ruff check .` — PASS, all checks passed.
- `python -c "import server"` — PASS.
- `python scripts/trusted_alpha_harness.py` — PASS, 57 focused checks and the
  live two-worker harness passed.
- `python scripts/restart_recovery.py` — PASS, 17/17 checks passed.
- `docker compose config` — PASS.

The first two post-refactor harness invocations each found one stale
three-argument call to the now registration-bound worker settlement helper.
Both callers were corrected; the successful harness and full-suite results
above are from the corrected final tree.

## Review boundary

Do not auto-merge. Review schema migration, ambiguous-commit recovery,
revocation/rotation enforcement, exact accepted-result replay, identity-file
permissions, contribution/verification grouping, compatibility behavior, and
secret-negative tests before merge.
