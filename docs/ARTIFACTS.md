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
  "entries": [
    {
      "relative_path": "code/index.html",
      "media_type": "text/html",
      "size_bytes": 1234,
      "sha256": "64-lowercase-hex-characters",
      "source_candidate_id": null,
      "source_execution_unit_id": null,
      "created_at": "2026-08-21T12:00:00+00:00"
    }
  ]
}
```

The internal artifact root is never part of either public model. Source fields
are optional; ensemble entries beneath `candidate_N/` are associated with
`candidate-N` automatically.

## Authenticated APIs

All canonical execution artifact routes require viewer authorization when
`viewer_key` is configured:

| Method and path | Response |
| --- | --- |
| `GET /v1/executions/{id}/artifacts` | complete private manifest |
| `GET /v1/executions/{id}/artifacts/{relative_path}` | one streamed file |
| `GET /v1/executions/{id}/download` | streamed ZIP of the complete private manifest |

Example:

```bash
curl -H "X-Viewer-Key: $VIEWER_KEY" \
  http://localhost:8000/v1/executions/EXECUTION_ID/artifacts

curl -H "X-Viewer-Key: $VIEWER_KEY" \
  -OJ http://localhost:8000/v1/executions/EXECUTION_ID/artifacts/code/index.html

curl -H "X-Viewer-Key: $VIEWER_KEY" \
  -OJ http://localhost:8000/v1/executions/EXECUTION_ID/download
```

Single-file responses use the detected media type and include `ETag` and
`X-Content-SHA256` with the manifest hash. The private route also sends the
known content length.

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
private execution route.

Share manifests exclude internal run records such as `full_log.json`,
`plan.json`, `review.md`, builder records, and transcripts. When candidate
detail is disabled, raw `candidate.md` files are excluded and artifacts tagged
to non-winning candidates are excluded. Enabling candidate detail permits the
candidate-scoped entries; it does not expose an absolute path.

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
it active. Terminal finalization refreshes the manifest and clears the active
marker. Active markers are durable so both the modern registry pruner and the
legacy `output/` pruner can avoid live roots.

There is no public artifact-upload API. The execution service registers output
produced by its own strategy paths.

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

Every manifest refresh walks with symlink following disabled and then resolves
each file strictly beneath the root. Every individual download refreshes the
manifest, resolves the requested file again, rejects symlinks again, and
recomputes its SHA-256. A file changed between manifest refresh and open is
rejected.

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
- A manifest always reflects a fresh bounded disk scan rather than trusting
  stale database rows.
- The root record stores an absolute path internally. That field must never be
  serialized into canonical or share responses.
- Share validator diagnostics are generic because raw failure text may contain
  internal filesystem paths.
- The private manifest can include internal logs because the viewer is the
  trusted operator role. Public shares apply an additional allowlist filter.

## Residual limitations

Artifact controls do not provide content moderation, antivirus scanning,
provenance signatures, immutable content addressing, encryption at rest,
per-user ownership, backup policy, or generated-code isolation. A compromised
orchestrator host can change artifacts and their SQLite metadata. Hash
recomputation detects ordinary drift at read time; it does not defend against a
host attacker who can change both file and database.
