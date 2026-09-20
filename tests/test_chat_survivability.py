"""Живучесть чата: бюджет времени на цепочку провайдеров и синхронные вызовы вне цикла событий.

Зачем отдельный файл: это не про качество ответа, а про то, что при зависшем провайдере или
медленном Neo4j api перестаёт отвечать даже на /health (замер 20.09.2026 — контейнер уходил в
unhealthy, лечился только перезапуском). Тесты стерегут две вещи:
  1) цепочка «основной → резерв» не выходит за общий бюджет времени (таймаут на попытку и
     предел на всю цепочку — разные величины);
  2) синхронные вызовы (Neo4j, чтения БД) выполняются в отдельном потоке, а не в цикле событий.
"""
import asyncio
import threading
import time

from tests.test_chat_context_params import _chunk, _func_map, _provider, _service


def _chain(monkeypatch, service, names=("m1", "m2")):
    """Собрать цепочку из N провайдеров, не трогая настройки стенда."""
    from src.api.services import chat_service as cs

    providers = [(_provider(), name) for name in names]
    monkeypatch.setattr(cs.provider_service, "get_function_provider_chain",
                        lambda *a, **kw: providers)
    return providers


def test_llm_chain_respects_total_budget(monkeypatch):
    """Зависший провайдер не должен держать цепочку дольше бюджета.

    Настройки теста: попытка 1 с, бюджет 2 с. Проверяем ДВЕ вещи без опоры на общее время запроса
    (в него в тестовой среде попадают попытки подключения к БД):
      * каждая попытка получает таймаут не больше заданного;
      * новая попытка не начинается, когда бюджет исчерпан.
    """
    import src.indexing.tables_settings as ts

    monkeypatch.setattr(ts, "get_tables_config", lambda: {"sql_enabled": False})  # не ходить в БД

    captured = {}
    # Попытка 1.5 с, бюджет 2 с: после первой попытки остаётся ~0.5 с — вторая начаться не должна
    # (граница выбрана с запасом, чтобы тест не зависел от долей секунды).
    service = _service(monkeypatch, {"llm_timeout": 1.5, "llm_budget": 2}, [_chunk(0.9)], captured)
    _chain(monkeypatch, service)
    attempts = []

    async def hanging_llm(**kwargs):
        attempts.append(kwargs.get("timeout"))
        await asyncio.sleep(kwargs.get("timeout") or 1.0)     # имитация зависшего провайдера
        return {"id": "1", "content": "", "model": "m", "usage": {}, "elapsed": 1.0,
                "error": "timeout"}

    monkeypatch.setattr(service, "_call_llm", hanging_llm)
    response = asyncio.run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True))

    assert attempts, "вызовы модели не состоялись — тест ничего не проверил"
    assert len(attempts) == 1, (
        f"после исчерпания бюджета попытки продолжаться не должны (было {len(attempts)})"
    )
    assert all(t is not None and t <= 1.5 + 0.01 for t in attempts), \
        f"таймаут попытки должен быть не больше заданного, получено {attempts}"
    assert "Не удалось получить ответ" in response["response"], \
        f"при исчерпании бюджета клиент должен получить понятное сообщение, а не пустоту: {response['response']!r}"


def test_chain_tries_reserve_within_budget(monkeypatch):
    """Если основной провайдер падает быстро, резерв должен быть испробован в рамках бюджета."""
    import src.indexing.tables_settings as ts

    monkeypatch.setattr(ts, "get_tables_config", lambda: {"sql_enabled": False})

    captured = {}
    service = _service(monkeypatch, {"llm_timeout": 1, "llm_budget": 10}, [_chunk(0.9)], captured)
    _chain(monkeypatch, service, names=("main", "reserve"))
    calls = []

    async def llm(**kwargs):
        calls.append(kwargs.get("model"))
        if kwargs.get("model") == "main":
            return {"content": "", "error": "timeout", "elapsed": 0.1, "model": "main", "usage": {}}
        return {"content": "ответ от резерва", "elapsed": 0.1, "model": kwargs.get("model"), "usage": {}}

    monkeypatch.setattr(service, "_call_llm", llm)
    response = asyncio.run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True))

    assert calls[:2] == ["main", "reserve"], f"резерв должен быть испробован: {calls}"
    assert response["response"] == "ответ от резерва"
    assert response["metadata"].get("fallback_used") is True


def test_llm_attempt_timeout_is_configurable(monkeypatch):
    """Таймаут попытки и бюджет цепочки берутся из привязки функции и зажимаются."""
    from src.api.services.chat_service import _cfg_llm_budget, _cfg_llm_timeout

    assert _cfg_llm_timeout({}) == 45.0 and _cfg_llm_budget({}) == 90.0
    assert _cfg_llm_timeout({"llm_timeout": 7}) == 7.0
    assert _cfg_llm_budget({"llm_budget": 30}) == 30.0
    assert _cfg_llm_timeout({"llm_timeout": 0}) == 1.0        # ниже минимума зажимаем
    assert _cfg_llm_timeout({"llm_timeout": 9999}) == 300.0
    assert _cfg_llm_budget({"llm_budget": 0}) == 2.0
    assert _cfg_llm_budget({"llm_budget": 9999}) == 600.0
    assert _cfg_llm_timeout({"llm_timeout": "abc"}) == 45.0   # мусор не ломает


def test_graph_search_runs_off_event_loop(monkeypatch):
    """Синхронный поиск по графу (драйвер Neo4j) должен уходить в отдельный поток.

    Иначе он блокирует цикл событий: пока Neo4j отвечает (или висит), api не отдаёт даже /health.
    """
    import src.indexing.knowledge_graph as kg

    seen = {}

    class _FakeKG:
        @staticmethod
        def hybrid_search(entities, doc_ids=None):
            seen["thread"] = threading.current_thread()
            return []

    monkeypatch.setattr(kg, "kg_service", _FakeKG)
    captured = {}
    service = _service(monkeypatch, {}, [_chunk(0.9)], captured)
    asyncio.run(service.generate_response(user_message="требования по защите", use_rag=True, is_admin=True))

    assert "thread" in seen, "поиск по графу не вызывался — тест ничего не проверил"
    assert seen["thread"] is not threading.main_thread(), \
        "синхронный Neo4j-поиск выполнился в цикле событий — это блокирует api"
