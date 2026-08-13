#!/usr/bin/env bash
# Mycelium — one-line node join (Mac / Linux)
#
#   curl -fsSL https://raw.githubusercontent.com/Jwrightsman/distributed-orchestrator/master/install.sh | bash -s -- http://ORCHESTRATOR_IP:8000
#
# Omit the URL to auto-discover an orchestrator on your LAN.
# Pass "--secret VALUE" after the URL if the orchestrator requires one.
#
# What it does: checks Python + Ollama (installs Ollama on Linux), downloads
# the repo to ~/distributed-orchestrator, installs two Python packages
# (httpx, rich), then runs join.py — which pulls the model and starts working.

set -euo pipefail

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

# 5. Join the network (join.py pulls the model and starts polling)
echo ""
cd "$DEST"
exec "$PY" join.py "$@"
