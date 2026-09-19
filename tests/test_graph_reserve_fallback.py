"""Резерв при извлечении графа: пустой ответ основного → повтор на резервном провайдере."""
import asyncio


def _run(coro):
    return asyncio.run(coro)


def _cfg(model, provider="deepseek", url="http://reserve"):
    return {"provider": provider, "url": url, "api_key": "k", "model": model,
            "system_prompt": "", "parameters": {}}


def test_graph_uses_reserve_when_primary_returns_nothing(monkeypatch):
    from src.api.services.provider_service import provider_service as ps
    from src.indexing import entity_extractor as ee

    extractor = ee.EntityExtractor()
    # Патчим метод у самого синглтона: extractor импортирует его внутри функции,
    # поэтому подмена модульной ссылки не сработала бы.
    monkeypatch.setattr(ps, "get_function_llm_chain",
                        lambda _f: [_cfg("deepseek-v4-flash"), _cfg("z-ai/glm-5.3-flash", "custom")])

    # Проверяем именно логику повтора: первый вызов — пустой ответ, второй — сущности
    calls = []

    async def fake_once(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            return {"entities": [], "relations": [], "facts": [], "warnings": ["LLM 429"]}
        return {"entities": [{"name": "ГОСТ"}], "relations": [], "facts": [], "warnings": []}

    monkeypatch.setattr(extractor, "_call_llm_once", fake_once)
    result = _run(extractor._call_llm(prompt="текст", model="deepseek-v4-flash",
                                      llm_url="http://primary", chunk_id="c1", pass_name="entities"))

    assert result.get("entities"), result
    assert calls == ["deepseek-v4-flash", "z-ai/glm-5.3-flash"], calls


def test_graph_without_reserve_returns_original(monkeypatch):
    from src.api.services.provider_service import provider_service as ps
    from src.indexing import entity_extractor as ee

    extractor = ee.EntityExtractor()
    monkeypatch.setattr(ps, "get_function_llm_chain", lambda _f: [_cfg("deepseek-v4-flash")])

    calls = []

    async def fake_once(**kwargs):
        calls.append(kwargs["model"])
        return {"entities": [], "relations": [], "facts": [], "warnings": ["LLM 500"]}

    monkeypatch.setattr(extractor, "_call_llm_once", fake_once)
    result = _run(extractor._call_llm(prompt="текст", model="deepseek-v4-flash",
                                      llm_url="http://primary", chunk_id="c1", pass_name="entities"))

    assert result.get("warnings") == ["LLM 500"]
    assert len(calls) == 1, "без резерва повторять нечего"
