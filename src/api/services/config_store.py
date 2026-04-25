"""
PostgreSQL Config Store для KAG

Хранит настройки системы в PostgreSQL (надежно, транзакционно).
Использует библиотеку SQLAlchemy.
"""

from typing import Dict, Any, Optional
import json
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from loguru import logger
from src.config import get_settings
from src.database.models import Base, SystemConfig


class PostgresConfigStore:
    """
    Хранилище конфигурации в PostgreSQL.
    
    Ключи хранятся в формате ID: {category}:{key}
    """

    def __init__(self, db_url: Optional[str] = None):
        """
        Инициализация хранилища.

        Args:
            db_url: URL базы данных (если не передан, строится из конфига)
        """
        settings = get_settings()

        if db_url:
            self._db_url = db_url
        else:
            # Строим URL из настроек Settings
            self._db_url = (
                f"postgresql://{settings.KC_DB_USERNAME}:{settings.KC_DB_PASSWORD}"
                f"@{settings.KC_DB_HOST}:{settings.KC_DB_PORT}/{settings.KC_DB_NAME}"
            )

        logger.info(f"Postgres Config Store: инициализация, db_url={self._db_url}")

        try:
            self._engine = create_engine(self._db_url, pool_pre_ping=True)
            self._Session = sessionmaker(bind=self._engine)

            try:
                Base.metadata.create_all(self._engine)
                logger.info(f"Postgres Config Store подключен: {self._db_url}")
                logger.info("Таблица system_configs проверена/создана")
            except Exception as db_err:
                logger.warning(f"БД недоступна, использую в памяти: {db_err}")
                self._engine = None
                self._Session = None
        except Exception as e:
            logger.warning(f"БД недоступна, использую в памяти: {e}")
            self._engine = None
            self._Session = None

    def _get_session(self):
        """Получить сессию БД"""
        if not self._Session:
            raise RuntimeError("База данных недоступна")
        return self._Session()

    def get(self, category: str, key: str = "default", default: Any = None) -> Any:
        """
        Получить значение из БД.
        """
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

    def set(self, category: str, key: str, value: Any) -> bool:
        """
        Сохранить значение в БД.
        """
        if not self._engine:
            # Пробуем переподключиться
            try:
                self._engine = create_engine(self._db_url, pool_pre_ping=True)
                self._Session = sessionmaker(bind=self._engine)
                Base.metadata.create_all(self._engine)
                logger.info("Postgres Config Store переподключен")
            except Exception as e:
                logger.debug(f"БД недоступна, пропускаю сохранение: {e}")
                return False

        try:
            session = self._get_session()
            config_id = f"{category}:{key}"

            # Сериализуем значение
            if isinstance(value, (dict, list, bool, int, float)):
                serialized = json.dumps(value)
            else:
                serialized = str(value)

            # Ищем существующую запись
            record = session.query(SystemConfig).filter_by(id=config_id).first()

            if record:
                record.value = serialized
                record.updated_at = datetime.utcnow()
            else:
                record = SystemConfig(
                    id=config_id,
                    category=category,
                    key=key,
                    value=serialized
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
        """
        Удалить значение из БД.
        """
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
            session.close()

    def get_all(self, category: str) -> Dict[str, Any]:
        """
        Получить все значения из категории.
        """
        try:
            session = self._get_session()
            records = session.query(SystemConfig).filter_by(category=category).all()
            
            result = {}
            for record in records:
                try:
                    result[record.key] = json.loads(record.value)
                except:
                    result[record.key] = record.value
            
            return result
        except Exception as e:
            logger.debug(f"БД недоступна, использую пустой кэш: {e}")
            return {}
        finally:
            if 'session' in locals():
                session.close()


# Глобальный экземпляр
config_store = PostgresConfigStore()
