# ADR 0002: Prerequisites for a Research Strategy

- Status: Accepted design constraint; implementation deferred
- Date: 2026-08-21
- Decision owners: Mycelium maintainers

## Context

Execution protocol v1 deliberately ships DAG and ensemble only. A future
`research` strategy would handle evidence gathering rather than merely generate
several answers. Calling the same model multiple times and merging prose does
not provide source provenance, claim support, freshness, conflict handling, or
privacy controls, so it is not a research protocol.

## Decision

Mycelium will not register or accept a production `research` strategy until the
following components have explicit contracts and tests.

### Source-provider and tool interface

Providers must expose bounded queries, source identity, canonical URL or
dataset id, retrieval status, content type, timestamps, and error behavior.
Tool availability and provider versions must be recorded. A strategy must be
able to distinguish search results, fetched primary sources, user-provided
material, and model prior knowledge.

### Network and domain policy

The request must state whether networking is disabled, restricted, or allowed.
Restricted mode needs an auditable domain/provider allowlist, redirect policy,
download and content-size limits, timeout rules, and defenses against local or
metadata endpoints. Placement must not weaken the coordinator's network policy.

### Credential isolation

Provider credentials must not be embedded in worker prompts or general task
payloads. A production design needs scoped credentials, process isolation,
redaction, rotation, revocation, and an explicit rule for whether retrieval
runs only on the coordinator or inside a trusted executor class.

### Provenance and per-claim citations

Every retained source must have a stable provenance record including provider,
original locator, canonical locator, author/publisher when known, retrieval
time, publication/update time when known, content hash, and transformation
history. Synthesized factual claims must point to one or more specific source
records and, where possible, bounded locations within those records.

### Retrieval timestamps and freshness

The strategy must record when each source was retrieved and how freshness was
evaluated. It must not represent cached or undated evidence as current. Requests
that require current information need an explicit recency policy and a visible
failure when the policy cannot be met.

### Duplicate-source handling

URL variations, mirrors, syndication, press-release copies, and model-generated
restatements must not be counted as independent corroboration. The protocol
needs canonicalization, content hashing or similarity signals, and provenance
relationships that identify derived copies.

### Conflicting evidence

Conflicts must be retained, not averaged away. The strategy needs rules for
source authority, date, primary versus secondary evidence, uncertainty, and
unresolved disagreement. A final answer must expose material conflicts and the
basis for choosing or declining a conclusion.

### Synthesis

Synthesis must consume structured source and claim records, respect the output
contract, and separate directly supported statements from inference. It must
not invent a citation to make unsupported prose look researched.

### Citation validation and verification

Mechanical checks must confirm that cited source ids exist, locators are valid,
quoted text is bounded, claims reference retrieved material, and duplicate
sources are not mislabeled as independent. Higher-assurance modes need sampled
entailment or human review, with the limits of any model-based verifier stated
plainly.

### Privacy implications

Research requests may disclose task text, project memory, source queries,
identifiers, or private documents to providers and workers. The contract needs
data-classification rules, local-only retrieval, retention controls, deletion,
logs that avoid credentials and sensitive contents, and informed operator
configuration before remote placement.

## Consequences

- No `research` identifier is added to `ExecutionRequestV1` in protocol v1.
- Parallel prompting, search snippets, or an LLM bibliography are not accepted
  substitutes for provenance.
- The existing strategy registry and dispatcher remain extension seams, but an
  implementation must first add source, provenance, claim, citation, policy,
  and verification contracts.
- Map, debate, consensus, and research modes remain out of this sprint.
