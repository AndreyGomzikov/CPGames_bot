#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"
[[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE not found" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo 'ERROR: python3 is required' >&2; exit 1; }

python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import secrets
import sys

path = Path(sys.argv[1])
raw = path.read_text(encoding="utf-8")
lines = raw.splitlines()

positions = {}
values = {}
for i, line in enumerate(lines):
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    positions[key] = i
    values[key] = value.strip()

existing_url = values.get("DATABASE_URL", "")
if existing_url and not existing_url.startswith("postgresql") and not existing_url.startswith("sqlite"):
    raise SystemExit(
        "ERROR: existing DATABASE_URL is neither PostgreSQL nor legacy SQLite; "
        "review it manually before deployment"
    )

updates = {
    "POSTGRES_DB": values.get("POSTGRES_DB") or "cpgames",
    "POSTGRES_USER": values.get("POSTGRES_USER") or "cpgames",
    "POSTGRES_PASSWORD": values.get("POSTGRES_PASSWORD") or secrets.token_urlsafe(32),
    "POSTGRES_HOST": values.get("POSTGRES_HOST") or "postgres",
    "POSTGRES_PORT": values.get("POSTGRES_PORT") or "5432",
}

# An old explicit SQLite URL must not override POSTGRES_* in src.config.
if existing_url.startswith("sqlite"):
    updates["DATABASE_URL"] = ""
elif "DATABASE_URL" not in values:
    updates["DATABASE_URL"] = ""

added = []
changed = []
for key, value in updates.items():
    rendered = f"{key}={value}"
    if key in positions:
        idx = positions[key]
        if lines[idx] != rendered:
            lines[idx] = rendered
            changed.append(key)
    else:
        added.append(key)

if added:
    if lines and lines[-1].strip():
        lines.append("")
    lines.append("# PostgreSQL (managed by deploy/prepare_postgres_env.sh)")
    for key in added:
        lines.append(f"{key}={updates[key]}")

path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("PostgreSQL env ready: " + ", ".join(updates))
if "POSTGRES_PASSWORD" in added:
    print("Generated a new strong POSTGRES_PASSWORD (value is intentionally not printed).")
if changed:
    print("Updated: " + ", ".join(changed))
PY

chmod 600 "$ENV_FILE"
