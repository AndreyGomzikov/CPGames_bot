#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "Docker is not installed"
command -v python3 >/dev/null 2>&1 || fail "python3 is not installed"
[[ -f .env ]] || fail "missing .env"
[[ -f credentials.json ]] || fail "missing credentials.json (required for Google Sheets/LLM context)"
chmod 600 .env credentials.json

# Preserve all existing secrets and add PostgreSQL settings only when missing.
./deploy/prepare_postgres_env.sh .env

eval "$(python3 - .env <<'PY'
from pathlib import Path
import shlex
import sys

values = {}
for raw in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():
    line = raw.strip()
    if not line or line.startswith('#') or '=' not in raw:
        continue
    key, value = raw.split('=', 1)
    values[key.strip()] = value.strip()

def first(*names):
    for name in names:
        value = values.get(name, '').strip()
        if value:
            return value
    return ''

def placeholder(value):
    value = value.strip()
    return not value or value.startswith('replace_')

required = {
    'VK_GROUP_ID': first('VK_GROUP_ID'),
    'VK_ACCESS_TOKEN': first('VK_ACCESS_TOKEN'),
    'TELEGRAM_BOT_TOKEN': first('TELEGRAM_BOT_TOKEN', 'TG_BOT_TOKEN'),
    'TELEGRAM_CHAT_ID': first('TELEGRAM_CHAT_ID', 'ADMIN_CHAT_ID'),
    'LLM_API_URL': first('LLM_API_URL'),
    'LLM_API_KEY': first('LLM_API_KEY'),
    'LLM_MODEL': first('LLM_MODEL'),
    'GOOGLE_SHEETS_ID': first('GOOGLE_SHEETS_ID'),
    'POSTGRES_DB': first('POSTGRES_DB'),
    'POSTGRES_USER': first('POSTGRES_USER'),
    'POSTGRES_PASSWORD': first('POSTGRES_PASSWORD'),
}
missing = [name for name, value in required.items() if placeholder(value)]
if missing:
    print('echo ' + shlex.quote('ERROR: missing or placeholder values: ' + ','.join(missing)))
    print('exit 1')
    raise SystemExit

database_url = first('DATABASE_URL')
if database_url and not database_url.startswith('postgresql'):
    print('echo ' + shlex.quote('ERROR: DATABASE_URL must be PostgreSQL or left empty to use POSTGRES_*'))
    print('exit 1')
    raise SystemExit

try:
    interval = int(first('CHECK_INTERVAL') or '60')
except ValueError:
    print('echo ' + shlex.quote('ERROR: CHECK_INTERVAL must be an integer'))
    print('exit 1')
    raise SystemExit
if interval < 10:
    print('echo ' + shlex.quote('ERROR: CHECK_INTERVAL must be >= 10 seconds'))
    print('exit 1')
    raise SystemExit
if interval > 600:
    print('echo ' + shlex.quote(f'WARNING: CHECK_INTERVAL={interval}s; SMM notifications may be delayed. 60s is recommended.'))

parser_enabled = first('TELEGRAM_PARSER_ENABLED').lower() in {'1','true','yes','on'}
if parser_enabled:
    api_id = first('API_ID')
    api_hash = first('API_HASH')
    if placeholder(api_hash) or placeholder(api_id) or api_id == '123456':
        print('echo ' + shlex.quote('ERROR: TELEGRAM_PARSER_ENABLED=true but API_ID/API_HASH are placeholders'))
        print('exit 1')
        raise SystemExit

print('TELEGRAM_PARSER_ENABLED=' + ('true' if parser_enabled else 'false'))
PY
)"

# Validate Compose only after local runtime files are known to exist.
docker compose config --quiet || fail "invalid Docker Compose configuration"

compose_args=()
app_services=(app)
if [[ "$TELEGRAM_PARSER_ENABLED" == "true" ]]; then
  compose_args+=(--profile telegram)
  app_services+=(telegram-parser)
else
  # Remove the optional worker if it was enabled by an older release.
  docker compose --profile telegram stop telegram-parser >/dev/null 2>&1 || true
  docker compose --profile telegram rm -f telegram-parser >/dev/null 2>&1 || true
fi

# Build the new application image while the currently deployed app can still run.
docker compose "${compose_args[@]}" build --pull "${app_services[@]}"

# Freeze application writes before copying the legacy SQLite database.
docker compose --profile telegram stop app telegram-parser >/dev/null 2>&1 || true

# PostgreSQL is internal-only: it is not published on a host port.
docker compose up -d postgres

for _ in $(seq 1 30); do
  postgres_id="$(docker compose ps -q postgres)"
  if [[ -n "$postgres_id" ]]; then
    postgres_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$postgres_id" 2>/dev/null || true)"
    if [[ "$postgres_health" == "healthy" ]]; then
      break
    fi
  fi
  sleep 2
done

postgres_id="$(docker compose ps -q postgres)"
[[ -n "$postgres_id" ]] || fail "postgres container did not start"
postgres_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$postgres_id" 2>/dev/null || true)"
[[ "$postgres_health" == "healthy" ]] || {
  docker compose logs --tail=200 postgres >&2
  fail "postgres health check failed"
}

# Safe and idempotent first-run migration:
# - if PostgreSQL is empty and /app/data/posts.db exists -> backup + import;
# - if PostgreSQL already has rows -> do not overwrite it on later deployments.
docker compose run --rm --no-deps \
  -e BACKGROUND_WORKERS_ENABLED=false \
  app python -m src.migrate_sqlite_to_postgres

# Start the application only after PostgreSQL is ready and migration succeeded.
up_services=(postgres "${app_services[@]}")
docker compose "${compose_args[@]}" up -d --remove-orphans "${up_services[@]}"
docker compose "${compose_args[@]}" ps

# Wait for the FastAPI src.main container healthcheck.
for _ in $(seq 1 30); do
  container_id="$(docker compose ps -q app)"
  if [[ -n "$container_id" ]]; then
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
    if [[ "$health" == "healthy" ]]; then
      printf 'Deployment is healthy: src.main is running in container %s\n' "$container_id"
      ./deploy/verify_deployment.sh
      if [[ "$TELEGRAM_PARSER_ENABLED" == "true" ]]; then
        printf 'Telegram parser enabled; inspect with: docker compose --profile telegram logs telegram-parser\n'
      else
        printf 'Telegram parser is disabled; VK polling + AI + Telegram SMM notifications are active in app.\n'
      fi
      exit 0
    fi
  fi
  sleep 5
done

printf 'Application health check failed\n' >&2
docker compose "${compose_args[@]}" logs --tail=250 postgres "${app_services[@]}" >&2
exit 1
