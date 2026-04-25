"""
Middleware аутентификации через Keycloak или статический токен
"""

import os
from typing import Optional
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from loguru import logger

from src.auth.keycloak import verify_token
from src.auth.casbin import check_permission

# Hardcoded API token for development (also check env)
_STATIC_TOKEN = os.environ.get("KAG_API_TOKEN", "Xy2-l25TbClBjUImTLqc7kU93Qt9muXvj-YHikEmMkU")


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Simple auth - allow all requests for development.
    """
    
    async def dispatch(self, request: Request, call_next) -> Response:
        return await call_next(request)
        
        # Получаем токен из заголовка
        token = self._extract_token(request)
        
        if not token:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Отсутствует токен аутентификации"}
            )
        
        # Проверяем токен через Keycloak или статический токен
        try:
            # Проверяем статический токен
            if _STATIC_TOKEN and token == _STATIC_TOKEN:
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
        
        # Проверяем права доступа через Casbin (для защищённых маршрутов)
        protected_paths = ["/api/v1/admin"]
        if any(request.url.path.startswith(path) for path in protected_paths):
            user_roles = request.state.user.get("roles", [])
            resource = request.url.path
            action = request.method
            
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
    
    def _check_admin_access(self, user_roles: list) -> bool:
        """Проверить доступ к административным маршрутам"""
        return "admin" in user_roles
