#!/usr/bin/env bash
# Imagination Agency (CS2 Agency) - start the dashboard (and the scheduler) on Linux / macOS.
#   bash start.sh                  run in this terminal (Ctrl+C stops); opens the browser
#   bash start.sh --background     run in the background, log in data/logs/dashboard.log
#   bash start.sh --stop           stop the background agency started with --background
#   bash start.sh -- ARGS...       extra arguments for "agency dashboard" (e.g. -- --interval 30)
# Already running (the port answers /api/health)? It just opens the browser.
# Console text is English, like the Windows scripts; the Hebrew UI lives in the browser.
set -euo pipefail
cd "$(dirname "$0")"

MODE="foreground"
NO_BROWSER=0
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --background) MODE="background" ;;
    --stop) MODE="stop" ;;
    --no-browser) NO_BROWSER=1 ;;
    --) shift; EXTRA=("$@"); break ;;
    *) EXTRA+=("$1") ;;
  esac
  shift
done

if [ ! -x .venv/bin/python ]; then
  echo "[X] The agency is not installed yet. Run first: bash install.sh"
  exit 1
fi
[ -f config.yaml ] || AGENCY_CLI_LANG="${AGENCY_CLI_LANG:-en}" .venv/bin/agency init >/dev/null || true

PORT="$(.venv/bin/python -c "from agency.config import load_settings as L; print(int(float(L().get('dashboard.port', 8765))))" 2>/dev/null || echo 8765)"
URL="http://127.0.0.1:${PORT}/"
PIDFILE="data/agency.pid"
LOG="data/logs/dashboard.log"

healthy() {
  .venv/bin/python - "$PORT" <<'PY' 2>/dev/null
import json, sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/health", timeout=3) as r:
        data = json.load(r)
    sys.exit(0 if data.get("ok") is True and data.get("app") == "cs2-agency" else 1)
except Exception:
    sys.exit(1)
PY
}

open_browser() {
  [ "$NO_BROWSER" = 1 ] && return 0
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 || true
  else echo "Open $URL in your browser."; fi
}

if [ "$MODE" = "stop" ]; then
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null && healthy; then
    kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE"
    echo "The agency stopped."
  else
    rm -f "$PIDFILE"
    echo "No background agency to stop (one started in a terminal stops with Ctrl+C there)."
  fi
  exit 0
fi

if healthy; then
  echo "The agency is already running: $URL"
  open_browser
  exit 0
fi

if [ "$MODE" = "background" ]; then
  mkdir -p data/logs
  echo "---- start $(date '+%Y-%m-%dT%H:%M:%S') ----" >> "$LOG"
  nohup .venv/bin/agency dashboard --no-browser ${EXTRA[@]+"${EXTRA[@]}"} >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  for _ in $(seq 1 120); do
    if healthy; then
      echo "The agency is running: $URL   (log: $LOG; stop: bash start.sh --stop)"
      open_browser
      exit 0
    fi
    kill -0 "$(cat "$PIDFILE")" 2>/dev/null || break
    sleep 0.75
  done
  echo "[X] The agency did not start. Last lines of $LOG:"
  tail -n 15 "$LOG" || true
  exit 1
fi

echo "Starting the dashboard... keep this terminal open so posts go out on time. Ctrl+C stops."
ARGS=(dashboard)
[ "$NO_BROWSER" = 1 ] && ARGS+=(--no-browser)
exec .venv/bin/agency "${ARGS[@]}" ${EXTRA[@]+"${EXTRA[@]}"}
