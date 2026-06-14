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
    
# Пути, которые ВСЕГДА доступны
    PUBLIC_PATHS = [
        "/setup",
        "/api/v1/setup",
        "/api/v1/health",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/favicon.ico",
        "/api/v1/admin_models/",
        "/api/v1/chat/",
        "/api/v1/upload/",
        "/api/v1/auth",
        "/api/v1/notifications",
        "/api/v1/documents/",
        "/qdrant",
        "/chunks",
        "/documents",
        "/admin",
        "/static/",
    ]
    
    # Также пропускаем если БД недоступна
    DATABASE_AVAILABLE = True

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
            
            # Если БД недоступна - пропускаем ( система в режиме разработки )
            if not config_store._engine:
                return await call_next(request)
            
            setup_status = config_store.get("setup", "status", {})
            
            if not setup_status.get("configured", False):
                return RedirectResponse(url="/setup", status_code=302)
        except Exception as e:
            # Если БД недоступна - пропускаем
            pass
        
        return await call_next(request)
