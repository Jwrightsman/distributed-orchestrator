# ADR 0007: Seal role-scoped artifact manifests at terminal finalization

- Status: Accepted for trusted-alpha RC1
- Date: 2026-08-23
- Decision scope: artifact delivery, sharing, and local integrity evidence

## Context

An execution root contains several kinds of files with different audiences:
requester deliverables, plans/reviews and other provenance, logs/transcripts,
raw candidate source, and internal metadata. Treating every file as one download
made it easy for an interface or share to expose audit material accidentally.

The earlier live manifest also rescanned the directory on every access. Its
per-file SHA-256 values detected a race during one read, but there was no stable
terminal baseline that a client could record and compare later. A file added or
modified after completion could silently become part of a later manifest. In an
ensemble, a losing candidate could remain beside the winner and filename
conventions alone were not an adequate publication boundary.

Trusted-alpha RC1 needs a bounded, explainable local integrity baseline. It does
not need distributed content addressing, a transparency log, remote notarization,
or a claim that generated content is safe or correct.

## Decision

Every manifest entry has one role:

- `deliverable` — requester-facing output;
- `provenance` — plan, review, builder, or revision record;
- `log` — log or transcript material;
- `candidate_source` — a raw candidate attempt; or
- `internal` — hidden metadata/manifest records.

Known paths are classified conservatively, and strategy code may attach explicit
role/source metadata. Unknown generated files remain deliverables so a legitimate
output is not silently discarded. Roles are publication policy, not content-
safety classifications.

Artifact roots have an explicit integrity mode:

- `active` — execution/finalization can still change the tree;
- `sealed` — terminal immutable SQLite entry baseline exists;
- `legacy_live` — historical registration that is rescanned on access; or
- `invalid` — a safe terminal baseline could not be established.

Before terminal ensemble/direct sealing, the execution applies its selected
winner subtree as the manifest prefix. Finalization holds the root active while
it performs one bounded, symlink-safe scan. It computes a canonical SHA-256 over
the sorted entries and their relative path, role, media type, size, content
digest, source candidate/unit, and entry timestamp. In one SQLite transaction it
replaces entry rows, stores the hash and seal timestamp, marks the root sealed,
and clears active state. Repeating seal returns the stored baseline. A failed
safe scan marks a non-sealed root invalid.

A sealed manifest is not refreshed from later directory contents. Every single-
file and ZIP retrieval still normalizes and confines the requested path, rejects
symlinks, resolves it beneath the registered root, and hashes the live bytes
against the sealed entry. Missing or changed content fails closed. Active and
legacy-live roots continue to rescan under the same bounds and cannot be labeled
sealed.

The manifest hash always identifies the complete sealed baseline. A role-
filtered manifest retains that hash rather than pretending to be a separately
signed filtered view.

## Delivery and sharing policy

Private `GET /v1/executions/{id}/artifacts` and `/download` default to
deliverables. Explicit `role=audit` and `/audit-download` expose the non-
deliverable audit set to the trusted viewer role. Deprecated `role=all` exists
only for compatibility.

Capability shares allow deliverables by default. Candidate source requires the
share's candidate-detail flag. Provenance, log, and internal roles are never
shareable, and candidate-scoped entries are excluded when there is no selected
winner. Share permissions cannot widen private role policy or cross an execution
root.

Normalized execution results expose primary deliverable names, deliverable and
audit manifest URLs, integrity mode, and sealed manifest hash. Clients must use
these fields rather than infer role or integrity from filenames.

## Consequences

- A terminal execution has one stable local baseline and winner scope.
- Later file drift is detected before either individual or archive delivery.
- Requester deliverables and operator audit material have separate defaults.
- Historical roots remain accessible without falsely upgrading their evidence.
- Final scan cost is bounded by configured file/per-file/aggregate quotas.
- Sealing failure can make an execution's artifact delivery unavailable rather
  than publishing an uncertain tree.
- Retention and verified backup/restore preserve or remove the local baseline as
  operator policy dictates; a restored seal has the same local meaning.

## Security and assurance boundary

The canonical hash is local integrity evidence. It is not a digital signature,
an independent timestamp, a transparency-log inclusion proof, model provenance,
a malware scan, generated-code isolation, behavioral validation, or a guarantee
that the requester intended the content. An administrator who can alter both
SQLite rows and files can create a new internally consistent baseline. Backups
preserve the recorded state but do not create a second trust authority.

Artifact role is also not assurance. A `deliverable` may be incorrect or unsafe;
`provenance` may be incomplete; and `candidate_source` may never have passed a
validator. Lifecycle, validation outcome, assurance level, integrity mode, and
post-hoc verification must remain separate UI dimensions.

## Rejected alternatives

### Keep rescanning every terminal root

Rejected because later additions and edits would silently redefine the claimed
terminal artifact set and there would be no stable baseline for clients.

### Publish every file to viewers and shares

Rejected because logs, prompts, candidate attempts, and internal metadata have a
different disclosure boundary from requester deliverables.

### Use filenames alone for policy

Rejected because filename conventions are incomplete and strategy/winner source
metadata is necessary for an honest scope. Names remain one classifier input,
not the public authorization decision.

### Treat SHA-256 as verified correctness or provenance

Rejected because a digest says that bytes match a baseline, not that the bytes
are safe, correct, independently witnessed, or produced by a claimed model.

### Adopt an external content-addressed/notarized store in RC1

Deferred. That requires retention, privacy, key ownership, migration, recovery,
and threat-model decisions beyond the bounded trusted-alpha release.
