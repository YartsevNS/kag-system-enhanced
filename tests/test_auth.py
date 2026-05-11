"""
Tests for auth endpoints (register, login, me).

Uses an in-memory SQLite database so no external DB is needed.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base
from src.database.user_models import User  # noqa: F401
from src.database.session import get_db, _get_engine


# ── Override settings for tests ──────────────────────────────────────────

@pytest.fixture(autouse=True)
def _override_settings(monkeypatch):
    """Use an in-memory SQLite DB and a fixed JWT secret for tests."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    # Reset cached settings
    from src.config import get_settings
    get_settings.cache_clear()


# ── DB session fixture ────────────────────────────────────────────────────

@pytest.fixture
def db_session():
    """Create a fresh in-memory DB session for each test."""
    from src.database.session import _engine, _SessionLocal

    # Force reinitialisation with the overridden DATABASE_URL
    import src.database.session as session_mod
    session_mod._engine = None
    session_mod._SessionLocal = None

    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Patch get_db to use our test session
    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    return override_get_db


# ── TestClient fixture ────────────────────────────────────────────────────

@pytest.fixture
def client(db_session):
    """FastAPI TestClient with overridden DB dependency."""
    from src.api.main import app
    app.dependency_overrides[get_db] = db_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Tests ─────────────────────────────────────────────────────────────────


class TestRegister:
    """Tests for POST /api/v1/auth/register"""

    def test_register_success(self, client):
        """Register a new user returns 201 and user info."""
        resp = client.post("/api/v1/auth/register", json={
            "username": "alice",
            "password": "secret123",
            "email": "alice@example.com",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["username"] == "alice"
        assert data["email"] == "alice@example.com"
        assert "hashed_password" not in data
        assert data["is_admin"] is False
        assert data["is_active"] is True
        assert "id" in data

    def test_register_duplicate_username(self, client):
        """Registering the same username twice returns 409."""
        client.post("/api/v1/auth/register", json={
            "username": "bob",
            "password": "secret123",
        })
        resp = client.post("/api/v1/auth/register", json={
            "username": "bob",
            "password": "other456",
        })
        assert resp.status_code == 409
        assert "already taken" in resp.json()["detail"].lower()

    def test_register_short_username(self, client):
        """Username shorter than 3 chars returns 422."""
        resp = client.post("/api/v1/auth/register", json={
            "username": "ab",
            "password": "secret123",
        })
        assert resp.status_code == 422

    def test_register_short_password(self, client):
        """Password shorter than 6 chars returns 422."""
        resp = client.post("/api/v1/auth/register", json={
            "username": "charlie",
            "password": "12345",
        })
        assert resp.status_code == 422


class TestLogin:
    """Tests for POST /api/v1/auth/login"""

    def _register_user(self, client, username="dave", password="secret123"):
        client.post("/api/v1/auth/register", json={
            "username": username,
            "password": password,
        })

    def test_login_success(self, client):
        """Valid credentials return JWT token."""
        self._register_user(client)
        resp = client.post("/api/v1/auth/login", json={
            "username": "dave",
            "password": "secret123",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_wrong_password(self, client):
        """Wrong password returns 401."""
        self._register_user(client)
        resp = client.post("/api/v1/auth/login", json={
            "username": "dave",
            "password": "wrong",
        })
        assert resp.status_code == 401

    def test_login_nonexistent_user(self, client):
        """Login with unknown username returns 401."""
        resp = client.post("/api/v1/auth/login", json={
            "username": "ghost",
            "password": "whatever",
        })
        assert resp.status_code == 401

    def test_login_inactive_user(self, client, db_session):
        """Inactive user cannot log in."""
        # Register and then deactivate
        self._register_user(client, username="eve", password="secret123")
        # Use the raw DB to deactivate
        from src.api.main import app
        # register first, then deactivate via override
        gen = db_session()
        db = next(gen)
        user = db.query(User).filter(User.username == "eve").first()
        user.is_active = False
        db.commit()
        db.close()

        resp = client.post("/api/v1/auth/login", json={
            "username": "eve",
            "password": "secret123",
        })
        assert resp.status_code == 401


class TestMe:
    """Tests for GET /api/v1/auth/me"""

    def _register_and_login(self, client, username="frank", password="secret123") -> str:
        client.post("/api/v1/auth/register", json={
            "username": username,
            "password": password,
        })
        resp = client.post("/api/v1/auth/login", json={
            "username": username,
            "password": password,
        })
        return resp.json()["access_token"]

    def test_me_authenticated(self, client):
        """Authenticated user gets their info."""
        token = self._register_and_login(client)
        resp = client.get("/api/v1/auth/me", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "frank"
        assert "hashed_password" not in data

    def test_me_no_token(self, client):
        """No token returns 401."""
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    def test_me_invalid_token(self, client):
        """Invalid JWT returns 401."""
        resp = client.get("/api/v1/auth/me", headers={
            "Authorization": "Bearer not.a.valid.token",
        })
        assert resp.status_code == 401

    def test_me_wrong_secret(self, client):
        """Token signed with wrong secret returns 401."""
        from jose import jwt
        from datetime import datetime, timedelta, timezone
        bad_token = jwt.encode(
            {"sub": "frank", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            "wrong-secret",
            algorithm="HS256",
        )
        resp = client.get("/api/v1/auth/me", headers={
            "Authorization": f"Bearer {bad_token}",
        })
        assert resp.status_code == 401
