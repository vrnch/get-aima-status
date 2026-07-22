#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

pip install -q -r requirements.txt
python -c "import playwright" >/dev/null 2>&1 || true
playwright install chromium >/dev/null

if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example to .env and fill in your secrets."
  exit 1
fi

exec python telegram_bot.py
