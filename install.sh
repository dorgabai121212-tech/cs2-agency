#!/usr/bin/env bash
# Imagination Agency (CS2 Agency) - installer for Linux / macOS.
#   bash install.sh [--no-start]
# Safe to run again (it is also the way to update). Works from any folder, paths with spaces too.
# Steps: Python >= 3.10, ffmpeg check, .venv, pip install -e ".[platforms]", agency init,
# then (when run in a terminal) offers to start the dashboard with start.sh.
# Console text is English, like the Windows installer; the Hebrew UI lives in the browser.
set -euo pipefail
cd "$(dirname "$0")"

ok()   { printf '\033[32m[OK]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[!]\033[0m  %s\n' "$1"; }
fail() { printf '\033[31m[X]\033[0m  %s\n' "$1"; exit 1; }

echo "==============================================="
echo "   Imagination Agency - setup"
echo "   Folder: $(pwd)"
echo "==============================================="

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && \
     "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
[ -n "$PY" ] || fail "Python 3.10+ not found. Install it (e.g. sudo apt install python3 python3-venv, or brew install python) and run again."
ok "Found $("$PY" --version 2>&1)"

if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  ok "ffmpeg is installed."
else
  warn "ffmpeg not found - no vertical cuts or thumbnails. Install: sudo apt install ffmpeg / brew install ffmpeg"
fi

if [ -x .venv/bin/python ] && .venv/bin/python -c 'import sys' 2>/dev/null; then
  ok "The virtual environment (.venv) already exists."
else
  echo "Creating the virtual environment (.venv)..."
  rm -rf .venv
  "$PY" -m venv .venv || fail "Creating .venv failed (on Ubuntu/Debian: sudo apt install python3-venv)."
  ok "Created the virtual environment."
fi

echo "Installing the agency and its libraries (this can take a few minutes)..."
.venv/bin/python -m pip install --disable-pip-version-check --quiet --upgrade pip || warn "Updating pip failed - continuing."
.venv/bin/python -m pip install --disable-pip-version-check -e ".[platforms]" || fail "Installing the libraries failed. Check the internet connection and run again."
ok "All libraries are installed."

echo "Preparing settings and the data folder (existing files are kept)..."
AGENCY_CLI_LANG="${AGENCY_CLI_LANG:-en}" .venv/bin/agency init || fail "agency init failed (see the message above)."

echo
ok "Done. Imagination Agency is installed in $(pwd)"
echo "  Start it:   bash start.sh            (stays in this terminal; Ctrl+C stops)"
echo "              bash start.sh --background   (log in data/logs/dashboard.log)"
echo "  Training mode is ON: nothing is really published until you switch a platform"
echo "  live on the Connections page of the dashboard."

if [ "${1:-}" != "--no-start" ] && [ -t 0 ]; then
  read -r -p "Start the dashboard now? [y/N] " answer
  case "$answer" in y|Y|yes|YES) exec bash ./start.sh ;; esac
fi
