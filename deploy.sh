#!/usr/bin/env bash
# Take a fresh Ubuntu VM to a running, secured orchestrator in one command.
#
#   ssh ubuntu@YOUR_VM_IP
#   curl -fsSL https://raw.githubusercontent.com/Jwrightsman/distributed-orchestrator/master/deploy.sh | bash
#
# Generates both auth keys, brings up Docker + Ollama + the orchestrator, pulls
# the model, waits for health, and prints exactly what you need to connect.
# Safe to re-run: existing keys in data/config.json are preserved.
#
# Full walkthrough (accounts, firewall ports, security notes): docs/DEPLOY.md

set -euo pipefail

REPO_URL="https://github.com/Jwrightsman/distributed-orchestrator"
DEST="${SWARM_DIR:-$HOME/distributed-orchestrator}"
MODEL="${SWARM_MODEL:-qwen3.5:4b}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m    %s\033[0m\n' "$1"; }

# ── 1. Docker ─────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker"
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker.io docker-compose-v2 git
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER" || true
  NEEDS_RELOGIN=1
else
  say "Docker already installed"
  command -v git >/dev/null 2>&1 || sudo apt-get install -y -qq git
fi

# Use sudo for docker until the group membership takes effect in a new session
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

# ── 2. Repo ───────────────────────────────────────────────────────────
# Three cases: files already uploaded (private repo — no clone possible), an
# existing git checkout to refresh, or a clean clone.
if [ -f "$DEST/docker-compose.yml" ] && [ ! -d "$DEST/.git" ]; then
  say "Using the code already present at $DEST"
elif [ -d "$DEST/.git" ]; then
  say "Updating existing checkout at $DEST"
  git -C "$DEST" pull --ff-only || warn "Could not pull (private repo or local changes) — using what is here"
else
  say "Cloning to $DEST"
  if ! git clone --depth 1 "$REPO_URL" "$DEST"; then
    warn "Clone failed. If the repository is private, copy the code up instead:"
    warn "  git archive --format=tar HEAD | ssh root@THIS_HOST 'mkdir -p ~/distributed-orchestrator && tar -x -C ~/distributed-orchestrator'"
    warn "then re-run this script."
    exit 1
  fi
fi
cd "$DEST"
mkdir -p data

# ── 3. Config with generated secrets ──────────────────────────────────
CONFIG="data/config.json"
if [ -f "$CONFIG" ] && grep -q node_secret "$CONFIG"; then
  say "Keeping existing $CONFIG (secrets preserved)"
else
  say "Generating auth keys"
  NODE_SECRET="$(openssl rand -hex 24)"
  PITCH_KEY="$(openssl rand -hex 24)"
  cat > "$CONFIG" <<EOF
{
  "ollama_url": "http://ollama:11434",
  "model": "$MODEL",
  "node_secret": "$NODE_SECRET",
  "pitch_key": "$PITCH_KEY",
  "output_max_mb": 500,
  "public_pitch": false
}
EOF
  chmod 600 "$CONFIG"
fi

NODE_SECRET="$(grep -o '"node_secret"[^,]*' "$CONFIG" | cut -d'"' -f4)"
PITCH_KEY="$(grep -o '"pitch_key"[^,]*' "$CONFIG" | cut -d'"' -f4)"

# ── 4. Launch ─────────────────────────────────────────────────────────
say "Building and starting containers"
$DOCKER compose up -d --build

say "Pulling $MODEL (a few minutes on first run)"
$DOCKER compose exec -T ollama ollama pull "$MODEL"

# ── 5. Wait for health ────────────────────────────────────────────────
say "Waiting for the orchestrator to report healthy"
for i in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health 2>/dev/null | grep -q '"status":"ok"'; then
    HEALTHY=1
    break
  fi
  sleep 5
done

PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo YOUR_VM_IP)"

if [ "${HEALTHY:-0}" != "1" ]; then
  warn "The orchestrator did not report healthy in 5 minutes."
  warn "Check the logs with:  $DOCKER compose logs --tail 50"
  exit 1
fi

# ── 6. What to do next ────────────────────────────────────────────────
cat <<EOF

$(say "Orchestrator is live")

  Dashboard   http://$PUBLIC_IP:8000/dashboard
  Landing     http://$PUBLIC_IP:8000/

  node_secret $NODE_SECRET
  pitch_key   $PITCH_KEY

Join a worker node from another machine:

  python join.py http://$PUBLIC_IP:8000 --secret $NODE_SECRET

Submit a task from anywhere:

  curl -X POST http://$PUBLIC_IP:8000/pitch/async \\
    -H "Content-Type: application/json" \\
    -H "X-Pitch-Key: $PITCH_KEY" \\
    -d '{"task": "Write a haiku about distributed computing"}'

Keep those two keys private — they are the only thing stopping strangers from
joining nodes or spending your compute. They are stored in $DEST/$CONFIG.
If port 8000 is not reachable, open it in your cloud firewall (docs/DEPLOY.md).

EOF

if [ "${NEEDS_RELOGIN:-0}" = "1" ]; then
  warn "Log out and back in to use docker without sudo."
fi
