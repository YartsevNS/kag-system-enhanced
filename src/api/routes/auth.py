"""
Auth endpoints: register, login, me.

POST /auth/register - Create new user
POST /auth/login    - Get JWT access token
GET  /auth/me       - Get current user info (requires JWT)
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from jose import jwt

from src.config import get_settings
from src.database.session import get_db
from src.database.user_models import User
from src.api.middleware.auth_v2 import get_current_user

router = APIRouter()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__ident="2b")


# ── Pydantic schemas ──────────────────────────────────────────────────────


class UserRegister(BaseModel):
    """Request body for user registration."""
    username: str
    password: str
    email: str | None = None

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


class TokenResponse(BaseModel):
    """JWT token response."""
    access_token: str
    token_type: str = "bearer"


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


# ── Helpers ───────────────────────────────────────────────────────────────


def _create_token(username: str) -> str:
    """Create a JWT access token for the given username."""
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": username,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


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
    # Check duplicate
    existing = db.query(User).filter(User.username == body.username).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken",
        )

    user = User(
        username=body.username,
        email=body.email,
        hashed_password=pwd_context.hash(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_to_response(user)


@router.post("/login", response_model=TokenResponse)
def login(body: UserLogin, db: Session = Depends(get_db)):
    """
    Login and get a JWT access token.

    Returns {"access_token": "...", "token_type": "bearer"}.
    401 if credentials are invalid.
    """
    user = db.query(User).filter(User.username == body.username).first()
    if not user or not pwd_context.verify(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is deactivated",
        )

    return {
        "access_token": _create_token(user.username),
        "token_type": "bearer",
    }


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    """
    Get current authenticated user info.

    Requires a valid JWT token in the Authorization header.
    """
    return _user_to_response(current_user)
