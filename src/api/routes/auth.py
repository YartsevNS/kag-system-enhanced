"""
Auth endpoints: register, login, refresh, me.

POST /auth/register - Create new user
POST /auth/login     - Get JWT access + refresh tokens
POST /auth/refresh   - Rotate refresh token → new access + refresh
GET  /auth/me        - Get current user info (requires JWT)
"""

from datetime import datetime, timedelta, timezone
import time
from collections import defaultdict

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

password_hash = PasswordHash([Argon2Hasher(), BcryptHasher()])

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session
import jwt

from src.config import get_settings
from src.database.session import get_db
from src.database.user_models import User
from src.api.middleware.auth_v2 import get_current_user

router = APIRouter()


# ── Pydantic schemas ──────────────────────────────────────────────────────


class UserRegister(BaseModel):
    """Request body for user registration."""
    username: str
    password: str
    email: str | None = None
    is_admin: bool = False

    @field_validator("username")
    @classmethod
    def username_min_length(cls, v: str) -> str:
        if len(v.strip()) < 3:
            raise ValueError("username must be at least 3 characters")
        return v.strip()

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("password must be at least 6 characters")
        return v


class UserLogin(BaseModel):
    """Request body for login."""
    username: str
    password: str


class RefreshRequest(BaseModel):
    """Request body for token refresh."""
    refresh_token: str


class TokenResponse(BaseModel):
    """JWT token response with access + refresh tokens."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # секунд до истечения access_token


class UserResponse(BaseModel):
    """Public user info (no password)."""
    id: str
    username: str
    email: str | None
    is_admin: bool
    is_active: bool
    created_at: datetime | None

    class Config:
        from_attributes = True


# ── Rate Limiter ──────────────────────────────────────────────────────────

# In-memory: {ip: [(timestamp, ...)]}
_rate_store: dict = defaultdict(list)
_RATE_LIMIT = 5       # попыток
_RATE_WINDOW = 60     # секунд


def _check_rate_limit(ip: str) -> None:
    """Проверить лимит попыток. Райзит 429 при превышении."""
    now = time.time()
    window_start = now - _RATE_WINDOW
    # Очистить старые записи
    _rate_store[ip] = [t for t in _rate_store[ip] if t > window_start]
    if len(_rate_store[ip]) >= _RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много попыток. Попробуйте через минуту.",
        )
    _rate_store[ip].append(now)


# ── Helpers ───────────────────────────────────────────────────────────────


def _create_access_token(username: str, roles: list | None = None) -> str:
    """Создать Access Token (15 мин)."""
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": username,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access",
    }
    if roles:
        payload["roles"] = roles
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _create_refresh_token(username: str) -> str:
    """Создать Refresh Token (7 дней)."""
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(days=7)
    payload = {
        "sub": username,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "refresh",
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _get_token_expiry() -> int:
    """Сколько секунд живёт access_token."""
    settings = get_settings()
    return settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60


def _user_to_response(user: User) -> dict:
    """Convert a User ORM object to a safe dict (no password)."""
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "created_at": user.created_at,
    }


def _user_roles(user: User) -> list:
    """Получить список ролей пользователя."""
    roles = []
    if user.is_admin:
        roles.append("admin")
    return roles


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(body: UserRegister, db: Session = Depends(get_db)):
    """
    Register a new user.

    - **username**: unique, min 3 characters
    - **password**: min 6 characters
    - **email**: optional

    Returns user info without password. 409 if username already exists.
    """
    existing = db.query(User).filter(User.username == body.username).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken",
        )

    user = User(
        username=body.username,
        email=body.email,
        is_admin=body.is_admin,
        hashed_password=password_hash.hash(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_to_response(user)


@router.post("/login", response_model=TokenResponse)
def login(body: UserLogin, request: Request, db: Session = Depends(get_db)):
    """
    Login and get JWT access + refresh tokens.

    Returns access_token (15 min) + refresh_token (7 days).
    401 if credentials are invalid.
    """
    _check_rate_limit(request.client.host if request.client else "unknown")
    user = db.query(User).filter(User.username == body.username).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    valid, new_hash = password_hash.verify_and_update(body.password, user.hashed_password)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if new_hash:
        user.hashed_password = new_hash
        db.add(user)
        db.commit()
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is deactivated",
        )

    roles = _user_roles(user)
    access_token = _create_access_token(user.username, roles)
    refresh_token = _create_refresh_token(user.username)

    response = JSONResponse(content={
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": _get_token_expiry(),
    })
    # httpOnly cookie — не доступен JavaScript
    response.set_cookie(
        key="kag_token",
        value=access_token,
        httponly=True,
        secure=False,  # True на проде с HTTPS
        samesite="lax",
        path="/",
        max_age=_get_token_expiry(),
    )
    return response


@router.post("/refresh", response_model=TokenResponse)
def refresh(body: RefreshRequest, db: Session = Depends(get_db)):
    """
    Обновить токены по refresh_token (ротация).

    Старый refresh_token валидируется, выпускается НОВАЯ пара токенов.
    401 если refresh_token недействителен или пользователь деактивирован.
    """
    settings = get_settings()

    # Валидация refresh_token
    try:
        payload = jwt.decode(
            body.refresh_token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": True},
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is not a refresh token",
        )

    username = payload.get("sub")
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    # Проверка пользователя
    user = db.query(User).filter(User.username == username).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or deactivated",
        )

    roles = _user_roles(user)
    access_token = _create_access_token(user.username, roles)
    refresh_token = _create_refresh_token(user.username)

    response = JSONResponse(content={
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": _get_token_expiry(),
    })
    response.set_cookie(
        key="kag_token",
        value=access_token,
        httponly=True,
        secure=False,
        samesite="lax",
        path="/",
        max_age=_get_token_expiry(),
    )
    return response


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    """
    Get current authenticated user info.

    Requires a valid JWT token in the Authorization header.
    """
    return _user_to_response(current_user)

@router.post("/logout", summary="Выход из системы")
def logout(request: Request, response: Response):
    """
    Удаляет токен из httpOnly cookie и возвращает успех.
    Клиент также должен очистить localStorage.
    """
    resp = JSONResponse({"status": "ok", "message": "Вы вышли из системы"})
    resp.delete_cookie(
        key="kag_token",
        path="/",
        secure=False,  # True для HTTPS
        httponly=True,
        samesite="lax"
    )
    return resp


class ChangePasswordRequest(BaseModel):
    """Запрос на смену пароля."""
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def new_password_min_length(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("new password must be at least 6 characters")
        return v


@router.post("/change-password", summary="Смена пароля")
def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Сменить пароль текущего пользователя.
    Требуется старый пароль для подтверждения.
    """
    valid, new_hash = password_hash.verify_and_update(body.current_password, current_user.hashed_password)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid current password",
        )

    current_user.hashed_password = password_hash.hash(body.new_password)
    db.add(current_user)
    db.commit()
    return {"status": "ok", "message": "Password changed"}
