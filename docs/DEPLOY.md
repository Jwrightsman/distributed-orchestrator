# Deployment

This guide assumes you have never administered a server. Every command is
written out in full and every command is explained. Work through it in order.

> **This configuration has not been reviewed by a security professional.**
> It was written carefully and it is tested, but that is not the same thing. If
> you are inviting people beyond your immediate circle — anyone you would not
> comfortably phone about a problem — get somebody who does this for a living
> to look at your deployment first. Nothing below is a substitute for that.

---

## Before anything else: TLS is not optional

Since 2026-09-05 a Mycelium worker **refuses plaintext `http://` to any address
except its own machine**. There is no flag, no environment variable and no
configuration key that relaxes it. A contributor literally cannot connect to
you until you are serving HTTPS.

That is deliberate. A worker sends its invitation code to your coordinator and
receives task text back; over plaintext both are readable, and rewritable, by
anything on the network path. Whether that is acceptable is not a judgement a
contributor is in a position to make about somebody else's server, so the
software makes it for them.

One consequence catches people out, so read it before you generate anything:

> **A self-signed certificate cannot be made to work.** A worker trusts the
> certifi CA bundle and nothing else, and it builds its HTTP client with
> `trust_env=False`, so `SSL_CERT_FILE` and friends are ignored — a private CA
> cannot be added to a contributor's machine even if they wanted to. Both paths
> below produce a certificate from a public CA, which is why both work.

---

## The two paths

| | **Path A — private overlay** | **Path B — public domain** |
| --- | --- | --- |
| Who can reach it | Only devices on your tailnet | Anyone on the Internet |
| Certificate | `tailscale cert` (Let's Encrypt) | Caddy (Let's Encrypt), automatic |
| Costs | Free for personal use | ~$12/year for a domain |
| Contributor installs | Tailscale, then Mycelium | Mycelium only |
| You must manage | A tailnet ACL and a device list | A public attack surface |
| Right for | A first invited node; a handful of friends | The launch |

**Start with Path A.** Not because it is easier — it is slightly harder, since
your contributor has to install one more thing — but because it removes the
entire category of problem where a stranger can reach your coordinator at all.
The port is on a private network; there is nothing on the public Internet to
find, scan, or guess at. For a first invited node, that is worth more than the
convenience of a public address.

Move to Path B when you want people to be able to join without you adding their
device to anything first — which is the launch, and not before.

You can run both later: Caddy can serve a tailnet name and a public domain from
one configuration file. Do not try to do that first.

---

# Path A — private overlay (Tailscale)

## A1. What you are building

```
   contributor's laptop                    your server
   ┌──────────────────┐                  ┌──────────────────────────┐
   │ Tailscale        │                  │ Tailscale                │
   │ Mycelium worker  │═══ tailnet ═════▶│ Caddy  :443  (TLS)       │
   └──────────────────┘   encrypted,     │   │                      │
                          private        │   ▼ loopback only        │
                                         │ Mycelium  127.0.0.1:8000 │
                                         └──────────────────────────┘
```

Two independent things protect the coordinator, and you need both:

- **The tailnet** decides *which machines can reach the address at all*.
- **TLS** protects what they send once they can.

Neither replaces the other. An overlay ACL is an assertion by you that a
contributor cannot verify; TLS is something their own software checks.

## A2. Install Tailscale on the server

Tailscale publishes a one-line installer that pipes a download straight into a
shell. This project does not hand those out — it deleted its own for the same
reason — so use the package repository instead. It is three lines rather than
one, and every line is something you can read before it runs:

```bash
curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/$(lsb_release -cs).noarmor.gpg" | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/$(lsb_release -cs).tailscale-keyring.list" | sudo tee /etc/apt/sources.list.d/tailscale.list
sudo apt-get update && sudo apt-get install -y tailscale
```

Then bring it up and sign in with the link it prints:

```bash
sudo tailscale up
```

Find the name your tailnet gave this machine. It always ends in `.ts.net`:

```bash
tailscale status --json | grep DNSName
```

Write that name down — call it `YOUR-HOST.YOUR-TAILNET.ts.net` below. It is the
address you will eventually give contributors, with `https://` in front.

## A3. Enable HTTPS in your tailnet

Certificates for tailnet names are off until you turn them on, once, in the
admin console: **[login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns)
→ HTTPS Certificates → Enable HTTPS**.

Then, on the server:

```bash
sudo tailscale cert YOUR-HOST.YOUR-TAILNET.ts.net
```

This writes two files into the current directory: a `.crt` and a `.key`. They
come from Let's Encrypt, which means every stock client already trusts them —
that is what makes this work where a self-signed certificate would not.

Put them somewhere Caddy can read and nobody else can:

```bash
sudo mkdir -p /var/lib/caddy
sudo mv YOUR-HOST.YOUR-TAILNET.ts.net.crt /var/lib/caddy/
sudo mv YOUR-HOST.YOUR-TAILNET.ts.net.key /var/lib/caddy/
sudo chown caddy:caddy /var/lib/caddy/YOUR-HOST.YOUR-TAILNET.ts.net.*
sudo chmod 600 /var/lib/caddy/YOUR-HOST.YOUR-TAILNET.ts.net.key
```

**Check the certificate before you go any further.** This is the step that
saves you from debugging a handshake over text message while a friend's install
fails:

```bash
python3 scripts/tls_local_check.py \
  --cert /var/lib/caddy/YOUR-HOST.YOUR-TAILNET.ts.net.crt \
  --key /var/lib/caddy/YOUR-HOST.YOUR-TAILNET.ts.net.key \
  --name YOUR-HOST.YOUR-TAILNET.ts.net
```

It serves that certificate to a fully verifying client on your own loopback
interface and tells you whether a worker would accept it. Nothing leaves the
machine. If it says `ACCEPTED`, contributors will get past the certificate.

> `tailscale cert` renews when you run it again. Renewal is **not** automatic
> the way Caddy's own issuance is, so put it in cron:
> ```bash
> echo '0 3 * * * root tailscale cert YOUR-HOST.YOUR-TAILNET.ts.net --cert-file /var/lib/caddy/YOUR-HOST.YOUR-TAILNET.ts.net.crt --key-file /var/lib/caddy/YOUR-HOST.YOUR-TAILNET.ts.net.key && systemctl reload caddy' | sudo tee /etc/cron.d/mycelium-cert
> ```

## A4. Start the coordinator

Get the code and run the deployment script. Clone first and run second, so you
can read what you are about to run:

```bash
git clone https://github.com/Jwrightsman/distributed-orchestrator
cd distributed-orchestrator
MYCELIUM_PRIVATE_OVERLAY_CONFIRMED=1 ./deploy.sh
```

`MYCELIUM_PRIVATE_OVERLAY_CONFIRMED=1` is you asserting that this host is
already on the overlay. The script cannot check that. Do not set it until
step A2 is done.

The script installs Docker if it is missing, generates the three credentials,
runs the configuration preflight, starts the containers, pulls the model, and
refuses to report success until `/health` is actually healthy. It never prints
a credential value, even under shell tracing.

The coordinator is now listening on `127.0.0.1:8000` and **nowhere else**. That
is deliberate and is covered in [The trap](#the-trap-docker-does-not-consult-ufw)
below.

## A5. Put Caddy in front

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

Copy the configuration this repository ships, and edit the two placeholder
names in it:

```bash
sudo cp deploy/Caddyfile.tailscale /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile
```

Check it parses before you load it — this catches typos while the old
configuration is still running:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## A6. Add a contributor

Two separate things have to happen, and **they are two independent
authorities**:

1. **Add their device to your tailnet.** Send them a tailnet invite from
   [login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines).
   They install Tailscale and accept. This decides whether their machine can
   reach your address at all.
2. **Send them an invitation code.** That is the `node_secret` from
   `data/config.json`, sent through a channel you would send a password over.
   This decides whether they can enrol once they can reach you.

Then send them [JOIN.md](JOIN.md) and the address
`https://YOUR-HOST.YOUR-TAILNET.ts.net`.

### Removing somebody

**Both authorities must be revoked, and neither one does the other's job.**

```bash
# 1. Revoke their Mycelium enrollment (does not touch the tailnet)
python3 scripts/node_enrollment_admin.py list
python3 scripts/node_enrollment_admin.py revoke ENROLLMENT-ID --reason "left the project"
```

```bash
# 2. Remove their device from the tailnet (does not touch the enrollment)
#    Do this in the admin console: login.tailscale.com/admin/machines
```

Removing the device from your tailnet stops that machine reaching the address.
It does **not** revoke the enrollment credential, which is a bearer token: if
that machine ever regains tailnet access — a re-invite, a shared account, a
device they still control — the credential still works. Equally, revoking the
enrollment does not remove them from your tailnet, where they can still reach
the address and anything else on it.

Do both. Every time.

---

# Path B — public domain (Caddy)

Use this for the launch, once you want people to be able to join without you
adding their device to anything.

## B1. Point a domain at the server

Buy a domain, then create one DNS **A record** pointing at your server's public
IPv4 address. If you have IPv6, add an **AAAA** record too. Wait for it to
propagate — usually minutes:

```bash
dig +short YOUR-DOMAIN.example.com
```

That must print your server's address before you continue. Caddy proves
ownership of the name over HTTP, so a wrong record means no certificate.

## B2. Start the coordinator

Identical to [A4](#a4-start-the-coordinator), except that this host is not on
an overlay, so the assertion is different. Deploy without it and then set the
HTTPS fields:

```bash
git clone https://github.com/Jwrightsman/distributed-orchestrator
cd distributed-orchestrator
mkdir -p data
chmod 700 data
python3 -c "from config import ensure_trusted_alpha_config as e; e('data/config.json', ollama_url='http://ollama:11434')"
```

`chmod 700` is not decoration. SQLite creates `events.db` at mode 0644 and
nothing in this project can change that, so the directory is what keeps other
accounts on the host away from the enrollment table and every run's output.
`deploy.sh` does this for you on Path A.

Then merge these four settings into `data/config.json`. Do not replace the
credential values the generator just wrote:

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

`trust_proxy_headers` stays `false`. The coordinator does not consume proxy
headers, and preflight treats `true` as an error rather than a preference — see
[What the proxy changes](#what-the-proxy-changes-about-what-the-application-sees).

Then:

```bash
python3 scripts/preflight.py --config data/config.json --state-dir data --mode trusted_alpha
docker compose up -d --build
docker compose exec ollama ollama pull qwen3.5:4b
```

## B3. Put Caddy in front

Install Caddy exactly as in [A5](#a5-put-caddy-in-front), then:

```bash
sudo cp deploy/Caddyfile.public /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile      # set your domain and your email
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy now obtains a Let's Encrypt certificate on its own, renews it on its own,
and redirects `http://` to `https://` with no configuration from you. Watch it
happen the first time:

```bash
sudo journalctl -u caddy -f
```

## B4. Open only what the proxy needs

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

Port 80 has to stay open: Caddy uses it both to renew the certificate and to
redirect visitors to HTTPS.

Now read the next section, because that firewall does less than it appears to.

---

## The trap: Docker does not consult ufw

This is the single most likely way a Mycelium deployment ends up exposed while
its owner believes it is not.

When Docker publishes a container port, it writes its own rules into an
iptables chain called `DOCKER`. The kernel evaluates that chain **before** the
chain ufw manages. So if a container publishes port 8000 to all interfaces:

```bash
sudo ufw deny 8000
sudo ufw status         # says: 8000  DENY  Anywhere
```

…ufw reports `DENY`, and port 8000 keeps answering the entire Internet. The
firewall is not broken and it is not lying about its own rules — its rules are
simply never reached. **Nothing in `ufw status` will ever reveal this.**

Two commands tell you the truth. Ask the kernel what is actually listening:

```bash
ss -tlnp | grep :8000
```

A safe deployment prints `127.0.0.1:8000` — loopback, this machine only. If it
prints `0.0.0.0:8000` or `*:8000`, the port is on the Internet right now.

And ask Docker what it published:

```bash
sudo iptables -L DOCKER -n
```

A safe deployment has no rule for 8000 there.

### How this repository avoids it

`docker-compose.yml` publishes the port as a literal:

```yaml
    ports:
      - "127.0.0.1:8000:8000"
```

The host side is pinned to loopback, so the socket is never created on a public
interface and there is nothing for a firewall to fail to block. This used to be
`${MYCELIUM_PUBLISH_ADDRESS:-127.0.0.1}:8000:8000` — a safe default with an
override that re-armed the trap from a `.env` file nobody reviews. The variable
is gone. Both paths above put a reverse proxy on the public socket instead, so
nothing needs it.

If you change that line, you are opting back into the trap. Do not.

---

## What the proxy changes about what the application sees

Behind a reverse proxy, every request arrives at the application from
`127.0.0.1`, because the proxy is the thing connecting to it. The application
sees the proxy, not the visitor.

The coordinator does not consume `X-Forwarded-For`. `trust_proxy_headers`
exists in the configuration, defaults to `false`, and trusted-alpha preflight
rejects `true` outright. Caddy still sets the header — that is its default and
it is harmless — but nothing reads it. This is deliberate: a forwarded header
is client-controlled unless every hop is verified, and RC1 does not verify hops.

Two things consequently behave differently behind a proxy, and both are worth
knowing before they surprise you:

**Open-mode idempotency scoping.** When no `pitch_key` is configured, canonical
submissions with an `Idempotency-Key` are scoped to the direct peer address
(`request.client.host`, in `routes_executions.py`), and forwarding headers are
deliberately ignored — there is a test asserting exactly that. Behind a proxy
every caller shares the address `127.0.0.1`, so they all collapse into **one**
idempotency scope: two different people sending the same key and the same task
would see one another's execution replayed.

This does not affect either path documented here, because both configure a
`pitch_key`, and a configured pitch key makes the scope the key rather than the
address. It matters only if you run open mode behind a proxy, which you should
not do. The scope was never authorization or identity — the threat model
already says so — but behind a proxy it stops being a useful duplicate boundary
too.

**The pitch rate limiter.** `_check_rate_limit` also buckets by
`request.client.host`. Behind a proxy that is one bucket for everybody, so the
limit becomes global rather than per-visitor: five pitches per minute across
all callers, not five each. For an invited alpha that is acceptable and
arguably what you want. For a public launch with `public_pitch` enabled, know
that one enthusiastic visitor can consume the whole allowance.

---

## Verify it, in this order

Each step assumes the previous one passed.

**1. The certificate, before the proxy is even wired up** (Path A):

```bash
python3 scripts/tls_local_check.py --cert CERT --key KEY --name YOUR-NAME
```

**2. The host, before you invite anybody:**

```bash
python3 scripts/deploy_preflight.py --state-dir data --url https://YOUR-ADDRESS
```

This is read-only — it changes nothing and prints no credential value. It
checks which ports are actually listening publicly, whether the coordinator's
own port is among them, SSH password login and root login, unattended security
upgrades, the state directory and database permissions, whether any of the
three authorities is empty, a placeholder, or weak, whether the certificate is
valid and not near expiry, and whether the protocol window answers over HTTPS.
Every finding names the command that fixes it.

Running it from the container works too, and needs nothing installed on the
host:

```bash
docker compose exec orchestrator python /app/scripts/deploy_preflight.py --state-dir /data
```

**3. From a machine that is not yours.** The preflight says so itself, because
a request made on the server can succeed through a route a stranger does not
have. Ask somebody to run:

```bash
curl -sS https://YOUR-ADDRESS/v1/worker-protocol
```

That is the exact unauthenticated call a joining worker makes first.

**4. Join your own second machine** before a contributor is ever the first
person to run the install path:

```bash
python3 worker_installer.py
```

**5. Work through [OPERATOR_PREFLIGHT.md](OPERATOR_PREFLIGHT.md)** for the
things no program can check for you.

---

## Local-only development

`local` is the compatibility default and needs no configuration:

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Or with Compose, which publishes to loopback:

```bash
docker compose up -d --build
```

Empty credentials keep the historical single-machine workflow. Startup and
`/health` both warn that private routes are unprotected. Do not read
compatibility mode as a secure LAN default — shared or public Wi-Fi is outside
that boundary. `node_enrollment_mode=compat` is local-only, and any legacy
worker session is represented as unenrolled.

---

## The three authorities

A trusted-alpha deployment needs three independent random values. Each must be
at least 32 characters and all three must differ.

| Configuration field | Authority granted to every holder |
| --- | --- |
| `viewer_key` | Read private executions, results, projects, nodes, artifacts and operator routes; administer shares |
| `pitch_key` | Submit work that consumes coordinator and worker compute |
| `node_secret` | Bootstrap a previously unused durable worker enrollment |

Do not reuse one value for two roles. Give each person or machine only the
authority it needs.

### How strong they have to be, and why

`deploy.sh` generates each one with `secrets.token_urlsafe(32)` — 32 bytes from
the operating system's cryptographic random source, about **256 bits**,
rendered as 43 URL-safe characters. That is the standard to meet.

The floor is **128 bits of real entropy**, which is what
`scripts/deploy_preflight.py` enforces. `config.py` separately enforces a
32-character minimum, but length alone is not strength: 32 repeated characters
is 32 characters.

`node_secret` deserves the most care of the three, because **nothing rate-limits
guesses at it** — see [Registration is not rate-limited](#registration-is-not-rate-limited)
below. Its strength is the entire defence.

Do not invent one yourself. A phrase you can remember is a phrase with far less
entropy than its length suggests, and the preflight refuses it for that reason.
Let the generator choose:

```bash
python3 -c "from config import ensure_trusted_alpha_config as e; e('data/config.json')"
```

Valid existing credentials are preserved; missing, short or duplicate ones are
replaced. Values are never printed. Move individual values out of
`data/config.json` through a secret manager or another secure channel — never
paste the whole file into chat, a ticket, or a log.

See [SECRET_ROTATION.md](SECRET_ROTATION.md) for changing them later, and for
what breaks when you do.

---

## Registration is not rate-limited

`POST /nodes/register` has **no rate limit**. This is a real gap and it is
recorded here rather than papered over.

The coordinator does have a per-IP limiter — `_check_rate_limit` in
`server_state.py`, configured by `pitch_rate_max` and `pitch_rate_window` — but
it is wired only into the pitch routes and `POST /v1/executions`. The
registration route does not call it. An attacker who can reach the coordinator
can therefore try invitation codes as fast as the network allows.

Three things stand between that and an enrolment:

- **The secret's entropy.** A 256-bit generated value is not reachable by
  guessing. This is the whole defence, which is why the section above insists
  on the generator.
- **Path A.** On a private overlay, an attacker cannot reach the route at all
  without first being on your tailnet.
- **Nothing durable is created by a failure.** A refused bootstrap leaves no
  enrolment, no session and no capability snapshot, so attempts accumulate no
  partial state.

**A failed bootstrap is also not logged in any way you would notice.**
`_check_node_auth` raises a plain `401` with no application log line, no event
on the event stream and no counter. The only trace is the access log — uvicorn's
`POST /nodes/register 401`, and Caddy's equivalent:

```bash
docker compose logs orchestrator | grep "nodes/register"
sudo grep "nodes/register" /var/log/caddy/mycelium.log
```

If you see a run of 401s against that path from an address you do not
recognise, rotate `node_secret` — see [SECRET_ROTATION.md](SECRET_ROTATION.md).

Adding a limiter was deliberately left out of this change: the existing
mechanism is pitch-scoped, and coupling registration to the pitch bucket would
change worker-protocol behaviour — stock workers re-register automatically after
a session expires — which is not something to do as a side effect of a
deployment guide. It belongs in its own change, with its own tests.

---

## Joining

The owner of each worker machine must explicitly consent before it joins:

```bash
python worker_installer.py
```

The guided installer asks for the address and the invitation code, describes
the model download and the CPU, RAM and disk cost in plain language, names
every file it will write, and waits for the owner. It performs the bootstrap
enrollment itself, so **the shared secret is never passed as a command-line
argument** — it is typed with echo off, or read from a file whose permissions
are checked first. Afterwards the machine has its own revocable credential and
starts with `python node.py --server https://ADDRESS` and no secret at all.

`python join.py https://ADDRESS` remains for existing scripted setups, and takes
the invitation code the same three ways:

| | |
| --- | --- |
| `--ask-secret` | Prompts with echo off. Nothing reaches argv or shell history. |
| `--secret-file PATH` | Reads it from a file that must be readable only by the running user. |
| `--secret VALUE` | Still works, still exposed: any other user on the machine can read it with `ps`, and the shell records the line in history. It prints a warning saying exactly that. |

**There is no `curl … | bash` installer.** `install.sh` and `install.ps1` were
deleted on 2026-09-05. Piping a download into a shell gives the person running
it no point at which to read what is about to run, which is the wrong default
for software whose entire request is "lend me your computer". Contributors
clone the repository and run the installer out of it.

To leave, `python worker_installer.py uninstall` drains from the coordinator and
removes the credential. An agent must not bypass the consent gate on someone
else's machine.

Pass a coordinator origin only — no path, query, user information or fragment.
The stock worker intentionally ignores ambient `HTTP_PROXY`/`HTTPS_PROXY`
settings so enrollment and session bearers cannot be inherited by an unreviewed
proxy. That is the same `trust_env=False` that makes a private CA impossible;
the two consequences come from one decision.

Before a consented join, the worker owner may set `model` and a strict
`worker_capability_overrides` object in the worker checkout's `config.json`.
`join.py` uses those when it starts `node.py`. For a direct start,
`node.py --model MODEL --capability-overrides PATH` layers a bounded JSON file
over config; `--capabilities` remains for legacy string tags. Override fields
are limited to hardware claims, typed features, executor version, model context
and variant, and maximum context. There is deliberately no model-digest override
and no serial/MAC/device-identifier field.

The worker advertises one immutable descriptor per process session. CPU,
architecture, memory, bounded GPU details and exact Ollama metadata are
best-effort detections; missing values stay unknown. All detected and overridden
values remain self-reported claims, not attestation or trust. Drain and stop the
worker, then start a new session, before changing its claim.

---

## Configuration preflight

`scripts/preflight.py` checks the *configuration*.
`scripts/deploy_preflight.py` checks the *host*. Run both.

```bash
python scripts/preflight.py --config data/config.json --state-dir data --mode trusted_alpha
python scripts/preflight.py --json
```

Preflight returns nonzero for an unsafe trusted-alpha deployment and never
prints credential values. It validates JSON, deployment mode, authority length
and separation, required durable enrollment, declared TLS/private-overlay
transport, public-pitch acknowledgement, HTTPS/cookie coherence, writable
SQLite/artifact/output/project paths, database integrity when one exists, and
availability of the single-coordinator OS lock. `private_overlay=true` is an
operator assertion; preflight cannot inspect Tailscale or WireGuard ACLs.

An active coordinator legitimately holds the lock. Use `--skip-lock-check` only
to validate other settings while it is running; the result warns that the lock
probe was skipped. Stop the coordinator and run the full command before a
restore or a controlled restart.

The generator writes `.mycelium-trusted-alpha` beside the config. This
out-of-band marker makes a later missing or malformed JSON file fail closed
instead of silently reverting to local defaults.

---

## Viewer login and health

Private APIs accept `X-Viewer-Key`, `Authorization: Bearer`, or a signed
HttpOnly viewer cookie:

```text
POST /v1/viewer/session
{"viewer_key":"..."}
```

The cookie is signed, short-lived, and does not contain the static key.
Rotating `viewer_key` invalidates existing viewer cookies.

Two public, sanitized checks:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/status.json
```

`/health` is deployment-ready only when `status` is `ok` and
`private_routes_protected` is `true`. With viewer authorization, private
`GET /v1/operator/health` reports the process `instance_id`, deployment mode,
preflight warnings and whether the coordinator lock is held. It contains no
credential values.

If you used `deploy/Caddyfile.public`, `/dashboard`, `/v1/operator/*` and
`/metrics` return 404 from the public address by design — reach them through an
SSH tunnel so the viewer key never crosses the Internet:

```bash
ssh -L 8000:127.0.0.1:8000 you@YOUR-SERVER
```

Then open `http://127.0.0.1:8000/dashboard` in your own browser.

---

## Optional capability-evidence shadow mode

Set `capability_evidence_mode` to `shadow` only when you want protected
counterfactual diagnostics. The coordinator records scoped operational evidence
in either mode; shadow mode additionally evaluates already hard-eligible
candidates after the real assignment. It freezes their assignment-time scopes,
never waits for evidence, and cannot rank, reorder, exclude or replace
production work. There is no active evidence-routing mode, and `verify_rate`
remains an independent default-off sampled-comparison setting.

```bash
curl -fsS -b viewer.cookies \
  "$BASE_URL/v1/operator/capability-evidence?limit=100&evidence_role=production"
```

The response reports the configured minimum, cold scopes as
`insufficient_evidence`, and `affects_routing: false`. Agreement means bounded
output-shape agreement, not correctness or trust. Descriptor, selected model,
task class and evidence-role changes create separate scopes. Only server-owned
lease-expiry and stale-node terminal causes are attributed to workers.

This endpoint is private operational inventory. It returns aggregates rather
than raw observations, and the evidence store omits prompt/output bodies, worker
error text, free-form reasons, credentials, nonces and session secrets. The rows
still live in `events.db` and are included in backups.

---

## Optional distributed tracing

Off by default, and two switches rather than one.

```json
{
  "tracing_enabled": true,
  "tracing_export": false,
  "tracing_endpoint": ""
}
```

`tracing_enabled` lets the coordinator accept a `traceparent` from a worker,
mint one when none arrives, and hand it back — so one cross-machine incident
reads as one thing. Nothing leaves the machine at this setting.

`tracing_export` sends finished spans to `tracing_endpoint` and needs the
optional extra (`pip install opentelemetry-sdk` — the SDK, not
`opentelemetry-api`, which on its own records nothing and is reported as export
being off). **No collector is shipped or configured.**

On a worker, export is that contributor's decision and never a condition of
joining. Turning it on for the coordinator does not turn it on for anyone's
node.

---

## Public pitch is an explicit exception

`public_pitch` lets unauthenticated visitors spend compute through the bounded
public profile. Trusted-alpha preflight rejects it unless both fields are true:

```json
{
  "public_pitch": true,
  "public_pitch_acknowledged": true
}
```

Rate, task-length and concurrency limits reduce abuse; they do not eliminate it,
and behind a reverse proxy the per-IP limit is one shared bucket (see
[What the proxy changes](#what-the-proxy-changes-about-what-the-application-sees)).
Enable this only for a supervised event and disable it afterward. It does not
make private execution or artifact routes public.

---

## Updating and recovery

Use the procedures in [Trusted Alpha Runbook](TRUSTED_ALPHA_RUNBOOK.md). Back up
before an update, keep one coordinator per `data/` directory, and expect queued
or running executions to become interrupted rather than resume after a restart.

---

## Troubleshooting

### A worker is refused for its protocol version

Check the window first. It needs no credential:

```bash
curl -s "$BASE_URL/v1/worker-protocol"
```

```json
{"node_protocol_min": "1", "node_protocol_max": "1",
 "supported_worker_protocol_versions": ["1"], "server_version": "0.3.0"}
```

A refused registration returns `426` and says which side is stale. Read
`detail.action`, not just the status:

| `detail.code` | what it means | what to do |
| --- | --- | --- |
| `worker_protocol_version_too_old` | the worker is behind the window | update the worker and rejoin; `detail.action` is `upgrade_worker` |
| `worker_protocol_version_too_new` | the worker is ahead of this coordinator | update the coordinator, or run a worker at a supported version; `detail.action` is `upgrade_coordinator` |
| `invalid_worker_protocol_version` (`422`) | the declared version is not a version token | a hand-edited descriptor; remove the override |

Nothing durable is created by a refusal. Workers already connected are
unaffected by a window change.

### Everything else

| Symptom | Action |
| --- | --- |
| A contributor's installer says the address must start with `https://` | You are not serving TLS yet. Path A or Path B above; there is no override on their side |
| A contributor's installer rejects your certificate | Run `scripts/tls_local_check.py`. A self-signed or private-CA certificate cannot be made to work |
| `ufw status` says a port is denied but it still answers | Docker published it. See [The trap](#the-trap-docker-does-not-consult-ufw); check with `ss -tlnp` |
| Caddy will not obtain a certificate | Check the DNS A record with `dig +short`, and that port 80 is open. Read `sudo journalctl -u caddy -f` |
| Preflight says another coordinator owns the state directory | Stop the other process or pick a different state directory; do not delete the lock file |
| `/health` is `degraded` | Check `docker compose logs ollama` and confirm the model pull completed |
| `/health` says private routes are unprotected | Configure a distinct `viewer_key`, run preflight, restart |
| Preflight rejects legacy-only node admission | Set `node_enrollment_mode` to `required` and upgrade stock workers, preserving their identity files |
| Initial worker bootstrap returns 401 | Confirm only that worker received the current `node_secret`; returning workers use their identity file |
| Registration says durable enrollment is required | Upgrade the worker; use `--identity-file` if its default configuration directory is unsuitable |
| Registration returns 409 | The label or credential belongs to another durable enrollment; inspect the protected enrollment list rather than reusing the label |
| Registration returns `node_capability_descriptor_conflict` | The live session tried to change its immutable claim; stop that worker and register a fresh session |
| A typed task excludes a worker | Inspect viewer-protected `/v1/operator/node-enrollments` and use its stable `reason_codes` |
| Capability evidence shows `insufficient_evidence` | The configured cold-start state, not a negative score |
| Pitch returns 401 | Send the current `pitch_key` on the pitch route |
| Browser logs in but immediately loses the cookie | HTTPS and `viewer_cookie_secure` disagree, or the proxy is not actually serving HTTPS |
| Compose is reachable only locally | That is correct and deliberate. Reach it through the proxy |
