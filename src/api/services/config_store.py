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
            db_url: URL базы данных (если не передан, берется из конфига)
        """
        settings = get_settings()
        
        # Формируем URL для подключения к Keycloak DB (так как это наш единственный Postgres)
        # KC_DB_URL=jdbc:postgresql://keycloak-db:5432/keycloak
        # Превращаем в: postgresql://keycloak:keycloak_password@keycloak-db:5432/key
        
        if db_url:
            self._db_url = db_url
        else:
            # Дефолтные настройки для контейнера keycloak-db
            # Берем из конфига, если есть, иначе используем дефолт из docker-compose.yml
            db_user = getattr(settings, 'KC_DB_USERNAME', 'keycloak')
            db_pass = getattr(settings, 'KC_DB_PASSWORD', 'keycloak_password')
            self._db_url = f"postgresql://{db_user}:{db_pass}@keycloak-db:5432/keycloak"
        
        try:
            self._engine = create_engine(self._db_url, pool_pre_ping=True)
            self._Session = sessionmaker(bind=self._engine)
            
            # Создаем таблицу если не существует
            Base.metadata.create_all(self._engine)
            logger.info(f"Postgres Config Store подключен: {self._db_url}")
        except Exception as e:
            logger.error(f"Ошибка инициализации Postgres Config Store: {e}")
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
            logger.error(f"Ошибка получения {category}:{key}: {e}")
            return default

    def set(self, category: str, key: str, value: Any) -> bool:
        """
        Сохранить значение в БД.
        """
        if not self._engine:
            logger.error("БД недоступна")
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
            logger.error(f"Ошибка сохранения {category}:{key}: {e}")
            return False
        finally:
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
            logger.error(f"Ошибка получения категории {category}: {e}")
            return {}
        finally:
            session.close()


# Глобальный экземпляр
config_store = PostgresConfigStore()
