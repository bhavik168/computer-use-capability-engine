#!/usr/bin/env bash
# Start the CoreBank Servicing Console. Creates the venv and installs
# dependencies on first run, then serves on http://127.0.0.1:${PORT:-5050}.
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-5050}"
VENV=".venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> Creating virtual environment"
  python3 -m venv "$VENV"
fi

echo "==> Installing dependencies"
"$VENV/bin/pip" install -q -r requirements.txt

if [ ! -f .env ]; then
  echo "==> No .env found; copying .env.example (app will use the in-memory store)"
  cp .env.example .env
fi

# Clear a stale server still holding the port from a previous run.
if lsof -ti "tcp:$PORT" >/dev/null 2>&1; then
  echo "==> Port $PORT busy; stopping the previous server"
  lsof -ti "tcp:$PORT" | xargs kill -9
  sleep 1
fi

echo "==> Serving on http://127.0.0.1:$PORT  (operator1 / pass1234)"
exec env PORT="$PORT" "$VENV/bin/python" app.py
