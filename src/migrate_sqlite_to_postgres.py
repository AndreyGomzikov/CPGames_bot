"""One-time, safe migration from the legacy SQLite DB to PostgreSQL.

The command is intentionally idempotent for deployments:
- creates PostgreSQL tables if needed;
- if PostgreSQL already contains application rows, it does NOT overwrite them;
- if PostgreSQL is empty and legacy SQLite exists, backs SQLite up and migrates
  posts/comments/prompts in one transaction;
- resets PostgreSQL ID sequences and verifies row counts.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from .config import DATABASE_PATH, DATABASE_URL
from .database import Base
from .models import Comment, Post, Prompt

MODELS = (Post, Comment, Prompt)


def _row_count(session, model) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _counts(session) -> dict[str, int]:
    return {model.__tablename__: _row_count(session, model) for model in MODELS}


def _as_dict(row, model) -> dict:
    return {column.name: getattr(row, column.name) for column in model.__table__.columns}


def _reset_postgres_sequences(conn) -> None:
    for table_name in ("posts", "comments", "prompts"):
        max_id = conn.execute(text(f'SELECT MAX(id) FROM "{table_name}"')).scalar()
        if max_id is None:
            conn.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence(:table_name, 'id'), 1, false)"
                ),
                {"table_name": table_name},
            )
        else:
            conn.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence(:table_name, 'id'), :max_id, true)"
                ),
                {"table_name": table_name, "max_id": int(max_id)},
            )


def migrate(sqlite_path: Path, database_url: str, backup_path: Path | None = None) -> int:
    if not database_url.startswith("postgresql"):
        raise RuntimeError(
            "Target DATABASE_URL must be PostgreSQL. "
            "Set POSTGRES_* variables or DATABASE_URL before migration."
        )

    destination_engine = create_engine(database_url, pool_pre_ping=True)
    if destination_engine.dialect.name != "postgresql":
        raise RuntimeError("Destination engine is not PostgreSQL")

    # Import models before create_all (already imported above) so all metadata is known.
    Base.metadata.create_all(bind=destination_engine)
    DestinationSession = sessionmaker(bind=destination_engine)

    with DestinationSession() as destination_session:
        destination_counts = _counts(destination_session)

    if any(destination_counts.values()):
        print(
            "ℹ️ PostgreSQL уже содержит данные; автоматический импорт SQLite пропущен: "
            + ", ".join(f"{k}={v}" for k, v in destination_counts.items())
        )
        return 0

    if not sqlite_path.exists():
        print(
            f"ℹ️ Legacy SQLite не найден: {sqlite_path}. "
            "PostgreSQL инициализирован как новая пустая БД."
        )
        return 0

    if backup_path is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = sqlite_path.with_name(
            f"{sqlite_path.name}.pre-postgresql-{stamp}.bak"
        )
    shutil.copy2(sqlite_path, backup_path)
    print(f"✅ Резервная копия SQLite создана: {backup_path}")

    source_engine = create_engine(
        f"sqlite:///{sqlite_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SourceSession = sessionmaker(bind=source_engine)

    source_inspector = inspect(source_engine)
    with SourceSession() as source_session:
        source_counts: dict[str, int] = {}
        payloads: dict[str, list[dict]] = {}
        for model in MODELS:
            table_name = model.__tablename__
            if not source_inspector.has_table(table_name):
                source_counts[table_name] = 0
                payloads[table_name] = []
                print(f"ℹ️ В legacy SQLite нет таблицы {table_name}; считаем её пустой")
                continue
            source_counts[table_name] = _row_count(source_session, model)
            rows = source_session.scalars(select(model).order_by(model.id)).all()
            payloads[table_name] = [_as_dict(row, model) for row in rows]

    print(
        "📦 SQLite: "
        + ", ".join(f"{k}={v}" for k, v in source_counts.items())
    )

    # One DB transaction: either all application rows arrive, or none do.
    with destination_engine.begin() as conn:
        for model in MODELS:
            rows = payloads[model.__tablename__]
            if rows:
                conn.execute(model.__table__.insert(), rows)
        _reset_postgres_sequences(conn)

    with DestinationSession() as destination_session:
        migrated_counts = _counts(destination_session)

    if migrated_counts != source_counts:
        raise RuntimeError(
            f"Count verification failed: SQLite={source_counts}, PostgreSQL={migrated_counts}"
        )

    print(
        "✅ Миграция SQLite → PostgreSQL завершена и проверена: "
        + ", ".join(f"{k}={v}" for k, v in migrated_counts.items())
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy CPGames SQLite DB to PostgreSQL")
    parser.add_argument("--sqlite-path", default=DATABASE_PATH)
    parser.add_argument("--database-url", default=DATABASE_URL)
    parser.add_argument("--backup-path", default=None)
    args = parser.parse_args()
    return migrate(
        Path(args.sqlite_path),
        args.database_url,
        Path(args.backup_path) if args.backup_path else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
