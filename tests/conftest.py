"""Общие фикстуры тестов KAG.

`auth_headers` — валидный локальный JWT для защищённых роутов. Middleware
(`src/api/middleware/security.py`) проверяет подпись тем же `settings.JWT_SECRET` и берёт
роли из payload, в БД не ходит — поэтому токен выпускается прямо в тесте.

Секрет — заведомо фейковый и РАЗНЫЙ на каждый прогон (uuid4), подменяется через
`monkeypatch`: после теста значение возвращается, в соседний файл не протекает.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def auth_headers(monkeypatch):
    """Заголовок Authorization с валидным локальным JWT админа."""
    import jwt as _jwt

    from src.config import get_settings

    settings = get_settings()
    # Тестовый секрет: уникальный на прогон, чтобы «тест зелёный» не мог объясняться
    # совпадением с реальным ключом. monkeypatch вернёт исходное значение после теста.
    monkeypatch.setattr(
        settings, "JWT_SECRET", f"test-not-for-prod-{uuid.uuid4().hex}"
    )
    payload = {
        "sub": "test-admin",
        "username": "test-admin",
        "roles": ["admin"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = _jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return {"Authorization": f"Bearer {token}"}
