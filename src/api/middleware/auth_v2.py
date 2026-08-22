"""
JWT authentication middleware / dependency for FastAPI.

Provides:
- get_current_user: FastAPI dependency that extracts and validates JWT token
- Extracts token from Authorization: Bearer <token> header
- Decodes JWT, looks up user in DB
- Sets request.state.current_user (for middleware-style usage)
"""

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
import jwt
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from src.config import get_settings
from src.database.session import get_db
from src.database.user_models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


async def _get_token(request: Request) -> Optional[str]:
    """Извлечь JWT: httpOnly cookie kag_token ИЛИ Authorization header.

    Приоритет у COOKIE (а не header): cookie свежий — сервер ставит его при
    каждом логине текущего пользователя. Authorization header может прийти
    из localStorage старой версии страницы (токен ПРОШЛОГО пользователя на
    общем компьютере) — он НЕ должен перебивать свежий cookie.
    Совпадает с порядком в SecurityMiddleware._extract_token.
    """
    token = request.cookies.get("kag_token")
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def _get_settings():
    """Return cached settings."""
    return get_settings()


def _sync_keycloak_user(db: Session, payload: dict) -> User:
    """Синхронизировать keycloak-пользователя в локальную таблицу users.

    Зачем: сессии чата, group_ids и прочие механизмы работают с локальным
    User. При входе через Keycloak (SSO) — создаём/обновляем запись:
    username = preferred_username, is_admin из роли admin в realm.
    """
    username = payload.get("preferred_username") or payload.get("sub") or "user"
    roles = set()
    realm_access = payload.get("realm_access") or {}
    roles.update(realm_access.get("roles", []) or [])
    is_admin = "admin" in roles

    user = db.query(User).filter(User.username == username).first()
    if user is None:
        user = User(
            username=username,
            is_admin=is_admin,
            is_active=True,
        )
        db.add(user)
    else:
        # Обновляем админ-права из keycloak-ролей (синхронизация)
        user.is_admin = is_admin
    db.commit()
    db.refresh(user)
    return user


async def get_current_user(
    token: Optional[str] = Depends(_get_token),
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency: extract and validate JWT, return User from DB.

    Поддерживает:
    - локальные JWT (sub=username, наш JWT_SECRET)
    - Keycloak JWT (SSO): preferred_username + роли admin/user;
      пользователь синхронизируется в users таблицу.

    Raises 401 if token is missing or invalid, or user not found.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    settings = _get_settings()

    # 1. Локальный JWT
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing subject",
            )
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
            )
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Inactive user",
            )
        return user
    except InvalidTokenError:
        # Не локальный — пробуем Keycloak
        pass

    # 2. Keycloak JWT (SSO)
    try:
        from src.api.middleware.security import _verify_keycloak
        payload_kc = _verify_keycloak(token, settings.KEYCLOAK_URL, settings.KEYCLOAK_REALM)
        user_kc = _sync_keycloak_user(db, payload_kc)
        if not user_kc.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Inactive user",
            )
        return user_kc
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        )


async def get_current_user_optional(
    token: Optional[str] = Depends(_get_token),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """
    Like get_current_user, but returns None instead of raising 401.

    Useful for endpoints that work with or without authentication.
    """
    if not token:
        return None
    try:
        return await get_current_user(token=token, db=db)
    except HTTPException:
        return None


async def auth_middleware(request: Request, call_next):
    """
    ASGI middleware: decode JWT and attach user to request.state.current_user.

    Non-blocking: if token is missing or invalid, request.state.current_user
    is set to None and the request continues.
    """
    auth_header = request.headers.get("Authorization")
    token = None
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:]

    if token:
        try:
            settings = _get_settings()
            payload = jwt.decode(
                token,
                settings.JWT_SECRET,
                algorithms=[settings.JWT_ALGORITHM],
            )
            username = payload.get("sub")
            if username:
                # We need a DB session — use a quick inline approach
                from src.database.session import get_db as _get_db
                db_gen = _get_db()
                db = next(db_gen)
                try:
                    user = db.query(User).filter(User.username == username).first()
                    if user and user.is_active:
                        request.state.current_user = user
                finally:
                    db.close()
        except (InvalidTokenError, Exception):
            pass

    if not hasattr(request.state, "current_user"):
        request.state.current_user = None

    return await call_next(request)
