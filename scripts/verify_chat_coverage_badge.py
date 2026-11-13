"""Проверка бейджа покрытия: вызвать настоящий маршрут чата без HTTP-авторизации.

Зачем так: у POST /api/v1/chat/ стоит обязательная аутентификация, а заводить пароль
в команду нельзя. Здесь маршрут вызывается как функция с подставным пользователем —
это тот же код, что отдаёт клиенту metadata (включая answerability), поэтому проверка
честная: если хук сломан, ответа с полем не будет.

Вызов идёт к реальной модели (один запрос), поэтому занимает время.
"""
import asyncio
import json

from src.api.routes.chat import send_message
from src.models import ChatRequest


class FakeUser:
    """Минимум, который читает маршрут: username, группы, admin-флаг."""

    id = "verifier"
    username = "verifier"
    email = "verifier@local"
    is_admin = True
    groups = []


import sys
Q = sys.argv[1] if len(sys.argv) > 1 else "какие меры защиты применяет организация"

req = ChatRequest(
    messages=[{"role": "user", "content": Q}],
    session_id=None,
)

try:
    resp = asyncio.run(send_message(req, current_user=FakeUser()))
    meta = getattr(resp, "metadata", {}) or {}
    ans = meta.get("answerability")
    print(json.dumps({
        "вопрос": Q,
        "ответ_есть": bool(getattr(resp, "response", "")),
        "длина_ответа": len(getattr(resp, "response", "") or ""),
        "answerability": ans,
        "ключи_metadata": sorted(meta.keys())[:12],
    }, ensure_ascii=False, indent=1))
except Exception as exc:  # noqa: BLE001
    print("ОШИБКА маршрута:", type(exc).__name__, str(exc)[:300])
