#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

docker compose exec -T app python - <<'PY'
from sqlalchemy import func, select, text

from src.database import SessionLocal, engine
from src.models import Comment, Post, Prompt

if engine.dialect.name != "postgresql":
    raise SystemExit(f"ERROR: expected PostgreSQL, got {engine.dialect.name}")

with engine.connect() as conn:
    conn.execute(text("SELECT 1"))

with SessionLocal() as session:
    counts = {
        "posts": session.scalar(select(func.count()).select_from(Post)),
        "comments": session.scalar(select(func.count()).select_from(Comment)),
        "prompts": session.scalar(select(func.count()).select_from(Prompt)),
    }

print("Database: PostgreSQL OK")
print("Rows:", ", ".join(f"{key}={value}" for key, value in counts.items()))
PY
