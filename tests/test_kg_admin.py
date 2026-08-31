"""
Тесты защиты админ-эндпоинтов /api/v1/kg.

Проверяем, что управляющие операции графа (rebuild, post-process,
stop-rebuild, domain-schema POST, watchdog) доступны только админу,
а read-эндпоинты (stats, entities/search) остаются открытыми.

Использует in-memory SQLite, внешние сервисы (Neo4j/Qdrant) не нужны —
эндпоинты ловят ошибки подключения и возвращают 200 с {"status": "error"}.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base
from src.database.user_models import User  # noqa: F401
from src.database.session import get_db


@pytest.fixture(autouse=True)
def _override_settings(monkeypatch):
    """Use an in-memory SQLite DB and a fixed JWT secret for tests."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    from src.config import get_settings
    get_settings.cache_clear()
    # Rate limiter живёт в памяти — сбрасываем между тестами, иначе 429
    from src.api.routes.auth import _rate_store
    _rate_store.clear()


@pytest.fixture
def db_session():
    """Create a fresh in-memory DB session for each test."""
    import src.database.session as session_mod
    session_mod._engine = None
    session_mod._SessionLocal = None

    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    return override_get_db


@pytest.fixture
def client(db_session):
    """FastAPI TestClient with overridden DB dependency."""
    from src.api.main import app
    # Обходим SetupCheckMiddleware (в тесте система "настроена")
    from src.api.middleware.setup_checker import SetupCheckMiddleware
    SetupCheckMiddleware._is_configured = lambda self: True
    app.dependency_overrides[get_db] = db_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _make_admin(db_session, username="root", password="admin123"):
    """Создать админа прямо в БД (register теперь admin-only)."""
    from pwdlib import PasswordHash
    from pwdlib.hashers.argon2 import Argon2Hasher
    from pwdlib.hashers.bcrypt import BcryptHasher
    ph = PasswordHash([Argon2Hasher(), BcryptHasher()])
    gen = db_session()
    db = next(gen)
    existing = db.query(User).filter(User.username == username).first()
    if existing is None:
        admin = User(
            username=username,
            email=f"{username}@example.com",
            is_admin=True,
            hashed_password=ph.hash(password),
        )
        db.add(admin)
        db.commit()
    db.close()


def _register(client, db_session, username, password="secret123", is_admin=False):
    # Регистрация доступна только админу — создаём админа и шлём с его токеном
    _make_admin(db_session)
    admin_login = client.post("/api/v1/auth/login", json={
        "username": "root",
        "password": "admin123",
    })
    assert admin_login.status_code == 200, admin_login.text
    admin_token = admin_login.json()["access_token"]
    resp = client.post("/api/v1/auth/register",
                       headers={"Authorization": f"Bearer {admin_token}"},
                       json={
        "username": username,
        "password": password,
        "is_admin": is_admin,
    })
    assert resp.status_code == 201, resp.text


def _login(client, username, password="secret123"):
    resp = client.post("/api/v1/auth/login", json={
        "username": username,
        "password": password,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


ADMIN_OPS = [
    ("post", "/api/v1/kg/rebuild-graph"),
    ("post", "/api/v1/kg/post-process"),
    ("post", "/api/v1/kg/stop-rebuild"),
    ("post", "/api/v1/kg/domain-schema"),
    ("post", "/api/v1/kg/watchdog/start"),
    ("post", "/api/v1/kg/watchdog/stop"),
    ("post", "/api/v1/kg/cypher"),
]


class TestKgAdminGuard:
    """Защита админ-операций графа знаний."""

    def test_open_read_endpoints_require_auth(self, client, db_session):
        """Read-эндпоинты /kg доступны любому авторизованному, но не анониму."""
        # Без токена — 401 (SecurityMiddleware требует вход на /api/v1/kg)
        for path in ["/api/v1/kg/stats",
                     "/api/v1/kg/entities/search?q=тест",
                     "/api/v1/kg/domain-schema"]:
            resp = client.get(path)
            assert resp.status_code == 401, f"{path}: {resp.status_code}"

        # Обычный пользователь — доступ открыт
        _register(client, db_session, "reader")
        token = _login(client, "reader")
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.get("/api/v1/kg/stats", headers=headers)
        assert resp.status_code == 200

        resp = client.get("/api/v1/kg/entities/search", params={"q": "тест"}, headers=headers)
        assert resp.status_code == 200

        resp = client.get("/api/v1/kg/domain-schema", headers=headers)
        assert resp.status_code == 200

    def test_admin_ops_require_auth(self, client):
        """Без токена админ-операции возвращают 401."""
        for _, path in ADMIN_OPS:
            resp = client.post(path)
            assert resp.status_code == 401, f"{path}: {resp.status_code}"

    def test_admin_ops_forbidden_for_regular_user(self, client, db_session):
        """Обычный пользователь получает 403."""
        _register(client, db_session, "regular")
        token = _login(client, "regular")
        headers = {"Authorization": f"Bearer {token}"}

        for _, path in ADMIN_OPS:
            resp = client.post(path, headers=headers)
            assert resp.status_code == 403, f"{path}: {resp.status_code} {resp.text}"

    def test_admin_ops_allowed_for_admin(self, client, db_session):
        """Админ проходит проверку (дальше может быть ошибка сервиса)."""
        _register(client, db_session, "boss", is_admin=True)
        token = _login(client, "boss")
        headers = {"Authorization": f"Bearer {token}"}

        for _, path in ADMIN_OPS:
            resp = client.post(path, headers=headers)
            # Стража пропустила — дальше 200 (тело может быть {"status":"error"}
            # из-за отсутствия Neo4j в тестовом окружении) либо 4xx/5xx сервиса,
            # но НЕ 401/403.
            assert resp.status_code != 401, f"{path}: 401"
            assert resp.status_code != 403, f"{path}: 403"

    def test_regular_user_can_read_stats(self, client, db_session):
        """Обычный пользователь видит статистику графа."""
        _register(client, db_session, "viewer")
        token = _login(client, "viewer")
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.get("/api/v1/kg/stats", headers=headers)
        assert resp.status_code == 200
