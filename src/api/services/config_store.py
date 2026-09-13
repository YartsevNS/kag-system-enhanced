"""
PostgreSQL Config Store для KAG

Хранит настройки системы в PostgreSQL (надежно, транзакционно).
Использует единый engine из src.database.session (та же БД kag, что и документы).

При недоступности БД работает в «памяти» (get → default, set → False),
не падая — это позволяет стартовать до прохождения setup wizard.
"""

from typing import Dict, Any, Optional, Tuple
import copy
import json
import threading
import time
from datetime import datetime
from loguru import logger

from src.database.models import SystemConfig
from src.database.session import get_engine, reset_db_engine


class PostgresConfigStore:
    """
    Хранилище конфигурации в PostgreSQL.

    Ключи хранятся в формате ID: {category}:{key}
    """

    # Короткий кэш чтения. Замер 2026-09-13: config_store.get = ~10 мс
    # (сеанс SQLAlchemy + запрос + разбор JSON), а вызывается он десятками мест
    # в async-коде. Две секунды убирают эти 10 мс с повторных чтений и при этом
    # оставляют изменения, сделанные другим процессом, видимыми почти сразу.
    CACHE_TTL_SECONDS = 2.0

    def __init__(self):
        # Ленивое подключение через единый engine (src.database.session).
        self._db_available = None  # None = не проверяли
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()

    # ── Кэш чтения ──────────────────────────────────────────────────────

    def _cache_get(self, config_id: str):
        with self._cache_lock:
            hit = self._cache.get(config_id)
        if not hit:
            return False, None
        expires_at, value = hit
        if expires_at < time.monotonic():
            with self._cache_lock:
                self._cache.pop(config_id, None)
            return False, None
        # Копия: вызывающий код не должен менять закэшированное значение.
        return True, copy.deepcopy(value)

    def _cache_put(self, config_id: str, value: Any) -> None:
        with self._cache_lock:
            self._cache[config_id] = (time.monotonic() + self.CACHE_TTL_SECONDS, value)

    def invalidate(self, category: str, key: str = "default") -> None:
        """Сбросить кэш по ключу (зовётся из set/delete/compare_and_set)."""
        with self._cache_lock:
            self._cache.pop(f"{category}:{key}", None)

    def _get_session(self):
        """Вернуть SQLAlchemy-сессию. Бросает исключение, если БД недоступна."""
        from src.database.session import get_session_local
        return get_session_local()()

    # ── Чтение ──────────────────────────────────────────────────────────

    def get(self, category: str, key: str = "default", default: Any = None) -> Any:
        config_id = f"{category}:{key}"
        cached, value = self._cache_get(config_id)
        if cached:
            return value
        try:
            session = self._get_session()
            config_id = f"{category}:{key}"
            try:
                record = session.query(SystemConfig).filter_by(id=config_id).first()
                if record and record.value:
                    # _decode_value: JSON, а если не разбирается — сырая строка
                    # (так лежат значения, записанные до 2026-09-13: «idle»,
                    # «running»). Прямой json.loads на них падал и отдавал None.
                    value = self._decode_value(record.value)
                    self._cache_put(config_id, value)
                    return value
                # Отрицательный кэш: «ключа нет» — тоже результат на TTL. Иначе
                # каждое чтение отсутствующего ключа идёт в БД (замер: 1.1 мс
                # против 0.006 мс из кэша). Семантика та же: свой set инвалидирует
                # ключ (виден сразу), чужая запись — за ≤ CACHE_TTL_SECONDS.
                self._cache_put(config_id, default)
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

            # ВСЁ пишем как JSON, включая строки: иначе round-trip ломается —
            # сырое «123» читалось как int, а «running» вообще как None
            # (json.loads не разбирает незакавыченный текст).
            try:
                serialized = json.dumps(value)
            except TypeError:
                serialized = json.dumps(str(value))

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
            self.invalidate(category, key)
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
            self.invalidate(category, key)
            return count > 0
        except Exception as e:
            logger.error(f"Ошибка удаления {category}:{key}: {e}")
            return False
        finally:
            if 'session' in locals():
                session.close()

    @staticmethod
    def _decode_value(raw: str) -> Any:
        """Разобрать значение из БД так же, как его пишет set().

        set() кладёт dict/list/bool/int/float через json.dumps, а строки — как есть
        (без кавычек), поэтому json.loads на «idle» падает: пробуем JSON, затем
        возвращаем строку.
        """
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw


    def compare_and_set(self, category: str, key: str, new_value: Any,
                        expected: Any) -> bool:
        """Атомарно записать значение, только если текущее равно expected.

        Нужно против гонки check-then-act: два параллельных запроса (например, два
        запуска перестроения графа) оба видели «не running» и запускали задачу дважды.
        Реализация — один UPDATE с условием по значению, а не чтение + запись.

        ВАЖНО про кэш: CAS всегда идёт в БД и кэш не читает, поэтому паттерн
        «get (возможно, стухший) → CAS» безопасен. Пример — /rebuild-graph: процесс B
        может видеть в кэше «idle», пока процесс A уже записал «running»; CAS вернёт
        False (в БД лежит running), роут отдаст 409. Именно поэтому get кэшируется,
        а compare_and_set — нет.
        """
        try:
            session = self._get_session()
            config_id = f"{category}:{key}"
            try:
                from src.database.models import SystemConfig
                current = session.query(SystemConfig).filter_by(id=config_id).first()
                current_value = self._decode_value(current.value) if current else None
                if current_value != expected:
                    return False
                payload = json.dumps(new_value, ensure_ascii=False) if isinstance(
                    new_value, (dict, list, bool, int, float)) else str(new_value)
                if current:
                    updated = session.query(SystemConfig).filter(
                        SystemConfig.id == config_id,
                        SystemConfig.value == current.value,
                    ).update({"value": payload}, synchronize_session=False)
                else:
                    session.add(SystemConfig(id=config_id, value=payload))
                    updated = 1
                session.commit()
                self.invalidate(category, key)
                return bool(updated)
            finally:
                session.close()
        except Exception as e:
            logger.error(f"Ошибка compare_and_set {category}:{key}: {e}")
            return False


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
