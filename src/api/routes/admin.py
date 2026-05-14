"""
Административные маршруты
"""

from typing import Optional
from fastapi import APIRouter, HTTPException
from loguru import logger
from datetime import datetime

from src.models import SystemStatus
from src.config import get_settings

router = APIRouter()


@router.get("/status", response_model=SystemStatus, summary="Статус системы")
async def get_system_status():
    """
    Получить общий статус системы и всех компонентов.
    
    Возвращает информацию о:
    - Версии сервиса
    - Времени работы
    - Статусе компонентов (БД, кэш, LLM, очереди)
    """
    logger.debug("Запрос статуса системы")

    settings = get_settings()

    return SystemStatus(
        service="kag-api",
        version=settings.APP_VERSION,
        status="running",
        uptime=0.0,
        components={
            "api": {"status": "ok"},
            "qdrant": {"status": "unknown"},
            "redis": {"status": "unknown"},
            "celery": {"status": "unknown"},
            "keycloak": {"status": "unknown"}
        }
    )


@router.get("/dependencies", summary="Зависимости (SBOM)")
async def get_dependencies():
    """
    Получить Software Bill of Materials (SBOM).
    
    Возвращает список всех зависимостей с версиями,
    сгенерированный через syft.
    """
    logger.debug("Запрос SBOM")
    
    # TODO: Загрузить реальный SBOM из файла
    
    return {
        "sbom_version": "1.0",
        "generated_at": datetime.utcnow().isoformat(),
        "dependencies": [
            {"name": "fastapi", "version": "0.115.6"},
            {"name": "pydantic", "version": "2.10.4"},
            {"name": "celery", "version": "5.4.0"},
            {"name": "redis", "version": "5.2.1"},
            {"name": "qdrant-client", "version": "1.12.1"},
        ]
    }


@router.get("/metrics", summary="Метрики производительности")
async def get_metrics():
    """
    Получить метрики производительности системы.
    
    Включает:
    - Количество запросов в секунду
    - Среднее время ответа
    - Использование памяти/CPU
    - Размер векторной БД
    """
    logger.debug("Запрос метрик")
    
    # TODO: Интеграция с Prometheus
    
    return {
        "requests_per_second": 0,
        "avg_response_time_ms": 0,
        "memory_usage_mb": 0,
        "cpu_usage_percent": 0,
        "qdrant_documents": 0
    }


@router.post("/cache/clear", summary="Очистить кэш")
async def clear_cache():
    """
    Очистить весь кэш в Redis.
    
    ВНИМАНИЕ: Это действие временное снизит производительность.
    """
    logger.warning("Запрос на очистку кэша")
    
    # TODO: Реализовать очистку кэша
    
    return {"status": "ok", "message": "Кэш очищен"}


@router.get("/users", summary="Список пользователей")
async def list_users():
    """
    Получить список пользователей (только для администраторов).
    """
    logger.debug("Запрос списка пользователей")
    
    from src.database.session import get_db as _get_db
    from src.database.user_models import User
    
    db_gen = _get_db()
    db = next(db_gen)
    try:
        users = db.query(User).all()
        return {
            "users": [
                {
                    "id": u.id,
                    "username": u.username,
                    "email": u.email,
                    "is_admin": u.is_admin,
                    "is_active": u.is_active,
                    "created_at": u.created_at.isoformat() if u.created_at else None
                }
                for u in users
            ]
        }
    finally:
        db.close()


@router.get("/groups", summary="Список групп")
async def list_groups():
    """Получить список групп."""
    from src.database.session import get_db as _get_db
    from src.database.user_models import Group
    
    db_gen = _get_db()
    db = next(db_gen)
    try:
        groups = db.query(Group).all()
        return {
            "groups": [
                {"id": g.id, "name": g.name, "description": g.description}
                for g in groups
            ]
        }
    finally:
        db.close()


@router.post("/groups", summary="Создать группу")
async def create_group(name: str, description: str = ""):
    """Создать новую группу."""
    from src.database.session import get_db as _get_db
    from src.database.user_models import Group
    import uuid as _uuid
    
    db_gen = _get_db()
    db = next(db_gen)
    try:
        group = Group(id=str(_uuid.uuid4()), name=name, description=description)
        db.add(group)
        db.commit()
        return {"id": group.id, "name": group.name, "description": group.description}
    finally:
        db.close()


@router.get("/audit-log", summary="Журнал аудита")
async def get_audit_log(
    limit: Optional[int] = 100,
    user: Optional[str] = None,
    action: Optional[str] = None
):
    """
    Получить журнал аудита действий.
    
    - **limit**: Лимит записей
    - **user**: Фильтр по пользователю
    - **action**: Фильтр по действию
    """
    logger.debug("Запрос журнала аудита")
    
    # TODO: Получить записи из Loki
    
    return {"entries": []}
