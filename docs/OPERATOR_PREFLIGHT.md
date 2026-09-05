# Operator pre-flight

Run through this **before** you send anyone an invitation.

The worker installer enforces what it can from the contributor's side: it
refuses plaintext transport, refuses to run as root, never puts a secret on a
command line, and never touches the host's security settings. None of that
protects the coordinator, and none of it protects contributors from *you*
misconfiguring the thing they are connecting to.

That is what this list is for. Everything here is something no program can
check on your behalf.

---

## 1. TLS, because the worker will not join without it

Since the worker refuses plaintext HTTP to any address but loopback, this is no
longer optional and no longer something you can defer. There is no flag on
either side that turns it off.

Keep the application bound to loopback and put a maintained TLS proxy in front
of it. Caddy is the shortest path — it obtains and renews certificates on its
own:

```
mycelium.example.com {
    encode zstd gzip

    # The dashboard streams events over a WebSocket.
    reverse_proxy 127.0.0.1:8000

    request_body {
        max_size 10MB
    }

    # A share URL is a bearer capability. Do not put it in a log.
    log {
        output file /var/log/caddy/mycelium.log
        format filter {
            request>uri query {
                delete token
            }
        }
    }
}
```

Then in `data/config.json`:

```json
{
  "deployment_mode": "trusted_alpha",
  "node_enrollment_mode": "required",
  "private_overlay": false,
  "https_enabled": true,
  "viewer_cookie_secure": true,
  "trust_proxy_headers": false
}
```

Merge those in; do not replace the authority values.

- [ ] A real certificate, from a real CA, that a stock client trusts.
- [ ] HTTP redirects to HTTPS.
- [ ] Port 8000 is **not** reachable from outside the host — otherwise a client
      can simply bypass the proxy and everything above is decoration.
- [ ] WebSocket upgrades reach `/ws/events`.
- [ ] `curl https://your-address/v1/worker-protocol` answers from another
      machine. That is the exact call a joining worker makes first.

### If you were using a private overlay

A Tailscale or WireGuard address is still a network address, so plaintext over
it is now refused. You do not have to abandon the overlay — put TLS on it:
`tailscale cert` issues a certificate for your tailnet name, and Caddy can serve
it. Keep the overlay ACL as well; TLS and the ACL solve different problems.

## 2. Secrets, and where they have already been

- [ ] `node_secret`, `pitch_key`, and `viewer_key` are **three different**
      values, each 32+ random characters.
- [ ] **Rotate every one of them after recording any video or screen share.**
      A key that appeared on screen for one frame is a public key. This is the
      most common way a small project leaks; assume it happened.
- [ ] Check whether a secret was ever committed, at any point in history:

      ```bash
      git log -p --all | grep -iE "node_secret|pitch_key|viewer_key|api[_-]?key|password" | head -50
      ```

      A value in an old commit is still in the repository. Rotate it — deleting
      the line in a later commit changes nothing.
- [ ] `data/config.json` is owner-only (`chmod 600`).
- [ ] Invitation codes go to contributors through a channel you would send a
      password over. Not a public issue, not a Discord channel with 200 people.
- [ ] You know how to revoke one node without disturbing the others:
      `python scripts/node_enrollment_admin.py` — try it once before you need
      it.

## 3. The host

- [ ] Firewall allows **22, 80, 443 only**. Everything else denied inbound.
- [ ] SSH uses keys. `PasswordAuthentication no` in `sshd_config`.
- [ ] Root login over SSH disabled.
- [ ] Unattended security upgrades are on:

      ```bash
      sudo apt install unattended-upgrades && sudo dpkg-reconfigure -plow unattended-upgrades
      ```
- [ ] The coordinator does not run as root.
- [ ] Backups exist **and you have restored one** — `scripts/backup.py` and
      `scripts/restore.py`. An unrestored backup is a hypothesis.

## 4. Before the first invitation

- [ ] `python scripts/preflight.py` passes in `trusted_alpha` mode.
- [ ] `/health` reports `status: ok`, `private_routes_protected: true`, and
      `node_enrollment_required: true`.
- [ ] `/status.json` from a machine that is not yours returns only counts —
      no task text, no hostnames.
- [ ] You have joined your *own* second machine with
      `python worker_installer.py` and watched it complete a task. Do not make
      a contributor the first person to run the install path.
- [ ] You have run `python worker_installer.py uninstall` on that machine and
      confirmed the credential is gone.
- [ ] Contributors get a link to [JOIN.md](JOIN.md), not a command to paste.

## 5. What you are asking people to trust you with

Say this out loud before you invite anyone, because they cannot verify it:

- You can read everything their machine produces.
- You choose the prompts their processor runs.
- You could, in principle, change what your coordinator sends. The worker will
  not execute it — that is
  [tested](../tests/test_contributor_safety.py) — but the protection is in
  their copy of the code, which means it is only as good as their copy being
  the real one.
- If you lose the host, you lose the enrollment table and everybody re-enrolls.

None of that is a reason not to run a network. It is the reason to invite people
who know you, and to say plainly what the arrangement is. See
[THREAT_MODEL.md](THREAT_MODEL.md).

## 6. Things this project deliberately does not do for you

Do not go looking for these; they are not missing by accident.

- No auto-update for workers. A contributor updates when they choose to.
- No telemetry, no crash reporting, no usage analytics.
- No `curl … | bash` install for the guided installer.
- No code signing or notarization.
- No sandbox around generated code. Review it before you run it.
