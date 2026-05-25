"""
SecurityMiddleware — проверка JWT (Keycloak JWKS + локальный fallback),
контроль ролей, логирование попыток доступа.

Заменяет устаревший AuthMiddleware.
"""

import time
from typing import Optional, Set

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from loguru import logger
from jose import jwt, jwk, JWTError

from src.config import get_settings

# ── Публичные пути (без токена) ───────────────────────────────────────

# Все, что НЕ начинается с /api/ — веб-страницы, статика — пропускаем
# Для API — проверяем токен, кроме явно публичных эндпоинтов

PUBLIC_API_PREFIXES: Set[str] = {
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/register",
    "/api/v1/health",
    "/api/v1/setup",
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
}

API_PREFIX = "/api/"

# ── Админские пути (требуют роль admin или kag-admin) ────────────────

ADMIN_PREFIXES: Set[str] = {
    "/api/v1/admin",
    "/api/v1/admin/",
}

ADMIN_ROLES: Set[str] = {"admin", "kag-admin"}

# ── Кеш JWKS ──────────────────────────────────────────────────────────

_jwks_cache: Optional[dict] = None
_jwks_checked_at: float = 0.0
_JWKS_CACHE_TTL = 300  # 5 минут


def _load_jwks(keycloak_url: str, realm: str) -> dict:
    """Загрузить JWKS с Keycloak (с кешированием на 5 минут)."""
    global _jwks_cache, _jwks_checked_at

    now = time.monotonic()
    if _jwks_cache is not None and (now - _jwks_checked_at) < _JWKS_CACHE_TTL:
        return _jwks_cache

    import urllib.request

    url = f"{keycloak_url}/realms/{realm}/protocol/openid-connect/certs"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            _jwks_cache = json.loads(resp.read())
    except Exception as e:
        logger.warning(f"[SEC] JWKS загрузка не удалась: {e}")
        _jwks_cache = {"keys": []}  # пустой кеш при ошибке

    _jwks_checked_at = now
    return _jwks_cache


import json


def _verify_keycloak(token: str, keycloak_url: str, realm: str) -> dict:
    """Проверить токен через JWKS Keycloak. Райзит JWTError при неудаче."""
    jwks = _load_jwks(keycloak_url, realm)

    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise JWTError("Token header missing 'kid'")

    key_data = next(
        (k for k in jwks.get("keys", []) if k.get("kid") == kid), None
    )
    if not key_data:
        raise JWTError(f"Key '{kid}' not found in JWKS")

    public_key = jwk.construct(key_data)

    payload = jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        audience="kag-api",
        options={"verify_exp": True, "verify_aud": True},
    )
    return payload


def _verify_local(token: str, jwt_secret: str, algorithm: str) -> dict:
    """Проверить локально выпущенный JWT (из auth.py). Райзит JWTError при неудаче."""
    return jwt.decode(
        token,
        jwt_secret,
        algorithms=[algorithm],
        options={"verify_exp": True},
    )


def _extract_roles(payload: dict) -> Set[str]:
    """Извлечь роли из payload (Keycloak или локальный формат)."""
    roles = set()

    # Keycloak: realm_access.roles
    realm_access = payload.get("realm_access") or {}
    roles.update(realm_access.get("roles", []))

    # Локальный формат: roles в корне
    local_roles = payload.get("roles", [])
    if isinstance(local_roles, list):
        roles.update(local_roles)

    # На случай если roles пришло строкой
    if isinstance(payload.get("role"), str):
        roles.add(payload["role"])

    return roles


def _get_username(payload: dict) -> str:
    """Извлечь имя пользователя из payload."""
    return (
        payload.get("preferred_username")
        or payload.get("sub")
        or payload.get("username")
        or "unknown"
    )


# ── Middleware ─────────────────────────────────────────────────────────

class SecurityMiddleware(BaseHTTPMiddleware):
    """
    Проверяет JWT для всех запросов, кроме PUBLIC_PREFIXES.

    Приоритет проверки:
    1. Keycloak JWKS (если токен содержит 'kid' в заголовке)
    2. Локальный JWT_SECRET (fallback)
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # 1. Не-API пути — веб-страницы, статика — без проверки
        if not path.startswith(API_PREFIX):
            return await call_next(request)

        # 2. Публичные API — без проверки
        if any(path.startswith(p) for p in PUBLIC_API_PREFIXES):
            return await call_next(request)

        # 2. Извлечь токен
        token = self._extract_token(request)
        if not token:
            logger.warning(
                f"[SEC] 401 — нет токена: {request.method} {path} "
                f"from {request.client.host if request.client else '?'}"
            )
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Требуется аутентификация"},
            )

        # 3. Проверить токен
        settings = get_settings()
        payload = None
        error_detail = "Токен недействителен"

        # 3a. Пробуем Keycloak JWKS (если есть kid)
        try:
            unverified_header = jwt.get_unverified_header(token)
            if "kid" in unverified_header:
                payload = _verify_keycloak(
                    token,
                    settings.KEYCLOAK_URL,
                    settings.KEYCLOAK_REALM,
                )
        except JWTError as e:
            logger.debug(f"[SEC] Keycloak verification failed: {e}")
        except Exception as e:
            logger.warning(f"[SEC] Keycloak JWKS error: {e}")

        # 3b. Fallback — локальный JWT
        if payload is None:
            try:
                payload = _verify_local(
                    token,
                    settings.JWT_SECRET,
                    settings.JWT_ALGORITHM,
                )
            except JWTError as e:
                logger.warning(
                    f"[SEC] 401 — невалидный JWT: {e} | "
                    f"{request.method} {path}"
                )
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": error_detail},
                )
            except Exception as e:
                logger.error(f"[SEC] Ошибка проверки JWT: {e}")
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={"detail": "Сервис аутентификации недоступен"},
                )

        # 4. Сохранить payload в request.state
        request.state.user = payload
        roles = _extract_roles(payload)
        request.state.roles = roles

        # 5. Проверка ролей для admin-путей
        if any(path.startswith(p) for p in ADMIN_PREFIXES):
            if not (roles & ADMIN_ROLES):
                logger.warning(
                    f"[SEC] 403 — недостаточно прав: "
                    f"user={_get_username(payload)} "
                    f"roles={roles} "
                    f"path={path}"
                )
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Недостаточно прав"},
                )

        logger.debug(
            f"[SEC] OK: user={_get_username(payload)} → "
            f"{request.method} {path}"
        )
        return await call_next(request)

    @staticmethod
    def _extract_token(request: Request) -> Optional[str]:
        """Извлечь Bearer токен из заголовка Authorization."""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None
