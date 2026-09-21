#!/usr/bin/env bash
set -euo pipefail

TARGET_FILE="${1:-.env}"

read_required() {
  local prompt="$1"
  local default_value="${2:-}"
  local value=""
  while [[ -z "$value" ]]; do
    if [[ -n "$default_value" ]]; then
      read -r -p "$prompt [$default_value]: " value
      value="${value:-$default_value}"
    else
      read -r -p "$prompt: " value
    fi
  done
  printf '%s' "$value"
}

read_optional() {
  local prompt="$1"
  local default_value="${2:-}"
  local value=""
  read -r -p "$prompt${default_value:+ [$default_value]}: " value
  printf '%s' "${value:-$default_value}"
}

read_secret() {
  local prompt="$1"
  local value=""
  while [[ -z "$value" ]]; do
    read -r -s -p "$prompt: " value
    printf '\n' >&2
  done
  printf '%s' "$value"
}

write_pair() {
  local key="$1"
  local value="$2"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || {
    printf 'Newlines are not allowed in %s\n' "$key" >&2
    exit 1
  }
  printf '%s=%s\n' "$key" "$value"
}

VK_GROUP_ID="$(read_required 'VK community numeric ID (owner_id, usually negative)')"
VK_ACCESS_TOKEN="$(read_secret 'VK access token')"
TELEGRAM_BOT_TOKEN="$(read_secret 'Telegram bot token')"
TELEGRAM_CHAT_ID="$(read_required 'Telegram SMM chat ID')"
LLM_API_URL="$(read_required 'LLM chat-completions URL' 'https://api.deepseek.com/chat/completions')"
LLM_API_KEY="$(read_secret 'LLM API key')"
LLM_MODEL="$(read_required 'LLM model')"
GOOGLE_SHEETS_ID="$(read_required 'Google Sheets ID')"
GOOGLE_SHEETS_CREDENTIALS="$(read_optional 'Google credentials path inside container' 'credentials.json')"
POSTGRES_DB="$(read_required 'PostgreSQL database name' 'cpgames')"
POSTGRES_USER="$(read_required 'PostgreSQL user' 'cpgames')"
POSTGRES_PASSWORD="$(read_secret 'PostgreSQL password')"
API_ID="$(read_optional 'Telethon API_ID (optional)')"
API_HASH="$(read_optional 'Telethon API_HASH (optional)')"
PHONE_NUMBER="$(read_optional 'Telethon phone number (optional)')"

TARGET_DIR="$(dirname "$TARGET_FILE")"
mkdir -p "$TARGET_DIR"
TEMP_FILE="$(mktemp "${TARGET_FILE}.tmp.XXXXXX")"
trap 'rm -f "$TEMP_FILE"' EXIT
umask 077
{
  printf '# PostgreSQL\n'
  write_pair POSTGRES_DB "$POSTGRES_DB"
  write_pair POSTGRES_USER "$POSTGRES_USER"
  write_pair POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
  write_pair POSTGRES_HOST "postgres"
  write_pair POSTGRES_PORT "5432"
  write_pair DATABASE_URL ""
  printf '\n# VK polling -> AI -> Telegram SMM notification\n'
  write_pair VK_GROUP_ID "$VK_GROUP_ID"
  write_pair VK_ACCESS_TOKEN "$VK_ACCESS_TOKEN"
  write_pair CHECK_INTERVAL "60"
  write_pair POSTS_COUNT "3"
  write_pair VK_NOTIFY_EXISTING "false"
  printf '\n# Telegram bot notifications\n'
  write_pair TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
  write_pair TELEGRAM_CHAT_ID "$TELEGRAM_CHAT_ID"
  printf '\n# LLM\n'
  write_pair LLM_API_URL "$LLM_API_URL"
  write_pair LLM_API_KEY "$LLM_API_KEY"
  write_pair LLM_MODEL "$LLM_MODEL"
  printf '\n# Google Sheets knowledge base\n'
  write_pair GOOGLE_SHEETS_ID "$GOOGLE_SHEETS_ID"
  write_pair GOOGLE_SHEETS_CREDENTIALS "$GOOGLE_SHEETS_CREDENTIALS"
  printf '\n# Optional Telegram comments parser\n'
  write_pair TELEGRAM_PARSER_ENABLED "false"
  write_pair API_ID "$API_ID"
  write_pair API_HASH "$API_HASH"
  write_pair PHONE_NUMBER "$PHONE_NUMBER"
  write_pair SESSION_NAME "/app/data/parser_session"
  write_pair TELEGRAM_CHANNEL "fabrica_igr"
  write_pair TELEGRAM_POSTS_LIMIT "3"
  write_pair TELEGRAM_COMMENTS_LIMIT "200"
  write_pair TELEGRAM_CHECK_INTERVAL "60"
  write_pair TELEGRAM_EXPORT_DIR "/app/data"
  write_pair TELEGRAM_STATE_FILE "/app/data/telegram_seen.json"
  write_pair TELEGRAM_NOTIFY_EXISTING "false"
} > "$TEMP_FILE"
chmod 600 "$TEMP_FILE"
mv -f "$TEMP_FILE" "$TARGET_FILE"
trap - EXIT
printf 'Created %s with permissions 600\n' "$TARGET_FILE"
