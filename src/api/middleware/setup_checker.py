"""
Middleware Setup Checker для KAG

Проверяет при каждом запросе, настроена ли система.
Если нет - редиректит на /setup
"""

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from loguru import logger


class SetupCheckMiddleware(BaseHTTPMiddleware):
    """
    Middleware для проверки первоначальной настройки.
    
    Если система не настроена, все запросы кроме /setup и статики
    будут перенаправляться на /setup.
    """

    async def dispatch(self, request: Request, call_next):
        """Проверка настройки при каждом запросе"""
        
        # Пути которые не требуют настройки
        public_paths = [
            "/setup",
            "/api/v1/setup",
            "/api/v1/health",
            "/static/setup.html",
            "/docs",
            "/openapi.json",
            "/redoc",
            "/favicon.ico",
        ]
        
        # Проверяем если это публичный путь
        if any(request.url.path.startswith(path) for path in public_paths):
            return await call_next(request)
        
        # Проверяем если это статический файл
        if request.url.path.startswith("/static/"):
            return await call_next(request)
        
        # Проверяем статус настройки
        try:
            from src.api.services.config_store import config_store
            setup_status = config_store.get("setup", "status", {})
            
            if not setup_status.get("configured", False):
                # Система не настроена - редиректим на setup
                logger.info(f"Система не настроена, редирект на /setup: {request.url.path}")
                return RedirectResponse(url="/setup", status_code=302)
        except Exception as e:
            logger.warning(f"Ошибка проверки статуса настройки: {e}")
            # В случае ошибки БД - пропускаем запрос
        
        return await call_next(request)
