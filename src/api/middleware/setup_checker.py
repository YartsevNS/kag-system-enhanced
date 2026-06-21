"""Middleware Setup Checker для KAG.

Если система не настроена, редиректит на /setup.
Пропускает только: /setup, /api/v1/setup, /login, /api/v1/auth, /static, /health, /docs.
"""

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from loguru import logger


class SetupCheckMiddleware(BaseHTTPMiddleware):
    PUBLIC_PATHS = {
        "/setup", "/api/v1/setup", "/api/v1/health",
        "/login", "/api/v1/auth", "/api/docs",
        "/api/redoc", "/api/openapi.json", "/static", "/favicon.ico",
    }

    def _is_configured(self) -> bool:
        """Проверить что система настроена через прямой SQL (не config_store)."""
        import os
        from sqlalchemy import create_engine, text
        try:
            db_url = os.environ.get("KAG_DB_URL", "postgresql://kag:kagpass123@kag-db:5432/kag")
            engine = create_engine(db_url, pool_pre_ping=True, echo=False)
            with engine.connect() as conn:
                r = conn.execute(text("SELECT value FROM system_configs WHERE id='setup:status'"))
                row = r.fetchone()
                if row:
                    import json
                    data = json.loads(row[0])
                    return data.get("configured", False)
                # Fallback: проверяем наличие admin пользователя
                r2 = conn.execute(text("SELECT count(*) FROM users WHERE username='admin'"))
                return r2.scalar() > 0
        except Exception:
            return False  # при ошибке — пропускаем (считаем что не настроено? нет: считаем что ОК)
        return False

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if any(path.startswith(p) for p in self.PUBLIC_PATHS):
            return await call_next(request)
        if path.startswith("/static/"):
            return await call_next(request)

        if not self._is_configured():
            logger.info(f"SetupCheck: редирект {path} → /setup")
            return RedirectResponse(url="/setup", status_code=302)

        return await call_next(request)
