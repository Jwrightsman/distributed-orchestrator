# ADR 0013: Parser-heavy validators execute behind a bounded process boundary

- Status: Accepted
- Date: 2026-08-31
- Transport amendment: 2026-09-01 (runner protocol V2)
- Decision scope: built-in validator execution, parent/child protocol, artifact
  staging, resource containment, and validation evidence

## Context

The first version of the validator registry ran every built-in validator in the
coordinator process. That kept the registry simple, but parser crashes,
accidental global-state mutation, excessive CPU or memory use, and incidental
file-descriptor access shared the coordinator's failure domain. An outer async
timeout could stop waiting for an inline worker thread without stopping the
parser inside that thread.

The input is model-produced text or extracted files. These inputs are
untrusted, even though the validator implementations are closed, trusted
built-ins. The required boundary is therefore a bounded process boundary for
trusted parser code, not a way to execute arbitrary generated code and not a
hostile-code sandbox.

The original V1 runner request placed output-consuming validators' complete
generated output inside the JSON control message. Canonical execution permits
up to 10,485,760 UTF-8 bytes, while the default subprocess request limit is 2
MiB. A valid `structured_json` or `json_schema` result could therefore fail
before parsing solely because JSON-in-JSON transport exceeded the control limit;
escaping could amplify the mismatch. The output budget and the control-message
budget have different purposes and must not impose an accidental smaller output
ceiling on one another.

## Decision

### Closed registry and execution classes

The validator registry remains a closed allowlist. Every built-in has a stable
version and one parent-owned execution class:

| Execution class | Built-in validators | Reason |
| --- | --- | --- |
| `subprocess_isolated` | `code_parse`, `structured_json`, `json_schema` | These invoke language or data parsers over untrusted content and have the materially larger crash, recursion, CPU, and allocation surface. |
| `inline_trusted` | `nonempty`, `artifact_extraction`, `artifact_contract`, `file_manifest` | These are bounded coordinator-owned checks over already constrained values or the validated artifact snapshot. |

The execution mode is configurable:

- `auto` is the default. It follows the table above.
- `subprocess` sends every compatible built-in through the runner. Every
  current built-in supports that mode.
- `inline` is a weaker local-development and debugging compatibility mode. It
  runs every built-in in the coordinator process. Evidence retains
  `inline_trusted` for normally inline checks and records
  `inline_compatibility` when this setting overrides a normally isolated
  parser. Trusted-alpha preflight rejects it.

An isolated validator never falls back to inline after a spawn, timeout,
protocol, staging, or process-tree cleanup failure. Adding an import path, command,
executable, callable, third-party package, or plugin name to the registry or
wire protocol requires a separate decision.

The default wall time, memory request, request-byte limit, and response-byte
limit are 10 seconds, 256 MiB, 2 MiB, and 32 KiB. Their strict inclusive
configuration ranges are 1–120 seconds, 128–1,024 MiB, 16 KiB–16 MiB, and
1–256 KiB. Numeric values must be integers rather than booleans. Invalid
trusted-alpha configuration fails; local configuration warns and restores the
bounded default.

### Versioned, strict parent/child protocol

New parent-side executions serialize `ValidatorRunnerRequestV2` as bounded
UTF-8 JSON on standard input. The child returns exactly one bounded
`ValidatorRunnerResponseV2` as UTF-8 JSON on standard output. The parser first
reads an explicit bounded string `protocol_version`, then dispatches to that
version's strict model. Missing, malformed, and unsupported versions are stable
protocol failures. A response must use the request's version and exact
allowlisted validator identity. A malformed V2 message is never interpreted as
V1, and V2 failure never triggers a V1 retry or inline fallback.

V1 remains explicitly parseable for compatibility and focused protocol tests.
It retains its inline `output` field and is not emitted by new production parent
calls. Unknown fields, unknown validator identities, version mismatches,
malformed JSON, excessive nesting or collection sizes, non-finite numbers, and
over-limit request or response bytes are rejected in both versions.

The V2 JSON request contains only the protocol version, allowlisted validator
identity and version, an optional fixed-path output reference, the
validator-specific bounded contract projection, validated normalized logical
filenames, and parent-clamped numeric limits. The reference is required for
`nonempty`, `structured_json`, and `json_schema` and forbidden for file-only
validators. It is exactly:

```json
{
  "relative_path": "__mycelium_validator_input__/output.utf8",
  "encoding": "utf-8",
  "byte_length": 12345,
  "sha256": "<64 lowercase hexadecimal characters>"
}
```

The path and encoding are literals, the byte length is bounded at 10,485,760,
the digest is lowercase SHA-256, and unknown fields are forbidden. The request
cannot name an import, executable, shell command, arbitrary callable, database,
credential, enrollment, session, attempt nonce, or unrelated execution. Task
content and generated output are not present in V2 JSON, process arguments, or
the child environment. Schemas and filenames remain bounded control metadata;
none of those values are command-line arguments.

`validator_subprocess_request_max_bytes` therefore limits the serialized JSON
control envelope. It does not limit the referenced output body. The execution's
existing `ExecutionRequestV1.max_output_bytes`, itself bounded by the protocol
maximum of 10,485,760 UTF-8 bytes, remains the authoritative per-execution
output ceiling. No second configurable output ceiling is introduced.

The response contains only the protocol version, validator identity and
version, boolean outcome, bounded optional score, bounded detail, and bounded
failure reason. The parent verifies response identity against its request and
rejects recognized bare or delimiter-prefixed absolute POSIX/Windows/UNC
host-path patterns, plus any `file://` pattern, in bounded response keys and
values. The wire model has no host-path field. Child-supplied
assurance, correctness,
required/optional status, contract-floor source, execution mode, containment
level, or lifecycle metadata in bounded detail is non-authoritative and cannot
alter the corresponding parent-owned evidence fields. The parent registry
remains authoritative for those meanings. V2 recursively rejects private
output-reference field names in detail. Output-consuming validators additionally
have closed built-in response shapes and exact ordinary failure reasons, so a
child cannot echo output text, a digest, or the private reference through detail
or `failure_reason`; an off-shape response is malformed protocol.

The runner is launched with the current Python interpreter without a shell. It
uses a sanitized environment allowlist, a fresh working directory with private
POSIX modes where supported, and controlled `TEMP`/`TMP` on Windows or `TMPDIR`
on POSIX inside that directory. It closes inherited descriptors where
supported and creates a new process group or platform equivalent. Incidental child stdout and stderr are discarded or
bounded away from the protocol channel; raw stderr is not validation evidence.

The launcher fixes the child `cwd` to the parent-created private validator
directory. After its early limits and environment sanitization, the runner
resolves that directory once and passes it to the closed dispatcher as explicit
`stage_root`. For `code_parse`, that directory contains the bounded copied
artifact inputs. For an output-consuming validator, it contains the one
protocol-owned output file in the reserved namespace. For metadata-only checks
forced through the subprocess, it remains empty and the request carries only
validated normalized logical filenames. The strict request cannot select,
replace, or derive the filesystem root from an ambient or operator working
directory.

### Private generated-output staging

Before process creation, the parent strictly encodes the exact canonical output
as UTF-8 and enforces the execution's authoritative byte budget and the hard 10
MiB protocol maximum. Inside the fresh workspace it exclusively creates
`__mycelium_validator_input__/output.utf8`, uses no-follow and descriptor-
relative operations where the standard library and platform expose them,
applies POSIX mode `0700` to the reserved directory and `0600` to the file, and
uses the best available inherited private ACL behavior on Windows. It writes
and hashes the exact bytes, records their exact length, and closes the file
before spawning the child. Cancellation and the clamped deadline are checked
during this work.

The reserved namespace is server-owned. Candidate artifact selection and V2
staged-file validation reject the namespace itself and every descendant, using
portable case-insensitive collision checks. Task data, model output, artifact
manifests, and request callers cannot choose or derive another output path.

The child accepts only the fixed path and `utf-8` encoding. It resolves beneath
the parent-controlled stage root, rejects missing, symlink/reparse, directory,
FIFO, socket, device, and other nonregular targets, and opens the file once.
Using that owned descriptor, it rechecks the regular-file identity and size,
reads at most the declared length plus one byte where the hard ceiling permits,
verifies exact byte length and SHA-256 with constant-time digest comparison,
and then decodes strict UTF-8.
Only the resulting string reaches the selected closed built-in validator.
Reference, file-type, size, digest, encoding, and oversize failures have stable
content-free reason codes and are infrastructure failures rather than ordinary
validator rejection.

### Validated artifact inputs and staged copies

Every artifact-aware runner receives only an explicit bounded set of normalized
logical names. The parent starts from the authoritative artifact root and
selected candidate subtree. Canonical strategy paths supply the validated entry
snapshot to both repair-time parsing and registry validation; standalone
compatibility callers without an ArtifactStore retain the root/path/type
boundary without that optional snapshot membership. A selected source may be
relative to the subtree or a current-materializer absolute path that resolves
inside it. The parent accepts regular files only and rejects traversal, paths
outside the selected candidate subtree, symlinks, FIFOs, sockets, devices,
duplicate portable paths, and names absent from the supplied authoritative
snapshot.

`code_parse` is the content-consuming file validator. Its subprocess path also
enforces per-file and aggregate byte limits, opens the selected files safely,
checks authoritative size and SHA-256 when supplied, and streams bytes into a
fresh staging directory. It does not create hard links or preserve source
metadata. The production copy path permits at most 20 files, 10 MiB per file,
10 MiB aggregate, and 200 characters per staged relative path. These limits are
separate from artifact delivery quotas and serialized runner request/response
configuration.

`artifact_extraction`, `artifact_contract`, and `file_manifest` consume only
artifact names and contract projections. When forced through `subprocess`, the
parent applies the same authoritative root/subtree confinement, regular-file
and symlink/special-file rejection, snapshot-membership, portable-duplicate,
file-count, and relative-path checks, but it neither copies nor rehashes file
content. Their child receives only the normalized logical names and an empty
private working directory. Large artifact bytes therefore do not cross the
process boundary merely for a metadata check, and the content-copy byte limits
do not apply to those checks.

The child never receives a coordinator artifact root. Process-tree termination
and reaping are attempted before private-workspace removal on success,
validation failure, protocol/reference failure, timeout, crash, spawn failure,
or cancellation. Failure to confirm
process-tree cleanup is a counted containment incident. Failure to delete the
temporary workspace records fail-closed `validator_stage_cleanup_failed`
evidence and increments the distinct `staging_cleanup_failures` counter. The
failed deletion can still leave a stale `mycelium-validator-*` directory for
operator cleanup.

This reduces accidental exposure. Because the child runs as the same operating-
system user, it is not mandatory access control and does not guarantee
filesystem confidentiality from a malicious child or compromised dependency.

### Deadlines, limits, and fail-closed behavior

The configured validator wall time is bounded and canonical repair/registry
validation clamps it to the execution's remaining deadline. A timeout or cancellation
requests child-process-group termination, descendant cleanup, and direct-child
reaping. Normal cleanup completes that sequence before stage removal is
attempted. Failure to confirm process-tree cleanup is counted and treated as a
containment incident. Failure solely to delete the temporary workspace also
fails closed and is counted separately. There are no runner retries.

On POSIX, the child applies available standard-library limits for CPU time,
address space, output-file size, open descriptors, and child-process creation,
in addition to parent-enforced wall time and I/O bounds. Support remains kernel-
and-runtime-dependent. On Windows, the runner retains wall-clock enforcement,
bounded pipes, a fresh process group, staging, and protocol validation. It also
best-effort creates a per-run Job Object and, immediately after spawn, attempts
to assign the runner with `KILL_ON_JOB_CLOSE` and `ActiveProcessLimit=1`.
Successful assignment strengthens process-tree cleanup, parent-exit handling,
and child-process prevention; it does not provide the POSIX CPU, address-space,
output-file, or descriptor limits.

Job creation, configuration, or assignment can be unavailable or fail,
including when a restrictive enclosing Job Object prevents assignment. The
runner is not spawned suspended, so work or a descendant created in the
spawn-to-assignment window is not retroactively covered. In either case the
existing process-group and `taskkill /T` path remains the live-parent fallback.
Evidence therefore retains `containment_level="windows_process_tree_best_effort"`
rather than claiming guaranteed Job membership or POSIX-equivalent limits.
Windows source-path race resistance is also best effort because Python does not
expose the POSIX `openat`/`O_NOFOLLOW` primitives there. Windows mode bits do not
establish a private DACL; the working tree inherits the host temporary root's
ACL, which the operator must secure. This boundary does not claim stage
confidentiality.

A new process group alone is not a parent-death contract. A successfully
assigned Windows Job Object closes with the coordinator and requests termination
of its assigned process, but assignment is best effort and has the pre-assignment
gap above. The runner uses no Linux parent-death signal or durable PID registry.
After abrupt coordinator death, the POSIX child's early hard alarm bounds its
normal lifetime to roughly 125 seconds. Windows has no equivalent child-side
alarm; a runner not covered by the Job Object, or a descendant that escaped
before assignment, can outlive the coordinator until an operator removes it.
Restart does not discover prior runner processes.

Spawn failure, timeout, crash, signal exit, malformed or mismatched response,
oversized response, invalid output reference, and artifact/output staging
failure become bounded `status="error"` validation
evidence with a stable parent-supplied reason. An unconfirmed process-tree
cleanup is reflected in error evidence or the content-free cleanup counter,
depending on the original outcome. Raw exceptions, stderr, input bodies,
schemas, and host paths are not copied into
evidence or metrics. Existing required/optional and contract-floor aggregation
then records candidate validation acceptance. A required isolated check records
nonpassing/error evidence and cannot fall back inline to preserve availability.
Afterward, the existing explicit `allow_unverified_fallback` policy may still
select and deliver a usable failed candidate with that validation outcome
preserved. A runner failure cannot redefine already committed lifecycle state.

### Evidence and observability

Existing evidence fields retain their meanings: validator identity and
version, required/optional status, contract-floor or explicit source, duration,
bounded failure reason, behavioral-correctness flag, aggregation, and assurance.
The parent adds bounded execution metadata such as execution mode, runner
protocol version, containment level, and termination reason when applicable. The child cannot
raise its assurance level or assert behavioral correctness; its bounded result
detail and ordinary validation reason remain non-authoritative.

Process-local, content-free counters distinguish runner starts, valid
responses, validation failures, timeouts, crashes, malformed or oversized
responses, oversized requests, spawn and staging failures, cancellations, and
process-tree and staging-workspace cleanup failures. Separate bounded totals
cover output staging, output-reference, size, digest, UTF-8, and oversize
failures. They are operational
diagnostics, not lifecycle or validation authority, and reset on coordinator
restart. Logs, evidence, and counters omit prompts, generated output, schemas,
source bytes, credentials, raw stderr, the private output-reference object/path/
digest, absolute workspace paths, and arbitrary exception text. Existing
bounded validator byte-count detail is unchanged; it is not the V2 reference.

### Parsing remains non-executing

`code_parse` parses supported generated files as data. It does not import a
generated module, run top-level statements, invoke a shell or compiler build
step, install a package, open a browser, run tests, or execute generated
networking. The other built-ins inspect bounded content or validated artifact
metadata without executing it. Parser success is structural evidence only and
does not prove the requested behavior.

`network_policy` remains recorded caller intent. It is not consumed as a
firewall or reliable network-denial control for the coordinator, runner,
workers, model providers, or code an operator later chooses to run.

## Consequences

- In `auto` or forced `subprocess`, parser-heavy built-ins no longer share the
  coordinator's process lifetime or mutable module state. Local `inline`
  deliberately gives up that separation.
- Request, response, staging, wall time, parent-directed process-tree cleanup,
  and available POSIX resources have explicit finite bounds.
- Any valid strict UTF-8 output within the execution's canonical byte budget can
  reach an applicable isolated validator without the 2 MiB control-envelope
  default becoming an unintended output ceiling.
- Output validation adds an ephemeral private file and bounded disk I/O. The V2
  control message carries only its fixed path, byte count, encoding, and digest,
  not the generated body.
- Forced-subprocess testing can exercise every current built-in through one
  protocol without opening a plugin mechanism.
- Process startup adds latency; `code_parse` file copying also adds temporary
  disk I/O, while metadata-only checks avoid copying artifact content.
- Containment strength differs by operating system and must be reported rather
  than normalized into an unsupported guarantee.
- A same-user process can still attempt host access. This decision is not a
  kernel, container, VM, WASM, filesystem-confidentiality, or network boundary.

## Rejected alternatives

### Keep every validator inline

Rejected because parser crashes and runaway resource use would retain the
coordinator's failure domain, and thread cancellation cannot reliably stop a
running parser.

### Execute generated code to obtain behavioral assurance

Rejected because parser containment is not an executable-workload sandbox.
Behavioral tests require a later task-specific assurance design and a stronger
isolation boundary.

### Add arbitrary validator plugins or dynamic imports

Rejected because a closed built-in allowlist is necessary for a strict minimal
protocol. Plugin provenance, installation, permissions, and compatibility are
separate problems.

### Increase the V1 JSON request limit

Rejected because it would continue to duplicate and JSON-escape the complete
output, couple two independent limits, increase pipe and parser exposure, and
still make escaping expansion part of validator eligibility. A fixed,
hash-and-size-bound workspace reference preserves the canonical output budget
while keeping the control envelope small.

### Require containers, gVisor, Firecracker, virtual machines, or WASM now

Rejected for this bounded step. Those technologies can provide materially
stronger hostile-code isolation, but would add deployment dependencies and do
not fit a runner that only invokes trusted built-in parsers. They remain
options for later executable-workload validation; this ADR does not simulate
their guarantees.
