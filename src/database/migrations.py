"""
Идемпотентная инициализация схемы БД.

`create_all` создаёт только НОВЫЕ таблицы, но не добавляет колонки в уже
существующие. Этот модуль закрывает разрыв: после create_all выполняет
лёгкие `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` для колонок, добавленных
в модели позже. Безопасно запускать при каждом старте.
"""

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.database.models import Base
from src.database.user_models import User, Group  # noqa: F401 — регистрация моделей
from src.database.document_models import Document, DocumentVersion  # noqa: F401
from src.database.monitoring_models import WatchedURL, WatchedFolder, Notification  # noqa: F401
from src.database.chat_models import ChatSession, ChatMessage  # noqa: F401 — регистрация моделей
from src.database.entity_alias_models import EntityAlias  # noqa: F401 — словарь алиасов для entity resolution

logger = logging.getLogger(__name__)

# Лёгкие миграции: (таблица, колонка, DDL-тип).
# При изменении моделей дополняйте этот список — ensure_schema подхватит.
_COLUMN_MIGRATIONS = [
    # documents — классификация и версионность (Фаза 2)
    ("documents", "document_type", "VARCHAR DEFAULT ''"),
    ("documents", "recognized_title", "VARCHAR DEFAULT ''"),
    ("documents", "summary", "TEXT DEFAULT ''"),
    ("documents", "topics", "TEXT DEFAULT '[]'"),
    ("documents", "previous_hash", "VARCHAR DEFAULT ''"),
    ("documents", "original_text", "TEXT"),
    ("documents", "source_metadata", "TEXT"),
]


def _ensure_columns(engine: Engine) -> None:
    """Добавить отсутствующие колонки через ALTER TABLE ADD COLUMN IF NOT EXISTS."""
    for table, column, ddl_type in _COLUMN_MIGRATIONS:
        stmt = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as e:
            # Таблица может ещё не существовать (первый запуск) — не критично,
            # create_all выше её создаст с полной схемой.
            logger.debug(f"Миграция {table}.{column} пропущена: {e}")


def ensure_schema(engine: Engine) -> None:
    """
    Создать таблицы (если нет) и применить лёгкие миграции колонок.

    Идемпотентно: можно вызывать при каждом старте и при каждом деплое.
    """
    Base.metadata.create_all(bind=engine)
    _ensure_columns(engine)
    logger.info("Схема БД актуальна (create_all + миграции колонок)")
