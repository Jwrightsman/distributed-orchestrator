# ADR 0017 — Artifact provenance is a binding of identity, not a claim of correctness

**Status:** Accepted (2026-09-03, Theme 3C)

**Context:** ROADMAP §5's transactional-ledger item asks for entries that carry
enough to audit, and notes hash-chaining as the intermediate step between a JSON
file and a distributed ledger — with tamper-evidence named as "the property that
actually matters", and decentralized consensus named as "a much more expensive
thing that is usually mistaken for it".

Most of the facts needed to say how an artifact was produced already existed and
were scattered: enrollment ID and node label on attempts and receipts (Theme 2A),
descriptor version and hash bound at assignment (Theme 2B), executor and model
identity (Theme 2B), validator identity and outcome (Theme 3B-1), and sealed
per-file manifest hashes (ADR 0007). Nothing bound them into one record a
recipient could check.

## The line this ADR draws, and keeps drawing

**A provenance envelope records how an artifact set was produced and under whose
identity. It establishes nothing about whether the output is correct, useful, or
honest.** SLSA and in-toto draw exactly this line; so does this.

**The ledger hash chain makes the contribution record tamper-evident, not
tamper-proof.** An operator with write access to the database can rewrite every
entry *and* every link, and verification will then report a clean chain. There is
no consensus, no external anchor, and no third party attesting to anything.

Both of those sentences appear in the code, in the operator command's own output,
in the envelope record itself, and in every document this PR touches. That is
deliberate: this is precisely the feature that gets marketed as "verifiable
compute", and an envelope travels further than its documentation does. Nothing
here may be described as verified, trustless, tamper-proof, or proof of correct
execution.

## The envelope

### Terminal state is not mutated

ADR 0009 makes terminal execution state monotonic and never reclassified. The
envelope is created when the artifact manifest seals — which is inside the
terminal path — so the constraint is the same one Theme 3B-1 worked under: the
envelope *references* the execution, attempt, and receipt; it never lives on
them, has no foreign key that could block or cascade into them, and is
append-only at the database via triggers.

It is also written **synchronously and best-effort**. The first implementation
awaited a thread, and that await was one more point at which the terminal task
could be cancelled between sealing and recording — which turned a completed
execution into an interrupted one. Provenance changing an execution's terminal
classification is exactly the failure this constraint exists to prevent, so the
await was removed. An execution whose envelope could not be written is still
completed, still published, and still settled; the envelope is simply absent,
which is a state the offline checker recognises.

### Contents

Envelope version; execution, unit, attempt and receipt IDs; the producing
enrollment ID, node label as it was at settlement, and identity class; capability
descriptor version and hash; executor kind and version; worker protocol version;
model provider, name, digest and variant; the ordered validators that ran with
their outcomes; the sealed manifest digest and per-file hashes; the settlement
reference; and a creation timestamp.

An execution may have several accepted receipts — an ensemble has one per
candidate — so the envelope carries a `producers` array with one fully-scoped
entry each, and populates the singular `attempt_id` / `receipt_id` fields only
when there is exactly one. More than one is recorded as
`unknown_facts: ["single_producer"]` rather than by picking a winner.

The node label is recorded so a reader can *recognise* a machine, never to
authenticate one. A label is display metadata and is not a trust key anywhere in
this system (ADR 0016, seam 2).

### Deterministic content

Canonical JSON — sorted keys, compact separators, UTF-8, no NaN — hashed with
SHA-256 under a domain separator. The same production facts always yield the same
envelope digest, and key order or whitespace cannot change it.

The digest covers the production facts and **excludes the reserved signature
slot**, because a signature is over the digest and including it would make the
digest depend on itself.

### Absence is explicit

A missing model digest, a legacy session with no enrollment, an execution that
produced no distributed attempt at all — each is recorded as `null` and named in
`unknown_facts`. Nothing is inferred and nothing is backfilled with a guess. In
particular, a legacy producer's enrollment is never reconstructed from its node
label, and an execution that ran locally does not get the coordinator's own
identity written in as if it had produced the work.

An execution that predates envelopes simply has none. That is a recognisable
state, not an error, and history is not rewritten to manufacture one.

### The reserved signature slot

`signature` and `signature_algorithm` exist, are always `NULL`, and are carried
in the exported envelope as `null`. Their semantics are fixed now so that adding
signing later is not a schema break for anyone already reading these bundles: a
signature, when it exists, will be a detached signature **over `envelope_digest`**
and over nothing else.

Signing is deferred, and not because it is hard to compute. It is deferred
because a signature is only worth what the key management behind it is worth, and
this project has no key custody story, no rotation story, and no revocation
story for a provenance key. Shipping a signature without those would produce
something that looks like a guarantee and is not one — the exact failure mode
this ADR exists to avoid. Sigstore, transparency logs, in-toto layouts and SLSA
attestation formats are all out for the same reason.

**Trigger to reopen:** a consumer exists who would actually check a signature,
and there is a decided answer to where the key lives and what happens when it is
lost.

### The bundle, and the offline check

An audit bundle carries `mycelium-provenance.json`. `check_envelope_against_files`
recomputes the envelope digest and every per-file hash against extracted files,
with no coordinator, no network, and no credential. That offline check is the
user-facing point of the envelope: a recipient can confirm the bytes they have
are the bytes that were sealed.

The addition is **additive**: a reader that does not know about the file still
extracts exactly the artifacts it always did, so the bundle format is not broken
and nothing is version-bumped.

What the offline check establishes: these bytes are the bytes recorded at seal
time, produced under this identity. What it does not establish: that the bytes
are correct, that the identity is honest, or that the coordinator recorded
truthfully. A recipient who concludes otherwise has been misled by something
other than a field name — the envelope says so in its own `establishes` field.

## The ledger chain

### How it works, and its genesis

Each entry carries `entry_index`, `previous_digest`, and `entry_digest`, where
the digest covers an explicit list of content columns plus the index and the
predecessor's digest. The first chained entry links to a fixed genesis constant.

Entries written before the chain existed keep `NULL` in all three columns and are
counted separately as `genesis_unchained_entries`. **History is not rewritten to
fabricate links it never had.** Verification reports that count so an operator
knows how much of the ledger the chain actually covers.

The digested column list is explicit rather than "every column", so a future
additive column is a deliberate decision rather than a silent invalidation of
every existing link.

### Atomicity and restart

The link is computed and written **inside the caller's transaction** — for a
settlement, the same `BEGIN IMMEDIATE` that writes the receipt and the
contribution. Nothing about settlement atomicity was relaxed to accommodate the
chain; the chain rides the transaction that already existed, so
`credit_matches_acceptance_exactly` holds unchanged.

The insert is `INSERT OR IGNORE` on a primary key of `contribution_id` with a
unique `attempt_id`. An ignored insert consumes no index, so Theme 1.1's lesson
holds: a replayed settlement, or an ambiguous commit whose caller retries,
resolves to the one existing entry — never a second one, and never a gap.
Concurrent settlements serialize on `BEGIN IMMEDIATE` and each reads a distinct
head.

There is deliberately **no unique constraint on `entry_index`**. With
`INSERT OR IGNORE`, a uniqueness violation would be silently swallowed and the
contribution would vanish — a settlement with no credit, which is worse than any
chain anomaly. Instead, a duplicate or missing index is *detected* by
verification and reported as a gap. Tamper-evidence is a detection property; this
is consistent with that.

### Linear, not Merkle

A Merkle tree buys efficient inclusion proofs: proving one entry is in the log to
a third party without handing over the whole log. Nobody needs that. There is no
third party, the whole log is small, and an operator verifying their own ledger
already has all of it.

**Upgrade path if that changes:** if inclusion proofs are ever needed — a
federation checking another instance's claims, or an external auditor — a Merkle
tree over the same canonical entry content is the replacement, and the existing
linear digests would become leaf hashes. It was not built now because a tree is
strictly more machinery for a capability that has no consumer.

### What a passing verification means

**It means:** no entry was changed without also recomputing every link after it.
That catches disk corruption, a partial restore, a truncated file, and a casual
edit — all of which are real and none of which are exotic.

**It does not mean:** that the recorded work happened, that it was correct, that
anyone is owed anything, or that the operator of this coordinator has not
rewritten the whole ledger. A party with write access who edits an entry *and*
recomputes every downstream link produces a chain that verifies clean. That is
**undetectable by this mechanism**, it is asserted as such by
`test_a_fully_recomputed_chain_is_not_detectable`, and if that test ever starts
failing, this mechanism has gained a property it does not claim and this ADR
needs updating.

Defending against the coordinator's own operator needs an external anchor or a
third party, which is a different and much more expensive thing. The permanent
constraint in ROADMAP §2 stands: no token, no blockchain, and no pretending that
tamper-evidence is consensus.

## Consequences

* A recipient of an audit bundle can check offline that the artifacts they hold
  are the ones that were sealed, and see under whose identity they were produced.
* An operator can detect ledger corruption with one command, and is told plainly
  what a clean result does and does not establish.
* Two more append-only tables' worth of durable state, both additive, both
  initializing idempotently on fresh and existing databases.
* The chain adds a `SELECT` per contribution insert. That is small, but it is
  inside the settlement transaction, and it measurably changed which thin
  coverage classes the adversarial campaign reaches — see
  `docs/adversarial-campaign.md`.
* `provenance_envelope_created` is counted by the campaign but not floored: the
  seal path contains an `await` that a background task does not survive under
  `TestClient`, so an artifact-producing execution lands `interrupted`. Making
  the campaign strategy produce artifacts was tried and measured, and it degraded
  the whole campaign to exercising interrupted executions. Envelope creation at
  seal time is covered directly instead.
