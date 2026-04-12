"""
Middleware Setup Checker для KAG

Если система не настроена, все запросы (кроме setup и статики) 
перенаправляются на /setup.
"""

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from loguru import logger


class SetupCheckMiddleware(BaseHTTPMiddleware):
    """
    Middleware для проверки первоначальной настройки.
    """
    
    # Пути, которые ВСЕГДА доступны (даже если система не настроена)
    PUBLIC_PATHS = [
        "/setup",
        "/api/v1/setup",          # Все эндпоинты Setup Wizard
        "/api/v1/health",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/favicon.ico",
    ]

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        
        # 1. Проверяем если это публичный путь - пропускаем
        if any(path.startswith(p) for p in self.PUBLIC_PATHS):
            return await call_next(request)
        
        # 2. Проверяем если это статический файл
        if path.startswith("/static/"):
            return await call_next(request)
        
        # 3. Проверяем статус настройки
        try:
            from src.api.services.config_store import config_store
            
            # Проверяем только если БД доступна
            if config_store._engine:
                setup_status = config_store.get("setup", "status", {})
                
                if not setup_status.get("configured", False):
                    # Система не настроена - редирект
                    return RedirectResponse(url="/setup", status_code=302)
        except Exception as e:
            # Если БД недоступна - считаем что система не настроена
            if not path.startswith("/setup"):
                return RedirectResponse(url="/setup", status_code=302)
        
        return await call_next(request)
