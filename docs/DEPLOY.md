# Deployment Guide

Three ways to run the orchestrator, from safest to most public. If you've never
deployed anything before: start with Path 1, move to Path 2 when you want
testers, and only do Path 3 when you want a 24/7 public orchestrator.

**Security in one paragraph:** the orchestrator has two locks — `node_secret`
(who may join as a worker) and `pitch_key` (who may submit tasks). Both live in
`config.json` and both are **off by default**, which is fine on your own Wi-Fi
but not on the internet. Path 3 requires both. Rate limiting (5 pitches per
minute per IP) and a disk cap on output (`output_max_mb`, default 500 MB) are
always on.

---

## Path 1 — Same Wi-Fi / LAN (for the demo video)

No accounts, nothing installed beyond the project itself. Machines must be on
the same network.

**On the main machine (orchestrator):**

```bash
py -m uvicorn server:app --host 0.0.0.0 --port 8000
```

Find your local IP address:

```bash
ipconfig
```

Look for "IPv4 Address" (something like `192.168.1.23`).

**On each other machine (worker node):**

```bash
py join.py http://192.168.1.23:8000
```

(Replace the IP with the orchestrator's. `join.py` checks Python and Ollama,
pulls the model, and connects. On the same LAN it can usually find the
orchestrator automatically: `py join.py` with no address.)

Dashboard: `http://192.168.1.23:8000/dashboard` from any device on the network.

> **Is this safe?** Anyone *on your Wi-Fi* can pitch tasks and join nodes.
> At home that's you and your family. On dorm/public Wi-Fi, set `node_secret`
> and `pitch_key` in `config.json` first (see Path 3, step 4).

---

## Path 2 — Tailscale (private testers anywhere)

Tailscale creates a private network between machines you invite — testers
anywhere in the world, **zero ports opened to the public internet**. Free for
up to 100 devices.

1. **Create a Tailscale account:** https://tailscale.com/ → "Get started" —
   sign in with Google or GitHub. Install Tailscale on the orchestrator
   machine and log in.
2. **Find your Tailscale IP:** run `tailscale ip -4` — it looks like
   `100.x.y.z`.
3. **Start the orchestrator** exactly as in Path 1.
4. **Invite testers:** in the Tailscale admin page (https://login.tailscale.com/admin/machines)
   choose "Invite external users". Each tester installs Tailscale, accepts the
   invite, then runs:

   ```bash
   python join.py http://100.x.y.z:8000
   ```

5. Recommended even here: set a `node_secret` (Path 3, step 4) and give it to
   testers — they pass it with `python join.py http://100.x.y.z:8000 --secret <value>`.

> **Is this safe?** Yes — traffic is end-to-end encrypted and only invited
> machines can reach you. Your home IP is never exposed. This is the
> recommended way to let strangers-you've-vetted join before going public.

---

## Path 3 — Public 24/7 orchestrator (Oracle free tier or Hetzner)

A cloud VM runs the orchestrator around the clock; your laptop and anyone
else's machine joins as a worker from anywhere. **Do not skip step 4.**

### 3a. Get a VM

**Oracle Cloud (free forever, best value):**
1. Sign up at https://www.oracle.com/cloud/free/ (credit card required for
   identity, not charged).
2. Create an instance: shape **VM.Standard.A1.Flex** (Ampere ARM), 4 CPUs,
   24 GB RAM — that's the free tier maximum and it runs qwen3.5:4b on CPU
   comfortably. OS: Ubuntu 24.04.
3. During creation, download the SSH private key it offers. On the
   "networking" step, note the public IP.
4. Open port 8000: Networking → Virtual Cloud Network → Security Lists →
   Add Ingress Rule: source `0.0.0.0/0`, protocol TCP, destination port `8000`.

**Hetzner (~€4/month, simpler UI):**
1. Sign up at https://www.hetzner.com/cloud
2. Create a server: type **CX22** (2 vCPU, 4 GB) works for orchestrator-only
   (planner/reviewer routed to nodes); pick **CAX21** (ARM, 8 GB) if the VM
   itself should run inference. OS: Ubuntu 24.04.
3. Add your SSH key during creation (Hetzner shows how to make one).
4. In the server's Firewall tab, allow inbound TCP on ports 22 and 8000.

### 3b. Install and run

SSH in (replace the IP):

```bash
ssh ubuntu@YOUR_VM_IP
```

Then on the VM:

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker
git clone https://github.com/Jwrightsman/distributed-orchestrator.git
cd distributed-orchestrator
mkdir -p data
```

### 3c. Configure (the security step — required)

Create `data/config.json` on the VM:

```bash
cat > data/config.json <<'EOF'
{
  "ollama_url": "http://ollama:11434",
  "model": "qwen3.5:4b",
  "node_secret": "REPLACE-WITH-LONG-RANDOM-STRING-1",
  "pitch_key": "REPLACE-WITH-LONG-RANDOM-STRING-2",
  "output_max_mb": 500
}
EOF
```

Generate two random strings with: `openssl rand -hex 24` (run it twice).

- `node_secret` — give this only to people you allow to join as workers.
- `pitch_key` — give this only to people you allow to submit tasks.

### 3d. Launch

```bash
docker compose up -d --build
docker compose exec ollama ollama pull qwen3.5:4b
```

Check it: `http://YOUR_VM_IP:8000/health` in a browser should show
`"status": "ok"`.

**Join your laptop as the first worker node** (from your laptop, not the VM):

```bash
py node.py --server http://YOUR_VM_IP:8000 --secret REPLACE-WITH-LONG-RANDOM-STRING-1
```

> **Is this safe?** With both keys set: joining and pitching require secrets,
> pitch rate limiting is on, and disk usage is capped. What this does *not*
> give you: HTTPS (traffic is readable in transit — fine for demo tasks, don't
> pitch anything sensitive), and the dashboard/history pages are readable by
> anyone who finds the address. Treat everything the orchestrator produces as
> public. If the VM is ever compromised, it holds nothing but this project's
> output and your two random strings — rotate them by editing config.json and
> restarting: `docker compose restart orchestrator`.

---

## The public pitch page (`/try`) — read before enabling

`"public_pitch": true` in config.json turns on a page where **anyone on the
internet can submit tasks with no key**. Protections that are always on with
it: 2 tasks per hour per visitor, at most 3 public tasks running at once,
300-character task limit, and a basic word filter.

**What can still go wrong:** strangers decide what your hardware works on for
minutes at a time; the word filter is basic, so someone determined can phrase
something distasteful and the swarm will write text about it; and outputs are
publicly readable. Enable it for demo events and launch windows, watch the
dashboard while it's on, and turn it off (`"public_pitch": false` + restart)
when you're not looking at it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/health` shows `"ollama": "unavailable"` | `docker compose logs ollama`; make sure the model pull finished |
| Node gets 401 on join | The node isn't sending the right `node_secret` |
| Pitches get 401 | Send header `X-Pitch-Key: <your pitch_key>` |
| Pitches get 429 | Rate limit: 5/minute per IP — wait a minute |
| VM slow / out of memory | Use a smaller model (`gemma3:1b`) or route builders to worker nodes |
