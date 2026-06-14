"""Middleware аутентификации через Keycloak или статический токен"""

import hmac
import os
import time
from typing import Optional, Set
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from loguru import logger

from src.auth.keycloak import verify_token

AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "false").lower() == "true"
STATIC_TOKEN = os.environ.get("KAG_API_TOKEN", "")

_KEYCLOAK_CACHE_TTL = 30
_keycloak_available: Optional[bool] = None
_keycloak_checked_at: float = 0.0

# Пути, доступные без авторизации
PUBLIC_PREFIXES: Set[str] = {
    "/login",
    "/setup",
    "/admin",
    "/",
    "/api/v1/auth",
    "/api/v1/health",
    "/api/v1/setup",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/static",
    "/favicon.ico",
}


def _check_keycloak_availability(keycloak_url: str, realm: str) -> bool:
    global _keycloak_available, _keycloak_checked_at

    now = time.monotonic()
    if _keycloak_available is not None and (now - _keycloak_checked_at) < _KEYCLOAK_CACHE_TTL:
        return _keycloak_available

    import httpx
    try:
        response = httpx.get(
            f"{keycloak_url}/realms/{realm}",
            timeout=5.0
        )
        _keycloak_available = response.status_code == 200
    except Exception as e:
        logger.warning(f"Keycloak недоступен: {e}")
        _keycloak_available = False

    _keycloak_checked_at = now
    return _keycloak_available


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware аутентификации.
    При AUTH_ENABLED=true требует валидный токен (JWT или статический).
    При AUTH_ENABLED=false пропускает все запросы (development mode).
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if not AUTH_ENABLED:
            return await call_next(request)

        # Публичные пути — пропускаем без авторизации
        if any(request.url.path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        from src.config import get_settings
        settings = get_settings()

        if not _check_keycloak_availability(settings.KEYCLOAK_URL, settings.KEYCLOAK_REALM):
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "detail": "Сервис аутентификации недоступен",
                    "hint": "Keycloak недоступен. Установите AUTH_ENABLED=false."
                }
            )

        token = self._extract_token(request)

        if not token:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Отсутствует токен аутентификации"}
            )

        try:
            if STATIC_TOKEN and hmac.compare_digest(token, STATIC_TOKEN):
                request.state.user = {"username": "api_user", "roles": ["api"]}
            else:
                payload = verify_token(token)
                request.state.user = payload
        except Exception as e:
            logger.warning(f"Неверный токен: {e}")
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Неверный токен аутентификации"}
            )

        protected_paths = ["/api/v1/admin"]
        if any(request.url.path.startswith(path) for path in protected_paths):
            user_roles = request.state.user.get("roles", [])
            if not self._check_admin_access(user_roles):
                logger.warning(
                    f"Доступ запрещён: user={request.state.user.get('username')}, "
                    f"path={request.url.path}"
                )
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Недостаточно прав для доступа"}
                )

        return await call_next(request)

    def _extract_token(self, request: Request) -> Optional[str]:
        """Извлечь JWT токен из заголовка Authorization"""
        auth_header = request.headers.get("Authorization")

        if not auth_header:
            return None

        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None

        return parts[1]

    def _check_admin_access(self, roles: list) -> bool:
        """Проверить, есть ли у пользователя права администратора"""
        return "admin" in roles or "kag-admin" in roles
