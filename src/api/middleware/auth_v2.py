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
from sqlalchemy.orm import Session

from src.config import get_settings
from src.database.session import get_db
from src.database.user_models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def _get_settings():
    """Return cached settings."""
    return get_settings()


async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency: extract and validate JWT, return User from DB.

    Raises 401 if token is missing or invalid, or user not found.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    settings = _get_settings()
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
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
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


async def get_current_user_optional(
    token: Optional[str] = Depends(oauth2_scheme),
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
        except (JWTError, Exception):
            pass

    if not hasattr(request.state, "current_user"):
        request.state.current_user = None

    return await call_next(request)
