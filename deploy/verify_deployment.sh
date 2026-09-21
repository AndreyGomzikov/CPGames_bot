#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

container_id="$(docker compose ps -q app)"
[[ -n "$container_id" ]] || {
  echo 'ERROR: app container is not running' >&2
  exit 1
}

health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
[[ "$health" == "healthy" ]] || {
  printf 'ERROR: app container health is %s\n' "$health" >&2
  exit 1
}

# Verify FastAPI from inside the container first.
docker compose exec -T app python - <<'PY'
import json
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5) as response:
    payload = json.loads(response.read().decode('utf-8'))
assert payload.get('status') == 'ok', payload
print(json.dumps(payload, ensure_ascii=False))
PY

# Verify that Docker publishes FastAPI on the VPS HTTP port used by the SMM panel.
python3 - <<'PY'
import json
import urllib.request

with urllib.request.urlopen('http://127.0.0.1/health', timeout=5) as response:
    payload = json.loads(response.read().decode('utf-8'))
assert payload.get('status') == 'ok', payload

with urllib.request.urlopen('http://127.0.0.1/', timeout=5) as response:
    assert response.status == 200, response.status
    response.read(1)

print('SMM web panel: http://127.0.0.1/ -> 200 OK')
PY

./deploy/verify_database.sh

printf 'Verified deployment: PostgreSQL + VK polling + Telegram SMM bot + HTTP web panel are running.\n'
