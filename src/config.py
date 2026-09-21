import os
from urllib.parse import quote_plus

from dotenv import load_dotenv

# Корень проекта остаётся уровнем выше src/, чтобы runtime-данные и шаблоны
# сохранили прежние пути после переноса исходников в пакет src.
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SRC_DIR)
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Папка для хранения runtime-данных (Telegram session/state и legacy SQLite backup).
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# Legacy SQLite database path is kept only for one-time migration/backup.
DATABASE_PATH = os.path.join(DATA_DIR, "posts.db")


def _database_url() -> str:
    """Return configured DB URL, preferring PostgreSQL in production.

    DATABASE_URL may be supplied explicitly. Otherwise, when PostgreSQL
    credentials are present, build a URL safely (URL-encoding user/password).
    SQLite remains a local-development fallback so unit tests and one-off local
    runs do not require a PostgreSQL server.
    """
    explicit = os.getenv("DATABASE_URL", "").strip()
    if explicit:
        return explicit

    pg_password = os.getenv("POSTGRES_PASSWORD", "").strip()
    if pg_password:
        pg_user = os.getenv("POSTGRES_USER", "cpgames").strip() or "cpgames"
        pg_db = os.getenv("POSTGRES_DB", "cpgames").strip() or "cpgames"
        pg_host = os.getenv("POSTGRES_HOST", "postgres").strip() or "postgres"
        pg_port = os.getenv("POSTGRES_PORT", "5432").strip() or "5432"
        return (
            "postgresql+psycopg2://"
            f"{quote_plus(pg_user)}:{quote_plus(pg_password)}@"
            f"{pg_host}:{pg_port}/{quote_plus(pg_db)}"
        )

    return f"sqlite:///{DATABASE_PATH}"


DATABASE_URL = _database_url()

# Путь к файлу состояния парсера VK
PARSER_STATE_FILE = os.path.join(DATA_DIR, "vk_parser_state.json")

# Папка для экспортов (CSV, JSON и т.д.)
EXPORT_DIR = DATA_DIR

# HTML-шаблоны остаются в корневой папке templates/.
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
