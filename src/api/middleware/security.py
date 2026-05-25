"""
SecurityMiddleware — проверка JWT для всех путей кроме публичных.
При отсутствии токена: веб-страницы → редирект /login, API → 401 JSON.
"""

import time
from typing import Optional, Set

from fastapi import Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from loguru import logger
from jose import jwt, jwk, JWTError

from src.config import get_settings

# ── Публичные пути (без токена) ───────────────────────────────────────

def _is_public(path: str) -> bool:
    """Проверить, является ли путь публичным (без токена)."""
    # Точные совпадения
    if path in ("/", "/login", "/setup", "/favicon.ico"):
        return True
    # Префиксы
    for p in ["/api/v1/auth/", "/api/v1/health", "/api/v1/setup", 
              "/api/docs", "/api/redoc", "/api/openapi.json", "/static/"]:
        if path.startswith(p):
            return True
    return False

# ── Админские пути (требуют роль admin или kag-admin) ────────────────

ADMIN_PREFIXES: Set[str] = {
    "/api/v1/admin",
}

ADMIN_ROLES: Set[str] = {"admin", "kag-admin"}

# ── Кеш JWKS ──────────────────────────────────────────────────────────

_jwks_cache: Optional[dict] = None
_jwks_checked_at: float = 0.0
_JWKS_CACHE_TTL = 300


from functools import lru_cache

import urllib.request, json


@lru_cache(maxsize=1)
def _load_jwks_cached(keycloak_url: str, realm: str) -> str:
    """Загрузить JWKS (с lru_cache). При ошибке возвращает пустой keys list."""
    url = f"{keycloak_url}/realms/{realm}/protocol/openid-connect/certs"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        logger.warning(f"[SEC] JWKS load failed: {e}")
        return '{"keys": []}'


def _load_jwks(keycloak_url: str, realm: str) -> dict:
    """Загрузить JWKS (с TTL-кешем)."""
    global _jwks_cache, _jwks_checked_at
    now = time.monotonic()
    if _jwks_cache is not None and (now - _jwks_checked_at) < _JWKS_CACHE_TTL:
        return _jwks_cache
    raw = _load_jwks_cached(keycloak_url, realm)
    # Инвалидируем lru_cache каждые 5 минут для принудительного обновления
    if _jwks_cache is not None:
        _load_jwks_cached.cache_clear()
    _jwks_cache = json.loads(raw)
    _jwks_checked_at = now
    return _jwks_cache


def _verify_keycloak(token: str, keycloak_url: str, realm: str) -> dict:
    """Проверить токен через JWKS Keycloak с полной валидацией."""
    jwks = _load_jwks(keycloak_url, realm)
    settings = get_settings()
    issuer = f"{keycloak_url}/realms/{realm}"

    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise JWTError("Token header missing 'kid'")
    key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if not key_data:
        raise JWTError(f"Key '{kid}' not found in JWKS")
    public_key = jwk.construct(key_data)

    return jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        audience=settings.KEYCLOAK_CLIENT_ID,
        issuer=issuer,
        options={
            "verify_signature": True,
            "verify_exp": True,
            "verify_nbf": True,
            "verify_iat": True,
            "verify_aud": True,
            "verify_iss": True,
            "require": ["exp", "sub", "iat"],
        },
    )


def _verify_local(token: str, jwt_secret: str, algorithm: str) -> dict:
    return jwt.decode(token, jwt_secret, algorithms=[algorithm],
                      options={"verify_exp": True})


def _extract_roles(payload: dict) -> Set[str]:
    roles = set()
    realm_access = payload.get("realm_access") or {}
    roles.update(realm_access.get("roles", []))
    local_roles = payload.get("roles", [])
    if isinstance(local_roles, list):
        roles.update(local_roles)
    if isinstance(payload.get("role"), str):
        roles.add(payload["role"])
    return roles


def _get_username(payload: dict) -> str:
    return (payload.get("preferred_username")
            or payload.get("sub")
            or payload.get("username")
            or "unknown")


def _is_api_path(path: str) -> bool:
    """API-пути для JSON-ответов, остальное — веб-страницы."""
    return path.startswith("/api/")


# ── Middleware ─────────────────────────────────────────────────────────

class SecurityMiddleware(BaseHTTPMiddleware):
    """Проверяет JWT для всех путей. Нет токена → /login или 401."""

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # 1. Публичные пути — без проверки
        if _is_public(path):
            return await call_next(request)

        # 2. Извлечь токен (cookie → Authorization header)
        token = self._extract_token(request)
        if not token:
            logger.warning(f"[SEC] 401 — нет токена: {request.method} {path}")
            return self._auth_required(path, request)

        # 3. Проверить токен
        settings = get_settings()
        payload = None

        try:
            unverified_header = jwt.get_unverified_header(token)
            if "kid" in unverified_header:
                payload = _verify_keycloak(token, settings.KEYCLOAK_URL, settings.KEYCLOAK_REALM)
        except Exception:
            pass

        if payload is None:
            try:
                payload = _verify_local(token, settings.JWT_SECRET, settings.JWT_ALGORITHM)
            except JWTError as e:
                logger.warning(f"[SEC] 401 — невалидный JWT: {e} | {request.method} {path}")
                return self._auth_required(path, request)
            except Exception as e:
                logger.error(f"[SEC] JWT error: {e}")
                return self._auth_required(path, request)

        # 4. Сохранить
        request.state.user = payload
        roles = _extract_roles(payload)
        request.state.roles = roles

        # 5. Admin-проверка
        if any(path.startswith(p) for p in ADMIN_PREFIXES):
            if not (roles & ADMIN_ROLES):
                logger.warning(f"[SEC] 403: user={_get_username(payload)} roles={roles} path={path}")
                return JSONResponse(
                    status_code=403,
                    content={
                        "error_code": "ACCESS_DENIED",
                        "detail": "Недостаточно прав",
                    },
                )

        logger.debug(f"[SEC] OK: user={_get_username(payload)} → {request.method} {path}")
        return await call_next(request)

    def _auth_required(self, path: str, request: Request) -> Response:
        """Возвращает редирект /login для веб-страниц или 401 JSON для API."""
        if _is_api_path(path):
            return JSONResponse(
                status_code=401,
                content={
                    "error_code": "AUTH_REQUIRED",
                    "detail": "Требуется аутентификация",
                },
            )
        # Веб-страница — редирект на /login с сохранением целевого URL
        from urllib.parse import quote
        next_url = quote(path, safe="")
        login_url = f"/login?next={next_url}"
        return RedirectResponse(url=login_url, status_code=302)

    @staticmethod
    def _extract_token(request: Request) -> Optional[str]:
        """Извлечь токен: сначала cookie kag_token, потом Authorization header."""
        # 1. Cookie
        token = request.cookies.get("kag_token")
        if token:
            return token
        # 2. Authorization header
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None
