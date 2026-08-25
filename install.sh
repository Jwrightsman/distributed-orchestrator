#!/usr/bin/env bash
# Mycelium — one-line node join (Mac / Linux)
#
#   curl -fsSL https://raw.githubusercontent.com/Jwrightsman/distributed-orchestrator/master/install.sh | bash -s -- http://ORCHESTRATOR_IP:8000
#
# An explicit URL is required so credentials are never sent to an
# unauthenticated LAN-discovery responder. Pass "--secret VALUE" after the URL
# for first enrollment if the orchestrator
# requires bootstrap admission. Use a private overlay or TLS URL. The stock
# worker stores its own credential in a private coordinator-hashed identity file
# and does not need the shared bootstrap secret when returning.
#
# What it does: checks Python + Ollama (installs Ollama on Linux), downloads
# the repo to ~/distributed-orchestrator, installs two Python packages
# (httpx, rich), then runs join.py — which pulls the model and starts working.

set -euo pipefail

if [ "$#" -lt 1 ] || [[ "$1" == -* ]]; then
  echo "An explicit coordinator origin is required for durable enrollment."
  echo "Use: install.sh https://COORDINATOR [--secret VALUE]"
  exit 1
fi

REPO_URL="https://github.com/Jwrightsman/distributed-orchestrator"
DEST="$HOME/distributed-orchestrator"

echo ""
echo "distributed-orchestrator node setup"
echo ""

# 1. Python 3.10+
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
      PY="$candidate"
      break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10+ is required. Install it (https://www.python.org or your package manager), then re-run."
  exit 1
fi
echo "  Python:  $("$PY" --version)"

# 2. Ollama
if ! command -v ollama >/dev/null 2>&1; then
  case "$(uname -s)" in
    Linux)
      echo "  Ollama:  not found — installing via the official script..."
      curl -fsSL https://ollama.com/install.sh | sh
      ;;
    Darwin)
      echo "Ollama is required. Download it from https://ollama.com/download (one click), then re-run this script."
      exit 1
      ;;
  esac
fi
echo "  Ollama:  installed"

# 3. Get or update the repo
if [ -f "$DEST/join.py" ]; then
  echo "  Repo:    already at $DEST"
  git -C "$DEST" pull --ff-only >/dev/null 2>&1 && echo "  Repo:    updated" || true
elif command -v git >/dev/null 2>&1; then
  git clone --depth 1 "$REPO_URL" "$DEST"
  echo "  Repo:    $DEST"
else
  echo "  Repo:    downloading (no git found)..."
  TMPZIP="$(mktemp -t swarm-node-XXXX).zip"
  curl -fsSL "$REPO_URL/archive/refs/heads/master.zip" -o "$TMPZIP"
  UNPACK="$(mktemp -d)"
  if command -v unzip >/dev/null 2>&1; then
    unzip -q "$TMPZIP" -d "$UNPACK"
  else
    "$PY" -c "import zipfile, sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$TMPZIP" "$UNPACK"
  fi
  mv "$UNPACK/distributed-orchestrator-master" "$DEST"
  rm -rf "$TMPZIP" "$UNPACK"
  echo "  Repo:    $DEST"
fi

# 4. Python deps (node needs just these two).
#    Newer distros refuse global pip installs (PEP 668) — fall back accordingly.
"$PY" -m pip install --quiet --disable-pip-version-check httpx rich 2>/dev/null \
  || "$PY" -m pip install --quiet --disable-pip-version-check --user httpx rich 2>/dev/null \
  || "$PY" -m pip install --quiet --disable-pip-version-check --user --break-system-packages httpx rich
echo "  Deps:    httpx, rich"

# 5. Join the network (join.py pulls the model and starts polling).
# Registration uses the shared admission secret only to create a durable,
# independently revocable enrollment, then receives a server-issued process-
# local node session. The enrollment credential stays in the user's private
# Mycelium configuration directory. The plaintext session token stays in worker
# memory, is sent on worker-protocol calls, and is refreshed automatically
# after a coordinator restart or a machine-readable session rejection. This is
# collision protection for node labels, not public-key machine identity.
#
# Reconnect stdin to the terminal before handing over. In the advertised
# `curl ... | bash -s -- URL` form, bash's stdin *is* the downloaded script, so
# anything exec'd here inherits a pipe rather than a terminal. join.py asks a
# human to type "yes" before it installs a model and starts using their CPU,
# and refuses when nobody is there to answer — so the one-liner did all the
# work and then stopped dead on "Not running in a terminal, so nobody can
# consent."
#
# /dev/tty is the controlling terminal regardless of what stdin was redirected
# to, which is exactly the distinction the consent gate cares about: a person
# is present. When there is genuinely no terminal — CI, a container, a remote
# unattended shell — the redirect fails, we fall through, and join.py refuses
# just as it should. Do NOT "fix" this by passing --yes here: consenting on the
# machine owner's behalf is the one thing this gate exists to prevent.
echo ""
cd "$DEST"
if [ -e /dev/tty ] && (: < /dev/tty) 2>/dev/null; then
  exec "$PY" join.py "$@" < /dev/tty
else
  exec "$PY" join.py "$@"
fi
