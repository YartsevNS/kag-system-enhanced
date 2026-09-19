"""Цепочка LLM-конфигов для прямых вызовов (анализ документа, граф).

Резерв из привязки функции должен работать не только в чате: обработка документов и граф
ходят к провайдеру напрямую, и падение основного провайдера раньше означало ошибку шага.
"""
def _provider_config(pid, name, models, type_="deepseek", enabled=True, url="http://127.0.0.1:9"):
    from src.api.services.provider_service import ProviderConfig

    return ProviderConfig(id=pid, name=name, type=type_, url=url, api_key="k",
                          models=list(models), enabled=enabled)


def _function_map(provider_id, model, fallback_provider_id="", fallback_model=""):
    from src.api.services.provider_service import FunctionMap

    return FunctionMap(function="doc_analysis", provider_id=provider_id, model=model,
                       system_prompt="промпт функции", parameters={"temperature": 0.1},
                       fallback_provider_id=fallback_provider_id, fallback_model=fallback_model)


def _service(monkeypatch, providers, fm):
    from src.api.services.provider_service import ProviderService

    service = ProviderService()
    monkeypatch.setattr(service, "_load_cache", lambda: None)
    monkeypatch.setattr(service, "load_default_prompt", lambda _f: "")
    service._provider_cache = {p.id: p for p in providers}
    service._function_cache = {fm.function: fm}
    service._cache_loaded = True
    return service


def test_chain_has_only_primary_without_fallback(monkeypatch):
    ps = _service(monkeypatch,
                  [_provider_config("p1", "deepseec", ["deepseek-flash"])],
                  _function_map("p1", "deepseek-flash"))
    chain = ps.get_function_llm_chain("doc_analysis")
    assert len(chain) == 1
    assert chain[0]["provider"] == "deepseek" and chain[0]["model"] == "deepseek-flash"
    assert chain[0]["system_prompt"] == "промпт функции"
    assert chain[0]["parameters"] == {"temperature": 0.1}


def test_chain_includes_fallback_provider(monkeypatch):
    ps = _service(monkeypatch,
                  [_provider_config("p1", "deepseec", ["deepseek-flash"]),
                   _provider_config("p2", "polza.ai", ["z-ai/glm-5.3-flash"], type_="custom",
                                    url="https://polza.ai/api")],
                  _function_map("p1", "deepseek-flash", "p2", "z-ai/glm-5.3-flash"))
    chain = ps.get_function_llm_chain("doc_analysis")
    assert [c["model"] for c in chain] == ["deepseek-flash", "z-ai/glm-5.3-flash"]
    assert chain[1]["url"] == "https://polza.ai/api"
    assert chain[1]["provider"] == "custom"


def test_chain_takes_first_model_of_fallback_provider(monkeypatch):
    ps = _service(monkeypatch,
                  [_provider_config("p1", "deepseec", ["deepseek-flash"]),
                   _provider_config("p2", "polza.ai", ["z-ai/glm-5.3-flash", "другая"], type_="custom")],
                  _function_map("p1", "deepseek-flash", "p2", ""))
    chain = ps.get_function_llm_chain("doc_analysis")
    assert chain[1]["model"] == "z-ai/glm-5.3-flash"


def test_chain_skips_disabled_fallback(monkeypatch):
    ps = _service(monkeypatch,
                  [_provider_config("p1", "deepseec", ["deepseek-flash"]),
                   _provider_config("p2", "off", ["m"], enabled=False)],
                  _function_map("p1", "deepseek-flash", "p2", "m"))
    assert len(ps.get_function_llm_chain("doc_analysis")) == 1


def test_analyzer_plan_tries_fallback_after_primary(monkeypatch):
    """Анализатор документа должен пройти основной провайдер дважды, затем резерв дважды."""
    import asyncio
    from src.api.services import document_analyzer as da

    analyzer = da.DocumentAnalyzer()
    primary = {"provider": "deepseek", "url": "http://primary", "api_key": "k",
               "model": "deepseek-flash", "system_prompt": "п", "parameters": {}}
    reserve = {"provider": "custom", "url": "http://reserve", "api_key": "k",
               "model": "z-ai/glm-5.3-flash", "system_prompt": "п", "parameters": {}}
    from src.api.services.provider_service import provider_service as ps_singleton

    # Патчим метод у самого синглтона: анализатор импортирует его внутри функции,
    # поэтому патч модуля не сработал бы.
    monkeypatch.setattr(ps_singleton, "get_function_llm_chain", lambda _f: [primary, reserve])

    used = []

    class _Resp:
        status = 200

        async def json(self):
            return {"choices": [{"message": {"content": '{"title": "т", "type": "policy", '
                                                       '"summary": "с", "topics": []}'}}]}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, **kw):
            used.append(url)
            if "primary" in url:
                # основной провайдер недоступен на обеих попытках → должен включиться резерв
                raise RuntimeError("primary недоступен")
            return _Resp()

    import sys
    import types

    fake_aiohttp = types.SimpleNamespace(ClientSession=_Session,
                                         ClientTimeout=lambda **kw: None)
    monkeypatch.setitem(sys.modules, "aiohttp", fake_aiohttp)

    result = asyncio.run(analyzer.analyze_document("doc-1", "текст документа " * 20, "f.pdf"))
    assert result.get("recognized_title") == "т", result
    assert any("reserve" in u for u in used), f"резерв не вызывался: {used}"
