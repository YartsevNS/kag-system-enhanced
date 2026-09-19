"""Приоритет настроек генерации чата: запрос → привязка функции → встроенный дефолт.

Зачем: до 19.09.2026 temperature/max_tokens из привязки функции (Админка → Модели LLM) на чат
не влияли — chat_service брал их из запроса, а UI чата присылал свои 0.7/2048, и поля в админке
вводили в заблуждение. Теперь админка задаёт значения по умолчанию, явные значения в запросе
(скрипты, внешние клиенты) выигрывают.
"""
import asyncio


def _run(coro):
    return asyncio.run(coro)


def _provider():
    from src.api.services.provider_service import ProviderConfig

    return ProviderConfig(id="p1", name="тестовый", type="deepseek", url="http://127.0.0.1:9", api_key="k")


def _func_map(parameters):
    from src.api.services.provider_service import FunctionMap

    return FunctionMap(function="chat", provider_id="p1", model="deepseek-flash",
                       system_prompt="отвечай кратко", parameters=parameters)


def _capture(monkeypatch, parameters, **request_kwargs):
    from src.api.services.chat_service import ChatService

    service = ChatService()
    monkeypatch.setattr(service, "_get_chat_provider", lambda: (_provider(), _func_map(parameters)))
    monkeypatch.setattr(service, "_get_total_docs", lambda: 0)
    captured = {}

    async def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return {"id": "1", "content": "ответ", "model": "m", "usage": {}, "elapsed": 0.1}

    monkeypatch.setattr(service, "_call_llm", fake_call_llm)
    _run(service.generate_response(user_message="вопрос", use_rag=False, is_admin=True, **request_kwargs))
    return captured


def test_request_wins_over_binding(monkeypatch):
    captured = _capture(monkeypatch, {"temperature": 0.1, "max_tokens": 1234},
                        temperature=0.9, max_tokens=777)
    assert captured["temperature"] == 0.9
    assert captured["max_tokens"] == 777


def test_binding_used_when_request_silent(monkeypatch):
    captured = _capture(monkeypatch, {"temperature": 0.2, "max_tokens": 2048})
    assert captured["temperature"] == 0.2
    assert captured["max_tokens"] == 2048


def test_builtin_defaults_when_nothing_set(monkeypatch):
    captured = _capture(monkeypatch, {})
    assert captured["temperature"] == 0.7
    assert captured["max_tokens"] == 4096


def test_broken_binding_values_fall_back(monkeypatch):
    captured = _capture(monkeypatch, {"temperature": "abc", "max_tokens": None})
    assert captured["temperature"] == 0.7
    assert captured["max_tokens"] == 4096


def test_ui_no_longer_sends_hardcoded_limits():
    """Страница чата не должна перебивать настройки админки хардкодом."""
    from pathlib import Path

    html = Path("src/api/static/chat.html").read_text(encoding="utf-8")
    call = html.split("${API_URL}/chat/", 1)[1].split("})", 1)[0]
    assert "max_tokens" not in call, "в запросе чата снова появился хардкод max_tokens"
    assert "temperature" not in call, "в запросе чата снова появился хардкод temperature"
