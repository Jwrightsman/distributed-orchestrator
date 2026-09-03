# ADR 0015 — Worker protocol compatibility window and deprecation policy

**Status:** Accepted (2026-09-03, Theme 4A)

**Context:** ROADMAP §6 lists protocol versioning as a prerequisite for external
operators: "a distributed node population can't be upgraded at once, so
compatibility and deprecation rules must exist *before* external operators depend
on them."

A version value already existed. Theme 2B put `worker_protocol_version` in the
executor descriptor and Theme 3B-1 records it on evidence rows. What did not
exist was any of the things that make a version useful:

* no advertised window a worker could read before enrolling;
* no distinguishable refusal — the field was `Literal["1"]`, so anything else was
  a generic pydantic 422 that told an operator nothing about which side was
  stale;
* no deprecation policy, so "we support version N" had no defined lifetime.

## Decision

### The window

The coordinator advertises an inclusive window of worker protocol versions and
its own version at `GET /v1/worker-protocol`, unauthenticated:

```json
{
  "node_protocol_min": "1",
  "node_protocol_max": "1",
  "supported_worker_protocol_versions": ["1"],
  "server_version": "0.3.0"
}
```

Unauthenticated because a worker has to know whether it will be admitted *before*
it presents a credential, and an operator debugging a refusal should not need an
invite to read the window they fell outside. It carries versions only: no build
fingerprint, no host, no deployment mode, no node or queue counts. Anything that
describes *this deployment* rather than *the protocol* does not belong on a
surface whose entire job is to be readable by strangers.

`NODE_PROTOCOL_MIN` and `NODE_PROTOCOL_MAX` are both 1. **This ADR defines the
mechanism and does not exercise it.** Nothing is bumped here.

### Declaration and refusal

A worker declares its version in `capability_descriptor.executor.worker_protocol_version`.
The field changed from `Literal["1"]` to bounded text so that an unsupported
value reaches the registration route and gets a useful answer instead of a
generic validation error. The window itself is checked in one place,
`worker_protocol.classify`.

| verdict | status | code | action |
| --- | --- | --- | --- |
| below the window | 426 | `worker_protocol_version_too_old` | `upgrade_worker` |
| above the window | 426 | `worker_protocol_version_too_new` | `upgrade_coordinator` |
| not a version token | 422 | `invalid_worker_protocol_version` | — |

Too-old and too-new are deliberately distinct. An operator running behind needs
to upgrade their worker; an operator running ahead needs to know the *coordinator*
is the stale side and that upgrading their worker again will not help. One code
for both would have made the more confusing case the one we handled worse.

Every refusal names the window in its body and in `X-Node-Protocol-Min` /
`X-Node-Protocol-Max` headers. A malformed declaration is never echoed back to
its sender.

### Where checking happens

Twice per registration, never per request:

1. on entry to `POST /nodes/register`, before any enrollment, session, or
   capability snapshot exists;
2. immediately before the session grant, after enrollment has been created or
   authenticated.

**A session established under a supported version stays valid for its lifetime.**
This is a deliberate choice, not an oversight. A coordinator that re-checked on
every poll would drop its entire connected fleet's in-flight work the moment it
moved its own window — turning a routine upgrade into an outage. Version
negotiation is an admission decision; it is not an authorization decision, and it
does not belong on the hot path.

A descriptorless legacy-compatibility registration declares no version and is
left alone. It is already gated by `node_enrollment_mode`, its behaviour is
documented, and refusing it over a version it never claimed would break a
documented path for no gain.

### What counts as a breaking change

A change is **breaking** — and requires `NODE_PROTOCOL_MAX + 1` — if a worker at
the current version would behave incorrectly against a coordinator with the
change, or vice versa. Concretely:

* removing or renaming a field a worker sends or reads;
* changing the meaning, units, or type of an existing field;
* adding a field the coordinator *requires* a worker to send;
* changing when a worker may or must call something, or what a status code means;
* changing attempt binding, settlement admissibility, or the streaming budget.

A change is **not** breaking, and does not move the window, if a worker at the
current version keeps working unchanged: adding an optional field with a
compatible default, adding a new endpoint, widening an accepted range, or
improving an error message.

### Deprecation policy

The project intends to honour this, not to aspire to it:

1. **The window holds at least two versions** once a second exists. An operator
   who is one version behind is never refused.
2. **Announcing.** A version becomes deprecated by being announced in
   `docs/PROTOCOL.md` and the changelog, with the release that will drop it
   named. Deprecated is not refused: a deprecated worker still registers and
   still works.
3. **Warning.** While deprecated, registration succeeds and the response carries
   a deprecation notice naming the version and the release that drops it. An
   operator who never reads a changelog still finds out, from the thing they run.
4. **Dropping.** `NODE_PROTOCOL_MIN` rises only in a release that says it does.
   From then, that version is refused with `worker_protocol_version_too_old` and
   an actionable message.
5. **A version is never dropped in the same release that deprecates it.**

What an operator sees at each stage: nothing (supported), a notice on
registration (deprecated), a 426 naming the window and telling them to upgrade
(dropped). At every stage `GET /v1/worker-protocol` answers "what does this
coordinator accept right now?" without a credential.

## Consequences

* An external operator has a defined answer to "what happens when the other side
  changes", and a place to read it that does not require an invite.
* One integer window, checked twice, documented once. No capability negotiation,
  no per-version feature flags, no content negotiation — those would each turn a
  compatibility question into a combinatorial one.
* The descriptor field is no longer a pinned literal, so an unsupported version
  is now a routing decision rather than a schema error. That is the point, but it
  does mean the shape check moved from pydantic into `worker_protocol.classify`,
  which is therefore tested directly.
* The window advertises versions the coordinator *accepts*. It is not a promise
  about what any particular worker implements, and it is not attestation: a
  worker can declare version 1 and behave badly, exactly as it could before.
