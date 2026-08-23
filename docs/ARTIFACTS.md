# Artifact manifests, downloads, and retention

The canonical artifact API replaces public filesystem paths with authenticated,
execution-scoped delivery. DAG output under `output/` and ensemble output under
`execution_artifacts/` use one durable registry and one path-safety policy.

This document describes delivery integrity, not content safety. Generated files
are not sandboxed, scanned for malware, or guaranteed behaviorally correct.

## Public models

`ArtifactManifestV1` contains:

```json
{
  "protocol_version": "1",
  "execution_id": "...",
  "created_at": "2026-08-21T12:00:00+00:00",
  "file_count": 1,
  "aggregate_size_bytes": 1234,
  "integrity_mode": "sealed",
  "manifest_hash": "64-lowercase-hex-characters",
  "sealed_at": "2026-08-21T12:00:05+00:00",
  "entries": [
    {
      "relative_path": "code/index.html",
      "media_type": "text/html",
      "size_bytes": 1234,
      "sha256": "64-lowercase-hex-characters",
      "role": "deliverable",
      "source_candidate_id": null,
      "source_execution_unit_id": null,
      "created_at": "2026-08-21T12:00:00+00:00"
    }
  ]
}
```

The internal artifact root is never part of either public model. Source fields
are optional; ensemble entries beneath `candidate_N/` are associated with
`candidate-N` automatically. Entry `role` is one of `deliverable`,
`provenance`, `log`, `candidate_source`, or `internal`.

`integrity_mode` is `active`, `sealed`, `legacy_live`, or `invalid`. A sealed
manifest has `manifest_hash` and `sealed_at`; the hash covers the canonical
sorted entry baseline, including role, media type, size, content hash, source
metadata, and entry timestamp. A role-filtered response retains the hash of the
complete sealed baseline; it is not a new signature over the filtered view.

## Authenticated APIs

All canonical execution artifact routes require viewer authorization when
`viewer_key` is configured:

| Method and path | Response |
| --- | --- |
| `GET /v1/executions/{id}/artifacts` | deliverable-only manifest (default) |
| `GET /v1/executions/{id}/artifacts?role=audit` | provenance, logs, candidate source, and internal manifest entries |
| `GET /v1/executions/{id}/artifacts?role=all` | deprecated complete compatibility view |
| `POST /v1/executions/{id}/artifacts/seal` | return the already committed sealed baseline; never publish an active/legacy baseline as newly terminal |
| `GET /v1/executions/{id}/artifacts/{relative_path}` | one streamed file |
| `GET /v1/executions/{id}/download` | streamed deliverable-only ZIP |
| `GET /v1/executions/{id}/audit-download` | streamed non-deliverable audit ZIP |

Every route in this table requires a durable terminal execution snapshot. A
sealed manifest must match the `sealed_manifest_hash` bound into that snapshot.
Until then, active files and even a prepared seal are finalization state, not an
authoritative terminal artifact publication. The routes fail closed instead of
using a process-local terminal result or a new unbound sealed manifest.

Example:

```bash
curl -H "X-Viewer-Key: $VIEWER_KEY" \
  http://localhost:8000/v1/executions/EXECUTION_ID/artifacts

curl -H "X-Viewer-Key: $VIEWER_KEY" \
  -OJ http://localhost:8000/v1/executions/EXECUTION_ID/artifacts/code/index.html

curl -H "X-Viewer-Key: $VIEWER_KEY" \
  -OJ http://localhost:8000/v1/executions/EXECUTION_ID/download

curl -H "X-Viewer-Key: $VIEWER_KEY" \
  -OJ http://localhost:8000/v1/executions/EXECUTION_ID/audit-download
```

Single-file responses use the detected media type and include `ETag` and
`X-Content-SHA256` with the manifest hash. The private route also sends the
known content length. The headers contain the entry's content hash, not the
whole-manifest hash.

The normalized execution result may use the historical `output_reference`
field to point to `/v1/executions/{id}/artifacts`. It does not expose the server
root. Authenticated legacy compatibility payloads may still contain
`project_dir`; new clients should use the canonical artifact API.

## Share-scoped APIs

A share token can retrieve artifacts only when it was created with
`allow_artifact_download=true`:

| Method and path | Response |
| --- | --- |
| `GET /v1/shares/{token}/artifacts` | filtered share manifest |
| `GET /v1/shares/{token}/artifacts/{relative_path}` | one allowed file |
| `GET /v1/shares/{token}/download` | ZIP containing only allowed files |

The token binds the request to one execution. Supplying a path from another
execution cannot cross the registered root, and the token cannot authorize a
private execution route. Share artifact delivery applies the same durable
terminal-execution gate as private delivery and, for sealed manifests, the same
manifest-hash binding check. Historical `legacy_live` manifests remain the
explicit rescan-based compatibility exception described below.

A share record can be created before an execution becomes terminal. Its public
view may show the last committed nonterminal execution snapshot, but it omits
the artifact manifest and cannot retrieve artifacts until the terminal gate is
satisfied.

Share manifests allow `deliverable` entries only by default and also exclude
internal run records such as `full_log.json`, `plan.json`, `review.md`, builder
records, and transcripts. When candidate detail is disabled, raw
`candidate.md` files are excluded and entries tagged to non-winning candidates
are excluded; if there is no winner, candidate-scoped entries are not exposed.
Enabling candidate detail also permits `candidate_source` entries. It never
permits `provenance`, `log`, or `internal` roles and does not expose an absolute
path.

## Root registration

The registry persists one internal root per execution in SQLite. Production
roots must be existing, non-symlink directories that are strict descendants of
one of:

- `output/`
- `execution_artifacts/`

An execution cannot change roots after registration, and a root cannot belong
to two executions. The storage-base directory itself can never be registered or
deleted as an execution root.

DAG registers its run root through the artifact-ready callback. Ensemble
registers `execution_artifacts/{execution_id}` before candidate work and marks
it active. Terminal finalization seals the final baseline and clears the active
marker. Active markers are durable so both the modern registry pruner and the
legacy `output/` pruner can avoid live roots, including while final hashing is
still in progress.

There is no public artifact-upload API. The execution service registers output
produced by its own strategy paths.

## Roles, winner scope, and sealing

Role classification separates the requester-facing deliverable from audit
material. Known output/code paths are deliverables; plan, review, builder, and
revision records are provenance; logs/transcripts are logs; `candidate.md` is
candidate source; dotfiles and manifest/metadata records are internal. Strategy
code can supply explicit source/role metadata when path inference would be
ambiguous.

For ensemble/direct execution, candidate subtrees are validated independently.
Once a winner exists, the execution manifest prefix is set to that winner's
subtree before final sealing, so a losing candidate cannot appear in the
primary deliverable merely because it was produced in the same root. Candidate
audit exposure remains explicit and role-scoped.

New roots begin `active`. Terminal finalization performs one final bounded scan,
atomically stores immutable entry rows and the canonical manifest hash, clears
the active marker, and changes the state to `sealed`. Repeating seal returns the
same stored baseline. A sealed manifest is never refreshed from later directory
contents. Every file or ZIP retrieval still resolves the live path and hashes
its bytes against the sealed entry; missing, changed, symlinked, or escaped
content fails closed.

Sealing the filesystem baseline does not itself publish terminal lifecycle
truth. The execution service must next commit the terminal execution snapshot
that references the manifest hash. Only that durable binding opens private and
share-scoped terminal delivery. If terminal persistence permanently fails, the
seal may remain as internal finalization state while the last durable execution
remains queued or running; callers cannot retrieve it as a terminal artifact.

If the final tree cannot be safely scanned, sealing marks it `invalid` and
retrieval returns an integrity failure. Historical registrations without the
new seal have `legacy_live` mode and are rescanned on access; they must not be
labeled sealed. The normalized execution exposes `primary_deliverables`,
`artifact_manifest_url`, `audit_manifest_url`, `artifact_integrity_mode`, and
`sealed_manifest_hash` so clients do not infer these states from filenames.

Sealing is local integrity evidence only. It is not a digital signature,
external timestamp, content-safety verdict, model provenance attestation, or
defense against a host administrator able to alter both SQLite and files. See
[ADR 0007](adr/0007-sealed-artifact-manifests.md).

## Path policy

Protocol paths are canonical POSIX-relative strings. The registry rejects:

- empty paths, NULs, replacement characters, and paths longer than 500
  characters;
- absolute paths, UNC paths, or Windows drive paths;
- colons and backslashes, including NTFS alternate-data-stream syntax;
- empty, `.` or `..` path segments;
- non-normalized spellings;
- percent-encoded and repeatedly encoded traversal;
- case-folded duplicate normalized paths;
- a symlink root, symlink directory, symlink file, or symlink component;
- any path whose strict resolution escapes the registered root.

The URL router may decode a path before application code receives it. The
normalizer performs bounded repeated decoding as a second line of defense so a
later decoding layer cannot make traversal meaningful.

Every active or legacy manifest refresh walks with symlink following disabled
and then resolves each file strictly beneath the root. Sealed manifests load
the immutable stored entry baseline. Every individual download resolves the
requested live file again, rejects symlinks again, and recomputes SHA-256. A
file changed after a refresh or seal is rejected.

These checks are performed on the server after filesystem resolution; a string
prefix check alone is never treated as confinement.

## Media types and hashes

Media types come from the normalized filename through Python's MIME database;
unknown types use `application/octet-stream`. The server computes SHA-256 by
streaming each file in 1 MiB chunks. Hashes are integrity metadata for the
current server file, not signatures or provenance proof.

Manifest `created_at` comes from root registration. Entry `created_at` reflects
the file modification timestamp. Refreshing a manifest updates the internal
root timestamp used by retention but does not rewrite those public creation
fields.

## Quotas

Defaults in `config.py` are:

| Setting | Default | Meaning |
| --- | --- | --- |
| `artifact_max_files` | 100 | maximum files in one manifest/archive |
| `artifact_max_file_bytes` | 50 MiB | maximum size of one file |
| `artifact_max_aggregate_bytes` | 100 MiB | maximum uncompressed bytes per execution manifest |
| `artifact_retention_seconds` | 7 days | age after which a terminal registered root is eligible for deletion |
| `execution_artifacts_max_mb` | 500 MiB | aggregate registered-root storage target |
| `output_max_mb` | 500 MiB | legacy total `output/` cap |

Manifest listing and ZIP preparation fail with `413` when a file tree exceeds a
configured artifact limit. Invalid trees and paths return a generic `400` so
the server does not echo internal paths. Unknown execution artifacts and missing
entries return `404`.

The execution request's `max_output_bytes` bounds model text. Artifact quotas
are separate because extraction can create multiple files and ZIP delivery has
different resource costs.

## Streaming and ZIP construction

Single artifacts use the framework's file response and are streamed from disk.
An archive is never assembled in an in-memory byte buffer. The registry:

1. refreshes and bounds the manifest;
2. creates a temporary ZIP file outside the artifact root;
3. resolves and re-hashes each included entry while writing normalized archive
   names;
4. streams the temporary file with `application/zip`;
5. deletes it in the response background task.

Share ZIPs receive the filtered relative-path set and cannot include hidden
private entries. A server crash can leave an operating-system temporary file;
normal successful and failed construction paths delete their temporary files.

## Retention and pruning

The registry prunes registered execution roots in both `output/` and
`execution_artifacts/`. A root is protected when either:

- its durable registry `active` flag is true; or
- the cleanup pass identifies its canonical execution as queued/running.

Pruning first accounts for all registered bytes, including active roots. It
then considers terminal roots in oldest-update order. A root is removed when it
exceeds the retention age or when deletion is needed to reach the aggregate
byte target. The path is revalidated immediately before recursive deletion.
The root directory, manifest rows, and root record are deleted together from
their respective disk/SQLite stores.

The legacy `output/` cap also skips roots returned by
`ArtifactStore.active_root_paths()`. Unregistered historical `output/` runs
remain governed by that legacy cap.

Retention is not a secure-erasure guarantee. Filesystems, backups, copied share
downloads, SQLite previews, and accepted worker receipts can retain related
content. Deleting artifacts does not delete the canonical execution or share
record, so later artifact access may return `404` while the redacted execution
share remains readable.

## Failure and operational behavior

- Artifact registration or manifest failure is represented as a structured
  `artifact_manifest_failed` execution error; it does not cause an unsafe path
  to be published.
- Required terminal-execution persistence failure leaves the last durable
  lifecycle authoritative and suppresses terminal manifest, file, ZIP, and
  share publication. A later restart may reconcile that execution to
  `interrupted`; it does not retroactively publish the failed terminal result.
- Legacy run pages, history/gallery/status views, CLI history, archives, and
  demo capture resolve registered root ownership before trusting mutable
  `full_log.json` fields. Current runs require the same durable terminal and
  sealed-hash binding; unmarked historical records remain the explicit
  live-rescan compatibility case.
- An active or legacy-live manifest reflects a fresh bounded disk scan. A sealed
  manifest uses its immutable SQLite baseline and re-hashes every requested live
  file before delivery.
- The root record stores an absolute path internally. That field must never be
  serialized into canonical or share responses.
- Share validator diagnostics are generic because raw failure text may contain
  internal filesystem paths.
- The explicit private `role=audit` and deprecated `role=all` views can include
  internal records because the viewer is the trusted operator role. The default
  private view and public shares apply narrower role allowlists.

## Residual limitations

Artifact controls do not provide content moderation, antivirus scanning,
provenance signatures, externally anchored content addressing, encryption at
rest, per-user ownership, or generated-code isolation. The backup tools preserve
the local sealed baseline but do not turn it into an independent attestation. A
compromised orchestrator host can change artifacts and their SQLite metadata.
Hash recomputation detects ordinary drift at read time; it does not defend
against a host attacker who can change both file and database. Commit-before-
publication narrows coordinator inconsistency; it is not an external
transaction, immutable object store, or host-independent attestation.
