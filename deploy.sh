#!/usr/bin/env bash
# Take a fresh Ubuntu VM to a trusted-alpha Mycelium coordinator.
#
#   ssh ubuntu@YOUR_VM_IP
#   git clone https://github.com/Jwrightsman/distributed-orchestrator
#   cd distributed-orchestrator
#   MYCELIUM_PRIVATE_OVERLAY_CONFIRMED=1 ./deploy.sh
#
# Clone first and run second, so there is a point at which the thing about to
# run can be read. This project deleted its contributor-facing one-liners on
# 2026-09-05 for exactly that reason, and the operator path should not keep one.
#
# Safe to re-run: valid existing authorities and unrelated configuration are
# preserved. Credential values are written atomically to data/config.json and
# are never copied into this script's output.
#
# Each generated authority is 32 bytes from the operating system's
# cryptographic random source, rendered as 43 URL-safe characters - about 256
# bits, against the 128-bit floor scripts/deploy_preflight.py enforces. That
# margin matters most for worker admission, because nothing rate-limits guesses
# at it; see docs/THREAT_MODEL.md section 14.

set +x  # Never expose generated credentials even if a caller used `bash -x`.
set -euo pipefail

REPO_URL="https://github.com/Jwrightsman/distributed-orchestrator"
DEST="${SWARM_DIR:-$HOME/distributed-orchestrator}"
REQUESTED_MODEL="${SWARM_MODEL:-}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m    %s\033[0m\n' "$1"; }

case "${MYCELIUM_PRIVATE_OVERLAY_CONFIRMED:-}" in
  1|true|yes) ;;
  *)
    warn "Trusted-alpha bearer credentials require an authenticated private overlay."
    warn "Join this host to that overlay, then rerun with MYCELIUM_PRIVATE_OVERLAY_CONFIRMED=1."
    exit 1
    ;;
esac

# 1. Docker and Python
if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker"
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker.io docker-compose-v2 git python3
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER" || true
  NEEDS_RELOGIN=1
else
  say "Docker already installed"
  command -v git >/dev/null 2>&1 || sudo apt-get install -y -qq git
  command -v python3 >/dev/null 2>&1 || sudo apt-get install -y -qq python3
fi

DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

# 2. Repository
if [ -f "$DEST/docker-compose.yml" ] && [ ! -d "$DEST/.git" ]; then
  say "Using the code already present at $DEST"
elif [ -d "$DEST/.git" ]; then
  say "Updating existing checkout at $DEST"
  git -C "$DEST" pull --ff-only \
    || warn "Could not fast-forward; preserving the existing checkout"
else
  say "Cloning to $DEST"
  if ! git clone --depth 1 "$REPO_URL" "$DEST"; then
    warn "Clone failed. Copy an audited checkout to $DEST and re-run this script."
    exit 1
  fi
fi
cd "$DEST"
mkdir -p data
# 700, not the umask default of 755. SQLite creates events.db at 0644 and
# nothing in this project can change that, so the directory is what keeps
# every other account on the host out of the enrollment table and the run
# output. scripts/deploy_preflight.py checks for exactly this.
chmod 700 data

# 3. Trusted-alpha configuration
CONFIG="data/config.json"
say "Creating or validating trusted-alpha configuration"
python3 - "$CONFIG" "$REQUESTED_MODEL" <<'PY'
import sys
from pathlib import Path

from config import ensure_trusted_alpha_config

path = Path(sys.argv[1])
requested_model = sys.argv[2] or None
result = ensure_trusted_alpha_config(
    path,
    model=requested_model,
    ollama_url="http://ollama:11434",
    private_overlay=True,
)
print(f"  Configuration: {result.path}")
if result.generated_authorities:
    print("  Generated authorities: " + ", ".join(result.generated_authorities))
if result.preserved_authorities:
    print("  Preserved authorities: " + ", ".join(result.preserved_authorities))
print("  Credential values were not printed.")
PY

# Validate configuration first without disturbing an existing coordinator.
python3 scripts/preflight.py \
  --config "$CONFIG" \
  --state-dir data \
  --mode trusted_alpha \
  --skip-lock-check

# A full lock probe is possible only after an earlier container releases it.
$DOCKER compose stop orchestrator >/dev/null 2>&1 || true
python3 scripts/preflight.py \
  --config "$CONFIG" \
  --state-dir data \
  --mode trusted_alpha

ACTIVE_MODEL="$(python3 - "$CONFIG" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["model"])
PY
)"

# 4. Launch exactly one coordinator process
say "Building and starting containers"
$DOCKER compose up -d --build

say "Pulling the configured model (this can take several minutes the first time)"
$DOCKER compose exec -T ollama ollama pull "$ACTIVE_MODEL"

# 5. Deployment health gate: inference must be ready and private routes must
# actually be protected. A merely reachable HTTP process is not sufficient.
say "Waiting for inference and private-route protection"
for _attempt in $(seq 1 60); do
  HEALTH_JSON="$(curl -fsS http://127.0.0.1:8000/health 2>/dev/null || true)"
  if printf '%s' "$HEALTH_JSON" | python3 -c \
      'import json,sys; from scripts.preflight import deployment_health_ready; raise SystemExit(0 if deployment_health_ready(json.load(sys.stdin)) else 1)' \
      2>/dev/null; then
    HEALTHY=1
    break
  fi
  sleep 5
done

if [ "${HEALTHY:-0}" != "1" ]; then
  warn "The coordinator did not pass the trusted-alpha health gate in 5 minutes."
  warn "Inspect logs with: $DOCKER compose logs --tail 100 orchestrator ollama"
  exit 1
fi

# 6. Handoff. Do not turn deployment output into a credential leak.
say "Trusted-alpha coordinator is healthy"
cat <<EOF

  Local health       http://127.0.0.1:8000/health
  Operator dashboard http://127.0.0.1:8000/dashboard
  Configuration      $DEST/$CONFIG

The three separate authorities (viewer_key, pitch_key, and node_secret) are in
the private configuration file above. Their values were deliberately not
printed, and each is about 256 bits of generated randomness. Move only the
authority a person or machine needs through your secret manager or another
secure channel. node_secret authorizes initial worker bootstrap only; each
current worker then uses its own private, independently
revocable enrollment identity. Viewer and pitch keys remain instance-wide.

NOBODY CAN JOIN YET. The coordinator is on 127.0.0.1:8000 and nowhere else,
which is correct, and a worker refuses plaintext http:// to anything but its
own machine, with no override on their side. You still need TLS in front.
docs/DEPLOY.md has the two paths in full; the short version is:

  Path A, private overlay:  tailscale cert YOUR-HOST.YOUR-TAILNET.ts.net
                            sudo cp deploy/Caddyfile.tailscale /etc/caddy/Caddyfile
  Path B, public domain:    sudo cp deploy/Caddyfile.public /etc/caddy/Caddyfile

Do not publish port 8000 itself to reach around the proxy. A host firewall will
not save you if you do: Docker writes published ports into its own iptables
chain, evaluated before ufw's, so "ufw deny 8000" reports deny on a port that
is still answering. Verify with "ss -tlnp", never with "ufw status".

Next checks:

  python3 scripts/preflight.py --config "$CONFIG" --state-dir data --mode trusted_alpha
  python3 scripts/deploy_preflight.py --state-dir data
  $DOCKER compose logs --tail 100 orchestrator

EOF

if [ "${NEEDS_RELOGIN:-0}" = "1" ]; then
  warn "Log out and back in to use Docker without sudo."
fi
