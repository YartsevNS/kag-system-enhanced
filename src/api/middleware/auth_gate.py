"""
Middleware аутентификации — JWT-based, browser-friendly.

- API запросы: возвращает 401 JSON
- Web страницы: редирект на /login?next=...
- Публичные пути: /login, /api/v1/auth, /setup, /docs, /static
"""

import os
from typing import Optional, Set
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from loguru import logger
import jwt

from src.config import get_settings

AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "false").lower() == "true"

# Пути, доступные без авторизации
PUBLIC_PREFIXES: Set[str] = {
    "/login",
    "/api/v1/auth",
    "/api/v1/health",
    "/api/v1/setup",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/static",
    "/favicon.ico",
}

# Дополнительные публичные пути (точные — для iframe/img)
PUBLIC_EXACT: Set[str] = set()

def _is_public(path: str) -> bool:
    """Проверить, является ли путь публичным."""
    if any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return True
    # Разрешить GET-запросы к preview/thumbnail/details без авторизации
    if path.startswith("/api/v1/upload/"):
        rest = path[len("/api/v1/upload/"):]
        if "/preview" in rest or "/thumbnail" in rest or "/details" in rest:
            return True
    return False

# Пути, которые НЕ редиректятся на /login (API)
API_PREFIXES: Set[str] = {
    "/api/",
}


class AuthGateMiddleware(BaseHTTPMiddleware):
    """JWT auth gate: redirect web to /login, 401 for API."""
    
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        
        # Public paths — always allow
        if _is_public(path):
            return await call_next(request)
        
        # Extract JWT from cookie or Authorization header
        token = self._extract_token(request)
        
        if not token:
            return self._auth_required(request)
        
        # Validate JWT
        try:
            settings = get_settings()
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
            request.state.username = payload.get("sub")
        except jwt.PyJWTError:
            return self._auth_required(request)
        
        return await call_next(request)
    
    def _extract_token(self, request: Request) -> Optional[str]:
        """Extract JWT from cookie first, then Authorization header."""
        # Check cookie
        token = request.cookies.get("kag_token")
        if token:
            return token
        
        # Check Authorization header
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        
        return None
    
    def _auth_required(self, request: Request):
        """Return 401 for API, redirect for web."""
        path = request.url.path
        
        if any(path.startswith(p) for p in API_PREFIXES):
            return JSONResponse(status_code=401, content={"detail": "Authentication required"})
        
        # Web request — redirect to login
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        return RedirectResponse(url=f"/login?next={next_url}", status_code=302)
