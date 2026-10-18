#!/usr/bin/env bash
# CS2 Agency - start the dashboard + scheduler.  Extra args go to `agency dashboard`.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/agency ]; then
  echo "[X] הסוכנות עוד לא מותקנת. הרץ קודם: bash install.sh"
  exit 1
fi
[ -f config.yaml ] || .venv/bin/agency init

echo "מפעיל את לוח הבקרה... השאר את החלון פתוח כדי שהמתזמן יפרסם בזמן. לעצירה: Ctrl+C"
exec .venv/bin/agency dashboard "$@"
