"""Служебные вызовы чата (классификатор домена и разбиение вопроса) — резерв и короткий таймаут.

Зачем: эти вызовы на критическом пути ответа пользователю. 19.09.2026 провайдер завис на
классификаторе домена, и ответ пришёл через 122 с, хотя сама генерация заняла 2.3 с. Теперь
оба вызова идут по цепочке «основной → резервная привязка» с таймаутом 30 с (вместо 120).
"""
import asyncio


def _run(coro):
    return asyncio.run(coro)


def _chain_cfg(model, url="http://primary", type_="deepseek"):
    return {"provider": type_, "url": url, "api_key": "k", "model": model,
            "system_prompt": "", "parameters": {"temperature": 0.1, "max_tokens": 150}}


def _service(monkeypatch, chain, answers):
    """answers — что вернуть на каждый вызов LLM по порядку."""
    from src.api.services import chat_service as cs

    service = cs.ChatService()
    monkeypatch.setattr(cs.provider_service, "get_function_llm_chain",
                        lambda _f, pid="", model="": list(chain))
    monkeypatch.setattr(cs.provider_service, "get_function_llm_config",
                        lambda _f: chain[0] if chain else None)
    monkeypatch.setattr(service, "_build_alias_domain_pairs", lambda *a, **kw: "")
    calls = []

    async def fake_call_llm(**kwargs):
        calls.append({"model": kwargs["model"], "timeout": kwargs.get("timeout"),
                      "url": kwargs["provider"].url})
        content = answers[len(calls) - 1] if len(calls) <= len(answers) else ""
        return {"id": str(len(calls)), "content": content, "model": kwargs["model"],
                "usage": {}, "elapsed": 0.1}

    monkeypatch.setattr(service, "_call_llm", fake_call_llm)
    return service, calls


def test_query_analysis_uses_reserve_and_short_timeout(monkeypatch):
    chain = [_chain_cfg("primary-model"), _chain_cfg("z-ai/glm-5.3-flash", "http://reserve", "custom")]
    service, calls = _service(monkeypatch, chain, answers=["", "infosec"])
    result = _run(service._detect_query_analysis("какие требования к защите информации?"))

    assert result and result.get("domain") == "infosec", result
    assert [c["model"] for c in calls] == ["primary-model", "z-ai/glm-5.3-flash"]
    assert all(c["timeout"] == 30.0 for c in calls), calls
    assert calls[1]["url"] == "http://reserve"


def test_query_analysis_no_reserve_keeps_old_behaviour(monkeypatch):
    chain = [_chain_cfg("primary-model")]
    service, calls = _service(monkeypatch, chain, answers=[""])
    result = _run(service._detect_query_analysis("какие требования к защите информации?"))
    assert result is None
    assert len(calls) == 1 and calls[0]["timeout"] == 30.0


def test_decomposition_uses_reserve(monkeypatch):
    chain = [_chain_cfg("primary-model"), _chain_cfg("z-ai/glm-5.3-flash", "http://reserve", "custom")]
    service, calls = _service(monkeypatch, chain,
                              answers=["", '{"subqueries": ["часть один", "часть два"]}'])
    subs = _run(service._decompose_query("Сравни требования ГОСТ 34.10 и ГОСТ 34.11, и перечисли отличия"))
    assert subs == ["часть один", "часть два"], subs
    assert len(calls) == 2, calls


def test_decomposition_short_question_not_called(monkeypatch):
    chain = [_chain_cfg("primary-model")]
    service, calls = _service(monkeypatch, chain, answers=[])
    subs = _run(service._decompose_query("что такое ГОСТ?"))
    assert subs == ["что такое ГОСТ?"]
    assert calls == [], "простые вопросы не декомпозируем — лишних вызовов быть не должно"
