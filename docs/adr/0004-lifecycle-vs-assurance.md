# ADR 0004: Lifecycle and Assurance Are Separate Dimensions

- Status: Accepted
- Date: 2026-08-21
- Decision scope: canonical execution result semantics

## Context

The original canonical status vocabulary included `unverified` beside
`queued`, `running`, `completed`, and `failed`. That combines two unrelated
questions:

1. **Lifecycle:** is work queued, running, finished, failed, cancelled, or
   interrupted?
2. **Assurance:** what evidence supports claims about the finished output?

An execution can finish normally without running a behavioral validator. It can
also fail after producing structurally valid artifacts. Treating `unverified`
as lifecycle makes polling ambiguous, encourages clients to mistake parsing for
correctness, and leaves no clean representation for cancellation or restart
interruption.

## Decision

Canonical results carry three independent fields:

```text
lifecycle_status:
  queued | running | completed | failed | cancelled | interrupted

validation_outcome:
  passed | failed | partial | not_run

assurance_level:
  unverified | structural | deterministic | model_judged
```

Lifecycle controls whether work is terminal. Validation outcome describes the
aggregate result of the checks that were required and run. Assurance describes
the strongest evidence actually earned by passing checks.

Every result also includes a structured validation summary:

- checks run;
- checks passed;
- checks failed or errored;
- checks not run;
- whether any check claims behavioral correctness;
- a bounded explanation.

The existing `status` field remains a documented compatibility projection. The
canonical service projects lifecycle `completed` with validation outcome
`passed` as `status="completed"` and other completed validation outcomes as
`status="unverified"`. New control flow must use `lifecycle_status`; new trust
decisions must use `validation_outcome`, `assurance_level`, and the summary.

## Assurance semantics

`structural` means the output has a checked shape, such as being nonempty,
valid JSON, extractable, manifest-conformant, or parseable by a supported
parser. It does not mean the requested behavior works.

`deterministic` means a deterministic contract check passed, currently JSON
Schema conformance. It proves that specific contract property, not general
behavioral correctness.

`model_judged` is reserved for model-evaluated evidence and must be identified
as model judgment rather than mechanical proof.

`unverified` means no passing evidence earned a stronger classification or the
selected fallback did not satisfy required validation.

No built-in structural or JSON Schema check currently sets
`proves_behavioral_correctness=true`. Generated code parsing is not execution,
and artifact extraction is not correctness.

## Validator policy

The output contract establishes a mandatory floor. Explicit validators may add
requirements but cannot remove floor checks. Floors use AND semantics.
`verification.require_all` controls only explicit required validators: all when
true, at least one when false. Optional validators do not decide acceptance.

The automatic strategy selector may use deterministic JSON Schema conformance
as a candidate-comparison property. It must not treat nonempty output,
extraction, code parsing, or a file manifest as deterministic proof of the
requested behavior.

## Lifecycle consequences

- A normal but weakly evidenced result is `completed`, not a seventh lifecycle
  state.
- A timeout is terminal `failed` and retryable, regardless of partial
  structural output.
- User cancellation is terminal `cancelled`.
- Non-resumable work discovered after coordinator restart is terminal
  `interrupted` and retryable.
- An unverified ensemble fallback can be lifecycle-completed while validation
  failed or was partial.
- Polling clients can stop on lifecycle without making a correctness judgment.

## Compatibility

Older clients may continue to read `status`. Accepted projections are:

| Lifecycle | Compatibility status |
| --- | --- |
| queued | `queued` |
| running | `running` |
| completed with validation outcome `passed` | `completed` |
| completed with validation outcome `failed`, `partial`, or `not_run` | `unverified` |
| failed | `failed` |
| cancelled | `cancelled` (or legacy failure projection where required) |
| interrupted | `interrupted` or legacy `failed` projection |

Legacy async jobs retain `complete` rather than canonical `completed`. Adapters
must not feed that spelling back into canonical lifecycle logic.

## Consequences

- REST, CLI, MCP, storage, events, and shares can describe completion and
  evidence without overloading one field.
- Validation reports become more verbose, but the added fields are explicit and
  machine-readable.
- Clients that treated `status != completed` as nonterminal must migrate to
  `lifecycle_status`.
- Marketing and UI copy must say what was checked, not simply "verified" or
  "working."

## Rejected alternatives

### Keep `unverified` as a lifecycle state

Rejected because it conflates completion with evidence and complicates
cancellation, timeout, and restart semantics.

### A single numeric confidence score

Rejected because scores from parsers, schemas, and model judges are not
commensurate and hide what was not checked.

### Treat every deterministic check as behavioral proof

Rejected because deterministic execution of the checker says nothing about the
scope of its claim. JSON Schema is deterministic and still proves only schema
conformance.

### Remove the old `status` field immediately

Rejected because compatibility adapters and clients already consume it. An
additive projection allows migration without silently changing old payloads.
