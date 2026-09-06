# Operator pre-flight

Run through this **before** you send anyone an invitation.

Most of what used to be prose on this page is now a program. Run it first:

```bash
python3 scripts/deploy_preflight.py --state-dir data --url https://YOUR-ADDRESS
```

It is read-only — it changes nothing, installs nothing, and prints no
credential value. Every finding it reports says in plain language what goes
wrong if you ignore it, and names the exact command that fixes it. From a
Compose deployment you can run it out of the container instead, which needs
nothing installed on the host:

```bash
docker compose exec orchestrator python /app/scripts/deploy_preflight.py --state-dir /data
```

**What the script checks, so you do not have to:** which ports are actually
listening on a public interface; whether the coordinator's own port is among
them; SSH password authentication and root login; unattended security upgrades;
the state directory's permissions; whether the config or the database is
readable by other accounts; whether any of the three authorities is empty, a
placeholder, or too weak; whether the certificate is valid and not near expiry;
and whether `/v1/worker-protocol` answers over HTTPS.

**What is left on this page** is everything no program can check on your
behalf. That is the rest of this document, and it is the part that matters.

---

## 1. Things a program cannot see

- [ ] **The protocol window answered from a machine that is not yours.** The
      script's own check runs on the coordinator, where a request can succeed
      through a route a stranger does not have — a hosts entry, a
      split-horizon DNS answer, a router folding the connection back inside.
      Ask somebody else, on somebody else's network, to run:

      ```bash
      curl -sS https://YOUR-ADDRESS/v1/worker-protocol
      ```

- [ ] **Your certificate is trusted, checked before you invite anybody.**

      ```bash
      python3 scripts/tls_local_check.py --cert CERT --key KEY --name YOUR-NAME
      ```

      This serves your certificate to a fully verifying client on your own
      loopback interface and reports whether a worker would accept it. Nothing
      leaves the machine. A self-signed certificate cannot be made to work —
      a worker trusts the certifi bundle and nothing else — and this is where
      you find that out, rather than during somebody's install.

- [ ] **WebSocket upgrades reach `/ws/events`.** Open the dashboard and confirm
      the event feed is live rather than polling every three seconds.

- [ ] **On Path A: the tailnet ACL is what you think it is.** `private_overlay`
      in the configuration is an assertion by you. Nothing inspects a Tailscale
      or WireGuard ACL, and a contributor cannot audit it either.

- [ ] **You have joined your own second machine** with
      `python worker_installer.py` and watched it complete a task. Do not make
      a contributor the first person to run the install path.

- [ ] **You have run `python worker_installer.py uninstall`** on that machine
      and confirmed the credential is gone.

- [ ] **Backups exist and you have restored one** — `scripts/backup.py` and
      `scripts/restore.py`. An unrestored backup is a hypothesis.

- [ ] **Contributors get a link to [JOIN.md](JOIN.md)**, not a command to paste.

## 2. Where your secrets have already been

The script checks that the three authorities are strong and distinct. It cannot
check where they have been.

- [ ] **Rotate every one of them after recording any video or screen share.** A
      key that appeared on screen for one frame is a public key. This is the
      most common way a small project leaks; assume it happened. The procedure,
      including which frames people forget to check, is in
      [SECRET_ROTATION.md](SECRET_ROTATION.md#after-recording-a-video-or-a-screen-share).

- [ ] **Check whether a secret was ever committed** — history, not the working
      tree:

      ```bash
      python3 scripts/secret_history_scan.py
      ```

      If it finds something: rotate first, then worry about history. A value in
      an old commit is still in the repository, and deleting the line in a
      later commit changes nothing. See
      [SECRET_ROTATION.md](SECRET_ROTATION.md#if-it-finds-something).

      A second section, where it appears, lists unreachable objects — leftovers
      on this machine that no push and no clone carries. Those do not fail the
      command, and `git gc --prune=now` clears them. See
      [SECRET_ROTATION.md](SECRET_ROTATION.md#two-kinds-of-finding).

- [ ] **Invitation codes go to contributors through a channel you would send a
      password over.** Not a public issue, not a Discord channel with 200
      people.

- [ ] **You know how to revoke one node without disturbing the others** —
      `python scripts/node_enrollment_admin.py`. Try it once before you need
      it. On Path A remember that removing a device from your tailnet and
      revoking its enrollment are two separate things and you need both.

## 3. Deployment shape

- [ ] `python scripts/preflight.py --mode trusted_alpha` passes. That is the
      *configuration* preflight; `deploy_preflight.py` is the *host* one. Run
      both.
- [ ] `/health` reports `status: ok`, `private_routes_protected: true`, and
      `node_enrollment_required: true`.
- [ ] `/status.json` from a machine that is not yours returns only counts — no
      task text, no hostnames.
- [ ] The coordinator does not run as root.
- [ ] You have read [The trap](DEPLOY.md#the-trap-docker-does-not-consult-ufw)
      and understand that `ufw status` cannot tell you whether a Docker-published
      port is exposed.

## 4. What you are asking people to trust you with

Say this out loud before you invite anyone, because they cannot verify it:

- You can read everything their machine produces.
- You choose the prompts their processor runs.
- You could, in principle, change what your coordinator sends. The worker will
  not execute it — that is
  [tested](../tests/test_contributor_safety.py) — but the protection is in
  their copy of the code, which means it is only as good as their copy being
  the real one.
- Nothing rate-limits guesses at your invitation code. Its entropy is the whole
  defence, and a failed attempt is not logged anywhere you would notice. See
  [DEPLOY.md](DEPLOY.md#registration-is-not-rate-limited).
- If you lose the host, you lose the enrollment table and everybody re-enrols.
- **This deployment configuration has not been reviewed by a security
  professional.** If you are inviting people beyond your immediate circle, get
  one.

None of that is a reason not to run a network. It is the reason to invite people
who know you, and to say plainly what the arrangement is. See
[THREAT_MODEL.md](THREAT_MODEL.md).

## 5. Things this project deliberately does not do for you

Do not go looking for these; they are not missing by accident.

- No auto-update for workers. A contributor updates when they choose to.
- No telemetry, no crash reporting, no usage analytics.
- No `curl … | bash` install at all. There were two — `install.sh` and
  `install.ps1` — and they were deleted on 2026-09-05. Contributors clone the
  repository and run `python worker_installer.py` out of it, so they have
  something to read before they run it. Do not hand anybody a one-liner.
- No code signing or notarization.
- No sandbox around generated code. Review it before you run it.
- No rate limit on `POST /nodes/register`. Recorded as a known gap rather than
  invented in a hurry; see [DEPLOY.md](DEPLOY.md#registration-is-not-rate-limited).
