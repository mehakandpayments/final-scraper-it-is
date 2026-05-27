#!/usr/bin/env bash
# Launch the Streamlit scraper UI.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtualenv (Python 3.12)…"
  python3.12 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m playwright install chromium
fi

exec .venv/bin/streamlit run app.py "$@"
