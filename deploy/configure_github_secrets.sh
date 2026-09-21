#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="${1:-}"
[[ -n "$REPOSITORY" ]] || {
  printf 'Usage: %s owner/repository\n' "$0" >&2
  exit 1
}

command -v gh >/dev/null 2>&1 || {
  printf 'GitHub CLI (gh) is required\n' >&2
  exit 1
}
command -v ssh-keyscan >/dev/null 2>&1 || {
  printf 'ssh-keyscan is required\n' >&2
  exit 1
}

gh auth status
read -r -p 'VPS host or IP [161.97.72.91]: ' VPS_HOST
read -r -p 'VPS SSH user [andrewgf1]: ' VPS_USER
read -r -p 'VPS SSH port [22]: ' VPS_PORT
read -r -p 'VPS deploy path [/var/sftp/cpgamesbot]: ' VPS_DEPLOY_PATH
read -r -p 'Path to private deploy key: ' PRIVATE_KEY_PATH
VPS_HOST="${VPS_HOST:-161.97.72.91}"
VPS_USER="${VPS_USER:-andrewgf1}"
VPS_PORT="${VPS_PORT:-22}"
VPS_DEPLOY_PATH="${VPS_DEPLOY_PATH:-/var/sftp/cpgamesbot}"

[[ -f "$PRIVATE_KEY_PATH" ]] || {
  printf 'Private key not found: %s\n' "$PRIVATE_KEY_PATH" >&2
  exit 1
}

KNOWN_HOSTS_FILE="$(mktemp)"
trap 'rm -f "$KNOWN_HOSTS_FILE"' EXIT
ssh-keyscan -p "$VPS_PORT" -H "$VPS_HOST" > "$KNOWN_HOSTS_FILE"
[[ -s "$KNOWN_HOSTS_FILE" ]] || {
  printf 'Could not read the VPS SSH host key\n' >&2
  exit 1
}

printf 'Verify these VPS host-key fingerprints before continuing:\n'
ssh-keygen -lf "$KNOWN_HOSTS_FILE"
read -r -p 'Type YES after verification: ' CONFIRMATION
[[ "$CONFIRMATION" == "YES" ]] || {
  printf 'Cancelled\n' >&2
  exit 1
}

printf '%s' "$VPS_HOST" | gh secret set VPS_HOST --repo "$REPOSITORY"
printf '%s' "$VPS_USER" | gh secret set VPS_USER --repo "$REPOSITORY"
printf '%s' "$VPS_PORT" | gh secret set VPS_PORT --repo "$REPOSITORY"
printf '%s' "$VPS_DEPLOY_PATH" | gh secret set VPS_DEPLOY_PATH --repo "$REPOSITORY"
gh secret set VPS_SSH_PRIVATE_KEY --repo "$REPOSITORY" < "$PRIVATE_KEY_PATH"
gh secret set VPS_KNOWN_HOSTS --repo "$REPOSITORY" < "$KNOWN_HOSTS_FILE"
printf 'GitHub repository deployment secrets configured for %s\n' "$REPOSITORY"
