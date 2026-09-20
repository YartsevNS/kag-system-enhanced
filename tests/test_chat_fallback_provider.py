"""Резервный провайдер для чата: цепочка «основной → резервный».

Зачем: у одного провайдера могут быть одновременно чат, обработка документов и тесты на один
ключ — при лимитах или сбое на его стороне вызов висит до таймаута (120 с), и пользователь
получает ошибку. Резерв задаётся в привязке функции (Админка → Модели LLM): если основной
не ответил (пустой content, ошибка, таймаут), запрос уходит на резерв, а в ответе появляется
флаг fallback_used.
"""
import asyncio


def _run(coro):
    return asyncio.run(coro)


def _provider(pid, name, type_="deepseek"):
    from src.api.services.provider_service import ProviderConfig

    return ProviderConfig(id=pid, name=name, type=type_, url="http://127.0.0.1:9", api_key="k")


def _func_map(parameters=None):
    from src.api.services.provider_service import FunctionMap

    return FunctionMap(function="chat", provider_id="p1", model="primary-model",
                       system_prompt="п", parameters=parameters or {})


def _service(monkeypatch, chain, behaviours):
    """chain — список (провайдер, модель); behaviours — что вернуть на каждый вызов."""
    from src.api.services import chat_service as cs

    service = cs.ChatService()
    monkeypatch.setattr(service, "_get_chat_provider", lambda: (chain[0][0], _func_map()))
    monkeypatch.setattr(service, "_get_total_docs", lambda: 0)
    monkeypatch.setattr(cs.provider_service, "get_function_provider_chain",
                        lambda _f, pid="", model="": list(chain))

    calls = []

    async def fake_call_llm(**kwargs):
        calls.append({"provider": kwargs["provider"].name, "model": kwargs["model"],
                      "extra": kwargs.get("extra_payload")})
        content, error = behaviours[len(calls) - 1]
        return {"id": str(len(calls)), "content": content, "model": kwargs["model"],
                "usage": {}, "elapsed": 0.1, "error": error}

    monkeypatch.setattr(service, "_call_llm", fake_call_llm)
    return service, calls


def test_fallback_used_when_primary_is_silent(monkeypatch):
    chain = [(_provider("p1", "deepseec"), "deepseek-flash"),
             (_provider("p2", "polza.ai", "custom"), "z-ai/glm-5.3-flash")]
    service, calls = _service(monkeypatch, chain, behaviours=[("", "timed out"), ("ответ резерва", None)])
    res = _run(service.generate_response(user_message="вопрос", use_rag=False, is_admin=True))

    assert res["response"] == "ответ резерва"
    assert res["metadata"]["fallback_used"] is True
    assert res["metadata"]["provider"] == "polza.ai"
    assert [c["provider"] for c in calls] == ["deepseec", "polza.ai"], calls
    assert calls[1]["model"] == "z-ai/glm-5.3-flash"


def test_fallback_not_used_when_primary_answers(monkeypatch):
    chain = [(_provider("p1", "deepseec"), "deepseek-flash"),
             (_provider("p2", "polza.ai", "custom"), "z-ai/glm-5.3-flash")]
    service, calls = _service(monkeypatch, chain, behaviours=[("ответ основного", None)])
    res = _run(service.generate_response(user_message="вопрос", use_rag=False, is_admin=True))

    assert res["response"] == "ответ основного"
    assert res["metadata"]["fallback_used"] is False
    assert len(calls) == 1, "резерв не должен вызываться, если основной ответил"


def test_error_marker_counts_as_failure(monkeypatch):
    """Ответ вида «❌ …» (ошибка на стороне приложения) тоже повод уйти на резерв."""
    chain = [(_provider("p1", "deepseec"), "deepseek-flash"),
             (_provider("p2", "giga", "gigachat"), "GigaChat-3-Pro")]
    service, calls = _service(monkeypatch, chain,
                              behaviours=[("❌ Ошибка подключения к LLM: ", None), ("ответ гигачата", None)])
    res = _run(service.generate_response(user_message="вопрос", use_rag=False, is_admin=True))

    assert res["response"] == "ответ гигачата"
    assert res["metadata"]["fallback_used"] is True


def test_without_fallback_error_is_returned(monkeypatch):
    """Резерва нет — пользователь получает понятное сообщение с технической причиной, не пустоту."""
    chain = [(_provider("p1", "deepseec"), "deepseek-flash")]
    service, calls = _service(monkeypatch, chain,
                              behaviours=[("❌ Ошибка подключения к LLM: timed out", "timed out")])
    res = _run(service.generate_response(user_message="вопрос", use_rag=False, is_admin=True))

    assert res["metadata"]["fallback_used"] is False
    assert "Не удалось получить ответ от модели" in res["response"]
    assert "timed out" in res["response"], "техническая причина должна остаться в тексте"


def test_extra_payload_computed_per_provider(monkeypatch):
    """no_think шлём deepseek/совместимым, локальным — нет: типы в цепочке могут различаться."""
    chain = [(_provider("p1", "deepseec", "deepseek"), "deepseek-flash"),
             (_provider("p2", "ollama-local", "ollama"), "mistral:7b")]
    service, calls = _service(monkeypatch, chain, behaviours=[("", "timeout"), ("ответ олламы", None)])
    _run(service.generate_response(user_message="вопрос", use_rag=False, is_admin=True))

    assert calls[0]["extra"] == {"thinking": {"type": "disabled"}}
    assert calls[1]["extra"] is None, "локальной модели параметр thinking не отправляем"
