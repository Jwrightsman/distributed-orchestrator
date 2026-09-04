# ADR 0018 — Trace context is propagated; export is the contributor's choice

**Status:** Accepted (2026-09-03, Theme 4B)

**Context:** ROADMAP section 6 asks for "every job traceable end to end". Today a
cross-machine timeout, reassignment, or settlement incident has to be
reconstructed from custom events on one side of the boundary: the coordinator's
`/events` stream says a lease expired, the worker's terminal says it was still
building, and lining the two up is done by hand against two clocks.

W3C trace context is the standard answer, and adopting it is mostly a matter of
deciding what *not* to do with it.

## The two decisions this ADR is really about

### Propagation and export are separate, and only one of them is telemetry

A worker runs on a machine somebody else owns.

**Accepting a trace ID and handing it back costs that contributor nothing.** The
coordinator minted the value, the worker echoes two headers, and nothing about
that machine is described. It is not telemetry in any sense a contributor would
recognise, and it is what lets the *operator* read their own incident.

**Exporting that worker's own spans to the operator's collector is telemetry
leaving a stranger's machine.** It is a separate switch, `tracing_export`, off
by default, and it is never a condition of joining. A worker that never enables
it still takes work, still settles, still earns credit, and still produces a
coordinator-side trace that is useful on its own. ROADMAP section 2:
"A contributor's own data never leaves their device unless they explicitly
permit it."

Collapsing these two into one flag would have been less code and would have
quietly made joining mean "and send me your telemetry".

### The coordinator's trace does not depend on the worker cooperating

A worker that ignores the headers entirely is not penalised and is not less
traceable from the coordinator's side: when no valid `traceparent` arrives, the
coordinator mints one. Propagation is offered, never required.

This matters more than it first looks. It means an old worker, a third-party
worker, or a worker whose operator switched everything off still leaves the
coordinator one readable story per unit - so the feature is worth having on the
day it ships rather than after a fleet upgrade.

## Three states, and what each one means

| state | condition | behaviour |
| --- | --- | --- |
| `off` | `tracing_enabled` false (**default**) | No header read, none written, no span built, nothing allocated per request |
| `propagating` | `tracing_enabled` true, no SDK | Context accepted, validated, minted, propagated; spans recorded in-process; nothing leaves the machine |
| `exporting` | `tracing_enabled` and `tracing_export` true, **SDK** importable | Spans additionally reach the operator's collector |

### Why the dependency is optional, and why it is the SDK not the API

The OpenTelemetry SDK is a real runtime dependency, and the repository's rule is
not to add one without needing it. It is imported nowhere at module scope - a
test parses every module's AST to assert that, rather than grepping for the
string, which would match this ADR, the docstrings, and the guarded import
itself. `python -c "import server"` therefore succeeds on a machine with nothing
installed, and a second test proves it in a subprocess that *refuses* every
`opentelemetry` import, because this repository's own environment has
`opentelemetry-api` installed transitively via `mcp` and could not otherwise
falsify the claim.

That transitive install produced the one real design correction here. The bridge
originally accepted `opentelemetry.api`; on its own the API returns a
non-recording tracer, so a deployment with only the API would have reported that
it was exporting while sending nothing anywhere. The bridge now requires
`opentelemetry.sdk`. This was found by a test asserting its own precondition -
"the SDK is absent here, so the no-SDK claims are being tested" - rather than by
anyone noticing an empty collector.

## Attributes are an allowlist, enforced by Python

`tracing.attributes()` takes keyword-only parameters. A key outside the set is a
`TypeError` raised by Python itself, not something a scanner has to notice, and
`ATTRIBUTE_ALLOWLIST` is *derived by calling that function* so the constant and
the enforcement cannot drift. A raw dictionary reaching `span()` is validated
against the same set and refused.

**No denylist, deliberately.** Finding F8 was a probe that searched text for
short needles and reported leaks that were not there, failing CI at random. A
scanner looking for secrets inside span attributes is the same mistake with the
polarity flipped: it would be both unreliable and reassuring. Nothing carrying
content has a slot to go in, so there is nothing to scan for.

Two absences are specific choices:

* **No `hostname`.** A node-supplied hostname is finding F2 from Theme 4A.
  `node_label` is present because it is the identity the rest of the system
  already displays - display metadata, never a trust key (ADR 0016, seam 2).
* **No `error_message`.** `error_code` is a fixed code the coordinator chose;
  worker error text is a string a stranger's machine wrote.

Never permitted anywhere: prompts, model outputs, artifact contents, schemas,
requester or enrollment credentials, session tokens, attempt nonces, idempotency
keys, worker error text.

## High-cardinality identifiers: spans yes, metric labels no

Execution, unit, attempt, receipt and enrollment IDs are exactly what makes a
trace worth following and exactly what would destroy a metrics backend, where
every distinct label value is a new time series. They are therefore span
attributes and are asserted to appear in no `/metrics` key or value. `/metrics`
keeps the fixed key set it already had; a test pins that set, so widening it is a
deliberate act.

Nothing about the existing telemetry is removed. Spans and events are joined on
the identifiers they already share rather than by writing one into the other - a
test asserts that no trace ID reaches `/events` or the logs.

## Trace context is untrusted input

A worker-supplied `traceparent` is bounded (256 bytes), validated against the
W3C grammar, and rejected for an all-zero trace or span ID, for version `ff`,
and for version `00` carrying extra fields. An invalid `tracestate` is dropped
while a valid `traceparent` survives, per the specification.

It is **never echoed back**. A rejected value is not named in any error body,
because reflecting it would put a stranger's string into the coordinator's own
response. A request carrying rubbish proceeds exactly as one carrying nothing.

And it decides nothing. Trace context is read by no routing, eligibility,
admission, settlement, credit, validation, or terminal-state code. A test runs
the same sequence traced and untraced and asserts the settlements, credits,
receipts and attempt states are identical.

## A reassigned unit stays in one trace

Found by a test rather than designed in: when a lease expired and another node
took the reclaimed unit, the second handout started a *fresh* trace, so the
coordinator's view of one unit was split in two - which is precisely the "where
did this job go?" question a trace exists to answer.

The coordinator now remembers which trace a unit belongs to and moves a later
handout, token batch, or submission into it. The map is process-local, carries
no content, bounded by eviction rather than by cleanup (a map emptied at five
call sites leaks the first time one is missed, and ROADMAP section 6 already
lists unbounded coordinator memory as a launch risk), and is empty after a
restart - which is correct, because a new epoch is a new trace. Nothing about it
appears on the wire.

## The validator subprocess gets environment, not payload

The runner's control message is deliberately minimal (ADR 0013), and PR #64
removed content from it rather than adding any. Its environment was already a
strict allowlist, so `TRACEPARENT` and `TRACESTATE` go there. That puts
*nothing at all* in the control message - a stricter reading of "nothing beyond
the two headers' worth of context" than adding two fields would have been, and
a test asserts `ValidatorRunnerRequestV2` gained no field.

The current span reaches the spawn site through a context variable rather than a
new parameter on every function between them, which works because the validator
is reached through `asyncio.to_thread` and that copies the calling context.

## What is deferred, and what is not claimed

**No improvement in diagnosis time is claimed, because none has been measured.**
The audit's test for this theme is diagnosis time on induced failures across
machines, which needs a live multi-node deployment and a diagnoser who did not
write the code. The protocol is written down in
[docs/experiments/trace-diagnosis-time.md](../experiments/trace-diagnosis-time.md),
including what would count as *no* improvement, stated in advance so the result
cannot be reinterpreted afterwards.

What was achievable and was done: five induced failures - lease expiry,
reassignment, settlement rejection, persistence failure, and a worker that
disappears mid-stream - each demonstrated to hang together under one trace ID in
`tests/test_trace_correlation.py`. That is a precondition for the experiment,
not a substitute for it.

A single-worker WAN trace was **not run**. No remote worker is running, and
standing one up means asking a person to install software on their machine,
which is not a thing to do to complete a checklist (`AGENTS.md`).

## Consequences

* One optional runtime dependency, imported nowhere at module scope, with the
  disabled and propagating paths tested on an environment where the SDK is
  genuinely absent.
* One bounded process-local map, and a span ring capped at 512 entries.
* A new configuration surface of three keys, all defaulting to off or empty.
* No sampling policy beyond on and off. A sampler is a real decision about cost
  and it has no data behind it yet; add one when a collector exists and its
  volume is a problem.
* No collector, backend, or dashboard is shipped or configured. Choosing and
  running one is the operator's job, and shipping a default endpoint would be a
  network-facing decision made on somebody's behalf.
