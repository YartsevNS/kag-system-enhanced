"""
Маршрут проверки работоспособности
"""

from fastapi import APIRouter
from datetime import datetime

from src.models import HealthCheck
from src.config import get_settings

router = APIRouter()


@router.get("/health", response_model=HealthCheck, tags=["health"])
async def health_check():
    """
    Проверка работоспособности сервиса
    
    Возвращает статус сервиса, временную метку и версию.
    Используется для healthcheck в Docker и мониторинга.
    """
    settings = get_settings()
    
    return HealthCheck(
        status="ok",
        timestamp=datetime.utcnow(),
        version="0.1.0"
    )
