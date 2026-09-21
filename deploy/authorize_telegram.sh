#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

[[ -f .env ]] || { echo "ERROR: .env not found" >&2; exit 1; }

echo "This is a one-time interactive Telegram/Telethon authorization."
echo "The session will be stored in the persistent cpgames-data Docker volume."

docker compose --profile telegram run --rm telegram-parser \
  python -m src.bot --posts 1 --comments-per-post 1 --output-dir /app/data

echo
echo "Telegram session authorization completed."
echo "Set TELEGRAM_PARSER_ENABLED=true in .env and run ./deploy/deploy.sh."
