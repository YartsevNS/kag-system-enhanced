"""
PostgreSQL Config Store для KAG

Хранит настройки системы в PostgreSQL (надежно, транзакционно).
Использует единый engine из src.database.session (та же БД kag, что и документы).

При недоступности БД работает в «памяти» (get → default, set → False),
не падая — это позволяет стартовать до прохождения setup wizard.
"""

from typing import Dict, Any, Optional
import json
from datetime import datetime
from loguru import logger

from src.database.models import SystemConfig
from src.database.session import get_engine, reset_db_engine


class PostgresConfigStore:
    """
    Хранилище конфигурации в PostgreSQL.

    Ключи хранятся в формате ID: {category}:{key}
    """

    def __init__(self):
        # Ленивое подключение через единый engine (src.database.session).
        self._db_available = None  # None = не проверяли

    def _get_session(self):
        """Вернуть SQLAlchemy-сессию. Бросает исключение, если БД недоступна."""
        from src.database.session import get_session_local
        return get_session_local()()

    # ── Чтение ──────────────────────────────────────────────────────────

    def get(self, category: str, key: str = "default", default: Any = None) -> Any:
        try:
            session = self._get_session()
            config_id = f"{category}:{key}"
            try:
                record = session.query(SystemConfig).filter_by(id=config_id).first()
                if record and record.value:
                    return json.loads(record.value)
                return default
            finally:
                session.close()
        except Exception as e:
            logger.debug(f"Ошибка получения {category}:{key}: {e}")
            return default

    # ── Запись ──────────────────────────────────────────────────────────

    def set(self, category: str, key: str, value: Any) -> bool:
        try:
            session = self._get_session()
            config_id = f"{category}:{key}"

            if isinstance(value, (dict, list, bool, int, float)):
                serialized = json.dumps(value)
            else:
                serialized = str(value)

            record = session.query(SystemConfig).filter_by(id=config_id).first()
            if record:
                record.value = serialized
                record.updated_at = datetime.utcnow()
            else:
                record = SystemConfig(
                    id=config_id,
                    category=category,
                    key=key,
                    value=serialized,
                )
                session.add(record)

            session.commit()
            logger.debug(f"Сохранено в Postgres: {config_id}")
            return True
        except Exception as e:
            logger.debug(f"БД недоступна, пропускаю сохранение: {e}")
            return False
        finally:
            if 'session' in locals():
                session.close()

    def delete(self, category: str, key: str = "default") -> bool:
        try:
            session = self._get_session()
            config_id = f"{category}:{key}"
            count = session.query(SystemConfig).filter_by(id=config_id).delete()
            session.commit()
            return count > 0
        except Exception as e:
            logger.error(f"Ошибка удаления {category}:{key}: {e}")
            return False
        finally:
            if 'session' in locals():
                session.close()

    def get_all(self, category: str) -> Dict[str, Any]:
        try:
            session = self._get_session()
            records = session.query(SystemConfig).filter_by(category=category).all()
            result = {}
            for record in records:
                try:
                    result[record.key] = json.loads(record.value)
                except Exception:
                    result[record.key] = record.value
            return result
        except Exception as e:
            logger.debug(f"БД недоступна, использую пустой кэш: {e}")
            return {}
        finally:
            if 'session' in locals():
                session.close()

    # ── Управление подключением ────────────────────────────────────────

    def reset(self) -> None:
        """Сбросить подключение (после смены пароля/URL в setup wizard)."""
        reset_db_engine()


# Глобальный экземпляр
config_store = PostgresConfigStore()
