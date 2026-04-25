"""
Middleware аутентификации через Keycloak
"""

from typing import Optional
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from loguru import logger

from src.auth.keycloak import verify_token
from src.auth.casbin import check_permission


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware для проверки аутентификации и авторизации.
    
    Интеграция с Keycloak для проверки JWT-токенов
    и Casbin для проверки RBAC-политик.
    """
    
    async def dispatch(self, request: Request, call_next) -> Response:
        """Обработка запроса"""
        
        # Пропускаем публичные маршруты
        public_paths = [
            "/",
            "/api/v1/health",
            "/docs",
            "/openapi.json",
            "/redoc",
            "/api/v1/admin/models/admin",
            "/static/index.html",
            "/docker",
            "/documents",
            "/setup",
            "/chunks",
            "/qdrant",
            "/api/v1/setup/check",
        ]

        public_prefixes = [
            "/api/v1/admin/models/",  # Все endpoints управления моделями (без auth)
            "/api/v1/chat/",  # Чат (для веб-интерфейса)
            "/api/v1/upload/",  # Загрузка документов
            "/api/v1/setup/",  # Setup Wizard API
        ]

        if request.url.path in public_paths:
            return await call_next(request)

        if request.url.path == "/admin":
            return await call_next(request)

        if any(request.url.path.startswith(prefix) for prefix in public_prefixes):
            # Логируем для отладки
            logger.debug(f"Auth middleware пропускает запрос: {request.url.path}")
            return await call_next(request)
        
        # Логируем заблокированные запросы
        logger.warning(f"Auth middleware блокирует запрос: {request.url.path}")
        
        # Получаем токен из заголовка
        token = self._extract_token(request)
        
        if not token:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Отсутствует токен аутентификации"}
            )
        
        # Проверяем токен через Keycloak
        try:
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
