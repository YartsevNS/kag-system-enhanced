"""Чат должен соблюдать галочку «Отключить размышления модели» из привязки функции.

Замер на стенде 19.09.2026 (провайдер deepseek, модель deepseek-flash через прокси):
  без параметра                                — content 0, reasoning 954 (ответ пустой)
  thinking: {"type": "disabled"}               — content 731, reasoning 0
  no_think: true                               — content 56, reasoning 795 (не работает)
  reasoning_effort: "none"                     — content 749, reasoning 0
Галочка в админке влияла только на извлечение графа: чат её игнорировал, поэтому при
небольшом max_tokens бюджет уходил в рассуждения и пользователь получал пустой ответ.
"""
import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


def _provider(type_: str = "deepseek"):
    from src.api.services.provider_service import ProviderConfig

    return ProviderConfig(id="p1", name="тестовый", type=type_, url="http://127.0.0.1:9", api_key="k")


def _func_map(no_think):
    from src.api.services.provider_service import FunctionMap

    return FunctionMap(
        function="chat",
        provider_id="p1",
        model="deepseek-flash",
        system_prompt="отвечай кратко",
        parameters={"temperature": 0.7, "max_tokens": 4096, "no_think": no_think},
    )


def _capture(monkeypatch, no_think, provider_type="deepseek"):
    """Прогон generate_response с перехватом аргументов вызова LLM."""
    from src.api.services.chat_service import ChatService

    service = ChatService()
    monkeypatch.setattr(service, "_get_chat_provider", lambda: (_provider(provider_type), _func_map(no_think)))
    monkeypatch.setattr(service, "_get_total_docs", lambda: 0)

    captured = {}

    async def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return {"id": "1", "content": "ответ", "model": "deepseek-flash", "usage": {}, "elapsed": 0.1}

    monkeypatch.setattr(service, "_call_llm", fake_call_llm)
    _run(service.generate_response(user_message="что такое макропруденциальные надбавки?",
                                   use_rag=False, is_admin=True))
    return captured


def test_no_think_enabled_passes_thinking_disabled(monkeypatch):
    captured = _capture(monkeypatch, no_think=True)
    assert captured.get("extra_payload") == {"thinking": {"type": "disabled"}}, captured.get("extra_payload")


def test_no_think_disabled_passes_nothing(monkeypatch):
    captured = _capture(monkeypatch, no_think=False)
    assert captured.get("extra_payload") is None, captured.get("extra_payload")


def test_default_is_no_think_on(monkeypatch):
    """Ключа no_think в параметрах нет — по умолчанию размышления отключаем (как в графе)."""
    from src.api.services.chat_service import ChatService

    service = ChatService()
    fm = _func_map(True)
    fm.parameters = {"temperature": 0.7, "max_tokens": 4096}
    monkeypatch.setattr(service, "_get_chat_provider", lambda: (_provider(), fm))
    monkeypatch.setattr(service, "_get_total_docs", lambda: 0)
    captured = {}

    async def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return {"id": "1", "content": "ответ", "model": "m", "usage": {}, "elapsed": 0.1}

    monkeypatch.setattr(service, "_call_llm", fake_call_llm)
    _run(service.generate_response(user_message="вопрос", use_rag=False, is_admin=True))
    assert captured.get("extra_payload") == {"thinking": {"type": "disabled"}}


def test_ollama_provider_gets_no_extra_param(monkeypatch):
    """У локальных моделей параметр thinking не шлём — там другая ручка (Ollama/llama.cpp)."""
    captured = _capture(monkeypatch, no_think=True, provider_type="ollama")
    assert captured.get("extra_payload") is None, captured.get("extra_payload")
