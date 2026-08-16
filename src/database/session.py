"""
Database session management for FastAPI.

Единая точка создания SQLAlchemy engine:
- Один URL для БД kag (не keycloak) — из KAG_DB_URL, с fallback на DATABASE_URL.
- Один singleton-engine для всех сервисов (config_store, document_repository, get_db).
- Идемпотентная инициализация схемы через ensure_schema.
- reset_db_engine() — сброс после смены пароля/URL (setup wizard).
"""

import os
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

from src.config import get_settings

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None
_engine_db_url: Optional[str] = None


def _is_valid_db_url(url: str) -> bool:
    """URL считается валидным, если задан и не содержит маску пароля `***`."""
    return bool(url) and "***" not in url and "://" in url


def resolve_db_url() -> str:
    """
    Определить URL БД kag.

    Приоритет:
    1. env KAG_DB_URL (если задан и не замаскирован)
    2. env DATABASE_URL
    3. settings.KAG_DB_URL / settings.DATABASE_URL
    4. сборка из KC_DB_* НЕ производится — это БД keycloak, не kag.
    """
    for candidate in (
        os.environ.get("KAG_DB_URL", ""),
        os.environ.get("DATABASE_URL", ""),
    ):
        if _is_valid_db_url(candidate):
            return candidate

    try:
        settings = get_settings()
        if _is_valid_db_url(settings.KAG_DB_URL):
            return settings.KAG_DB_URL
        if _is_valid_db_url(settings.DATABASE_URL):
            return settings.DATABASE_URL
    except Exception:
        pass

    # Последний шанс — env KAG_DB_URL даже с маской (для первого старта до setup):
    # вернём его как есть, а подключение само упадёт мягко при недоступности.
    raw = os.environ.get("KAG_DB_URL", "")
    if raw:
        return raw
    return "postgresql://kag:kag@localhost:5432/kag"


def _create_engine(db_url: str) -> Engine:
    connect_args = {}
    if "sqlite" in db_url:
        connect_args["check_same_thread"] = False
    return create_engine(db_url, connect_args=connect_args, pool_pre_ping=True)


def get_engine() -> Engine:
    """Единый singleton-engine (ленивое создание)."""
    global _engine, _SessionLocal, _engine_db_url
    if _engine is None:
        db_url = resolve_db_url()
        _engine = _create_engine(db_url)
        _engine_db_url = db_url
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        # Идемпотентно: создаёт таблицы и добавляет недостающие колонки.
        ensure_schema_retry(_engine)
    return _engine


def ensure_schema_retry(engine: Engine, attempts: int = 5, delay: float = 2.0) -> None:
    """
    Применить схему БД с повторными попытками.

    При старте контейнеров БД может ещё не принимать подключения,
    поэтому ensure_schema повторяется, а не проглатывается раз и навсегда.
    """
    import time as _time
    from src.database.migrations import ensure_schema
    import logging
    _log = logging.getLogger(__name__)
    for attempt in range(1, attempts + 1):
        try:
            ensure_schema(engine)
            return
        except Exception as e:
            if attempt == attempts:
                _log.warning(f"ensure_schema не выполнен после {attempts} попыток: {e}")
                return
            _log.warning(f"ensure_schema попытка {attempt}/{attempts} не удалась: {e}; повтор через {delay}с")
            _time.sleep(delay)


def get_session_local() -> sessionmaker:
    get_engine()
    return _SessionLocal


def reset_db_engine() -> None:
    """Сбросить engine после смены KAG_DB_URL (setup wizard)."""
    global _engine, _SessionLocal, _engine_db_url
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:
            pass
    _engine = None
    _SessionLocal = None
    _engine_db_url = None


def get_db() -> Session:
    """
    FastAPI dependency that yields a SQLAlchemy session.

    Usage:
        @router.get("/items")
        def list_items(db: Session = Depends(get_db)):
            ...
    """
    get_engine()
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Обратная совместимость (старый код импортирует _get_engine / _SessionLocal) ──

def _get_engine() -> Engine:
    """Legacy-алиас для get_engine()."""
    return get_engine()

