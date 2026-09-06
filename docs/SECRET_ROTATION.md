# Rotating secrets

Four credentials exist in a Mycelium deployment, they break different things
when they change, and they are rotated differently. This is the procedure for
each, plus the two situations that should make you rotate without being asked.

Nothing here prints a credential value, and neither should you.

| Credential | Where it lives | Who holds it | What breaks when it changes |
| --- | --- | --- | --- |
| `node_secret` | `data/config.json` | Whoever you invited, until they finish enrolling | New bootstraps only. **Already-enrolled workers are unaffected** |
| `pitch_key` | `data/config.json` | Whoever submits work | Every pitcher, immediately |
| `viewer_key` | `data/config.json` | You, and anyone you gave read access | Every browser session, immediately |
| Enrollment credential | The worker's own identity file | One worker machine | That one worker, and nobody else |

---

## Rotate the invitation code (`node_secret`)

**The cheapest rotation in the system, and the one to reach for first.** It
admits an *initial* bootstrap and nothing else. Every worker that has already
enrolled uses its own durable credential and a process-local session, so
rotating this does not disconnect a single running node.

Rotate it whenever you are even slightly unsure who has it.

```bash
python3 -c "from config import ensure_trusted_alpha_config as e; e('data/config.json')"
```

That regenerates any authority that is missing, short, or duplicated — and
preserves valid ones. To force this one specifically, empty it first:

```bash
python3 - <<'PY'
import json
from pathlib import Path

path = Path("data/config.json")
settings = json.loads(path.read_text(encoding="utf-8"))
settings["node_secret"] = ""          # forces regeneration, prints nothing
path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
PY
python3 -c "from config import ensure_trusted_alpha_config as e; e('data/config.json')"
docker compose restart orchestrator
```

Then:

```bash
python3 scripts/preflight.py --config data/config.json --state-dir data --mode trusted_alpha --skip-lock-check
python3 scripts/deploy_preflight.py --state-dir data
```

**What the people you invited must do:** anyone who had the old code and had
not finished enrolling needs the new one. Anyone already enrolled does nothing
and notices nothing.

---

## Rotate the pitch key

```bash
# empty pitch_key the same way as above, then regenerate and restart
docker compose restart orchestrator
```

**What breaks:** every pitcher, at once. Anything sending `X-Pitch-Key` gets a
`401` until it has the new value — that includes your own scripts, the MCP
server if you configured it with the key, and any dashboard bookmark that
relied on it.

Send the new value through a secret manager or another channel you would send
a password over. Not a public issue, not a group chat.

---

## Rotate the viewer key

```bash
# empty viewer_key, regenerate, restart
docker compose restart orchestrator
```

**What breaks:** every browser session immediately. The viewer cookie is signed
with the key as HMAC material rather than containing it, so rotating the key
invalidates every outstanding cookie by construction. Everybody logs in again.
Existing share links (`/v1/shares/{token}`) are separate capabilities and are
**not** affected — revoke those individually if you need to.

---

## Rotate one worker's enrollment credential

Use this when one contributor's machine is compromised, sold, or handed on, and
you do not want to disturb anybody else.

```bash
python3 scripts/node_enrollment_admin.py list
```

That prints enrollment metadata and never a credential or a digest. Find the
enrollment id, then:

```bash
python3 scripts/node_enrollment_admin.py rotate ENROLLMENT-ID \
    --coordinator https://YOUR-ADDRESS \
    --identity-output ./new-identity.json
```

The replacement identity is written to that file with owner-only permissions
rather than printed. Get the file to the worker's owner through a secure
channel; they replace their identity file with it and restart `node.py`.

**What breaks:** that worker, until it has the new file. Nobody else.

### Or revoke it outright

```bash
python3 scripts/node_enrollment_admin.py revoke ENROLLMENT-ID \
    --reason "machine sold"
```

The node stops being admitted. Work it was holding is reclaimed and reassigned
automatically. Do not delete history or reuse the label.

**On Path A, revoking the enrollment is only half of removing somebody.**
Tailnet membership and Mycelium enrollment are two independent authorities.
Remove their device from the tailnet as well — see
[DEPLOY.md](DEPLOY.md#removing-somebody).

---

## After recording a video or a screen share

**Rotate everything. Assume it leaked.**

A credential that was on screen for one frame is a public credential. Video
gets paused, scrubbed, downloaded and enhanced, and thumbnails are generated
from frames you did not choose. This is the most common way a small project
leaks a key, and it is entirely avoidable.

Do all of this, in order:

1. **Rotate all three shared authorities**, not just the one you think was
   visible:

   ```bash
   python3 - <<'PY'
   import json
   from pathlib import Path

   path = Path("data/config.json")
   settings = json.loads(path.read_text(encoding="utf-8"))
   for authority in ("node_secret", "pitch_key", "viewer_key"):
       settings[authority] = ""
   path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
   PY
   python3 -c "from config import ensure_trusted_alpha_config as e; e('data/config.json')"
   docker compose restart orchestrator
   ```

2. **Check every frame, not every scene you remember.** Things that end up on
   screen without anyone deciding to put them there: a terminal scrollback
   buffer when you scroll up, an editor tab strip, a browser autocomplete
   dropdown, a notification toast, the dashboard's own URL bar if a share token
   is in it, a `docker compose logs` window, and the moment between opening
   `config.json` and remembering that it is open.

3. **Re-send the new invitation code** to anyone mid-enrolment.

4. **Only then** consider whether the recording is still publishable.

Rotating before you publish is free. Rotating afterwards is a race.

---

## Checking whether a secret was ever committed

Deleting a credential in a later commit changes nothing: the old blob is still
in the repository, still in every clone, and still on GitHub if you pushed.

```bash
python3 scripts/secret_history_scan.py
```

This scans **history** — every blob in the object database, including ones no
branch points at any more — rather than the working tree. It prints locations
and rule names, never values. It knows this project's three authority names,
private key blocks, a handful of API key formats their issuers publish, and
fields whose name says "secret" assigned a value that looks generated.

It is evidence rather than proof: a credential chosen to look like an ordinary
word would slip past it, which is a further reason to let the generator pick
them.

### Two kinds of finding

The report has up to two sections, and they are not the same emergency.

**In this repository's history.** A branch or a tag reaches the blob, or it is
staged in the index and a commit away from the same thing. It is in every
clone, it is on the remote if you have pushed, and it is what the steps below
are for. This is the section that makes the command exit non-zero.

**In unreachable objects.** Nothing reaches the blob. This is mostly what
`git add` leaves behind when you stage a file and then amend the commit away:
the blob is written the moment you stage it, and it outlives the commit that
never happened. `git push` and `git clone` do not transfer these, so the remote
has never had them, and a fresh clone of your repository does not contain them
at all. That last point is why they do not fail the command — a check that goes
red over objects only your machine has is a check you stop believing.

They are still on your disk, and still worth clearing:

```bash
git gc --prune=now
```

If the value was ever pushed on a branch, or shown in a screen share, rotate it
anyway. That an object is unreachable *now* says nothing about where it has
already been.

Where a finding in that second section names a path, git no longer records one:
the path was recovered by matching the blob's content against the files still
in history, and names the file the blob was a draft of. It is a good guess
rather than a fact, which is why an ambiguous match is left unnamed and
reported rather than quietly filed under a path that would have silenced it.

### If it finds something

**Rotate first. Everything else is secondary.**

1. **Rotate the affected credential now**, using the relevant section above. A
   value that was ever pushed is a value strangers have had the opportunity to
   read. Rewriting history does not un-read it.
2. **Re-issue what depends on it.** A new invitation code goes to anyone
   mid-enrolment; a new viewer key logs every browser out; a new pitch key goes
   to whoever pitches.
3. **Revoke any enrollment you are unsure about**, one at a time:

   ```bash
   python3 scripts/node_enrollment_admin.py list
   python3 scripts/node_enrollment_admin.py revoke ENROLLMENT-ID \
       --reason "credential found in git history"
   ```

4. **Only now** decide whether to rewrite history. It is disruptive, it breaks
   every existing clone, and it is the *least* urgent step. If the repository
   is public, treat it as cosmetic: anything already fetched stays fetched.

The order matters because rewriting history feels like fixing the problem and
is not. The credential is the problem.

---

## What rotation does not fix

- **Anything already read.** Rotation closes a door; it does not recover what
  went through it. Task text, results and artifacts that a holder of the old
  key could read, they read.
- **A compromised host.** If someone had shell access to the coordinator they
  had `data/config.json`, `events.db`, and every enrollment record. Rotating
  credentials on a machine you do not trust is rotating them into the same
  attacker's hands. Rebuild the host first.
- **Trust.** Losing the host loses the enrollment table, and everybody
  re-enrols. Say so to your contributors rather than letting them discover it.

See [THREAT_MODEL.md](THREAT_MODEL.md) for what each of these credentials does
and does not protect.
