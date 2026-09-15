#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating .venv ..."
  python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  echo "No .env found — copying .env.example to .env"
  cp .env.example .env
fi

PORT="$(grep -E '^PORT=' .env | cut -d= -f2 | tr -d '[:space:]')"
PORT="${PORT:-5050}"
if lsof -ti tcp:"$PORT" >/dev/null 2>&1; then
  echo "Port $PORT is in use — stopping the existing listener."
  lsof -ti tcp:"$PORT" | xargs kill -9 || true
fi

echo "Starting Member Servicing Portal on http://127.0.0.1:$PORT"
exec ./.venv/bin/python app.py
