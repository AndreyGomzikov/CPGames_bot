#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-/var/sftp/cpgamesbot}"

printf 'Project directory: %s\n' "$PROJECT_DIR"
[[ -d "$PROJECT_DIR" ]] || { echo 'ERROR: project directory does not exist' >&2; exit 1; }
[[ -w "$PROJECT_DIR" ]] || { echo 'ERROR: project directory is not writable' >&2; exit 1; }

for command in docker python3; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'ERROR: required command is missing: %s\n' "$command" >&2
    exit 1
  }
done

docker ps >/dev/null 2>&1 || {
  echo 'ERROR: current user cannot access Docker daemon' >&2
  exit 1
}
docker compose version >/dev/null

[[ -f "$PROJECT_DIR/.env" ]] || { echo 'ERROR: .env is missing' >&2; exit 1; }
[[ -f "$PROJECT_DIR/credentials.json" ]] || {
  echo 'ERROR: credentials.json is missing (required for Google Sheets/LLM context)' >&2
  exit 1
}

printf 'Docker: OK\n'
printf 'Docker Compose: OK\n'
printf 'Write access: OK\n'
printf '.env: OK\n'
printf 'credentials.json: OK\n'
printf 'HTTP port 80 is used by the SMM web panel; HTTPS/443 is not required.\n'
printf 'Server preflight: OK\n'
