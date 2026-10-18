#!/usr/bin/env bash
# CS2 Agency - installer for Linux / macOS.   Usage: bash install.sh [--no-start]
set -euo pipefail
cd "$(dirname "$0")"

ok()   { printf '\033[32m[V]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$1"; }
fail() { printf '\033[31m[X]\033[0m %s\n' "$1"; exit 1; }

echo "==============================================="
echo "   סוכנות CS2 - התקנה"
echo "==============================================="

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && \
     "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
[ -n "$PY" ] || fail "לא נמצא Python 3.10 ומעלה. התקן (למשל: sudo apt install python3 python3-venv / brew install python) ונסה שוב."
ok "נמצא $("$PY" --version)"

if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg מותקן."
else
  warn "ffmpeg לא נמצא - אין חיתוך אנכי ותמונות ממוזערות. התקנה: sudo apt install ffmpeg / brew install ffmpeg"
fi

if [ -x .venv/bin/python ]; then
  ok "סביבה וירטואלית (.venv) כבר קיימת."
else
  echo "יוצר סביבה וירטואלית (.venv)..."
  "$PY" -m venv .venv || fail "יצירת .venv נכשלה (ב-Ubuntu: sudo apt install python3-venv)."
  ok "נוצרה סביבה וירטואלית."
fi

echo "מתקין ספריות..."
.venv/bin/python -m pip install --disable-pip-version-check --upgrade pip || warn "עדכון pip נכשל - ממשיך."
.venv/bin/python -m pip install --disable-pip-version-check -e ".[platforms]" || fail "התקנת הספריות נכשלה."
ok "כל הספריות הותקנו."

.venv/bin/agency init || fail "agency init נכשל."

echo
ok "ההתקנה הסתיימה!"
echo "  1. מפתח Claude:   nano .env    (ANTHROPIC_API_KEY=...; בלי מפתח - מצב הדגמה)"
echo "  2. הגדרות ערוץ:   nano config.yaml"
echo "  3. הפעלה:         bash start.sh"
echo "מצב ניסיון (dry_run) פעיל - שום דבר לא יעלה באמת לרשתות עד שתשנה."

if [ "${1:-}" != "--no-start" ] && [ -t 0 ]; then
  read -r -p "להפעיל את לוח הבקרה עכשיו? [y/n] " answer
  case "$answer" in y|Y|yes|כ|כן) exec bash ./start.sh ;; esac
fi
