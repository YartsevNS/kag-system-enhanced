"""Выбор провайдера/модели на стороне чата (клик по названию модели).

В привязке функции (админка) остаются основной и резервный провайдеры; пользователь в чате
может выбрать другого провайдера/модель под свой вопрос. Приоритет: запрос → привязка функции.
Недоступный (выключенный/несуществующий) провайдер не ломает запрос — берётся привязка.
"""
import asyncio


def _run(coro):
    return asyncio.run(coro)


def _provider_config(pid, name, models, type_="custom", enabled=True):
    from src.api.services.provider_service import ProviderConfig

    return ProviderConfig(id=pid, name=name, type=type_, url="http://127.0.0.1:9",
                          api_key="k", models=list(models), enabled=enabled)


def _function_map(provider_id, model, fallback_provider_id="", fallback_model=""):
    from src.api.services.provider_service import FunctionMap

    return FunctionMap(function="chat", provider_id=provider_id, model=model, system_prompt="п",
                       parameters={}, fallback_provider_id=fallback_provider_id,
                       fallback_model=fallback_model)


def _service_with_cache(monkeypatch, providers, function_map):
    from src.api.services.provider_service import ProviderService

    service = ProviderService()
    monkeypatch.setattr(service, "_load_cache", lambda: None)  # не ходим в БД
    service._provider_cache = {p.id: p for p in providers}
    service._function_cache = {"chat": function_map}
    service._cache_loaded = True
    return service


def test_chain_uses_binding_by_default(monkeypatch):
    ps = _service_with_cache(
        monkeypatch,
        [_provider_config("p1", "deepseec", ["deepseek-flash"]),
         _provider_config("p2", "polza.ai", ["z-ai/glm-5.3-flash"])],
        _function_map("p1", "deepseek-flash", "p2", "z-ai/glm-5.3-flash"),
    )
    chain = ps.get_function_provider_chain("chat")
    assert [(p.name, m) for p, m in chain] == [("deepseec", "deepseek-flash"),
                                               ("polza.ai", "z-ai/glm-5.3-flash")]


def test_chain_honours_client_choice(monkeypatch):
    ps = _service_with_cache(
        monkeypatch,
        [_provider_config("p1", "deepseec", ["deepseek-flash"]),
         _provider_config("p2", "polza.ai", ["z-ai/glm-5.3-flash"]),
         _provider_config("p3", "giga", ["GigaChat-3-Pro"], type_="gigachat")],
        _function_map("p1", "deepseek-flash", "p2", "z-ai/glm-5.3-flash"),
    )
    chain = ps.get_function_provider_chain("chat", "p3", "GigaChat-3-Pro")
    assert chain[0][0].name == "giga" and chain[0][1] == "GigaChat-3-Pro"
    assert chain[1][0].name == "polza.ai", "резерв из привязки сохраняется"


def test_chain_ignores_unknown_or_disabled_provider(monkeypatch):
    ps = _service_with_cache(
        monkeypatch,
        [_provider_config("p1", "deepseec", ["deepseek-flash"]),
         _provider_config("p2", "off", ["m"], enabled=False)],
        _function_map("p1", "deepseek-flash"),
    )
    for bad in ("нет-такого", "p2"):
        chain = ps.get_function_provider_chain("chat", bad, "что-то")
        assert chain[0][0].name == "deepseec", f"выбор {bad} должен игнорироваться"


def test_chain_model_only_override(monkeypatch):
    ps = _service_with_cache(
        monkeypatch,
        [_provider_config("p1", "deepseec", ["deepseek-flash", "deepseek-v4-pro"])],
        _function_map("p1", "deepseek-flash"),
    )
    chain = ps.get_function_provider_chain("chat", "", "deepseek-v4-pro")
    assert chain == [(ps._provider_cache["p1"], "deepseek-v4-pro")]


def test_chat_uses_the_chosen_provider(monkeypatch):
    from src.api.services import chat_service as cs

    provider = _provider_config("p2", "polza.ai", ["z-ai/glm-5.3-flash"])
    service = cs.ChatService()
    monkeypatch.setattr(service, "_get_chat_provider",
                        lambda: (_provider_config("p1", "deepseec", ["deepseek-flash"]),
                                 _function_map("p1", "deepseek-flash")))
    monkeypatch.setattr(service, "_get_total_docs", lambda: 0)
    monkeypatch.setattr(cs.provider_service, "get_function_provider_chain",
                        lambda _f, pid="", model="": [(provider, model or "z-ai/glm-5.3-flash")])
    captured = {}

    async def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return {"id": "1", "content": "ответ", "model": kwargs["model"], "usage": {}, "elapsed": 0.1}

    monkeypatch.setattr(service, "_call_llm", fake_call_llm)
    res = _run(service.generate_response(user_message="вопрос", use_rag=False, is_admin=True,
                                        provider_id="p2", model="z-ai/glm-5.3-flash"))

    assert captured["provider"].name == "polza.ai"
    assert captured["model"] == "z-ai/glm-5.3-flash"
    assert res["metadata"]["provider"] == "polza.ai"
    assert res["metadata"]["fallback_used"] is False
