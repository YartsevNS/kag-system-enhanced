"""
Middleware Setup Checker для KAG.

Если система не настроена, редиректит на /setup.
Пропускает только: /setup, /api/v1/setup, /login, /api/v1/auth, /static, /health, /docs.
"""

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from loguru import logger


class SetupCheckMiddleware(BaseHTTPMiddleware):
    """
    Middleware для проверки первоначальной настройки.
    Если setup не завершён — редирект на /setup (кроме публичных путей).
    """

    PUBLIC_PATHS = {
        "/setup",
        "/api/v1/setup",
        "/api/v1/health",
        "/login",
        "/api/v1/auth",
        "/api/docs",
        "/api/redoc",
        "/api/openapi.json",
        "/static",
        "/favicon.ico",
    }

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Публичные пути (включая сам setup) — пропускаем
        if any(path.startswith(p) for p in self.PUBLIC_PATHS):
            return await call_next(request)

        # Статические файлы
        if path.startswith("/static/"):
            return await call_next(request)

        # Проверяем статус настройки
        try:
            from src.api.services.config_store import config_store

            setup_status = config_store.get("setup", "status", {})
            logger.debug(f"SetupCheck: status={setup_status}")
            if setup_status and not setup_status.get("configured", False):
                logger.info(f"SetupCheck: редирект {path} → /setup (не настроено)")
                return RedirectResponse(url="/setup", status_code=302)
        except Exception:
            pass

        return await call_next(request)
