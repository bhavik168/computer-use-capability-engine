#!/usr/bin/env bash
# Run the acceptance smoke test against the Flask test client.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "==> Creating virtual environment"
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

exec .venv/bin/python smoke_test.py
