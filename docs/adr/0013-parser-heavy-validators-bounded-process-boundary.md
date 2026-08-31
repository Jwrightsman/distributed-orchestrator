# ADR 0013: Parser-heavy validators execute behind a bounded process boundary

- Status: Accepted
- Date: 2026-08-31
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

The parent serializes `ValidatorRunnerRequestV1` as bounded UTF-8 JSON on
standard input. The child returns exactly one bounded
`ValidatorRunnerResponseV1` as UTF-8 JSON on standard output. Unknown fields,
unknown validator identities, version mismatches, malformed JSON, excessive
nesting or collection sizes, non-finite numbers, and over-limit request or
response bytes are rejected.

The request contains only the protocol version, allowlisted validator identity
and version, the minimal bounded output or validator-specific contract
projection, staged relative filenames, and parent-clamped numeric limits. It
cannot name an import, executable, shell command, arbitrary callable, database,
credential, enrollment, session, attempt nonce, or unrelated execution. Task
content, generated output, schemas, and filenames are never command-line
arguments.

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
remains authoritative for those meanings.

The runner is launched with the current Python interpreter without a shell. It
uses a sanitized environment allowlist, a fresh working directory with private
POSIX modes where supported, and controlled `TEMP`/`TMP` on Windows or `TMPDIR`
on POSIX inside that directory. It closes inherited descriptors where
supported and creates a new process group or platform equivalent. Incidental child stdout and stderr are discarded or
bounded away from the protocol channel; raw stderr is not validation evidence.

The launcher fixes the child `cwd` to the parent-created staged-input directory.
After its early limits and environment sanitization, the runner resolves that
directory once and passes it to the closed dispatcher as explicit `stage_root`.
The strict request can name only staged relative files; it cannot select,
replace, or derive the filesystem root from an ambient or operator working
directory.

### Staged artifact copies

A file-consuming runner receives only an explicit bounded set of copies. The
parent starts from the authoritative artifact root and selected candidate
subtree. Canonical strategy paths supply the validated entry snapshot to both
repair-time parsing and registry validation; standalone compatibility callers
without an ArtifactStore retain the root/path/type/size boundary without that
optional hash snapshot. A selected source may be relative to the
subtree or a current-materializer absolute path that resolves inside it; the
child receives only normalized staged relative names. The parent accepts
regular files only and rejects traversal, paths outside the selected candidate subtree,
symlinks, FIFOs, sockets, devices, duplicate portable paths, and changes that
invalidate the authoritative size or hash claim.

The parent enforces file-count, per-file-byte, aggregate-byte, and relative-path
limits, then copies bytes into a fresh staging directory. It does
not create hard links or preserve source metadata. The child sees only staged
relative paths, not coordinator artifact roots. Process-tree termination and
reaping are attempted before stage removal on success, validation failure,
timeout, crash, or cancellation. Failure to confirm process-tree
cleanup is a counted containment incident. Failure to delete the temporary
workspace records fail-closed `validator_stage_cleanup_failed` evidence and
increments the distinct `staging_cleanup_failures` counter. The failed deletion
can still leave a stale `mycelium-validator-*` directory for operator cleanup.

The production stage uses fixed limits of 20 files, 10 MiB per file, 10 MiB
aggregate, and 200 characters per staged relative path. These are separate from
artifact delivery quotas and serialized runner request/response configuration.

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
oversized output, and staging failure become bounded `status="error"` validation
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
process-tree and staging-workspace cleanup failures. They are operational
diagnostics, not lifecycle or validation authority, and reset on coordinator
restart. Logs, evidence, and counters omit prompts, generated output, schemas,
source bytes, credentials, raw stderr, and arbitrary exception text.

### Parsing remains non-executing

`code_parse` parses supported generated files as data. It does not import a
generated module, run top-level statements, invoke a shell or compiler build
step, install a package, open a browser, run tests, or execute generated
networking. The other built-ins likewise inspect content without executing it.
Parser success is structural evidence only and does not prove the requested
behavior.

`network_policy` remains recorded caller intent. It is not consumed as a
firewall or reliable network-denial control for the coordinator, runner,
workers, model providers, or code an operator later chooses to run.

## Consequences

- In `auto` or forced `subprocess`, parser-heavy built-ins no longer share the
  coordinator's process lifetime or mutable module state. Local `inline`
  deliberately gives up that separation.
- Request, response, staging, wall time, parent-directed process-tree cleanup,
  and available POSIX resources have explicit finite bounds.
- Forced-subprocess testing can exercise every current built-in through one
  protocol without opening a plugin mechanism.
- Process startup and file copying add latency and temporary disk I/O.
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

### Require containers, gVisor, Firecracker, virtual machines, or WASM now

Rejected for this bounded step. Those technologies can provide materially
stronger hostile-code isolation, but would add deployment dependencies and do
not fit a runner that only invokes trusted built-in parsers. They remain
options for later executable-workload validation; this ADR does not simulate
their guarantees.
