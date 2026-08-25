# Security policy

## Reporting a vulnerability

**Email <wrightsmanjett@gmail.com>.** Put "SECURITY" in the subject line.

Do not open a public issue for anything exploitable. Public issues are the right
place for everything else, including things that merely look alarming — if you
are unsure which you have, email it and it can always be moved into the open.

Useful things to include, none of them required:

- What the problem is, and what an attacker gets out of it
- How to reproduce it — a command, a request, a short script
- Which version you tested (`git rev-parse HEAD`, or the `build` value from
  `/status.json` on a running orchestrator)
- Whether you think it is already covered by
  [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)

## What to expect back

This is a project run by **one person**, not a company with a security team.
Being honest about that up front is more useful than a response-time promise
that gets broken:

| | |
| --- | --- |
| **First reply** | Within about 7 days. If you have not heard back in 14, email again — assume it got lost, not ignored |
| **Assessment** | A plain-language answer: whether it reproduces, whether it is already a known limitation, and what happens next |
| **Fix** | For something exploitable in a normal deployment, as fast as one person can. For a hardening item, it goes on the roadmap with the reasoning written down |
| **Credit** | Your name in the commit and the release note, if you want it. Say if you would rather stay anonymous |
| **Disclosure** | Coordinated. Roughly 90 days, or as soon as a fix ships, whichever is first. If you have a deadline, say so and it will be respected |

There is **no bug bounty** and no money involved. Nothing here is a legal
agreement.

## Before you report: much of it is already written down

[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) describes what this system does
and does not defend against **today**. A lot of what looks like a vulnerability
is a documented Phase 0 limitation, and saying so is not a brush-off — the
whole point of writing the threat model down was to make the honesty checkable.

Known and documented, so probably not news:

- **`node_secret` is shared bootstrap admission, not per-node identity.** Each
  enrolled worker has a separate digest-only bearer credential and independent
  revocation, but an admitted worker can still return arbitrary model output.
  Enrollment is not physical-machine identity, attestation, or Sybil defense.
- **Local compatibility mode deliberately fails open.** When `viewer_key` is
  empty, `/history`, `/events`, `/gallery`, `/ledger`, and other private routes
  remain readable for loopback development. Trusted-alpha preflight requires a
  separate viewer authority and durable enrollment; do not expose local mode.
- **Worker nodes see the text of the tasks they are given.** Inherent — you
  cannot ask a machine to work on a prompt without showing it the prompt.
- **Generated code is not sandboxed** and must not be executed unreviewed.
- **No HTTPS out of the box.** Bearer admission, enrollment, session, pitch,
  viewer, and share credentials require TLS or an authenticated private overlay.
- **Credits are contribution points, not currency.** No token, no wallet, no
  monetary value, so no credit bug costs anyone money.

**Still worth reporting even though they are listed:** a way to make any of
these *worse* than documented — reading data the threat model says is private,
getting a node to execute something, getting credit settled for work not done,
bypassing `pitch_key` on a write endpoint, causing remote code execution on the
orchestrator, or extracting any admission, enrollment, session, pitch, viewer,
attempt, or share credential from outside the box.

**Definitely report** anything the threat model does not mention at all. A gap
in that document is itself the finding.

## Scope

**In scope:** this repository, and the orchestrator, node, MCP server and
installers it contains.

**Out of scope:** the model's *output* — that a 4B model writes wrong or
insecure code is a measured quality property (about 57% of eval tasks come back
runnable, and that number is published precisely so nobody is surprised), not a
vulnerability. Also out of scope: third-party dependencies, which should go to
their own projects, and Ollama itself.

**Please do not** test against the public orchestrator or anyone else's node
without asking first. Run your own — `docs/DEPLOY.md` Path 1 takes a few
minutes on a laptop — and attack that instead. Testing against a stranger's
hardware is the one thing that would make this project's contributors
regret opting in.

## Supported versions

`master` only. This is Phase 0: there are no release branches and no backports.
Fixes land on `master` and deployments update with a `git pull` and rebuild —
`python scripts/verify_deploy.py <url>` will tell you whether a given
orchestrator is actually running the code you think it is.
