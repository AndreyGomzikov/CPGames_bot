#!/usr/bin/env bash
set -euo pipefail

DEPLOY_PATH="${DEPLOY_PATH:-/var/sftp/cpgamesbot}"
DEPLOY_USER="${DEPLOY_USER:-${SUDO_USER:-root}}"
SSH_PORT="${SSH_PORT:-22}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

[[ "$(id -u)" -eq 0 ]] || fail "run this script as root"
id "$DEPLOY_USER" >/dev/null 2>&1 || fail "user does not exist: $DEPLOY_USER"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git openssl python3 rsync ufw

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

systemctl enable --now docker
usermod -aG docker "$DEPLOY_USER"

install -d -m 750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$DEPLOY_PATH"

if [[ "$SOURCE_DIR" != "$DEPLOY_PATH" ]]; then
  rsync -a --delete \
    --exclude='.git/' \
    --exclude='.env' \
    --exclude='credentials.json' \
    --exclude='*.session' \
    --exclude='*.session-journal' \
    --exclude='data/' \
    "$SOURCE_DIR/" "$DEPLOY_PATH/"
  chown -R "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_PATH"
fi

ufw allow "${SSH_PORT}/tcp"
ufw --force enable

chmod 755 "$DEPLOY_PATH"/deploy/*.sh
printf 'VPS bootstrap completed. Deploy path: %s; owner: %s\n' \
  "$DEPLOY_PATH" "$DEPLOY_USER"
docker --version
docker compose version
ufw status
